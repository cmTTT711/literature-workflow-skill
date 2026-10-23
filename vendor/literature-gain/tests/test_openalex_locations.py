import asyncio

import httpx
import pytest
from test_oa_acquisition import TITLE, pdf_bytes

from paper_acquisition.acquisition.models import AcquisitionError
from paper_acquisition.acquisition.openalex_locations import (
    canonical_url,
    exhaust_openalex,
    fetch_works,
    work_candidates,
)
from paper_acquisition.acquisition.service import MetadataClients, resolve_all
from paper_acquisition.config import Settings
from paper_acquisition.master import prepare_master
from paper_acquisition.models import Paper
from paper_acquisition.storage import Catalog, write_new


def location(url, version="publishedVersion", kind="repository"):
    return {
        "pdf_url": url,
        "is_oa": True,
        "version": version,
        "license": "cc-by",
        "source": {"id": "S1", "display_name": "Repository", "type": kind, "is_oa": True},
    }


def test_all_entry_points_versions_and_url_dedup():
    paper = Paper(doi="10.1234/example")
    published = location("https://EXAMPLE.org:443/a%7Eb.pdf#page=2", kind="journal")
    work = {
        "id": "W1",
        "best_oa_location": published,
        "primary_location": published,
        "locations": [
            published,
            location("https://repo.example/accepted.pdf", "acceptedVersion"),
            location("https://repo.example/submitted.pdf", "submittedVersion"),
            {"landing_page_url": "https://repo.example/landing", "source": {"type": "repository"}},
        ],
        "open_access": {"oa_url": "https://repo.example/oa"},
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf"},
    }
    candidates, stats = work_candidates(paper, work)
    assert stats["locations_count"] == 4 and stats["multiple_locations"] == 1
    assert stats["before_dedup"] == 8 and stats["after_dedup"] == 6
    assert [c.version for c in candidates[:3]] == ["publishedVersion", "acceptedVersion", "submittedVersion"]
    assert candidates[3].resolver_provenance[0]["origin"] == "content_urls.pdf"
    assert len(candidates[0].resolver_provenance) == 3
    assert candidates[0].source_url == "https://example.org/a~b.pdf"
    assert all(c.resolver_provenance[0]["location"]["source"]["is_oa"] for c in candidates[:3])
    assert sum(not c.direct_pdf for c in candidates) == 2


def test_content_pdf_duplicate_and_missing_pdf():
    paper = Paper(doi="10.1234/example")
    work = {
        "locations": [
            location("https://content.openalex.org/works/W1.pdf"),
            {"landing_page_url": "https://repo.example/a"},
            {},
        ],
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf#page=1"},
    }
    candidates, stats = work_candidates(paper, work)
    assert len(candidates) == 2 and stats["content_pdf"] == 1
    assert len(candidates[0].resolver_provenance) == 2
    assert canonical_url("https://user:password@example.org/a") is None
    assert canonical_url("https://example.org/a?signature=One") != canonical_url(
        "https://example.org/a?signature=one"
    )


