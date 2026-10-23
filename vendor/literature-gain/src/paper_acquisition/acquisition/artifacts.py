"""Verified artifact persistence; shared library for managed collections."""

import hashlib
import os
import shutil
from uuid import uuid4

from ..models import now
from ..storage import dumps, file_hash
from .models import AcquisitionError


def verified_artifact(catalog, paper_id):
    for path, digest in catalog.db.execute(
        "SELECT path,sha256 FROM artifacts WHERE paper_id=? AND status='VERIFIED'", (paper_id,)
    ):
        file = (catalog.root / path).resolve()
        if file.is_file() and file_hash(file) == digest:
            return True
    return False


def promote(catalog, paper, attempt_id, staging, candidate, result):
    from ..library.store import Library, library_root
    from .naming import paper_target, version_target

    if shared := library_root(catalog.root):
        with Library(shared) as library:
            entry = library.add(staging, paper, result, version=candidate.version)
        return register_library_artifact(catalog, paper, attempt_id, entry)

    existing = catalog.db.execute(
        "SELECT path FROM artifacts WHERE sha256=? AND status='VERIFIED'", (result.sha256,)
    ).fetchall()
    target = None
    for (path,) in existing:
        file = (catalog.root / path).resolve()
        if file.is_file() and file_hash(file) == result.sha256:
            target = file
            break
    duplicate = target is not None
    if target is None:
        source = candidate.source_type if candidate.source_type != "semantic_scholar" else "other"
        target = version_target(
            paper_target(catalog, catalog.root / "downloads" / "program" / source, paper), result.sha256
        )
        if not target.resolve().is_relative_to((catalog.root / "downloads" / "program" / source).resolve()):
            raise AcquisitionError("ARTIFACT_PATH_ESCAPE")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_hash(target) != result.sha256:
                raise AcquisitionError("IMMUTABLE_ARTIFACT_CONFLICT")
        else:
            # Publish an already-complete copy atomically, without replacement.
            temporary = target.with_suffix(f".{uuid4().hex}.part")
            try:
                with staging.open("rb") as src, temporary.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    if file_hash(target) != result.sha256:
        raise AcquisitionError("PROMOTION_HASH_MISMATCH")
    catalog.execute(
        "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            uuid4().hex,
            paper.paper_id,
            attempt_id,
            "program",
            "VERIFIED",
            str(target.relative_to(catalog.root)),
            result.sha256,
            result.size_bytes,
            now(),
            dumps({**result.model_dump(), "duplicate_file": duplicate}),
        ),
    )
    return {"artifact_path": str(target.relative_to(catalog.root)), "duplicate_file": duplicate}


def register_library_artifact(catalog, paper, attempt_id, entry):
    path = os.path.relpath(entry["absolute_path"], catalog.root)
    catalog.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id,sha256) "
        "DO UPDATE SET path=excluded.path,status=excluded.status,channel=excluded.channel,verification_json=excluded.verification_json",
        (
            uuid4().hex,
            paper.paper_id,
            attempt_id,
            entry["channel"],
            "VERIFIED",
            path,
            entry["sha256"],
            entry["size_bytes"],
            now(),
            entry["verification_json"],
        ),
    )
    return {"artifact_path": path, "duplicate_file": True, "shared_library": True}


def adopt_library(catalog, paper, entry, channel=None):
    """Record local reuse with its own provenance, without an HTTP request."""
    from .models import Candidate

    candidate = Candidate(
        candidate_id=hashlib.sha256((paper.paper_id + "|shared-library").encode()).hexdigest(),
        source_type="other",
        source_url="https://doi.org/" + entry["doi"],
        reason="VERIFIED_SHARED_LIBRARY",
        eligible=False,
    )
    catalog.save_candidate(paper.paper_id, candidate, replace=False)
    attempt_id = hashlib.sha256((candidate.candidate_id + entry["sha256"]).encode()).hexdigest()
    catalog.execute(
        "INSERT OR IGNORE INTO acquisition_attempts VALUES(?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            paper.paper_id,
            candidate.candidate_id,
            now(),
            now(),
            "VERIFIED",
            "",
            dumps({"shared_library": True, "network_requests": 0}),
        ),
    )
    return register_library_artifact(
        catalog, paper, attempt_id, {**entry, "channel": channel or entry["channel"]}
    )
