from __future__ import annotations
import argparse,json,sys,os
from pathlib import Path
from .config import Config
from .identity import Paper,read_papers
from .onboarding import guide,doctor,interactive_setup

def parser():
    p=argparse.ArgumentParser(prog="scanpdf",description="Windows 学校文献主件与附件下载")
    p.add_argument("--home",type=Path,help="本机配置和独立会话目录")
    sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("setup",help="逐步配置自己的学校")
    for name in ["school","school-en","email","access","institution-url","output","browser"]:s.add_argument("--"+name)
    s.add_argument("--headless",action="store_true")
    b=sub.add_parser("browsers",help="检查或下载隔离的浏览器内核");b.add_argument("--install",action="store_true");b.add_argument("--bundle",type=Path,help="安装随包的原版本公开浏览器内核")
    broker=sub.add_parser("broker",help="原 Elsevier 常驻会话状态与停止");broker.add_argument("action",choices=["status","stop"])
    sub.add_parser("guide",help="输出首次配置的分步指引")
    d=sub.add_parser("doctor",help="检查依赖和学校网络");d.add_argument("--network",action="store_true")
    l=sub.add_parser("login",help="打开独立的机构会话供本人登录");l.add_argument("--doi",required=True);l.add_argument("--url");l.add_argument("--wait-for-confirmation",action="store_true")
    confirm=sub.add_parser("confirm-login",help="用户完成本机认证后确认保存会话");confirm.add_argument("request_id")
    imp=sub.add_parser("import-session",help="显式复制本人已有会话，保留源目录不变")
    imp.add_argument("--doi",required=True);imp.add_argument("--source",required=True,type=Path)
    for name in ["get","batch"]:
        d=sub.add_parser(name,help="下载主件和附件" if name=="get" else "按出版社逐篇处理 CSV 或 DOI 清单")
        d.add_argument("input");d.add_argument("--output",type=Path);d.add_argument("--no-si",action="store_true");d.add_argument("--si-only",action="store_true");d.add_argument("--redo",action="store_true");d.add_argument("--respect-target-paths",action="store_true")
    v=sub.add_parser("verify",help="核验本地 PDF 对应的 DOI 与标题");v.add_argument("file",type=Path);v.add_argument("--doi",required=True);v.add_argument("--title",default="")
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        cfg=Config.load(args.home)
        os.environ["SCANPDF_HOME"]=str(cfg.home)
        os.environ["SCANSCI_PDF_DATA_DIR"]=str(cfg.home/"scansci"/cfg.school_id)
        if args.command=="setup":
            if not args.school and not args.email:return interactive_setup(cfg) or 0
            for src,dst in [("school","school"),("school_en","school_en"),("email","email"),("access","access"),("institution_url","institution_url"),("output","output_dir"),("browser","browser_executable")]:
                if getattr(args,src,None) is not None:setattr(cfg,dst,getattr(args,src))
            if args.headless:cfg.headless=True
            cfg.onboarding["school_configuration"]="complete";cfg.save();print(json.dumps(guide(cfg),ensure_ascii=False,indent=2));return 0
        if args.command=="browsers":
            from .runtimes import browsers
            if args.bundle:
                from .runtime_bundle import install_bundle
                install_bundle(cfg,args.bundle)
            report=browsers(cfg,args.install);print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report["ready"] else 2
        if args.command=="guide":print(json.dumps(guide(cfg),ensure_ascii=False,indent=2));return 0
        if args.command=="doctor":
            checks=doctor(cfg,args.network);print(json.dumps(checks,ensure_ascii=False,indent=2));return 0 if checks["dependencies_ready"] else 2
        if args.command=="verify":
            from .verification import verify_pdf
            verdict=verify_pdf(args.file.read_bytes(),Paper(args.doi).doi,args.title);print(json.dumps(verdict.as_dict(),ensure_ascii=False,indent=2));return 0 if verdict.status=="verified" else 2
        cfg.validate()
        if args.command=="broker":
            from .elsevier_broker import ElsevierBroker
            controller=ElsevierBroker(cfg)
            print(json.dumps(controller.stop() if args.action=="stop" else {"running":controller.running(),**controller.state()},ensure_ascii=False,indent=2));return 0
        if args.command=="confirm-login":
            import re
            from .config import atomic_json
            if not re.fullmatch(r"[a-f0-9]{32}",args.request_id):raise ValueError("无效登录请求 ID")
            p=cfg.home/"login_requests"/(args.request_id+".json")
            if not p.exists() or json.loads(p.read_text(encoding="utf-8")).get("status")!="waiting_for_user":raise ValueError("登录请求不存在或已结束")
            p.with_suffix(".done").write_text("confirmed",encoding="utf-8");print("已确认，等待原登录进程保存会话后再开始下载。");return 0
        if args.command in {"login","import-session"}:
            from .publishers import route_for
            route=route_for(Paper(args.doi).doi,cfg)
            if args.command=="import-session":
                from .sessions import import_session
                print(json.dumps(import_session(args.source,cfg.profile(route.publisher,route.backend),route.backend),ensure_ascii=False,indent=2));return 0
            from .browser import BrowserSession
            from .publishers import institution_login_url
            url=args.url or institution_login_url(Paper(args.doi).doi,cfg)
            if route.publisher=="elsevier":
                from .elsevier_broker import ElsevierBroker
                result=ElsevierBroker(cfg).submit("login",url=url,wait_for_confirmation=args.wait_for_confirmation)
                print(json.dumps(result,ensure_ascii=False,indent=2))
            else:
                with BrowserSession(route,cfg) as b:print(json.dumps(b.login(url,args.wait_for_confirmation),ensure_ascii=False,indent=2))
            return 0
        from .download import run
        papers=[Paper(args.input)] if args.command=="get" else read_papers(Path(args.input))
        summary=run(papers,cfg,args.output,si=not args.no_si,si_only=args.si_only,redo=args.redo,respect_targets=args.respect_target_paths)
        print(json.dumps(summary,ensure_ascii=False,indent=2));return 0 if not summary["needs_attention"] else 2
    except (ValueError,OSError,RuntimeError,json.JSONDecodeError) as e:
        print(f"错误：{e}",file=sys.stderr);return 2
