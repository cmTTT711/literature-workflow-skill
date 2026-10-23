from ..models import Page, Paper
from ..normalization import ids, year
from .base import DiscoveryProvider, boolean_query, decode


class Adapter(DiscoveryProvider):
    name, key_env, key_required, first_cursor = "wos", "WOS_API_KEY", True, 1

    def translate(self, profile, query):
        q = "TS=(" + boolean_query(query, profile) + ")"
        if profile.year_from or profile.year_to:
            q += f" AND PY=({profile.year_from or 1000}-{profile.year_to or 9999})"
        return {"q": q, "db": "WOS"}

    async def fetch(self, translated, cursor, size):
        size = min(size, 50)
        data = decode(
            await self.transport.request(
                "GET",
                "https://api.clarivate.com/apis/wos-starter/v1/documents",
                params={**translated, "page": cursor, "limit": size},
                headers={"X-ApiKey": self.key},
            )
        )
        total = data["metadata"]["total"]
        return Page(
            records=data["hits"],
            total=total,
            next_cursor=int(cursor) + 1 if int(cursor) * size < total else None,
        )

    def normalize(self, r):
        source, external = r.get("source") or {}, r.get("identifiers") or {}
        links = r.get("links") or {}
        return Paper(
            doi=external.get("doi"),
            title=r.get("title") or "",
            authors=[a.get("displayName") or "" for a in (r.get("names") or {}).get("authors", [])],
            year=year(source.get("publishYear")),
            journal=source.get("sourceTitle"),
            publication_date=source.get("publishMonth"),
            document_type=", ".join(r.get("types") or []) or None,
            external_ids=ids(wos=r.get("uid"), pmid=external.get("pmid")),
            landing_urls=[links["record"]] if links.get("record") else [],
        )
