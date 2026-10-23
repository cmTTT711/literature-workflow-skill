import asyncio
import json
from pathlib import Path

import pytest
from test_oa_acquisition import FakeDownloader, oa_paper, pdf_bytes

from paper_acquisition.acquisition.models import AcquisitionError
from paper_acquisition.acquisition.service import acquire, acquisition_audit, resolve_all
from paper_acquisition.acquisition.verifier import UnifiedPDFVerifier
from paper_acquisition.config import Settings
from paper_acquisition.library.collections import attach_collection, publish_collection, reuse_library
from paper_acquisition.library.store import Library, file_hash
from paper_acquisition.master import agent_task_target, audit_master, prepare_master
from paper_acquisition.models import SearchProfile
from paper_acquisition.storage import Catalog
from paper_acquisition.workflow import run_search


def seed(root, paper):
    catalog = Catalog(root)
    catalog.save_paper(paper)
    catalog.start_run(SearchProfile(topic="多铁材料磁电耦合", year_from=2023, year_to=2023))
    catalog.close()
    asyncio.run(prepare_master(root, online=False))
    return root


def test_library_doi_and_hash_deduplication_and_tamper(tmp_path):
    paper = oa_paper(year=2024)
    pdf_bytes(tmp_path)
    path = tmp_path / "fixture.pdf"
    with Library(tmp_path / "library") as library:
        result = library.verify(path, paper, UnifiedPDFVerifier())
        first = library.add(path, paper, result)
        assert library.add(path, paper, result)["absolute_path"] == first["absolute_path"]
        assert library.lookup("https://doi.org/10.1234/EXAMPLE")
        assert library.audit()["pdf_files"] == 1
        Path(first["absolute_path"]).write_bytes(b"changed")
        with pytest.raises(ValueError, match="MISSING_OR_CHANGED"):
            library.lookup(paper.doi)
        assert library.audit()["errors"]


def test_conflicting_article_doi_not_admitted(tmp_path):
    pdf_bytes(tmp_path, "DOI: 10.1234/unrelated")
    with Library(tmp_path / "library") as library:
        result = library.verify(tmp_path / "fixture.pdf", oa_paper(), UnifiedPDFVerifier())
        assert result.status == "VERIFY_FAILED"
        with pytest.raises(AcquisitionError, match="IDENTITY_NOT_CONFIRMED"):
            library.add(tmp_path / "fixture.pdf", oa_paper(), result)
        assert not list((tmp_path / "library").rglob("*.pdf"))


def test_import_agent_compact_and_rerun_links(tmp_path):
    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data" / "first", paper)
    body = pdf_bytes(tmp_path)
    catalog = Catalog(root)
    target = agent_task_target(catalog, paper, "elsevier")
    catalog.close()
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    # A byte-identical staging copy must disappear only after a durable shared copy exists.
    staging = root / "downloads/program/staging/copy.pdf"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(body)
    first = publish_collection(root, workspace=tmp_path, compact=True)
    assert first["available"] == 1 and first["agent_required"] == 0
    assert not target.exists() and not staging.exists()
    assert first["compaction"]["reclaimed_bytes"] == len(body) * 2
    second = publish_collection(root, workspace=tmp_path, compact=True)
    assert second["result_dir"] == first["result_dir"]
    assert second["available"] == 1 and second["compaction"]["actions"] == 0
    destination = Path(first["result_dir"])
    assert not list(destination.rglob("*.html"))
    copies = list(destination.rglob("*.pdf"))
    assert len(copies) == 3
    assert len({p.stat().st_ino for p in copies}) == 3
    assert len({file_hash(p) for p in copies}) == 1
    assert not list((destination / "pdfs/automatic").iterdir())
    from paper_acquisition.toolbox.audit import audit_run

    assert audit_run(destination)["errors"] == []
    copies[0].write_bytes(b"changed")
    assert any("PDF_HASH_MISMATCH" in e for e in audit_run(destination)["errors"])
    assert not acquisition_audit(root)["errors"]


