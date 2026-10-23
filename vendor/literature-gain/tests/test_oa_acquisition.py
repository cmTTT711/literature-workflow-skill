import asyncio
import hashlib
import subprocess

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from paper_acquisition.acquisition.models import AcquisitionError, AcquisitionOptions
from paper_acquisition.acquisition.network import PublicDownloader, public_url, retry_seconds
from paper_acquisition.acquisition.resolver import resolve_metadata
from paper_acquisition.acquisition.service import acquire, acquisition_audit, resolve_all
from paper_acquisition.acquisition.verifier import UnifiedPDFVerifier
from paper_acquisition.models import Paper
from paper_acquisition.storage import Catalog, write_new

TITLE = "Magnetoelectric coupling in layered quantum materials"


def pdf_bytes(tmp_path, text=TITLE + " DOI: 10.1234/example"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(
        ("BT /F1 10 Tf 20 700 Td (" + text.replace("(", "\\(").replace(")", "\\)") + ") Tj ET").encode()
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "fixture.pdf"
    writer.write(path)
    return path.read_bytes()


def oa_paper(**kwargs):
    values = {
        "title": TITLE,
        "doi": "10.1234/example",
        "oa_metadata": [
            {
                "provider": "openalex",
                "locations": [
                    {
                        "pdf_url": "https://publisher.example/paper.pdf",
                        "is_oa": True,
                        "license": "cc-by",
                        "source": {"type": "journal"},
                    }
                ],
            }
        ],
    }
    values.update(kwargs)
    return Paper(**values)


def test_policy_priority_and_unknown_pdf():
    paper = oa_paper()
    paper.oa_metadata.extend(
        [
            {"provider": "semantic_scholar", "openAccessPdf": {"url": "https://unknown.example/x.pdf"}},
            {
                "provider": "openalex",
                "locations": [
                    {"pdf_url": "https://repo.example/x.pdf", "is_oa": True, "source": {"type": "repository"}}
                ],
            },
        ]
    )
    paper.external_ids["arxiv"] = ["2401.12345"]
    candidates = resolve_metadata(paper)
    assert [c.source_type for c in candidates] == ["publisher_oa", "openalex", "arxiv", "semantic_scholar"]
    assert [c.eligible for c in candidates] == [True, True, True, False]
    assert all(c.reason and c.resolver_provenance and c.candidate_id for c in candidates)


def test_publisher_bronze_and_closed_copy_rejected():
    paper = oa_paper(
        oa_metadata=[
            {
                "provider": "openalex",
                "locations": [
                    {"pdf_url": "https://publisher.example/a", "is_oa": True, "source": {"type": "journal"}},
                    {"pdf_url": "https://publisher.example/b", "is_oa": False, "license": "cc-by"},
                ],
            }
        ]
    )
    assert not any(c.eligible for c in resolve_metadata(paper))


def test_unpaywall_open_and_openaire_coar():
    paper = oa_paper(oa_metadata=[])
    extras = [
        (
            "unpaywall",
            {
                "is_oa": True,
                "oa_locations": [
                    {
                        "url_for_pdf": "https://repository.example/a",
                        "host_type": "repository",
                        "version": "acceptedVersion",
                    }
                ],
            },
            {"evidence_id": "123"},
        ),
        (
            "openaire",
            {
                "instances": [
                    {
                        "accessRight": {"code": "c_abf2", "openAccessRoute": "GREEN"},
                        "urls": ["https://repo.example/b"],
                    }
                ]
            },
            {},
        ),
    ]
    candidates = resolve_metadata(paper, extras)
    assert len(candidates) == 2 and all(c.eligible for c in candidates)
    assert candidates[0].version == "acceptedVersion"


def test_candidate_ids_stable_on_rerun():
    paper = oa_paper()
    assert resolve_metadata(paper)[0].candidate_id == resolve_metadata(paper)[0].candidate_id


def test_funder_doi_is_not_an_article_doi():
    from paper_acquisition.providers.openaire import Adapter

    paper = Adapter(None).normalize(
        {"mainTitle": TITLE, "instances": [{"urls": ["https://doi.org/10.13039/501100011033"]}]}
    )
    assert paper.doi is None


def test_core_metadata_requires_explicit_open_evidence():
    paper = oa_paper(oa_metadata=[{"provider": "core", "downloadUrl": "https://repository.example/file.pdf"}])
    assert not resolve_metadata(paper)[0].eligible
    paper.oa_metadata[0].update(isOpenAccess=True, license="cc-by", version="accepted")
    assert resolve_metadata(paper)[0].eligible


def test_crossref_embargo_and_version_must_match():
    paper = oa_paper(oa_metadata=[])
    metadata = {
        "license": [
            {
                "URL": "https://creativecommons.org/licenses/by/4.0/",
                "content-version": "am",
                "start": {"timestamp": 0},
            }
        ],
        "link": [
            {
                "URL": "https://publisher.example/a.pdf",
                "content-type": "application/pdf",
                "content-version": "vor",
            }
        ],
    }
    assert not resolve_metadata(paper, [("crossref", metadata, {})])[0].eligible
    metadata["license"][0]["content-version"] = "vor"
    metadata["license"][0]["start"]["timestamp"] = 99999999999999
    assert not resolve_metadata(paper, [("crossref", metadata, {})])[0].eligible
    metadata["license"][0]["start"]["timestamp"] = 0
    assert resolve_metadata(paper, [("crossref", metadata, {})])[0].eligible


async def valid(_):
    pass


async def no_sleep(_):
    pass


def downloader(tmp_path, handler, **kwargs):
    return PublicDownloader(
        tmp_path,
        AcquisitionOptions(interval=0, **kwargs),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        validator=valid,
        sleep=no_sleep,
    )


def test_download_retry_and_staging(tmp_path):
    calls = []
    body = pdf_bytes(tmp_path)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(503) if calls.count("/paper.pdf") == 1 else httpx.Response(200, content=body)

    async def run():
        d = downloader(tmp_path, handler)
        target = tmp_path / "downloads/program/staging/a.pdf"
        result = await d.download(resolve_metadata(oa_paper())[0], target)
        assert target.read_bytes() == body
        assert len(result["trace"]) == 3
        await d.close()

    asyncio.run(run())


def test_retry_after_defers_and_persists_cooldown(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return (
            httpx.Response(200, text="User-agent: *\nAllow: /")
            if request.url.path == "/robots.txt"
            else httpx.Response(429, headers={"Retry-After": "600"})
        )

    async def run():
        d = downloader(tmp_path, handler)
        c = resolve_metadata(oa_paper())[0]
        for expected in ["HTTP_429", "HOST_COOLDOWN"]:
            with pytest.raises(AcquisitionError, match=expected):
                await d.download(c, tmp_path / "downloads/program/staging/a.pdf")
        await d.close()

    asyncio.run(run())
    assert calls.count("/paper.pdf") == 1
    assert retry_seconds("600") == 600
    assert retry_seconds("not-a-date", 2) == 2


def test_robots_denial_prevents_pdf_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    async def run():
        d = downloader(tmp_path, handler)
        with pytest.raises(AcquisitionError, match="ROBOTS_DISALLOWED"):
            await d.download(resolve_metadata(oa_paper())[0], tmp_path / "downloads/program/staging/a.pdf")
        await d.close()

    asyncio.run(run())
    assert calls == ["/robots.txt"]


def test_redirect_is_revalidated(tmp_path):
    async def guard(url):
        if "127.0.0.1" in url:
            raise AcquisitionError("NONPUBLIC_ADDRESS")

    async def run():
        d = downloader(
            tmp_path,
            lambda r: (
                httpx.Response(200, text="User-agent: *\nAllow: /")
                if r.url.path == "/robots.txt"
                else httpx.Response(302, headers={"location": "http://127.0.0.1/a"})
            ),
        )
        d.validator = guard
        with pytest.raises(AcquisitionError, match="HTTPS_DOWNGRADE|NONPUBLIC_ADDRESS"):
            await d.download(resolve_metadata(oa_paper())[0], tmp_path / "downloads/program/staging/a.pdf")
        await d.close()

    asyncio.run(run())


def test_public_url_rejects_local_address():
    with pytest.raises(AcquisitionError, match="NONPUBLIC_ADDRESS"):
        asyncio.run(public_url("http://127.0.0.1/a.pdf"))


def test_synthetic_dns_is_exact_https_allowlist(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.58", 443))],
    )
    asyncio.run(public_url("https://arxiv.org/pdf/2401.12345"))
    asyncio.run(public_url("https://content.openalex.org/works/W4388922797.pdf"))
    for url in ["https://arxiv.org.evil.example/a", "http://arxiv.org/a", "https://unknown.example/a"]:
        with pytest.raises(AcquisitionError, match="NONPUBLIC_ADDRESS"):
            asyncio.run(public_url(url))
    with pytest.raises(AcquisitionError, match="AUTHENTICATION_ENDPOINT"):
        asyncio.run(public_url("https://idp.nature.com/login"))


def test_download_total_timeout(tmp_path):
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200)

    async def run():
        d = downloader(tmp_path, handler, total_download_timeout=0.01)
        with pytest.raises(AcquisitionError, match="TOTAL_DOWNLOAD_TIMEOUT"):
            await d.download(resolve_metadata(oa_paper())[0], tmp_path / "downloads/program/staging/a.pdf")
        await d.close()

    asyncio.run(run())


def test_starved_pool_is_recreated_with_bounded_retry(tmp_path):
    def starved(request):
        raise httpx.PoolTimeout("test pool unavailable", request=request)

    async def run():
        d = downloader(tmp_path, starved)

        def healthy(request):
            return httpx.Response(
                200,
                content=b"User-agent: *\nAllow: /\n"
                if request.url.path == "/robots.txt"
                else b"%PDF-1.7\n" + b"x" * 600,
            )

        d.new_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(healthy))
        target = tmp_path / "downloads/program/staging/recovered.pdf"
        result = await d.download(resolve_metadata(oa_paper())[0], target)
        assert target.read_bytes().startswith(b"%PDF-")
        assert sum(e.get("pool_recreated", False) for e in result["trace"]) == 1
        await d.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,ok,doi_match",
    [
        (TITLE + " DOI: 10.1234/example", True, True),
        (TITLE, True, False),
        ("A completely unrelated biological article DOI: 10.1234/example", True, True),
        (TITLE + " DOI: 10.1234/wrong", False, False),
    ],
)
def test_pdf_identity(tmp_path, text, ok, doi_match):
    pdf_bytes(tmp_path, text)
    result = UnifiedPDFVerifier().verify(tmp_path / "fixture.pdf", oa_paper())
    assert (result.status == "VERIFIED") is ok
    assert result.doi_match is doi_match
    assert result.page_count == 1
    assert result.sha256 == hashlib.sha256((tmp_path / "fixture.pdf").read_bytes()).hexdigest()


