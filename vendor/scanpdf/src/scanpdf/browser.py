from __future__ import annotations
import base64, time, json, os, re
from pathlib import Path
from urllib.parse import urlparse, urljoin
from ._scansci.browser_backend import launch_persistent_context
from ._scansci.publisher_pdf_router import build_pdf_candidates, discover_pdf_candidates_from_html
from .attachments import discover, save_attachment, dedupe_files

CF=("just a moment","请稍候","checking your browser","attention required","client challenge","security verification")
FETCH_JS="""async ({url, limit}) => {
 const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),30000);
 try { const r=await fetch(url,{credentials:'include',signal:ctrl.signal});
 if(!r.ok)return {status:r.status};
 if(Number(r.headers.get('content-length'))>limit)return {status:413};
 const b=new Uint8Array(await r.arrayBuffer());if(b.length>limit)return {status:413};
 let s='';for(let i=0;i<b.length;i+=32768)s+=String.fromCharCode.apply(null,b.subarray(i,i+32768));
 return {status:r.status,body:btoa(s),type:r.headers.get('content-type')||''};
 } finally {clearTimeout(timer);}
}"""

class BrowserSession:
    def __init__(self,route,config,emit=print):
        self.route=route;self.config=config;self.emit=emit;self.ctx=None;self.lock=None;self.article_pages={};self.owns_lock=False
    def __enter__(self):
        profile=self.config.profile(self.route.publisher,self.route.backend);profile.mkdir(parents=True,exist_ok=True)
        self.lock=profile.parent/(profile.name+".scanpdf-lock")
        try:
            if not self.owns_lock:
                from .owned_lock import acquire
                acquire(self.lock);self.owns_lock=True
        except FileExistsError:raise RuntimeError(f"会话已占用或上次异常结束：{self.lock}；确认无相关浏览器运行后清除此锁")
        try:
            if self.route.backend=="camoufox":
                from .runtimes import locked_camoufox
                locked_camoufox(self.config)
            if self.route.backend=="cloakbrowser":
                from .runtimes import locked_cloakbrowser
                locked_cloakbrowser(self.config)
            extra={}
            if not self.config.network_proxy:
                if self.route.backend=="camoufox":extra["firefox_user_prefs"]={"network.proxy.type":0}
                elif self.route.backend=="cloakbrowser":extra["args"]=["--disable-features=CrossOriginOpenerPolicy"]
                else:extra["args"]=["--no-proxy-server"]
            self.emit("  启动独立浏览器会话")
            if self.route.publisher=="elsevier" and self.route.backend=="cloakbrowser":
                from .elsevier import open_context
                self.ctx=open_context(self)
            else:
                self.ctx=launch_persistent_context(str(profile),headless=self.config.headless,
                     config=self.config.browser_config(self.route.backend),accept_downloads=True,timeout=30000,**extra)
            from .cookie_state import restore
            restore(self.ctx,self.config,self.route)
            self.ctx.set_default_timeout(30000 if self.route.publisher=="elsevier" else 15000)
            if self.route.backend=="cloakbrowser":
                self.emit("  CloakBrowser 实际内核："+str(self.ctx.browser.version))
            self.emit("  浏览器会话就绪")
            return self
        except Exception:
            if self.ctx:
                try:self.ctx.close()
                except Exception:pass
                self.ctx=None
            self.lock.unlink(missing_ok=True);raise
    def __exit__(self,*args):
        try:
            if self.ctx:
                try:
                    from .cookie_state import save
                    save(self.ctx,self.config,self.route)
                except Exception as error:self.emit("会话附加保存未完成："+type(error).__name__)
                self.ctx.close()
        finally:
            if self.lock:self.lock.unlink(missing_ok=True)
    def _title(self,page):
        try:return page.title().lower()
        except Exception:return ""
    def _content(self,page):
        for _ in range(4):
            try:return page.content()
            except Exception:page.wait_for_timeout(800)
        return ""
    def _ready(self,page):
        deadline=time.monotonic()+self.config.challenge_wait
        notified=False
        while True:
            try:
                page.wait_for_load_state("domcontentloaded",timeout=3000)
                title=page.title().lower()
                if "/connect/authorize" in page.url and "prompt=none" in page.url:
                    if time.monotonic()>=deadline:return False
                    page.wait_for_timeout(1500);continue
                if not any(x in title for x in CF):
                    page.wait_for_timeout(700)
                    if self._content(page):return True
            except Exception:pass
            if time.monotonic()>=deadline:return False
            if not notified:
                self.emit("浏览器正在等待站点验证；如窗口有验证，请在本机完成。")
                notified=True
            page.wait_for_timeout(2000)
    def _goto(self,page,url):
        try:page.goto(url,wait_until="domcontentloaded",timeout=45000)
        except Exception:pass # navigation may intentionally turn into a browser download
    def _watch(self,page,captures,downloads):
        def response(resp):
            try:
                ct=resp.headers.get("content-type","")
                length=int(resp.headers.get("content-length") or 0)
                if self.route.backend=="camoufox" and resp.status==200 and "application/pdf" in ct and length<=self.config.max_file_mb*1048576:
                    body=resp.body()
                    if len(body)<=self.config.max_file_mb*1048576:captures.append((body,resp.url,ct))
            except Exception:pass
        page.on("response",response);page.on("download",lambda d:downloads.append(d))
    def _fetch(self,page,url):
        limit=self.config.max_file_mb*1048576
        # Wiley's browser fetch must precede context.request: same cookies, different network fingerprint.
        try:
            r=page.evaluate(FETCH_JS,{"url":url,"limit":limit})
            if r.get("body"):return base64.b64decode(r["body"]),r.get("type","")
        except Exception:pass
        try:
            r=self.ctx.request.get(url,timeout=20000,headers={"Referer":page.url})
            if r.status==200 and int(r.headers.get("content-length") or 0)<=limit:
                b=r.body()
                if len(b)<=limit:return b,r.headers.get("content-type","")
        except Exception:pass
        return b"",""
    def _downloads(self,downloads):
        out=[]
        for d in downloads:
            try:
                path=d.path()
                if path and Path(path).stat().st_size<=self.config.max_file_mb*1048576:
                    out.append((Path(path).read_bytes(),d.url,""))
            except Exception:pass
        return out
    def article_url(self,paper):
        if paper.article_url:return paper.article_url
        if self.route.publisher=="sage" and self.config.school in {"哈尔滨工业大学","Harbin Institute of Technology"}:
            return "https://sage.cnpereading.com/doi/"+paper.doi
        from .publishers import route_for
        return route_for(paper.doi,self.config).article
    def _diagnostic(self,page,paper):
        from bs4 import BeautifulSoup
        from .config import atomic_json
        from .identity import stem
        soup=BeautifulSoup(self._content(page),"html.parser")
        text=soup.get_text(" ",strip=True).lower()
        markers={k:(k in text) for k in ["full access","available access","access provided","institutional access","purchase access","buy now","supporting information","supplementary"]}
        links=[]
        for a in soup.find_all("a",href=True):
            href=a["href"]
            if "pdf" in href.lower() or "pdf" in a.get_text().lower():
                u=urlparse(urljoin(page.url,href));links.append({"host":u.netloc,"path":u.path,"label":a.get_text(" ",strip=True)[:90]})
        meta=[{"name":str(m.get("name",m.get("property",""))),"value":str(m.get("content",""))[:250]} for m in soup.find_all("meta") if str(m.get("name",m.get("property",""))).lower() in {"citation_doi","citation_pdf_url","citation_title"}]
        u=urlparse(page.url);path=self.config.home/"diagnostics"/(stem(paper.doi)+".json")
        atomic_json(path,{"doi":paper.doi,"title":self._title(page),"host":u.netloc,"path":u.path,"access_markers":markers,"meta":meta,"pdf_links":links[:20],"password_field":bool(page.locator('input[type="password"]').count())})
        return str(path)
    def _wiley_main(self,page,paper,accept):
        for attempt in range(3):
            if attempt:
                self.emit(f"  Wiley 重新加载文章并重试：{attempt+1}/3")
                self._goto(page,self.article_url(paper));page.wait_for_timeout(1500)
                if not self._ready(page):break
            host=urlparse(page.url).netloc
            for route in ["pdfdirect","pdf"]:
                url=f"https://{host}/doi/{route}/{paper.doi}?download=true"
                try:
                    result=page.evaluate(FETCH_JS,{"url":url,"limit":self.config.max_file_mb*1048576})
                    body=base64.b64decode(result["body"]) if result.get("body") else b""
                    self.emit(f"  Wiley 页内 fetch：HTTP {result.get('status')}，{len(body)} 字节")
                    if body and accept(body,url):return {"status":"ok","url":url}
                except Exception as e:self.emit("  Wiley 页内 fetch："+type(e).__name__)
            if attempt<2:page.wait_for_timeout(3000)
        return None
    def _article_identity(self,html,url,paper):
        from bs4 import BeautifulSoup
        from urllib.parse import unquote
        from .verification import title_score,words
        soup=BeautifulSoup(html,"html.parser")
        title=soup.title.get_text(" ",strip=True).lower() if soup.title else ""
        if any(x in title for x in CF) or re.search(r"/login|/connect/authorize|/idp/|/sso/|shibboleth",url,re.I):return False
        ids=[str(m.get("content","")).strip().lower() for m in soup.find_all("meta") if str(m.get("name",m.get("property",""))).lower() in {"citation_doi","dc.identifier","dc.identifier.doi","prism.doi"}]
        if any(paper.doi==i.removeprefix("https://doi.org/").removeprefix("doi:").strip() for i in ids):return True
        if paper.doi in unquote(urlparse(url).path).lower():return True
        titles=[str(m.get("content","")) for m in soup.find_all("meta") if str(m.get("name","")).lower()=="citation_title"]
        titles += [x.get_text(" ",strip=True) for x in soup.find_all("h1")]
        titles.append(title)
        return len(words(paper.title))>=4 and any(title_score(paper.title,t)>=0.85 for t in titles)
    def _click_pdf_action(self,page,paper,accept,captures,downloads):
        popups=[]
        def on_page(p):
            popups.append(p);self._watch(p,captures,downloads)
        self.ctx.on("page",on_page)
        try:
            actions=page.get_by_role("button",name=re.compile(r"^(view\s+|download\s+)?pdf$",re.I))
            if not actions.count():actions=page.get_by_role("link",name=re.compile(r"^(?:(?:view|download)\s+)?pdf$|^open the pdf for",re.I))
            if not actions.count():return None
            actions.first.click(timeout=10000);page.wait_for_timeout(3500)
            for b,u,_ in captures+self._downloads(downloads):
                if accept(b,u):return {"status":"ok","url":u}
            for target in [page]+popups:
                if target.is_closed():continue
                candidates=discover_pdf_candidates_from_html(self._content(target),target.url)
                if target.url!=self.article_url(paper):candidates.insert(0,target.url)
                for u in list(dict.fromkeys(candidates))[:4]:
                    b,_=self._fetch(target,u)
                    if b and accept(b,u):return {"status":"ok","url":u}
        except Exception:pass
        finally:
            self.ctx.remove_listener("page",on_page)
            for p in popups:
                if not p.is_closed():p.close()
        return None
    def fetch_main(self,paper,accept):
        self.emit("  创建论文独立页面")
        for previous in self.article_pages.values():
            if not previous.is_closed():previous.close()
        self.article_pages.clear()
        page=self.ctx.new_page();captures=[];downloads=[]
        self._watch(page,captures,downloads)
        try:
            self.emit("  加载文章页："+paper.doi)
            self._goto(page,self.article_url(paper));page.wait_for_timeout(2000)
            if not self._ready(page):return {"status":"needs_manual","reason":"browser_challenge","url":page.url}
            html=self._content(page);source=page.url
            if self._article_identity(html,source,paper):self.article_pages[paper.doi]=page
            if self.route.publisher=="sage":
                result=self._click_pdf_action(page,paper,accept,captures,downloads)
                if result:return result
            for body,url,ct in captures+self._downloads(downloads):
                if accept(body,url):return {"status":"ok","url":url}
            if self.route.publisher=="wiley":
                result=self._wiley_main(page,paper,accept)
                if result:return result
            found=discover_pdf_candidates_from_html(html,source)
            urls=build_pdf_candidates(self.route.profile,paper.doi,source_url=source,discovered_urls=found) if self.route.profile else found
            host=urlparse(source).netloc
            if self.route.publisher=="wiley":
                urls=[f"https://{host}/doi/pdfdirect/{paper.doi}?download=true",f"https://{host}/doi/pdf/{paper.doi}?download=true"]+urls
            if self.route.publisher in {"pnas","aiaa","jpsj","annual_reviews"}:
                urls=[urljoin(source,"/doi/pdf/"+paper.doi+"?download=true")]+urls
            # IEEE stamp landing pages embed the actual PDF in iframe/embed elements.
            urls=list(dict.fromkeys(urls))[:12]
            deadline=time.monotonic()+150
            for u in urls[:6]:
                if time.monotonic()>deadline:break
                if re.search(r"suppl|mmc\d|moesm|supporting",u,re.I):continue
                self.emit("  尝试出版社 PDF 路径："+urlparse(u).netloc)
                body,ct=self._fetch(page,u)
                if body and accept(body,u):return {"status":"ok","url":u}
                nav=self.ctx.new_page();nav_c=[];nav_d=[];self._watch(nav,nav_c,nav_d)
                try:
                    self._goto(nav,u);nav.wait_for_timeout(1500)
                    if not self._ready(nav):continue
                    for b,uu,_ in nav_c+self._downloads(nav_d):
                        if accept(b,uu):return {"status":"ok","url":uu}
                    if nav.url!=u:
                        b,_=self._fetch(nav,nav.url)
                        if b and accept(b,nav.url):return {"status":"ok","url":nav.url}
                    for u2 in discover_pdf_candidates_from_html(self._content(nav),nav.url)[:4]:
                        if u2==u:continue
                        b,_=self._fetch(page,u2)
                        if b and accept(b,u2):return {"status":"ok","url":u2}
                finally:nav.close()
            result=self._click_pdf_action(page,paper,accept,captures,downloads)
            if result:return result
            for b,u,_ in captures+self._downloads(downloads):
                if accept(b,u):return {"status":"ok","url":u}
            return {"status":"needs_manual","reason":"pdf_not_captured_or_access_not_confirmed","url":source,"diagnostic_path":self._diagnostic(page,paper)}
        finally:
            if self.article_pages.get(paper.doi) is not page:page.close()
    def fetch_si(self,paper,out):
        page=self.article_pages.pop(paper.doi,None)
        reused=page is not None and not page.is_closed()
        if not reused:page=self.ctx.new_page()
        try:
            self.emit("  加载文章页："+paper.doi)
            if not reused:self._goto(page,self.article_url(paper));page.wait_for_timeout(2000)
            if not self._ready(page):return {"status":"needs_manual","reason":"browser_challenge","files":[]}
            page.evaluate("window.scrollTo(0,document.body.scrollHeight)");page.wait_for_timeout(2000)
            identity_ok=False
            for _ in range(5):
                if self._article_identity(self._content(page),page.url,paper):identity_ok=True;break
                page.wait_for_timeout(1500)
            if not identity_ok:return {"status":"needs_manual","reason":"article_identity_unproven_for_si","files":[]}
            urls=list(dict.fromkeys(paper.attachment_urls+discover(self._content(page),page.url)))
            if not urls and (page.locator('input[type="password"]').count() or re.search(r"/login|/idp/|/sso/|shibboleth",page.url,re.I)):
                return {"status":"needs_manual","reason":"institution_login_required","files":[]}
            if not urls:return {"status":"zero","reason":"no_si_links_discovered_on_loaded_article","files":[],"article_url":page.url}
            files=[];rejected=[]
            deadline=time.monotonic()+120
            for i,u in enumerate(urls[:20],1):
                if time.monotonic()>deadline:
                    rejected.extend(urls[i-1:]);break
                self.emit("  尝试出版社 PDF 路径："+urlparse(u).netloc)
                body,ct=self._fetch(page,u);saved=save_attachment(out,i,body,ct,u)
                if not saved and body and ("text/html" in ct or body.lstrip().startswith(b"<")):
                    children=discover(body.decode("utf-8","replace"),u)
                    for j,child in enumerate(children[:15],1):
                        cb,cct=self._fetch(page,child);cs=save_attachment(out,f"{i}.{j}",cb,cct,child)
                        if cs and not any(f["sha256"]==cs["sha256"] for f in files):files.append(cs)
                    if children and all(any(f["source_url"]==child for f in files) for child in children[:15]):continue
                if not saved:
                    nav=self.ctx.new_page();captures=[];downloads=[];self._watch(nav,captures,downloads)
                    try:
                        self._goto(nav,u);nav.wait_for_timeout(1500)
                        if self._ready(nav):
                            for b,uu,typ in captures+self._downloads(downloads):
                                saved=save_attachment(out,i,b,typ,uu)
                                if saved:break
                            if not saved:
                                b,typ=self._fetch(page,nav.url);saved=save_attachment(out,i,b,typ,nav.url)
                    finally:nav.close()
                if saved:
                    if not any(f["sha256"]==saved["sha256"] for f in files):files.append(saved)
                else:rejected.append(u)
            files=dedupe_files(files)
            return {"status":"ok" if files and not rejected else "partial" if files else "needs_manual","files":files,"failed_urls":rejected,"article_url":page.url}
        finally:page.close()
    def login(self,url,wait_for_confirmation=False):
        if self.config.headless:raise ValueError("机构登录必须使用可见浏览器")
        page=self.ctx.new_page()
        try:
            self._goto(page,url)
            self.emit("请在浏览器中自行完成学校登录/验证码，回到终端按回车保存会话。完成登录不代表已获所有出版社权限。")
            import sys,uuid
            from .config import atomic_json
            if wait_for_confirmation or not sys.stdin.isatty():
                request_id=uuid.uuid4().hex
                request=self.config.home/"login_requests"/(request_id+".json")
                done=request.with_suffix(".done")
                atomic_json(request,{"id":request_id,"publisher":self.route.publisher,"status":"waiting_for_user"})
                self.emit("LOGIN_REQUEST_ID="+request_id)
                self.emit("完成后由 agent 执行 scanpdf confirm-login "+request_id)
                deadline=time.monotonic()+self.config.login_timeout
                while not done.exists() and time.monotonic()<deadline:page.wait_for_timeout(1000)
                if not done.exists():
                    atomic_json(request,{"id":request_id,"status":"timed_out"})
                    return {"status":"manual_login_timeout","request_id":request_id}
                atomic_json(request,{"id":request_id,"status":"session_saved_access_unverified"})
                done.unlink(missing_ok=True)
            else:input()
            from .cookie_state import save
            saved=save(self.ctx,self.config,self.route)
            return {"url":page.url,"status":"session_saved_access_unverified","session_path":str(saved)}
        finally:page.close()
