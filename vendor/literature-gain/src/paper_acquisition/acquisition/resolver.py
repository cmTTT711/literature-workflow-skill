"""Evidence-driven OA policy. A PDF-looking URL by itself never grants access."""

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .models import PRIORITY, Candidate

POLICY_VERSION = "oa-policy-1"


def open_license(value):
    value = str(value or "").lower().strip().rstrip("/")
    return (
        bool(re.fullmatch(r"cc-(?:by(?:-nc)?(?:-nd|-sa)?|0)(?:-[\d.]+)?", value))
        or value in {"public-domain", "public domain", "cc0"}
        or bool(
            re.match(
                r"^https?://creativecommons.org/(?:licenses/(?:by(?:-nc)?(?:-nd|-sa)?)/|publicdomain/)", value
            )
        )
    )


def arxiv_pdf(url):
    parts = urlsplit(str(url))
    if parts.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return None
    match = re.fullmatch(r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?", parts.path)
    return f"https://arxiv.org/pdf/{match[1]}" if match else None


def candidate(
    source,
    url,
    *,
    license=None,
    version=None,
    oa=False,
    repository=False,
    publisher=False,
    direct=False,
    evidence=None,
):
    parts = urlsplit(str(url or ""))
    safe = parts.scheme in {"http", "https"} and bool(parts.hostname) and not parts.username
    ax = arxiv_pdf(url) if safe else None
    licensed = open_license(license)
    reason = "URL_WITHOUT_SUFFICIENT_OPEN_ACCESS_EVIDENCE"
    eligible = False
    if safe and ax:
        url, direct, source, eligible = ax, True, "arxiv", True
        reason = "ARXIV_PUBLIC_DISTRIBUTION; robots and rate limits checked before download"
    elif safe and oa and licensed:
        eligible = True
        reason = "EXPLICIT_OA_AND_OPEN_LICENSE"
        if publisher:
            source = "publisher_oa"
    elif safe and oa and repository and source in {"openalex", "unpaywall", "openaire", "core"}:
        eligible = True
        reason = "TRUSTED_INDEX_EXPLICIT_OPEN_REPOSITORY_COPY; license unavailable"
    if not safe:
        reason = "UNSAFE_OR_MISSING_URL"
    return Candidate(
        source_type=source,
        source_url=str(url),
        license=license,
        version=version,
        resolver_provenance=[{"policy": POLICY_VERSION, **(evidence or {})}],
        confidence=0.98 if eligible and licensed else 0.90 if eligible else 0.2,
        reason=reason,
        eligible=eligible,
        direct_pdf=direct,
        priority=PRIORITY.get(source, 80),
    )


def locations(source, data, evidence):
    result = []
    entries = [data.get("best_oa_location"), *(data.get("locations") or data.get("oa_locations") or [])]
    for loc in entries:
        if not isinstance(loc, dict):
            continue
        host_type = loc.get("host_type") or (loc.get("source") or {}).get("type")
        url = loc.get("pdf_url") or loc.get("url_for_pdf") or loc.get("landing_page_url") or loc.get("url")
        if not url:
            continue
        result.append(
            candidate(
                source,
                url,
                license=loc.get("license"),
                version=loc.get("version"),
                oa=loc.get("is_oa") is True or (source == "unpaywall" and data.get("is_oa") is True),
                repository=host_type == "repository",
                publisher=host_type in {"publisher", "journal"},
                direct=bool(loc.get("pdf_url") or loc.get("url_for_pdf")),
                evidence={**evidence, "location": loc},
            )
        )
    return result


def resolve_metadata(paper, extra=()):
    result = []
    entries = [
        (m.get("provider"), m, {"paper_id": paper.paper_id, "source_records": paper.provenance})
        for m in paper.oa_metadata
    ]
    entries.extend(extra)
    for source, data, provenance in entries:
        evidence = {"resolver": source, **provenance}
        if source == "openalex":
            from .openalex_locations import work_candidates

            candidates, _ = work_candidates(paper, data, evidence, normal_requests=False)
            result.extend(candidates)
        elif source == "unpaywall":
            result.extend(locations(source, data, evidence))
        elif source == "openaire":
            for instance in data.get("instances") or []:
                access = instance.get("accessRight") or {}
                if isinstance(access, str):
                    access = {"code": access}
                opened = (
                    access.get("code") in {"OPEN", "OPEN_ACCESS", "c_abf2"} or access.get("label") == "OPEN"
                )
                for url in instance.get("urls") or []:
                    result.append(
                        candidate(
                            source,
                            url,
                            license=instance.get("license"),
                            version=instance.get("version"),
                            oa=opened,
                            repository=str(access.get("openAccessRoute")).lower() == "green",
                            direct=urlsplit(url).path.lower().endswith(".pdf"),
                            evidence={**evidence, "instance": instance},
                        )
                    )
        elif source == "semantic_scholar":
            pdf = data.get("openAccessPdf") or {}
            if pdf.get("url"):
                result.append(
                    candidate(
                        source,
                        pdf["url"],
                        license=pdf.get("license"),
                        oa=bool(pdf.get("status") in {"GOLD", "GREEN", "HYBRID"}),
                        direct=True,
                        evidence={**evidence, "pdf": pdf},
                    )
                )
        elif source == "core":
            if data.get("downloadUrl"):
                result.append(
                    candidate(
                        source,
                        data["downloadUrl"],
                        license=data.get("license"),
                        version=data.get("version"),
                        oa=data.get("isOpenAccess") is True,
                        repository=True,
                        direct=True,
                        evidence=evidence,
                    )
                )
        elif source == "crossref":
            # Crossref text-mining links alone carry no OA permission.
            for link in data.get("link") or []:
                if link.get("URL"):
                    licenses = [
                        l.get("URL")
                        for l in data.get("license") or []
                        if l.get("content-version") == link.get("content-version")
                        and l.get("content-version") in {"vor", "am"}
                        and (l.get("start") or {}).get("timestamp", float("inf"))
                        <= datetime.now(UTC).timestamp() * 1000
                    ]
                    lic = next((l for l in licenses if open_license(l)), None)
                    result.append(
                        candidate(
                            "publisher_oa",
                            link["URL"],
                            license=lic,
                            oa=bool(lic),
                            publisher=True,
                            direct=link.get("content-type") == "application/pdf",
                            version=link.get("content-version"),
                            evidence=evidence,
                        )
                    )
    for ax in paper.external_ids.get("arxiv", []):
        url = f"https://arxiv.org/abs/{ax}"
        if arxiv_pdf(url):
            result.append(
                candidate(
                    "arxiv",
                    url,
                    version="submittedVersion",
                    evidence={"resolver": "arxiv_identifier", "source_records": paper.provenance},
                )
            )
    for url in paper.landing_urls:
        if arxiv_pdf(url):
            result.append(
                candidate(
                    "arxiv",
                    url,
                    evidence={"resolver": "declared_arxiv_url", "source_records": paper.provenance},
                )
            )
    unique = {}
    for item in sorted(result, key=lambda c: (not c.eligible, c.priority, not c.direct_pdf)):
        key = (item.source_type, item.source_url)
        if key in unique:
            unique[key].resolver_provenance.extend(item.resolver_provenance)
            continue
        item.candidate_id = hashlib.sha256(f"{POLICY_VERSION}|{paper.paper_id}|{key}".encode()).hexdigest()
        unique[key] = item
    return sorted(unique.values(), key=lambda c: (c.priority, not c.direct_pdf, c.source_url))
