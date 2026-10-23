"""Optional supplementary-file pipeline; never runs PDF acquisition or agents."""

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import httpx

from ..acquisition.models import AcquisitionError, AcquisitionOptions
from ..acquisition.network import PublicDownloader
from ..library.store import atomic_json, collection_config, file_hash, library_root
from ..locking import RunLock
from ..models import now
from ..normalization import normalize_doi
from ..storage import write_csv
from .discovery import extract_links
from .store import MAX_BYTES, Store, paper_key

FIELDS = [
    "doi",
    "title",
    "year",
    "publisher",
    "reason",
    "landing_urls",
    "attachment_urls",
    "automatic_files",
    "agent_files",
    "target_dir",
]
FETCH_ERRORS = (AcquisitionError, httpx.HTTPError, OSError, ValueError, TimeoutError)
README = """# 附件任务与回填

attachment_required.csv / .jsonl 是独立附件清单，正文任务仍使用 agent_required.csv。
本清单只提供任务，不自动启动或分派 Agent。附件处理由用户自行安排。
每行关联一个 DOI，target_dir 是唯一的附件接收目录；文件可保留原名，可放多个文件。
不要将正文放入此目录。已自动下载的附件列在 automatic_files，避免重复获取。
attachment_urls 是自动发现但尚未成功获取的链接；没有链接的行需要人工查找。
NO_EXPLICIT_LINKS / NOT_SCANNED 均不代表论文没有附件。

保存后运行 attachments publish --data-dir <本次工作目录>（或普通 publish）导入并汇总。
同一 DOI 的相同内容只汇总一次，automatic 优先；两个渠道的来源均记录在 manifest。
回填文件不会自动表示该 DOI 的附件已经齐全。确认齐全后运行：
  paper-acquisition attachments mark --data-dir <目录> --doi <DOI> --outcome complete
确认论文确实没有附件时，使用 --outcome none；重新开放任务使用 --outcome reopen。
mark complete 要求至少一个已导入文件；mark none 要求没有已导入文件。
附件状态独立于正文状态。已确认的任务可通过 reopen 恢复。
"""


def enabled(root):
    return (Path(root) / "attachments/state.sqlite").is_file()


def result_dir(root):
    config = collection_config(root)
    return (Path(root) / config["result"]).resolve() if config else None


