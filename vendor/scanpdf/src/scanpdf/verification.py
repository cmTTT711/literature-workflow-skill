from __future__ import annotations
import re, hashlib, unicodedata, zipfile, io, gzip
from dataclasses import dataclass, asdict

@dataclass
class Verdict:
    status: str
    reason: str
    sha256: str = ""
    size: int = 0
    pages: int = 0
    title_score: float = 0
    def as_dict(self): return asdict(self)

def words(text):
    t=unicodedata.normalize("NFKD",text).casefold()
    t="".join(c for c in t if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", t)

def title_score(title, text):
    # Scientific titles differ in subscript spacing (NbOCl3 / NbOCl 3) and XML boundaries.
    # Normalize letter/number boundaries on both sides; keep the same shingle threshold.
    def tokens(value):return [part for word in words(value) for part in re.findall(r"[a-z]+|[0-9]+",word)]
    a,b=tokens(title),tokens(text)
    if not a or not b:return 0.0
    n=min(3,len(a));expected={tuple(a[i:i+n]) for i in range(len(a)-n+1)}
    found={tuple(b[i:i+n]) for i in range(len(b)-n+1)}
    return len(expected & found)/len(expected)

def verify_pdf(body: bytes, doi: str, title: str="") -> Verdict:
    h=hashlib.sha256(body).hexdigest();v=Verdict("invalid","not_pdf",h,len(body))
    if not body.startswith(b"%PDF-"):return v
    try:
        import pymupdf
        with pymupdf.open(stream=body,filetype="pdf") as doc:
            v.pages=len(doc)
            if not v.pages: return v
            first=doc[0].get_text();front=first[:5000]
            text="\n".join(doc[i].get_text() for i in range(min(3,len(doc))))[:25000]
    except Exception:
        v.reason="unreadable_pdf";return v
    # SI and correspondence are not main articles, even when they contain the DOI.
    start=" ".join(words(first[:450]))
    if re.match(r"^(?:s\d+\s+)?(?:supporting information|supplementary information|electronic supplementary|supplemental material)\b",start):
        v.reason="supplement_in_main_slot";return v
    v.title_score=title_score(title,text[:15000]) if title else 0
    normalized=unicodedata.normalize("NFKC",front).lower().replace("\u00ad","")
    for dash in ["\u2010","\u2011","\u2013","\u2212"]:normalized=normalized.replace(dash,"-")
    target=doi.lower()
    pattern=r"(?<![a-z0-9])"+r"\s*".join(re.escape(c) for c in target)+r"(?![a-z0-9])"
    matched=re.search(pattern,normalized) is not None
    # Target DOI must appear on page 1, not just among references in later pages.
    if matched:
        v.status="verified";v.reason="first_page_doi";return v
    others=re.findall(r"10\.\d{4,9}/[^\s<>]+",front.lower())
    if others:
        v.status="needs_review";v.reason="conflicting_first_page_doi";return v
    if title and len(words(title))>=4 and v.title_score>=0.85:
        v.status="verified";v.reason="title_shingles";return v
    v.status="needs_review";v.reason="identity_not_proven";return v

def supplementary_type(body: bytes, content_type: str="", url: str="") -> str | None:
    ct=content_type.lower().split(";")[0].strip()
    head=body[:1500].lstrip().lower()
    if not body or ct.startswith("image/") or b"<html" in head or b"<!doctype html" in head or b"challenge-platform" in head:return None
    if body.startswith(b"%PDF-"):
        try:
            import pymupdf
            with pymupdf.open(stream=body,filetype="pdf") as doc:
                return ".pdf" if len(doc) else None
        except Exception:return None
    if body.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                names=z.namelist()
                if not names or z.testzip() is not None:return None
                if any(n.startswith("word/") for n in names):return ".docx"
                if any(n.startswith("xl/") for n in names):return ".xlsx"
                if any(n.startswith("ppt/") for n in names):return ".pptx"
                return ".zip"
        except Exception:return None
    if body.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:f.read(1024)
            return ".gz"
        except Exception:return None
    if body.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        from urllib.parse import urlsplit
        ext=urlsplit(url).path.lower().rsplit(".",1)[-1]
        return "."+ext if ext in {"doc","xls","ppt","opj"} else ".ole"
    if ct in {"text/plain","text/csv"}:
        try:
            s=body.decode("utf-8-sig")
            if "\x00" not in s and not any(x in s.lower() for x in ["<script", "just a moment", "access denied"]):return ".csv" if ct=="text/csv" else ".txt"
        except UnicodeDecodeError:pass
    return None
