"""One user-facing project layout and index; native catalogs remain authoritative."""
from pathlib import Path
import json

from .config import atomic_json

LAYOUT = ("pdfs/all", "pdfs/automatic", "pdfs/agent", "attachment/all",
          "attachment/automatic", "attachment/agent", "classifications/by_publisher", "agent_tasks")
STATUS_NAMES = {"CREATED": "待开始", "RUNNING": "运行中", "COMPLETED": "已完成",
                "FINISHED_WITH_PENDING": "有待办", "INTERRUPTED": "已中断"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def project_overview(job, workspace):
    result = Path(job["result_dir"]).resolve()
    if result.parent != workspace / "results":
        raise ValueError("项目成果目录超出正式成果区")
    if not result.is_dir():
        return
    for relative in LAYOUT:
        (result / relative).mkdir(parents=True, exist_ok=True)
    native = read(result / "run.json") if (result / "run.json").exists() else {}
    summary = job.get("summary", {})
    attachment = native.get("attachments") or {}
    lines = [f"# {job.get('label') or job['profile']['topic']}", "",
             f"任务编号：`{job['id']}`。状态：{STATUS_NAMES.get(job['status'], job['status'])}。", "",
             f"正文：{native.get('available', summary.get('main_available', 0))} / "
             f"{native.get('total', summary.get('main_total', 0))} 篇；"
             f"正文待办：{native.get('agent_required', summary.get('main_remaining', 0))} 篇。", "",
             "| 内容 | 位置 |", "|---|---|", "| 全部已核验正文 | [pdfs/all](pdfs/all/) |",
             "| 自动渠道 / 浏览器渠道正文 | pdfs/automatic / pdfs/agent |",
             "| 已取得的附件 | [attachment/all](attachment/all/) |",
             "| 按出版社及关键词分类 | [classifications](classifications/) |",
             "| 剩余正文和附件清单 | [agent_tasks](agent_tasks/) |", ""]
    if attachment:
        lines.append(f"有效附件 {attachment.get('files', 0)} 份；附件待办 {attachment.get('agent_required', 0)} 篇。")
    else:
        lines.append("附件目录为空时，只表示本项目尚无已发布附件，不表示论文没有附件。")
    lines.extend(["", "恢复同一任务使用 resume --job " + job["id"] + "，沿用原检索条件和接收路径。",
                  "内部运行记录和共享文献库在工作区的 workspace 目录；学校登录态和 API 凭据不放在成果目录。", ""])
    (result / "项目说明.md").write_text("\n".join(lines), encoding="utf-8-sig")


def refresh_project_index(config, *, overview_job=None):
    workspace = Path(config.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted((config.home / "jobs").glob("*/state.json")):
        job = read(path)
        if job.get("project_kind", "formal") != "formal" or Path(job["workspace"]).resolve() != workspace:
            continue
        if job.get("result_dir") and (overview_job is None or overview_job["id"] == job["id"]):
            project_overview(job, workspace)
        summary = job.get("summary", {})
        rows.append({"id": job["id"], "name": job.get("label") or job["profile"]["topic"],
                     "years": [job["profile"].get("year_from"), job["profile"].get("year_to")],
                     "status": job["status"], "result_dir": job.get("result_dir"),
                     "main_available": summary.get("main_available", 0),
                     "main_total": summary.get("main_total", 0),
                     "main_remaining": summary.get("main_remaining", 0),
                     "si_files": summary.get("si_files", 0)})
    rows.sort(key=lambda row: (row["years"][0] or 0, row["name"], row["id"]))
    atomic_json(workspace / "projects.json", {"version": 1, "projects": rows})
    lines = ["# 正式项目清单", "", "所有正式项目使用相同目录结构；测试、验收和归档任务不放入 results。", "",
             "| 项目 | 正文 | 正文待办 | 已有附件 | 状态 |", "|---|---:|---:|---:|---|"]
    for row in rows:
        start, end = row["years"]
        years = str(start) if start == end and start else f"{start or '不限'}–{end or '不限'}"
        title = (row["name"] + " · " + years).replace("|", "\\|").replace("\n", " ")
        link = f"[{title}](<{Path(row['result_dir']).as_posix()}/>)" if row["result_dir"] else title
        lines.append(f"| {link} | {row['main_available']}/{row['main_total']} | {row['main_remaining']} | "
                     f"{row['si_files']} | {STATUS_NAMES.get(row['status'], row['status'])} |")
    lines.extend(["", "项目文件夹命名：主题_年份_创建时间_唯一标识。每个项目都包含项目说明、正文、附件、分类和待办清单。",
                  "目录中的空附件文件夹不表示论文没有附件；实际结果以任务记录为准。", ""])
    (workspace / "项目清单.md").write_text("\n".join(lines), encoding="utf-8-sig")
    return rows
