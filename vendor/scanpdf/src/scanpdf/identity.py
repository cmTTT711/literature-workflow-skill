from __future__ import annotations
import re, hashlib, csv, json
from pathlib import Path
from dataclasses import dataclass, field
from urllib.parse import unquote

def normalize_doi(value: str) -> str:
    s = unquote(str(value or "").strip())
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", s, flags=re.I)
    if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", s) or any(chr(c) in s for c in (60,62,92,34)):
        raise ValueError(f"无效 DOI：{value}")
    return s.lower()

def stem(doi: str) -> str:
    d = normalize_doi(doi)
    return re.sub(r"[^a-zA-Z0-9._-]", "_", d)[:100] + "_" + hashlib.sha256(d.encode()).hexdigest()[:10]

@dataclass
class Paper:
    doi: str
    title: str = ""
    publisher: str = ""
    target_path: str = ""
    article_url: str = ""
    attachment_urls: list[str] = field(default_factory=list)
    def __post_init__(self): self.doi = normalize_doi(self.doi)

def read_papers(path: Path) -> list[Paper]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f: rows = list(csv.DictReader(f))
        if rows and not any(k.lower() == "doi" for k in rows[0]): raise ValueError("CSV 缺少 doi 列")
        out=[]
        for row in rows:
            row={k.lower().strip():v for k,v in row.items() if k}
            if not row.get("doi", "").strip(): continue
            raw=row.get("attachment_urls", "").strip()
            urls=json.loads(raw) if raw else []
            if not isinstance(urls,list) or not all(isinstance(u,str) and u.startswith("https://") for u in urls):
                raise ValueError("attachment_urls 必须是 HTTPS URL 的 JSON 数组")
            out.append(Paper(row["doi"],row.get("title", ""),row.get("publisher", ""),row.get("target_path", ""),row.get("article_url", ""),urls))
    else:
        out=[Paper(l.strip()) for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    seen=set();unique=[]
    for paper in out:
        if paper.doi in seen: continue
        seen.add(paper.doi);unique.append(paper)
    return unique
