from __future__ import annotations
import importlib.metadata, shutil, sys, platform, json
from pathlib import Path
from .network import session

STEPS=[
 {"step":1,"title":"确认学校和邮箱","action":"填写学校正式全名、可选英文名和真实联系邮箱。不要使用作者的学校或邮箱。"},
 {"step":2,"title":"选择访问方式","action":"在校内选择 campus；学校支持 CARSI 时可选 carsi；其他机构入口选择 institution 并填写学校图书馆公布的 HTTPS URL。"},
 {"step":3,"title":"检查依赖与网络","action":"运行 scanpdf doctor --network；论文域名必须按学校要求走校园网，agent 服务保持其所需网络。配置分流，不全局关闭代理。"},
 {"step":4,"title":"完成机构认证","action":"先尝试学校 IP 直授。确需认证时，运行 scanpdf login --doi <目标DOI>，在可见浏览器中自行选择机构、输入账号和验证码。完成后回到终端按回车保存会话。"},
 {"step":5,"title":"验证主件和附件","action":"运行 scanpdf get <目标DOI>；检查 manifest.json 中的身份校验和 si/si_manifest.json 中的附件结果。进入文章页或登录成功不能代替下载验证。"},
 {"step":6,"title":"复用配置与会话","action":"以后沿用本机配置。学校改变使用独立会话；会话失效再运行对应出版社 login。不要把个人会话打包分发。"}
]

def guide(config):
    return {"configured":bool(config.school and config.email),"school":config.school,"access":config.access,"steps":STEPS,"next_action":"scanpdf setup" if not config.school or not config.email else "scanpdf doctor --network"}

def doctor(config,network=False):
    packages={}
    for p in ["requests","beautifulsoup4","pymupdf","patchright","camoufox","cloakbrowser","pycryptodome"]:
        try:packages[p]=importlib.metadata.version(p)
        except importlib.metadata.PackageNotFoundError:packages[p]=None
    from ._scansci.browser_backend import find_local_browser
    checks={"platform":platform.system(),"python":sys.version.split()[0],"home":str(config.home),"school_configured":bool(config.school and config.email),"packages":packages,"chromium":config.browser_executable or find_local_browser(),"network":{}}
    if network:
        for name,url in [("publisher","https://pubs.acs.org/cdn-cgi/trace"),("elsevier","https://www.sciencedirect.com/cdn-cgi/trace"),("agent","https://api.openai.com/cdn-cgi/trace")]:
            try:
                import requests
                with (requests.Session() if name=="agent" else session(config)) as s:
                    r=s.get(url,timeout=(6,10))
                    ip=next((l[3:] for l in r.text.splitlines() if l.startswith("ip=")),None)
                    checks["network"][name]={"status":r.status_code,"egress_ip":ip}
            except Exception as e:checks["network"][name]={"error":type(e).__name__}
    from .runtimes import browsers
    checks["browser_runtimes"]=browsers(config)
    checks["python_dependencies_ready"]=bool(all(packages.values()))
    checks["dependencies_ready"]=bool(all(packages.values()) and checks["browser_runtimes"]["ready"])
    return checks

def interactive_setup(config):
    import re
    from urllib.parse import urlsplit
    print("步骤 1/3：学校与邮箱。输入你自己的信息；已有值可按回车保留。")
    def ask(label,old,valid=lambda value:True,error="输入无效，请重试"):
        while True:
            value=input(f"{label} [{old}]：").strip() or old
            if valid(value):return value
            print(error)
    config.school=ask("学校正式全名",config.school,lambda value:bool(value.strip()),"学校全名不能为空")
    config.school_en=ask("学校英文名（可留空）",config.school_en)
    config.email=ask("真实联系邮箱",config.email,lambda value:bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",value)) and not value.endswith((".invalid","@example.com")),"请填写有效的真实联系邮箱")
    print("步骤 2/3：机构访问方式。campus=校内 IP；carsi=学校支持的 CARSI；institution=学校官方其他认证入口。")
    config.access=ask("访问方式",config.access,lambda value:value in {"campus","carsi","institution"},"请选择 campus、carsi 或 institution")
    if config.access=="institution":
        config.institution_url=ask("学校图书馆官方 HTTPS 认证入口",config.institution_url,lambda value:urlsplit(value).scheme=="https" and bool(urlsplit(value).hostname),"请填写学校图书馆公布的完整 HTTPS 入口")
    print("步骤 3/3：下载目录。登录和下载验证将在保存后按向导继续。")
    config.output_dir=ask("下载目录",config.output_dir)
    config.onboarding["school_configuration"]="complete"
    config.save()
    print(json.dumps(guide(config),ensure_ascii=False,indent=2))
