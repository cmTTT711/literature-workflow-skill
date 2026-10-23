"""Small adapter around the user's ScanSci get / SI engines, with verified placement."""
import os,sys,subprocess,json,time
from pathlib import Path
from .browser import BrowserSession
from .identity import stem
from .scansci_config import engine_config,write_engine_config
from .attachments import save_attachment,dedupe_files

class ScanPDFSession(BrowserSession):
    def __enter__(self):
        if self.route.publisher=="elsevier" and not getattr(self,"_in_broker",False):
            from .elsevier_broker import ElsevierBroker
            self.broker=ElsevierBroker(self.config,self.emit).ensure()
            return self
        profile=self.config.profile(self.route.publisher,self.route.backend);profile.mkdir(parents=True,exist_ok=True)
        self.lock=profile.parent/(profile.name+".scanpdf-lock")
        try:
            from .owned_lock import acquire
            acquire(self.lock);self.owns_lock=True
        except FileExistsError:raise RuntimeError(f"会话已占用或上次异常结束：{self.lock}")
        return self
    def _ensure_browser(self):
        if not self.ctx:super().__enter__()
    def fetch_main(self,paper,accept):
        work=self.config.home/"runs"/stem(paper.doi)/str(time.time_ns());work.mkdir(parents=True,exist_ok=True)
        if self.route.publisher=="elsevier":
            if not getattr(self,"_in_broker",False):
                result=self.broker.submit("main",paper,work)
                candidate=Path(result.get("candidate") or work/"not-found")
                if result.get("status")=="ok" and candidate.is_file() and accept(candidate.read_bytes(),result.get("url","")):return result
                return {**result,"status":"needs_manual"}
            self._ensure_browser()
            from .elsevier import fetch_main
            return fetch_main(self,paper,accept,work)
        # Recorded dedicated routes retain one context for the main PDF and SI.
        # The generic PNAS get tier repeatedly reopened its challenge; the historical
        # Camoufox article -> PDF navigation path passed with the same local session.
        if self.route.publisher in {"sage","pnas"}:
            self._ensure_browser()
            result=super().fetch_main(paper,accept)
            if self.route.publisher=="pnas":result.update(engine="historical_camoufox_navigation",source="PNAS(Browser)")
            return result
        root,cfg=write_engine_config(self.config,self.route,work,paper)
        env=os.environ.copy();env["PYTHONUTF8"]="1";env["SCANPDF_HOME"]=str(self.config.home)
        env["SCANSCI_PDF_DATA_DIR"]=str(root)
        # Browser caches remain private to this portable installation.
        if self.route.backend=="camoufox":
            from .runtimes import isolate_camoufox
            # The worker also isolates before the original engine imports its backend.
        result_file=work/"result.json"
        native_doi=paper.doi
        if native_doi.startswith("10.1103/"):
            from ._scansci.publisher_profiles import _aps_canonical_doi
            native_doi=_aps_canonical_doi(native_doi)
        command=[sys.executable,"-u","-m","scanpdf.scansci_worker","--config-root",str(root),"--doi",native_doi,"--output",str(work),"--result",str(result_file)]
        self.emit("  原 ScanPDF get：legal_only，逐篇执行（日志保存到独立运行目录）")
        with (work/"engine.log").open("w",encoding="utf-8") as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env)
            try:child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                if os.name=="nt":subprocess.run(["taskkill","/F","/T","/PID",str(child.pid)],capture_output=True,timeout=20)
                else:child.kill()
                child.wait()
                if self.route.publisher in {"pnas","aiaa","jpsj","annual_reviews","ieee","oxford"}:
                    self.emit("  原 get 超时，使用已记录的该社文章页与 PDF 导航流程")
                    self._ensure_browser();return super().fetch_main(paper,accept)
                return {"status":"needs_manual","reason":"original_get_timeout","log_path":str(work/"engine.log")}
        result=json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {"success":False,"error":"original_get_no_result"}
        path=Path(result.get("file") or work/"not-found")
        if result.get("success") and path.is_file() and accept(path.read_bytes(),result.get("pdf_url") or result.get("url") or self.article_url(paper)):
            return {"status":"ok","source":result.get("source"),"engine":"bundled_scansci_get","log_path":str(work/"engine.log")}
        # Routes C and the inline-viewer fallback come from the user's recorded successful scripts.
        if self.route.publisher in {"wiley","rsc","pnas","aiaa","jpsj","annual_reviews","ieee","oxford"}:
            self.emit("  原 get 未取到已验证主件，按历史记录的专用流程补抓")
            self._ensure_browser();return super().fetch_main(paper,accept)
        return {"status":"needs_manual","reason":result.get("error") or "original_get_identity_not_proven","source":result.get("source"),"engine":"bundled_scansci_get","log_path":str(work/"engine.log")}
    def fetch_si(self,paper,out):
        if self.route.publisher=="elsevier" and not getattr(self,"_in_broker",False):
            return self.broker.submit("si",paper,out)
        from ._scansci import si_publisher
        from .network import session as http_session
        _,cfg=engine_config(self.config,self.route,out)
        if self.route.publisher in {"elsevier","sage","pnas","aiaa","jpsj","annual_reviews","ieee","oxford"}:cfg["_portable_article_url"]=self.article_url(paper)
        cfg["_portable_verify_article"]=lambda html,url:self._article_identity(html,url,paper)
        cfg["_portable_diagnostic"]=lambda page:self._diagnostic(page,paper)
        staging=self.config.home/"runs"/stem(paper.doi)/("si-"+str(time.time_ns()))
        records={};original_save=si_publisher._save;failed=set();browser=None;via="http"
        cfg["_portable_si_http_attempt"]=lambda url,ok:failed.discard(url) if ok else failed.add(url)
        def checked_save(folder,i,url,body,ctype):
            f=save_attachment(Path(folder),i,body,ctype,url)
            if f:
                records[str(Path(f["path"]).resolve())]=f;failed.discard(url);return f["path"]
            return None
        class TrackedSI(si_publisher.BrowserSISession):
            def _download(self,page,folder,i,url,depth=0):
                paths=super()._download(page,folder,i,url,depth)
                if paths:failed.discard(url)
                else:failed.add(url)
                return paths
        si_publisher._save=checked_save
        self.emit("  原 ScanPDF 附件流程：HTTP 快路 → 清单直链 → 按社会话")
        try:
            paths=si_publisher.http_fetch(paper.doi,staging,config=cfg)
            if paper.attachment_urls:
                via="csv_direct"
                with http_session(self.config) as client:
                    for i,url in enumerate(paper.attachment_urls,1):
                        if "link.aps.org" in url:continue # Original runner leaves this intermediate page to the browser.
                        if any(record.get("source_url")==url for record in records.values()):continue
                        failed.add(url)
                        try:
                            r=client.get(url,timeout=(10,40))
                            if r.status_code==200:
                                path=checked_save(staging,i,url,r.content,r.headers.get("content-type",""))
                                if path:paths.append(path)
                        except Exception:pass
            if not paths or failed:
                via="browser";self._ensure_browser()
                browser=TrackedSI(self.route.publisher,self.route.backend,cfg,ctx=self.ctx)
                try:paths+=browser.fetch(paper.doi,staging,cf_wait_s=self.config.challenge_wait)
                except Exception as error:
                    if not paths:raise
                    failed.add("browser_recovery_failed:"+type(error).__name__)
        finally:si_publisher._save=original_save
        files=[];rejected=[]
        for i,path in enumerate(paths,1):
            src=Path(path);record=records.get(str(src.resolve()),{})
            ctype={".txt":"text/plain",".csv":"text/csv"}.get(src.suffix.lower(),"")
            f=save_attachment(out,i,src.read_bytes(),ctype,record.get("source_url") or self.article_url(paper))
            if f:files.append(f)
            else:rejected.append(src.name)
        files=dedupe_files(files)
        incomplete=bool(rejected or failed)
        status="partial" if files and incomplete else "ok" if files else "zero" if browser and browser.last_page_ok and not incomplete else "needs_manual"
        return {"status":status,"files":files,"reason":"loaded_article_no_si_found" if status=="zero" else "" if files else getattr(browser,"last_reason","article_or_download_needs_review"),"rejected":rejected,"failed_urls":sorted(failed),"engine":"bundled_scansci_si","via":via,"article_url":self.article_url(paper)}
