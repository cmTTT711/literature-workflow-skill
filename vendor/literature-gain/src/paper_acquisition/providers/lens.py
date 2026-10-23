from ..models import Page, Paper
from ..normalization import ids
from .base import DiscoveryProvider, decode, keyword_terms


class Adapter(DiscoveryProvider):
    name, key_env, key_required = "lens", "LENS_API_KEY", True
    cache_ttl = 40

    def translate(self, profile, query):
        def match(term):
            return {
                "bool": {
                    "should": [{"match_phrase": {"title": term}}, {"match_phrase": {"abstract": term}}],
                    "minimum_should_match": 1,
                }
            }

        query_body = {
            "must": [match(q) for q in [*keyword_terms(query), *profile.include_terms]],
            "must_not": [match(q) for q in profile.exclude_terms],
        }
        years = {}
        if profile.year_from:
            years["gte"] = profile.year_from
        if profile.year_to:
            years["lte"] = profile.year_to
        if years:
            query_body["filter"] = [{"range": {"year_published": years}}]
        return {"query": {"bool": query_body}}

    async def fetch(self, translated, cursor, size):
        body = (
            {"scroll_id": cursor, "scroll": "1m"} if cursor else {**translated, "size": size, "scroll": "1m"}
        )
        response = await self.transport.request(
            "POST",
            "https://api.lens.org/scholarly/search",
            body=body,
            headers={"Authorization": f"Bearer {self.key}"},
        )
        if response.status_code == 204:
            return Page(records=[])
        data = decode(response)
        return Page(records=data["data"], total=data.get("total"), next_cursor=data.get("scroll_id"))

    def normalize(self, r):
        external = {i["type"]: i["value"] for i in r.get("external_ids", [])}
        return Paper(
            doi=external.get("doi"),
            title=r.get("title") or "",
            authors=[
                " ".join(filter(None, [a.get("first_name"), a.get("last_name")]))
                for a in r.get("authors", [])
            ],
            year=r.get("year_published"),
            journal=(r.get("source") or {}).get("title"),
            publisher_raw=(r.get("source") or {}).get("publisher") or r.get("publisher"),
            abstract=r.get("abstract"),
            publication_date=r.get("date_published"),
            document_type=r.get("publication_type"),
            external_ids=ids(lens=r.get("lens_id"), pmid=external.get("pmid"), arxiv=external.get("arxiv")),
            oa_metadata=[{"provider": self.name, "open_access": r.get("open_access")}],
            language=(r.get("languages") or [None])[0],
            landing_urls=[
                u["url"] for u in (r.get("source_urls") or []) if u.get("url") and u.get("type") == "html"
            ],
        )
