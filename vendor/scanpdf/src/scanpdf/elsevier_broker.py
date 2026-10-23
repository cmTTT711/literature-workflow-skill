"""Portable host for the original Elsevier long-lived CloakBrowser job loop."""
from pathlib import Path
import argparse,datetime,json,os,sys,time,uuid,threading
from dataclasses import asdict
from .config import Config,atomic_json
from .identity import Paper
from .publishers import route_for
from ._elsevier import session_broker as original

def process_stamp(pid):
    if os.name!="nt":
        try:return str(Path(f"/proc/{pid}").stat().st_ctime_ns)
        except OSError:return None
    import ctypes
    from ctypes import wintypes
    k=ctypes.WinDLL("kernel32",use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4;k.GetProcessTimes.restype=wintypes.BOOL
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.OpenProcess(0x1000,False,int(pid))
    if not handle:return None
    try:
        k.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype=wintypes.BOOL
        exit_code=wintypes.DWORD()
        if not k.GetExitCodeProcess(handle,ctypes.byref(exit_code)) or exit_code.value!=259:return None
        created,ended,kernel,user=(wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle,*[ctypes.byref(v) for v in (created,ended,kernel,user)]):return None
        return str((created.dwHighDateTime<<32)|created.dwLowDateTime)
    finally:k.CloseHandle(handle)

class ElsevierBroker:
    def __init__(self,config,emit=print):
        self.config=config;self.emit=emit
        self.root=config.home/"brokers"/config.school_id/"cloakbrowser"
        original.BROKER_ROOT=self.root
        self.directory=original.broker_dir("elsevier")
    def state(self):
        try:return json.loads((self.directory/"state.json").read_text(encoding="utf-8"))
        except (OSError,ValueError):return {}
    def running(self):
        state=self.state()
        if state.get("status") not in {"ready","busy"}:return False
        stamp=process_stamp(int(state.get("pid") or 0))
        if not stamp or stamp!=state.get("process_created_at"):return False
        try:return time.time()-(self.directory/"state.json").stat().st_mtime<15
        except OSError:return False
    def ensure(self):
        if self.running():
            self.emit("  复用原 Elsevier broker：同一 CloakBrowser 会话")
            return self
        state=self.state()
        stamp=process_stamp(int(state.get("pid") or 0))
        if stamp and stamp==state.get("process_created_at") and state.get("status") in {"ready","busy"}:
            raise RuntimeError("Elsevier broker 进程仍在运行但心跳过期；保留原会话，先检查日志，不另起浏览器")
        self.directory.mkdir(parents=True,exist_ok=True)
        lock=self.directory/"start.lock"
        try:
            from .owned_lock import acquire
            acquire(lock)
        except RuntimeError:
            deadline=time.monotonic()+65
            while time.monotonic()<deadline:
                if self.running():return self
                time.sleep(1)
            raise RuntimeError("Elsevier broker 启动锁尚未释放；请先检查 broker status")
        try:
            if self.running():return self
            os.environ["SCANPDF_HOME"]=str(self.config.home)
            os.environ["SCANPDF_BROKER_ROOT"]=str(self.root)
            self.emit("  启动原 Elsevier broker：CloakBrowser 146，保留长连接会话")
            child=original.start_broker_process(publisher="elsevier",browser_profile=str(self.config.profile("elsevier","cloakbrowser")),institution=self.config.school,ttl_seconds=259200,cwd=self.config.home)
            deadline=time.monotonic()+65
            while time.monotonic()<deadline:
                if self.running():return self
                if child.poll() is not None:break
                time.sleep(.5)
            raise RuntimeError("Elsevier broker 启动未完成，请查看 "+str(self.directory/"broker.err.log"))
        finally:lock.unlink(missing_ok=True)
    def submit(self,kind,paper=None,output=None,**extra):
        self.ensure();request_id=uuid.uuid4().hex;queue=self.directory/"queue";queue.mkdir(exist_ok=True)
        job={"id":request_id,"kind":kind,"paper":asdict(paper) if paper else None,"output":str(output) if output else "",**extra}
        atomic_json(queue/(request_id+".json"),job)
        done=queue/(request_id+".done.json");deadline=time.monotonic()+max(600,self.config.login_timeout+600)
        shown=set();log_path=self.directory/"broker.out.log"
        log_offset=log_path.stat().st_size if log_path.exists() else 0
        while time.monotonic()<deadline:
            if done.exists():
                result=json.loads(done.read_text(encoding="utf-8"));done.unlink(missing_ok=True);return result
            # Forward stage names / login request markers, never cookie values or signed URLs.
            if log_path.exists():
                with log_path.open(encoding="utf-8",errors="replace") as f:
                    f.seek(log_offset);lines=f.read();log_offset=f.tell()
                for line in lines.splitlines():
                    if "Elsevier 原引擎：" in line or "读取结束：" in line or line.startswith("LOGIN_REQUEST_ID="):
                        if line not in shown:self.emit(line);shown.add(line)
            if not self.running():raise RuntimeError("Elsevier broker 已退出；保留队列与日志供排障")
            time.sleep(.5)
        raise RuntimeError("Elsevier broker 本次任务等待超时；会话保留，先检查状态和日志，不重复启动浏览器")
    def stop(self):
        if not self.running():return {"status":"stopped"}
        (self.directory/"stop").touch()
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and self.running():time.sleep(.5)
        return {"status":"stop_requested" if self.running() else "stopped"}

def run_broker(config,ttl):
    from .scansci_session import ScanPDFSession
    from .browser import BrowserSession
    from .verification import verify_pdf
    from .elsevier import fetch_main
    route=route_for("10.1016/broker",config)
    broker=ElsevierBroker(config)
    directory=broker.directory;queue=directory/"queue";queue.mkdir(parents=True,exist_ok=True)
    session=ScanPDFSession(route,config,lambda message:print(message,flush=True));session._in_broker=True
    # The original downloader launches once; all queued records use this same context.
    BrowserSession.__enter__(session)
    state=asdict(original.BrokerState(publisher="elsevier",profile_dir=str(config.profile("elsevier","cloakbrowser")),pid=os.getpid(),queue_dir=str(queue),started_at=datetime.datetime.now().isoformat(timespec="seconds"),ttl_seconds=ttl))
    state.update(status="ready",process_created_at=process_stamp(os.getpid()),session_id=uuid.uuid4().hex)
    stop_heartbeat=threading.Event()
    def heartbeat():
        while not stop_heartbeat.is_set():
            state["heartbeat_at"]=datetime.datetime.now().isoformat(timespec="seconds")
            try:atomic_json(directory/"state.json",dict(state))
            except OSError as error:
                print("Elsevier broker 心跳写入暂不可用："+type(error).__name__,flush=True)
            stop_heartbeat.wait(2)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    deadline=time.monotonic()+ttl
    try:
        while time.monotonic()<deadline and not (directory/"stop").exists():
            jobs=sorted(p for p in queue.glob("*.json") if not p.name.endswith(".done.json"))
            for path in jobs:
                job=json.loads(path.read_text(encoding="utf-8"));state.update(status="busy",job_id=job["id"])
                try:
                    paper=Paper(**job["paper"]) if job.get("paper") else None
                    output=Path(job["output"]) if job.get("output") else None
                    if job["kind"]=="main":
                        accepted={};output.mkdir(parents=True,exist_ok=True)
                        def accept(body,url):
                            verdict=verify_pdf(body,paper.doi,paper.title)
                            if verdict.status!="verified":return False
                            candidate=output/"broker_verified.pdf";candidate.write_bytes(body)
                            accepted.update(candidate=str(candidate),url=url)
                            return True
                        result=fetch_main(session,paper,accept,output);result.update(accepted)
                    elif job["kind"]=="si":result=session.fetch_si(paper,output)
                    elif job["kind"]=="login":result=session.login(job["url"],job.get("wait_for_confirmation",False))
                    else:raise ValueError("Unknown broker job")
                    result.update(broker_session_id=state["session_id"],broker_pid=state["pid"],backend="cloakbrowser")
                except Exception as e:result={"status":"needs_manual","reason":type(e).__name__+": "+str(e)[:300]}
                atomic_json(queue/(job["id"]+".done.json"),result);path.unlink(missing_ok=True)
                state.update(status="ready",job_id=None)
            # Dispatch browser events while idle, preserving the challenge/session state.
            session.ctx.pages[0].wait_for_timeout(500) if session.ctx.pages else time.sleep(.5)
    finally:
        state["status"]="stopped";stop_heartbeat.set();thread.join(timeout=3)
        atomic_json(directory/"state.json",state)
        BrowserSession.__exit__(session)

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["run"]);p.add_argument("--home",type=Path,required=True);p.add_argument("--publisher",default="elsevier");p.add_argument("--browser-profile");p.add_argument("--institution");p.add_argument("--ttl",type=int,default=259200)
    args=p.parse_args();config=Config.load(args.home);config.validate();run_broker(config,args.ttl)
if __name__=="__main__":main()
