from dataclasses import dataclass
from ._scansci.publisher_profiles import infer_publisher_profile, get_publisher_profile

@dataclass(frozen=True)
class Route:
    publisher: str
    backend: str
    article: str
    profile: object | None = None

BACKENDS={"rsc":"camoufox", "pnas":"camoufox", "aiaa":"camoufox", "jpsj":"camoufox", "elsevier":"cloakbrowser"}
PREFIX={"10.7566":"jpsj", "10.35848":"jpsj", "10.1073":"pnas", "10.2514":"aiaa", "10.1146":"annual_reviews", "10.1177":"sage", "10.23919":"ieee", "10.1134":"springer"}

def route_for(doi, config=None):
    profile = infer_publisher_profile(doi)
    pub = (profile.name.lower().replace(" ", "_") if profile else PREFIX.get(doi.split("/")[0], "other"))
    aliases={"aip_publishing":"aip", "oxford_academic":"oxford", "world_scientific":"world_scientific", "annual-reviews":"annual_reviews", "oxfordacademic":"oxford", "springer_nature":"springer", "springer/nature":"springer", "annual_reviews":"annual_reviews", "taylor_&_francis":"tandfonline", "oxford_university_press":"oxford"}
    pub=aliases.get(pub,pub)
    backend=(config.backend_overrides.get(pub) if config else None) or BACKENDS.get(pub,"patchright")
    if backend not in {"patchright","camoufox","cloakbrowser"}: raise ValueError("未知浏览器后端")
    article=profile.article_url(doi) if profile else "https://doi.org/"+doi
    if doi.startswith("10.1038/"):article="https://www.nature.com/articles/"+doi.split("/",1)[1]
    elif doi.startswith("10.1007/"):article="https://link.springer.com/article/"+doi
    if pub=="jpsj": article="https://journals.jps.jp/doi/"+doi
    if pub=="pnas": article="https://www.pnas.org/doi/"+doi
    if pub=="aiaa": article="https://arc.aiaa.org/doi/"+doi
    if pub=="annual_reviews":article="https://www.annualreviews.org/doi/"+doi
    return Route(pub,backend,article,profile)


def institution_login_url(doi,config):
    if config.access=="institution":return config.institution_url
    if doi.startswith(("10.1038/","10.1007/")):
        target="https://www.nature.com" if doi.startswith("10.1038/") else "https://link.springer.com"
        return "https://wayf.springernature.com/?redirect_uri="+target
    return route_for(doi,config).article
