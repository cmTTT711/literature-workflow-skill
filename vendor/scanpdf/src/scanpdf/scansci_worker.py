"""Run the bundled original single-paper get engine in an isolated process."""
import argparse,os,json
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument("--config-root",required=True);p.add_argument("--doi",required=True);p.add_argument("--output",required=True);p.add_argument("--result",required=True)
    a=p.parse_args();os.environ["SCANSCI_PDF_DATA_DIR"]=a.config_root
    from .config import Config
    from .runtimes import isolate_camoufox,locked_camoufox
    portable_config=Config.load()
    cfg=json.loads((Path(a.config_root)/"config.json").read_text(encoding="utf-8"))
    if cfg.get("browser_backend")=="camoufox":locked_camoufox(portable_config)
    else:isolate_camoufox(portable_config)
    from ._scansci.sources import download
    from .config import atomic_json
    try:
        result=download(a.doi,a.output,scihub_enabled=False,use_tor=False,rename=True,strategy="legal_only")
        atomic_json(Path(a.result),result)
    except Exception as e:
        atomic_json(Path(a.result),{"success":False,"error":type(e).__name__+": "+str(e)[:300]})
    finally:
        from ._scansci.browser_engine import _reap_browsers_at_exit
        _reap_browsers_at_exit()
if __name__=="__main__":main()
