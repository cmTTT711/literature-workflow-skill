import asyncio

import httpx
import pytest

from paper_acquisition.acquisition.openalex_locations import work_candidates
from paper_acquisition.acquisition.retry_openalex import ContentAuth
from paper_acquisition.models import Paper
from paper_acquisition.normalization import normalize_doi
from paper_acquisition.providers import registry


def test_preflight_retry_and_verified_rerun(tmp_path):
    import json

    from test_master import article, save
    from test_oa_acquisition import FakeDownloader, pdf_bytes

    from paper_acquisition.acquisition.models import AcquisitionError
    from paper_acquisition.acquisition.retry_openalex import content_targets, retry_blocked
    from paper_acquisition.acquisition.service import acquire
    from paper_acquisition.master import prepare_master
    from paper_acquisition.storage import Catalog

    paper = article(doi="10.1234/example", title="Magnetoelectric coupling in layered quantum materials")
    save(tmp_path, paper)
    asyncio.run(prepare_master(tmp_path, online=False, update_candidates=False))
    work = {
        "doi": paper.doi,
        "title": "Another metadata title",
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf"},
    }
    c = Catalog(tmp_path)
    paper = c.papers()[0]
    candidate = work_candidates(paper, work)[0][0]
    c.execute(
        "INSERT INTO access_candidates VALUES(?,?,?,?,?)",
        (candidate.candidate_id, paper.paper_id, candidate.priority, 1, candidate.model_dump_json()),
    )
    c.close()

    class Blocked(FakeDownloader):
        async def download(self, candidate, target):
            raise AcquisitionError("NONPUBLIC_ADDRESS")

    asyncio.run(acquire(tmp_path, downloader=Blocked(b"")))
    c = Catalog(tmp_path)
    assert content_targets(c) == [candidate.candidate_id]
    c.close()
    (tmp_path / "acquisition/openalex-locations-state.json").write_text(
        json.dumps({paper.paper_id: [["openalex", work, {"identity_accepted": False}]]})
    )
    recovered = FakeDownloader(pdf_bytes(tmp_path))
    report = asyncio.run(retry_blocked(tmp_path, downloader_factory=lambda _: recovered))
    assert report["new_verified"] == 1 and report["title_doi_matched_admitted"] == 1
    asyncio.run(retry_blocked(tmp_path, downloader_factory=lambda _: recovered))
    assert recovered.calls == 1
    assert not (tmp_path / "agent_tasks/agent_required.jsonl").read_text()


@pytest.mark.parametrize(
    "provider,own",
    [
        ("openalex", {"doi": "https://doi.org/10.1234/ABC"}),
        ("crossref", {"DOI": "10.1234/ABC"}),
        ("semantic_scholar", {"externalIds": {"DOI": "10.1234/ABC"}}),
        ("scopus", {"prism:doi": "10.1234/ABC"}),
        ("lens", {"external_ids": [{"type": "doi", "value": "10.1234/ABC"}]}),
        ("wos", {"identifiers": {"doi": "10.1234/ABC"}}),
    ],
)
def test_declared_field_only(provider, own):
    unrelated = {"references": [{"doi": "10.1234/reference"}], "funder": {"DOI": "10.13039/123"}}
    adapter = registry(include_metadata_sources=True)[provider](None)
    assert normalize_doi(adapter.normalize({**unrelated, **own}).doi) == "10.1234/abc"
    assert adapter.normalize(unrelated).doi is None


def test_funder_rejected_and_exact_work_doi_controls_identity():
    assert normalize_doi("https://doi.org/10.13039/123") is None
    paper = Paper(doi="10.1234/abc", title="Original title")
    work = {
        "doi": "https://doi.org/10.1234/ABC",
        "title": "Completely different",
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf"},
    }
    assert len(work_candidates(paper, work)[0]) == 1
    assert not work_candidates(paper, {**work, "doi": "10.1234/other"})[0]


def test_content_auth_is_scoped_and_redirects_do_not_leak_key():
    async def run():
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://files.example/a?sig=a%20b"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), auth=ContentAuth("test-canary")
        ) as client:
            for url in [
                "https://content.openalex.org/works/W1.pdf",
                "https://content.openalex.org/robots.txt",
                "https://files.example/a.pdf",
            ]:
                response = await client.get(url)
                assert response.headers["location"] == "https://files.example/a?sig=a%20b"
        assert seen[0].url.params["api_key"] == "test-canary"
        assert all("api_key" not in request.url.params for request in seen[1:])

        def redirect(request):
            return httpx.Response(
                302, headers={"location": "https://files.example/a?api_key=test-canary&x=1"}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(redirect), auth=ContentAuth("test-canary")
        ) as client:
            response = await client.get("https://content.openalex.org/works/W1.pdf")
            assert "test-canary" not in response.headers["location"]

    asyncio.run(run())


def test_normal_downloader_uses_openalex_key_without_persisting_it(tmp_path, monkeypatch):
    from paper_acquisition.acquisition import network
    from paper_acquisition.acquisition.models import AcquisitionOptions
    from paper_acquisition.acquisition.resolver import candidate

    monkeypatch.setenv("OPENALEX_API_KEY", "test-content-key")
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, content=b"%PDF-1.7\nexample")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    async def valid(_url):
        return None

    async def no_sleep(_seconds):
        return None

    async def run():
        downloader = network.PublicDownloader(
            tmp_path, AcquisitionOptions(interval=0), validator=valid, sleep=no_sleep
        )
        source = candidate(
            "openalex",
            "https://content.openalex.org/works/W4388922797.pdf",
            license="cc-by",
            oa=True,
            direct=True,
        )
        target = tmp_path / "downloads/program/staging/result.pdf"
        result = await downloader.download(source, target)
        await downloader.close()
        assert target.read_bytes().startswith(b"%PDF-")
        assert all("test-content-key" not in str(item) for item in result.values())
        assert "test-content-key" not in source.model_dump_json()

    asyncio.run(run())
    assert requests[0].url.path == "/robots.txt"
    assert "api_key" not in requests[0].url.params
    assert requests[1].url.params["api_key"] == "test-content-key"