def write_tasks(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    warnings = []
    for extension in ("csv", "jsonl"):
        path = folder / ("attachment_required." + extension)
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temp.open("w", encoding="utf-8-sig" if extension == "csv" else "utf-8", newline="") as f:
                if extension == "csv":
                    write_csv(f, rows, FIELDS, escape_fields=set(FIELDS) - {"doi", "target_dir"})
                else:
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
            try:
                temp.replace(path)
            except PermissionError:
                alternate = path.with_name(path.stem + ".updated-" + uuid4().hex[:8] + path.suffix)
                temp.replace(alternate)
                warnings.append({"locked_path": str(path), "updated_path": str(alternate)})
        finally:
            temp.unlink(missing_ok=True)
    (folder / "README_ATTACHMENTS.md").write_text(README, encoding="utf-8")
    return warnings


def export_tasks(store):
    files = store.files()
    rows = []
    for row in store.papers():
        doi = row["doi"]
        if row["manual_state"] in {"complete", "none"}:
            continue
        pending = [
            dict(r)
            for r in store.db.execute(
                "SELECT * FROM candidates WHERE doi=? AND status IN ('PENDING','FAILED') ORDER BY url", (doi,)
            )
        ]
        if row["scan_state"] == "LINKS_FOUND" and not pending:
            continue
        paper = json.loads(row["metadata"])
        target = store.agent_dir(doi)
        target.mkdir(parents=True, exist_ok=True)
        rows.append(
            {
                "doi": doi,
                "title": paper.get("title", ""),
                "year": paper.get("year"),
                "publisher": paper.get("publisher_normalized", "other"),
                "reason": "; ".join(
                    filter(
                        None,
                        [row["scan_state"], row["scan_error"], *(r["error"] or r["status"] for r in pending)],
                    )
                ),
                "landing_urls": ["https://doi.org/" + doi, *paper.get("landing_urls", [])],
                "attachment_urls": [r["url"] for r in pending],
                "automatic_files": [
                    r["name"] for r in files if r["doi"] == doi and r["channel"] == "automatic"
                ],
                "agent_files": [r["name"] for r in files if r["doi"] == doi and r["channel"] == "agent"],
                "target_dir": str(target),
            }
        )
    warnings = write_tasks(store.root / "agent_tasks", rows)
    destination = result_dir(store.root)
    if destination:
        warnings.extend(write_tasks(destination / "agent_tasks", rows))
    return {"agent_required": len(rows), "papers": len(store.papers()), "task_export_warnings": warnings}


def publish_files(store):
    from ..acquisition.naming import pdf_filename
    from ..reporting import copy_verified, replace_directory

    destination = result_dir(store.root)
    if not destination:
        return {"status": "WORKSPACE_ONLY", "message": "Run publish to create a result collection."}
    working = library_root(store.root).parent
    stage = working / "temp" / ("att-" + uuid4().hex[:12])
    history = working / "history" / "attachment-publications"
    for channel in ("automatic", "agent", "all"):
        (stage / channel).mkdir(parents=True)
    grouped = {}
    for row in store.files():
        grouped.setdefault((row["doi"], row["sha256"]), []).append(row)
    papers = {r["doi"]: json.loads(r["metadata"]) for r in store.papers()}
    # Account for both the result and temporary paths in Windows UTF-16 units.
    base_units = max(
        len(str(base / "automatic").encode("utf-16-le")) // 2 for base in (stage, destination / "attachment")
    )
    longest_file = max(
        (len(row["name"].encode("utf-16-le")) // 2 for sources in grouped.values() for row in sources),
        default=0,
    )
    folder_budget = min(110, 245 - base_units - longest_file - 2)
    folders = {
        doi: pdf_filename(
            paper.get("year"),
            paper.get("title"),
            max_units=folder_budget + 4,
            suffix="_" + hashlib.sha256(doi.encode()).hexdigest()[:12],
        ).removesuffix(".pdf")
        for doi, paper in papers.items()
        if any(key[0] == doi for key in grouped)
    }
    if len({name.casefold() for name in folders.values()}) != len(folders):
        raise ValueError("ATTACHMENT_FOLDER_COLLISION")
    published = []
    for (doi, digest), sources in sorted(grouped.items()):
        chosen = next((r for r in sources if r["channel"] == "automatic"), sources[0])
        relative = Path(folders[doi]) / chosen["name"]
        for channel in (chosen["channel"], "all"):
            copy_verified(store.home / chosen["path"], stage / channel / relative, digest)
        published.append(
            {
                "doi": doi,
                "title": papers[doi].get("title", ""),
                "year": papers[doi].get("year"),
                "sha256": digest,
                "bytes": chosen["bytes"],
                "channel": chosen["channel"],
                "path": (Path("all") / relative).as_posix(),
                "sources": [
                    {"channel": r["channel"], "provenance": json.loads(r["provenance"])} for r in sources
                ],
            }
        )
    atomic_json(stage / "manifest.json", {"version": 1, "files": published})
    replace_directory(stage, destination / "attachment", history)
    return {
        "status": "PUBLISHED",
        "files": len(published),
        "papers_with_files": len({r["doi"] for r in published}),
        "by_channel": dict(Counter(r["channel"] for r in published)),
        "result_dir": str(destination / "attachment"),
    }


def finish(store, *, publish=True):
    errors = store.import_agent()
    files = store.files()
    keys = {(r["doi"], r["sha256"]) for r in files}
    automatic = {(r["doi"], r["sha256"]) for r in files if r["channel"] == "automatic"}
    report = {
        **export_tasks(store),
        "import_errors": errors,
        "files": len(keys),
        "papers_with_files": len({r["doi"] for r in files}),
        "by_channel": {"automatic": len(automatic), "agent": len(keys - automatic)},
    }
    if publish:
        report.update(publish_files(store))
    report["updated_at"] = now()
    atomic_json(store.home / "report.json", report)
    destination = result_dir(store.root)
    if destination and publish:
        run_path = destination / "run.json"
        saved = json.loads(run_path.read_text(encoding="utf-8"))
        atomic_json(run_path, {**saved, "attachments": report})
    return report


def local(root, *, publish=True):
    root = Path(root).resolve()
    with RunLock(root / "attachments.lock"), Store(root) as store:
        return finish(store, publish=publish)


def safe_publish(root):
    """Called only after PDF publication; attachment failure cannot fail that publication."""
    if not enabled(root):
        return None
    try:
        return local(root)
    except Exception as error:  # noqa: BLE001 -- isolate optional attachments from PDF publication
        return {"status": "ATTACHMENT_ERROR", "error": str(error)}


async def acquire(root, *, retry_failed=False, refresh=False, limit=None, downloader=None):
    root = Path(root).resolve()
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    with RunLock(root / "attachments.lock"), Store(root) as store:
        owned = downloader is None
        downloader = downloader or PublicDownloader(
            store.home, AcquisitionOptions(max_bytes=MAX_BYTES, attempts=2, timeout_seconds=30)
        )
        processed = 0
        try:
            for row in store.papers():
                if row["manual_state"] or (limit is not None and processed >= limit):
                    continue
                doi = row["doi"]
                paper = json.loads(row["metadata"])
                needs_scan = (
                    refresh
                    or row["scan_state"] == "NOT_SCANNED"
                    or (retry_failed and row["scan_state"] in {"SCAN_FAILED", "PARTIAL", "NO_EXPLICIT_LINKS"})
                )
                pending = store.db.execute(
                    "SELECT 1 FROM candidates WHERE doi=? AND (status='PENDING' OR (? AND status='FAILED'))",
                    (doi, retry_failed),
                ).fetchone()
                if not needs_scan and not pending:
                    continue
                processed += 1
                try:
                    async with asyncio.timeout(300):
                        if needs_scan:
                            await discover_paper(store, downloader, paper)
                        await download_candidates(store, downloader, doi, retry_failed)
                except FETCH_ERRORS as error:
                    store.scan(doi, "PARTIAL", type(error).__name__ + ": " + str(error))
        finally:
            if owned:
                await downloader.close()
            # Keep a usable handoff even if this run is interrupted between papers.
            report = finish(store)
        return {**report, "processed": processed}


async def discover_paper(store, downloader, paper):
    doi = paper["doi"]
    urls = list(dict.fromkeys(["https://doi.org/" + quote(doi, safe="/"), *paper.get("landing_urls", [])]))
    errors, count, succeeded = [], 0, 0
    # Bounded, one-hop discovery. Dynamic pages and unlinked data repositories remain manual tasks.
    if len(urls) > 3:
        errors.append("LANDING_PAGE_LIMIT")
    for url in urls[:3]:
        try:
            downloader.trace = []
            body, final, _ = await downloader.fetch(url)
            if len(body) > 4 * 1024 * 1024 or body.startswith(b"%PDF-"):
                raise ValueError("NOT_A_LANDING_PAGE")
            if not any(token in body[:4096].lower() for token in (b"<html", b"<!doctype html", b"<head")):
                raise ValueError("LANDING_PAGE_NOT_HTML")
            succeeded += 1
            links = extract_links(body, final)
            if len(links) > 50:
                errors.append("CANDIDATE_LIMIT")
            for link in links[:50]:
                store.db.execute(
                    "INSERT OR IGNORE INTO candidates(doi,url,page_url,label) VALUES(?,?,?,?)",
                    (doi, link["url"], final, link["label"]),
                )
                count += 1
            store.db.commit()
            # Retain the exact source for candidate provenance, outside PDF raw records.
            digest = hashlib.sha256(body).hexdigest()
            evidence = store.home / "evidence" / paper_key(doi) / (digest + ".html")
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_bytes(body)
        except FETCH_ERRORS as error:
            errors.append(type(error).__name__ + ": " + str(error))
    state = (
        "PARTIAL"
        if errors and succeeded
        else "SCAN_FAILED"
        if errors
        else ("LINKS_FOUND" if count else "NO_EXPLICIT_LINKS")
    )
    store.scan(doi, state, "; ".join(errors))


async def download_candidates(store, downloader, doi, retry_failed):
    candidates = store.db.execute(
        "SELECT * FROM candidates WHERE doi=? AND (status='PENDING' OR (? AND status='FAILED')) ORDER BY url",
        (doi, retry_failed),
    ).fetchall()
    for candidate in candidates:
        url = candidate["url"]
        try:
            downloader.trace = []
            body, final, _ = await downloader.fetch(url)
            name = unquote(Path(urlsplit(final).path).name) or "attachment"
            digest = store.put(
                doi,
                body,
                name,
                "automatic",
                {
                    "source_url": url,
                    "final_url": final,
                    "page_url": candidate["page_url"],
                    "label": candidate["label"],
                    "fetched_at": now(),
                },
            )
            store.db.execute(
                "UPDATE candidates SET status='DOWNLOADED',error='',sha256=?,final_url=? WHERE doi=? AND url=?",
                (digest, final, doi, url),
            )
        except FETCH_ERRORS as error:
            store.db.execute(
                "UPDATE candidates SET status='FAILED',error=? WHERE doi=? AND url=?",
                (type(error).__name__ + ": " + str(error), doi, url),
            )
        store.db.commit()


def mark(root, doi, outcome):
    root = Path(root).resolve()
    doi = normalize_doi(doi)
    if outcome not in {"complete", "none", "reopen"}:
        raise ValueError("INVALID_OUTCOME")
    with RunLock(root / "attachments.lock"), Store(root) as store:
        if doi not in store.active:
            raise ValueError("DOI_NOT_IN_CATALOG")
        errors = store.import_agent()
        if any(e["doi"] == doi for e in errors):
            raise ValueError("AGENT_FILES_NEED_REVIEW")
        has_files = any(r["doi"] == doi for r in store.files())
        if outcome == "complete" and not has_files:
            raise ValueError("NO_IMPORTED_ATTACHMENTS")
        if outcome == "none" and has_files:
            raise ValueError("ATTACHMENTS_ALREADY_PRESENT")
        store.db.execute(
            "UPDATE papers SET manual_state=?,updated_at=? WHERE doi=?",
            ("" if outcome == "reopen" else outcome, now(), doi),
        )
        if outcome == "reopen":
            store.db.execute("UPDATE papers SET scan_state='NOT_SCANNED',scan_error='' WHERE doi=?", (doi,))
        store.db.commit()
        return finish(store)


def audit(root):
    root = Path(root).resolve()
    if not enabled(root):
        return {"files": 0, "errors": ["ATTACHMENTS_NOT_ENABLED"]}
    errors = []
    with RunLock(root / "attachments.lock"), Store(root) as store:
        try:
            files = store.files()
        except ValueError as error:
            files = []
            errors.append(str(error))
        destination = result_dir(root)
        manifest = destination / "attachment/manifest.json" if destination else None
        if manifest and manifest.is_file():
            saved = json.loads(manifest.read_text(encoding="utf-8"))["files"]
            expected = set()
            seen = set()
            for row in saved:
                key = (row["doi"], row["sha256"])
                if key in seen:
                    errors.append("DUPLICATE_ATTACHMENT")
                seen.add(key)
                for channel in (row["channel"], "all"):
                    path = manifest.parent / channel / Path(row["path"]).relative_to("all")
                    if not path.resolve().is_relative_to(manifest.parent.resolve()):
                        errors.append("ATTACHMENT_PATH_ESCAPE")
                        continue
                    expected.add(path)
                    if not path.is_file() or file_hash(path) != row["sha256"]:
                        errors.append("ATTACHMENT_COPY_MISSING_OR_CHANGED: " + str(path))
            if seen != {(r["doi"], r["sha256"]) for r in files}:
                errors.append("ATTACHMENT_MANIFEST_STALE")
            actual = {
                p
                for ch in ("automatic", "agent", "all")
                for p in (manifest.parent / ch).rglob("*")
                if p.is_file()
            }
            if actual != expected:
                errors.append("ATTACHMENT_OUTPUT_SET_MISMATCH")
        elif destination:
            errors.append("ATTACHMENT_MANIFEST_MISSING")
        return {"files": len({(r["doi"], r["sha256"]) for r in files}), "errors": errors}
