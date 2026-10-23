import asyncio

import httpx
import pytest
from pydantic import ValidationError

from paper_acquisition.config import Settings
from paper_acquisition.dedup import Deduplicator
from paper_acquisition.discovery import discover, rebuild
from paper_acquisition.errors import AuthenticationError, MissingCredentials, RateLimitError
from paper_acquisition.http import Transport
from paper_acquisition.models import Paper, SearchProfile
from paper_acquisition.normalization import finalize, normalize_doi, original_doi, publisher
from paper_acquisition.providers import registry
from paper_acquisition.storage import Catalog


@pytest.mark.parametrize("value", ["https://doi.org/10.1234/ABC", "doi:10.1234/abc", "10.1234/ABC"])
def test_doi(value):
    assert normalize_doi(value) == "10.1234/abc"


def test_doi_punctuation_is_not_destroyed():
    assert normalize_doi("10.1234/abc(1)") == "10.1234/abc(1)"
    assert normalize_doi("not a doi") is None


def test_original_doi_preserves_provider_casing_while_identity_key_casefolds():
    value = "https://doi.org/10.1103/PhysRevB.110.L161408"
    assert original_doi(value) == "10.1103/PhysRevB.110.L161408"
    paper = finalize(Paper(doi=value))
    assert paper.doi == "10.1103/PhysRevB.110.L161408"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("American Physical Society", "aps"),
        ("AIP Publishing", "aip"),
        ("Springer Nature", "springer_nature"),
        ("Taylor & Francis", "taylor_francis"),
        ("SAGE Publications", "sage"),
        ("Unknown", "other"),
    ],
)
def test_publishers(value, expected):
    assert publisher(value) == expected


def test_profile_validation():
    with pytest.raises(ValidationError):
        SearchProfile(topic="x", year_from=2026, year_to=2000)
    with pytest.raises(ValidationError):
        SearchProfile(topic=" ")
    with pytest.raises(ValidationError):
        SearchProfile(topic="x", max_results_per_provider=0)


def test_dedup_preserves_provenance():
    d = Deduplicator([])
    d.add(finalize(Paper(doi="https://doi.org/10.1234/X", source_hits=["a"], provenance=["1"])))
    result = d.add(finalize(Paper(doi="10.1234/x", source_hits=["b"], provenance=["2"])))
    assert len(d.papers) == 1
    assert result.provenance == ["1", "2"]
    assert result.source_hits == ["a", "b"]
    assert result.doi == "10.1234/X"


def test_conflicting_dois_never_merge():
    d = Deduplicator([])
    d.add(Paper(doi="10.1234/a", title="Same title", external_ids={"pmid": ["1"]}))
    result = d.add(Paper(doi="10.1234/b", title="Same title", external_ids={"pmid": ["1"]}))
    assert len(d.papers) == 2
    assert result.doi == "10.1234/b"


def test_title_and_external_ids_never_merge_without_doi():
    d = Deduplicator([])
    for _ in range(3):
        d.add(
            Paper(title="A study", year=2020, authors=["Alice"], journal="J", external_ids={"arxiv": ["123"]})
        )
    assert len(d.papers) == 3


@pytest.mark.parametrize("name", ["scopus", "wos", "lens", "core"])
def test_missing_credentials(name, monkeypatch):
    cls = registry(include_metadata_sources=True)[name]
    monkeypatch.delenv(cls.key_env, raising=False)
    with pytest.raises(MissingCredentials):
        cls(None).check()


def test_provider_queries_are_translated():
    profile = SearchProfile(
        topic="coupling",
        year_from=2000,
        year_to=2026,
        include_terms=["magnetic"],
        exclude_terms=["biological"],
    )
    translated = {
        name: cls(None).translate(profile, "coupling")
        for name, cls in registry(include_metadata_sources=True).items()
        if name != "core"
    }
    assert "TITLE-ABS-KEY" in translated["scopus"]["query"]
    assert '"coupling" AND "magnetic"' in translated["scopus"]["query"]
    assert "TS=(" in translated["wos"]["q"]
    assert '"coupling" AND "magnetic"' in translated["wos"]["q"]
    assert "all:" in translated["arxiv"]["search_query"]
    assert "ANDNOT" in translated["arxiv"]["search_query"]
    assert "query.bibliographic" in translated["crossref"]
    assert "year" in translated["semantic_scholar"]
    assert translated["semantic_scholar"]["query"].startswith('"coupling" + "magnetic"')


