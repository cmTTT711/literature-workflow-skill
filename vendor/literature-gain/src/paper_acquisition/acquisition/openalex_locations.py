"""Complete OpenAlex Work lookup and deterministic location enumeration."""

import hashlib
import re
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from ..config import credential
from ..normalization import normalize_doi
from .models import Candidate
from .network import SYNTHETIC_DNS_HOSTS

VERSION = "openalex-locations-1"


def canonical_url(url):
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username:
        return None
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"

    def normalize(match):
        character = chr(int(match[1], 16))
        return (
            character
            if character.isascii() and (character.isalnum() or character in "-._~")
            else "%" + match[1].upper()
        )

    path = re.sub(r"%([0-9a-fA-F]{2})", normalize, parts.path or "/")
    # Query strings (including signatures) are preserved; fragments are not sent by HTTP.
    scheme = parts.scheme.lower()
    if scheme == "http" and parts.hostname.lower() in SYNTHETIC_DNS_HOSTS:
        scheme = "https"  # Match the existing downloader's exact-host HTTPS upgrade.
    return urlunsplit((scheme, host, path, parts.query, ""))


def work_candidates(paper, work, evidence=None, *, normal_requests=True):
    raw = []
    if work.get("doi") and normalize_doi(work["doi"]) != normalize_doi(paper.doi):
        return [], {"doi_mismatch": 1}
    locations = work.get("locations") or []
    best = work.get("best_oa_location") or {}
    best_pdf = canonical_url(best.get("pdf_url"))
    extra_pdfs = set()

    def add(url, origin, loc=None, direct=False, content=False):
        normalized = canonical_url(url)
        if not normalized:
            return
        loc = loc or {}
        source = loc.get("source") or {}
        version = loc.get("version")
        rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}.get(version, 3)
        host_rank = 0 if source.get("type") in {"journal", "publisher"} else 1
        priority = -30 + rank * 2 + host_rank if direct and not content else -10 if content else 0
        raw.append(
            Candidate(
                candidate_id=hashlib.sha256(f"{VERSION}|{paper.paper_id}|{normalized}".encode()).hexdigest(),
                source_type="openalex",
                source_url=normalized,
                license=loc.get("license"),
                version=version,
                resolver_provenance=[
                    {
                        "resolver": VERSION,
                        "work_id": work.get("id"),
                        "doi": paper.doi,
                        "origin": origin,
                        "location": loc,
                        **(evidence or {}),
                    }
                ],
                confidence=0.9,
                reason="DECLARED_OPENALEX_CONTENT_PDF"
                if content
                else "DECLARED_OPENALEX_PDF; normal anonymous request, no license assertion"
                if direct
                else "DECLARED_LOCATION; existing simple landing-page parser only",
                eligible=True,
                direct_pdf=direct,
                priority=priority,
            )
        )

    for origin, loc in [
        ("best_oa_location", best),
        ("primary_location", work.get("primary_location")),
        *((f"locations[{i}]", loc) for i, loc in enumerate(locations)),
    ]:
        if not isinstance(loc, dict):
            continue
        if loc.get("pdf_url"):
            add(loc["pdf_url"], origin, loc, direct=True)
            if origin.startswith("locations[") and canonical_url(loc["pdf_url"]) != best_pdf:
                extra_pdfs.add(canonical_url(loc["pdf_url"]))
        if loc.get("landing_page_url"):
            add(loc["landing_page_url"], origin, loc)
    oa = work.get("open_access") or {}
    if oa.get("oa_url"):
        add(oa["oa_url"], "open_access.oa_url", direct=urlsplit(oa["oa_url"]).path.lower().endswith(".pdf"))
    content = (work.get("content_urls") or {}).get("pdf")
    if content:
        add(content, "content_urls.pdf", direct=True, content=True)
    if not normal_requests:
        from .resolver import candidate

        for item in raw:
            provenance = item.resolver_provenance[0]
            loc = provenance["location"]
            host = (loc.get("source") or {}).get("type")
            if provenance["origin"] == "content_urls.pdf":
                continue
            policy = candidate(
                "openalex",
                item.source_url,
                license=item.license,
                version=item.version,
                oa=loc.get("is_oa") is True,
                repository=host == "repository",
                publisher=host in {"journal", "publisher"},
                direct=item.direct_pdf,
            )
            item.source_type = policy.source_type
            item.eligible = policy.eligible
    unique = {}
    for item in sorted(raw, key=lambda c: (c.priority, c.source_url)):
        if item.source_url in unique:
            unique[item.source_url].resolver_provenance.extend(item.resolver_provenance)
        else:
            unique[item.source_url] = item
    return list(unique.values()), {
        "locations_count": len(locations),
        "multiple_locations": int(len(locations) > 1),
        "best_pdf": int(bool(best_pdf)),
        "locations_extra_pdf": len(extra_pdfs - {None}),
        "content_pdf": int(bool(content)),
        "before_dedup": len(raw),
        "after_dedup": len(unique),
    }


