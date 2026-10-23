"""Attachment state never writes to the article catalog or PDF library."""

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path

from ..library.store import file_hash
from ..models import now
from ..normalization import normalize_doi

MAX_BYTES = 100 * 1024 * 1024


def paper_key(doi):
    return "doi_" + hashlib.sha256(normalize_doi(doi).encode()).hexdigest()[:16]


def filename(name, digest, extension):
    stem = re.sub(r"[^\w .-]", "_", Path(name).stem).strip(" .")[:24] or "attachment"
    return digest[:16] + "_" + stem + extension


def validate(body, name):
    """Check containers without requiring an article DOI/title inside supplementary files."""
    if not body or len(body) > MAX_BYTES:
        raise ValueError("EMPTY_OR_OVERSIZED_ATTACHMENT")
    start = body[:4096].lstrip().lower()
    if any(token in start for token in (b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("HTML_RESPONSE_NOT_ATTACHMENT")
    if re.search(rb"<(?:[a-z0-9_.-]+:)?(?:rss|feed)(?:\s|>)", start):
        raise ValueError("SITE_FEED_NOT_ATTACHMENT")
    suffix = Path(name).suffix.lower()
    if body.startswith(b"%PDF-"):
        if b"%%EOF" not in body[-4096:]:
            raise ValueError("INCOMPLETE_PDF_ATTACHMENT")
        return ".pdf"
    if body.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = set(archive.namelist())
                if not names:
                    raise ValueError("EMPTY_ARCHIVE")
                for prefix, ext in (("word/", ".docx"), ("xl/", ".xlsx"), ("ppt/", ".pptx")):
                    if "[Content_Types].xml" in names and any(n.startswith(prefix) for n in names):
                        return ext
        except zipfile.BadZipFile:
            raise ValueError("INVALID_ZIP_ATTACHMENT") from None
        return ".zip"
    for magic, ext in (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF8", ".gif"),
        (b"II*\x00", ".tif"),
        (b"MM\x00*", ".tif"),
        (b"\x1f\x8b", ".gz"),
    ):
        if body.startswith(magic):
            return ext
    if body[4:8] == b"ftyp":
        return ".mp4"
    if body.startswith(bytes.fromhex("d0cf11e0a1b11ae1")) and suffix in {".doc", ".xls", ".ppt"}:
        return suffix
    if suffix in {".txt", ".csv", ".tsv", ".json", ".xml", ".cif", ".bib", ".tex"}:
        try:
            text = body.decode("utf-8-sig")
            if "\x00" in text or start.startswith((b"access denied", b"forbidden", b"unauthorized")):
                raise ValueError("INVALID_TEXT_ATTACHMENT")
            if suffix == ".json":
                json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("INVALID_TEXT_ATTACHMENT") from None
        return suffix
    raise ValueError("UNSUPPORTED_ATTACHMENT_FORMAT")


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        catalog_path = self.root / "catalog.sqlite"
        if not catalog_path.is_file():
            raise ValueError("CATALOG_NOT_FOUND")
        self.home = self.root / "attachments"
        if not self.home.resolve().is_relative_to(self.root):
            raise ValueError("ATTACHMENT_PATH_ESCAPE")
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "state.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS papers(
                doi TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                scan_state TEXT NOT NULL DEFAULT 'NOT_SCANNED', scan_error TEXT NOT NULL DEFAULT '',
                manual_state TEXT NOT NULL DEFAULT '', updated_at TEXT);
            CREATE TABLE IF NOT EXISTS candidates(
                doi TEXT NOT NULL, url TEXT NOT NULL, page_url TEXT NOT NULL, label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', error TEXT NOT NULL DEFAULT '',
                sha256 TEXT, final_url TEXT, PRIMARY KEY(doi,url));
            CREATE TABLE IF NOT EXISTS files(
                doi TEXT NOT NULL, sha256 TEXT NOT NULL, channel TEXT NOT NULL,
                name TEXT NOT NULL, path TEXT NOT NULL, bytes INTEGER NOT NULL,
                provenance TEXT NOT NULL, PRIMARY KEY(doi,sha256,channel));
        """)
        self.sync(catalog_path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def sync(self, catalog_path):
        with sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True) as source:
            excluded = {r[0] for r in source.execute("SELECT paper_id FROM master_exclusions")}
            rows = source.execute("SELECT paper_id,paper_json FROM papers").fetchall()
            self.article_hashes = {
                r[0] for r in source.execute("SELECT sha256 FROM artifacts WHERE status='VERIFIED'")
            }
        self.active = set()
        for pid, raw in rows:
            paper = json.loads(raw)
            doi = normalize_doi(paper.get("doi"))
            if not doi or pid in excluded or doi.startswith("10.13039/"):
                continue
            self.active.add(doi)
            paper["doi"] = doi
            self.db.execute(
                "INSERT INTO papers(doi,metadata) VALUES(?,?) ON CONFLICT(doi) "
                "DO UPDATE SET metadata=excluded.metadata",
                (doi, json.dumps(paper, ensure_ascii=False)),
            )
        self.db.commit()

    def papers(self):
        return [
            dict(r) for r in self.db.execute("SELECT * FROM papers ORDER BY doi") if r["doi"] in self.active
        ]

    def agent_dir(self, doi):
        return self.home / "agent" / paper_key(doi)

    def put(self, doi, body, name, channel, provenance):
        extension = validate(body, name)
        digest = hashlib.sha256(body).hexdigest()
        if digest in self.article_hashes:
            raise ValueError("ARTICLE_PDF_IS_NOT_ATTACHMENT")
        path = self.home / "blobs" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if file_hash(path) != digest:
                raise ValueError("ATTACHMENT_BLOB_CHANGED")
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(path)
        self.db.execute(
            "INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?,?)",
            (
                doi,
                digest,
                channel,
                filename(name, digest, extension),
                str(path.relative_to(self.home)),
                len(body),
                json.dumps(provenance, ensure_ascii=False),
            ),
        )
        self.db.commit()
        return digest

    def import_agent(self):
        errors = []
        for row in self.papers():
            doi = row["doi"]
            folder = self.agent_dir(doi)
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    if (
                        not folder.resolve().is_relative_to(self.home / "agent")
                        or not path.resolve().is_relative_to(folder.resolve())
                        or path.is_symlink()
                    ):
                        raise ValueError("ATTACHMENT_PATH_ESCAPE")
                    if path.stat().st_size > MAX_BYTES:
                        raise ValueError("OVERSIZED_ATTACHMENT")
                    self.put(doi, path.read_bytes(), path.name, "agent", {"input_path": str(path)})
                except (ValueError, OSError) as error:
                    errors.append({"doi": doi, "path": str(path), "error": str(error)})
        return errors

    def files(self):
        result = []
        for row in self.db.execute("SELECT * FROM files ORDER BY doi,sha256,channel"):
            row = dict(row)
            if row["doi"] not in self.active:
                continue
            path = self.home / row["path"]
            if not path.resolve().is_relative_to((self.home / "blobs").resolve()):
                raise ValueError("ATTACHMENT_PATH_ESCAPE")
            if not path.is_file() or file_hash(path) != row["sha256"]:
                raise ValueError("ATTACHMENT_BLOB_MISSING_OR_CHANGED: " + row["sha256"])
            result.append(row)
        return result

    def scan(self, doi, state, error=""):
        self.db.execute(
            "UPDATE papers SET scan_state=?,scan_error=?,updated_at=? WHERE doi=?", (state, error, now(), doi)
        )
        self.db.commit()
