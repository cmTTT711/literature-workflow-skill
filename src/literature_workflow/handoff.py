"""Execute the existing CSV protocols; the catalog remains the publication authority."""
from pathlib import Path
import csv, hashlib, json
from urllib.parse import urlsplit

from .config import atomic_json

def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def read_tasks(root, attachments=False):
    from paper_acquisition.normalization import normalize_doi
    root = Path(root).resolve()
    filename = "attachment_required.csv" if attachments else "agent_required.csv"
    path = root / "agent_tasks" / filename
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    from paper_acquisition.storage import Catalog
    with Catalog(root) as catalog:
        allowed = {r[0] for r in catalog.db.execute("SELECT doi FROM master_records")}
    seen = set()
    for row in rows:
        doi = normalize_doi(row.get("doi"))
        if not doi or doi not in allowed or doi in seen:
            raise ValueError("清单 DOI 与当前运行不一致或重复")
        seen.add(doi)
        field = "target_dir" if attachments else "target_path"
        if not row.get(field):
            raise ValueError("清单缺少 " + field)
        target = Path(row[field]).resolve()
        base = root / ("attachments/agent" if attachments else "downloads/agent")
        if not target.is_relative_to(base) or target == base:
            raise ValueError("清单目标路径超出本次运行的接收目录")
    return rows

def _signature(row):
    stable = {k: row.get(k) for k in ("doi", "title", "publisher", "target_path", "target_dir", "attachment_urls")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def _ledger(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

def _runner():
    from scanpdf.download import run
    return run

def download_main(root, config, *, retry=False, runner=None, emit=print):
    from scanpdf.identity import Paper
    root = Path(root).resolve()
    rows = read_tasks(root)
    folder = root / "browser_handoff"
    path = folder / "main_attempts.json"
    ledger = _ledger(path)
    selected = [r for r in rows if retry or ledger.get(r["doi"], {}).get("signature") != _signature(r)
                or ledger.get(r["doi"], {}).get("status") == "ok"]
    if not selected:
        return {"requested": 0, "remaining_input": len(rows), "previous_attempts_skipped": len(rows)}
    atomic_json(folder / "main_input.json", selected)
    papers = [Paper(r["doi"], r["title"], r["publisher"], r["target_path"]) for r in selected]
    result = (runner or _runner())(papers, config.scan_config(), folder, si=False,
                                   respect_targets=True, emit=emit)
    manifest = _ledger(folder / "manifest.json")
    for row in selected:
        entry = manifest.get(row["doi"].lower(), {})
        ledger[row["doi"]] = {"signature": _signature(row), "status": entry.get("status", "needs_manual"),
                              "target_path": row["target_path"]}
    atomic_json(path, ledger)
    return result

def _urls(row):
    values = json.loads(row.get("attachment_urls") or "[]")
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError("附件 URL 字段不是列表")
    for value in values:
        part = urlsplit(value)
        if part.scheme not in {"https", "http"} or not part.hostname or part.username:
            raise ValueError("附件清单含不支持的 URL")
    return values

def _copy_file(source, target, expected):
    from paper_acquisition.reporting import copy_verified
    if target.exists():
        if digest(target) != expected:
            raise ValueError("附件接收位置已有内容不同的同名文件")
        return
    copy_verified(source, target, expected)

def import_si(root, rows, results, *, emit=print):
    """A successful browser attempt resolves known links, never asserts 'no SI'."""
    from paper_acquisition.attachments.store import Store, validate
    from paper_acquisition.attachments.service import finish
    root = Path(root).resolve()
    source_base = root / "browser_handoff/si"
    errors, evidence = [], {}
    for row in rows:
        target_dir = Path(row["target_dir"]).resolve()
        base = root / "attachments/agent"
        if target_dir == base or not target_dir.is_relative_to(base):
            raise ValueError("附件接收位置超出本次运行")
        doi = row["doi"].lower()
        result = results.get(doi, {})
        copied = []
        for record in result.get("files", []):
            try:
                source = Path(record["path"]).resolve()
                if not source.is_relative_to(source_base) or digest(source) != record["sha256"]:
                    raise ValueError("附件下载台账的路径或哈希不匹配")
                validate(source.read_bytes(), source.name)
                target = target_dir / (record["sha256"][:16] + "_" + source.name)
                _copy_file(source, target, record["sha256"])
                copied.append(record)
            except (OSError, ValueError, KeyError) as error:
                errors.append({"doi": doi, "error": type(error).__name__ + ": " + str(error)})
        evidence[doi] = {"requested_urls": _urls(row), "result": result, "copied": len(copied)}
    atomic_json(root / "browser_handoff/attachment_evidence.json", evidence)
    with Store(root) as store:
        import_errors = store.import_agent()
        broken = {e["doi"] for e in [*errors, *import_errors]}
        for row in rows:
            doi = row["doi"].lower()
            result = results.get(doi, {})
            files = result.get("files", [])
            # Completion means all explicitly requested links were handled. It does
            # not create manual_state=complete/none or infer absence from zero files.
            if doi not in broken and result.get("status") == "ok" and files and len(files) < 10:
                for url in _urls(row):
                    store.db.execute("UPDATE candidates SET status='BROWSER_RESOLVED',error='' WHERE doi=? AND url=?",
                                     (doi, url))
                store.scan(doi, "LINKS_FOUND", "")
            elif result.get("status") == "partial" or doi in broken:
                store.scan(doi, "PARTIAL", "BROWSER_RESULT_NEEDS_REVIEW")
        store.db.commit()
        report = finish(store)
    report["browser_import_errors"] = errors
    return report

def download_si(root, config, *, retry=False, runner=None, emit=print):
    from scanpdf.identity import Paper
    root = Path(root).resolve()
    rows = read_tasks(root, attachments=True)
    folder = root / "browser_handoff"
    path = folder / "si_attempts.json"
    ledger = _ledger(path)
    selected = [r for r in rows if retry or ledger.get(r["doi"], {}).get("signature") != _signature(r)]
    if not selected:
        from paper_acquisition.attachments.service import local
        return {**local(root), "requested": 0, "previous_attempts_skipped": len(rows)}
    papers = [Paper(r["doi"], r["title"], r["publisher"], attachment_urls=_urls(r)) for r in selected]
    atomic_json(folder / "si_input.json", selected)
    result = (runner or _runner())(papers, config.scan_config(), folder, si_only=True, emit=emit)
    manifest = _ledger(folder / "si/si_manifest.json")
    report = import_si(root, selected, manifest, emit=emit)
    for row in selected:
        ledger[row["doi"]] = {"signature": _signature(row), "status": manifest.get(row["doi"].lower(), {}).get("status", "needs_manual"),
                              "target_dir": row["target_dir"]}
    atomic_json(path, ledger)
    return {**report, "browser": result, "requested": len(selected)}
