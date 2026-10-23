import asyncio
import csv
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_oa_acquisition import FakeDownloader, oa_paper, pdf_bytes

from paper_acquisition.acquisition.models import AcquisitionError
from paper_acquisition.acquisition.service import acquire, resolve_all
from paper_acquisition.master import (
    AGENT_FIELDS,
    MASTER_FIELDS,
    acquire_master,
    agent_path,
    enable_publisher_requests,
    export_agent,
    pending_acquisition,
    prepare_master,
    safe_doi_filename,
)
from paper_acquisition.models import Paper
from paper_acquisition.normalization import normalize_doi
from paper_acquisition.storage import Catalog


@pytest.fixture(autouse=True)
def isolate_openalex_preparation(monkeypatch):
    # Master unit tests use locally resolved candidates. Full automatic preparation
    # is exercised with real orchestration and mocked HTTP in test_workflow_regressions.
    monkeypatch.setattr("paper_acquisition.master.exhaust_openalex", AsyncMock(return_value={}))

    def factory(source):
        payload = {"results": [], "meta": {"count": 0}, "header": {"numFound": 0}}
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))

    async def resolution(root, **kwargs):
        kwargs["client_factory"] = kwargs.get("client_factory") or factory
        return await resolve_all(root, **kwargs)

    monkeypatch.setattr("paper_acquisition.master.resolve_all", resolution)


def save(root, *papers):
    catalog = Catalog(root)
    for paper in papers:
        catalog.save_paper(paper)
    catalog.close()


def article(**kwargs):
    return Paper(
        **{
            "doi": "10.1234/a",
            "title": "Quantum magnetic materials",
            "document_type": "journal-article",
            "publisher_raw": "Elsevier",
            **kwargs,
        }
    )


def test_doi_only_admission_review_and_exact_outputs(tmp_path):
    save(
        tmp_path,
        article(),
        article(doi="https://doi.org/10.1234/A", source_hits=["lens"]),
        article(doi=None),
        article(doi="10.13039/100000001"),
        article(doi="10.1234/dataset", document_type="dataset"),
        article(doi="10.1234/book", document_type="book-chapter"),
        article(doi="10.1234/unknown", publisher_raw=None),
        article(doi="10.1234/type", document_type="unknown"),
        article(doi="10.1234/conflict", flags=["IDENTIFIER_CONFLICT"]),
    )
    report = asyncio.run(prepare_master(tmp_path, online=False))
    assert report["total_master"] == 4 and report["REVIEW"] == 0
    with pytest.raises(ValueError, match="ACQUISITION_INCOMPLETE"):
        export_agent(tmp_path)
    asyncio.run(resolve_all(tmp_path))
    export_agent(tmp_path)
    with (tmp_path / "catalog/master_papers.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == MASTER_FIELDS
        rows = list(reader)
    assert len({row["doi"] for row in rows}) == 4
    assert json.loads(next(row["source_hits"] for row in rows if row["doi"] == "10.1234/a")) == ["lens"]
    with (tmp_path / "agent_tasks/agent_required.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == AGENT_FIELDS
        tasks = list(reader)
    assert {normalize_doi(task["doi"]) for task in tasks} == {
        "10.1234/a",
        "10.1234/unknown",
        "10.1234/type",
        "10.1234/conflict",
    }
    assert tasks[0]["publisher"] == "elsevier"
    assert len(list((tmp_path / "agent_tasks/by_publisher").glob("*.csv"))) == 14
    catalog = Catalog(tmp_path)
    assert catalog.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not catalog.db.execute("PRAGMA foreign_key_check").fetchall()
    catalog.close()


def test_agent_export_uses_original_doi_but_keeps_stable_target_key(tmp_path):
    paper = article(doi="10.1103/PhysRevB.110.L161408", publisher_raw="American Physical Society", year=2024)
    from paper_acquisition.normalization import finalize

    save(tmp_path, finalize(paper))
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))
    export_agent(tmp_path)
    with (tmp_path / "agent_tasks/by_publisher/aps.csv").open(encoding="utf-8-sig", newline="") as f:
        task = next(csv.DictReader(f))
    assert task["doi"] == "10.1103/PhysRevB.110.L161408"
    assert task["year"] == "2024"
    assert task["target_path"].endswith("2024_Quantum magnetic materials.pdf")


def test_crossref_completion_is_cached_and_raw_audited(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    save(tmp_path, article(publisher_raw=None, document_type=None))
    requests = []

    def factory(source):
        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "DOI": "10.1234/a",
                        "title": ["Quantum magnetic materials"],
                        "publisher": "Elsevier",
                        "type": "journal-article",
                        "container-title": ["Journal"],
                    }
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    first = asyncio.run(prepare_master(tmp_path, client_factory=factory))
    second = asyncio.run(prepare_master(tmp_path, client_factory=factory))
    assert first["REVIEW"] == second["REVIEW"] == 0
    assert len(requests) == 1
    c = Catalog(tmp_path)
    assert c.db.execute("SELECT count(*) FROM resolver_evidence").fetchone()[0] == 1
    assert c.papers()[0].journal == "Journal"
    c.close()


def test_optional_crossref_failure_stops_service_not_master(tmp_path):
    save(tmp_path, *(article(doi=f"10.1234/{i}", publisher_raw=None) for i in range(4)))
    requests = []

    def factory(source):
        def handle(request):
            requests.append(request)
            return httpx.Response(403)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    report = asyncio.run(prepare_master(tmp_path, client_factory=factory))
    assert len(requests) == 1
    assert report["crossref_unavailable"] and report["total_master"] == 4
    assert report["AGENT_REQUIRED"] == 4 and report["REVIEW"] == 0