async def fetch_works(papers, clients, *, include_conflicts=False):
    extra = defaultdict(list)
    by_doi = {normalize_doi(p.doi): p for p in papers if normalize_doi(p.doi)}
    transport = clients.transport("openalex")
    headers = (
        {"Authorization": "Bearer " + credential("OPENALEX_API_KEY")}
        if credential("OPENALEX_API_KEY")
        else {}
    )
    dois = sorted(by_doi)
    for start in range(0, len(dois), 100):
        batch = dois[start : start + 100]
        try:
            response = await transport.request(
                "GET",
                "https://api.openalex.org/works",
                params={"filter": "doi:" + "|".join("https://doi.org/" + d for d in batch), "per_page": 100},
                headers=headers,
            )
            payload = response.json()
            records = payload["results"]
            if payload.get("meta", {}).get("count", len(records)) > len(records):
                clients.errors.append({"source": "openalex", "code": "LOOKUP_TRUNCATED", "dois": batch})
            matched = set()
            for record in records:
                doi = normalize_doi(record.get("doi"))
                if doi not in batch:
                    continue
                paper = by_doi[doi]
                matched.add(doi)
                warnings = []
                if record.get("title") and paper.title.casefold() != record["title"].casefold():
                    warnings.append("TITLE_METADATA_DIFFERS")
                extra[paper.paper_id].append(
                    (
                        "openalex",
                        record,
                        {
                            "evidence_id": transport.last_raw_path,
                            "identity_accepted": True,
                            "warnings": warnings,
                        },
                    )
                )
            clients.openalex_missing = getattr(clients, "openalex_missing", []) + sorted(set(batch) - matched)
        except Exception as error:  # noqa: BLE001 -- isolated, bounded optional provider failure
            clients.errors.append({"source": "openalex", "code": type(error).__name__, "dois": batch})
            break
    return extra


