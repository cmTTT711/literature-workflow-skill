"""Standalone CARSI download source — decoupled from WebVPN.

CARSI can now be used independently: set carsi_enabled=True and carsi_idp_name,
and it will be tried in the download tier system without requiring instsci_enabled.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from ..log import get_logger

log = get_logger()


# Publisher key: (path regex, canonical path template).
# A resolver/mirror URL carries the article's PII (or DOI suffix) but not the
# path the primary domain actually serves, so the path has to be translated —
# rewriting only the host yields a 404 like
# sciencedirect.com/retrieve/pii/S0272... instead of
# www.sciencedirect.com/science/article/pii/S0272...
_PATH_REWRITES: dict[str, tuple[str, str]] = {
    "sciencedirect": (r"/pii/([A-Za-z0-9]+)", "/science/article/pii/{0}"),
}


def _rebuild_on_primary_domain(url: str, publisher: str, primary_domain: str) -> str:
    """Rebuild a resolver URL on the publisher's primary domain.

    Keeps the article identity (PII / DOI / article id) while translating the
    path to the canonical form the primary domain actually serves.
    """
    parsed = urlparse(url)
    host = primary_domain
    path = parsed.path or "/"

    # Resolver hosts carry no publisher path to translate — rewriting them
    # would fabricate a bogus URL, so leave them untouched.
    if (parsed.hostname or "").lower() in ("doi.org", "dx.doi.org"):
        return url

    rewrite = _PATH_REWRITES.get(publisher)
    if rewrite:
        match = re.search(rewrite[0], path, re.IGNORECASE)
        if match:
            path = rewrite[1].format(match.group(1))
    if not host.startswith("www."):
        host = "www." + host

    return urlunparse(parsed._replace(scheme="https", netloc=host, path=path))


def try_carsi(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth without WebVPN dependency.

    Returns a result dict on success, None on failure.
    """
    if not config.get("carsi_enabled", False):
        return None

    idp_name = config.get("carsi_idp_name", "").strip()
    if not idp_name:
        return None

    try:
        from .carsi import CARSIClient, detect_publisher
        from .instsci import _resolve_doi_url

        resolved_url = _resolve_doi_url(doi)
        if not resolved_url:
            resolved_url = f"https://doi.org/{doi}"

        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None

        log.info(f"   [CARSI] Trying {publisher} via {idp_name} for {doi}")
        client = CARSIClient(config)

        # If resolved URL is on a mirror/proxy domain, rebuild using primary domain
        from urllib.parse import urlparse
        cfg = client._publisher_configs.get(publisher)
        if cfg:
            resolved_host = urlparse(resolved_url).hostname or ""
            primary_domain = cfg.domains[0]
            if resolved_host and primary_domain not in resolved_host:
                resolved_url = _rebuild_on_primary_domain(
                    resolved_url, publisher, primary_domain)
                log.info(f"   [CARSI] Redirected to primary domain: {resolved_url[:80]}")

        # Try browser download (CloakBrowser first, Selenium fallback)
        result = client.download_via_browser(doi, resolved_url, output_path)
        if result:
            return result
    except ImportError:
        return None
    except Exception as e:
        log.info(f"   [CARSI] {e}")
    return None