def test_html_masquerading_pdf(tmp_path):
    path = tmp_path / "login.pdf"
    path.write_bytes(b"<html>Access denied Login</html>" + b" " * 600)
    result = UnifiedPDFVerifier().verify(path, oa_paper())
    assert result.status == "VERIFY_FAILED" and "HTML_OR_NON_PDF" in result.reasons


def test_zero_pages_and_malformed_pdf(tmp_path):
    path = tmp_path / "empty.pdf"
    writer = PdfWriter()
    writer.add_metadata({"/Title": "padding" * 100})
    writer.write(path)
    assert "INVALID_PAGE_COUNT" in UnifiedPDFVerifier().verify(path, oa_paper()).reasons
    path.write_bytes(b"%PDF-1.7\n" + b"X" * 700)
    assert UnifiedPDFVerifier().verify(path, oa_paper()).status == "VERIFY_FAILED"


def test_parser_timeout_is_not_verified(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert UnifiedPDFVerifier().verify(tmp_path / "x.pdf", oa_paper()).reasons == ["PARSER_TIMEOUT"]


class FakeDownloader:
    def __init__(self, body):
        self.body, self.calls, self.trace = body, 0, []

    async def download(self, candidate, target):
        self.calls += 1
        write_new(target, self.body)
        return {"trace": [], "bytes": len(self.body)}

    async def close(self):
        pass


def test_resolution_acquisition_resume_duplicates_and_integrity(tmp_path):
    body = pdf_bytes(tmp_path)
    catalog = Catalog(tmp_path)
    for _ in range(2):
        catalog.save_paper(oa_paper())
    catalog.close()

    async def run():
        first = await resolve_all(tmp_path)
        second = await resolve_all(tmp_path)
        assert first["oa_resolved"] == 2 and second["processed"] == 0
        d = FakeDownloader(body)
        report = await acquire(tmp_path, limit=2, downloader=d)
        assert report["verified"] == 2 and d.calls == 1
        rerun = await acquire(tmp_path, limit=2, downloader=d)
        assert rerun["sample_papers"] == 0 and d.calls == 1

    asyncio.run(run())
    audit = acquisition_audit(tmp_path)
    assert audit["integrity"] == "ok" and not audit["errors"]
    catalog = Catalog(tmp_path)
    assert catalog.db.execute("SELECT count(DISTINCT path) FROM artifacts").fetchone()[0] == 1
    assert catalog.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 2
    catalog.close()


def test_resume_staging_never_downloads_again(tmp_path):
    body = pdf_bytes(tmp_path)
    paper = oa_paper()
    catalog = Catalog(tmp_path)
    catalog.save_paper(paper)
    catalog.close()
    asyncio.run(resolve_all(tmp_path))
    catalog = Catalog(tmp_path)
    cid = catalog.db.execute("SELECT candidate_id FROM access_candidates").fetchone()[0]
    path = "downloads/program/staging/interrupted.pdf"
    write_new(tmp_path / path, body)
    catalog.execute(
        "INSERT INTO acquisition_attempts VALUES(?,?,?,'today',NULL,'PROGRAM_DOWNLOADED_UNVERIFIED',?,'{}')",
        ("attempt", paper.paper_id, cid, path),
    )
    catalog.close()
    d = FakeDownloader(body)
    result = asyncio.run(acquire(tmp_path, downloader=d))
    assert result["verified"] == 1 and d.calls == 0


def test_verify_failure_queues_and_rerun_is_safe(tmp_path):
    c = Catalog(tmp_path)
    c.save_paper(oa_paper())
    c.close()
    asyncio.run(resolve_all(tmp_path))
    d = FakeDownloader(b"<html>login</html>" + b" " * 600)
    result = asyncio.run(acquire(tmp_path, downloader=d))
    assert result["institutional_required"] == 1 and result["verified"] == 0
    asyncio.run(acquire(tmp_path, downloader=d))
    assert d.calls == 1


def test_online_lookup_matches_exact_doi_and_archives(tmp_path, monkeypatch):
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("PAPER_CONTACT_EMAIL", raising=False)
    paper = oa_paper(oa_metadata=[])
    c = Catalog(tmp_path)
    c.save_paper(paper)
    c.close()

    def factory(source):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "meta": {"count": 1},
                        "results": [{"doi": paper.doi, "locations": oa_paper().oa_metadata[0]["locations"]}],
                    },
                )
            )
        )

    result = asyncio.run(resolve_all(tmp_path, online=True, client_factory=factory))
    assert result["oa_resolved"] == 1
    assert result["metadata_requests"]["openalex"]["network_requests"] == 1
    assert not acquisition_audit(tmp_path)["errors"]


