"""Exercise both original engines with simulated remote transport, real files and catalogs."""
from pathlib import Path
import asyncio, csv, hashlib, json
import httpx, pytest
import pymupdf

from literature_workflow.config import Config, KEY_NAMES, guide
from literature_workflow.pipeline import new_job, execute
from literature_workflow.handoff import read_tasks, import_si, _signature
from paper_acquisition.models import SearchProfile
from paper_acquisition.acquisition.models import AcquisitionError

DOI = "10.1234/layered-test"
TITLE = "Magnetoelectric coupling in atomic layered materials"

def pdf(title):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), title + "\nDOI: " + DOI)
    body = doc.tobytes(); doc.close()
    return body

@pytest.fixture
def environment(tmp_path, tmp_path_factory, monkeypatch):
    for name in KEY_NAMES: monkeypatch.delenv(name, raising=False)
    cfg = Config(home=tmp_path / "settings", workspace=str(tmp_path_factory.mktemp("w") / "r"),
                 school="测试大学", email="researcher@school.edu", providers=["openalex"])
    cfg.save()
    return cfg

@pytest.fixture
def transports(monkeypatch):
    import paper_acquisition.workflow as native
    import paper_acquisition.attachments.service as attachments
    from paper_acquisition.discovery import discover
    from paper_acquisition.master import acquire_master
    from scanpdf.download import run as scan_run
    from scanpdf.attachments import save_attachment
    work = {"id": "https://openalex.org/W123456789", "doi": "https://doi.org/" + DOI,
            "title": TITLE, "publication_year": 2023, "type": "article",
            "primary_location": {"source": {"host_organization_name": "Wiley"}},
            "locations": [{"pdf_url": "https://publisher.example/main.pdf", "is_oa": True,
                           "license": "cc-by", "source": {"type": "repository"}}]}
    calls = {"main": 0, "si": 0, "auto": 0}
    def factory(source):
        def answer(request):
            if source == "openalex": return httpx.Response(200, json={"meta": {"count": 1}, "results": [work]})
            if source == "openaire": return httpx.Response(200, json={"header": {"numFound": 0}, "results": []})
            raise AssertionError(source)
        return httpx.AsyncClient(transport=httpx.MockTransport(answer))
    async def search(profile, settings):
        return await discover(profile, settings, client_factory=factory)
    class Automatic:
        trace = []
        async def download(self, candidate, target):
            calls["auto"] += 1
            raise AcquisitionError("HTTP_403")
        async def close(self): pass
    async def acquire(root, **options):
        return await acquire_master(root, client_factory=factory, downloader=Automatic(), **options)
    real_attachments = attachments.acquire
    class AttachmentHTTP:
        trace = []
        async def fetch(self, url):
            if url.endswith("si.pdf"): raise AcquisitionError("HTTP_403")
            return b'<html><h1>Research</h1><a href="https://publisher.example/si.pdf">Supporting information</a></html>', url, 200
    async def acquire_si(root, **options):
        return await real_attachments(root, downloader=AttachmentHTTP(), **options)
    monkeypatch.setattr(native, "discover", search)
    monkeypatch.setattr(native, "acquire_master", acquire)
    monkeypatch.setattr(attachments, "acquire", acquire_si)
    monkeypatch.setattr("scanpdf.download.metadata", lambda paper, config: paper)
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def fetch_main(self, paper, accept):
            calls["main"] += 1
            assert accept(pdf(TITLE), "https://publisher.example/main.pdf")
            return {"status": "ok", "engine": "test_transport"}
        def fetch_si(self, paper, out):
            calls["si"] += 1
            f = save_attachment(out, 1, pdf("Supporting Information"), "application/pdf", "https://publisher.example/si.pdf")
            return {"status": "ok", "files": [f], "failed_urls": []}
    def runner(*args, **kwargs):
        return scan_run(*args, **kwargs, session_factory=Session)
    return calls, runner

