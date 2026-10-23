import json,os
from pathlib import Path
from scanpdf.config import Config
from scanpdf.identity import Paper
from scanpdf.publishers import route_for,institution_login_url
from scanpdf.scansci_config import engine_config
from scanpdf.scansci_session import ScanPDFSession
from scanpdf._scansci.si_publisher import BrowserSISession
from test_integrity import pdf,TITLE,D1


def test_native_config_is_school_scoped_and_keeps_legal_get(tmp_path):
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    route=route_for("10.1038/s41563-023-01759-y",cfg)
    root,raw=engine_config(cfg,route,tmp_path/"output")
    assert root.is_relative_to(tmp_path)
    assert raw["download_strategy"]=="legal_only" and raw["scihub_enabled"] is False
    assert raw["batch_default_lanes"] is False
    assert raw["chrome_profile_dir"]==str(cfg.profile("springer","patchright"))
    assert raw["elsevier_api_key"]=="" and raw["carsi_idp_name"]=="测试大学"


def test_nature_and_springer_use_the_historical_wayf_entrances(tmp_path):
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    assert institution_login_url("10.1038/test",cfg)=="https://wayf.springernature.com/?redirect_uri=https://www.nature.com"
    assert institution_login_url("10.1007/test",cfg)=="https://wayf.springernature.com/?redirect_uri=https://link.springer.com"
    cfg.access="institution";cfg.institution_url="https://library.school.edu/login"
    assert institution_login_url("10.1038/test",cfg)==cfg.institution_url


def test_original_si_403_uses_navigation_without_unbound_content_type(tmp_path):
    class Response:status=403
    class Request:
        def get(self,*a,**kw):return Response()
    class Context:request=Request()
    class Page:context=Context()
    s=object.__new__(BrowserSISession)
    s._download_via_navigation=lambda folder,i,url:["navigation-result"]
    assert s._download(Page(),tmp_path,1,"https://publisher.test/si.pdf")==["navigation-result"]


def test_original_si_http_pdf_reaches_save_not_unreachable_branch(tmp_path,monkeypatch):
    from scanpdf._scansci import si_publisher
    class Response:
        status=200;headers={"content-type":"application/pdf"}
        def body(self):return pdf()
    class Request:
        def get(self,*a,**kw):return Response()
    class Context:request=Request()
    class Page:context=Context()
    monkeypatch.setattr(si_publisher,"_save",lambda *args:"saved-pdf")
    s=object.__new__(BrowserSISession)
    assert s._download(Page(),tmp_path,1,"https://publisher.test/si.pdf")==["saved-pdf"]


def test_adapter_invokes_bundled_original_get_and_reports_source(tmp_path,monkeypatch):
    import scanpdf.scansci_session as module
    class Child:
        def __init__(self,command,**kw):
            assert command[command.index("-m")+1]=="scanpdf.scansci_worker"
            result=Path(command[command.index("--result")+1]);path=result.parent/"downloaded.pdf";path.write_bytes(pdf())
            result.write_text(json.dumps({"success":True,"file":str(path),"source":"NatureDirect","url":"https://publisher.test/article"}),encoding="utf-8")
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(module.subprocess,"Popen",Child)
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    s=ScanPDFSession(route_for("10.1038/test",cfg),cfg,lambda _:None)
    accepted=[]
    result=s.fetch_main(Paper("10.1038/test",TITLE),lambda b,u:accepted.append(b) or True)
    assert accepted and result["engine"]=="bundled_scansci_get" and result["source"]=="NatureDirect"


def test_elsevier_refuses_alternative_kernel(tmp_path):
    import pytest
    from scanpdf.elsevier import fetch_main
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path,backend_overrides={"elsevier":"patchright"})
    s=ScanPDFSession(route_for("10.1016/test",cfg),cfg,lambda _:None)
    with pytest.raises(ValueError,match="CloakBrowser"):fetch_main(s,Paper("10.1016/test"),lambda *a:True,tmp_path)


def test_si_identity_rejects_doi_in_sso_query_but_accepts_rendered_title(tmp_path):
    from scanpdf.browser import BrowserSession
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    s=BrowserSession(route_for(D1),cfg)
    assert not s._article_identity("<title>Login</title>","https://idp.test/login?target="+D1,Paper(D1,TITLE))
    assert s._article_identity("<html><h1>"+TITLE+"</h1></html>","https://publisher.test/document/123",Paper(D1,TITLE))


def test_confirm_login_needs_a_current_request(tmp_path,capsys):
    from scanpdf.cli import main
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path);cfg.save()
    request_id="a"*32;p=tmp_path/"login_requests"/(request_id+".json");p.parent.mkdir()
    p.write_text(json.dumps({"status":"waiting_for_user"}),encoding="utf-8")
    assert main(["--home",str(tmp_path),"confirm-login",request_id])==0
    assert p.with_suffix(".done").exists()
    assert main(["--home",str(tmp_path),"confirm-login","../bad"])==2


