from pathlib import Path
import shutil,sqlite3

def import_session(source: Path,target: Path,backend: str):
    source=source.expanduser().resolve();target=target.resolve()
    if source==target or source in target.parents or target in source.parents:raise ValueError("源会话和目标会话必须相互独立")
    if not source.is_dir():raise ValueError("源会话目录不存在")
    firefox=(source/"prefs.js").exists() or (source/"cookies.sqlite").exists()
    chromium=(source/"Default").is_dir() or (source/"Local State").exists()
    if (backend=="camoufox" and not firefox) or (backend!="camoufox" and not chromium):raise ValueError("源会话没有目标浏览器需要的数据，请重新登录或显式导入 cookies")
    if target.exists() and any(target.iterdir()):raise ValueError("目标会话已有数据；为避免覆盖，请使用新的 --home")
    excluded={"Cache","Code Cache","GPUCache","Service Worker","ShaderCache","GrShaderCache","Crashpad","BrowserMetrics"}
    def ignore(path,names):return [n for n in names if n in excluded or n.startswith("Singleton") or n in {"lock",".parentlock","parent.lock","LOCK"}]
    shutil.copytree(source,target,ignore=ignore,dirs_exist_ok=True)
    # Back up databases consistently if the source has a WAL journal.
    for f in source.rglob("*"):
        if f.is_file() and f.name in {"Cookies","cookies.sqlite","Login Data","History"}:
            dest=target/f.relative_to(source)
            if not dest.parent.exists():continue
            try:
                src=sqlite3.connect(f"file:{f.as_posix()}?mode=ro",uri=True);dst=sqlite3.connect(dest)
                src.backup(dst);dst.close();src.close()
                for suffix in ["-wal","-shm"]:Path(str(dest)+suffix).unlink(missing_ok=True)
            except sqlite3.Error:pass
    return {"status":"copied_not_yet_verified","profile":str(target),"source_preserved":True,"backend":backend}
