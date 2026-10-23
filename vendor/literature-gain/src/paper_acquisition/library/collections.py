"""Publish existing or newly acquired catalogs into the shared library."""

import csv
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..acquisition.artifacts import adopt_library, verified_artifact
from ..acquisition.layout import safe_doi_filename
from ..acquisition.models import AcquisitionOptions, PDFResult
from ..acquisition.verifier import UnifiedPDFVerifier
from ..locking import RunLock
from ..master import (
    agent_task,
    agent_task_target,
    export_master,
    master_rows,
    pending_acquisition,
    refresh_user_states,
    undecided_papers,
    write_agent_tasks,
)
from ..models import SearchProfile, now
from ..normalization import normalize_doi
from ..storage import Catalog
from .store import Library, atomic_json, collection_config, file_hash, library_root


def safe_label(value):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    text = re.sub(r"\s+", " ", text).strip(" .")[:48].rstrip(" .") or "检索"
    if text.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        text = "检索_" + text
    return text


def years(profile):
    start, end = profile.year_from, profile.year_to
    if start and start == end:
        return str(start)
    return f"{start or '不限'}-{end or '不限'}" if start or end else "全部年份"


def attach_collection(root, workspace, profile, *, label=None, new=False):
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    if collection_config(root) and not new:
        return (root / collection_config(root)["result"]).resolve()
    title = safe_label(label or profile.topic) + "_" + years(profile)
    destination = (
        workspace
        / "results"
        / (title + "_" + datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S") + "_" + uuid4().hex[:6])
    )
    destination.mkdir(parents=True)
    atomic_json(
        root / "collection.json",
        {
            "version": 1,
            "label": title,
            "profile": profile.model_dump(),
            "created_at": now(),
            "library": os.path.relpath(workspace / "workspace" / "library", root),
            "result": os.path.relpath(destination, root),
        },
    )
    atomic_json(
        destination / "run.json",
        {"status": "CREATED", "catalog": os.path.relpath(root, destination), "profile": profile.model_dump()},
    )
    return destination


def saved_profile(root):
    with closing(
        sqlite3.connect(f"file:{(Path(root).resolve() / 'catalog.sqlite').as_posix()}?mode=ro", uri=True)
    ) as db:
        row = db.execute("SELECT profile_json FROM search_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("NO_SEARCH_PROFILE: supply a profile for this collection")
    return SearchProfile.model_validate_json(row[0])


def reuse_library(root):
    shared = library_root(root)
    if not shared:
        return 0
    with Catalog(Path(root)) as catalog:
        reused = 0
        with Library(shared) as library:
            for paper, state, reasons, row in list(master_rows(catalog)):
                if reasons:
                    continue
                if verified_artifact(catalog, paper.paper_id):
                    continue
                if entry := library.lookup(paper.doi):
                    adopt_library(catalog, paper, entry, channel="program")
                    reused += 1
            refresh_user_states(catalog)
    return reused


def source_paths(catalog):
    """Only exact database/manifest mappings; never guess identity from similar filenames."""
    paths = {}
    for pid, path, digest, channel in catalog.db.execute(
        "SELECT paper_id,path,sha256,channel FROM artifacts WHERE status='VERIFIED'"
    ):
        paths.setdefault(pid, []).append(((catalog.root / path).resolve(), channel, digest))
    by_doi = {normalize_doi(p.doi): p.paper_id for p in catalog.iter_papers() if p.doi}
    task_file = catalog.root / "agent_tasks/agent_required.csv"
    if task_file.exists():
        with task_file.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                pid = by_doi.get(normalize_doi(row["doi"]))
                target = Path(row["target_path"])
                if pid and target.resolve().is_relative_to(catalog.root / "downloads"):
                    paths.setdefault(pid, []).append((target, "agent", None))
    for paper, _, _, row in master_rows(catalog):
        folder = catalog.root / "downloads/agent" / row["publisher"]
        paths.setdefault(paper.paper_id, []).extend(
            [
                (agent_task_target(catalog, paper, row["publisher"]), "agent", None),
                (folder / safe_doi_filename(paper.doi), "agent", None),
            ]
        )
    return paths


def publish_collection(root, *, workspace=None, label=None, compact=False, progress=None):
    root = Path(root).resolve()
    if not (root / "catalog.sqlite").is_file():
        raise ValueError("CATALOG_NOT_FOUND")
    with (
        RunLock(root / "collection.lock"),
        RunLock(root / "acquisition.lock"),
        RunLock(root / "discovery.lock"),
    ):
        if not collection_config(root):
            attach_collection(root, workspace or Path.cwd(), saved_profile(root), label=label)
        config = collection_config(root)
        destination = (root / config["result"]).resolve()
        with Catalog(root) as catalog:
            if undecided_papers(catalog):
                raise ValueError("MASTER_INCOMPLETE: run acquire or prepare-master before publication")
            backup = root / "history" / "before-library"
            backup.mkdir(parents=True, exist_ok=True)
            if not (backup / "catalog.sqlite").exists():
                with closing(sqlite3.connect(backup / "catalog.sqlite")) as db:
                    catalog.db.backup(db)
                for filename in ("agent_required.csv", "agent_required.jsonl"):
                    old = root / "agent_tasks" / filename
                    if old.exists():
                        (backup / filename).write_bytes(old.read_bytes())
            with closing(sqlite3.connect(backup / "catalog.sqlite")) as original_db:
                original = dict(
                    original_db.execute("SELECT status,count(*) FROM master_records GROUP BY status")
                )
            paths = source_paths(catalog)
            verifier = UnifiedPDFVerifier(AcquisitionOptions(max_bytes=200 * 1024 * 1024))
            recovery_file = root / "recovery.json"
            recovery = (
                {r["doi"]: r for r in json.loads(recovery_file.read_text(encoding="utf-8"))["records"]}
                if recovery_file.exists()
                else {}
            )
            failures, rows, tasks = [], [], []
            master = list(master_rows(catalog))
            with Library(library_root(root)) as library:
                for index, (paper, state, reasons, row) in enumerate(master):
                    entry = library.lookup(paper.doi) if not reasons else None
                    historical = recovery.get(normalize_doi(paper.doi))
                    if historical and historical.get("excluded_reason"):
                        entry = None
                        failures.append({"doi": paper.doi, "reasons": [historical["excluded_reason"]]})
                    channel = (
                        historical["channel"]
                        if historical
                        else next(
                            (c for p, c, h in paths.get(paper.paper_id, []) if h and p.is_file()), "agent"
                        )
                    )
                    if historical and not reasons and not historical.get("excluded_reason"):
                        for source in historical["sources"]:
                            if entry and source["sha256"] == entry["sha256"] and not source.get("version"):
                                break
                            path = Path(source["path"])
                            if not path.is_file() or file_hash(path) != source["sha256"]:
                                failures.append(
                                    {
                                        "doi": paper.doi,
                                        "path": str(path),
                                        "reasons": ["HISTORICAL_SOURCE_MISSING_OR_CHANGED"],
                                    }
                                )
                                continue
                            result = (
                                PDFResult.model_validate(source["verification"])
                                if source.get("verification")
                                else library.verify(path, paper, verifier)
                            )
                            if source["evidence"] == "external_doi_mapping" and not (
                                result.title_match or result.doi_match
                            ):
                                result.status = "VERIFY_FAILED"
                                result.reasons.append("EXTERNAL_MAPPING_IDENTITY_NOT_CONFIRMED")
                            if result.status == "VERIFIED":
                                entry = library.add(
                                    path, paper, result, channel, version=source.get("version")
                                )
                                if not source.get("version"):
                                    break
                            else:
                                failures.append(
                                    {"doi": paper.doi, "path": str(path), "reasons": result.reasons}
                                )
                    if entry is None and not reasons and not historical:
                        seen = set()
                        for path, channel, expected in paths.get(paper.paper_id, []):
                            if not path.is_file() or path.resolve() in seen:
                                continue
                            seen.add(path.resolve())
                            result = library.verify(path, paper, verifier)
                            if expected and expected != result.sha256:
                                result.status = "VERIFY_FAILED"
                                result.reasons.append("HISTORICAL_HASH_CHANGED")
                            if result.status == "VERIFIED":
                                entry = library.add(path, paper, result, channel)
                                break
                            failures.append({"doi": paper.doi, "path": str(path), "reasons": result.reasons})
                    if entry:
                        # A fresh automatic acquisition supersedes an old Agent task only
                        # when this exact selected PDF was verified by a real download.
                        downloaded_automatically = catalog.db.execute(
                            "SELECT 1 FROM acquisition_attempts a "
                            "JOIN access_candidates c USING(candidate_id) "
                            "WHERE a.paper_id=? AND a.status='VERIFIED' "
                            "AND json_extract(a.result_json,'$.verification.sha256')=? "
                            "AND json_extract(c.candidate_json,'$.reason')!='VERIFIED_SHARED_LIBRARY' "
                            "LIMIT 1",
                            (paper.paper_id, entry["sha256"]),
                        ).fetchone()
                        if downloaded_automatically:
                            channel = "program"
                        entry = {**entry, "channel": channel}
                    catalog.execute(
                        "UPDATE artifacts SET status='UNVERIFIED' WHERE paper_id=?", (paper.paper_id,)
                    )
                    if entry:
                        adopt_library(catalog, paper, entry)
                    elif not reasons:
                        tasks.append(agent_task(catalog, paper, row))
                    rows.append(
                        {
                            "doi": row["doi"],
                            "title": paper.title,
                            "year": paper.year,
                            "publisher": row["publisher"],
                            "publisher_raw": paper.publisher_raw or "",
                            "status": "AVAILABLE" if entry else "REVIEW" if reasons else "MISSING",
                            "channel": entry["channel"] if entry else "",
                            "pdf_path": entry["absolute_path"] if entry else "",
                            "sha256": entry["sha256"] if entry else "",
                            "reason": "; ".join(reasons) if reasons else "",
                        }
                    )
                    if progress and (index % 20 == 0 or index + 1 == len(master)):
                        progress(
                            {
                                "processed": index + 1,
                                "total": len(master),
                                "available": sum(bool(r["pdf_path"]) for r in rows),
                            }
                        )
                refresh_user_states(catalog)
                export_master(catalog)
                pending = set(pending_acquisition(catalog))
                tasks = [task for task in tasks if normalize_doi(task["doi"]) not in pending]
                failed_dois = {normalize_doi(item["doi"]) for item in failures}
                for row in rows:
                    if not row["pdf_path"] and row["status"] != "REVIEW":
                        row["status"] = "PROGRAM_PENDING" if row["doi"] in pending else "AGENT_REQUIRED"
                        if row["doi"] in failed_dois:
                            row["reason"] = "LOCAL_PDF_IDENTITY_OR_INTEGRITY_REVIEW"
                report = {
                    "status": "PUBLISHED",
                    "created_at": config["created_at"],
                    "updated_at": now(),
                    "profile": config["profile"],
                    "catalog": os.path.relpath(root, destination),
                    "historical_counts": original,
                    "verification_failures": failures,
                    "program_pending": len(pending),
                }
                if compact:
                    report["compaction"] = compact_downloads(catalog, library)
            from ..reporting import render_collection

            render_collection(destination, config["label"], rows, tasks, report)
            # Keep internal and user-facing task lists identical after import/reverification.
            write_agent_tasks(root / "agent_tasks", tasks)
            # Offline attachment import happens after the complete PDF result and task list exist.
            # Attachment failures have their own status and never fail PDF publication.
            from ..attachments.service import safe_publish

            attachment_report = safe_publish(root)
            if attachment_report is not None:
                report["attachments"] = attachment_report
                atomic_json(destination / "run.json", report)
            return {"result_dir": str(destination), **report}


def compact_downloads(catalog, library):
    """Journal before deleting exact shared-library copies; keep unknown bytes recoverable."""
    root = catalog.root
    known = {row["sha256"]: library.root / row["path"] for row in library.db.execute("SELECT * FROM files")}
    journal = root / "history" / ("compact-" + uuid4().hex + ".json")
    actions = []
    for path in sorted((root / "downloads").rglob("*.pdf")):
        if not path.resolve().is_relative_to(root / "downloads"):
            raise ValueError("DOWNLOAD_PATH_ESCAPE")
        digest = file_hash(path)
        target = known.get(digest)
        if target and file_hash(target) == digest:
            actions.append(
                {
                    "old": str(path),
                    "target": str(target),
                    "sha256": digest,
                    "action": "deduplicate",
                    "bytes": path.stat().st_size,
                }
            )
        elif "staging" in path.parts or "program" in path.parts:
            target = root / "history" / "unverified" / path.relative_to(root / "downloads")
            actions.append(
                {
                    "old": str(path),
                    "target": str(target),
                    "sha256": digest,
                    "action": "archive",
                    "bytes": path.stat().st_size,
                }
            )
    atomic_json(journal, {"state": "PLANNED", "actions": actions})
    reclaimed = 0
    for action in actions:
        old, target = Path(action["old"]), Path(action["target"])
        if action["action"] == "archive":
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if file_hash(target) != action["sha256"]:
                    raise ValueError("ARCHIVE_CONFLICT")
            else:
                # Hard link then update paths; interrupted runs still retain a recoverable source.
                os.link(old, target)
        relative = os.path.relpath(target, root)
        with catalog.db:
            for spelling in (str(old), str(old.relative_to(root)), old.relative_to(root).as_posix()):
                catalog.db.execute("UPDATE artifacts SET path=? WHERE path=?", (relative, spelling))
                catalog.db.execute(
                    "UPDATE acquisition_attempts SET staging_path=? WHERE staging_path=?",
                    (relative, spelling),
                )
        if file_hash(target) != action["sha256"]:
            raise ValueError("COMPACTION_HASH_MISMATCH")
        old.unlink()
        if action["action"] == "deduplicate":
            reclaimed += action["bytes"]
    atomic_json(journal, {"state": "COMPLETED", "actions": actions, "reclaimed_bytes": reclaimed})
    return {"actions": len(actions), "reclaimed_bytes": reclaimed, "journal": str(journal)}
