"""Whole-workflow recovery and compatibility tests, with no live network or user data."""

import asyncio
import csv
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from test_library import seed
from test_master import article, save
from test_oa_acquisition import FakeDownloader, pdf_bytes

from paper_acquisition.acquisition.metadata import MetadataClients
from paper_acquisition.acquisition.models import AcquisitionError
from paper_acquisition.acquisition.verifier import UnifiedPDFVerifier
from paper_acquisition.attachments.service import FIELDS, write_tasks
from paper_acquisition.config import Settings
from paper_acquisition.discovery import discover, rebuild
from paper_acquisition.library.collections import attach_collection, publish_collection
from paper_acquisition.library.store import Library, file_hash
from paper_acquisition.master import (
    AGENT_FIELDS,
    acquire_master,
    agent_task_target,
    audit_master,
    prepare_master,
)
from paper_acquisition.models import SearchProfile, now
from paper_acquisition.providers import registry
from paper_acquisition.storage import Catalog, write_new
from paper_acquisition.toolbox.audit import audit_run
from paper_acquisition.toolbox.classification import classify
from paper_acquisition.workflow import acquire_collection, discovery_checkpoint, resume_collection, run_search


@pytest.fixture(autouse=True)
def fast_requests(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("paper_acquisition.http.asyncio.sleep", no_sleep)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("PAPER_CONTACT_EMAIL", raising=False)


def metadata_factory(works, calls):
    def factory(source):
        def respond(request):
            calls.append(source)
            if source == "openalex":
                return httpx.Response(200, json={"meta": {"count": len(works)}, "results": works})
            if source == "openaire":
                return httpx.Response(200, json={"header": {"numFound": 0}, "results": []})
            raise AssertionError(source)

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    return factory


@pytest.mark.parametrize("success", [True, False])
def test_one_automatic_command_tries_all_locations_then_exports(tmp_path, success):
    root = tmp_path / "run"
    paper = article()
    seed(root, paper)
    attach_collection(root, tmp_path, SearchProfile(topic="test"))
    work = {
        "doi": paper.doi,
        "title": paper.title,
        # Deliberately non-OA metadata: these are the declared ordinary requests
        # previously available only through openalex-exhaust.
        "locations": [{"pdf_url": f"https://repository.example/{i}.pdf"} for i in range(6)],
    }
    good = pdf_bytes(tmp_path, "DOI: " + paper.doi)

    class Downloader:
        def __init__(self):
            self.trace, self.calls = [], []

        async def download(self, candidate, target):
            self.calls.append(candidate.source_url)
            if not success or not candidate.source_url.endswith("/4.pdf"):
                raise AcquisitionError("HTTP_404")
            write_new(target, good)
            return {"trace": []}

        async def close(self):
            pass

    downloader, requests = Downloader(), []
    factory = metadata_factory([work], requests)
    report = asyncio.run(
        acquire_master(root, client_factory=factory, downloader=downloader, max_candidates=2)
    )
    assert len(downloader.calls) == (5 if success else 6)
    assert report["PROGRAM_VERIFIED"] == int(success)
    assert report["AGENT_REQUIRED"] == int(not success)
    assert report["agent_exported"] and report["pending_acquisition"] == 0
    with (root / "agent_tasks/agent_required.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == AGENT_FIELDS
        tasks = list(reader)
    if not success:
        assert Path(tasks[0]["target_path"]).is_relative_to(root / "downloads/agent/elsevier")
    before = (len(downloader.calls), len(requests))
    asyncio.run(acquire_master(root, client_factory=factory, downloader=downloader))
    assert before == (len(downloader.calls), len(requests))
    published = publish_collection(root)
    assert published["available"] == int(success)
    assert not audit_run(published["result_dir"])["errors"]


def test_openaire_remains_a_fallback_when_other_candidates_exist(tmp_path):
    paper = article()
    root = seed(tmp_path / "run", paper)
    good = pdf_bytes(tmp_path, "DOI: " + paper.doi)
    calls = []

    def factory(source):
        if source == "openalex":
            body = {
                "results": [
                    {
                        "doi": paper.doi,
                        "locations": [
                            {
                                "pdf_url": "https://repo.example/broken.pdf",
                                "is_oa": True,
                                "source": {"type": "repository"},
                            }
                        ],
                    }
                ]
            }
        else:
            assert source == "openaire"
            body = {
                "header": {"numFound": 1},
                "results": [
                    {
                        "mainTitle": paper.title,
                        "pids": [{"scheme": "doi", "value": paper.doi}],
                        "instances": [
                            {
                                "urls": ["https://repo.example/good.pdf"],
                                "accessRight": {"code": "OPEN", "openAccessRoute": "green"},
                            }
                        ],
                    }
                ],
            }
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))

    class Downloader:
        def __init__(self):
            self.trace = []

        async def download(self, candidate, target):
            calls.append(candidate.source_url)
            if candidate.source_url.endswith("broken.pdf"):
                raise AcquisitionError("HTTP_404")
            write_new(target, good)
            return {"trace": []}

        async def close(self):
            pass

    report = asyncio.run(acquire_master(root, client_factory=factory, downloader=Downloader()))
    assert report["PROGRAM_VERIFIED"] == 1
    assert calls == ["https://repo.example/broken.pdf", "https://repo.example/good.pdf"]


