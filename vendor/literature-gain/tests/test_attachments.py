import asyncio
import json
from pathlib import Path

import pytest
from test_library import seed
from test_oa_acquisition import oa_paper, pdf_bytes

from paper_acquisition.attachments.discovery import extract_links
from paper_acquisition.attachments.service import acquire, audit, local, mark
from paper_acquisition.attachments.store import Store, validate
from paper_acquisition.library.collections import publish_collection
from paper_acquisition.master import AGENT_FIELDS, agent_task_target
from paper_acquisition.storage import Catalog


class Downloader:
    def __init__(self, body, *, fail=False):
        self.body, self.fail, self.calls, self.trace = body, fail, [], []

    async def fetch(self, url):
        self.calls.append(url)
        if url.endswith("supplement.pdf"):
            if self.fail:
                raise ValueError("HTTP_403")
            return self.body, url, 200
        return (
            (
                b'<html><a href="/paper.pdf">Article PDF</a>'
                b'<h2>Supporting information</h2><a href="/supplement.pdf">File 1</a>'
                b'<h2>References</h2><a href="/reference.pdf">Reference</a></html>'
            ),
            "https://publisher.example/article",
            200,
        )


def test_extract_only_explicit_attachments():
    html = b"""<html><a href="article.pdf">Download PDF</a>
    <h2>Supplementary information</h2><a href="files/data.zip">Data</a>
    <h2>References</h2><a href="other.pdf">Paper</a>
    <div class="supplementary-material"><a href="s1.xlsx">Table</a></div>
    <a href="#supplement">Supplement</a><a href="javascript:alert(1)">Supplement</a></html>"""
    links = extract_links(html, "https://example.org/paper/")
    assert {r["url"] for r in links} == {
        "https://example.org/paper/files/data.zip",
        "https://example.org/paper/s1.xlsx",
    }


def test_supplement_heading_cannot_leak_into_site_footer():
    html = b'''<html><body><section><h2>Supplementary Files</h2>
    <a href="files/table.docx">Table</a></section><div><a href="paper.pdf">Download PDF</a></div>
    <footer><a href="/rss.xml">RSS feed</a><a href="/privacy">Privacy</a></footer></body></html>'''
    links = extract_links(html, "https://example.org/article")
    assert [r["url"] for r in links] == ["https://example.org/files/table.docx"]
    with pytest.raises(ValueError, match="SITE_FEED_NOT_ATTACHMENT"):
        validate(b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>', "rss.xml")


def test_researchsquare_nested_heading_keeps_only_current_article_assets():
    html = b'''<html><div><button><div><h2>Supplementary Files</h2></div></button>
    <a href="https://assets-eu.researchsquare.com/files/rs-123/v1/table.docx">Table</a></div>
    <a href="https://assets-eu.researchsquare.com/files/rs-999/v1/other.pdf">Other</a>
    <a href="https://assets-eu.researchsquare.com/files/rs-123/v2/revised.pdf">Version 2</a>
    <a href="/article/rs-123/v1.pdf">Download PDF</a><footer><a href="/rss.xml">RSS</a></footer></html>'''
    links = extract_links(html, "https://www.researchsquare.com/article/rs-123/v1")
    assert [r["url"] for r in links] == ["https://assets-eu.researchsquare.com/files/rs-123/v1/table.docx"]


def test_locked_csv_keeps_old_copy_and_exports_an_updated_copy(tmp_path, monkeypatch):
    from paper_acquisition.attachments.service import write_tasks

    path = tmp_path / "attachment_required.csv"
    path.write_text("old opened copy", encoding="utf-8")
    original = Path.replace

    def locked(self, target):
        if Path(target) == path:
            raise PermissionError("opened by spreadsheet application")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    warnings = write_tasks(tmp_path, [])
    assert path.read_text(encoding="utf-8") == "old opened copy"
    assert len(warnings) == 1
    assert Path(warnings[0]["updated_path"]).is_file()
    assert (tmp_path / "attachment_required.jsonl").is_file()


def test_auto_agent_union_resume_and_pdf_isolation(tmp_path):
    paper = oa_paper(document_type="journal-article")
    root = seed(tmp_path / "data", paper)
    body = pdf_bytes(tmp_path)
    catalog = Catalog(root)
    target = agent_task_target(catalog, paper, "other")
    catalog.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    initial = publish_collection(root, workspace=tmp_path)
    destination = Path(initial["result_dir"])
    original = {str(p): p.read_bytes() for p in (destination / "pdfs").rglob("*.pdf")}
    supplement = pdf_bytes(tmp_path, "Supplementary data for material measurements")
    downloader = Downloader(supplement)
    first = asyncio.run(acquire(root, downloader=downloader))
    assert first["files"] == 1 and first["agent_required"] == 0
    calls = len(downloader.calls)
    assert asyncio.run(acquire(root, downloader=downloader))["processed"] == 0
    assert len(downloader.calls) == calls
    with Store(root) as store:
        inbox = store.agent_dir(paper.doi)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "same.pdf").write_bytes(supplement)
    (inbox / "data.csv").write_text("x,y\n1,2\n")
    report = publish_collection(root, workspace=tmp_path, compact=True)
    assert report["available"] == initial["available"] == 1
    assert report["attachments"]["files"] == 2
    assert report["attachments"]["by_channel"] == {"automatic": 1, "agent": 1}
    assert (inbox / "same.pdf").exists()  # PDF compaction never consumes attachment inputs.
    assert all(Path(p).read_bytes() == b for p, b in original.items())
    manifest = json.loads((destination / "attachment/manifest.json").read_text())
    pdf = next(r for r in manifest["files"] if r["channel"] == "automatic")
    assert Path(pdf["path"]).parent.name.startswith(f"{paper.year or 'unknown_year'}_Magnetoelectric")
    assert not Path(pdf["path"]).parent.name.startswith("doi_")
    assert {s["channel"] for s in pdf["sources"]} == {"automatic", "agent"}
    assert not audit(root)["errors"]
    assert AGENT_FIELDS == ["doi", "title", "year", "publisher", "target_path"]
    with Store(root) as store, pytest.raises(ValueError, match="ARTICLE_PDF_IS_NOT_ATTACHMENT"):
        store.put(paper.doi, body, "article.pdf", "agent", {})
    from paper_acquisition.master import export_agent

    assert export_agent(root)["attachments"]["files"] == 2
    assert mark(root, paper.doi, "reopen")["agent_required"] == 1
    assert mark(root, paper.doi, "complete")["agent_required"] == 0
    again = local(root)
    assert again["files"] == 2
    assert not audit(root)["errors"]
    (destination / "attachment" / pdf["path"]).write_bytes(b"changed")
    assert any("CHANGED" in e for e in audit(root)["errors"])