@pytest.mark.parametrize("size,batches", [(0, []), (100, [100]), (101, [100, 1])])
def test_batch_boundary_full_work_metadata(tmp_path, monkeypatch, size, batches):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    seen = []

    def factory(source):
        def handle(request):
            assert source == "openalex" and request.url.params["per_page"] == "100"
            assert "select" not in request.url.params
            dois = request.url.params["filter"][4:].split("|")
            seen.append(len(dois))
            return httpx.Response(
                200,
                json={
                    "meta": {"count": len(dois)},
                    "results": [
                        {"id": f"W{i}", "doi": d, "title": TITLE, "locations": []} for i, d in enumerate(dois)
                    ],
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def run():
        c = Catalog(tmp_path)
        clients = MetadataClients(c, Settings(data_dir=tmp_path), factory)
        try:
            rows = await fetch_works([Paper(doi=f"10.1234/{i}", title=TITLE) for i in range(size)], clients)
            assert len(rows) == size
        finally:
            await clients.close()
            c.close()

    asyncio.run(run())
    assert seen == batches


@pytest.mark.parametrize("first_failure", ["HTTP_403", "VERIFY_FAILED"])
def test_first_location_failure_then_success_and_verified_rerun(tmp_path, first_failure):
    paper = Paper(
        doi="10.1234/example", title=TITLE, publisher_raw="Elsevier", document_type="journal-article"
    )
    c = Catalog(tmp_path)
    c.save_paper(paper)
    c.close()
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))
    best = location("https://publisher.example/first.pdf", kind="journal")
    work = {
        "id": "W1",
        "doi": paper.doi,
        "title": TITLE,
        "best_oa_location": best,
        "locations": [
            best,
            location("https://repo.example/second.pdf"),
            location("https://repo.example/third.pdf", "submittedVersion"),
        ],
    }
    queries = []

    def factory(source):
        def handle(request):
            queries.append(1)
            return httpx.Response(200, json={"meta": {"count": 1}, "results": [work]})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    good = pdf_bytes(tmp_path)

    class Downloader:
        def __init__(self):
            self.calls = []
            self.trace = []

        async def download(self, candidate, target):
            self.calls.append(candidate.source_url)
            if "first.pdf" in candidate.source_url:
                if first_failure == "HTTP_403":
                    raise AcquisitionError("HTTP_403")
                write_new(target, b"<html>Login</html>" + b" " * 600)
            else:
                write_new(target, good)
            return {"trace": []}

        async def close(self):
            pass

    downloader = Downloader()
    report = asyncio.run(exhaust_openalex(tmp_path, client_factory=factory, downloader=downloader))
    assert report["new_verified"] == 1 and not report["remaining_candidates"]
    assert downloader.calls == ["https://publisher.example/first.pdf", "https://repo.example/second.pdf"]
    again = asyncio.run(exhaust_openalex(tmp_path, client_factory=factory, downloader=downloader))
    assert again["queried_dois"] == 0 and again["new_attempts"] == 0 and len(queries) == 1
    c = Catalog(tmp_path)
    assert c.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not c.db.execute("PRAGMA foreign_key_check").fetchall()
    assert c.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    c.close()


def test_all_locations_exhausted_beyond_per_pass_limit(tmp_path):
    paper = Paper(
        doi="10.1234/example", title=TITLE, publisher_raw="Elsevier", document_type="journal-article"
    )
    c = Catalog(tmp_path)
    c.save_paper(paper)
    c.close()
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))
    work = {
        "id": "W1",
        "doi": paper.doi,
        "title": TITLE,
        "locations": [location(f"https://repo.example/{i}.pdf") for i in range(5)],
    }

    def factory(source):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"results": [work], "meta": {"count": 1}})
            )
        )

    class Downloader:
        def __init__(self):
            self.calls = []
            self.trace = []

        async def download(self, candidate, target):
            self.calls.append(candidate.source_url)
            raise AcquisitionError("HTTP_403")

        async def close(self):
            pass

    d = Downloader()
    r = asyncio.run(exhaust_openalex(tmp_path, client_factory=factory, downloader=d))
    assert len(d.calls) == 5 and len(set(d.calls)) == 5 and not r["remaining_candidates"]
    again = asyncio.run(exhaust_openalex(tmp_path, client_factory=factory, downloader=d))
    assert again["new_attempts"] == 0 and len(d.calls) == 5


def test_historical_canonical_failure_is_not_retried(tmp_path):
    from paper_acquisition.acquisition.service import acquire

    paper = Paper(
        doi="10.1234/example",
        title=TITLE,
        publisher_raw="Elsevier",
        document_type="journal-article",
        oa_metadata=[{"provider": "openalex", "locations": [location("https://repo.example/%7E.pdf#old")]}],
    )
    c = Catalog(tmp_path)
    c.save_paper(paper)
    c.close()
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))

    class Downloader:
        def __init__(self):
            self.calls = []
            self.trace = []

        async def download(self, candidate, target):
            self.calls.append(candidate.source_url)
            raise AcquisitionError("HTTP_403")

        async def close(self):
            pass

    d = Downloader()
    asyncio.run(acquire(tmp_path, downloader=d))
    work = {
        "id": "W1",
        "doi": paper.doi,
        "title": TITLE,
        "locations": [location("https://REPO.example:443/~.pdf#new")],
    }

    def factory(source):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [work]}))
        )

    r = asyncio.run(exhaust_openalex(tmp_path, client_factory=factory, downloader=d))
    assert len(d.calls) == 1 and r["new_attempts"] == 0 and not r["remaining_candidates"]
    assert canonical_url("http://arxiv.org/pdf/2401.12345") == canonical_url(
        "https://arxiv.org/pdf/2401.12345"
    )