def test_cross_run_reuse_without_downloading(tmp_path):
    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data/first", paper)
    attach_collection(root, tmp_path, SearchProfile(topic="test", year_from=2023, year_to=2023))
    asyncio.run(resolve_all(root))
    downloader = FakeDownloader(pdf_bytes(tmp_path))
    asyncio.run(acquire(root, downloader=downloader))
    assert downloader.calls == 1
    assert not list((root / "downloads/program/staging").glob("*.pdf"))
    other_paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    other = seed(tmp_path / "data/second", other_paper)
    attach_collection(other, tmp_path, SearchProfile(topic="test", year_from=2023, year_to=2023))
    assert reuse_library(other) == 1
    asyncio.run(acquire(other, downloader=downloader))
    assert downloader.calls == 1
    assert publish_collection(other)["available"] == 1
    assert len(list((tmp_path / "workspace/library/pdf").glob("*.pdf"))) == 1
    assert len(list((tmp_path / "results").glob("*/run.json"))) == 2
    assert not acquisition_audit(other)["errors"]


def test_unverified_staging_archived_not_deleted(tmp_path):
    root = seed(tmp_path / "data/first", oa_paper(document_type="journal-article"))
    path = root / "downloads/program/staging/unknown.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a PDF")
    report = publish_collection(root, workspace=tmp_path, compact=True)
    assert report["available"] == 0
    assert not path.exists()
    assert (root / "history/unverified/program/staging/unknown.pdf").read_bytes() == b"not a PDF"


def test_run_twice_creates_independent_collections(tmp_path, monkeypatch):
    async def discovery(profile, settings):
        catalog = Catalog(settings.data_dir)
        catalog.start_run(profile)
        catalog.close()
        return {"status": "SUCCESS"}

    monkeypatch.setattr("paper_acquisition.workflow.discover", discovery)
    settings = Settings(offline=True)
    profile = SearchProfile(topic="test", year_from=2024, year_to=2026)
    first = asyncio.run(run_search(profile, settings, tmp_path))
    second = asyncio.run(run_search(profile, settings, tmp_path))
    assert first["result_dir"] != second["result_dir"]
    assert "test_2024-2026_" in first["result_dir"]
    for report in (first, second):
        saved = json.loads((Path(report["result_dir"]) / "run.json").read_text(encoding="utf-8"))
        assert saved["offline"] and saved["status"] == "COMPLETED"


def test_long_catalog_relative_library_paths_are_normalized(tmp_path):
    paper = oa_paper(
        title="A long title " * 16, year=2024, publisher_raw="Elsevier", document_type="journal-article"
    )
    root = seed(tmp_path / "data" / ("long_catalog_name_" * 4), paper)
    attach_collection(root, tmp_path, SearchProfile(topic="test"))
    pdf_bytes(tmp_path, "DOI: 10.1234/example")
    with Library(tmp_path / "workspace/library") as library:
        result = library.verify(tmp_path / "fixture.pdf", paper, UnifiedPDFVerifier())
        library.add(tmp_path / "fixture.pdf", paper, result)
    assert reuse_library(root) == 1
    report = publish_collection(root)
    assert report["available"] == 1 and report["program_pending"] == 0
    assert audit_master(root)["PROGRAM_VERIFIED"] == 1
    assert not acquisition_audit(root)["errors"]


def test_published_version_replaces_preprint_and_keeps_history(tmp_path):
    paper = oa_paper()
    path = tmp_path / "fixture.pdf"
    with Library(tmp_path / "library") as library:
        pdf_bytes(tmp_path)
        first = library.add(
            path, paper, library.verify(path, paper, UnifiedPDFVerifier()), version="submittedVersion"
        )
        pdf_bytes(tmp_path, "DOI: 10.1234/example Published journal article")
        result = library.verify(path, paper, UnifiedPDFVerifier())
        final = library.add(path, paper, result, version="publishedVersion")
        assert final["sha256"] != first["sha256"]
        assert Path(first["absolute_path"]).is_file()
        assert library.lookup(paper.doi)["version"] == "publishedVersion"


