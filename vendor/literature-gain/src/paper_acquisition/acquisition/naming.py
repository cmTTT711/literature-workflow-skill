"""Persistent, Windows-safe year/title names shared by all acquisition routes."""

import re
import unicodedata
from pathlib import Path

from ..storage import file_hash


def pdf_filename(year, title, *, max_units=180, suffix=""):
    title = unicodedata.normalize("NFKC", str(title or "Untitled"))
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" .") or "Untitled"
    prefix = f"{year or 'unknown_year'}_"
    budget = max_units - len((prefix + suffix + ".pdf").encode("utf-16-le")) // 2
    if budget < 12:
        raise ValueError("PDF_DIRECTORY_PATH_TOO_LONG")
    title = title.encode("utf-16-le")[: budget * 2].decode("utf-16-le", errors="ignore").rstrip(" .")
    return prefix + title + suffix + ".pdf"


def paper_target(catalog, folder, paper):
    folder = Path(folder).resolve()
    if not folder.is_relative_to(catalog.root / "downloads"):
        raise ValueError("PDF_PATH_ESCAPE")
    relative = str(folder.relative_to(catalog.root))
    row = catalog.db.execute(
        "SELECT filename FROM pdf_names WHERE folder=? AND paper_id=?", (relative, paper.paper_id)
    ).fetchone()
    if row:
        return folder / row[0]
    # Stay below legacy Windows MAX_PATH as well as the per-component limit.
    limit = min(180, 245 - len(str(folder).encode("utf-16-le")) // 2)
    for number in range(10000):
        suffix = "" if number == 0 else f"_{number + 1}"
        name = pdf_filename(paper.year, paper.title, max_units=limit, suffix=suffix)
        occupied = catalog.db.execute(
            "SELECT 1 FROM pdf_names WHERE folder=? AND filename=? COLLATE NOCASE", (relative, name)
        ).fetchone()
        if not occupied and not (folder / name).exists():
            catalog.execute("INSERT INTO pdf_names VALUES(?,?,?)", (relative, paper.paper_id, name))
            return folder / name
    raise ValueError("PDF_NAME_COLLISION_LIMIT")


def version_target(target, digest):
    """Keep different PDF versions of one paper without overwriting either."""
    if not target.exists() or file_hash(target) == digest:
        return target
    return target.with_name(target.stem + "_" + digest[:12] + ".pdf")
