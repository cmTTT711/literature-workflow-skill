import asyncio
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..config import Settings, credential
from ..locking import RunLock
from ..models import AcquisitionStatus, now
from ..storage import Catalog, dumps, file_hash, write_new
from .artifacts import promote, verified_artifact
from .layout import initialize_layout
from .metadata import MetadataClients
from .models import AccessStatus, AcquisitionError, AcquisitionOptions, Candidate
from .network import SYNTHETIC_DNS_HOSTS, PublicDownloader
from .resolver import POLICY_VERSION, resolve_metadata
from .verifier import UnifiedPDFVerifier

RESOLVER_VERSION = "all-sources-1"


def transition(catalog, paper, status, detail=None):
    paper.status = AcquisitionStatus(status)
    catalog.save_paper(paper)
    catalog.execute(
        "UPDATE access_resolution SET status=?,updated_at=? WHERE paper_id=?", (status, now(), paper.paper_id)
    )
    catalog.execute(
        "INSERT INTO acquisition_events VALUES(?,?,?,?,?)",
        (uuid4().hex, paper.paper_id, status, now(), dumps(detail or {})),
    )


def initialize_catalog(source_root, target_root):
    """Copy a Discovery catalog including provenance; no change to original master."""
    if (target_root / "catalog.sqlite").exists():
        raise ValueError("ACQUISITION_CATALOG_ALREADY_EXISTS")
    source, target = Catalog(source_root), Catalog(target_root)
    try:
        source.db.backup(target.db)
        for table, field in (("raw_pages", "path"), ("source_records", "raw_path")):
            for rowid, path in target.db.execute(f"SELECT rowid,{field} FROM {table}").fetchall():
                target.execute(
                    f"UPDATE {table} SET {field}=? WHERE rowid=?",
                    (str((source.root / path).resolve()), rowid),
                )
    finally:
        source.close()
        target.close()
    # Run additive schema migration after backup.
    Catalog(target_root).close()
    initialize_layout(target_root)


