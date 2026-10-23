from ..config import credential
from ..models import Page, Paper
from ..normalization import ids, year
from .base import DiscoveryProvider, decode, keyword_terms, phrase


class Adapter(DiscoveryProvider):
    name, key_env, key_required, first_cursor = "scopus", "SCOPUS_API_KEY", True, "*"
    server_document_types = frozenset({"journal article"})
    server_languages = frozenset({"en", "eng", "english"})

    def translate(self, profile, query):
        # Scopus uses braces for an exact phrase. Double quotes are only a loose phrase,
        # so keep individual query keywords quoted and render configured multiword
        # include/exclude terms with braces.
        def exact(term):
            cleaned = term.replace("{", " ").replace("}", " ").strip()
            return "{" + cleaned + "}" if " " in cleaned else phrase(cleaned)

        required = [phrase(term) for term in keyword_terms(query)]
        required.extend(exact(term) for term in profile.include_terms)
        expression = " AND ".join(required)
        expression += "".join(" AND NOT " + exact(term) for term in profile.exclude_terms)
        q = "TITLE-ABS-KEY(" + expression + ")"
        if profile.year_from:
            q += f" AND PUBYEAR > {profile.year_from - 1}"
        if profile.year_to:
            q += f" AND PUBYEAR < {profile.year_to + 1}"
        if profile.language and profile.language.lower() in {"en", "eng", "english"}:
            q += " AND LANGUAGE(english)"
        if any(value.casefold() == "journal article" for value in profile.document_types):
            q += " AND DOCTYPE(ar)"
        return {"query": q, "view": "STANDARD"}

    async def fetch(self, translated, cursor, size):
        headers = {"X-ELS-APIKey": self.key, "Accept": "application/json"}
        if token := credential("SCOPUS_INSTTOKEN"):
            headers["X-ELS-Insttoken"] = token
        data = decode(
            await self.transport.request(
                "GET",
                "https://api.elsevier.com/content/search/scopus",
                params={**translated, "cursor": cursor, "count": min(size, 25)},
                headers=headers,
            )
        )["search-results"]
        total = int(data["opensearch:totalResults"])
        return Page(
            records=data.get("entry", []) if total else [],
            total=total,
            next_cursor=(data.get("cursor") or {}).get("@next"),
        )

    def normalize(self, r):
        return Paper(
            doi=r.get("prism:doi"),
            title=r.get("dc:title") or "",
            authors=[r["dc:creator"]] if r.get("dc:creator") else [],
            flags=["AUTHORS_FIRST_ONLY"] if r.get("dc:creator") else [],
            year=year(r.get("prism:coverDate")),
            publication_date=r.get("prism:coverDate"),
            journal=r.get("prism:publicationName"),
            publisher_raw=r.get("dc:publisher"),
            document_type=r.get("subtypeDescription"),
            external_ids=ids(scopus=r.get("eid")),
            landing_urls=[
                link["@href"]
                for link in r.get("link", [])
                if link.get("@ref") == "scopus" and link.get("@href")
            ],
        )
