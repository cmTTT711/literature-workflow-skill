import argparse
import asyncio
import json
import logging
from pathlib import Path

from .acquisition.service import acquisition_audit, initialize_catalog, resolve_all
from .audit import audit
from .config import Settings, load_env
from .discovery import discover, rebuild
from .master import audit_master, export_agent, prepare_master
from .models import SearchProfile
from .providers import registry
from .storage import Catalog


def emit(report, *, ensure_ascii=True, failed=False):
    print(json.dumps(report, ensure_ascii=ensure_ascii, indent=2))
    if failed:
        raise SystemExit(2)


def search_profile(args, values=None):
    if values is None:
        values = json.loads(args.profile.read_text(encoding="utf-8-sig")) if args.profile else {}
    for argument, field in (
        ("topic", "topic"),
        ("year_from", "year_from"),
        ("year_to", "year_to"),
        ("max_results", "max_results_per_provider"),
        ("slice_years", "year_slice_size"),
    ):
        if (value := getattr(args, argument, None)) is not None:
            values[field] = value
    return SearchProfile(**values)


def build_parser():
    parser = argparse.ArgumentParser(prog="paper-acquisition")
    sub = parser.add_subparsers(dest="command", required=True)
    attachments = sub.add_parser("attachments", help="Independent supplementary-file tools")
    attachments.add_argument("action", choices=["acquire", "export", "publish", "mark", "audit"])
    attachments.add_argument("--data-dir", type=Path, required=True)
    attachments.add_argument("--limit", type=int)
    attachments.add_argument("--retry-failed", action="store_true")
    attachments.add_argument("--refresh", action="store_true")
    attachments.add_argument("--doi")
    attachments.add_argument("--outcome", choices=["complete", "none", "reopen"])
    run = sub.add_parser("run", help="Search, acquire and generate an independent collection")
    run.add_argument("--label", help="Human-readable folder label; search terms remain unchanged")
    resume = sub.add_parser("resume", help="Continue an interrupted collection without creating a new folder")
    resume.add_argument("--run-dir", required=True, type=Path)
    resume.add_argument("--offline", action="store_true")
    publish = sub.add_parser("publish", help="Import local PDFs and regenerate offline collection views")
    publish.add_argument("--data-dir", required=True, type=Path)
    publish.add_argument("--label")
    publish.add_argument(
        "--compact",
        action="store_true",
        help="Remove exact duplicate working copies after verified publication",
    )
    library_audit = sub.add_parser("audit-library")
    run_audit = sub.add_parser("audit-run", help="Verify PDF copies and DOI uniqueness in a published run")
    run_audit.add_argument("--run-dir", required=True, type=Path)
    classification = sub.add_parser("classify", help="Classify standalone PDFs from a run's pdfs/all")
    classification.add_argument("--run-dir", required=True, type=Path)
    classification.add_argument("--by", choices=["publisher", "keywords"], default="publisher")
    classification.add_argument("--rules", type=Path)
    classification.add_argument("--name")
    for command in (run, resume, publish, library_audit):
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--config", type=Path)
    discovery = sub.add_parser("discover")
    discovery.add_argument(
        "--resume-run-id", help="Start audited safe replay of a saved profile under a new run"
    )
    for command in (run, discovery):
        command.add_argument("--topic")
        command.add_argument("--profile", type=Path)
        command.add_argument("--year-from", type=int)
        command.add_argument("--year-to", type=int)
        command.add_argument("--max-results", type=int)
        command.add_argument("--providers", help="Comma-separated provider names")
        command.add_argument("--offline", action="store_true", help="Use local data without network requests")
    discovery.add_argument(
        "--slice-years", type=int, help="Audited year partitions; requires both year bounds"
    )
    discovery.add_argument(
        "--force-refresh", action="store_true", help="Bypass fresh successful response cache"
    )
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--online", action="store_true")
    resolve.add_argument("--refresh", action="store_true")
    download = sub.add_parser("acquire", help="Try all automatic routes, update handoff and publish in place")
    download.add_argument(
        "--limit", type=int, help="Optional sample bound; omitted processes all master papers"
    )
    download.add_argument("--sources", help="Optional comma-separated OA candidate sources")
    download.add_argument("--retry-failed", action="store_true")
    download.add_argument("--paper-ids", help="Comma-separated paper IDs for a bounded sample or rerun")
    download.add_argument(
        "--max-candidates",
        type=int,
        default=3,
        help="Compatibility option; the automatic workflow still tries all candidates",
    )
    exhaustive = sub.add_parser("openalex-exhaust", help="Compatibility command; included in run/acquire")
    exhaustive.add_argument("--refresh", action="store_true")
    init = sub.add_parser("init-acquisition")
    init.add_argument("--source-data-dir", required=True, type=Path)
    rename = sub.add_parser("rename-pdfs")
    rename.add_argument("--apply", action="store_true", help="Apply backed-up year/title filename migration")
    for command in [
        discovery,
        exhaustive,
        sub.add_parser("retry-openalex-blocked"),
        sub.add_parser("export"),
        sub.add_parser("rebuild"),
        sub.add_parser("audit"),
        resolve,
        download,
        init,
        rename,
        sub.add_parser("audit-acquisition"),
        sub.add_parser("prepare-master"),
        sub.add_parser("export-agent"),
    ]:
        command.add_argument("--config", type=Path)
        command.add_argument("--data-dir", type=Path)
    sub.add_parser("providers").add_argument("--json", action="store_true")
    return parser