def test_partial_master_is_completed_before_acquisition(tmp_path):
    seed(tmp_path, article())
    save(tmp_path, article(doi="10.1234/second"))
    assert audit_master(tmp_path)["undecided_papers"] == 1
    with pytest.raises(ValueError, match="MASTER_INCOMPLETE"):
        publish_collection(tmp_path, workspace=tmp_path / "workspace-root")
    report = asyncio.run(acquire_master(tmp_path, client_factory=metadata_factory([], [])))
    assert report["total_master"] == 2
    assert audit_master(tmp_path)["undecided_papers"] == 0


def test_crossref_transient_failure_can_recover(tmp_path):
    save(tmp_path, article(publisher_raw=None, document_type=None))
    statuses = iter([503, 200])
    calls = []

    def factory(_):
        def respond(request):
            status = next(statuses)
            calls.append(status)
            return httpx.Response(
                status,
                json={"message": {"DOI": "10.1234/a", "publisher": "Elsevier", "type": "journal-article"}},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    for _ in range(3):
        asyncio.run(prepare_master(tmp_path, client_factory=factory))
    assert calls == [503, 200]
    c = Catalog(tmp_path)
    assert c.papers()[0].publisher_raw == "Elsevier"
    c.close()


def test_doi_case_and_individual_unpaywall_failure_do_not_drop_later_papers(tmp_path, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "review@example.org")
    papers = [article(doi="10.1234/A"), article(doi="10.1234/B")]

    def factory(source):
        def respond(request):
            if source == "unpaywall":
                if request.url.path.endswith("/a"):
                    return httpx.Response(404)
                return httpx.Response(200, json={"doi": "10.1234/b"})
            if source == "openaire":
                return httpx.Response(200, json={"header": {"numFound": 0}, "results": []})
            return httpx.Response(200, json={"results": []})

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def run():
        catalog = Catalog(tmp_path)
        clients = MetadataClients(catalog, Settings(data_dir=tmp_path), factory)
        try:
            extra = await clients.enrich(papers, True)
            assert [entry[0] for entry in extra[papers[1].paper_id]] == ["unpaywall"]
            assert len(clients.errors) == 1
        finally:
            await clients.close()
            catalog.close()

    asyncio.run(run())


def test_rebuild_keeps_original_query_screening(tmp_path):
    records = [
        {"doi": "10.1234/keep", "title": "multiferroic magnetoelectric coupling"},
        {"doi": "10.1234/drop", "title": "unrelated material"},
    ]
    profile = SearchProfile(
        topic="multiferroic",
        include_terms=["magnetoelectric coupling"],
        extensions={"require_terms_in_title_or_abstract": True},
    )
    settings = Settings(data_dir=tmp_path, providers=["openalex"])
    report = asyncio.run(discover(profile, settings, client_factory=metadata_factory(records, [])))
    assert report["master_papers"] == 1
    output = rebuild(settings)
    rows = [json.loads(line) for line in (output / "master_papers.jsonl").read_text().splitlines()]
    assert [row["doi"] for row in rows] == ["10.1234/keep"]


def test_resume_retries_only_failed_providers_with_original_settings(tmp_path, monkeypatch):
    profile = SearchProfile(topic="test", max_results_per_provider=7)
    root = tmp_path / "catalog"
    c = Catalog(root)
    run_id = c.start_run(profile)
    for name, status in [("openalex", "SUCCESS"), ("lens", "FAILED")]:
        qid = c.query(run_id, name, "test")
        c.execute("UPDATE provider_queries SET status=?,finished_at=? WHERE query_id=?", (status, now(), qid))
    c.execute("UPDATE search_runs SET status='PARTIAL',finished_at=? WHERE run_id=?", (now(), run_id))
    c.close()
    destination = attach_collection(root, tmp_path, profile)
    discovery = AsyncMock()
    monkeypatch.setattr("paper_acquisition.workflow.discover", discovery)
    monkeypatch.setattr("paper_acquisition.workflow.prepare_master", AsyncMock())
    monkeypatch.setattr("paper_acquisition.workflow.acquire_collection", AsyncMock(return_value={}))
    report = asyncio.run(resume_collection(destination, Settings(providers=["openalex", "lens"])))
    assert discovery.await_count == 1
    resumed_profile, settings = discovery.await_args.args
    assert settings.providers == ["lens"] and settings.force_refresh
    assert resumed_profile.max_results_per_provider == 7
    assert report["status"] == "PARTIAL"


def test_intentional_discovery_budget_does_not_trigger_resume(tmp_path):
    c = Catalog(tmp_path)
    run_id = c.start_run(SearchProfile(topic="test"))
    qid = c.query(run_id, "openalex", "test")
    c.execute("UPDATE provider_queries SET status='TRUNCATED',warnings_json='[]' WHERE query_id=?", (qid,))
    c.execute("UPDATE search_runs SET finished_at=? WHERE run_id=?", (now(), run_id))
    assert not discovery_checkpoint(c, ["openalex"])["pending_providers"]
    c.close()


def test_acquire_reuses_existing_result_and_agent_target(tmp_path, monkeypatch):
    root = seed(tmp_path / "catalog", article())
    destination = attach_collection(root, tmp_path, SearchProfile(topic="test"))
    c = Catalog(root)
    before = agent_task_target(c, c.papers()[0], "elsevier")
    c.close()
    monkeypatch.setattr("paper_acquisition.workflow.acquire_master", AsyncMock(return_value={}))
    report = asyncio.run(acquire_collection(root, tmp_path))
    assert Path(report["result_dir"]) == destination
    c = Catalog(root)
    assert agent_task_target(c, c.papers()[0], "elsevier") == before
    c.close()


def test_custom_classification_tracks_republished_version(tmp_path):
    paper = article()
    root = seed(tmp_path / "catalog", paper)
    c = Catalog(root)
    target = agent_task_target(c, paper, "elsevier")
    c.close()
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf_bytes(tmp_path, "DOI: 10.1234/a spectroscopy"))
    report = publish_collection(root, workspace=tmp_path)
    scheme = classify(
        report["result_dir"], by="keywords", name="methods", rules={"spectroscopy": ["spectroscopy"]}
    )
    original = file_hash(next(Path(scheme["output"]).rglob("*.pdf")))
    pdf_bytes(tmp_path, "DOI: 10.1234/a spectroscopy published final version")
    with Library(tmp_path / "workspace/library") as library:
        result = library.verify(tmp_path / "fixture.pdf", paper, UnifiedPDFVerifier())
        library.add(tmp_path / "fixture.pdf", paper, result, version="publishedVersion")
    publish_collection(root)
    assert file_hash(next(Path(scheme["output"]).rglob("*.pdf"))) != original
    assert not audit_run(report["result_dir"])["errors"]


