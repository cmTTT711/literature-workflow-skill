"""One user-facing command for setup, search, download, publication and recovery."""
from pathlib import Path
import argparse, getpass, importlib.metadata, json, os, sys
from .config import Config, KEY_NAMES, PROVIDER_KEYS, guide

def parser():
    p = argparse.ArgumentParser(prog="literature-workflow", description="检索、按清单下载正文附件、回填与归档")
    p.add_argument("--home", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("guide", help="首次使用的分步配置引导")
    s = sub.add_parser("setup", help="学校、邮箱、检索来源与保存位置")
    for name in ("school", "email", "access", "institution-url", "workspace", "providers"):
        s.add_argument("--" + name)
    keys = sub.add_parser("keys", help="只显示凭据是否已配置；可在本机终端输入 key")
    keys.add_argument("--set", choices=KEY_NAMES)
    sub.add_parser("doctor", help="检查依赖、内核和已选服务的配置")
    b = sub.add_parser("browsers")
    b.add_argument("--bundle", type=Path)
    b.add_argument("--install", action="store_true")
    login = sub.add_parser("login")
    login.add_argument("--doi", required=True)
    login.add_argument("--wait-for-confirmation", action="store_true")
    confirm = sub.add_parser("confirm-login")
    confirm.add_argument("request_id")
    imp = sub.add_parser("import-session")
    imp.add_argument("--doi", required=True)
    imp.add_argument("--source", type=Path, required=True)
    broker = sub.add_parser("broker")
    broker.add_argument("action", choices=("status", "stop"))
    run = sub.add_parser("run", help="一个命令完成检索到正文及附件下载归档")
    run.add_argument("--profile", type=Path)
    run.add_argument("--topic")
    run.add_argument("--year-from", type=int)
    run.add_argument("--year-to", type=int)
    run.add_argument("--include", action="append")
    run.add_argument("--exclude", action="append")
    run.add_argument("--max-results", type=int)
    run.add_argument("--providers")
    run.add_argument("--label")
    run.add_argument("--no-si", action="store_true")
    run.add_argument("--classification-rules", type=Path)
    resume = sub.add_parser("resume")
    resume.add_argument("--job", required=True)
    resume.add_argument("--retry-failed", action="store_true")
    resume.add_argument("--offline", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--job")
    return p

def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)

def main(argv=None):
    # Browser broker children must use the same encoding as their persisted logs.
    os.environ["PYTHONUTF8"] = "1"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    args = parser().parse_args(argv)
    config = Config.load(args.home)
    try:
        if args.command == "guide":
            emit(guide(config)); return 0
        if args.command == "setup":
            for name in ("school", "email", "access", "institution_url", "workspace"):
                if getattr(args, name, None) is not None:
                    setattr(config, name, getattr(args, name))
            if args.providers:
                config.providers = [n.strip() for n in args.providers.split(",") if n.strip()]
            config.__post_init__()
            config.save(); emit(guide(config)); return 0
        if args.command == "keys":
            if args.set:
                if not sys.stdin.isatty():
                    raise ValueError("在本机终端运行 keys --set，或只在本机编辑 " + str(config.credentials_path))
                config.home.mkdir(parents=True, exist_ok=True)
                value = getpass.getpass(args.set + "（仅保存在本机，不回显）：").strip()
                if not value or any(c in value for c in "\r\n\"'"):
                    raise ValueError("输入为空或含不支持的字符")
                lines = config.credentials_path.read_text(encoding="utf-8").splitlines() if config.credentials_path.exists() else []
                lines = [line for line in lines if not line.startswith(args.set + "=")]
                lines.append(args.set + "=" + value)
                config.credentials_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            emit({"credential_file": str(config.credentials_path), "configured": {k: bool(v) for k, v in config.credentials().items()}})
            return 0
        config.activate()
        if args.command in {"browsers", "login", "confirm-login", "import-session", "broker"}:
            from scanpdf.cli import main as scan_main
            command = ["--home", str(config.scan_config().home), args.command]
            if args.command == "browsers":
                if args.bundle: command += ["--bundle", str(args.bundle)]
                if args.install: command.append("--install")
            elif args.command == "login":
                command += ["--doi", args.doi]
                if args.wait_for_confirmation: command.append("--wait-for-confirmation")
            elif args.command == "confirm-login": command.append(args.request_id)
            elif args.command == "import-session": command += ["--doi", args.doi, "--source", str(args.source)]
            else: command.append(args.action)
            return scan_main(command)
        if args.command == "doctor":
            from scanpdf.onboarding import doctor
            checks = doctor(config.scan_config())
            packages = {}
            for name in ("paper-acquisition", "scanpdf-portable", "literature-workflow", "pydantic", "pypdf"):
                try: packages[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError: packages[name] = None
            values = config.credentials()
            emit({"dependencies": packages, "download": checks, "school_configured": bool(config.school and config.email),
                  "providers": {n: {"credential_configured": bool(values[PROVIDER_KEYS[n]])} for n in config.providers},
                  "workspace": config.workspace})
            return 0 if checks["dependencies_ready"] and all(packages.values()) else 2
        from .pipeline import new_job, execute, load_job
        if args.command == "status":
            if args.job: emit(load_job(config, args.job))
            else:
                emit([{k: r.get(k) for k in ("id", "status", "updated_at", "result_dir", "summary")}
                      for path in sorted((config.home / "jobs").glob("*/state.json"))
                      for r in [json.loads(path.read_text(encoding="utf-8"))]])
            return 0
        if args.command == "run":
            from paper_acquisition.models import SearchProfile
            raw = json.loads(args.profile.read_text(encoding="utf-8-sig")) if args.profile else {}
            for name in ("topic", "year_from", "year_to"):
                if getattr(args, name) is not None: raw[name] = getattr(args, name)
            if args.max_results is not None: raw["max_results_per_provider"] = args.max_results
            if args.include is not None: raw["include_terms"] = args.include
            if args.exclude is not None: raw["exclude_terms"] = args.exclude
            providers = [n.strip() for n in args.providers.split(",")] if args.providers else None
            rules = json.loads(args.classification_rules.read_text(encoding="utf-8-sig")) if args.classification_rules else None
            job = new_job(config, SearchProfile(**raw), label=args.label, providers=providers,
                          si=not args.no_si, rules=rules)
            print("任务编号：" + job["id"], flush=True)
            report = execute(config, job)
        else:
            report = execute(config, load_job(config, args.job), resume=True,
                             retry=args.retry_failed, offline=args.offline)
        emit(report)
        return 0 if report["status"] == "COMPLETED" else 2
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print("未完成：" + config.redact(str(error)), file=sys.stderr)
        return 2