def test_search_handoff_exact_paths_import_publication_and_resume(environment, transports):
    calls, runner = transports
    job = new_job(environment, SearchProfile(topic="magnetoelectric", max_results_per_provider=10),
                  rules={"magnetoelectric": ["magnetoelectric"]})
    result = execute(environment, job, main_runner=runner, si_runner=runner, emit=lambda _: None)
    root = Path(result["catalog"])
    assert result["status"] == "COMPLETED"
    assert (result["main_total"], result["main_available"], result["main_remaining"]) == (1, 1, 0)
    assert (result["si_files"], result["si_remaining"]) == (1, 0)
    row = json.loads((root / "browser_handoff/main_input.json").read_text(encoding="utf-8"))[0]
    assert Path(row["target_path"]).read_bytes().startswith(b"%PDF-")
    assert Path(row["target_path"]).is_relative_to(root / "downloads/agent/wiley")
    si_row = json.loads((root / "browser_handoff/si_input.json").read_text(encoding="utf-8"))[0]
    assert len(list(Path(si_row["target_dir"]).glob("*.pdf"))) == 1
    output = Path(result["result_dir"])
    assert len(list((output / "pdfs/agent").glob("*.pdf"))) == 1
    assert len(list((output / "pdfs/all").glob("*.pdf"))) == 1
    assert len(list((output / "attachment/all").rglob("*.pdf"))) == 1
    assert list((output / "classifications/by_publisher/wiley").glob("*.pdf"))
    assert list((output / "classifications/by_keywords/magnetoelectric").glob("*.pdf"))
    projects = json.loads((Path(environment.workspace) / "projects.json").read_text(encoding="utf-8"))["projects"]
    assert [(p["id"], p["main_available"], p["si_files"]) for p in projects] == [(job["id"], 1, 1)]
    assert (output / "项目说明.md").is_file()
    from paper_acquisition.toolbox.audit import audit_run
    assert not audit_run(output)["errors"]
    before = dict(calls)
    resumed = execute(environment, job, resume=True, main_runner=runner, si_runner=runner, emit=lambda _: None)
    assert resumed["result_dir"] == result["result_dir"]
    assert resumed["status"] == "COMPLETED" and calls == before


def test_formal_project_index_excludes_archive_and_keeps_empty_si_unconfirmed(environment, transports, tmp_path):
    from literature_workflow.projects import refresh_project_index
    from literature_workflow.config import atomic_json
    _, runner = transports
    job = new_job(environment, SearchProfile(topic="official"), label="正式检索", si=False)
    result = execute(environment, job, main_runner=runner, emit=lambda _: None)
    archived = {**job, "id": "abcdefabcdef", "project_kind": "archived", "result_dir": str(tmp_path / "archive-only")}
    atomic_json(environment.home / "jobs/abcdefabcdef/state.json", archived)
    projects = refresh_project_index(environment)
    assert [p["id"] for p in projects] == [job["id"]]
    assert not Path(archived["result_dir"]).exists()
    assert (Path(result["result_dir"]) / "attachment/all").is_dir()
    assert not (Path(result["catalog"]) / "attachments/state.sqlite").exists()
    assert "没有附件" in (Path(result["result_dir"]) / "项目说明.md").read_text(encoding="utf-8-sig")

def test_no_si_found_is_not_manual_none_or_false_completion(environment, transports):
    calls, runner = transports
    def no_si(papers, config, output, **options):
        from scanpdf.config import atomic_json
        calls["si"] += 1
        atomic_json(Path(output) / "si/si_manifest.json", {DOI: {"status": "zero", "files": []}})
        return {"requested": 1}
    job = new_job(environment, SearchProfile(topic="test"))
    result = execute(environment, job, main_runner=runner, si_runner=no_si, emit=lambda _: None)
    assert result["status"] == "FINISHED_WITH_PENDING" and result["si_remaining"] == 1
    from paper_acquisition.attachments.store import Store
    with Store(Path(result["catalog"])) as store:
        assert store.papers()[0]["manual_state"] == ""
    before = calls["si"]
    execute(environment, job, resume=True, main_runner=runner, si_runner=no_si, emit=lambda _: None)
    assert calls["si"] == before

