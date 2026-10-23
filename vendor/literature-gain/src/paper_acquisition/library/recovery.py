"""Recover exact historical DOI/file mappings from backups and migration journals."""

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from ..normalization import normalize_doi
from .store import atomic_json, file_hash


def recover_sources(root, external=None):
    root = Path(root).resolve()
    backup = root / "history/before-library"
    if not (backup / "catalog.sqlite").exists():
        raise ValueError("HISTORICAL_BACKUP_NOT_FOUND")
    moved = {}
    for journal in (root / "history").glob("compact-*.json"):
        for action in json.loads(journal.read_text(encoding="utf-8"))["actions"]:
            moved[str(Path(action["old"]).resolve())] = Path(action["target"])

    def locate(path):
        path = Path(path).resolve()
        seen = set()
        while not path.is_file() and str(path) in moved and str(path) not in seen:
            seen.add(str(path))
            path = moved[str(path)].resolve()
        return path if path.is_file() else None

    alternate = defaultdict(list)
    if external:
        external = Path(external).resolve()
        for folder in ("downloads", "dl2023"):
            index = external / folder / ".doi_index.json"
            if index.exists():
                for doi, value in json.loads(index.read_text(encoding="utf-8")).items():
                    if isinstance(value, dict) and value.get("file"):
                        for path in (external / value["file"], index.parent / Path(value["file"]).name):
                            if path.is_file():
                                alternate[normalize_doi(doi)].append(path.resolve())
        audit = external / "audit_all.json"
        if audit.exists():
            for value, info in json.loads(audit.read_text(encoding="utf-8")).items():
                if isinstance(info, dict) and normalize_doi(info.get("doi")):
                    path = (external / value).resolve()
                    if path.is_file() and info.get("score", 0) >= 0.9:
                        alternate[normalize_doi(info["doi"])].append(path)
    records = []
    with sqlite3.connect(backup / "catalog.sqlite") as db:
        for doi, pid in db.execute("SELECT doi,paper_id FROM master_records WHERE status='PROGRAM_VERIFIED'"):
            options = []
            for path, digest, verification, attempt in db.execute(
                "SELECT path,sha256,verification_json,attempt_id FROM artifacts WHERE paper_id=? AND status='VERIFIED'",
                (pid,),
            ):
                found = locate(root / path)
                version = db.execute(
                    "SELECT json_extract(c.candidate_json,'$.version') FROM acquisition_attempts a JOIN access_candidates c USING(candidate_id) WHERE a.attempt_id=?",
                    (attempt,),
                ).fetchone()
                if found and file_hash(found) == digest:
                    options.append(
                        {
                            "path": str(found),
                            "sha256": digest,
                            "verification": json.loads(verification),
                            "version": version[0] if version else None,
                            "evidence": "historical_verified_hash",
                        }
                    )
            records.append({"doi": normalize_doi(doi), "channel": "program", "sources": options})
    with (backup / "agent_required.csv").open(encoding="utf-8-sig", newline="") as stream:
        for task in csv.DictReader(stream):
            doi = normalize_doi(task["doi"])
            primary = locate(task["target_path"])
            paths = list(dict.fromkeys(([primary] if primary else []) + alternate[doi]))
            records.append(
                {
                    "doi": doi,
                    "channel": "agent",
                    "sources": [
                        {
                            "path": str(path),
                            "sha256": file_hash(path),
                            "version": None,
                            "evidence": "original_agent_target"
                            if path == primary
                            else "external_doi_mapping",
                        }
                        for path in paths
                    ],
                }
            )
    report = {
        "records": records,
        "counts": {
            channel: {
                "expected": sum(r["channel"] == channel for r in records),
                "found": sum(r["channel"] == channel and bool(r["sources"]) for r in records),
            }
            for channel in ("program", "agent")
        },
    }
    atomic_json(root / "recovery.json", report)
    return report