def test_original_si_discards_figures_and_decodes_publisher_query():
    from scanpdf._scansci.supplementary import extract_supplementary_links
    html='<a href="/cms/asset/paper-fig-0001-m.jpg">Figure</a><a href="/DownloadFile/DownloadImage.aspx?attachment=1">Figure</a><a href="/action/downloadSupplement?doi=10.1/x&amp;file=si.pdf">SI</a>'
    assert extract_supplementary_links(html,"https://publisher.test/article")==["https://publisher.test/action/downloadSupplement?doi=10.1/x&file=si.pdf"]


def test_session_restore_keeps_new_live_cookie_and_discards_expired():
    from scanpdf.cookie_state import missing_cookies
    saved=[dict(name="session",value="old",domain=".publisher.test",path="/",expires=-1),dict(name="expired",value="x",domain=".publisher.test",expires=1),dict(name="idp",value="secret",domain=".school.test",path="/",expires=-1,httpOnly=True,secure=True)]
    live=[dict(name="session",value="new",domain="publisher.test",path="/")]
    result=missing_cookies(saved,live)
    assert len(result)==1 and result[0]["name"]=="idp" and result[0]["httpOnly"]
    assert "expires" not in result[0]


def test_login_session_exports_to_original_carsi_formats_without_extra_browser(tmp_path,monkeypatch):
    from scanpdf.cookie_state import save
    from scanpdf._scansci import browser_engine
    monkeypatch.setattr(browser_engine,"import_cookies",lambda *args:(_ for _ in ()).throw(AssertionError("must not launch another browser")))
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    route=route_for("10.1038/example",cfg)
    class Context:
        def storage_state(self):return {"cookies":[dict(name="session",value="local-secret",domain=".nature.com",path="/",expires=-1)],"origins":[]}
    path=save(Context(),cfg,route)
    root,raw=engine_config(cfg,route,tmp_path)
    assert path.exists() and (Path(raw["cache_dir"])/"carsi_cookies"/"nature.json").exists()
    assert "local-secret" in (Path(raw["cache_dir"])/"publisher_cookies.txt").read_text()


def test_broker_rejects_reused_pid_and_stale_heartbeat(tmp_path,monkeypatch):
    import time
    from scanpdf import elsevier_broker as module
    from scanpdf.config import atomic_json
    cfg=Config(school="测试大学",email="user@school.edu",home=tmp_path)
    broker=module.ElsevierBroker(cfg,lambda _:None)
    path=broker.directory/"state.json"
    atomic_json(path,{"status":"ready","pid":123,"process_created_at":"old"})
    monkeypatch.setattr(module,"process_stamp",lambda pid:"new")
    assert not broker.running()
    monkeypatch.setattr(module,"process_stamp",lambda pid:"old")
    assert broker.running()
    os.utime(path,(time.time()-60,time.time()-60))
    assert not broker.running()


def test_first_school_setup_recovers_invalid_entries(tmp_path,monkeypatch,capsys):
    from scanpdf.onboarding import interactive_setup
    responses=iter(["","测试大学","","bad-email","user@school.edu","bad-access","institution","http://bad","https://library.school.edu/login",str(tmp_path/"papers")])
    monkeypatch.setattr("builtins.input",lambda _:next(responses))
    cfg=Config(home=tmp_path/"home");interactive_setup(cfg)
    saved=Config.load(cfg.home)
    assert saved.school=="测试大学" and saved.access=="institution" and saved.institution_url=="https://library.school.edu/login"
    assert "请填写有效" in capsys.readouterr().out


def test_institutional_aps_profile_uses_original_case_normalizer():
    from scanpdf._scansci.institutional.publisher_profiles import APS_PROFILE
    assert "/prb/" in APS_PROFILE.article_url("10.1103/physrevb.107.174403")
    assert "PhysRevB.107.174403" in APS_PROFILE.pdf_urls("10.1103/physrevb.107.174403")[0]


def test_owned_lock_rejects_live_owner_and_recovers_dead_owner(tmp_path,monkeypatch):
    import pytest
    from scanpdf import owned_lock
    path=tmp_path/"profile.lock"
    monkeypatch.setattr(owned_lock,"process_stamp",lambda pid:"live")
    owned_lock.acquire(path)
    with pytest.raises(RuntimeError,match="正在使用"):owned_lock.acquire(path)
    path.write_text(json.dumps({"pid":123,"created":"dead"}),encoding="utf-8")
    owned_lock.acquire(path)
    assert json.loads(path.read_text())["pid"]==os.getpid()


def test_runtime_bundle_rejects_path_escape_and_corrupt_file(tmp_path):
    import pytest,zipfile,hashlib
    from scanpdf.runtime_bundle import install_bundle
    cfg=Config(home=tmp_path/"home")
    archive=tmp_path/"bad.zip"
    for name in ["../outside.txt","camoufox/browsers/official/152.0.4-beta.30-ea52a02f/wrong.exe"]:
        with zipfile.ZipFile(archive,"w") as z:
            z.writestr(name,b"wrong")
            z.writestr("runtime-manifest.json",json.dumps({"format":1,"files":{name:{"sha256":"0"*64,"size":5}}}))
        with pytest.raises(ValueError):install_bundle(cfg,archive)
    assert not (tmp_path/"outside.txt").exists()
    assert not list((cfg.home/"browsers").glob(".install-*"))