async def exhaust_openalex(
    root,
    *,
    refresh=False,
    client_factory=None,
    downloader=None,
    verifier=None,
    paper_ids=None,
    prepare_only=False,
    handoff=True,
    retry_failed=False,
):
    import json
    from collections import Counter

    from ..config import Settings
    from ..locking import RunLock
    from ..master import export_agent, refresh_user_states, user_summary
    from ..storage import Catalog, dumps
    from .service import MetadataClients, acquire, failed_urls, verified_artifact

    root = root.resolve()
    state_path = root / "acquisition" / "openalex-locations-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with RunLock(root / "openalex-locations.lock"):
        catalog = Catalog(root)
        clients = MetadataClients(catalog, Settings(data_dir=root, attempts=2), client_factory)
        try:
            refresh_user_states(catalog)
            before = user_summary(catalog)
            papers = {p.paper_id: p for p in catalog.iter_papers()}
            master = dict(catalog.db.execute("SELECT paper_id,status FROM master_records"))
            selected = {
                pid: p
                for pid, p in papers.items()
                if pid in master
                and not verified_artifact(catalog, pid)
                and (paper_ids is None or pid in paper_ids)
            }
            saved = (
                json.loads(state_path.read_text(encoding="utf-8"))
                if state_path.exists() and not refresh
                else {}
            )
            todo = [p for pid, p in selected.items() if pid not in saved]
            extra = await fetch_works(todo, clients, include_conflicts=True) if todo else {}
            for pid, entries in extra.items():
                saved[pid] = entries
            # Missing records are a recorded lookup outcome, not repeated per-paper requests on rerun.
            for p in todo:
                if normalize_doi(p.doi) in getattr(clients, "openalex_missing", []):
                    saved[p.paper_id] = []
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(dumps(saved), encoding="utf-8")
            temporary.replace(state_path)
            stats = Counter()
            allowed = set()
            candidate_ids = set()
            manifest = []
            new_agent = set()
            all_new_pdf = set()
            attempts_before = catalog.db.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0]
            downloaded_before = catalog.db.execute(
                "SELECT count(*) FROM acquisition_attempts WHERE json_extract(result_json,'$.verification') IS NOT NULL"
            ).fetchone()[0]
            for pid, paper in selected.items():
                entries = saved.get(pid, [])
                if not entries:
                    continue
                stats["works_found"] += 1
                # Preserve historical URL spelling so existing failed_urls/resume policy still applies.
                history = {}
                aliases = defaultdict(list)
                for (raw,) in catalog.db.execute(
                    "SELECT candidate_json FROM access_candidates WHERE paper_id=?", (pid,)
                ):
                    old = json.loads(raw)
                    history.setdefault(canonical_url(old["source_url"]), old["source_url"])
                    if old["source_type"] == "openalex":
                        aliases[canonical_url(old["source_url"])].append(old)
                attempted = {
                    canonical_url(url)
                    for (url,) in catalog.db.execute(
                        "SELECT json_extract(c.candidate_json,'$.source_url') FROM acquisition_attempts a JOIN access_candidates c USING(candidate_id) WHERE a.paper_id=?",
                        (pid,),
                    )
                }
                candidates = {}
                identity_conflict = any(
                    normalize_doi(work.get("doi")) != normalize_doi(paper.doi) for _, work, _ in entries
                )
                stats["identity_conflict_works"] += int(identity_conflict)
                for _, work, evidence in entries:
                    found, counts = work_candidates(paper, work, evidence)
                    stats.update(counts)
                    for item in found:
                        key = canonical_url(item.source_url)
                        if key in candidates:
                            candidates[key].resolver_provenance.extend(item.resolver_provenance)
                        else:
                            candidates[key] = item
                failed = set() if retry_failed else {canonical_url(u) for u in failed_urls(catalog, pid)}
                for key, item in candidates.items():
                    if aliases[key]:
                        item.candidate_id = aliases[key][0]["candidate_id"]
                    # Only the canonical representative stays active. Historical attempts and
                    # staging paths keep their foreign keys and are never erased.
                    for duplicate in aliases[key]:
                        if duplicate["candidate_id"] != item.candidate_id or key in failed:
                            catalog.execute(
                                "UPDATE access_candidates SET eligible=0,candidate_json=json_set(candidate_json,'$.eligible',json('false')) WHERE candidate_id=?",
                                (duplicate["candidate_id"],),
                            )
                    if (
                        item.direct_pdf
                        and key not in attempted
                        and not identity_conflict
                        and master[pid] != "REVIEW"
                    ):
                        all_new_pdf.add((pid, key))
                    if key not in history and master[pid] == "AGENT_REQUIRED":
                        new_agent.add(pid)
                    # A terminal old failure is already exhausted; retain provenance without retrying aliases.
                    if key in failed:
                        stats["previously_failed_urls_skipped"] += 1
                        manifest.append({"paper_id": pid, "url": key, "outcome": "PREVIOUS_TERMINAL_FAILURE"})
                        continue
                    if master[pid] == "REVIEW" or identity_conflict:
                        stats["review_candidates_recorded"] += 1
                        manifest.append(
                            {"paper_id": pid, "url": key, "outcome": "IDENTITY_REVIEW_NOT_ACQUIRED"}
                        )
                        continue
                    item.source_url = history.get(key, item.source_url)
                    catalog.save_candidate(pid, item)
                    allowed.add(pid)
                    candidate_ids.add(item.candidate_id)
                    manifest.append({"paper_id": pid, "url": key, "candidate_id": item.candidate_id})
            requests = clients.report()
            errors = clients.errors
        finally:
            await clients.close()
            catalog.close()
        if allowed and not prepare_only:
            await acquire(
                root,
                limit=len(allowed),
                sources={"openalex"},
                paper_ids=allowed,
                candidate_ids=candidate_ids,
                downloader=downloader,
                verifier=verifier,
                retry_failed=retry_failed,
                exhaust_candidates=True,
            )
        with Catalog(root) as catalog:
            refresh_user_states(catalog)
            after = user_summary(catalog)
            remaining = []
            for entry in manifest:
                pid = entry["paper_id"]
                if entry.get("outcome") or verified_artifact(catalog, pid):
                    continue
                if entry["url"] not in {canonical_url(u) for u in failed_urls(catalog, pid)}:
                    remaining.append(entry)
            report = {
                "phase": "CANDIDATES_PREPARED" if prepare_only else "ACQUISITION_FINISHED",
                "master_total": len(master),
                "queried_dois": len(todo),
                "metadata_reused": len(selected) - len(todo),
                "verified_skipped": len(master) - len(selected),
                **dict(stats),
                "new_unattempted_pdf_candidates": len(all_new_pdf),
                "agent_papers_with_new_candidates": len(new_agent),
                "before": before,
                "after": after,
                "new_downloads": catalog.db.execute(
                    "SELECT count(*) FROM acquisition_attempts WHERE json_extract(result_json,'$.verification') IS NOT NULL"
                ).fetchone()[0]
                - downloaded_before,
                "new_attempts": catalog.db.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0]
                - attempts_before,
                "new_verified": after["PROGRAM_VERIFIED"] - before["PROGRAM_VERIFIED"],
                "remaining_candidates": remaining,
                "requests": requests,
                "errors": errors,
                "missing_work_dois": [selected[pid].doi for pid in selected if not saved.get(pid)],
            }
            report_path = root / "acquisition" / "reports" / "openalex-locations-latest.json"
            report_path.write_text(dumps(report), encoding="utf-8")
            (root / "acquisition" / "reports" / "openalex-locations-manifest.json").write_text(
                dumps(manifest), encoding="utf-8"
            )
        if handoff and not prepare_only and not remaining:
            export_agent(root)
        return report