async def resolve_all(root, *, online=False, refresh=False, client_factory=None, paper_ids=None):
    with RunLock(root / "acquisition.lock"):
        catalog = Catalog(root)
        clients = MetadataClients(catalog, Settings(data_dir=root, attempts=2), client_factory)
        run_id = uuid4().hex
        catalog.execute("INSERT INTO resolution_runs VALUES(?,?,NULL,NULL)", (run_id, now()))
        try:
            papers = catalog.papers()
            if paper_ids is not None:
                papers = [p for p in papers if p.paper_id in paper_ids]
            corrections = []
            for paper in papers:
                if paper.doi and paper.doi.startswith("10.13039/"):
                    paper.external_ids.setdefault("rejected_funder_doi", []).append(paper.doi)
                    paper.doi = None
                    paper.flags.append("FUNDER_DOI_REJECTED_AS_ARTICLE_ID")
                    catalog.save_paper(paper)
                    corrections.append(paper.paper_id)
            existing = {
                pid: json.loads(detail)
                for pid, detail in catalog.db.execute("SELECT paper_id,result_json FROM access_resolution")
            }
            selected = [
                p
                for p in papers
                if refresh
                or p.paper_id not in existing
                or (
                    online
                    and (
                        existing[p.paper_id].get("resolver_version") != RESOLVER_VERSION
                        or not existing[p.paper_id].get("online_requested")
                    )
                )
            ]
            extra = await clients.enrich(selected, online)
            source_payloads = defaultdict(list)
            for record_id, provider, paper_id, payload in catalog.db.execute(
                "SELECT record_id,provider,paper_id,payload_json FROM source_records WHERE paper_id IS NOT NULL"
            ):
                if provider in {"crossref", "openalex"}:
                    source_payloads[paper_id].append(
                        (provider, json.loads(payload), {"source_record": record_id})
                    )
            for paper in selected:
                candidates = resolve_metadata(
                    paper, [*source_payloads[paper.paper_id], *extra[paper.paper_id]]
                )
                # Keep candidates referenced by previous attempts, but deactivate stale policy decisions.
                catalog.execute("UPDATE access_candidates SET eligible=0 WHERE paper_id=?", (paper.paper_id,))
                for c in candidates:
                    catalog.save_candidate(paper.paper_id, c)
                status = (
                    AccessStatus.OA_RESOLVED
                    if any(c.eligible for c in candidates)
                    else AccessStatus.INSTITUTIONAL_REQUIRED
                )
                if any(c.eligible for c in candidates):
                    remaining = catalog.db.execute(
                        "SELECT count(*) FROM access_candidates c WHERE paper_id=? AND eligible=1 AND NOT EXISTS "
                        "(SELECT 1 FROM acquisition_attempts a WHERE a.candidate_id=c.candidate_id AND a.status IN ('DOWNLOAD_FAILED','VERIFY_FAILED'))",
                        (paper.paper_id,),
                    ).fetchone()[0]
                    if not remaining:
                        status = AccessStatus.INSTITUTIONAL_REQUIRED
                if catalog.db.execute(
                    "SELECT 1 FROM artifacts WHERE paper_id=? AND status='VERIFIED'", (paper.paper_id,)
                ).fetchone():
                    status = AccessStatus.VERIFIED
                detail = {
                    "policy": POLICY_VERSION,
                    "resolver_version": RESOLVER_VERSION,
                    "candidates": len(candidates),
                    "eligible": sum(c.eligible for c in candidates),
                    "online_requested": online,
                    "reason": "OPEN_CANDIDATE_FOUND"
                    if any(c.eligible for c in candidates)
                    else "NO_CONFIRMED_PROGRAMMATIC_OA; not proof of paywall",
                }
                catalog.execute(
                    "INSERT INTO access_resolution VALUES(?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET "
                    "run_id=excluded.run_id,status=excluded.status,updated_at=excluded.updated_at,result_json=excluded.result_json",
                    (paper.paper_id, run_id, status, now(), dumps(detail)),
                )
                transition(catalog, paper, status, detail)
            report = summary(catalog)
            report.update(
                corrected_funder_doi_papers=corrections,
                run_id=run_id,
                processed=len(selected),
                resumed_skipped=len(papers) - len(selected),
                metadata_requests=clients.report(),
                resolver_errors=clients.errors,
                source_availability={
                    "unpaywall": "CONFIGURED"
                    if credential("UNPAYWALL_EMAIL") or credential("PAPER_CONTACT_EMAIL")
                    else "WAITING_FOR_CONTACT_EMAIL",
                    "core": "METADATA_ONLY; LIVE_API_NOT_IMPLEMENTED",
                    "openaire": "ONLINE_AND_DISCOVERY_METADATA" if online else "DISCOVERY_METADATA_REPLAY",
                    "semantic_scholar": "DISCOVERY_METADATA_REPLAY",
                    "arxiv": "IDENTIFIER_RESOLUTION",
                },
            )
            catalog.execute(
                "UPDATE resolution_runs SET finished_at=?,report_json=? WHERE run_id=?",
                (now(), dumps(report), run_id),
            )
            write_new(root / "acquisition" / "reports" / f"resolve-{run_id}.json", dumps(report))
            return report
        finally:
            await clients.close()
            catalog.close()


def summary(catalog):
    statuses = dict(catalog.db.execute("SELECT status,count(*) FROM access_resolution GROUP BY status"))
    per_source = {}
    for source, payload in catalog.db.execute("SELECT candidate_json,eligible FROM access_candidates"):
        c = Candidate.model_validate_json(source)
        entry = per_source.setdefault(c.source_type, {"candidates": 0, "eligible_candidates": 0})
        entry["candidates"] += 1
        entry["eligible_candidates"] += payload
    for source, entry in per_source.items():
        entry["papers"] = catalog.db.execute(
            "SELECT count(DISTINCT paper_id) FROM access_candidates WHERE eligible=1 AND json_extract(candidate_json,'$.source_type')=?",
            (source,),
        ).fetchone()[0]
    verified = catalog.db.execute(
        "SELECT count(DISTINCT paper_id) FROM artifacts WHERE status='VERIFIED'"
    ).fetchone()[0]
    return {
        "total_papers": catalog.db.execute("SELECT count(*) FROM papers").fetchone()[0],
        "oa_resolved": catalog.db.execute(
            "SELECT count(DISTINCT paper_id) FROM access_candidates WHERE eligible=1"
        ).fetchone()[0],
        "program_downloaded": catalog.db.execute(
            "SELECT count(DISTINCT paper_id) FROM acquisition_attempts WHERE status IN ('VERIFIED','VERIFY_FAILED','PROGRAM_DOWNLOADED_UNVERIFIED')"
        ).fetchone()[0],
        "verified": verified,
        "institutional_required": statuses.get("INSTITUTIONAL_REQUIRED", 0),
        "unresolved": catalog.db.execute(
            "SELECT count(*) FROM papers WHERE paper_id NOT IN (SELECT paper_id FROM access_resolution)"
        ).fetchone()[0],
        "failed": catalog.db.execute(
            "SELECT count(DISTINCT paper_id) FROM acquisition_attempts WHERE status IN ('DOWNLOAD_FAILED','VERIFY_FAILED') AND paper_id NOT IN (SELECT paper_id FROM artifacts WHERE status='VERIFIED')"
        ).fetchone()[0],
        "status_counts": statuses,
        "by_source": per_source,
    }


