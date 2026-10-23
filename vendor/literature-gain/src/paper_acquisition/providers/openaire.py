from ..models import Page, Paper
from ..normalization import ids, normalize_doi, year
from .base import DiscoveryProvider, boolean_query, decode


class Adapter(DiscoveryProvider):
    name, key_env, first_cursor = "openaire", "OPENAIRE_TOKEN", "*"

    def translate(self, profile, query):
        params = {"search": boolean_query(query, profile), "type": "publication"}
        if profile.year_from:
            params["fromPublicationYear"] = profile.year_from
        if profile.year_to:
            params["toPublicationYear"] = profile.year_to
        return params

    async def fetch(self, translated, cursor, size):
        data = decode(
            await self.transport.request(
                "GET",
                "https://api.openaire.eu/graph/v3/research-products",
                params={**translated, "cursor": cursor, "pageSize": size},
                headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
            )
        )
        return Page(
            records=data["results"],
            total=data["header"]["numFound"],
            next_cursor=data["header"].get("nextCursor"),
        )

    def normalize(self, r):
        urls = [url for instance in (r.get("instances") or []) for url in (instance.get("urls") or [])]
        doi = next(
            (pid["value"] for pid in (r.get("pids") or []) if str(pid.get("scheme")).lower() == "doi"), None
        )
        doi_from_url = False
        if not doi:
            doi = next(
                (
                    normalize_doi(url)
                    for url in urls
                    if normalize_doi(url) and not normalize_doi(url).startswith("10.13039/")
                ),
                None,
            )
            doi_from_url = bool(doi)
        external = ids(openaire=r.get("id"))
        for pid in r.get("pids") or []:
            namespace = str(pid.get("scheme") or "").lower()
            if namespace and namespace != "doi" and pid.get("value"):
                external.setdefault(namespace, []).append(pid["value"])
        return Paper(
            doi=doi,
            title=r.get("mainTitle") or "",
            authors=[
                a.get("fullName") or " ".join(filter(None, [a.get("name"), a.get("surname")]))
                for a in (r.get("authors") or [])
            ],
            year=year(r.get("publicationDate")),
            journal=(r.get("container") or {}).get("name"),
            publisher_raw=r.get("publisher"),
            publication_date=r.get("publicationDate"),
            document_type=r.get("type"),
            abstract="\n".join(r.get("descriptions") or []) or None,
            external_ids=external,
            flags=["DOI_FROM_DECLARED_URL"] if doi_from_url else [],
            oa_metadata=[
                {
                    "provider": self.name,
                    "bestAccessRight": r.get("bestAccessRight"),
                    "instances": r.get("instances") or [],
                }
            ],
            language=(r.get("language") or {}).get("code"),
            landing_urls=urls,
        )
