from ..models import Page, Paper
from ..normalization import ids
from .base import DiscoveryProvider, boolean_query, decode


class Adapter(DiscoveryProvider):
    name, key_env, first_cursor = "openalex", "OPENALEX_API_KEY", "*"
    server_document_types = frozenset({"journal article"})
    server_languages = None

    def translate(self, profile, query):
        params = {"search": boolean_query(query, profile)}
        filters = []
        if profile.year_from:
            filters.append(f"from_publication_date:{profile.year_from}-01-01")
        if profile.year_to:
            filters.append(f"to_publication_date:{profile.year_to}-12-31")
        if profile.language:
            filters.append(f"language:{profile.language}")
        if any(value.casefold() == "journal article" for value in profile.document_types):
            filters.append("type:article")
        if filters:
            params["filter"] = ",".join(filters)
        return params

    async def fetch(self, translated, cursor, size):
        response = await self.transport.request(
            "GET",
            "https://api.openalex.org/works",
            params={**translated, "cursor": cursor, "per_page": size},
            headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
        )
        data = decode(response)
        return Page(
            records=data["results"], total=data["meta"]["count"], next_cursor=data["meta"].get("next_cursor")
        )

    def normalize(self, r):
        location = r.get("primary_location") or {}
        source = location.get("source") or {}
        words = sorted(
            (position, word)
            for word, positions in (r.get("abstract_inverted_index") or {}).items()
            for position in positions
        )
        external = r.get("ids") or {}
        return Paper(
            doi=r.get("doi"),
            title=r.get("title") or "",
            authors=[a["author"]["display_name"] for a in r.get("authorships", []) if a.get("author")],
            year=r.get("publication_year"),
            journal=source.get("display_name"),
            publisher_raw=source.get("host_organization_name"),
            abstract=" ".join(word for _, word in words) or None,
            publication_date=r.get("publication_date"),
            document_type=r.get("type"),
            language=r.get("language"),
            external_ids=ids(openalex=r.get("id"), pmid=external.get("pmid")),
            oa_metadata=[
                {
                    "provider": self.name,
                    "open_access": r.get("open_access"),
                    "locations": r.get("locations") or [],
                }
            ],
            landing_urls=[location["landing_page_url"]] if location.get("landing_page_url") else [],
        )