def test_multiword_queries_require_each_keyword_independently():
    profile = SearchProfile(topic="multiferroic magnetoelectric coupling")
    translated = {
        name: registry()[name](None).translate(profile, profile.topic)
        for name in ["openalex", "scopus", "wos", "semantic_scholar", "lens"]
    }
    required = '"multiferroic" AND "magnetoelectric" AND "coupling"'
    assert translated["openalex"]["search"] == required
    assert required in translated["scopus"]["query"]
    assert required in translated["wos"]["q"]
    assert translated["semantic_scholar"]["query"] == ('"multiferroic" + "magnetoelectric" + "coupling"')
    assert len(translated["lens"]["query"]["bool"]["must"]) == 3


def test_scopus_uses_braces_for_exact_include_phrase_and_server_filters():
    profile = SearchProfile(
        topic="multiferroic",
        include_terms=["magnetoelectric coupling"],
        year_from=2024,
        year_to=2026,
        language="en",
        document_types=["journal article"],
    )
    query = registry()["scopus"](None).translate(profile, "multiferroic")["query"]
    assert '"multiferroic" AND {magnetoelectric coupling}' in query
    assert "LANGUAGE(english)" in query
    assert "DOCTYPE(ar)" in query


def test_http_retry_and_auth(monkeypatch):

    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = Transport(Settings(attempts=3), 0, lambda *args: "raw", client)
        with pytest.raises(RateLimitError):
            await transport.request("GET", "https://example.org")
        await transport.close()

    asyncio.run(run())
    assert len(calls) == 3


def test_auth_failure_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403)

    async def run():
        transport = Transport(
            Settings(), 0, lambda *args: "raw", httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        with pytest.raises(AuthenticationError):
            await transport.request("GET", "https://example.org")
        await transport.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_discover_pagination_union_rerun_and_rebuild(tmp_path, monkeypatch):

    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    monkeypatch.delenv("SCOPUS_API_KEY", raising=False)
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.host == "api.openalex.org":
            cursor = request.url.params["cursor"]
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2, "next_cursor": "next" if cursor == "*" else None},
                    "results": [
                        {
                            "id": "W1" if cursor == "*" else "W2",
                            "doi": "10.1234/a" if cursor == "*" else "10.1234/b",
                            "title": "Paper " + cursor,
                        }
                    ],
                },
            )
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={"message": {"total-results": 1, "items": [{"DOI": "10.1234/a", "title": ["Paper *"]}]}},
            )
        return httpx.Response(400)

    def factory(name):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    settings = Settings(
        data_dir=tmp_path, providers=["openalex", "crossref", "scopus", "openaire"], page_size=1
    )
    profile = SearchProfile(topic="x", max_results_per_provider=10)
    report = asyncio.run(
        discover(profile, settings, client_factory=factory, adapters=registry(include_metadata_sources=True))
    )
    assert [p["status"] for p in report["providers"]] == ["SUCCESS", "SUCCESS", "SKIPPED", "FAILED"]
    assert report["status"] == "PARTIAL"
    report2 = asyncio.run(
        discover(profile, settings, client_factory=factory, adapters=registry(include_metadata_sources=True))
    )
    assert report["run_id"] != report2["run_id"]
    catalog = Catalog(tmp_path)
    assert len(catalog.papers()) == 2
    merged = next(p for p in catalog.papers() if p.doi == "10.1234/a")
    assert len(merged.provenance) == 4
    assert set(merged.source_hits) == {"openalex", "crossref"}
    assert catalog.db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 6
    assert catalog.db.execute("PRAGMA foreign_key_check").fetchall() == []
    catalog.close()
    result = rebuild(settings)
    assert len(result.joinpath("master_papers.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert len(list(tmp_path.glob("discovery/raw/**/*.body"))) == 8


def test_raw_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-canary-value")

    def factory(name):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"meta": {"count": 0}, "results": []})
            )
        )

    report = asyncio.run(
        discover(
            SearchProfile(topic="x"),
            Settings(data_dir=tmp_path, providers=["openalex"]),
            client_factory=factory,
        )
    )
    assert report["providers"][0]["status"] == "SUCCESS"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"secret-canary-value" not in path.read_bytes()


def test_export_is_immutable(tmp_path):
    catalog = Catalog(tmp_path)
    catalog.save_paper(Paper(title="=SUM(A1:A2)"))
    first = catalog.export()
    before = (first / "master_papers.csv").read_bytes()
    assert first != catalog.export()
    assert (first / "master_papers.csv").read_bytes() == before
    assert "'=SUM" in before.decode("utf-8-sig")
    catalog.close()