def test_strict_capabilities_accept_only_implemented_filters():
    profile = SearchProfile(
        topic="test",
        language="en",
        document_types=["journal article"],
        extensions={"require_terms_in_title_or_abstract": True},
        strict_capabilities=True,
    )
    for name in ("openalex", "scopus"):
        assert registry()[name](None).warnings(profile) == []


def test_attachment_csv_escapes_text_but_json_preserves_it(tmp_path):
    row = dict.fromkeys(FIELDS, "")
    row.update(doi="10.1234/a", title="=1+1", target_dir=str(tmp_path))
    write_tasks(tmp_path, [row])
    with (tmp_path / "attachment_required.csv").open(encoding="utf-8-sig", newline="") as f:
        output = next(csv.DictReader(f))
    assert output["title"] == "'=1+1"
    assert output["doi"] == row["doi"] and output["target_dir"] == row["target_dir"]
    assert json.loads((tmp_path / "attachment_required.jsonl").read_text())["title"] == "=1+1"


def test_run_and_resume_share_the_complete_workflow(tmp_path, monkeypatch):
    work = {
        "doi": "10.1234/a",
        "title": "Quantum magnetic materials",
        "type": "article",
        "publication_year": 2023,
        "primary_location": {"source": {"host_organization_name": "Elsevier"}},
        "locations": [{"pdf_url": "https://repo.example/final.pdf"}],
    }
    requests = []
    factory = metadata_factory([work], requests)
    downloader = FakeDownloader(pdf_bytes(tmp_path, "DOI: 10.1234/a"))

    async def discovery(profile, settings):
        return await discover(profile, settings, client_factory=factory)

    async def acquisition(root, **kwargs):
        return await acquire_master(root, client_factory=factory, downloader=downloader, **kwargs)

    monkeypatch.setattr("paper_acquisition.workflow.discover", discovery)
    monkeypatch.setattr("paper_acquisition.workflow.acquire_master", acquisition)
    report = asyncio.run(
        run_search(
            SearchProfile(topic="Quantum magnetic materials"),
            Settings(providers=["openalex"]),
            tmp_path,
            label="stable-flow",
        )
    )
    assert report["status"] == "COMPLETED" and report["available"] == 1, report
    assert report["agent_required"] == 0 and report["by_channel"] == {"program": 1}
    before = len(requests)
    resumed = asyncio.run(resume_collection(Path(report["result_dir"]), Settings()))
    assert resumed["result_dir"] == report["result_dir"]
    assert resumed["status"] == "COMPLETED" and downloader.calls == 1
    assert len(requests) == before
    assert not audit_run(resumed["result_dir"])["errors"]


def test_interruption_between_search_variants_is_resumable(tmp_path):
    c = Catalog(tmp_path)
    run_id = c.start_run(SearchProfile(topic="test", queries=["first", "second"]))
    qid = c.query(run_id, "openalex", "first")
    c.execute("UPDATE provider_queries SET status='SUCCESS',finished_at=? WHERE query_id=?", (now(), qid))
    assert discovery_checkpoint(c, ["openalex"])["pending_providers"] == ["openalex"]
    c.close()
