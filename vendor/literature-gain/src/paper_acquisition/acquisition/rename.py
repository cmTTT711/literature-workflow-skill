"""Offline migration with database backup and a recoverable path journal."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..locking import RunLock
from ..storage import Catalog, file_hash
from .naming import paper_target


def migrate_pdf_names(root, *, apply=False):
    root = Path(root).resolve()
    with RunLock(root / "acquisition.lock"), RunLock(root / "discovery.lock"), Catalog(root) as catalog:
        return _migrate(catalog, apply)


def _migrate(catalog, apply):
    root = catalog.root
    papers = {p.paper_id: p for p in catalog.iter_papers()}
    known = {}
    program = root / "downloads/program"
    for pid, value in catalog.db.execute("SELECT paper_id,path FROM artifacts"):
        if pid in papers and value and (root / value).resolve().is_relative_to(program):
            known.setdefault((root / value).resolve(), papers[pid])
    all_pdfs = [p for p in program.rglob("*.pdf") if not p.is_relative_to(program / "staging")]
    # Recognize already-migrated files on a safe rerun.
    for folder, pid, filename in catalog.db.execute("SELECT * FROM pdf_names"):
        if pid in papers and (root / folder).is_relative_to(program):
            known[(root / folder / filename).resolve()] = papers[pid]
    unknown = [str(p) for p in all_pdfs if p.resolve() not in known]
    backup = root / "filename_migrations" / datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    if apply:
        backup.mkdir(parents=True)
        with sqlite3.connect(backup / "catalog.sqlite") as db:
            catalog.db.backup(db)
    # Simulate reservations without changing the real DB for dry runs.
    if not apply:
        memory = sqlite3.connect(":memory:")
        catalog.db.backup(memory)
        original = catalog.db
        catalog.db = memory
    moves, mapping, used = [], {}, set()
    try:
        for old, paper in sorted(known.items(), key=lambda item: str(item[0])):
            if not old.is_relative_to(program):
                raise ValueError(f"PDF_PATH_ESCAPE: {old}")
            if not old.is_file():
                continue
            target = paper_target(catalog, old.parent, paper)
            if old.exists() and old != target:
                base = target
                number = 2
                while target in used or target.exists():
                    target = base.with_name(f"{base.stem}_{number}.pdf")
                    number += 1
                used.add(target)
                moves.append(
                    {
                        "old": str(old),
                        "new": str(target),
                        "sha256": file_hash(old),
                    }
                )
            mapping[str(old)] = str(target)
            mapping[str(old.relative_to(root))] = str(target.relative_to(root))
        report = {
            "root": str(root),
            "pdf_count": len(all_pdfs),
            "rename_count": len(moves),
            "unknown": unknown,
            "moves": moves,
        }
        if not apply:
            return report
        (backup / "journal.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        completed = []
        try:
            for move in moves:
                old, new = Path(move["old"]), Path(move["new"])
                if new.exists():
                    raise FileExistsError(new)
                old.rename(new)
                completed.append(move)
                if file_hash(new) != move["sha256"]:
                    raise ValueError("RENAME_HASH_MISMATCH")

            # Update live DB paths, including embedded result/event JSON. Raw evidence stays intact.
            def replace(value):
                if isinstance(value, str):
                    return mapping.get(value, mapping.get(value.replace("/", "\\"), value))
                if isinstance(value, dict):
                    return {k: replace(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [replace(v) for v in value]
                return value

            with catalog.db:
                for table in ["artifacts", "acquisition_attempts", "access_resolution", "acquisition_events"]:
                    cols = [r[1] for r in catalog.db.execute(f"PRAGMA table_info({table})")]
                    for row in catalog.db.execute(f"SELECT rowid,* FROM {table}").fetchall():
                        for col, value in zip(cols, row[1:]):
                            if not isinstance(value, str):
                                continue
                            updated = replace(value)
                            if col.endswith("_json"):
                                updated = json.dumps(replace(json.loads(value)), ensure_ascii=False)
                            if updated != value:
                                catalog.db.execute(
                                    f'UPDATE {table} SET "{col}"=? WHERE rowid=?', (updated, row[0])
                                )
            report["applied"] = True
            report["backup"] = str(backup)
            (backup / "result.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return report
        except Exception:
            for move in reversed(completed):
                Path(move["new"]).rename(move["old"])
            with sqlite3.connect(backup / "catalog.sqlite") as db:
                db.backup(catalog.db)
            raise
    finally:
        if not apply:
            catalog.db.close()
            catalog.db = original
