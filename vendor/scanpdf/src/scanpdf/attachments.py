import re, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from bs4 import BeautifulSoup
from .verification import supplementary_type

MARKER=re.compile(r"suppl|supp_|supporting|attachment|mmc\d|moesm|article-supplement|downloadsupplement|mmedia|[-_]sup[-_]\d|/cms/",re.I)

def discover(html,base,max_files=20):
    soup=BeautifulSoup(html or "", "html.parser");out=[]
    for a in soup.find_all("a",href=True):
        h=a["href"];label=a.get_text(" ",strip=True)
        if h.startswith(("#","javascript:","mailto:")):continue
        if "/journal/" in h and re.search(r"/vol/|/issue/",h):continue
        if not MARKER.search(h) and not (MARKER.search(label) and re.search(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|csv|txt)(?:[?#]|$)",h,re.I)):continue
        from urllib.parse import urlsplit
        if re.search(r"\.(?:jpe?g|png|gif|webp|svg|tiff?|eps|bmp|ico)(?:[?#]|$)",h,re.I):continue
        if re.search(r"-fig-\d",h,re.I):continue
        u=urljoin(base,h)
        if urldefrag(u)[0]==urldefrag(base)[0]:continue
        if u.startswith(("http://","https://")) and u not in out:out.append(u)
    # Elsevier embeds downloadable SI URLs in the article JSON as well as anchors.
    for m in re.finditer(r"https?://ars\.els-cdn\.com/content/image/[^\s\"'<>]+?mmc\d+\.[a-zA-Z0-9]+", html or ""):
        if m.group() not in out:out.append(m.group())
    return out[:max_files]

def save_attachment(out: Path, number, body, content_type, url):
    ext=supplementary_type(body,content_type,url)
    if not ext:return None
    out.mkdir(parents=True,exist_ok=True)
    sha=hashlib.sha256(body).hexdigest()
    path=out/f"SI{number}_{sha[:10]}{ext}"
    tmp=path.with_suffix(ext+".part");tmp.write_bytes(body);tmp.replace(path)
    return {"path":str(path.resolve()),"sha256":sha,"size":len(body),"source_url":url,"content_fingerprint":content_fingerprint(body,ext)}


def content_fingerprint(body,ext):
    if ext!=".pdf":return hashlib.sha256(body).hexdigest()
    try:
        import pymupdf,json
        h=hashlib.sha256()
        with pymupdf.open(stream=body,filetype="pdf") as doc:
            if len(doc)>100:return hashlib.sha256(body).hexdigest()
            for page in doc:
                h.update(page.get_text().encode())
                h.update(json.dumps(page.get_drawings(),default=str,sort_keys=True).encode())
                for img in page.get_images(full=True):h.update(doc.extract_image(img[0])["image"])
        return h.hexdigest()
    except Exception:return hashlib.sha256(body).hexdigest()

def dedupe_files(files):
    unique=[];seen={}
    for f in files:
        key=f.get("content_fingerprint") or f["sha256"]
        if key in seen:
            prior=seen[key]
            prior.setdefault("alternate_sources",[]).append(f["source_url"])
            if f["path"]!=prior["path"]:Path(f["path"]).unlink(missing_ok=True)
        else:seen[key]=f;unique.append(f)
    return unique
