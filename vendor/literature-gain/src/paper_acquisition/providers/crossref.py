from ..config import credential
from ..models import Page, Paper
from ..normalization import year
from .base import DiscoveryProvider, decode


class Adapter(DiscoveryProvider):
    name, first_cursor = "crossref", "*"
    allow_repeated_cursor = True

    def warnings(self, profile):
        if profile.strict_capabilities and profile.exclude_terms:
            from ..errors import QueryError

            raise QueryError("CROSSREF_BOOLEAN_EXCLUSION_UNSUPPORTED")
        return super().warnings(profile) + [
            "Crossref query is relevance-ranked; excludes retained for screening"
        ]

    def translate(self, profile, query):
        params = {"query.bibliographic": " ".join([query, *profile.include_terms])}
        filters = []
        if profile.year_from:
            filters.append(f"from-pub-date:{profile.year_from}-01-01")
        if profile.year_to:
            filters.append(f"until-pub-date:{profile.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        return params

    async def fetch(self, translated, cursor, size):
        params = {**translated, "rows": size, "cursor": cursor}
        if email := credential("PAPER_CONTACT_EMAIL"):
            params["mailto"] = email
        data = decode(await self.transport.request("GET", "https://api.crossref.org/works", params=params))[
            "message"
        ]
        return Page(
            records=data["items"],
            total=data["total-results"],
            next_cursor=data.get("next-cursor") if len(data["items"]) >= size else None,
        )

    def normalize(self, r):
        date = (r.get("published") or r.get("issued") or {}).get("date-parts", [[]])[0]
        date_text = "-".join(str(v).zfill(2) for v in date) or None
        return Paper(
            doi=r.get("DOI"),
            title=(r.get("title") or [""])[0],
            authors=[
                a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))
                for a in r.get("author", [])
            ],
            year=year(date_text),
            publication_date=date_text,
            journal=(r.get("container-title") or [None])[0],
            publisher_raw=r.get("publisher"),
            abstract=r.get("abstract"),
            document_type=r.get("type"),
            language=r.get("language"),
            oa_metadata=[
                {"provider": self.name, "licenses": r.get("license", []), "links": r.get("link", [])}
            ],
            landing_urls=[r["URL"]] if r.get("URL") else [],
        )
