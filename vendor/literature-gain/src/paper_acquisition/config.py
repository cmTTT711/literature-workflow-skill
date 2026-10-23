import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("workspace/catalog")
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    attempts: int = Field(default=3, ge=1, le=8)
    max_pages: int = Field(default=1000, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    cache_ttl_seconds: int = Field(default=86400, ge=0)
    force_refresh: bool = False
    offline: bool = False
    arxiv_interval: float = Field(default=3.2, ge=3.0)
    coordination_dir: Path = Path("workspace/coordination")
    providers: list[str] = Field(
        default_factory=lambda: [
            "openalex",
            "scopus",
            "wos",
            "semantic_scholar",
            "lens",
        ]
    )

    @classmethod
    def load(cls, path: Path | None = None):
        return cls(**(tomllib.loads(path.read_text(encoding="utf-8")) if path else {}))


def credential(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


ENV_NAMES = frozenset(
    {
        "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "SCOPUS_API_KEY",
        "SCOPUS_INSTTOKEN",
        "WOS_API_KEY",
        "LENS_API_KEY",
        "CORE_API_KEY",
        "OPENAIRE_TOKEN",
        "PAPER_CONTACT_EMAIL",
        "UNPAYWALL_EMAIL",
    }
)


def load_env(path: Path = Path(".env")) -> dict[str, bool]:
    """Load only supported names; never interpolate, log or return values. Shell env wins."""
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$", line)
            if not match or match[1] not in ENV_NAMES:
                continue
            value = match[2].strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                end = value.find(quote, 1)
                if end == -1:
                    raise ValueError("Invalid quoted value in local environment file")
                value = value[1:end]
            else:
                value = value.split(" #", 1)[0].strip()
            os.environ.setdefault(match[1], value)
    return {name: bool(credential(name)) for name in sorted(ENV_NAMES)}
