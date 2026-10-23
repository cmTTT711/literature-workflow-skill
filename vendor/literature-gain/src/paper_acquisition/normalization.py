import re
import unicodedata
from html import unescape
from urllib.parse import unquote, urlsplit

from .models import Publisher


def original_doi(value):
    if not value:
        return None
    value = unquote(str(value).strip())
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.IGNORECASE).strip()
    key = value.casefold()
    return (
        value
        if re.fullmatch(r"10\.\d{4,9}/\S+", value, re.IGNORECASE) and not key.startswith("10.13039/")
        else None
    )


def normalize_doi(value):
    value = original_doi(value)
    return value.casefold() if value else None


def clean(value):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def title_key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", clean(value)).casefold()))


def year(value):
    found = re.search(r"\b(1\d{3}|2\d{3})\b", str(value or ""))
    return int(found[0]) if found else None


def ids(**values):
    return {key: [str(value)] for key, value in values.items() if value}


def safe_urls(values):
    return list(dict.fromkeys(str(v) for v in values if v and urlsplit(str(v)).scheme in {"http", "https"}))


def publisher(value):
    key = title_key(value)
    aliases = {
        Publisher.ELSEVIER: ["elsevier", "academic press"],
        Publisher.APS: ["american physical society"],
        Publisher.AIP: ["american institute of physics", "aip publishing"],
        Publisher.SPRINGER_NATURE: ["springer", "nature publishing", "nature portfolio", "macmillan"],
        Publisher.WILEY: ["wiley"],
        Publisher.ACS: ["american chemical society"],
        Publisher.IOP: ["iop publishing", "institute of physics"],
        Publisher.RSC: ["royal society of chemistry"],
        Publisher.TAYLOR_FRANCIS: ["taylor francis", "taylor and francis", "routledge"],
        Publisher.IEEE: ["ieee", "institute of electrical and electronics engineers"],
        Publisher.OXFORD: ["oxford university press"],
        Publisher.CAMBRIDGE: ["cambridge university press"],
        Publisher.SAGE: ["sage"],
    }
    for canonical, patterns in aliases.items():
        if any(re.search(r"\b" + re.escape(pattern) + r"\b", key) for pattern in patterns):
            return canonical
    return Publisher.OTHER


def finalize(paper):
    paper.doi = original_doi(paper.doi)
    paper.title = clean(paper.title)
    paper.authors = [clean(a) for a in paper.authors if clean(a)]
    paper.publisher_normalized = publisher(paper.publisher_raw)
    paper.landing_urls = safe_urls(
        paper.landing_urls + ([f"https://doi.org/{paper.doi}"] if paper.doi else [])
    )
    for key, values in paper.external_ids.items():
        if key == "arxiv":
            paper.external_ids[key] = [re.sub(r"v\d+$", "", v.rsplit("/abs/", 1)[-1]) for v in values]
        if key in {"pmid", "pmcid"}:
            paper.external_ids[key] = [v.rstrip("/").rsplit("/", 1)[-1] for v in values]
    return paper
