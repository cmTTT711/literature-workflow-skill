from .models import Publisher
from .normalization import normalize_doi


def merge(target, incoming):
    for field in (
        "doi",
        "title",
        "authors",
        "year",
        "journal",
        "publisher_raw",
        "abstract",
        "document_type",
        "publication_date",
        "language",
    ):
        if not getattr(target, field) and getattr(incoming, field):
            setattr(target, field, getattr(incoming, field))
    if target.publisher_normalized == Publisher.OTHER:
        target.publisher_normalized = incoming.publisher_normalized
    for field in ("source_hits", "provenance", "landing_urls", "flags", "possible_duplicates", "oa_metadata"):
        values = getattr(target, field)
        for value in getattr(incoming, field):
            if value not in values:
                values.append(value)
    for namespace, values in incoming.external_ids.items():
        target.external_ids[namespace] = list(dict.fromkeys(target.external_ids.get(namespace, []) + values))
    return target


class Deduplicator:
    """DOI is the sole merge key; DOI-less debug records never enter the formal master."""

    def __init__(self, papers):
        self.papers = papers
        self.by_doi = {}
        self.comparisons = 0
        for paper in papers:
            self.refresh(paper)

    def refresh(self, paper):
        if key := normalize_doi(paper.doi):
            self.by_doi[key] = paper

    def add(self, incoming):
        key = normalize_doi(incoming.doi)
        existing = self.by_doi.get(key) if key else None
        if existing:
            self.comparisons += 1
            return merge(existing, incoming)
        self.papers.append(incoming)
        self.refresh(incoming)
        return incoming
