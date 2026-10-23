from pathlib import Path
from ._elsevier.config import Config as EngineConfig
from ._elsevier.publisher_batch import PublisherBatchDownloader, PaperRecord
from ._elsevier.publisher_profiles import get_publisher_profile

def make_engine(session,paper=None):
    cfg=session.config
    engine_config=EngineConfig(school="",email=cfg.email,institution_name_zh=cfg.school,
        institution_name_en=cfg.school_en,carsi_enabled=cfg.access=="carsi",carsi_idp_name=cfg.school,
        chrome_profile_dir=str(cfg.profile("elsevier","cloakbrowser")),output_dir=str(cfg.home/"runs"/"elsevier"),
        cache_dir=str(cfg.home/"cache"/"elsevier"),cookie_path=str(cfg.home/"sessions"/"elsevier.json"),
        carsi_cookie_dir=str(cfg.home/"sessions"/"carsi"))
    profile=get_publisher_profile("elsevier")
    if paper and paper.article_url:
        from dataclasses import replace
        profile=replace(profile,article_url_template=paper.article_url)
    class ObservedDownloader(PublisherBatchDownloader):
        # Observe native stages; do not replace their download behavior.
        def _event(self,result,event,detail=""):
            super()._event(result,event,detail)
            session.emit("  Elsevier 原引擎："+event)
        def _fetch_pdf_url(self,url,*,page=None):
            from urllib.parse import urlsplit
            import time
            session.emit("  Elsevier 按原方式读取："+urlsplit(url).netloc)
            start=time.monotonic();body,final=super()._fetch_pdf_url(url,page=page)
            session.emit(f"  Elsevier 读取结束：{len(body or b'')} 字节，{time.monotonic()-start:.1f} 秒")
            return body,final
    return ObservedDownloader(engine_config,profile=profile,institution_query=cfg.school,
        login_timeout_sec=cfg.login_timeout,pdf_timeout_sec=90)

def open_context(session):
    if session.route.backend!="cloakbrowser":raise ValueError("Elsevier 必须使用原 CloakBrowser 内核")
    return make_engine(session)._launch_context()

def fetch_main(session,paper,accept,work):
    if session.route.backend!="cloakbrowser":raise ValueError("Elsevier 必须使用历史已验证的 CloakBrowser 链路，禁止静默替换内核")
    dl=make_engine(session,paper)
    work.mkdir(parents=True,exist_ok=True)
    record=PaperRecord(paper.doi,paper.title,url=paper.article_url)
    result=dl.fetch_one(session.ctx,record,work)
    if result.pdf_path and Path(result.pdf_path).exists():
        if accept(Path(result.pdf_path).read_bytes(),result.pdf_url):return {"status":"ok","url":result.pdf_url,"engine_state":result.state,"engine":"original_elsevier_cloak"}
    return {"status":"needs_manual","reason":result.reason or "identity_not_proven","engine_state":result.state,"engine":"original_elsevier_cloak"}
