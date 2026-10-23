from __future__ import annotations
from pathlib import Path
import json, hashlib, datetime, shutil, traceback
from collections import defaultdict
from .config import atomic_json
from .identity import stem
from .publishers import route_for
from .verification import verify_pdf
from .network import metadata
from .scansci_session import ScanPDFSession

def valid_resume(record,paper):
    try:
        p=Path(record["path"]);b=p.read_bytes()
        return record.get("status")=="ok" and hashlib.sha256(b).hexdigest()==record["sha256"] and verify_pdf(b,paper.doi,paper.title).status=="verified"
    except (KeyError,OSError):return False

def valid_si_resume(rec):
    if rec.get("status")!="ok" or not rec.get("files"):return False
    try:return all(hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest()==f["sha256"] for f in rec["files"])
    except (OSError,KeyError):return False

def retire_replaced_si(previous,current,folder):
    # Remove only files this ledger created and still owns, after a complete result.
    if current.get("status") not in {"ok","zero"}:return
    keep={str(Path(f["path"]).resolve()) for f in current.get("files",[])}
    for old in previous.get("files",[]):
        try:
            path=Path(old["path"]).resolve()
            if path.is_relative_to(folder.resolve()) and str(path) not in keep and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==old["sha256"]:path.unlink()
        except (OSError,KeyError):pass


