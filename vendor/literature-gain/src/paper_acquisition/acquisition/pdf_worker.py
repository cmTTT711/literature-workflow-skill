"""Isolated parser worker. Input on stdin, only a compact JSON result on stdout."""

import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from ..normalization import normalize_doi, title_key
from .models import PDFResult


def title_similarity(title, text):
    expected, actual = title_key(title), title_key(text)
    if not expected:
        return 0.0
    if expected in actual:
        return 1.0
    words, tokens = expected.split(), actual.split()[:1200]
    if len(words) < 4:
        return 0.0
    width = len(words)
    # Contiguous windows avoid a DOI/title mentioned far apart in a bibliography.
    return max(
        (
            SequenceMatcher(None, expected, " ".join(tokens[i : i + width])).ratio()
            for i in range(max(0, len(tokens) - width + 1))
        ),
        default=0.0,
    )


def inspect(path, paper, minimum=512, maximum=50 * 1024 * 1024):
    from pypdf import PdfReader

    result = PDFResult(exists=path.is_file())
    if not result.exists:
        result.reasons.append("FILE_MISSING")
        return result
    result.size_bytes = path.stat().st_size
    if not minimum <= result.size_bytes <= maximum:
        result.reasons.append("FILE_SIZE_OUT_OF_RANGE")
        return result
    data = path.read_bytes()
    result.sha256 = hashlib.sha256(data).hexdigest()
    result.pdf_magic = data.startswith(b"%PDF-")
    if not result.pdf_magic:
        result.reasons.append(
            "HTML_OR_NON_PDF"
            if any(t in data[:4096].lower() for t in (b"<html", b"<!doctype", b"access denied", b"login"))
            else "PDF_MAGIC_MISSING"
        )
        return result
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            result.reasons.append("ENCRYPTED_PDF")
            return result
        result.page_count = len(reader.pages)
        result.parseable = True
        if not 0 < result.page_count <= 2000:
            result.reasons.append("INVALID_PAGE_COUNT")
            return result
        # Inspect bounded opening pages, including publisher covers and attached errata.
        pages = []
        labeled = set()
        for page in reader.pages[: min(5, result.page_count)]:
            try:
                text = (page.extract_text() or "")[:12000]
            except Exception:  # noqa: BLE001 -- text extraction is auxiliary, not file identity
                text = ""
            text = re.split(r"\n\s*(?:references|bibliography)\s*\n", text, flags=re.IGNORECASE)[0]
            pages.append(text)
            # Only explicitly labeled DOI lines are conflict evidence, not arbitrary citations.
            for line in text.splitlines():
                match = re.search(
                    r"(?:\bdoi\s*:\s*|^\s*https?://(?:dx\.)?doi\.org/)(10\..+)", line, re.IGNORECASE
                )
                if match:
                    token = re.sub(r"\s*([/.-])\s*", r"\1", match[1]).split()[0]
                    if value := normalize_doi(token.rstrip(".,;")):
                        labeled.add(value)
        front = "\n".join(pages)
        result.title_score = title_similarity(paper.get("title", ""), front)
        result.title_match = result.title_score >= 0.90
        doi = normalize_doi(paper.get("doi"))
        found = {normalize_doi(s.rstrip(".,;)]}")) for s in re.findall(r"10\.\d{4,9}/[^\s<>\"]+", front)}
        # Match the known DOI across extraction whitespace without inventing identifiers.
        doi_pattern = r"\s*".join(re.escape(char) for char in doi) if doi else ""
        result.doi_match = (
            (doi in found or doi in labeled or bool(re.search(doi_pattern + r"(?![a-z0-9])", front, re.IGNORECASE)))
            if doi
            else None
        )
        if doi and labeled and not result.doi_match:
            result.reasons.append("FRONT_MATTER_DOI_CONFLICT")
        if not result.reasons:
            result.status = "VERIFIED"
            if not result.doi_match:
                result.reasons.append("DOI_NOT_OBSERVED; no explicit conflicting article DOI")
    except Exception as error:  # noqa: BLE001 -- parser failures stay structured, worker is time bounded
        result.reasons.append("PDF_PARSE_ERROR:" + type(error).__name__)
    return result


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    print(
        inspect(
            Path(payload["path"]), payload["paper"], payload["minimum"], payload["maximum"]
        ).model_dump_json()
    )
