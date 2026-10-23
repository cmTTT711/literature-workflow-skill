"""Deterministic publisher/keyword classification without AI or network access."""

import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from ..acquisition.naming import pdf_filename
from ..library.collections import safe_label
from ..library.store import atomic_json, library_root
from ..locking import RunLock
from ..reporting import copy_verified, replace_directory


def refresh_classifications(run_dir):
    """Rebuild only schemes belonging to this publication; historical snapshots stay independent."""
    run_dir = Path(run_dir).resolve()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    catalog = (run_dir / run["catalog"]).resolve()
    manifest = json.loads((run_dir / run["manifest"]).read_text(encoding="utf-8"))
    plans = dict(manifest.get("classifications", {}))
    for path in (catalog / "classifications").glob("*.json"):
        saved = json.loads(path.read_text(encoding="utf-8"))
        output = Path(saved["output"]).resolve()
        if output.parent == run_dir / "classifications":
            plans.setdefault(output.name, saved)
    classify(run_dir, by="publisher")
    for name, saved in plans.items():
        if name != "by_publisher":
            classify(run_dir, by=saved["method"], rules=saved.get("rules"), name=name)


def classify(run_dir, *, by="publisher", rules=None, name=None):
    run_dir = Path(run_dir).resolve()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    catalog = (run_dir / run["catalog"]).resolve()
    manifest = json.loads((run_dir / run["manifest"]).resolve().read_text(encoding="utf-8"))
    if (catalog / manifest["result"]).resolve() != run_dir:
        raise ValueError("RUN_MANIFEST_MISMATCH")
    if by not in {"publisher", "keywords"}:
        raise ValueError("UNKNOWN_CLASSIFICATION_METHOD")
    if by == "keywords" and (
        not rules
        or any(
            not isinstance(v, list) or not v or any(not isinstance(w, str) or not w for w in v)
            for v in rules.values()
        )
    ):
        raise ValueError("KEYWORD_RULES_REQUIRED")
    if rules and len({safe_label(k).casefold() for k in rules}) != len(rules):
        raise ValueError("CLASSIFICATION_NAME_COLLISION")
    name = safe_label(name or ("by_publisher" if by == "publisher" else "by_keywords"))
    with RunLock(catalog / "classification.lock"):
        working = library_root(catalog).parent
        stage = working / "temp" / uuid4().hex[:8]
        stage.mkdir(parents=True)
        counts = Counter()
        for row in manifest["rows"]:
            source = (run_dir / row["pdf_path"]).resolve()
            if source.parent != run_dir / "pdfs/all":
                raise ValueError("CLASSIFICATION_SOURCE_ESCAPE")
            if by == "publisher":
                groups = [row["publisher"] or "unknown"]
            else:
                from pypdf import PdfReader

                text = (
                    row["title"] + "\n" + "\n".join(p.extract_text() or "" for p in PdfReader(source).pages)
                ).casefold()
                groups = [
                    group for group, words in rules.items() if any(w.casefold() in text for w in words)
                ] or ["unclassified"]
            for group in set(groups):
                group = safe_label(group)
                import hashlib

                limit = min(
                    82,
                    240 - len(str(run_dir / "classifications" / name / group)),
                    240 - len(str(stage / group)),
                )
                filename = pdf_filename(
                    row["year"],
                    row["title"],
                    max_units=limit,
                    suffix="_" + hashlib.sha256(row["doi"].encode()).hexdigest()[:12],
                )
                copy_verified(source, stage / group / filename, row["sha256"])
                counts[group] += 1
        target = run_dir / "classifications" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_directory(stage, target, working / "history/classifications")
        report = {"method": by, "rules": rules, "counts": dict(counts), "output": str(target)}
        manifest.setdefault("classifications", {})[name] = {"method": by, "rules": rules}
        atomic_json((run_dir / run["manifest"]).resolve(), manifest)
        atomic_json(catalog / "classifications" / (name + ".json"), report)
        return report
