"""Provider registry auto-discovers one Adapter per module; core stays unchanged."""

import importlib
import pkgutil

DISCOVERY_NAMES = frozenset({"openalex", "scopus", "wos", "semantic_scholar", "lens"})


def registry(*, include_metadata_sources=False):
    result = {}
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_") or module.name == "base":
            continue
        loaded = importlib.import_module(f"{__name__}.{module.name}")
        if hasattr(loaded, "Adapter") and (
            include_metadata_sources or loaded.Adapter.name in DISCOVERY_NAMES
        ):
            result[loaded.Adapter.name] = loaded.Adapter
    return result