def test_handoff_rejects_target_outside_current_run(environment, transports, tmp_path):
    _, runner = transports
    job = new_job(environment, SearchProfile(topic="test"), si=False)
    result = execute(environment, job, main_runner=runner, emit=lambda _: None)
    root = Path(result["catalog"])
    path = root / "agent_tasks/agent_required.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["doi", "title", "year", "publisher", "target_path"])
        w.writeheader(); w.writerow(dict(doi=DOI, title=TITLE, year=2023, publisher="wiley", target_path=str(tmp_path / "outside.pdf")))
    with pytest.raises(ValueError, match="超出"): read_tasks(root)
    assert not (tmp_path / "outside.pdf").exists()

def test_key_configuration_is_private_and_optional_sources_are_not_forced(environment):
    environment.credentials_path.write_text("OPENALEX_API_KEY=private-test-value\nLENS_API_KEY=\n", encoding="utf-8")
    report = guide(environment)
    assert report["configured_credentials"]["OPENALEX_API_KEY"] is True
    assert "private-test-value" not in json.dumps(report)
    assert "private-test-value" not in (environment.home / "config.json").read_text(encoding="utf-8")
    environment.validate(credentials=True)
    with pytest.raises(ValueError, match="LENS_API_KEY"): environment.validate(["lens"], credentials=True)

def test_progress_fields_do_not_invalidate_si_attempt():
    row = {"doi": DOI, "target_dir": "target", "title": TITLE, "attachment_urls": "[]", "agent_files": "[]"}
    updated = {**row, "agent_files": '["si.pdf"]', "reason": "UPDATED_PROGRESS"}
    assert _signature(row) == _signature(updated)
    assert _signature(row) != _signature({**row, "attachment_urls": '["https://publisher.example/new.pdf"]'})

def test_native_root_hook_rejects_reusing_or_escaping_run(tmp_path):
    from paper_acquisition.workflow import run_search
    from paper_acquisition.config import Settings
    with pytest.raises(ValueError, match="NEW_RUN_ROOT"):
        asyncio.run(run_search(SearchProfile(topic="test"), Settings(), tmp_path, root=tmp_path / "outside"))

def test_redaction_and_job_path_validation(environment):
    from literature_workflow.pipeline import job_path
    environment.credentials_path.write_text("LENS_API_KEY=local-secret-value\n", encoding="utf-8")
    assert environment.redact("Failed local-secret-value") == "Failed [REDACTED]"
    with pytest.raises(ValueError): job_path(environment, "../../other")

def test_interruption_after_download_recovers_without_redownload(environment, transports):
    calls, runner = transports
    job = new_job(environment, SearchProfile(topic="test"), si=False)
    def interrupted(*args, **kwargs):
        runner(*args, **kwargs)
        raise RuntimeError("simulated process interruption after durable PDF save")
    with pytest.raises(RuntimeError, match="interruption"):
        execute(environment, job, main_runner=interrupted, emit=lambda _: None)
    assert job["status"] == "INTERRUPTED" and calls["main"] == 1
    result = execute(environment, job, resume=True, main_runner=runner, emit=lambda _: None)
    assert result["main_available"] == 1 and result["status"] == "COMPLETED"
    assert calls["main"] == 1

def test_codex_store_virtualized_appdata_does_not_lengthen_default_home(tmp_path,monkeypatch):
    from literature_workflow.config import default_home
    monkeypatch.delenv('LITERATURE_WORKFLOW_HOME',raising=False)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local'))
    with monkeypatch.context() as m:
        m.setattr(Path,'home',classmethod(lambda cls:tmp_path/'user'))
        result=default_home()
    assert result.is_relative_to(tmp_path/'user')
    assert 'LocalCache' not in result.parts
    monkeypatch.setenv('LITERATURE_WORKFLOW_HOME',str(tmp_path/'explicit'))
    assert default_home()==(tmp_path/'explicit').resolve()