def test_camoufox_bundle_marker_prevents_sdk_cleanup_and_upgrade(tmp_path,monkeypatch):
    import scanpdf.runtimes as runtime
    import camoufox.multiversion as versions
    from types import SimpleNamespace
    root=tmp_path/"browsers"/"camoufox";path=root/"browsers"/"official"/"152.0.4-beta.30-ea52a02f"
    path.mkdir(parents=True);marker=root/".compatible"
    monkeypatch.setattr(runtime,"isolate_camoufox",lambda config:SimpleNamespace(INSTALL_DIR=root))
    monkeypatch.setattr(versions,"find_installed_version",lambda version:path)
    monkeypatch.setattr(versions,"set_active",lambda value:None)
    monkeypatch.setattr(versions,"COMPAT_FLAG",marker)
    assert runtime.locked_camoufox(Config(home=tmp_path))==path
    assert marker.exists()


def test_aps_supplemental_resolver_is_canonical_not_abstract_anchor():
    from scanpdf._scansci.supplementary import extract_supplementary_links
    assert extract_supplementary_links('<a href="http://link.aps.org/supplemental/10.1103/PhysRevB.107.174403">SI</a>',"https://journals.aps.org/prb/abstract/10.1103/PhysRevB.107.174403")==['https://journals.aps.org/prb/supplemental/10.1103/PhysRevB.107.174403']


def test_jpsj_and_pnas_supplement_indexes_are_discovered():
    from scanpdf._scansci.supplementary import extract_supplementary_links
    href="/doi/suppl/10.7566/JPSJ.93.094709"
    assert extract_supplementary_links('<a href="'+href+'">Supplemental material</a>',"https://journals.jps.jp/doi/10.7566/JPSJ.93.094709")==["https://journals.jps.jp"+href]


def test_custom_institution_is_kept_in_original_institutional_tier(tmp_path):
    from scanpdf._scansci.sources import _any_institutional_path
    from scanpdf._scansci.institutional.instsci_bridge import _any_institutional_configured
    cfg=Config(school="测试大学",email="user@school.edu",access="institution",institution_url="https://library.school.edu/login",home=tmp_path)
    _,raw=engine_config(cfg,route_for("10.1063/test",cfg),tmp_path)
    assert _any_institutional_path(raw) and _any_institutional_configured(raw)
    assert raw["carsi_enabled"] is False and raw["is_campus_network"] is False


def test_oup_supplement_issue_is_not_supplementary_material():
    from scanpdf._scansci.supplementary import extract_supplementary_links
    html='<a href="/mam/article-pdf/30/Supplement_1/ozae044.749/58669442/ozae044.749.pdf">PDF</a><a href="/doi/suppl_file/test.s001.pdf">SI</a>'
    assert extract_supplementary_links(html,'https://academic.oup.com/mam/article/30/Supplement_1/ozae044.749/123')==['https://academic.oup.com/doi/suppl_file/test.s001.pdf']


def test_exited_process_does_not_own_profile_lock():
    import subprocess,sys
    from scanpdf.elsevier_broker import process_stamp
    child=subprocess.Popen([sys.executable,'-c','pass'])
    child.wait()
    assert process_stamp(child.pid) is None


import pytest
@pytest.mark.parametrize("recover",[False,True])
def test_partial_http_si_is_not_reported_as_complete(tmp_path,monkeypatch,recover):
    from scanpdf._scansci import si_publisher
    url='https://publisher.test/suppl-missing.pdf'
    def http(doi,folder,config):
        one=si_publisher._save(folder,1,'https://publisher.test/suppl-one.pdf',pdf(),"application/pdf")
        config['_portable_si_http_attempt'](url,False)
        return [one]
    class Browser:
        last_page_ok=True
        def __init__(self,*args,**kwargs):pass
        def fetch(self,doi,folder,cf_wait_s):
            if not recover:return []
            return [si_publisher._save(folder,2,url,pdf(title='Different supporting data'),"application/pdf")]
    monkeypatch.setattr(si_publisher,'http_fetch',http)
    monkeypatch.setattr(si_publisher,'BrowserSISession',Browser)
    cfg=Config(school='测试大学',email='user@school.edu',home=tmp_path/'home')
    session=ScanPDFSession(route_for(D1,cfg),cfg,lambda _:None)
    session._ensure_browser=lambda:None
    result=session.fetch_si(Paper(D1,TITLE),tmp_path/'out')
    assert result['status']==('ok' if recover else 'partial')
    assert len(result['files'])==(2 if recover else 1)
    assert result['failed_urls']==([] if recover else [url])
