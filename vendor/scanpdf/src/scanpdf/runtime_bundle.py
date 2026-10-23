"""Install only public pinned browser binaries from a checked portable archive."""
from pathlib import Path,PurePosixPath
import hashlib,json,shutil,uuid,zipfile

ALLOWED=("camoufox/browsers/official/152.0.4-beta.30-ea52a02f/","cloakbrowser/chromium-146.0.7680.177.5/")

def install_bundle(config,archive):
    root=config.home/"browsers"
    root.mkdir(parents=True,exist_ok=True)
    staging=root/(".install-"+uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(archive) as z:
            manifest=json.loads(z.read("runtime-manifest.json"))
            if manifest.get("format")!=1:raise ValueError("不支持的浏览器安装包")
            names=[i.filename for i in z.infolist() if not i.is_dir() and i.filename!="runtime-manifest.json"]
            if len(names)!=len(set(names)) or set(names)!=set(manifest["files"]):raise ValueError("浏览器安装包目录不一致")
            if sum(i.file_size for i in z.infolist())>3_000_000_000:raise ValueError("浏览器安装包大小异常")
            for name in names:
                p=PurePosixPath(name)
                if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or not name.startswith(ALLOWED):raise ValueError("浏览器安装包包含非法路径")
            for n,name in enumerate(names,1):
                target=staging.joinpath(*PurePosixPath(name).parts);target.parent.mkdir(parents=True,exist_ok=True)
                h=hashlib.sha256();size=0
                with z.open(name) as src,target.open("wb") as dst:
                    while chunk:=src.read(1024*1024):dst.write(chunk);h.update(chunk);size+=len(chunk)
                expected=manifest["files"][name]
                if h.hexdigest()!=expected["sha256"] or size!=expected["size"]:raise ValueError("浏览器文件校验失败："+name)
                if n%200==0:print(f"已校验浏览器文件 {n}/{len(names)}",flush=True)
        for prefix in ALLOWED:
            source=staging/prefix;destination=root/prefix
            if not source.is_dir():raise ValueError("浏览器安装包缺少原版内核")
            if destination.exists():
                # Never touch an existing installation or an active browser.
                existing=[(name,info) for name,info in manifest["files"].items() if name.startswith(prefix)]
                if all((root/name).is_file() and (root/name).stat().st_size==info["size"] and hashlib.file_digest((root/name).open("rb"),"sha256").hexdigest()==info["sha256"] for name,info in existing):continue
                raise ValueError("浏览器目录已存在且内容不同，请使用新的数据目录，勿覆盖活动内核："+str(destination))
            destination.parent.mkdir(parents=True,exist_ok=True);source.rename(destination)
        from .runtimes import browsers
        result=browsers(config)
        if not result["ready"]:raise RuntimeError("内核文件已校验，但还需要安装 Chrome/Edge："+str(result))
        return result
    finally:
        if staging.exists():shutil.rmtree(staging)
