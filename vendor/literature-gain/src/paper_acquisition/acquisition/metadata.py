"""Optional metadata clients, isolated from download orchestration."""

import hashlib
from collections import defaultdict
from urllib.parse import quote
from uuid import uuid4

from ..cache import ResponseCache
from ..config import credential
from ..http import Transport
from ..models import now
from ..normalization import normalize_doi
from ..storage import dumps, write_new
from .pdf_worker import title_similarity


class MetadataClients:
    def __init__(self, catalog, settings, client_factory=None):
        self.catalog, self.settings = catalog, settings
        self.clients, self.errors = {}, []
        self.client_factory = client_factory

    def transport(self, source):
        if source not in self.clients:

            def observer(request, response, attempt):
                evidence_id = uuid4().hex
                path = self.catalog.root / "acquisition" / "resolver_raw" / source / f"{evidence_id}.json"
                write_new(path, response.content)
                meta = {
                    "request": request,
                    "http_status": response.status_code,
                    "attempt": attempt,
                    "at": now(),
                }
                self.catalog.execute(
                    "INSERT INTO resolver_evidence VALUES(?,?,?,?,?)",
                    (
                        evidence_id,
                        source,
                        str(path.relative_to(self.catalog.root)),
                        hashlib.sha256(response.content).hexdigest(),
                        dumps(meta),
                    ),
                )
                return evidence_id

            self.clients[source] = Transport(
                self.settings,
                1.1,
                observer,
                client=self.client_factory(source) if self.client_factory else None,
                cache=ResponseCache(self.catalog.root / "acquisition" / "cache" / source, 86400),
                provider=source,
            )
        return self.clients[source]

    async def enrich(self, papers, online):
        extra = defaultdict(list)
        if not online:
            return extra
        by_doi = defaultdict(list)
        papers_by_id = {p.paper_id: p for p in papers}
        for paper in papers:
            if doi := normalize_doi(paper.doi):
                by_doi[doi].append(paper.paper_id)
        from .openalex_locations import fetch_works

        extra = await fetch_works(papers, self)
        dois = sorted(by_doi)
        email = credential("UNPAYWALL_EMAIL") or credential("PAPER_CONTACT_EMAIL")
        if email:
            transport = self.transport("unpaywall")
            transport.secrets.add(email)
            for doi in dois:
                try:
                    response = await transport.request(
                        "GET", "https://api.unpaywall.org/v2/" + quote(doi, safe="/"), params={"email": email}
                    )
                    record = response.json()
                    if normalize_doi(record.get("doi")) != doi:
                        raise ValueError("UNPAYWALL_DOI_MISMATCH")
                    for paper_id in by_doi[doi]:
                        extra[paper_id].append(
                            ("unpaywall", record, {"evidence_id": transport.last_raw_path})
                        )
                except Exception as error:  # noqa: BLE001
                    self.errors.append({"source": "unpaywall", "code": type(error).__name__, "doi": doi})
                    # A missing or malformed individual DOI must not starve later papers.
                    if transport.last_status not in {200, 404, 410}:
                        break
        transport = self.transport("openaire")
        # OpenAIRE allows at most four logical operators (five PID values).
        # A candidate URL is not a downloaded PDF. Retain lower-priority fallbacks too.
        for start in range(0, len(dois), 5):
            batch = dois[start : start + 5]
            cursor = "*"
            seen = set()
            received = 0
            try:
                for page_number in range(5):
                    response = await transport.request(
                        "GET",
                        "https://api.openaire.eu/graph/v3/research-products",
                        params={
                            "pid": "(" + " OR ".join('"' + d + '"' for d in batch) + ")",
                            "pageSize": 100,
                            "cursor": cursor,
                        },
                        headers={"Authorization": "Bearer " + credential("OPENAIRE_TOKEN")}
                        if credential("OPENAIRE_TOKEN")
                        else {},
                    )
                    payload = response.json()
                    received += len(payload["results"])
                    for record in payload["results"]:
                        ids = {
                            normalize_doi(p.get("value"))
                            for p in record.get("pids") or []
                            if str(p.get("scheme")).lower() == "doi"
                        }
                        for instance in record.get("instances") or []:
                            ids.update(normalize_doi(u) for u in instance.get("urls") or [])
                        for doi in ids.intersection(batch):
                            for paper_id in by_doi[doi]:
                                if (
                                    title_similarity(
                                        papers_by_id[paper_id].title, record.get("mainTitle") or ""
                                    )
                                    >= 0.75
                                ):
                                    extra[paper_id].append(
                                        ("openaire", record, {"evidence_id": transport.last_raw_path})
                                    )
                    next_cursor = payload["header"].get("nextCursor")
                    total = payload["header"].get("numFound")
                    if total is not None and received >= total:
                        break
                    if not payload["results"] or not next_cursor:
                        if total is not None and received < total:
                            self.errors.append(
                                {"source": "openaire", "code": "LOOKUP_TRUNCATED", "dois": batch}
                            )
                        break
                    if next_cursor in seen or page_number == 4:
                        self.errors.append({"source": "openaire", "code": "LOOKUP_TRUNCATED", "dois": batch})
                        break
                    seen.add(next_cursor)
                    cursor = next_cursor
            except Exception as error:  # noqa: BLE001 -- authenticated and anonymous service failures isolated
                self.errors.append({"source": "openaire", "code": type(error).__name__, "dois": batch})
                break
        return extra

    def report(self):
        return {
            source: {
                "network_requests": t.requests,
                "cache_hits": t.cache_hits,
                "http_status": t.last_status,
                "rate_limit": t.rate_headers,
            }
            for source, t in self.clients.items()
        }

    async def close(self):
        for transport in self.clients.values():
            await transport.close()
