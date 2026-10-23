from ..models import Page, Paper
from ..normalization import ids
from .base import DiscoveryProvider, decode, keyword_terms, phrase


class Adapter(DiscoveryProvider):
    name, key_env = "semantic_scholar", "SEMANTIC_SCHOLAR_API_KEY"
    server_document_types = frozenset({"journal article"})

    def translate(self, profile, query):
        text = " + ".join(phrase(q) for q in [*keyword_terms(query), *profile.include_terms])
        text += "".join(" + -" + phrase(q) for q in profile.exclude_terms)
        params = {
            "query": text,
            "fields": "title,authors,year,venue,publicationDate,publicationTypes,"
            "externalIds,abstract,openAccessPdf,url,journal",
        }
        if profile.year_from or profile.year_to:
            params["year"] = f"{profile.year_from or ''}-{profile.year_to or ''}"
        if any(value.casefold() == "journal article" for value in profile.document_types):
            params["publicationTypes"] = "JournalArticle"
        return params

    async def fetch(self, translated, cursor, size):
        params = dict(translated)
        if cursor:
            params["token"] = cursor
        data = decode(
            await self.transport.request(
                "GET",
                "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                params=params,
                headers={"x-api-key": self.key} if self.key else {},
            )
        )
        # Bulk endpoint chooses batch size (up to 1000); preserve the entire returned batch on disk.
        return Page(records=data["data"], total=data.get("total"), next_cursor=data.get("token"))

    def normalize(self, r):
        external = r.get("externalIds") or {}
        return Paper(
            doi=external.get("DOI"),
            title=r.get("title") or "",
            authors=[a["name"] for a in r.get("authors", [])],
            year=r.get("year"),
            journal=(r.get("journal") or {}).get("name") or r.get("venue"),
            abstract=r.get("abstract"),
            publication_date=r.get("publicationDate"),
            document_type=", ".join(r.get("publicationTypes") or []) or None,
            external_ids=ids(
                semantic_scholar=r.get("paperId"),
                pmid=external.get("PubMed"),
                arxiv=external.get("ArXiv"),
                pmcid=external.get("PubMedCentral"),
            ),
            oa_metadata=[{"provider": self.name, "openAccessPdf": r.get("openAccessPdf")}],
            landing_urls=[r["url"]] if r.get("url") else [],
        )
