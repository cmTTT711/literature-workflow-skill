import hashlib
import re
from pathlib import Path

from ..models import Publisher
from ..normalization import normalize_doi


def initialize_layout(root: Path):
    # Publisher/source folders are created on demand, rather than dozens of empty directories.
    (root / "downloads" / "program" / "staging").mkdir(parents=True, exist_ok=True)


def safe_doi_filename(doi):
    canonical = normalize_doi(doi)
    if not canonical:
        raise ValueError("INVALID_DOI")
    readable = re.sub(r"[^a-z0-9.-]", "_", canonical)[:64].rstrip(". ")
    return f"{readable}--{hashlib.sha256(canonical.encode()).hexdigest()}.pdf"


def agent_target(root: Path, publisher: Publisher, doi: str) -> Path:
    publisher = Publisher(publisher)
    base = (root / "downloads" / "agent").resolve()
    target = (base / publisher.value / safe_doi_filename(doi)).resolve()
    if not target.is_relative_to(base):
        raise ValueError("agent target escaped agent directory")
    return target
