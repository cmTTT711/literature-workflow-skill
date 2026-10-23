"""Keep the original workflow and automate its existing Agent handoff."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import asyncio, json, os, re

from .config import atomic_json
from .handoff import download_main, download_si
from .projects import refresh_project_index

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def working_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)

def job_path(config, job_id):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise ValueError("无效任务编号")
    return config.home / "jobs" / job_id / "state.json"

def load_job(config, job_id):
    path = job_path(config, job_id)
    if not path.is_file():
        raise ValueError("任务不存在")
    return json.loads(path.read_text(encoding="utf-8"))

def new_job(config, profile, *, label=None, providers=None, si=True, rules=None):
    providers = list(dict.fromkeys(providers or config.providers))
    config.validate(providers, credentials=True)
    job_id = uuid4().hex[:12]
    workspace = Path(config.workspace)
    job = {"version": 1, "id": job_id, "created_at": now(), "status": "CREATED",
           "workspace": str(workspace), "catalog": str(workspace / "workspace/runs" / job_id),
           "profile": profile.model_dump(mode="json"), "label": label or profile.topic, "providers": providers,
           "project_kind": "formal",
           "include_si": si, "classification_rules": rules, "stages": {}}
    atomic_json(job_path(config, job_id), job)
    refresh_project_index(config, overview_job=job)
    return job

def execute(config, job, *, resume=False, retry=False, offline=False, emit=print,
            main_runner=None, si_runner=None):
    from paper_acquisition.config import Settings
    from paper_acquisition.models import SearchProfile
    from paper_acquisition.workflow import run_search, resume_collection, acquire_collection
    from paper_acquisition.library.collections import publish_collection
    from paper_acquisition.library.store import collection_config
    from paper_acquisition.locking import RunLock
    from paper_acquisition.attachments import service as attachments
    root = Path(job["catalog"]).resolve()
    workspace = Path(job["workspace"]).resolve()
    if root != workspace / "workspace/runs" / job["id"]:
        raise ValueError("任务记录的工作目录不一致")
    config.validate(job["providers"], credentials=not offline)
    config.activate()
    path = job_path(config, job["id"])
    def save():
        job["updated_at"] = now()
        atomic_json(path, job)
        refresh_project_index(config, overview_job=job)
    def stage(name, label, function):
        job["stages"][name] = {"status": "RUNNING", "started_at": now()}
        save()
        emit(label)
        report = function()
        job["stages"][name].update(status="FINISHED", finished_at=now(), report=report)
        save()
        return report
    with RunLock(path.parent / "job.lock"), working_directory(workspace):
        job["status"] = "RUNNING"
        save()
        try:
            settings = Settings(providers=job["providers"], offline=offline)
            if (root / "catalog.sqlite").is_file():
                config_run = collection_config(root)
                if not config_run:
                    raise ValueError("原检索运行缺少 collection.json")
                destination = (root / config_run["result"]).resolve()
                report = stage("discovery", "继续原检索与自动下载，保留查询条件和已完成结果。",
                               lambda: asyncio.run(resume_collection(destination, settings)))
                if retry and not offline:
                    report = stage("automatic_retry", "按要求重试原自动渠道的失败项。",
                                   lambda: asyncio.run(acquire_collection(root, workspace, retry_failed=True)))
            else:
                if resume:
                    emit("上次尚未建立检索数据库，按已保存的原始条件开始。")
                report = stage("discovery", "开始原 literature_gain 检索、筛选、去重与自动下载。",
                               lambda: asyncio.run(run_search(SearchProfile(**job["profile"]), settings,
                                                              workspace, label=job["label"], root=root)))
                destination = Path(report["result_dir"]).resolve()
            job["result_dir"] = str(destination)
            if not offline and report.get("discovery_status") != "NO_DATA":
                stage("browser_main", "自动读取正文清单，按 target_path 下载剩余主 PDF。",
                      lambda: download_main(root, config, retry=retry, runner=main_runner, emit=emit))
            published = stage("publish_main", "回填原文献库，核验并发布正文与分类。",
                              lambda: publish_collection(root, compact=False))
            si_report = {}
            if job["include_si"]:
                if not offline:
                    stage("automatic_si", "执行原附件自动发现和下载，生成独立附件清单。",
                          lambda: asyncio.run(attachments.acquire(root, retry_failed=retry)))
                    si_report = stage("browser_si", "自动读取附件清单，按 target_dir 回填并发布附件。",
                                      lambda: download_si(root, config, retry=retry, runner=si_runner, emit=emit))
                elif attachments.enabled(root):
                    si_report = stage("publish_si", "仅离线导入已有附件并汇总。", lambda: attachments.local(root))
            if job.get("classification_rules"):
                from paper_acquisition.toolbox.classification import classify
                stage("classification", "按本次保存的关键词规则更新分类。",
                      lambda: classify(destination, by="keywords", rules=job["classification_rules"]))
            from paper_acquisition.storage import Catalog
            from paper_acquisition.workflow import discovery_checkpoint
            with Catalog(root) as catalog:
                discovery = discovery_checkpoint(catalog, job["providers"])
            summary = {"job": job["id"], "result_dir": str(destination), "catalog": str(root),
                       "main_total": published["total"], "main_available": published["available"],
                       "main_remaining": published["agent_required"],
                       "program_pending": published.get("program_pending", 0),
                       "si_files": si_report.get("files", 0),
                       "si_remaining": si_report.get("agent_required", 0),
                       "discovery": discovery, "offline": offline}
            pending = (summary["main_remaining"] or summary["program_pending"] or summary["si_remaining"]
                       or discovery["status"] == "PARTIAL" or si_report.get("import_errors")
                       or si_report.get("browser_import_errors"))
            job["status"] = "FINISHED_WITH_PENDING" if pending else "COMPLETED"
            summary["status"] = job["status"]
            job["summary"] = summary
            job["finished_at"] = now()
            run_json = destination / "run.json"
            native = json.loads(run_json.read_text(encoding="utf-8"))
            atomic_json(run_json, {**native, "integrated_workflow": summary})
            save()
            return summary
        except BaseException as error:
            job["status"] = "INTERRUPTED"
            job["error"] = {"type": type(error).__name__, "message": config.redact(str(error))[:400]}
            save()
            raise
