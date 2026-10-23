import asyncio

import httpx
import pytest

from paper_acquisition.audit import audit
from paper_acquisition.cache import ResponseCache
from paper_acquisition.config import Settings, load_env
from paper_acquisition.dedup import Deduplicator
from paper_acquisition.discovery import discover
from paper_acquisition.errors import OfflineCacheMiss, QueryError, RateLimitError
from paper_acquisition.http import Transport
from paper_acquisition.locking import RunLock
from paper_acquisition.models import Paper, SearchProfile
from paper_acquisition.providers import registry
from paper_acquisition.storage import Catalog


def test_final_registry():
    assert set(registry()) == {
        "openalex",
        "scopus",
        "wos",
        "semantic_scholar",
        "lens",
    }
    assert set(Settings().providers) == set(registry())


def test_local_env_only_reports_presence(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SCOPUS_API_KEY", raising=False)
    monkeypatch.setenv("OPENALEX_API_KEY", "shell-canary")
    file = tmp_path / ".env"
    file.write_text(
        'export SCOPUS_API_KEY="local-canary" # private\nOPENALEX_API_KEY=ignored\nUNKNOWN_SECRET=x\n'
    )
    result = load_env(file)
    assert result["SCOPUS_API_KEY"] is True
    assert all(isinstance(v, bool) for v in result.values())
    import os

    assert os.environ["OPENALEX_API_KEY"] == "shell-canary"
    assert capsys.readouterr().out == ""


def test_year_range_regression():
    adapter = registry()["semantic_scholar"](None)
    assert (
        adapter.translate(SearchProfile(topic="x", year_from=2000, year_to=2026), "x")["year"] == "2000-2026"
    )
    assert adapter.translate(SearchProfile(topic="x", year_from=2000), "x")["year"] == "2000-"


def test_year_slices_do_not_overlap():
    profile = SearchProfile(topic="x", year_from=2000, year_to=2004, year_slice_size=2)
    assert [(p.year_from, p.year_to) for p, q in profile.search_tasks()] == [
        (2000, 2001),
        (2002, 2003),
        (2004, 2004),
    ]


def test_strict_capability_rejects_unsupported_filter():
    with pytest.raises(QueryError):
        registry()["lens"](None).warnings(SearchProfile(topic="x", language="en", strict_capabilities=True))
    with pytest.raises(QueryError):
        registry(include_metadata_sources=True)["crossref"](None).warnings(
            SearchProfile(topic="x", exclude_terms=["biology"], strict_capabilities=True)
        )


def test_missing_cursor_never_claims_completion(tmp_path):
    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"meta": {"count": 100}, "results": [{"id": "W1", "title": "A"}]}
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
    assert report["providers"][0]["status"] == "TRUNCATED"
    assert "REMOTE_TOTAL_NOT_EXHAUSTED" in report["providers"][0]["warnings"]


def test_optional_title_abstract_filter_rejects_unverifiable_hits(tmp_path):
    records = [
        {
            "id": "W1",
            "doi": "https://doi.org/10.1234/keep",
            "title": "A multiferroic study",
            "abstract_inverted_index": {"magnetoelectric": [0], "coupling": [1]},
        },
        {
            "id": "W2",
            "doi": "https://doi.org/10.1234/drop",
            "title": "A multiferroic study without the required phrase",
        },
    ]

    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"meta": {"count": 2}, "results": records})
            )
        )

    profile = SearchProfile(
        topic="multiferroic",
        include_terms=["magnetoelectric coupling"],
        extensions={"require_terms_in_title_or_abstract": True},
    )
    report = asyncio.run(
        discover(profile, Settings(data_dir=tmp_path, providers=["openalex"]), client_factory=factory)
    )
    assert report["master_papers"] == 1
    catalog = Catalog(tmp_path)
    assert [paper.doi for paper in catalog.papers()] == ["10.1234/keep"]
    catalog.close()