def test_failed_download_handoff_retry_and_no_pdf_state_change(tmp_path):
    paper = oa_paper(document_type="journal-article")
    root = seed(tmp_path / "data", paper)
    with CatalogConnection(root) as c:
        before = c.execute("SELECT * FROM master_records").fetchall()
    downloader = Downloader(pdf_bytes(tmp_path), fail=True)
    report = asyncio.run(acquire(root, downloader=downloader))
    assert report["agent_required"] == 1
    tasks = [
        json.loads(line) for line in (root / "agent_tasks/attachment_required.jsonl").read_text().splitlines()
    ]
    assert tasks[0]["attachment_urls"] == ["https://publisher.example/supplement.pdf"]
    assert Path(tasks[0]["target_dir"]).is_dir()
    calls = len(downloader.calls)
    asyncio.run(acquire(root, downloader=downloader))
    assert len(downloader.calls) == calls
    downloader.fail = False
    assert asyncio.run(acquire(root, downloader=downloader, retry_failed=True))["agent_required"] == 0
    with CatalogConnection(root) as c:
        assert c.execute("SELECT * FROM master_records").fetchall() == before


def CatalogConnection(root):
    import sqlite3

    return sqlite3.connect(root / "catalog.sqlite")


def test_manual_confirmation_and_invalid_html(tmp_path):
    paper = oa_paper(document_type="journal-article")
    root = seed(tmp_path / "data", paper)
    assert local(root)["agent_required"] == 1
    assert mark(root, paper.doi, "none")["agent_required"] == 0
    assert mark(root, paper.doi, "reopen")["agent_required"] == 1
    with pytest.raises(ValueError, match="NO_IMPORTED"):
        mark(root, paper.doi, "complete")
    with Store(root) as store:
        inbox = store.agent_dir(paper.doi)
    (inbox / "fake.pdf").write_text("<html>Sign in</html>")
    assert local(root)["import_errors"]
    (inbox / "fake.pdf").unlink()
    (inbox / "data.csv").write_text("x,y\n1,2\n")
    assert local(root)["agent_required"] == 1  # A single file is not proof of completeness.
    assert mark(root, paper.doi, "complete")["agent_required"] == 0
    with pytest.raises(ValueError, match="HTML_RESPONSE"):
        validate(b"<!DOCTYPE html><html>Forbidden</html>", "table.csv")


def test_broken_attachment_store_does_not_fail_pdf_publish(tmp_path):
    root = seed(tmp_path / "data", oa_paper(document_type="journal-article"))
    publish_collection(root, workspace=tmp_path)
    local(root)
    (root / "attachments/state.sqlite").write_bytes(b"broken database")
    report = publish_collection(root, workspace=tmp_path)
    assert report["status"] == "PUBLISHED"
    assert report["attachments"]["status"] == "ATTACHMENT_ERROR"
