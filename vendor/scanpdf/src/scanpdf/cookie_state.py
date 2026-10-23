"""Keep local session cookies across browser exits and feed the original engine."""
from pathlib import Path
import json,time
from .config import atomic_json

def state_path(config,route):
    profile=config.profile(route.publisher,route.backend)
    return profile.parent/(profile.name+".cookies.json")

def missing_cookies(saved,live):
    key=lambda cookie:(cookie.get("name"),cookie.get("domain", "").lstrip("."),cookie.get("path","/"))
    present={key(cookie) for cookie in live}
    allowed={"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    result=[]
    for cookie in saved:
        if not cookie.get("domain") or key(cookie) in present:continue
        expiry=cookie.get("expires",-1)
        if expiry and expiry>0 and expiry<time.time():continue
        clean={k:v for k,v in cookie.items() if k in allowed}
        if clean.get("expires",-1)<=0:clean.pop("expires",None)
        result.append(clean)
    return result

def restore(context,config,route):
    path=state_path(config,route)
    if not path.exists():return
    saved=json.loads(path.read_text(encoding="utf-8")).get("cookies",[])
    cookies=missing_cookies(saved,context.cookies())
    if cookies:context.add_cookies(cookies)

def save(context,config,route):
    state=context.storage_state()
    atomic_json(state_path(config,route),state)
    from .scansci_config import engine_config
    from ._scansci._publisher_strategies_core import _save_all_cookie_formats
    _,raw=engine_config(config,route,config.home/"runs")
    cookies=state.get("cookies",[])
    publishers={route.publisher}
    if route.publisher=="springer":
        publishers.update("nature" if "nature.com" in c.get("domain","") else "springer" for c in cookies)
    for publisher in publishers:_save_all_cookie_formats(cookies,publisher,raw,bridge=False)
    return state_path(config,route)
