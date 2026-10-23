"""Check a published run's PDF sets, hashes and independent physical copies."""

import hashlib
import json
from pathlib import Path

from ..library.store import file_hash


def audit_run(run_dir):
    run_dir = Path(run_dir).resolve()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / run["manifest"]).resolve().read_text(encoding="utf-8"))
    rows = manifest["rows"]
    errors, inodes = [], set()
    by_suffix = {hashlib.sha256(r["doi"].encode()).hexdigest()[:12]: r for r in rows}
    if len(by_suffix) != len(rows):
        errors.append("DUPLICATE_DOI_OR_FILENAME_KEY")
    counts = {}
    for folder in [run_dir / "pdfs" / name for name in ("automatic", "agent", "all")] + [
        p for p in (run_dir / "classifications").rglob("*") if p.is_dir() and any(p.glob("*.pdf"))
    ]:
        seen = set()
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() != ".pdf":
                errors.append("NON_PDF_IN_PDF_DIRECTORY: " + str(path))
                continue
            row = by_suffix.get(path.stem.rsplit("_", 1)[-1])
            if not row:
                errors.append("UNKNOWN_PDF: " + str(path))
                continue
            if row["doi"] in seen:
                errors.append("DUPLICATE_DOI: " + str(path))
            seen.add(row["doi"])
            if file_hash(path) != row["sha256"]:
                errors.append("PDF_HASH_MISMATCH: " + str(path))
            inode = (path.stat().st_dev, path.stat().st_ino)
            if inode in inodes or path.is_symlink() or path.stat().st_nlink != 1:
                errors.append("PDF_IS_NOT_INDEPENDENT_COPY: " + str(path))
            inodes.add(inode)
        key = folder.relative_to(run_dir).as_posix()
        counts[key] = len(seen)
        if folder.parent == run_dir / "pdfs":
            channel = {"automatic": "program", "agent": "agent", "all": None}[folder.name]
            expected = {r["doi"] for r in rows if not channel or r["channel"] == channel}
            if seen != expected:
                errors.append("CHANNEL_OR_TOTAL_SET_MISMATCH: " + key)
    if list(run_dir.rglob("*.html")):
        errors.append("HTML_OUTPUT_PRESENT")
    return {"run_dir": str(run_dir), "counts": counts, "errors": errors}
