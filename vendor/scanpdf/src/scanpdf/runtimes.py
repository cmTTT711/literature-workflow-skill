from pathlib import Path
import os,sys

def isolate_camoufox(config):
    import camoufox.pkgman as pkg
    old=Path(pkg.INSTALL_DIR)
    new=config.home/"browsers"/"camoufox";new.mkdir(parents=True,exist_ok=True)
    pkg.INSTALL_DIR=new
    import camoufox.multiversion
    for name,module in list(sys.modules.items()):
        if not name.startswith("camoufox.") or module is None:continue
        for key,value in list(vars(module).items()):
            if isinstance(value,Path):
                try:relative=value.relative_to(old)
                except ValueError:continue
                setattr(module,key,new/relative)
    return pkg

def locked_camoufox(config,install=False):
    import json
    pkg=isolate_camoufox(config)
    lock=json.loads((Path(__file__).parent/"data"/"runtime-lock.json").read_text(encoding="utf-8"))["camoufox"]
    from camoufox.multiversion import find_installed_version,set_active,COMPAT_FLAG
    path=find_installed_version(lock["version"]+"-"+lock["build"])
    if path is None:
        if not install:raise FileNotFoundError("需要原流程 Camoufox "+lock["version"]+"-"+lock["build"]+"；运行 scanpdf browsers --install")
        selected=pkg.AvailableVersion(version=pkg.Version(version=lock["version"],build=lock["build"]),url=lock["url"],is_prerelease=False,sha256=lock["sha256"])
        class BoundedFetcher(pkg.CamoufoxFetcher):
            @staticmethod
            def download_file(file,url):
                # Keep the original installer and SHA check; bound transport retries only.
                import requests,time
                last=None
                for attempt in range(3):
                    file.seek(0);file.truncate()
                    try:
                        print(f"下载原 Camoufox 内核（尝试 {attempt+1}/3）",flush=True)
                        with requests.get(url,stream=True,timeout=(15,45)) as response:
                            response.raise_for_status()
                            for chunk in response.iter_content(1024*1024):file.write(chunk)
                        file.seek(0);return file
                    except requests.RequestException as error:
                        last=error
                        if attempt<2:time.sleep(2)
                raise RuntimeError("原 Camoufox 下载未完成，请保持 GitHub 代理后重试 browsers --install："+type(last).__name__) from last
        BoundedFetcher(selected_version=selected).install()
        path=find_installed_version(lock["version"]+"-"+lock["build"])
    if path is None:raise FileNotFoundError("原 Camoufox 内核安装后未找到")
    COMPAT_FLAG.parent.mkdir(parents=True,exist_ok=True)
    COMPAT_FLAG.touch()  # Same marker written by the original versioned installer.
    set_active(Path(path).relative_to(pkg.INSTALL_DIR).as_posix())
    return Path(path)

def locked_cloakbrowser(config,install=False):
    from ._elsevier.cloakbrowser_compat import configure_builtin_cloakbrowser,ensure_cloakbrowser_platform_compatible
    os.environ["CLOAKBROWSER_VERSION"]="146.0.7680.177.5"
    os.environ["CLOAKBROWSER_AUTO_UPDATE"]="false"
    os.environ["CLOAKBROWSER_CACHE_DIR"]=str(config.home/"browsers"/"cloakbrowser")
    # Apply only inside this tool process; never inherit an unrelated plugin's binary override.
    os.environ.pop("CLOAKBROWSER_BINARY_PATH",None)
    configure_builtin_cloakbrowser(config.home/"browsers"/"cloakbrowser");ensure_cloakbrowser_platform_compatible()
    from cloakbrowser.config import get_binary_path,get_chromium_version
    if get_chromium_version()!="146.0.7680.177.5":raise RuntimeError("CloakBrowser 内核版本未与原流程对齐，请按随包锁文件安装")
    path=Path(get_binary_path())
    if not path.exists() and install:
        from cloakbrowser.download import ensure_binary
        path=Path(ensure_binary(browser_version="146.0.7680.177.5"))
        if path.resolve()!=Path(get_binary_path("146.0.7680.177.5")).resolve():raise RuntimeError("官方安装返回了非固定版本；请检查本机 CloakBrowser 授权配置，不会静默替换内核")
    if not path.exists():raise FileNotFoundError("CloakBrowser 146 内核尚未下载；运行 scanpdf browsers --install")
    return path

def browsers(config,install=False):
    from ._scansci.browser_backend import find_local_browser
    report={"chromium":config.browser_executable or find_local_browser()}
    try:report["camoufox"]=str(locked_camoufox(config,install))
    except Exception as e:report["camoufox_error"]=type(e).__name__+": "+str(e)
    try:report["cloakbrowser"]=str(locked_cloakbrowser(config,install))
    except Exception as e:report["cloakbrowser_error"]=type(e).__name__+": "+str(e)
    report["ready"]=bool(report.get("chromium") and report.get("camoufox") and report.get("cloakbrowser"))
    return report