def test_crossref_unchanged_cursor_can_advance(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "total-results": 3,
                    "next-cursor": "same",
                    "items": [{"DOI": f"10.1234/{len(calls)}"}],
                }
            },
        )

    def factory(_):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(
        discover(
            SearchProfile(topic="x", max_results_per_provider=5),
            Settings(data_dir=tmp_path, providers=["crossref"], page_size=1),
            client_factory=factory,
            adapters=registry(include_metadata_sources=True),
        )
    )
    assert len(calls) == 3
    assert report["providers"][0]["status"] == "SUCCESS"


def test_page_size_is_fixed_in_last_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WOS_API_KEY", "mock-key")

    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    sizes = []

    def handler(request):
        size = int(request.url.params["limit"])
        page = int(request.url.params["page"])
        sizes.append(size)
        return httpx.Response(
            200,
            json={
                "metadata": {"total": 4},
                "hits": [{"uid": str(i), "title": str(i)} for i in range((page - 1) * size, page * size)],
            },
        )

    def factory(_):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(
        discover(
            SearchProfile(topic="x", max_results_per_provider=3),
            Settings(data_dir=tmp_path, providers=["wos"], page_size=2),
            client_factory=factory,
        )
    )
    assert sizes == [2, 2]
    assert report["providers"][0]["raw_returned_records"] == 4
    catalog = Catalog(tmp_path)
    assert {p.title for p in catalog.papers()} == {"0", "1", "2"}
    catalog.close()


def test_response_cache_and_offline_miss(tmp_path):
    settings = Settings(data_dir=tmp_path)
    cache = ResponseCache(tmp_path / "cache", 3600)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": 1})

    async def run():
        t = Transport(
            settings,
            0,
            lambda *a: "raw",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            cache=cache,
        )
        await t.request("GET", "https://example.org")
        await t.request("GET", "https://example.org")
        assert t.requests == 1 and t.cache_hits == 1
        settings.offline = True
        with pytest.raises(OfflineCacheMiss):
            await t.request("GET", "https://example.org/new")
        await t.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_run_lock_released(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path), pytest.raises(RuntimeError), RunLock(path):
        pass
    with RunLock(path):
        pass


def test_arxiv_cooldown_blocks_next_run(tmp_path):
    settings = Settings(data_dir=tmp_path, coordination_dir=tmp_path / "coordination", attempts=1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "600"})

    async def run():
        for _ in range(2):
            t = Transport(
                settings,
                3.2,
                lambda *a: "raw",
                httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                provider="arxiv",
            )
            with pytest.raises(RateLimitError):
                await t.request("GET", "https://export.arxiv.org/api/query")
            await t.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_scale_index_and_streaming_export(tmp_path, monkeypatch):
    dedup = Deduplicator([])
    for i in range(10000):
        dedup.add(Paper(doi=f"10.1234/{i}", title=f"Material sample {i}"))
    for i in range(1000):
        dedup.add(Paper(doi=f"10.1234/{i}", title=f"Material sample {i}"))
    assert len(dedup.papers) == 10000
    assert dedup.comparisons == 1000
    catalog = Catalog(tmp_path)
    with catalog.db:
        catalog.db.executemany(
            "INSERT INTO papers VALUES(?,?,?)",
            [(p.paper_id, p.doi, p.model_dump_json()) for p in dedup.papers],
        )
    monkeypatch.setattr(catalog, "papers", lambda: pytest.fail("Exporter must stream rows"))
    output = catalog.export()
    with (output / "master_papers.jsonl").open(encoding="utf-8") as f:
        assert sum(1 for _ in f) == 10000
    assert catalog.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    catalog.close()


def test_audit_detects_raw_damage(tmp_path):
    def factory(_):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"meta": {"count": 1}, "results": [{"id": "W1", "title": "A"}]}
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
    assert audit(tmp_path)["raw_errors"] == 0
    next(tmp_path.glob("discovery/raw/**/*.body")).write_bytes(b"damaged")
    assert audit(tmp_path)["raw_errors"] == 1
