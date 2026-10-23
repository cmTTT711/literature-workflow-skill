from ..errors import DocumentationBlocked
from .base import DiscoveryProvider


class Adapter(DiscoveryProvider):
    """Explicitly gated until official v3 wire documentation can be inspected.

    Official landing page confirms API/key availability, but both documentation
    URLs returned 403 on 2026-09-14. Do not substitute guessed wire contracts.
    """

    name, key_env, key_required, interval = "core", "CORE_API_KEY", True, 10.1

    def check(self):
        super().check()
        raise DocumentationBlocked("CORE_OFFICIAL_WIRE_DOCS_BLOCKED_403")

    def translate(self, profile, query):
        raise DocumentationBlocked("CORE_OFFICIAL_WIRE_DOCS_BLOCKED_403")

    async def fetch(self, translated, cursor, size):
        raise DocumentationBlocked("CORE_OFFICIAL_WIRE_DOCS_BLOCKED_403")

    def normalize(self, record):
        raise DocumentationBlocked("CORE_OFFICIAL_WIRE_DOCS_BLOCKED_403")
