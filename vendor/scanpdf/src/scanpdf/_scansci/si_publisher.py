# -*- coding: utf-8 -*-
"""SI 旁路：复用各社既有通路（publisher_profile_<pub> + 指定后端）抓补充材料。

设计铁律（与主件下载隔离）：
- 本模块只被独立 runner（工作区 si_runner.py）显式调用；
  不接入 pipeline / main.py，download_si 配置默认关，主件链路不会走到这里。
- 后端与 profile 按 AGENTS.md §7 的出版社-后端表复用，与主件下载同一套
  （RSC=camoufox，Wiley/ACS/AIP/APS=patchright，profile 统一 publisher_profile_<pub>）。
- 一个出版社开一个持久上下文，跨 DOI 复用（AGENTS.md 铁律 3），每 DOI 换新 page（铁律：坑 1）。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from .supplementary import (
    _article_url as _article_url_registry,
    _ext_from_content_type,
    _ext_of,
    _safe_stem,
    _user_agent,
    extract_supplementary_links,
)

logger = logging.getLogger(__name__)


def _page_content(page):
    for _ in range(10):
        try:return page.content()
        except Exception:page.wait_for_timeout(500)
    return ""

def _page_title(page):
    try:return page.title() or ""
    except Exception:return ""

# DOI 前缀 → (publisher, backend)；与 AGENTS.md §7 分组表一致
SPECS: dict[str, tuple[str, str]] = {
    "10.1002": ("wiley", "patchright"),
    "10.1111": ("wiley", "patchright"),
    "10.1155": ("wiley", "patchright"),
    "10.1021": ("acs", "patchright"),
    "10.1063": ("aip", "patchright"),
    "10.1103": ("aps", "patchright"),
    "10.1039": ("rsc", "camoufox"),
    "10.3390": ("mdpi", "patchright"),
    "10.1007": ("springer", "patchright"),
    "10.1038": ("springer", "patchright"),
    "10.21203": ("springer", "patchright"),
    "10.1088": ("iop", "patchright"),
    "10.7498": ("iop", "patchright"),
    "10.1016": ("elsevier", "instsci"),  # SD 会话在 instsci chrome-profile（主件 24/24 的财富）
    "10.1126": ("science", "patchright"),        # AAAS，publisher_profile_science
    "10.1146": ("annual reviews", "patchright"),  # Annual Reviews，校园 IP 直授
    "10.1080": ("tandfonline", "patchright"),     # T&F
    "10.1109": ("ieee", "patchright"),            # 校园 IP 直授
    "10.1107": ("iucr", "patchright"),            # IUCr，无 profile（新建）
    "10.7566": ("jpsj", "camoufox"),              # JPSJ，AGENTS.md 有主件通路
    "10.35848": ("jpsj", "camoufox"),             # JSAP (APEX/JAP)
}

_CF_MARKERS = (
    "just a moment",
    "请稍候",
    "challenge-platform",
    "cf-chl",
    "attention required",
    "checking your browser",
    "client challenge",
)

# 页面上 SI 锚点的等待选择器（懒加载区块出现时命中）
_SI_ANCHOR_SEL = (
    'a[href*="article-supplement"], a[href*="supplementarydata"], a[href*="suppdata"], '
    'a[href*="downloadSupplement"], a[href*="/cms/"], a[href*="MOESM"], '
    'a[href*="suppl_file"], a[href*="supplemental"], a[href*="supp_info"]'
)

_SKIP_CT = ("text/html",)


def spec_for(doi: str) -> tuple[str, str] | None:
    prefix = doi.split("/", 1)[0] if "/" in doi else ""
    return SPECS.get(prefix)


def _article_url(doi: str) -> str | None:
    """DOI → 文章落地页。APS/Elsevier 走专用构造器，其余走注册表。"""
    if doi.lower().startswith("10.1103/"):
        try:
            from .publisher_profiles import _aps_article_url
            u = _aps_article_url(doi)
            if u:
                return u
        except Exception:
            pass
        # 插件 helper 的兜底缺口：revmodphys / prxquantum / prper 不带 physrev 前缀
        u = _aps_url_fallback(doi)
        if u:
            return u
    if doi.lower().startswith("10.1016/"):
        u = _elsevier_url_from_pii(doi)
        if u:
            return u
    return _article_url_registry(doi)


def _elsevier_url_from_pii(doi: str, timeout: int = 25) -> str | None:
    """linkinghub 跳转里带 PII（doi.org → /retrieve/pii/Sxxxxxxxx）。
    Crossref 的 alternative-id 字段不可靠（实测被截断），以跳转为准。"""
    import requests
    try:
        r = requests.get(f"https://doi.org/{doi}", headers={"User-Agent": _user_agent()},
                         timeout=timeout, allow_redirects=True)
        mm = re.search(r"/pii/(S\d{16,17}(?:\([^)]*\))?)", str(r.url), re.I)
        if mm:
            return f"https://www.sciencedirect.com/science/article/pii/{mm.group(1)}"
    except Exception:
        pass
    return None


_APS_FALLBACK = {
    "revmodphys": ("RevModPhys", "rmp"),
    "prxquantum": ("PRXQuantum", "prxquantum"),
    "prper": ("PRPER", "prper"),
    "physrevmaterials": ("PhysRevMaterials", "prmaterials"),
    "physrevfluids": ("PhysRevFluids", "prfluids"),
}


def _aps_url_fallback(doi: str) -> str | None:
    suffix = doi.split("/", 1)[-1]
    journal, _, rest = suffix.partition(".")
    entry = _APS_FALLBACK.get(journal.lower())
    if not entry or not rest:
        return None
    return f"https://journals.aps.org/{entry[1]}/abstract/10.1103/{entry[0]}.{rest}"


# ---------------------------------------------------------------- HTTP 快路

def http_fetch(doi: str, out_dir: str | Path, timeout: int = 25, max_files: int = 10, *, config: dict | None = None) -> list[str]:
    """纯 HTTP 抓 SI（Springer/IOP 等 Cloudflare 宽松社）。

    被拦（非 200 / linkinghub）返回 []，由调用方决定是否走浏览器。
    """
    article_url = (config or {}).get("_portable_article_url") or _article_url(doi)
    if not article_url:
        return []
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.trust_env = False
    headers = {"User-Agent": _user_agent()}
    proxy = _network_proxy(config or {})
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = session.get(article_url, headers=headers, proxies=proxies,
                           timeout=timeout, allow_redirects=True)
    except Exception:
        return []
    if resp.status_code != 200 or "linkinghub." in str(resp.url):
        return []

    check=(config or {}).get("_portable_verify_article")
    if check and not check(resp.text,str(resp.url)):return []
    links = extract_supplementary_links(resp.text, str(resp.url), max_files=max_files)
    saved: list[str] = []
    notify=(config or {}).get("_portable_si_http_attempt")
    for i, url in enumerate(links, 1):
        path=None
        try:
            r = session.get(url, headers=headers, proxies=proxies,
                            timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                ctype = (r.headers.get("content-type") or "").lower()
                if "text/html" not in ctype:path=_save(out,i,url,r.content,ctype)
        except Exception:pass
        if path:saved.append(path)
        if notify:notify(url,bool(path))
    return saved



def _network_proxy(config: dict) -> str:
    try:
        from .config import load_config
        return (config or {}).get("network_proxy") or load_config().get("network_proxy", "")
    except Exception:
        return ""


def _save(out: Path, i: int, url: str, body: bytes, ctype: str) -> str | None:
    if ctype.startswith("image/"):
        return None  # 插图不是 SI（Wiley /cms/asset 的 JPEG 教训）
    ext = _ext_of(url) or _ext_from_content_type(ctype) or ".bin"
    if ext == ".bin" and not body.startswith(
        (b"%PDF", b"PK\x03\x04", bytes.fromhex("d0cf11e0a1b11ae1"), b"\x1f\x8b")
    ):
        return None  # 未知二进制 = 挑战页/错误页伪装（CF octet-stream 教训）
    path = out / f"SI{i}{ext}"
    path.write_bytes(body)
    return str(path)


# ---------------------------------------------------------------- 浏览器通路

class BrowserSISession:
    """一个出版社一个持久上下文（复用该社主件下载的 profile 与后端）。"""

    def __init__(self, publisher: str, backend: str, config: dict | None = None,
                 profile_dir: str | Path | None = None, ctx: Any = None):
        if ctx is not None:
            # 预构建的上下文（如 instsci CloakBrowser 会话）
            self.config = dict(config or {})
            self.publisher = publisher
            self.backend = backend
            self.profile = Path(str(profile_dir or "instsci-session"))
            self.ctx = ctx
            self.last_page_ok = True
            logger.info(f"[SI] {publisher}: using provided context (backend={backend})")
            return
        from .browser_engine import get_persistent_context

        cfg = dict(config or {})
        cfg["browser_backend"] = backend  # 按社覆盖全局后端
        self.config = cfg
        self.publisher = publisher
        self.backend = backend
        cache = Path(cfg.get("cache_dir") or Path.home() / ".scansci-pdf" / "cache")
        self.profile = Path(profile_dir or cache / f"publisher_profile_{publisher}")
        self.ctx = get_persistent_context(self.profile, cfg)
        self.last_page_ok = True  # 上一篇是否未被 CF 拦死（runner 用它区分 zero / needs_manual）
        logger.info(f"[SI] {publisher}: persistent context ready (backend={backend}, profile={self.profile.name})")

    def fetch(self, doi: str, out_dir: str | Path, max_files: int = 10,
              cf_wait_s: int = 30) -> list[str]:
        """抓一篇；CF 挑战时等 cf_wait_s 秒（可见窗口可手动点一次验证）。

        返回保存的文件列表；页面被挑战拦死返回 []（由 runner 记 needs_manual）。
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        article_url = self.config.get("_portable_article_url") or _article_url(doi)
        if not article_url:
            return []

        page = self.ctx.new_page()  # 每 DOI 新 page：防监听器/状态串味（坑 1）
        try:
            try:page.goto(article_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:pass # The redirect may still finish after navigation was interrupted.
            html, links = self._extract_with_cf_wait(page, article_url, cf_wait_s)
            if not links:
                # SI 区块常为 JS 懒加载（如 RSC articledatasupplements、MDPI 滚动后注入）：
                # 先滚到底触发渲染，再等强标记锚点，最后重提
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                try:
                    page.wait_for_selector(_SI_ANCHOR_SEL, timeout=15_000)
                except Exception:
                    pass
                links = extract_supplementary_links(_page_content(page), page.url, max_files=max_files)
            check=self.config.get("_portable_verify_article")
            if check:
                proven=False
                for _ in range(12):
                    try:
                        if check(_page_content(page),page.url):proven=True;break
                    except Exception:pass
                    page.wait_for_timeout(1500)
                if not proven:
                    self.last_page_ok=False
                    self.last_reason="article_identity_unproven_after_render"
                    diagnostic=self.config.get("_portable_diagnostic")
                    if diagnostic:diagnostic(page)
                    return []
            if not links:
                # A silent SSO redirect may have completed during the identity wait.
                links=extract_supplementary_links(_page_content(page),page.url,max_files=max_files)
            if not links:
                return []
            saved: list[str] = []
            for i, url in enumerate(links, 1):
                saved.extend(self._download(page, out, i, url))
            return [s for s in saved if s]
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _extract_with_cf_wait(self, page, article_url: str, cf_wait_s: int):
        """提取 SI 链接；挑战判定只看页面标题（正文里的 challenge-platform
        脚本标签是 CF 站常态，不代表被拦——APS 15 篇误报的教训）。"""
        deadline = time.time() + max(cf_wait_s, 0)
        links: list[str] = []
        while True:
            page.wait_for_timeout(2500)
            title = _page_title(page).lower()
            challenged = any(m in title for m in _CF_MARKERS)
            if "/connect/authorize" in page.url or "doi.org/" in page.url:
                if time.time()<deadline:page.wait_for_timeout(1500);continue
                self.last_page_ok=False;self.last_reason="article_redirect_timeout"
                return _page_content(page),[]
            links = extract_supplementary_links(_page_content(page), page.url, max_files=10)
            if links and not challenged:
                self.last_page_ok = True
                return _page_content(page), links
            if not challenged:
                # 无挑战但没链接：再给 JS 懒加载一次机会
                page.wait_for_timeout(4000)
                html = _page_content(page)
                links = extract_supplementary_links(html, page.url, max_files=10)
                self.last_page_ok = True
                return html, links
            if time.time() >= deadline:
                logger.warning(f"[SI] {self.publisher}: Cloudflare challenge not cleared within {cf_wait_s}s")
                self.last_page_ok = False
                return _page_content(page), []
            page.wait_for_timeout(4000)

    def _download(self, page, out: Path, i, url: str, depth: int = 0) -> list[str]:
        """两级下载：context.request 快路 → 403/HTML 时导航捕获兜底。"""
        try:
            r = page.context.request.get(url, timeout=45_000)
        except Exception as e:
            logger.warning(f"[SI] {self.publisher}: download failed {url[:80]}: {e!r}")
            r = None
        if r is not None and r.status == 200:
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.body()
            if "text/html" in ctype:
                if depth >= 1 or not body:return []
                inner = extract_supplementary_links(body.decode("utf-8", "replace"), getattr(r,"url",url), max_files=5)
                if inner:
                    saved = []
                    for j,u2 in enumerate(inner,1):saved.extend(self._download(page,out,f"{i}.{j}",u2,depth+1))
                    return saved
                return self._download_via_navigation(out,i,url) if depth==0 else []
            path = _save(out,i,url,body,ctype)
            return [path] if path else []

        # 快路失败（403/超时）：真实导航 + CF 等待 + 下载事件/响应捕获
        return self._download_via_navigation(out, i, url)

    def _download_via_navigation(self, out: Path, i, url: str, cf_wait_s: int = 45) -> list[str]:
        """导航到附件 URL：部分社（如 RSC article-supplement）对裸请求 403，
        但真实导航可过 CF，再由下载事件或最终响应出文件。"""
        nav = self.ctx.new_page()
        got: dict = {}
        dls: list = []

        def _on_resp(r):
            try:
                ctype=r.headers.get("content-type", "")
                if getattr(getattr(r,"request",None),"resource_type","")=="document" or "pdf" in ctype or "zip" in ctype or r.url==url:
                    got["url"] = r.url
                    got["ctype"] = ctype
            except Exception:
                pass

        def _on_dl(d):
            dls.append(d)

        nav.on("response", _on_resp)
        nav.on("download", _on_dl)
        saved: list[str] = []
        try:
            try:
                nav.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass  # 下载型响应可能中断 goto，事件已挂钩
            # CF 自动放行等待
            deadline = time.time() + cf_wait_s
            while time.time() < deadline:
                t = (nav.title() or "").lower()
                if not any(m in t for m in _CF_MARKERS):
                    break
                nav.wait_for_timeout(5000)
            children=[]
            # An index reached after a 403 may now expose the actual SI files.
            if not any(m in _page_title(nav).lower() for m in _CF_MARKERS):
                children=extract_supplementary_links(_page_content(nav),nav.url,max_files=10)
                children=[child for child in children if child not in {url,nav.url} and not ("link.aps.org/supplemental/" in child)]
                for j,child in enumerate(children,1):saved.extend(self._download(nav,out,f"{i}.{j}",child,depth=1))
                if saved:return saved
            # Do not wait for an event that was already received during navigation.
            if not dls and not children:
                try:nav.wait_for_event("download", timeout=5000)
                except Exception:pass
            for d in dls:
                name = d.suggested_filename or f"SI{i}"
                path = out / f"SI{i}_{_safe_stem(name)}"
                try:
                    d.save_as(str(path))
                    saved.append(str(path))
                except Exception:
                    pass
            if saved:
                return saved
            # 无下载事件：若最终响应是文件类型，取 body
            ctype = (got.get("ctype") or "").lower()
            if ctype and "text/html" not in ctype:
                try:
                    body = nav.context.request.get(got.get("url") or url, timeout=45_000).body()
                    path = _save(out, i, got.get("url") or url, body, ctype)
                    if path:
                        saved.append(path)
                except Exception:
                    pass
            return saved
        finally:
            try:
                nav.close()
            except Exception:
                pass

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------- instsci 会话

def open_instsci_session(config: dict | None = None) -> "BrowserSISession":
    """复用 instsci 的 CloakBrowser 会话（SD 主件 24/24 战役的 chrome-profile 财富）。"""
    raise RuntimeError("Use the portable Elsevier context; external InstSci sessions are disabled")


# ---------------------------------------------------------------- 归属工具

def si_stem(doi: str) -> str:
    """与主件 PDF 的 {DOI_stem}_*.pdf 命名对齐。"""
    return _safe_stem(doi)