def failed_urls(catalog, paper_id):
    latest = {}
    for url, status, detail in catalog.db.execute(
        "SELECT json_extract(c.candidate_json,'$.source_url'),a.status,a.result_json "
        "FROM acquisition_attempts a JOIN access_candidates c USING(candidate_id) "
        "WHERE a.paper_id=? ORDER BY a.rowid",
        (paper_id,),
    ):
        latest[url] = (status, json.loads(detail))
    result = set()
    for url, (status, detail) in latest.items():
        obsolete_preflight = (
            detail.get("error", {}).get("code") == "NONPUBLIC_ADDRESS"
            and urlsplit(url).hostname in SYNTHETIC_DNS_HOSTS
            and detail.get("network_policy") != "exact-synthetic-dns-1"
        )
        obsolete_pool = not detail.get("pool_recovery") and any(
            event.get("error") == "PoolTimeout" and not event.get("pool_recreated")
            for event in detail.get("trace", [])
        )
        if status in {"DOWNLOAD_FAILED", "VERIFY_FAILED"} and not (obsolete_preflight or obsolete_pool):
            result.add(url)
    return result


async def acquire(
    root,
    *,
    limit=10,
    options=None,
    retry_failed=False,
    sources=None,
    downloader=None,
    verifier=None,
    paper_ids=None,
    candidate_ids=None,
    exhaust_candidates=False,
):
    root = root.resolve()
    options = options or AcquisitionOptions()
    with RunLock(root / "acquisition.lock"), RunLock(Path("workspace/coordination/program.lock")):
        catalog = Catalog(root)
        initialize_layout(root)
        downloader = downloader or PublicDownloader(root, options)
        verifier = verifier or UnifiedPDFVerifier(options)
        report_id = uuid4().hex
        selected_count = 0
        outcomes = []
        try:
            for paper in catalog.iter_papers():
                if paper_ids is not None and paper.paper_id not in paper_ids:
                    continue
                if selected_count >= limit:
                    break
                if verified_artifact(catalog, paper.paper_id):
                    continue
                raw = catalog.db.execute(
                    "SELECT candidate_json FROM access_candidates WHERE paper_id=? AND eligible=1 "
                    "ORDER BY priority,json_extract(candidate_json,'$.direct_pdf') DESC,"
                    "json_extract(candidate_json,'$.source_url'),candidate_id",
                    (paper.paper_id,),
                ).fetchall()
                candidates = [Candidate.model_validate_json(r[0]) for r in raw]
                if candidate_ids is not None:
                    candidates = [c for c in candidates if c.candidate_id in candidate_ids]
                if sources:
                    candidates = [c for c in candidates if c.source_type in sources]
                if not candidates:
                    continue
                attempted = False
                used_urls = set() if retry_failed else failed_urls(catalog, paper.paper_id)
                considered = 0
                for candidate in candidates:
                    if candidate.source_url in used_urls:
                        continue
                    used_urls.add(candidate.source_url)
                    last = catalog.db.execute(
                        "SELECT attempt_id,status,staging_path,result_json FROM acquisition_attempts WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
                        (candidate.candidate_id,),
                    ).fetchone()
                    if not exhaust_candidates and considered >= options.max_candidates:
                        break
                    considered += 1
                    attempted = True
                    if (
                        last
                        and last[1] in {"PROGRAM_DOWNLOAD_PENDING", "PROGRAM_DOWNLOADED_UNVERIFIED"}
                        and (root / last[2]).is_file()
                    ):
                        attempt_id, _, path, previous = last
                        staging = root / path
                        detail = {**json.loads(previous), "resumed_staging": True}
                    else:
                        if last and last[1] in {"PROGRAM_DOWNLOAD_PENDING", "PROGRAM_DOWNLOADED_UNVERIFIED"}:
                            catalog.execute(
                                "UPDATE acquisition_attempts SET status='DOWNLOAD_FAILED',finished_at=?,result_json=? WHERE attempt_id=?",
                                (now(), dumps({"error": {"code": "INTERRUPTED_BEFORE_STAGING"}}), last[0]),
                            )
                        attempt_id = uuid4().hex
                        staging = root / "downloads" / "program" / "staging" / f"{attempt_id}.pdf"
                        catalog.execute(
                            "INSERT INTO acquisition_attempts VALUES(?,?,?,?,NULL,?,?,?)",
                            (
                                attempt_id,
                                paper.paper_id,
                                candidate.candidate_id,
                                now(),
                                "PROGRAM_DOWNLOAD_PENDING",
                                str(staging.relative_to(root)),
                                "{}",
                            ),
                        )
                        transition(
                            catalog, paper, AccessStatus.PROGRAM_DOWNLOAD_PENDING, {"attempt_id": attempt_id}
                        )
                        try:
                            reuse = catalog.db.execute(
                                "SELECT a.path,a.sha256 FROM artifacts a JOIN acquisition_attempts t ON a.attempt_id=t.attempt_id "
                                "JOIN access_candidates c ON c.candidate_id=t.candidate_id WHERE a.status='VERIFIED' "
                                "AND json_extract(c.candidate_json,'$.source_url')=?",
                                (candidate.source_url,),
                            ).fetchall()
                            reusable = next(
                                (
                                    (root / p).resolve()
                                    for p, digest in reuse
                                    if (root / p).resolve().is_file()
                                    and file_hash((root / p).resolve())
                                    == digest
                                ),
                                None,
                            )
                            if reusable:
                                write_new(staging, reusable.read_bytes())
                                detail = {
                                    "reused_verified_file": str(reusable.relative_to(root)),
                                    "trace": [],
                                }
                            else:
                                detail = await downloader.download(candidate, staging)
                        except (AcquisitionError, OSError, ValueError, httpx.RequestError) as caught:
                            error = (
                                caught
                                if isinstance(caught, AcquisitionError)
                                else AcquisitionError(type(caught).__name__)
                            )
                            detail = {
                                "error": error.as_dict(),
                                "trace": downloader.trace,
                                "network_policy": "exact-synthetic-dns-1",
                                "pool_recovery": 1,
                            }
                            catalog.execute(
                                "UPDATE acquisition_attempts SET status='DOWNLOAD_FAILED',finished_at=?,result_json=? WHERE attempt_id=?",
                                (now(), dumps(detail), attempt_id),
                            )
                            transition(catalog, paper, AccessStatus.DOWNLOAD_FAILED, detail)
                            outcomes.append(
                                {
                                    "paper_id": paper.paper_id,
                                    "source": candidate.source_type,
                                    "status": "DOWNLOAD_FAILED",
                                    "code": error.code,
                                }
                            )
                            continue
                    catalog.execute(
                        "UPDATE acquisition_attempts SET status='PROGRAM_DOWNLOADED_UNVERIFIED',result_json=? WHERE attempt_id=?",
                        (dumps(detail), attempt_id),
                    )
                    transition(
                        catalog, paper, AccessStatus.PROGRAM_DOWNLOADED_UNVERIFIED, {"attempt_id": attempt_id}
                    )
                    result = await asyncio.to_thread(verifier.verify, staging, paper)
                    detail["verification"] = result.model_dump()
                    status = result.status
                    if status == "VERIFIED":
                        try:
                            detail.update(promote(catalog, paper, attempt_id, staging, candidate, result))
                        except (AcquisitionError, OSError) as caught:
                            error = (
                                caught
                                if isinstance(caught, AcquisitionError)
                                else AcquisitionError(type(caught).__name__)
                            )
                            status = "VERIFY_FAILED"
                            detail["error"] = error.as_dict()
                    catalog.execute(
                        "UPDATE acquisition_attempts SET status=?,finished_at=?,result_json=? WHERE attempt_id=?",
                        (status, now(), dumps(detail), attempt_id),
                    )
                    transition(catalog, paper, status, {"attempt_id": attempt_id, "reasons": result.reasons})
                    outcomes.append(
                        {"paper_id": paper.paper_id, "source": candidate.source_type, "status": status}
                    )
                    if status == "VERIFIED":
                        # The verified artifact is durable and indexed before removing its transient copy.
                        if staging.resolve().is_relative_to(root / "downloads/program/staging"):
                            staging.unlink(missing_ok=True)
                        break
                if attempted:
                    selected_count += 1
                    if not verified_artifact(catalog, paper.paper_id):
                        # Unattempted alternates remain eligible for a later bounded run.
                        remaining = catalog.db.execute(
                            "SELECT count(*) FROM access_candidates c WHERE paper_id=? AND eligible=1 AND NOT EXISTS (SELECT 1 FROM acquisition_attempts a WHERE a.candidate_id=c.candidate_id)",
                            (paper.paper_id,),
                        ).fetchone()[0]
                        transition(
                            catalog,
                            paper,
                            AccessStatus.OA_RESOLVED if remaining else AccessStatus.INSTITUTIONAL_REQUIRED,
                            {
                                "reason": "ALTERNATES_PENDING"
                                if remaining
                                else "PROGRAMMATIC_CANDIDATES_EXHAUSTED"
                            },
                        )
            report = summary(catalog)
            report.update(run_id=report_id, sample_papers=selected_count, outcomes=outcomes)
            write_new(root / "acquisition" / "reports" / f"acquire-{report_id}.json", dumps(report))
            return report
        finally:
            await downloader.close()
            catalog.close()


