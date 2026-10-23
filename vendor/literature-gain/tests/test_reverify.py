import asyncio

from pypdf import PdfReader, PdfWriter
from test_master import prepare_master, save
from test_oa_acquisition import FakeDownloader, oa_paper, pdf_bytes

from paper_acquisition.acquisition.reverify import reverify
from paper_acquisition.acquisition.service import acquire, resolve_all
from paper_acquisition.acquisition.verifier import UnifiedPDFVerifier


def test_title_and_missing_doi_are_not_rejection_reasons(tmp_path):
    pdf_bytes(tmp_path, "Readable manuscript with no article identifiers")
    result = UnifiedPDFVerifier().verify(tmp_path / "fixture.pdf", oa_paper())
    assert result.status == "VERIFIED" and not result.doi_match and not result.title_match


def test_cover_erratum_and_whitespace_doi(tmp_path):
    writer = PdfWriter()
    for text in ["Publisher cover", "Erratum DOI: 10.1234/erratum", "DOI: 10.1234 / example"]:
        pdf_bytes(tmp_path, text)
        writer.add_page(PdfReader(tmp_path / "fixture.pdf").pages[0])
    writer.write(tmp_path / "combined.pdf")
    result = UnifiedPDFVerifier().verify(tmp_path / "combined.pdf", oa_paper())
    assert result.status == "VERIFIED" and result.doi_match


def test_offline_reverify_preserves_staging_and_rerun_zero(tmp_path):
    paper = oa_paper(publisher_raw="Elsevier", document_type="journal-article")
    save(tmp_path, paper)
    asyncio.run(prepare_master(tmp_path, online=False))
    asyncio.run(resolve_all(tmp_path))

    class OldVerifier:
        def verify(self, path, paper):
            result = UnifiedPDFVerifier().verify(path, paper)
            result.status = "VERIFY_FAILED"
            result.verifier_version = "1"
            result.reasons = ["TITLE_MISMATCH_OR_NO_TEXT"]
            return result

    asyncio.run(acquire(tmp_path, downloader=FakeDownloader(pdf_bytes(tmp_path)), verifier=OldVerifier()))
    files = list((tmp_path / "downloads/program/staging").glob("*"))
    report = reverify(tmp_path)
    assert report["after"]["PROGRAM_VERIFIED"] == 1 and report["network_requests"] == 0
    assert list((tmp_path / "downloads/program/staging").glob("*")) == files
    assert not reverify(tmp_path)["outcomes"]
    assert not (tmp_path / "agent_tasks/agent_required.jsonl").read_text()
