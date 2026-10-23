"""Optional live checks. Missing credentials are explicit skips, never claimed successes."""

import asyncio
import os

import pytest

from paper_acquisition.config import Settings, credential
from paper_acquisition.discovery import discover
from paper_acquisition.models import SearchProfile
from paper_acquisition.providers import registry


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(registry()))
def test_live_provider(name, tmp_path):
    if os.environ.get("PAPER_LIVE") != "1":
        pytest.skip("Live network disabled; PAPER_LIVE=1 to opt in")
    cls = registry()[name]
    if cls.key_required and not credential(cls.key_env):
        pytest.skip("NOT_TESTED_WITH_LIVE_API: missing " + cls.key_env)
    if name == "core":
        pytest.skip("NOT_TESTED_WITH_LIVE_API: official wire docs blocked")
    report = asyncio.run(
        discover(
            SearchProfile(topic="magnetoelectric coupling", max_results_per_provider=1),
            Settings(data_dir=tmp_path, providers=[name], attempts=1),
        )
    )
    assert report["providers"][0]["error"] is None
    assert report["providers"][0]["status"] in {"SUCCESS", "TRUNCATED"}
