"""A local lock records process creation time so a recycled PID cannot own it."""
import json,os
from .elsevier_broker import process_stamp

def acquire(path):
    for _ in range(2):
        try:
            with path.open("x",encoding="utf-8") as f:json.dump({"pid":os.getpid(),"created":process_stamp(os.getpid())},f)
            return
        except FileExistsError:
            try:owner=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,ValueError):raise RuntimeError("遗留锁无进程记录，请确认相关任务已退出后处理："+str(path))
            if not owner.get("created") or process_stamp(int(owner.get("pid",0)))==owner["created"]:raise RuntimeError("任务或会话正在使用："+str(path))
            path.unlink(missing_ok=True)
    raise RuntimeError("无法取得会话锁："+str(path))