def test_unknown_publisher_doi_is_still_acquired_automatically(tmp_path, monkeypatch):
    paper = oa_paper(publisher_raw=None, document_type="preprint")
    save(tmp_path, paper)
    initial = asyncio.run(prepare_master(tmp_path, online=False))
    assert initial["AGENT_REQUIRED"] == 1 and initial["REVIEW"] == 0
    asyncio.run(resolve_all(tmp_path, online=False))
    downloader = FakeDownloader(pdf_bytes(tmp_path))

    async def injected(root, **kwargs):
        return await acquire(root, downloader=downloader, **kwargs)

    monkeypatch.setattr("paper_acquisition.master.acquire", injected)
    monkeypatch.setattr(
        "paper_acquisition.master.verified_artifact", lambda _catalog, _paper_id: downloader.calls > 0
    )
    report = asyncio.run(acquire_master(tmp_path))
    assert downloader.calls == 1
    assert report["PROGRAM_VERIFIED"] == 1, report
    assert report["REVIEW"] == 0


def test_agent_paths_are_doi_unique_windows_safe(tmp_path):
    names = [safe_doi_filename(x) for x in ("10.1234/a:b", "10.1234/a_b", "10.1234/" + "x" * 250)]
    assert len(set(names)) == 3
    assert all(len(n) < 150 and not any(c in n for c in '<>:"/\\|?*') for n in names)
    assert safe_doi_filename("https://doi.org/10.1234/A") == safe_doi_filename("10.1234/a")
    assert agent_path(tmp_path, "10.1234/a", "aps").is_relative_to(tmp_path / "downloads/agent")
    with pytest.raises(ValueError):
        agent_path(tmp_path, "10.1234/a", "../program")


def test_declared_official_publisher_pdf_uses_normal_requests_only(tmp_path):
    paper = article(
        publisher_raw="American Physical Society",
        oa_metadata=[
            {
                "provider": "semantic_scholar",
                "openAccessPdf": {"url": "https://journals.aps.org/prb/pdf/10.1234/a"},
            },
            {"provider": "semantic_scholar", "openAccessPdf": {"url": "https://untrusted.example/a.pdf"}},
        ],
    )
    save(tmp_path, paper)
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))
    c = Catalog(tmp_path)
    enable_publisher_requests(c, c.papers()[0])
    candidates = [json.loads(row[0]) for row in c.db.execute("SELECT candidate_json FROM access_candidates")]
    assert sum(x["eligible"] for x in candidates) == 1
    official = next(x for x in candidates if x["eligible"])
    assert official["license"] is None and "not an OA" in official["reason"]
    c.close()


def test_full_acquire_exhausts_all_candidates_and_rerun_zero(tmp_path, monkeypatch):
    paper = oa_paper(publisher_raw="Elsevier", document_type="journal-article")
    paper.oa_metadata[0]["locations"] = [
        {"pdf_url": f"https://repository.example/{i}.pdf", "is_oa": True, "source": {"type": "repository"}}
        for i in range(5)
    ]
    paper.oa_metadata.append({"provider": "unpaywall", "locations": paper.oa_metadata[0]["locations"]})
    save(tmp_path, paper)
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))

    class FailingDownloader:
        def __init__(self):
            self.trace = []
            self.calls = 0

        async def download(self, candidate, staging):
            self.calls += 1
            raise AcquisitionError("HTTP_403")

        async def close(self):
            pass

    downloader = FailingDownloader()

    async def injected(root, **kwargs):
        return await acquire(root, downloader=downloader, **kwargs)

    monkeypatch.setattr("paper_acquisition.master.acquire", injected)
    report = asyncio.run(acquire_master(tmp_path, max_candidates=2))
    assert downloader.calls == 5 and report["AGENT_REQUIRED"] == 1
    asyncio.run(acquire_master(tmp_path, max_candidates=2))
    assert downloader.calls == 5
    c = Catalog(tmp_path)
    assert not pending_acquisition(c)
    c.close()


def test_verified_never_enters_agent_tasks_or_redownloads(tmp_path, monkeypatch):
    save(tmp_path, oa_paper(publisher_raw="Elsevier", document_type="journal-article"))
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))
    downloader = FakeDownloader(pdf_bytes(tmp_path))

    async def injected(root, **kwargs):
        return await acquire(root, downloader=downloader, **kwargs)

    monkeypatch.setattr("paper_acquisition.master.acquire", injected)
    report = asyncio.run(acquire_master(tmp_path))
    assert report["PROGRAM_VERIFIED"] == 1 and report["AGENT_REQUIRED"] == 0
    asyncio.run(acquire_master(tmp_path))
    assert downloader.calls == 1
    assert not (tmp_path / "agent_tasks/agent_required.jsonl").read_text()


def test_obsolete_dns_preflight_retried_once_not_forever(tmp_path):
    paper = oa_paper(
        publisher_raw="Elsevier",
        document_type="journal-article",
        oa_metadata=[],
        external_ids={"arxiv": ["2401.12345"]},
    )
    save(tmp_path, paper)
    asyncio.run(resolve_all(tmp_path))

    class FailingDownloader:
        def __init__(self):
            self.calls, self.trace = 0, []

        async def download(self, candidate, staging):
            self.calls += 1
            raise AcquisitionError("NONPUBLIC_ADDRESS")

        async def close(self):
            pass

    downloader = FailingDownloader()
    asyncio.run(acquire(tmp_path, downloader=downloader))
    c = Catalog(tmp_path)
    c.execute("UPDATE acquisition_attempts SET result_json=json_remove(result_json,'$.network_policy')")
    c.close()
    asyncio.run(acquire(tmp_path, downloader=downloader))
    asyncio.run(acquire(tmp_path, downloader=downloader))
    assert downloader.calls == 2
