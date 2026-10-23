import asyncio
import json

import httpx
import pytest

from paper_acquisition.config import Settings
from paper_acquisition.discovery import discover, rebuild
from paper_acquisition.errors import NetworkError, RateLimitError
from paper_acquisition.http import Transport
from paper_acquisition.models import SearchProfile
from paper_acquisition.storage import Catalog


def test_echoed_credentials_are_redacted(tmp_path, monkeypatch):
    secret = "unique-secret-canary"
    monkeypatch.setenv("OPENALEX_API_KEY", secret)

    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"meta": {"count": 1}, "results": [{"id": "W1", "title": secret}]}
                )
            )
        )

    asyncio.run(
        discover(
            SearchProfile(topic="x"),
            Settings(data_dir=tmp_path, providers=["openalex"]),
            client_factory=factory,
        )
    )
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_malformed_record_retained_and_query_partial(tmp_path):
    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "meta": {"count": 2},
                        "results": [
                            {"title": "Bad", "publication_year": "not-a-year"},
                            {"id": "W1", "title": "Good"},
                        ],
                    },
                )
            )
        )

    report = asyncio.run(
        discover(
            SearchProfile(topic="x"),
            Settings(data_dir=tmp_path, providers=["openalex"]),
            client_factory=factory,
        )
    )
    result = report["providers"][0]
    assert result["status"] == "PARTIAL"
    assert result["normalized_records"] == 1
    assert result["normalization_failures"] == 1
    catalog = Catalog(tmp_path)
    assert catalog.db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
    assert catalog.db.execute("SELECT status FROM provider_queries").fetchone()[0] == "PARTIAL"
    assert len(catalog.papers()) == 1
    catalog.close()


def test_large_server_batch_is_preserved_but_capped(tmp_path):
    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"total": 5, "data": [{"paperId": str(i), "title": "Paper"} for i in range(5)]}
                )
            )
        )

    report = asyncio.run(
        discover(
            SearchProfile(topic="x", max_results_per_provider=2),
            Settings(data_dir=tmp_path, providers=["semantic_scholar"]),
            client_factory=factory,
        )
    )
    assert report["providers"][0]["status"] == "TRUNCATED"
    assert report["providers"][0]["records"] == 2
    raw = next(tmp_path.glob("discovery/raw/**/*.body"))
    assert len(json.loads(raw.read_bytes())["data"]) == 5


def test_query_variants_are_each_executed(tmp_path):
    queries = []

    def handler(request):
        queries.append(request.url.params["search"])
        return httpx.Response(200, json={"meta": {"count": 1}, "results": [{"id": "W1", "title": "Paper"}]})

    def factory(_):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(
        discover(
            SearchProfile(topic="x", queries=["first", "second"], max_results_per_provider=2),
            Settings(data_dir=tmp_path, providers=["openalex"]),
            client_factory=factory,
        )
    )
    assert queries == ['"first"', '"second"']
    assert report["master_papers"] == 1


def test_repeated_page_is_bounded(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "meta": {"count": 100, "next_cursor": str(len(calls))},
                "results": [{"id": "W1", "title": "Paper"}],
            },
        )

    def factory(_):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(
        discover(
            SearchProfile(topic="x", max_results_per_provider=100),
            Settings(data_dir=tmp_path, providers=["openalex"]),
            client_factory=factory,
        )
    )
    assert len(calls) == 2
    assert report["providers"][0]["status"] == "TRUNCATED"
    assert "REPEATED_PAGE" in report["providers"][0]["warnings"]


def test_rebuild_same_payload_without_ids(tmp_path):
    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"meta": {"count": 1}, "results": [{"title": "No identifiers"}]}
                )
            )
        )

    settings = Settings(data_dir=tmp_path, providers=["openalex"])
    for _ in range(2):
        asyncio.run(discover(SearchProfile(topic="x"), settings, client_factory=factory))
    output = rebuild(settings)
    records = output.joinpath("master_papers.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    assert len(json.loads(records[0])["provenance"]) == 2


def test_long_retry_after_does_not_retry_early():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async def run():
        transport = Transport(
            Settings(), 0, lambda *a: "raw", httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(RateLimitError):
            await transport.request("GET", "https://example.org")
        await transport.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_timeout_is_bounded_and_classified(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 7
        raise httpx.ReadTimeout("do not log this URL or credentials")

    async def run():
        transport = Transport(
            Settings(attempts=2, timeout_seconds=7),
            0,
            lambda *a: "raw",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(NetworkError):
            await transport.request("GET", "https://example.org")
        await transport.close()

    asyncio.run(run())
    assert len(calls) == 2
