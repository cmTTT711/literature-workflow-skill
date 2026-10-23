import asyncio

import httpx
import pytest

from paper_acquisition.config import Settings
from paper_acquisition.errors import DocumentationBlocked, ResponseError
from paper_acquisition.http import Transport
from paper_acquisition.models import SearchProfile
from paper_acquisition.normalization import finalize
from paper_acquisition.providers import registry


@pytest.mark.parametrize(
    "name,payload",
    [
        ("scopus", {"dc:title": "Test paper", "prism:doi": "10.1234/X", "eid": "2-s2.0-1"}),
        ("wos", {"title": "Test paper", "identifiers": {"doi": "10.1234/X"}, "uid": "WOS:1"}),
        (
            "lens",
            {"title": "Test paper", "lens_id": "1", "external_ids": [{"type": "doi", "value": "10.1234/X"}]},
        ),
        (
            "openaire",
            {"mainTitle": "Test paper", "pids": [{"scheme": "doi", "value": "10.1234/X"}], "id": "oa:1"},
        ),
        ("semantic_scholar", {"title": "Test paper", "paperId": "123", "externalIds": {"DOI": "10.1234/X"}}),
        ("openalex", {"title": "Test paper", "id": "W1", "doi": "10.1234/X"}),
        ("crossref", {"title": ["Test paper"], "DOI": "10.1234/X"}),
        ("arxiv", {"title": "Test paper", "id": "http://arxiv.org/abs/1234.56789v2", "doi": "10.1234/X"}),
    ],
)
def test_normalize_contract(name, payload):
    paper = finalize(registry(include_metadata_sources=True)[name](None).normalize(payload))
    assert paper.title == "Test paper"
    assert paper.doi == "10.1234/X"


def test_openaire_null_fields_regression():
    paper = registry(include_metadata_sources=True)["openaire"](None).normalize(
        {
            "mainTitle": "Test",
            "id": "1",
            "pids": None,
            "authors": None,
            "container": None,
            "publisher": None,
            "descriptions": None,
            "language": {"code": "eng"},
            "instances": [{"urls": ["https://example.org/article"]}],
        }
    )
    assert paper.doi is None
    assert paper.language == "eng"
    assert paper.landing_urls == ["https://example.org/article"]


@pytest.mark.parametrize(
    "name,envelope,cursor,next_cursor",
    [
        (
            "scopus",
            {
                "search-results": {
                    "opensearch:totalResults": "30",
                    "entry": [{"eid": "1"}],
                    "cursor": {"@next": "c2"},
                }
            },
            "*",
            "c2",
        ),
        ("wos", {"metadata": {"total": 30}, "hits": [{"uid": "1"}]}, 1, 2),
        ("lens", {"total": 30, "data": [{"lens_id": "1"}], "scroll_id": "c2"}, None, "c2"),
        ("openaire", {"header": {"numFound": 30, "nextCursor": "c2"}, "results": [{"id": "1"}]}, "*", "c2"),
        ("semantic_scholar", {"total": 30, "data": [{"paperId": "1"}], "token": "c2"}, None, "c2"),
    ],
)
def test_fetch_mock_contract(name, envelope, cursor, next_cursor, monkeypatch):
    cls = registry(include_metadata_sources=True)[name]
    if cls.key_env:
        monkeypatch.setenv(cls.key_env, "mock-key")
    requests = []

    async def run():

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=envelope)

        transport = Transport(
            Settings(), 0, lambda *a: "raw", httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        provider = cls(transport)
        provider.check()
        page = await provider.fetch(provider.translate(SearchProfile(topic="x"), "x"), cursor, 10)
        assert page.next_cursor == next_cursor
        assert len(page.records) == 1
        await transport.close()

    asyncio.run(run())
    expected_headers = {
        "scopus": "X-ELS-APIKey",
        "wos": "X-ApiKey",
        "lens": "Authorization",
        "semantic_scholar": "x-api-key",
        "openaire": "Authorization",
    }
    if name in expected_headers:
        assert "mock-key" in requests[0].headers[expected_headers[name]]


def test_arxiv_xml_and_pagination():
    feed = '<feed xmlns="http://www.w3.org/2005/Atom"\n        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">\n        <opensearch:totalResults>2</opensearch:totalResults><entry>\n        <id>http://arxiv.org/abs/1234.56789v2</id><title>Test paper</title>\n        <author><name>Alice</name></author><published>2020-01-01</published>\n        <link href="http://arxiv.org/pdf/1234.56789v2" title="pdf"/></entry></feed>'

    async def run():
        transport = Transport(
            Settings(),
            0,
            lambda *args: "raw",
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=feed))),
        )
        adapter = registry(include_metadata_sources=True)["arxiv"](transport)
        page = await adapter.fetch(adapter.translate(SearchProfile(topic="x"), "x"), 0, 1)
        assert page.total == 2 and page.next_cursor == 1
        paper = finalize(adapter.normalize(page.records[0]))
        assert paper.external_ids["arxiv"] == ["1234.56789"]
        assert paper.authors == ["Alice"]
        await transport.close()

    asyncio.run(run())


def test_arxiv_http_200_error_feed_is_failure():

    async def run():
        transport = Transport(
            Settings(),
            0,
            lambda *args: "raw",
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200,
                        text='<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#1</id></entry></feed>',
                    )
                )
            ),
        )
        adapter = registry(include_metadata_sources=True)["arxiv"](transport)
        with pytest.raises(ResponseError):
            await adapter.fetch({}, 0, 1)
        await transport.close()

    asyncio.run(run())


def test_core_explicit_gate(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "mock-secret")
    with pytest.raises(DocumentationBlocked):
        registry(include_metadata_sources=True)["core"](None).check()


def test_lens_source_publisher():
    paper = registry(include_metadata_sources=True)["lens"](None).normalize(
        {"title": "Test", "source": {"publisher": "Example Press", "title": "Physics"}}
    )
    assert paper.publisher_raw == "Example Press"
    assert paper.journal == "Physics"


def test_scopus_standard_author_is_explicitly_partial():
    paper = registry(include_metadata_sources=True)["scopus"](None).normalize(
        {"dc:creator": "Alice", "dc:title": "Test"}
    )
    assert paper.authors == ["Alice"]
    assert "AUTHORS_FIRST_ONLY" in paper.flags


def test_openaire_declared_doi_url_and_repository_identifier():
    paper = registry(include_metadata_sources=True)["openaire"](None).normalize(
        {
            "mainTitle": "Test",
            "pids": [{"scheme": "handle", "value": "123/456"}],
            "instances": [{"urls": ["https://doi.org/10.1234/ABC"]}],
        }
    )
    assert paper.doi == "10.1234/abc"
    assert paper.external_ids["handle"] == ["123/456"]
    assert "DOI_FROM_DECLARED_URL" in paper.flags
