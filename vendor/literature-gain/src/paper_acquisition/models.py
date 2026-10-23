from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


class SearchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1)
    queries: list[str] = Field(default_factory=list)
    include_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    year_from: int | None = Field(default=None, ge=1000, le=9999)
    year_to: int | None = Field(default=None, ge=1000, le=9999)
    document_types: list[str] = Field(default_factory=list)
    language: str | None = None
    max_results_per_provider: int = Field(default=500, ge=1)
    extensions: dict[str, Any] = Field(default_factory=dict)
    year_slice_size: int | None = Field(default=None, ge=1, le=100)
    strict_capabilities: bool = False

    @model_validator(mode="after")
    def validate_profile(self):
        if not self.topic.strip() or any(not q.strip() for q in self.queries):
            raise ValueError("topic/queries cannot be blank")
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not exceed year_to")
        if self.year_slice_size and not (self.year_from and self.year_to):
            raise ValueError("year slicing requires both year_from and year_to")
        return self

    def variants(self) -> list[str]:
        return list(dict.fromkeys(self.queries or [self.topic]))

    def search_tasks(self):
        for query in self.variants():
            if self.year_slice_size:
                for start in range(self.year_from, self.year_to + 1, self.year_slice_size):
                    yield (
                        self.model_copy(
                            update={
                                "year_from": start,
                                "year_to": min(self.year_to, start + self.year_slice_size - 1),
                            }
                        ),
                        query,
                    )
            else:
                yield self, query


class Publisher(StrEnum):
    ELSEVIER = "elsevier"
    APS = "aps"
    AIP = "aip"
    SPRINGER_NATURE = "springer_nature"
    WILEY = "wiley"
    ACS = "acs"
    IOP = "iop"
    RSC = "rsc"
    TAYLOR_FRANCIS = "taylor_francis"
    IEEE = "ieee"
    OXFORD = "oxford"
    CAMBRIDGE = "cambridge"
    SAGE = "sage"
    OTHER = "other"


class AcquisitionStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    OA_CANDIDATE = "OA_CANDIDATE"
    INSTITUTIONAL_QUEUED = "INSTITUTIONAL_QUEUED"
    DOWNLOADED_UNVERIFIED = "DOWNLOADED_UNVERIFIED"
    VERIFIED = "VERIFIED"
    REVIEW_NEEDED = "REVIEW_NEEDED"
    FAILED = "FAILED"
    OA_RESOLVED = "OA_RESOLVED"
    PROGRAM_DOWNLOAD_PENDING = "PROGRAM_DOWNLOAD_PENDING"
    PROGRAM_DOWNLOADED_UNVERIFIED = "PROGRAM_DOWNLOADED_UNVERIFIED"
    INSTITUTIONAL_REQUIRED = "INSTITUTIONAL_REQUIRED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"


class Paper(BaseModel):
    paper_id: str = Field(default_factory=lambda: uuid4().hex)
    doi: str | None = None
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    publisher_raw: str | None = None
    publisher_normalized: Publisher = Publisher.OTHER
    abstract: str | None = None
    document_type: str | None = None
    publication_date: str | None = None
    language: str | None = None
    external_ids: dict[str, list[str]] = Field(default_factory=dict)
    source_hits: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    oa_metadata: list[dict[str, Any]] = Field(default_factory=list)
    landing_urls: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    possible_duplicates: list[str] = Field(default_factory=list)
    status: AcquisitionStatus = AcquisitionStatus.DISCOVERED


class RawRecord(BaseModel):
    record_id: str
    provider: str
    query_id: str
    raw_path: str
    ordinal: int
    payload: dict[str, Any]


class Page(BaseModel):
    records: list[dict[str, Any]]
    total: int | None = None
    next_cursor: str | int | None = None
    warnings: list[str] = Field(default_factory=list)


class ProviderResult(BaseModel):
    provider: str
    status: str
    records: int = 0
    raw_returned_records: int = 0
    normalized_records: int = 0
    normalization_failures: int = 0
    access_status: str = "PROVIDER_ERROR"
    authentication: str = "anonymous"
    http_status: int | None = None
    network_requests: int = 0
    cache_hits: int = 0
    rate_limit: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
