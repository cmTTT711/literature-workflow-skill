from defusedxml import ElementTree as ET

from ..errors import ResponseError
from ..models import Page, Paper
from ..normalization import ids, year
from .base import DiscoveryProvider, boolean_query

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
    "x": "http://arxiv.org/schemas/atom",
}


class Adapter(DiscoveryProvider):
    name, first_cursor, interval, result_ceiling = "arxiv", 0, 3.1, 30000
    cache_ttl = 86400

    def translate(self, profile, query):
        q = boolean_query(query, profile, field="all:")
        q = q.replace("AND NOT", "ANDNOT")
        if profile.year_from or profile.year_to:
            q += f" AND submittedDate:[{profile.year_from or 1991}01010000 TO {profile.year_to or 9999}12312359]"
        return {"search_query": q, "sortBy": "submittedDate", "sortOrder": "ascending"}

    async def fetch(self, translated, cursor, size):
        response = await self.transport.request(
            "GET",
            "https://export.arxiv.org/api/query",
            params={**translated, "start": cursor, "max_results": size},
        )
        try:
            root = ET.fromstring(response.content)
            records = []
            for entry in root.findall("a:entry", NS):

                def text(path, entry=entry):
                    return entry.findtext(path, namespaces=NS)

                if "api/errors" in (text("a:id") or ""):
                    raise ResponseError("ARXIV_ERROR_ENTRY")
                records.append(
                    {
                        "id": text("a:id"),
                        "title": text("a:title"),
                        "doi": text("x:doi"),
                        "abstract": text("a:summary"),
                        "published": text("a:published"),
                        "journal": text("x:journal_ref"),
                        "authors": [
                            a.findtext("a:name", namespaces=NS) for a in entry.findall("a:author", NS)
                        ],
                        "links": [dict(link.attrib) for link in entry.findall("a:link", NS)],
                    }
                )
            total = int(root.findtext("o:totalResults", namespaces=NS))
        except (ET.ParseError, TypeError, ValueError) as error:
            raise ResponseError("INVALID_ARXIV_FEED") from error
        next_start = int(cursor) + len(records)
        return Page(
            records=records, total=total, next_cursor=next_start if records and next_start < total else None
        )

    def normalize(self, r):
        return Paper(
            doi=r.get("doi"),
            title=r.get("title") or "",
            authors=r.get("authors", []),
            year=year(r.get("published")),
            publication_date=r.get("published"),
            journal=r.get("journal"),
            abstract=r.get("abstract"),
            document_type="preprint",
            external_ids=ids(arxiv=r.get("id")),
            oa_metadata=[{"provider": self.name, "links": r.get("links", [])}],
            landing_urls=[r["id"]] if r.get("id") else [],
        )