def main():
    load_env()
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "attachments":
        from .attachments import service

        if args.action == "mark":
            if not args.doi or not args.outcome:
                parser.error("attachments mark requires --doi and --outcome")
            report = service.mark(args.data_dir, args.doi, args.outcome)
        elif args.action == "acquire":
            if args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            report = asyncio.run(
                service.acquire(
                    args.data_dir, limit=args.limit, retry_failed=args.retry_failed, refresh=args.refresh
                )
            )
        elif args.action == "audit":
            report = service.audit(args.data_dir)
        else:
            report = service.local(args.data_dir, publish=args.action == "publish")
        emit(report, ensure_ascii=False, failed=report.get("errors") or report.get("import_errors"))
        return
    if args.command == "audit-run":
        from .toolbox.audit import audit_run

        report = audit_run(args.run_dir)
        emit(report, ensure_ascii=False, failed=report["errors"])
        return
    if args.command == "classify":
        from .toolbox.classification import classify

        rules = json.loads(args.rules.read_text(encoding="utf-8-sig")) if args.rules else None
        emit(classify(args.run_dir, by=args.by, rules=rules, name=args.name), ensure_ascii=False)
        return
    if args.command in {"run", "resume", "publish", "audit-library"}:
        from .library.collections import publish_collection
        from .library.store import Library
        from .workflow import resume_collection, run_search

        settings = Settings.load(args.config)
        if args.command == "run":
            values = json.loads(args.profile.read_text(encoding="utf-8-sig")) if args.profile else {}
            if not (args.topic if args.topic is not None else values.get("topic")):
                parser.error("run requires --topic or a profile containing topic")
            settings.offline = args.offline
            if args.providers:
                settings.providers = [v.strip() for v in args.providers.split(",")]
            report = asyncio.run(
                run_search(search_profile(args, values), settings, args.workspace, label=args.label)
            )
        elif args.command == "resume":
            settings.offline = args.offline
            report = asyncio.run(resume_collection(args.run_dir, settings))
        elif args.command == "publish":
            report = publish_collection(
                args.data_dir, workspace=args.workspace, label=args.label, compact=args.compact
            )
        else:
            with Library(args.workspace / "workspace/library") as library:
                report = library.audit()
        emit(report, ensure_ascii=False, failed=report.get("errors") or report.get("status") == "NO_DATA")
        return
    if args.command == "providers":
        print(
            json.dumps([cls.capabilities() for cls in registry().values()], indent=2)
            if args.json
            else "\n".join(sorted(registry()))
        )
        return
    settings = Settings.load(args.config)
    if args.data_dir:
        settings.data_dir = args.data_dir
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("paper_acquisition")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(settings.data_dir / "events.jsonl", encoding="utf-8")
    logger.addHandler(handler)
    try:
        if args.command == "discover":
            settings.force_refresh = args.force_refresh
            settings.offline = args.offline
            if args.offline and args.force_refresh:
                parser.error("--offline and --force-refresh are mutually exclusive")
            values = None
            if args.resume_run_id:
                with Catalog(settings.data_dir) as catalog:
                    row = catalog.db.execute(
                        "SELECT profile_json FROM search_runs WHERE run_id=?", (args.resume_run_id,)
                    ).fetchone()
                    if not row:
                        parser.error("Unknown saved run ID")
                    values = json.loads(row[0])
                    settings.providers = [
                        r[0]
                        for r in catalog.db.execute(
                            "SELECT DISTINCT provider FROM provider_queries WHERE run_id=?",
                            (args.resume_run_id,),
                        )
                        if r[0] in registry()
                    ]
            profile = search_profile(args, values)
            if args.providers:
                settings.providers = [name.strip() for name in args.providers.split(",")]
            report = asyncio.run(discover(profile, settings, parent_run_id=args.resume_run_id))
            report["master"] = asyncio.run(prepare_master(settings.data_dir, online=not args.offline))
            emit(report, ensure_ascii=False, failed=report["status"] == "NO_DATA")
        elif args.command == "init-acquisition":
            initialize_catalog(args.source_data_dir, settings.data_dir)
            print("Acquisition catalog initialized")
        elif args.command == "resolve":
            emit(asyncio.run(resolve_all(settings.data_dir, online=args.online, refresh=args.refresh)))
        elif args.command == "acquire":
            from .workflow import acquire_collection

            if args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            emit(
                asyncio.run(
                    acquire_collection(
                        settings.data_dir,
                        Path.cwd(),
                        limit=args.limit,
                        max_candidates=args.max_candidates,
                        retry_failed=args.retry_failed,
                        sources=set(args.sources.split(",")) if args.sources else None,
                        paper_ids=set(args.paper_ids.split(",")) if args.paper_ids else None,
                    )
                )
            )
        elif args.command == "retry-openalex-blocked":
            from .acquisition.retry_openalex import retry_blocked

            emit(asyncio.run(retry_blocked(settings.data_dir)))
        elif args.command == "openalex-exhaust":
            from .acquisition.openalex_locations import exhaust_openalex

            emit(asyncio.run(exhaust_openalex(settings.data_dir, refresh=args.refresh)))
        elif args.command == "prepare-master":
            emit(asyncio.run(prepare_master(settings.data_dir)))
        elif args.command == "rename-pdfs":
            from .acquisition.rename import migrate_pdf_names

            report = migrate_pdf_names(settings.data_dir, apply=args.apply)
            emit({k: v for k, v in report.items() if k != "moves"}, ensure_ascii=False)
        elif args.command == "export-agent":
            emit(export_agent(settings.data_dir))
        elif args.command == "audit-acquisition":
            report = acquisition_audit(settings.data_dir)
            emit(report, failed=report["integrity"] != "ok" or report["errors"])
        elif args.command == "audit":
            report = audit(settings.data_dir)
            report["master"] = audit_master(settings.data_dir)
            emit(
                report,
                failed=(
                    report["master"]["errors"]
                    or report["integrity"] != "ok"
                    or any(report[k] for k in ("foreign_key_errors", "raw_errors", "provenance_errors"))
                ),
            )
        elif args.command == "rebuild":
            print(rebuild(settings))
        else:
            with Catalog(settings.data_dir) as catalog:
                print(catalog.export())
    finally:
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    main()
