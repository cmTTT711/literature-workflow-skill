"""Narrow replay of pre-HTTP Content failures and former DOI/title blockers."""

import json
from collections import Counter
from urllib.parse import urlsplit

from ..locking import RunLock
from ..master import export_agent, prepare_master, refresh_user_states, user_summary
from ..models import now
from ..normalization import normalize_doi
from ..storage import Catalog, dumps
from .network import ContentAuth, PublicDownloader  # noqa: F401 -- preserve public import
from .openalex_locations import canonical_url, work_candidates
from .service import acquire, failed_urls, verified_artifact


def content_downloader(root):
    return PublicDownloader(root)


def content_targets(catalog):
    latest = {}
    for cid, pid, raw, status, detail in catalog.db.execute(
        "SELECT a.candidate_id,a.paper_id,c.candidate_json,a.status,a.result_json "
        "FROM acquisition_attempts a JOIN access_candidates c USING(candidate_id) ORDER BY a.rowid"
    ):
        candidate = json.loads(raw)
        if urlsplit(candidate["source_url"]).hostname == "content.openalex.org":
            latest[(pid, canonical_url(candidate["source_url"]))] = (cid, status, json.loads(detail))
    return [
        cid
        for (pid, _), (cid, status, detail) in latest.items()
        if status == "DOWNLOAD_FAILED"
        and detail.get("error", {}).get("code") == "NONPUBLIC_ADDRESS"
        and not detail.get("trace")
        and not verified_artifact(catalog, pid)
    ]


async def retry_blocked(root, *, downloader_factory=content_downloader):
    root = root.resolve()
    folder = root / "acquisition"
    folder.mkdir(parents=True, exist_ok=True)
    plan_path = folder / "openalex-blocked-plan.json"
    report_path = folder / "reports/openalex-blocked-result.json"
    status_path = folder / "OPENALEX_RETRY_STATUS.txt"
    status_path.write_text("RUNNING\nLocal Python only; no model calls.\n", encoding="utf-8")
    with RunLock(root / "openalex-blocked.lock"):
        with Catalog(root) as c:
            if plan_path.exists():
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            else:
                state = json.loads((folder / "openalex-locations-state.json").read_text(encoding="utf-8"))
                old = {
                    pid: entries
                    for pid, entries in state.items()
                    if any(e.get("identity_accepted") is False for _, _, e in entries)
                }
                plan = {
                    "before": user_summary(c),
                    "start": now(),
                    "attempt_rowid": c.db.execute(
                        "SELECT coalesce(max(rowid),0) FROM acquisition_attempts"
                    ).fetchone()[0],
                    "content_candidates": content_targets(c),
                    "title_works": old,
                }
                plan_path.write_text(dumps(plan), encoding="utf-8")
        admission = await prepare_master(root, online=False, update_candidates=False)
        if "doi_source_audit" not in plan:
            plan["doi_source_audit"] = admission["doi_source_audit"]
            plan_path.write_text(dumps(plan), encoding="utf-8")
        with Catalog(root) as c:
            master_ids = {pid for (pid,) in c.db.execute("SELECT paper_id FROM master_records")}
            master = {p.paper_id: p for p in c.iter_papers() if p.paper_id in master_ids}
            blocked = set(content_targets(c))
            planned = set(plan["content_candidates"])
            retry_ids = set()
            # Resume interrupted requests from this plan without replaying terminal HTTP/verification outcomes.
            for cid, pid, status in c.db.execute(
                "SELECT c.candidate_id,c.paper_id,(SELECT a.status FROM acquisition_attempts a "
                "WHERE a.candidate_id=c.candidate_id ORDER BY a.rowid DESC LIMIT 1) FROM access_candidates c"
            ):
                if (
                    cid in planned
                    and pid in master
                    and not verified_artifact(c, pid)
                    and (
                        cid in blocked
                        or status in {"PROGRAM_DOWNLOAD_PENDING", "PROGRAM_DOWNLOADED_UNVERIFIED"}
                    )
                ):
                    retry_ids.add(cid)
            for cid in retry_ids:
                c.execute(
                    "UPDATE access_candidates SET eligible=1,candidate_json=json_set(candidate_json,'$.eligible',json('true')) WHERE candidate_id=?",
                    (cid,),
                )
            title_ids = set()
            admitted = []
            excluded = []
            for pid, entries in plan["title_works"].items():
                if pid not in master:
                    excluded.append({"paper_id": pid, "reason": "NOT_IN_DOI_MASTER"})
                    continue
                paper = master[pid]
                matched = [
                    (w, e)
                    for _, w, e in entries
                    if normalize_doi(w.get("doi")) == normalize_doi(paper.doi) and normalize_doi(paper.doi)
                ]
                if not matched:
                    excluded.append({"paper_id": pid, "reason": "WORK_DOI_MISMATCH"})
                    continue
                admitted.append(pid)
                if verified_artifact(c, pid):
                    continue
                prior = {canonical_url(u) for u in failed_urls(c, pid)}
                for work, evidence in matched:
                    evidence = {**evidence, "identity_accepted": True, "warnings": ["TITLE_METADATA_DIFFERS"]}
                    candidates, _ = work_candidates(paper, work, evidence)
                    for item in candidates:
                        key = canonical_url(item.source_url)
                        if key in prior:
                            continue
                        c.save_candidate(pid, item)
                        title_ids.add(item.candidate_id)
        if retry_ids:
            await acquire(
                root,
                limit=len(master),
                candidate_ids=retry_ids,
                retry_failed=True,
                paper_ids=set(master),
                downloader=downloader_factory(root),
            )
        if title_ids:
            await acquire(
                root,
                limit=len(master),
                candidate_ids=title_ids,
                paper_ids=set(master),
                downloader=downloader_factory(root),
                exhaust_candidates=True,
            )
        with Catalog(root) as c:
            refresh_user_states(c)
            rows = c.db.execute(
                "SELECT candidate_id,status,result_json,staging_path FROM acquisition_attempts WHERE rowid>?",
                (plan["attempt_rowid"],),
            ).fetchall()
            codes = Counter()
            http_success = 0
            pdf_files = set()
            content_retried = set()
            content_http = set()
            for cid, status, raw, path in rows:
                detail = json.loads(raw)
                trace = detail.get("trace", [])
                events = [e for e in trace if not e.get("robots")]
                if any(e.get("status") == 200 for e in events):
                    http_success += 1
                if cid in plan["content_candidates"]:
                    content_retried.add(cid)
                    if events:
                        content_http.add(cid)
                if (root / path).is_file() and (root / path).read_bytes().startswith(b"%PDF-"):
                    pdf_files.add(path)
                codes[detail.get("error", {}).get("code") or status] += 1
            after = user_summary(c)
            report = {
                "before": plan["before"],
                "after": after,
                "doi_audit": plan["doi_source_audit"],
                "original_title_conflicts": len(plan["title_works"]),
                "title_doi_matched_admitted": len(admitted),
                "title_excluded": excluded,
                "original_nonpublic_content_candidates": len(plan["content_candidates"]),
                "content_retried": len(content_retried),
                "content_http_issued": len(content_http),
                "http_200_attempts": http_success,
                "pdf_staging_files": len(pdf_files),
                "new_verified": after["PROGRAM_VERIFIED"] - plan["before"]["PROGRAM_VERIFIED"],
                "outcomes": dict(codes),
                "new_attempts": len(rows),
            }
            report_path.write_text(dumps(report), encoding="utf-8")
        export_agent(root)
        status_path.write_text(
            "COMPLETED\n" + json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
