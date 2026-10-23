import requests,json,hashlib
from urllib.parse import quote,urlsplit
from .config import atomic_json

def session(config=None):
    s=requests.Session();s.trust_env=False
    s.headers["User-Agent"]="ScanPDF/0.1 (institutional research)"
    if config and config.network_proxy:s.proxies.update({"http":config.network_proxy,"https":config.network_proxy})
    return s

def metadata(paper,config):
    from bs4 import BeautifulSoup
    from .publishers import route_for
    cache=config.home/"cache"/"metadata"/(hashlib.sha256(paper.doi.encode()).hexdigest()+".json")
    m=None
    try:
        if cache.exists():m=json.loads(cache.read_text(encoding="utf-8"))
        if not m or str(m.get("DOI","")).lower()!=paper.doi:
            with session(config) as s:
                r=s.get("https://api.crossref.org/works/"+quote(paper.doi,safe=""),params={"mailto":config.email},timeout=(6,12))
                r.raise_for_status();m=r.json()["message"]
                if str(m.get("DOI","")).lower()!=paper.doi:return paper
                atomic_json(cache,m)
        if paper.title:paper.title=BeautifulSoup(paper.title,"html.parser").get_text("",strip=True)
        if not paper.title:paper.title=BeautifulSoup(" ".join(m.get("title") or []),"html.parser").get_text("",strip=True)
        url=str(m.get("resource",{}).get("primary",{}).get("URL", ""))
        pub=route_for(paper.doi,config).publisher
        if not paper.article_url and pub in {"rsc","elsevier","aps","aip","iop","ieee","other"} and url.startswith(("http://","https://")) and "doi.org" not in urlsplit(url).netloc:
            if pub=="elsevier":
                from ._scansci.publisher_pdf_router import extract_elsevier_pii
                pii=extract_elsevier_pii(url)
                if pii:url="https://www.sciencedirect.com/science/article/pii/"+pii
            paper.article_url=url.replace("http://","https://",1)
    except Exception:pass
    return paper