def acquisition_audit(root):
    from ..library.store import library_root

    with Catalog(root) as catalog:
        report = summary(catalog)
        errors = []
        for row in catalog.db.execute("PRAGMA foreign_key_check"):
            errors.append({"code": "FOREIGN_KEY_ERROR", "table": row[0]})
        for evidence_id, path, digest in catalog.db.execute(
            "SELECT evidence_id,path,sha256 FROM resolver_evidence"
        ):
            file = (catalog.root / path).resolve()
            if not file.is_file() or file_hash(file) != digest:
                errors.append({"code": "RESOLVER_EVIDENCE_HASH", "evidence_id": evidence_id})
        for artifact_id, paper_id, path, digest, verification in catalog.db.execute(
            "SELECT artifact_id,paper_id,path,sha256,verification_json FROM artifacts WHERE status='VERIFIED'"
        ):
            file = (catalog.root / path).resolve()
            shared = library_root(catalog.root)
            allowed = file.resolve().is_relative_to(catalog.root / "downloads" / "program") or (
                shared is not None and file.resolve().is_relative_to(shared / "pdf")
            )
            if (
                not allowed
                or "staging" in file.parts
                or not file.is_file()
                or file_hash(file) != digest
                or json.loads(verification).get("status") != "VERIFIED"
            ):
                errors.append(
                    {"code": "VERIFIED_ARTIFACT_INVALID", "artifact_id": artifact_id, "paper_id": paper_id}
                )
        report.update(integrity=catalog.db.execute("PRAGMA integrity_check").fetchone()[0], errors=errors)
        report["verified_by_source"] = dict(
            catalog.db.execute(
                "SELECT json_extract(c.candidate_json,'$.source_type'),count(DISTINCT a.paper_id) FROM artifacts a "
                "JOIN acquisition_attempts t ON t.attempt_id=a.attempt_id JOIN access_candidates c ON c.candidate_id=t.candidate_id "
                "WHERE a.status='VERIFIED' GROUP BY 1"
            )
        )
        return report
