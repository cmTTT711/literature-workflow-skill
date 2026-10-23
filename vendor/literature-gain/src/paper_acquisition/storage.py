import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from .models import Paper, now


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_csv(stream, rows, fields, *, escape_fields=None, sort_keys=False):
    """Keep identifiers exact while escaping the caller's free-form spreadsheet columns."""
    escaped = set(fields if escape_fields is None else escape_fields)
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {}
        for key in fields:
            value = row[key]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=sort_keys)
            if key in escaped and isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                value = "'" + value
            values[key] = value
        writer.writerow(values)


def write_new(path: Path, content: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content.encode("utf-8") if isinstance(content, str) else content)


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "catalog.sqlite")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        # Retire the unused orchestration placeholder only when no historical rows exist.
        if (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='acquisition_tasks' AND type='table'"
            ).fetchone()
            and not self.db.execute("SELECT 1 FROM acquisition_tasks LIMIT 1").fetchone()
        ):
            self.db.execute("DROP TABLE acquisition_tasks")
        self.db.commit()

    def execute(self, sql, params=()):
        with self.db:
            return self.db.execute(sql, params)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def save_candidate(self, paper_id, candidate, *, replace=True):
        conflict = (
            "DO UPDATE SET priority=excluded.priority,eligible=excluded.eligible,"
            "candidate_json=excluded.candidate_json"
            if replace
            else "DO NOTHING"
        )
        self.execute(
            "INSERT INTO access_candidates VALUES(?,?,?,?,?) ON CONFLICT(candidate_id) " + conflict,
            (
                candidate.candidate_id,
                paper_id,
                candidate.priority,
                int(candidate.eligible),
                candidate.model_dump_json(),
            ),
        )

    def start_run(self, profile):
        run_id = uuid4().hex
        self.execute(
            "INSERT INTO search_runs VALUES(?,?,NULL,?,'RUNNING',NULL)",
            (run_id, now(), profile.model_dump_json()),
        )
        return run_id

    def query(self, run_id, provider, user_query):
        query_id = uuid4().hex
        self.execute(
            "INSERT INTO provider_queries(query_id,run_id,provider,user_query,started_at,status) "
            "VALUES(?,?,?,?,?,'RUNNING')",
            (query_id, run_id, provider, user_query, now()),
        )
        return query_id

    def raw_response(self, run_id, provider, query_id, request, response, attempt):
        page_id = uuid4().hex
        path = self.root / "discovery" / "raw" / provider / run_id / query_id / f"{page_id}.body"
        write_new(path, response.content)
        metadata = {
            "request": request,
            "queried_at": now(),
            "http_status": response.status_code,
            "attempt": attempt,
            "content_type": response.headers.get("content-type"),
            "headers": {
                k: v
                for k, v in response.headers.items()
                if k.lower() == "retry-after" or k.lower().startswith(("x-ratelimit-", "x-rate-limit-"))
            },
        }
        write_new(path.with_suffix(".json"), dumps(metadata))
        self.execute(
            "INSERT INTO raw_pages VALUES(?,?,?,?,?)",
            (
                page_id,
                query_id,
                str(path.relative_to(self.root)),
                hashlib.sha256(response.content).hexdigest(),
                dumps(metadata),
            ),
        )
        return str(path.relative_to(self.root))

    def papers(self):
        return list(self.iter_papers())

    def iter_papers(self):
        for row in self.db.execute("SELECT paper_json FROM papers ORDER BY paper_id"):
            yield Paper.model_validate_json(row[0])

    def save_paper(self, paper):
        self.execute(
            "INSERT INTO papers VALUES(?,?,?) ON CONFLICT(paper_id) DO UPDATE SET "
            "doi=excluded.doi,paper_json=excluded.paper_json",
            (paper.paper_id, paper.doi, paper.model_dump_json()),
        )

    def export(self):
        # Immutable snapshots: no previous export is silently overwritten.
        destination = self.root / "exports" / uuid4().hex
        destination.mkdir(parents=True)
        with (
            (destination / "master_papers.csv").open("x", encoding="utf-8-sig", newline="") as stream,
            (destination / "master_papers.jsonl").open("x", encoding="utf-8") as jsonl,
        ):

            def rows():
                for model in self.iter_papers():
                    paper = model.model_dump(mode="json")
                    jsonl.write(dumps(paper) + "\n")
                    yield paper

            write_csv(stream, rows(), list(Paper.model_fields), sort_keys=True)
        return destination