def run(papers,config,output=None,*,si=True,si_only=False,redo=False,respect_targets=False,emit=print,session_factory=ScanPDFSession):
    config.validate();out=Path(output or config.output_dir).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
    lock=out/".scanpdf-run.lock"
    try:
        from .owned_lock import acquire
        acquire(lock)
    except FileExistsError:raise RuntimeError(f"输出目录正在使用，或上次异常结束：{lock}")
    manifest_path=out/"manifest.json";si_path=out/"si"/"si_manifest.json"
    try:
        manifest=json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        si_manifest=json.loads(si_path.read_text(encoding="utf-8")) if si_path.exists() else {}
        groups=defaultdict(list)
        for p in papers:
            route=route_for(p.doi,config);groups[(route.publisher,route.backend)].append(p)
        summary={"requested":len(papers),"main_ok":0,"si_ok":0,"needs_attention":0,"records":[]}
        def flush():
            atomic_json(manifest_path,manifest);atomic_json(si_path,si_manifest)
        for (publisher,backend),group in groups.items():
            needed=[p for p in group if redo or (not si_only and not valid_resume(manifest.get(p.doi,{}),p)) or (si and not valid_si_resume(si_manifest.get(p.doi,{})))]
            if not needed:
                for p in group:
                    summary["main_ok"]+=int(valid_resume(manifest.get(p.doi,{}),p))
                    summary["si_ok"]+=int(valid_si_resume(si_manifest.get(p.doi,{})))
                    summary["records"].append({"doi":p.doi,"main":manifest.get(p.doi,{}).get("status"),"si":si_manifest.get(p.doi,{}).get("status"),"resumed":True})
                continue
            emit(f"[{publisher}] {backend}：{len(needed)} 篇，一个会话逐篇处理")
            try:
                with session_factory(route_for(group[0].doi,config),config,emit) as browser:
                    for paper in group:
                        entry=manifest.get(paper.doi,{})
                        if not paper.title and entry.get("title"):paper.title=entry["title"]
                        if not paper.article_url and entry.get("article_url"):paper.article_url=entry["article_url"]
                        metadata(paper,config)
                        main_ok=valid_resume(entry,paper) and (si_only or not redo)
                        work=out/"quarantine"/stem(paper.doi);work.mkdir(parents=True,exist_ok=True)
                        if not si_only and not main_ok:
                            metadata(paper,config)
                            accepted={}
                            attempts=[]
                            def accept(body,url):
                                if not body.startswith(b"%PDF-"):return False
                                v=verify_pdf(body,paper.doi,paper.title)
                                attempts.append({"source_url":url,"article_url":paper.article_url or route_for(paper.doi,config).article,**v.as_dict()})
                                if v.status!="verified":
                                    q=work/(v.sha256[:12]+".pdf")
                                    if not q.exists():q.write_bytes(body)
                                    return False
                                dest=Path(paper.target_path).expanduser().resolve() if respect_targets and paper.target_path else out/"papers"/(stem(paper.doi)+".pdf")
                                dest.parent.mkdir(parents=True,exist_ok=True)
                                if dest.exists() and dest.read_bytes()!=body:
                                    old=verify_pdf(dest.read_bytes(),paper.doi,paper.title)
                                    if old.status!="verified":
                                        shutil.copy2(dest,work/("previous_"+old.sha256[:12]+".pdf"))
                                tmp=dest.with_suffix(".pdf.part");tmp.write_bytes(body);tmp.replace(dest)
                                accepted.update({"doi":paper.doi,"title":paper.title,"publisher":publisher,"backend":backend,"status":"ok","path":str(dest),"source_url":url,"article_url":paper.article_url or route_for(paper.doi,config).article,**v.as_dict()})
                                accepted["status"]="ok"
                                return True
                            try:
                                for prior in entry.get("attempts",[]):
                                    q=work/(str(prior.get("sha256",""))[:12]+".pdf")
                                    if q.exists() and hashlib.sha256(q.read_bytes()).hexdigest()==prior.get("sha256") and accept(q.read_bytes(),prior.get("source_url","")):
                                        emit("  已重新核验本次历史候选，恢复主件落位")
                                        break
                                if accepted:result={"status":"ok"}
                                else:result=browser.fetch_main(paper,accept)
                                if accepted:
                                    accepted.update({k:result[k] for k in ("source","engine","engine_state","log_path","broker_session_id","broker_pid") if k in result})
                                entry=accepted or {"doi":paper.doi,"title":paper.title,"publisher":publisher,"backend":backend,**result,"attempts":attempts}
                            except Exception as e:
                                entry={"doi":paper.doi,"status":"failed","reason":type(e).__name__+": "+str(e)[:240],"publisher":publisher,"attempts":attempts}
                            manifest[paper.doi]=entry;main_ok=entry.get("status")=="ok";flush()
                            emit(f"  {paper.doi} 主件：{entry.get('status')} ({entry.get('reason','')})")
                        if si and (redo or not valid_si_resume(si_manifest.get(paper.doi,{}))):
                            try:
                                si_folder=out/"si"/stem(paper.doi)
                                previous_si=si_manifest.get(paper.doi,{})
                                new_si=browser.fetch_si(paper,si_folder)
                                si_manifest[paper.doi]=new_si
                                retire_replaced_si(previous_si,new_si,si_folder)
                            except Exception as e:si_manifest[paper.doi]={"status":"needs_manual","reason":type(e).__name__+": "+str(e)[:240],"files":[]}
                            si_manifest[paper.doi].update(doi=paper.doi,publisher=publisher)
                            flush();emit(f"  {paper.doi} 附件：{si_manifest[paper.doi]['status']}")
                        summary["main_ok"]+=int(main_ok)
                        si_state=si_manifest.get(paper.doi,{}).get("status","not_requested")
                        summary["si_ok"]+=int(si_state=="ok")
                        if (not main_ok and not si_only) or (si and si_state not in {"ok","zero"}):summary["needs_attention"]+=1
                        summary["records"].append({"doi":paper.doi,"main":entry.get("status","not_requested"),"si":si_state})
            except Exception as e:
                emit(f"[{publisher}] 会话失败：{type(e).__name__}: {e}")
                for paper in group:
                    if paper.doi not in {x['doi'] for x in summary['records']}:
                        if not si_only and not valid_resume(manifest.get(paper.doi,{}),paper):manifest[paper.doi]={"doi":paper.doi,"status":"needs_manual","reason":"browser_session_unavailable","error":type(e).__name__+": "+str(e)[:240]}
                        if si and not valid_si_resume(si_manifest.get(paper.doi,{})):si_manifest[paper.doi]={"doi":paper.doi,"status":"needs_manual","reason":"browser_session_unavailable","files":[]}
                        summary["needs_attention"]+=1
                        summary['records'].append({'doi':paper.doi,'main':manifest.get(paper.doi,{}).get('status'),'si':si_manifest.get(paper.doi,{}).get('status')})
                flush()
        summary["finished_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
        atomic_json(out/"last_run.json",summary)
        return summary
    finally:lock.unlink(missing_ok=True)
