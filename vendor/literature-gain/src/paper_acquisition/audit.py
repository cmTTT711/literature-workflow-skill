"""Offline database, provenance, raw hash and native metadata completeness audit."""

import json

from .normalization import finalize
from .providers import registry
from .storage import Catalog, file_hash


def audit(root):
    with Catalog(root) as catalog:
        report = {
            "integrity": catalog.db.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_errors": len(catalog.db.execute("PRAGMA foreign_key_check").fetchall()),
            "papers": catalog.db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
            "source_records": catalog.db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0],
            "raw_pages": 0,
            "raw_errors": 0,
            "provenance_errors": 0,
            "providers": {},
        }
        for path, digest in catalog.db.execute("SELECT path,sha256 FROM raw_pages"):
            report["raw_pages"] += 1
            file = catalog.root / path
            if not file.is_file() or file_hash(file) != digest:
                report["raw_errors"] += 1
        for source, data in catalog.db.execute(
            "SELECT record_id,paper_json FROM source_records JOIN papers USING(paper_id)"
        ):
            if source not in json.loads(data)["provenance"]:
                report["provenance_errors"] += 1
        for paper in catalog.iter_papers():
            for source in paper.provenance:
                row = catalog.db.execute(
                    "SELECT paper_id FROM source_records WHERE record_id=?", (source,)
                ).fetchone()
                if row is None or row[0] != paper.paper_id:
                    report["provenance_errors"] += 1
        adapters = registry(include_metadata_sources=True)
        for provider in adapters:
            stats = {
                "unique_payloads": 0,
                "normalization_errors": 0,
                "fields_present": {
                    key: 0 for key in ("doi", "title", "authors", "journal", "year", "publisher_raw")
                },
                "years": {},
            }
            for (payload,) in catalog.db.execute(
                "SELECT DISTINCT payload_json FROM source_records WHERE provider=?", (provider,)
            ):
                stats["unique_payloads"] += 1
                try:
                    paper = finalize(adapters[provider](None).normalize(json.loads(payload)))
                except (ValueError, TypeError, KeyError, AttributeError):
                    stats["normalization_errors"] += 1
                    continue
                for field in stats["fields_present"]:
                    stats["fields_present"][field] += bool(getattr(paper, field))
                if paper.year:
                    key = str(paper.year)
                    stats["years"][key] = stats["years"].get(key, 0) + 1
            report["providers"][provider] = stats
        return report
