"""One selected file per DOI; identical bytes are stored only once across runs."""

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from ..acquisition.models import AcquisitionError
from ..acquisition.naming import pdf_filename
from ..locking import RunLock
from ..models import now
from ..normalization import normalize_doi
from ..storage import file_hash


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def collection_config(root):
    path = Path(root) / "collection.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def library_root(root):
    config = collection_config(root)
    return (Path(root) / config["library"]).resolve() if config else None


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files(
                sha256 TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                size_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(
                doi TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES files,
                paper_json TEXT NOT NULL, channel TEXT NOT NULL,
                verification_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checks(
                cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL);
        """)
        if "version" not in {r[1] for r in self.db.execute("PRAGMA table_info(entries)")}:
            self.db.execute("ALTER TABLE entries ADD COLUMN version TEXT")
            self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def lookup(self, doi):
        row = self.db.execute(
            "SELECT * FROM entries JOIN files USING(sha256) WHERE doi=?", (normalize_doi(doi),)
        ).fetchone()
        if row:
            entry = dict(row)
            path = self.root / entry["path"]
            if not path.resolve().is_relative_to(self.root / "pdf"):
                raise ValueError("LIBRARY_PATH_ESCAPE")
            if path.is_file() and file_hash(path) == entry["sha256"]:
                entry["absolute_path"] = str(path)
                return entry
            raise ValueError("LIBRARY_FILE_MISSING_OR_CHANGED: " + str(path))
        return None

    def verify(self, source, paper, verifier):
        from ..acquisition.models import PDFResult

        digest = file_hash(source)
        key = hashlib.sha256(
            json.dumps(
                [
                    digest,
                    normalize_doi(paper.doi),
                    paper.title,
                    "verifier-2-wrapped-doi",
                    verifier.options.max_bytes,
                ]
            ).encode()
        ).hexdigest()
        row = self.db.execute("SELECT result_json FROM checks WHERE cache_key=?", (key,)).fetchone()
        if row:
            return PDFResult.model_validate_json(row[0])
        result = verifier.verify(source, paper)
        # Retry transient parser failures on subsequent publication attempts.
        if result.sha256 == digest:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO checks VALUES(?,?)", (key, result.model_dump_json()))
        return result

    def add(self, source, paper, result, channel="program", version=None):
        source = Path(source)
        doi = normalize_doi(paper.doi)
        if not doi or result.status != "VERIFIED":
            raise AcquisitionError("ARTICLE_IDENTITY_NOT_CONFIRMED")
        if not result.sha256 or file_hash(source) != result.sha256:
            raise AcquisitionError("PROMOTION_HASH_MISMATCH")
        with RunLock(self.root / "library.lock"):
            existing = self.lookup(doi)
            rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
            if existing and existing["sha256"] == result.sha256:
                if rank.get(version, 3) < rank.get(existing["version"], 3):
                    with self.db:
                        self.db.execute("UPDATE entries SET version=? WHERE doi=?", (version, doi))
                    return self.lookup(doi)
                return existing
            if existing and rank.get(version, 3) >= rank.get(existing["version"], 3):
                return existing
            row = self.db.execute("SELECT path FROM files WHERE sha256=?", (result.sha256,)).fetchone()
            if row:
                target = self.root / row[0]
                if not target.is_file() or file_hash(target) != result.sha256:
                    raise ValueError("LIBRARY_FILE_MISSING_OR_CHANGED")
            else:
                folder = self.root / "pdf"
                folder.mkdir(exist_ok=True)
                limit = min(170, 240 - len(str(folder).encode("utf-16-le")) // 2)
                # Stable DOI suffix prevents same-title and case-insensitive Windows collisions.
                name = pdf_filename(
                    paper.year,
                    paper.title,
                    max_units=limit,
                    suffix="_" + hashlib.sha256(doi.encode()).hexdigest()[:12] + "_" + result.sha256[:8],
                )
                target = folder / name
                if target.exists() and file_hash(target) != result.sha256:
                    raise ValueError("LIBRARY_FILENAME_CONFLICT")
                if not target.exists():
                    temporary = folder / (uuid4().hex + ".part")
                    try:
                        with source.open("rb") as src, temporary.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                            dst.flush()
                            os.fsync(dst.fileno())
                        if file_hash(temporary) != result.sha256:
                            raise ValueError("LIBRARY_COPY_HASH_MISMATCH")
                        os.link(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO files VALUES(?,?,?)",
                    (
                        result.sha256,
                        str(target.relative_to(self.root)),
                        target.stat().st_size,
                    ),
                )
                self.db.execute(
                    "INSERT INTO entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(doi) DO UPDATE SET "
                    "sha256=excluded.sha256,paper_json=excluded.paper_json,channel=excluded.channel,"
                    "verification_json=excluded.verification_json,version=excluded.version",
                    (
                        doi,
                        result.sha256,
                        paper.model_dump_json(),
                        channel,
                        result.model_dump_json(),
                        now(),
                        version,
                    ),
                )
            return self.lookup(doi)

    def audit(self):
        errors, hashes = [], {}
        known = set()
        for row in self.db.execute("SELECT * FROM files"):
            path = self.root / row["path"]
            known.add(path.resolve())
            if not path.is_file() or file_hash(path) != row["sha256"]:
                errors.append({"code": "MISSING_OR_CHANGED", "path": str(path)})
        for path in (self.root / "pdf").rglob("*.pdf"):
            digest = file_hash(path)
            if digest in hashes:
                errors.append({"code": "DUPLICATE_PDF", "path": str(path)})
            hashes[digest] = str(path)
            if path.resolve() not in known:
                errors.append({"code": "UNINDEXED_PDF", "path": str(path)})
        return {
            "papers": self.db.execute("SELECT count(*) FROM entries").fetchone()[0],
            "pdf_files": len(hashes),
            "errors": errors,
            "integrity": self.db.execute("PRAGMA integrity_check").fetchone()[0],
        }