def test_wrapped_doi_followed_by_next_line_is_matched(tmp_path):
    pdf_bytes(tmp_path, "https://doi.org/10.1234/example\nCopyright 2024")
    result = UnifiedPDFVerifier().verify(tmp_path / "fixture.pdf", oa_paper())
    assert result.status == "VERIFIED" and result.doi_match


def test_keyword_classification_reads_total_pdf_and_copies(tmp_path):
    from paper_acquisition.toolbox.classification import classify

    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data/first", paper)
    catalog = Catalog(root)
    target = agent_task_target(catalog, paper, "elsevier")
    catalog.close()
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf_bytes(tmp_path, "DOI: 10.1234/example magnetoelectric spectroscopy"))
    report = publish_collection(root, workspace=tmp_path)
    result = classify(
        report["result_dir"], by="keywords", name="methods", rules={"spectroscopy": ["spectroscopy"]}
    )
    assert result["counts"] == {"spectroscopy": 1}
    source = next((Path(report["result_dir"]) / "pdfs/all").glob("*.pdf"))
    output = next(Path(result["output"]).rglob("*.pdf"))
    assert source.stat().st_ino != output.stat().st_ino
    assert file_hash(source) == file_hash(output)
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SOURCE_PDF_CHANGED"):
        classify(report["result_dir"])


def test_resume_preserves_agent_channel_and_historical_classification(tmp_path):
    from paper_acquisition.toolbox.classification import classify

    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data/first", paper)
    catalog = Catalog(root)
    target = agent_task_target(catalog, paper, "elsevier")
    catalog.close()
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf_bytes(tmp_path))
    first = publish_collection(root, workspace=tmp_path)
    assert first["by_channel"] == {"agent": 1}
    assert reuse_library(root) == 0
    assert publish_collection(root)["by_channel"] == {"agent": 1}
    attach_collection(root, tmp_path, SearchProfile(topic="test"), new=True)
    second = publish_collection(root)
    assert first["result_dir"] != second["result_dir"]
    assert classify(first["result_dir"])["counts"] == {"elsevier": 1}


def test_reviewed_invalid_doi_is_excluded_even_when_cached(tmp_path):
    from paper_acquisition.library.store import atomic_json

    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data/first", paper)
    catalog = Catalog(root)
    target = agent_task_target(catalog, paper, "elsevier")
    catalog.close()
    target.parent.mkdir(parents=True)
    target.write_bytes(pdf_bytes(tmp_path))
    assert publish_collection(root, workspace=tmp_path)["available"] == 1
    atomic_json(
        root / "recovery.json",
        {
            "records": [
                {"doi": paper.doi, "channel": "agent", "sources": [], "excluded_reason": "WRONG_DOI_MAPPING"}
            ]
        },
    )
    report = publish_collection(root)
    assert report["available"] == 0
    assert not list((Path(report["result_dir"]) / "pdfs").rglob("*.pdf"))


def test_actual_automatic_download_supersedes_historical_agent_task(tmp_path):
    from paper_acquisition.library.store import atomic_json

    paper = oa_paper(year=2023, publisher_raw="Elsevier", document_type="journal-article")
    root = seed(tmp_path / "data/first", paper)
    attach_collection(root, tmp_path, SearchProfile(topic="test", year_from=2023, year_to=2023))
    atomic_json(
        root / "recovery.json",
        {"records": [{"doi": paper.doi, "channel": "agent", "sources": []}]},
    )
    asyncio.run(resolve_all(root))
    downloader = FakeDownloader(pdf_bytes(tmp_path))
    asyncio.run(acquire(root, downloader=downloader))
    report = publish_collection(root)
    assert downloader.calls == 1
    assert report["by_channel"] == {"program": 1}
    assert len(list((Path(report["result_dir"]) / "pdfs/automatic").glob("*.pdf"))) == 1
    assert not list((Path(report["result_dir"]) / "pdfs/agent").glob("*.pdf"))
