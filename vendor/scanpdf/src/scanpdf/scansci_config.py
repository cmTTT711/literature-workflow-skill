"""Translate portable settings into the user's original ScanSci configuration."""
from pathlib import Path
from .config import atomic_json

def engine_config(config,route,output):
    from ._scansci.config import DEFAULT_CONFIG
    values=dict(DEFAULT_CONFIG)
    root=config.home/"scansci"/config.school_id/route.backend/route.publisher
    values.update(email=config.email,output_dir=str(output),cache_dir=str(root/"cache"),
        browser_backend=route.backend,browser_executable=config.browser_executable,
        browser_headless=config.headless,chrome_profile_dir=str(config.profile(route.publisher,route.backend)),
        carsi_enabled=config.access=="carsi",carsi_idp_name=config.school,
        vpnsci_school=config.school,is_campus_network=config.access=="campus",
        network_proxy=config.network_proxy,download_strategy="legal_only",scihub_enabled=False,
        batch_default_lanes=False,download_si=False,progress_bar_auto=False,
        browser_login_timeout=config.login_timeout,challenge_wait_seconds=config.challenge_wait,
        auto_rename=True)
    # School's official institution URL is user-configured, never guessed from a school substring.
    if config.access=="institution":values["institution_login_url"]=config.institution_url
    return root,values

def write_engine_config(config,route,output,paper=None):
    root,values=engine_config(config,route,output)
    if paper:
        article={"url":paper.article_url or route.article,"title":paper.title}
        values["_portable_articles"]={paper.doi:article}
        if paper.doi.startswith("10.1103/"):
            from ._scansci.publisher_profiles import _aps_canonical_doi
            values["_portable_articles"][_aps_canonical_doi(paper.doi)]=article
    atomic_json(root/"config.json",values)
    return root,values
