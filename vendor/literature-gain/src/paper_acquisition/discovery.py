import asyncio
import hashlib
import json
import logging
from uuid import uuid4

from .acquisition.layout import initialize_layout
from .cache import ResponseCache
from .dedup import Deduplicator, merge
from .errors import DocumentationBlocked, MissingCredentials
from .http import Transport
from .locking import RunLock
from .models import ProviderResult, RawRecord, SearchProfile, now
from .normalization import finalize, title_key
from .providers import registry
from .providers.base import keyword_terms
from .storage import Catalog, dumps, write_new


async def discover(profile, settings, *, adapters=None, client_factory=None, parent_run_id=None):
    with RunLock(settings.data_dir / "discovery.lock"):
        return await _discover(
            profile, settings, adapters=adapters, client_factory=client_factory, parent_run_id=parent_run_id
        )


def _matches_required_title_abstract(paper, profile, query):
    if not profile.extensions.get("require_terms_in_title_or_abstract", False):
        return True
    searchable = title_key(f"{paper.title} {paper.abstract or ''}")
    required = [*keyword_terms(query), *profile.include_terms]
    return all(title_key(term) in searchable for term in required)


async def _discover(profile, settings, *, adapters=None, client_factory=None, parent_run_id=None):
    available = adapters or registry()
    unknown = set(settings.providers) - set(available)
    if unknown:
        raise ValueError("Unknown providers: " + ", ".join(sorted(unknown)))
    with Catalog(settings.data_dir) as catalog:
        initialize_layout(settings.data_dir)
        for name in settings.providers:
            (settings.data_dir / "discovery" / "raw" / name).mkdir(parents=True, exist_ok=True)
        run_id = catalog.start_run(profile)
        dedup = Deduplicator(catalog.papers())
        fingerprints = {}
        papers_by_id = {p.paper_id: p for p in dedup.papers}
        for provider, payload, paper_id in catalog.db.execute(
            "SELECT provider,payload_json,paper_id FROM source_records WHERE paper_id IS NOT NULL"
        ):
            fingerprints[(provider, hashlib.sha256(payload.encode()).hexdigest())] = paper_id

        async def worker(name):
            provider_class = available[name]
            query_id = ""

            def observe(request, response, attempt):
                return catalog.raw_response(run_id, name, query_id, request, response, attempt)

            transport = Transport(
                settings,
                settings.arxiv_interval if name == "arxiv" else provider_class.interval,
                observe,
                client_factory(name) if client_factory else None,
                cache=ResponseCache(
                    settings.data_dir / "cache" / name, min(settings.cache_ttl_seconds, provider_class.cache_ttl)
                )
                if not client_factory
                else None,
                provider=name,
            )
            adapter = provider_class(transport)
            count, statuses, warnings, errors = 0, [], [], []
            normalized_count, normalization_failures = 0, 0
            raw_count = 0
            tasks = list(profile.search_tasks())
            try:
                for variant_index, (task_profile, query) in enumerate(tasks):
                    query_id = catalog.query(run_id, name, query)
                    status, local_count, total = "RUNNING", 0, None
                    error_name = None
                    local_errors, notes = [], []
                    try:
                        adapter.check()
                        translated = adapter.translate(task_profile, query)
                        notes = adapter.warnings(task_profile)
                        warnings.extend(notes)
                        catalog.execute(
                            "UPDATE provider_queries SET executed_json=?,warnings_json=? WHERE query_id=?",
                            (dumps(translated), dumps(notes), query_id),
                        )
                        cursor = adapter.first_cursor
                        seen_pages = set()
                        # Equal budgets among variants prevent the first query starving all the others.
                        variants_left = len(tasks) - variant_index
                        budget = max(0, (profile.max_results_per_provider - count) // variants_left)
                        if budget == 0:
                            status = "TRUNCATED"
                        else:
                            for _ in range(settings.max_pages):
                                size = min(settings.page_size, budget)
                                page = await adapter.fetch(translated, cursor, size)
                                total = page.total
                                raw_count += len(page.records)
                                catalog.execute(
                                    "INSERT INTO query_progress VALUES(?,?,?,?,?,?) ON CONFLICT(query_id) "
                                    "DO UPDATE SET cursor_json=excluded.cursor_json,page_size=excluded.page_size,"
                                    "received=excluded.received,updated_at=excluded.updated_at,state=excluded.state",
                                    (
                                        query_id,
                                        dumps(page.next_cursor),
                                        size,
                                        local_count + len(page.records),
                                        now(),
                                        "RAW_SAVED",
                                    ),
                                )
                                warnings.extend(page.warnings)
                                notes.extend(page.warnings)
                                page_hash = hashlib.sha256(dumps(page.records).encode()).hexdigest()
                                if page.records and page_hash in seen_pages:
                                    status = "TRUNCATED"
                                    warnings.append("REPEATED_PAGE")
                                    notes.append("REPEATED_PAGE")
                                    break
                                seen_pages.add(page_hash)
                                for ordinal, payload in enumerate(page.records[: budget - local_count]):
                                    raw = RawRecord(
                                        record_id=uuid4().hex,
                                        provider=name,
                                        query_id=query_id,
                                        raw_path=transport.last_raw_path,
                                        ordinal=ordinal,
                                        payload=payload,
                                    )
                                    payload_json = dumps(payload)
                                    catalog.execute(
                                        "INSERT INTO source_records VALUES(?,?,?,?,?,?,NULL,NULL)",
                                        (raw.record_id, query_id, name, raw.raw_path, ordinal, payload_json),
                                    )
                                    try:
                                        paper = finalize(adapter.normalize(payload))
                                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                                        catalog.execute(
                                            "UPDATE source_records SET normalization_error=? WHERE record_id=?",
                                            (type(error).__name__, raw.record_id),
                                        )
                                        local_errors.append("NORMALIZATION_" + type(error).__name__)
                                        normalization_failures += 1
                                    else:
                                        if not _matches_required_title_abstract(paper, task_profile, query):
                                            catalog.execute(
                                                "UPDATE source_records SET normalization_error=? WHERE record_id=?",
                                                ("TITLE_ABSTRACT_FILTERED", raw.record_id),
                                            )
                                            local_count += 1
                                            count += 1
                                            continue
                                        paper.source_hits, paper.provenance = [name], [raw.record_id]
                                        signature = (name, hashlib.sha256(payload_json.encode()).hexdigest())
                                        existing = papers_by_id.get(fingerprints.get(signature))
                                        canonical = merge(existing, paper) if existing else dedup.add(paper)
                                        dedup.refresh(canonical)
                                        papers_by_id[canonical.paper_id] = canonical
                                        fingerprints[signature] = canonical.paper_id
                                        catalog.save_paper(canonical)
                                        catalog.execute(
                                            "UPDATE source_records SET paper_id=? WHERE record_id=?",
                                            (canonical.paper_id, raw.record_id),
                                        )
                                        normalized_count += 1
                                    local_count += 1
                                    count += 1
                                incomplete_total = total is not None and local_count < total
                                exhausted = (
                                    not page.records
                                    or page.next_cursor is None
                                    or (total is not None and local_count >= total)
                                )
                                if exhausted and incomplete_total:
                                    status = "TRUNCATED"
                                    notes.append("REMOTE_TOTAL_NOT_EXHAUSTED")
                                    warnings.append("REMOTE_TOTAL_NOT_EXHAUSTED")
                                    break
                                if exhausted and len(page.records) <= size:
                                    status = "SUCCESS"
                                    break
                                if local_count >= budget:
                                    status = "TRUNCATED"
                                    break
                                if exhausted:
                                    status = "SUCCESS"
                                    break
                                if (page.next_cursor == cursor and not adapter.allow_repeated_cursor) or (
                                    adapter.result_ceiling and local_count >= adapter.result_ceiling
                                ):
                                    status = "TRUNCATED"
                                    warnings.append("CURSOR_STALLED_OR_PROVIDER_CEILING")
                                    notes.append("CURSOR_STALLED_OR_PROVIDER_CEILING")
                                    break
                                cursor = page.next_cursor
                            else:
                                status = "TRUNCATED"
                    except (MissingCredentials, DocumentationBlocked) as error:
                        status, error_name = "SKIPPED", str(error)
                    except Exception as error:  # noqa: BLE001 -- provider isolation is an explicit boundary
                        status, error_name = ("PARTIAL" if local_count else "FAILED"), type(error).__name__
                        errors.append(error_name)
                    if local_errors:
                        errors.extend(local_errors)
                        status = "PARTIAL"
                        error_name = ", ".join(sorted(set(local_errors)))
                    catalog.execute(
                        "UPDATE provider_queries SET finished_at=?,status=?,total=?,records=?,error=?,warnings_json=? "
                        "WHERE query_id=?",
                        (now(), status, total, local_count, error_name, dumps(notes), query_id),
                    )
                    statuses.append(status)
                    catalog.execute(
                        "UPDATE query_progress SET state=?,updated_at=? WHERE query_id=?",
                        (status, now(), query_id),
                    )
                if errors:
                    final_status = "PARTIAL" if count else "FAILED"
                elif all(s == "SKIPPED" for s in statuses):
                    final_status = "SKIPPED"
                elif "TRUNCATED" in statuses:
                    final_status = "TRUNCATED"
                else:
                    final_status = "SUCCESS"
                result = ProviderResult(
                    provider=name,
                    status=final_status,
                    records=count,
                    raw_returned_records=raw_count,
                    normalized_records=normalized_count,
                    normalization_failures=normalization_failures,
                    access_status=(
                        "WAITING_FOR_CREDENTIALS"
                        if final_status == "SKIPPED" and not adapter.key
                        else transport.access_status
                        or (
                            "ANONYMOUS_MODE"
                            if name == "semantic_scholar" and not adapter.key and normalized_count
                            else "LIVE_OK"
                            if normalized_count
                            else "PROVIDER_ERROR"
                        )
                    ),
                    authentication="authenticated" if adapter.key else "anonymous",
                    http_status=transport.last_status,
                    network_requests=transport.requests,
                    cache_hits=transport.cache_hits,
                    rate_limit=transport.rate_headers,
                    error=", ".join(sorted(set(errors))) or error_name,
                    warnings=list(dict.fromkeys(warnings)),
                )
                if normalized_count and transport.requests == 0 and transport.cache_hits:
                    result.access_status = "CACHED_OK"
                logging.getLogger("paper_acquisition").info(
                    dumps({"event": "provider_complete", **result.model_dump()})
                )
                return result
            finally:
                await transport.close()

        results = await asyncio.gather(*(worker(name) for name in dict.fromkeys(settings.providers)))
        for paper in dedup.papers:
            catalog.save_paper(paper)
        report = {
            "run_id": run_id,
            "providers": [r.model_dump() for r in results],
            "master_papers": len(dedup.papers),
            "parent_run_id": parent_run_id,
            "recovery_mode": "safe_replay" if parent_run_id else None,
            "dedup_comparisons": dedup.comparisons,
        }
        states = {r.status for r in results}
        report["status"] = (
            "SUCCESS"
            if states == {"SUCCESS"}
            else "NO_DATA"
            if not any(r.records for r in results) and "SUCCESS" not in states
            else "PARTIAL"
        )
        export_dir = catalog.export()
        report["export_dir"] = str(export_dir)
        catalog.execute(
            "UPDATE search_runs SET finished_at=?,status=?,report_json=? WHERE run_id=?",
            (now(), report["status"], dumps(report), run_id),
        )
        write_new(settings.data_dir / "runs" / run_id / "report.json", dumps(report))
        return report


def rebuild(settings):
    """Replay preserved records into an independent derived catalog; keep original intact."""
    source = Catalog(settings.data_dir)
    destination = settings.data_dir / "rebuilds" / uuid4().hex
    target = Catalog(destination)
    try:
        source.db.backup(target.db)
        with target.db:
            target.db.execute("UPDATE source_records SET paper_id=NULL,normalization_error=NULL")
            if target.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks' AND type='table'"
            ).fetchone():
                target.db.execute("DELETE FROM acquisition_tasks")
            target.db.execute("DELETE FROM papers")
        dedup = Deduplicator([])
        fingerprints = {}
        available = registry(include_metadata_sources=True)
        for record_id, provider, payload, profile_json, query in target.db.execute(
            "SELECT s.record_id,s.provider,s.payload_json,r.profile_json,q.user_query "
            "FROM source_records s JOIN provider_queries q USING(query_id) "
            "JOIN search_runs r USING(run_id) ORDER BY s.rowid"
        ).fetchall():
            try:
                paper = finalize(available[provider](None).normalize(json.loads(payload)))
                if not _matches_required_title_abstract(
                    paper, SearchProfile.model_validate_json(profile_json), query
                ):
                    target.execute(
                        "UPDATE source_records SET normalization_error=? WHERE record_id=?",
                        ("TITLE_ABSTRACT_FILTERED", record_id),
                    )
                    continue
                paper.source_hits, paper.provenance = [provider], [record_id]
                signature = (provider, hashlib.sha256(payload.encode()).hexdigest())
                canonical = (
                    merge(fingerprints[signature], paper) if signature in fingerprints else dedup.add(paper)
                )
                fingerprints[signature] = canonical
                dedup.refresh(canonical)
                target.save_paper(canonical)
                target.execute(
                    "UPDATE source_records SET paper_id=? WHERE record_id=?", (canonical.paper_id, record_id)
                )
            except Exception as error:  # noqa: BLE001 -- preserve individual failures during replay
                target.execute(
                    "UPDATE source_records SET normalization_error=? WHERE record_id=?",
                    (type(error).__name__, record_id),
                )
        for paper in dedup.papers:
            target.save_paper(paper)
        # Raw paths continue pointing to the original immutable archive.
        for table in ("raw_pages", "source_records"):
            field = "path" if table == "raw_pages" else "raw_path"
            for rowid, path in target.db.execute(f"SELECT rowid,{field} FROM {table}").fetchall():
                target.execute(
                    f"UPDATE {table} SET {field}=? WHERE rowid=?",
                    (str((source.root / path).resolve()), rowid),
                )
        return target.export()
    finally:
        source.close()
        target.close()