def test_openaire_online_pid_batch_limit_and_identity(tmp_path, monkeypatch):
    import re

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("PAPER_CONTACT_EMAIL", raising=False)
    c = Catalog(tmp_path)
    for i in range(6):
        c.save_paper(oa_paper(doi=f"10.1234/{i}", oa_metadata=[]))
    c.close()
    batches = []

    def factory(source):
        def handler(request):
            if source == "openalex":
                return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
            query = request.url.params["pid"]
            dois = re.findall(r'"([^\"]+)"', query)
            batches.append(dois)
            return httpx.Response(
                200,
                json={
                    "header": {"numFound": len(dois)},
                    "results": [
                        {
                            "pids": [{"scheme": "doi", "value": doi}],
                            "mainTitle": TITLE,
                            "instances": [
                                {
                                    "accessRight": {"code": "c_abf2", "openAccessRoute": "green"},
                                    "urls": [f"https://repo.example/{doi}.pdf"],
                                }
                            ],
                        }
                        for doi in dois
                    ],
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(resolve_all(tmp_path, online=True, client_factory=factory))
    assert report["oa_resolved"] == 6
    assert list(map(len, batches)) == [5, 1]
    assert report["resolver_errors"] == []


def test_audit_detects_tampered_verified_pdf(tmp_path):
    body = pdf_bytes(tmp_path)
    c = Catalog(tmp_path)
    c.save_paper(oa_paper())
    c.close()
    asyncio.run(resolve_all(tmp_path))
    asyncio.run(acquire(tmp_path, downloader=FakeDownloader(body)))
    c = Catalog(tmp_path)
    path = c.db.execute("SELECT path FROM artifacts").fetchone()[0]
    c.close()
    (tmp_path / path).write_bytes(b"corrupt")
    assert acquisition_audit(tmp_path)["errors"][0]["code"] == "VERIFIED_ARTIFACT_INVALID"


def test_unpaywall_email_redaction_and_online_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "private-contact@example.org")
    monkeypatch.delenv("PAPER_CONTACT_EMAIL", raising=False)
    c = Catalog(tmp_path)
    c.save_paper(oa_paper(oa_metadata=[]))
    c.close()

    def factory(source):
        def handler(request):
            if source == "openalex":
                return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
            if source == "openaire":
                return httpx.Response(200, json={"header": {"numFound": 0}, "results": []})
            assert source == "unpaywall"
            assert request.url.params["email"] == "private-contact@example.org"
            return httpx.Response(
                200,
                json={
                    "doi": "10.1234/example",
                    "is_oa": True,
                    "echo": "private-contact@example.org",
                    "oa_locations": [
                        {
                            "url_for_pdf": "https://repo.example/a.pdf",
                            "host_type": "repository",
                            "version": "acceptedVersion",
                        }
                    ],
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    report = asyncio.run(resolve_all(tmp_path, online=True, client_factory=factory))
    assert report["oa_resolved"] == 1 and not report["resolver_errors"]
    for path in (tmp_path / "acquisition").rglob("*"):
        if path.is_file():
            assert b"private-contact@example.org" not in path.read_bytes()


def test_download_size_limit_is_bounded(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, content=b"%PDF-" + b"x" * 2048)

    async def run():
        d = downloader(tmp_path, handler, max_bytes=1024)
        with pytest.raises(AcquisitionError, match="RESPONSE_TOO_LARGE"):
            await d.download(resolve_metadata(oa_paper())[0], tmp_path / "downloads/program/staging/a.pdf")
        await d.close()

    asyncio.run(run())
