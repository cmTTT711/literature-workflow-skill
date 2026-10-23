import json
from abc import ABC, abstractmethod

from ..config import credential
from ..errors import MissingCredentials, QueryError, ResponseError
from ..models import Page, Paper


def phrase(value):
    return '"' + value.replace("\\", " ").replace('"', " ").strip() + '"'


def keyword_terms(value):
    """Split a user query into required keywords instead of one exact phrase."""
    return [item for item in value.replace("\\", " ").replace('"', " ").split() if item]


def boolean_query(query, profile, *, field="", negation="NOT", conjunction="AND"):
    def term(text):
        return field + phrase(text)

    required = [*keyword_terms(query), *profile.include_terms]
    result = f" {conjunction} ".join(term(item) for item in required)
    for item in profile.exclude_terms:
        result += f" {conjunction} {negation} " + term(item)
    return result


def decode(response):
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Expected an object")
        return value
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ResponseError("INVALID_JSON_OBJECT") from error


class DiscoveryProvider(ABC):
    name: str
    key_env: str | None = None
    key_required = False
    interval = 1.0
    first_cursor: str | int | None = None
    result_ceiling: int | None = None
    cache_ttl = 180
    allow_repeated_cursor = False
    server_document_types = frozenset()
    server_languages = frozenset()

    def __init__(self, transport):
        self.transport = transport
        self.key = credential(self.key_env) if self.key_env else None

    def check(self):
        if self.key_required and not self.key:
            raise MissingCredentials(f"MISSING_{self.key_env}")

    def warnings(self, profile):
        notes = []
        if any(t.casefold() not in self.server_document_types for t in profile.document_types):
            notes.append("document_types retained for screening; no cross-provider type equivalence assumed")
        if (
            profile.language
            and self.server_languages is not None
            and profile.language.casefold() not in self.server_languages
        ):
            notes.append("language retained for screening; not applied server-side")
        if set(profile.extensions) - {"require_terms_in_title_or_abstract"}:
            notes.append("unknown extensions retained but not applied")
        if notes and profile.strict_capabilities:
            raise QueryError("UNSUPPORTED_PROFILE_CAPABILITY")
        return notes

    @classmethod
    def capabilities(cls):
        return {
            "implemented": cls.name != "core",
            "provider": cls.name,
            "requires_credentials": cls.key_required,
            "pagination": "cursor" if not isinstance(cls.first_cursor, int) else "offset_or_page",
            "result_ceiling": cls.result_ceiling,
            "year_slicing": True,
            "document_types": "server_query" if cls.server_document_types else "audit_hint",
            "language": "server_query"
            if cls.server_languages is None or cls.server_languages
            else "audit_hint",
            "supported_document_types": sorted(cls.server_document_types),
            "supported_languages": sorted(cls.server_languages) if cls.server_languages is not None else None,
            "exclude_terms": "audit_hint" if cls.name == "crossref" else "server_query",
            "cache_ttl": cls.cache_ttl,
        }

    @abstractmethod
    def translate(self, profile, query):
        """Return provider-specific public parameters or JSON/DSL body."""

    @abstractmethod
    async def fetch(self, translated, cursor, size) -> Page:
        """Retrieve one page. Search service owns pagination, raw persistence and UNION."""

    @abstractmethod
    def normalize(self, record) -> Paper:
        """Map provider fields to canonical bibliographic metadata."""
