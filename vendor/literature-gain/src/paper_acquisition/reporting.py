"""Publish standalone PDF copies; machine manifests stay in the working catalog."""

import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from uuid import uuid4

from .acquisition.naming import pdf_filename
from .library.store import atomic_json, file_hash, library_root
from .master import write_agent_tasks
from .normalization import normalize_doi


def copy_verified(source, target, digest):
    source, target = Path(source).resolve(), Path(target)
    if file_hash(source) != digest:
        raise ValueError("SOURCE_PDF_CHANGED: " + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if file_hash(target) != digest:
        raise ValueError("PDF_COPY_HASH_MISMATCH: " + str(target))


def replace_directory(staged, destination, history):
    """Retain the previous output for recovery before publishing a complete new tree."""
    staged, destination = Path(staged), Path(destination)
    old = None
    if destination.exists():
        staged_files = {p.relative_to(staged): p for p in staged.rglob("*") if p.is_file()}
        existing_files = {p.relative_to(destination): p for p in destination.rglob("*") if p.is_file()}
        if staged_files.keys() == existing_files.keys() and all(
            file_hash(p) == file_hash(existing_files[k]) for k, p in staged_files.items()
        ):
            if staged.resolve() == destination.resolve() or not staged.resolve().is_relative_to(
                Path(history).resolve().parent.parent
            ):
                raise ValueError("STAGING_PATH_ESCAPE")
            shutil.rmtree(staged)
            return
    if destination.exists():
        history = Path(history)
        history.mkdir(parents=True, exist_ok=True)
        old = history / (destination.name + "-" + uuid4().hex[:12])
        destination.rename(old)
    try:
        staged.rename(destination)
    except BaseException:
        if old is not None:
            old.rename(destination)
        raise


def render_collection(destination, label, rows, tasks, report):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    catalog = (destination / report["catalog"]).resolve()
    working = library_root(catalog).parent
    stage = working / "temp" / uuid4().hex[:8]
    history = working / "history/publications"
    for name in ("automatic", "agent", "all"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    seen, published = set(), []
    for row in rows:
        if not row["pdf_path"]:
            continue
        doi = normalize_doi(row["doi"])
        if not doi or doi in seen:
            raise ValueError("DUPLICATE_OR_MISSING_DOI: " + str(doi))
        seen.add(doi)
        if row["channel"] not in ("program", "agent"):
            raise ValueError("UNKNOWN_DOWNLOAD_CHANNEL")
        limit = min(
            82,
            240 - len(str(destination / "classifications/by_publisher/springer_nature")),
            240 - len(str(stage / "automatic")),
        )
        name = pdf_filename(
            row["year"],
            row["title"],
            max_units=limit,
            suffix="_" + hashlib.sha256(doi.encode()).hexdigest()[:12],
        )
        channel = "automatic" if row["channel"] == "program" else "agent"
        for folder in (channel, "all"):
            copy_verified(row["pdf_path"], stage / folder / name, row["sha256"])
        published.append({**row, "doi": doi, "pdf_path": "pdfs/all/" + name})
    replace_directory(stage, destination / "pdfs", history)
    for path in list(destination.iterdir()):
        if path.name not in {"pdfs", "classifications", "agent_tasks", "run.json", "attachment"}:
            history.mkdir(parents=True, exist_ok=True)
            path.rename(history / (uuid4().hex[:8] + "-" + path.name))
    manifest = (
        catalog / "publications" / (hashlib.sha256(destination.name.encode()).hexdigest()[:16] + ".json")
    )
    schemes = (
        json.loads(manifest.read_text(encoding="utf-8")).get("classifications", {})
        if manifest.exists()
        else {}
    )
    atomic_json(
        manifest,
        {"result": os.path.relpath(destination, catalog), "rows": published, "classifications": schemes},
    )
    report["manifest"] = os.path.relpath(manifest, destination)
    write_agent_tasks(destination / "agent_tasks", tasks, by_publisher=False)
    report.update(
        total=len(rows),
        available=len(published),
        agent_required=len(tasks),
        by_channel=dict(Counter(r["channel"] for r in published)),
        by_status=dict(Counter(r["status"] for r in rows)),
    )
    atomic_json(destination / "run.json", report)
    from .toolbox.classification import refresh_classifications

    refresh_classifications(destination)
    return report
