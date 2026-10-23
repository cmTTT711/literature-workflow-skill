from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AccessStatus(StrEnum):
    OA_RESOLVED = "OA_RESOLVED"
    PROGRAM_DOWNLOAD_PENDING = "PROGRAM_DOWNLOAD_PENDING"
    PROGRAM_DOWNLOADED_UNVERIFIED = "PROGRAM_DOWNLOADED_UNVERIFIED"
    VERIFIED = "VERIFIED"
    INSTITUTIONAL_REQUIRED = "INSTITUTIONAL_REQUIRED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"


PRIORITY = {
    "publisher_oa": 0,
    "openalex": 10,
    "unpaywall": 20,
    "core": 30,
    "openaire": 40,
    "arxiv": 50,
    "repositories": 60,
    "semantic_scholar": 70,
    "other": 80,
}


class Candidate(BaseModel):
    candidate_id: str = ""
    source_type: Literal[
        "publisher_oa",
        "openalex",
        "unpaywall",
        "core",
        "openaire",
        "arxiv",
        "repositories",
        "semantic_scholar",
        "other",
    ]
    source_url: str
    license: str | None = None
    version: str | None = None
    resolver_provenance: list[dict] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str
    eligible: bool = False
    direct_pdf: bool = False
    priority: int = 80


class PDFResult(BaseModel):
    status: str = "VERIFY_FAILED"
    exists: bool = False
    pdf_magic: bool = False
    parseable: bool = False
    size_bytes: int = 0
    page_count: int = 0
    sha256: str | None = None
    doi_match: bool | None = None
    title_match: bool = False
    title_score: float = 0
    reasons: list[str] = Field(default_factory=list)
    verifier_version: str = "2"


class AcquisitionOptions(BaseModel):
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    total_download_timeout: float = Field(default=90, gt=0, le=600)
    parse_timeout: float = Field(default=20, gt=0, le=120)
    attempts: int = Field(default=2, ge=1, le=3)
    interval: float = Field(default=2, ge=0)
    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    min_bytes: int = Field(default=512, ge=1)
    max_candidates: int = Field(default=3, ge=1, le=10)


class AcquisitionError(Exception):
    def __init__(self, code, *, retryable=False, details=None):
        self.code, self.retryable, self.details = code, retryable, details or {}
        super().__init__(code)

    def as_dict(self):
        return {"code": self.code, "retryable": self.retryable, "details": self.details}
