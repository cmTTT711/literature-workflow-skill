"""User command compatibility after consolidating CLI and storage helpers."""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from paper_acquisition.cli import main
from paper_acquisition.storage import file_hash


@pytest.mark.parametrize("command", ["run", "discover"])
def test_search_commands_preserve_profile_overrides(tmp_path, monkeypatch, capsys, command):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"topic": "original", "include_terms": ["ceramic"], "year_from": 2020}))
    search = AsyncMock(return_value={"status": "PARTIAL"})
    monkeypatch.setattr("paper_acquisition.workflow.run_search", search)
    monkeypatch.setattr("paper_acquisition.cli.discover", search)
    monkeypatch.setattr("paper_acquisition.cli.prepare_master", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "sys.argv",
        [
            "paper-acquisition",
            command,
            "--profile",
            str(profile),
            "--topic",
            "magnetic",
            "--year-to",
            "2025",
            "--max-results",
            "7",
            "--providers",
            "openalex,lens",
            "--offline",
        ],
    )
    main()
    actual, settings = search.call_args.args[:2]
    assert actual.topic == "magnetic" and actual.include_terms == ["ceramic"]
    assert (actual.year_from, actual.year_to, actual.max_results_per_provider) == (2020, 2025, 7)
    assert settings.offline and settings.providers == ["openalex", "lens"]
    assert json.loads(capsys.readouterr().out)["status"] == "PARTIAL"


def test_acquire_keeps_legacy_options_and_data_directory(tmp_path, monkeypatch, capsys):
    acquire = AsyncMock(return_value={"available": 2})
    monkeypatch.setattr("paper_acquisition.workflow.acquire_collection", acquire)
    root = tmp_path / "run"
    monkeypatch.setattr(
        "sys.argv",
        [
            "paper-acquisition",
            "acquire",
            "--data-dir",
            str(root),
            "--limit",
            "2",
            "--sources",
            "openalex,arxiv",
            "--paper-ids",
            "a,b",
            "--max-candidates",
            "4",
            "--retry-failed",
        ],
    )
    main()
    assert acquire.call_args.args == (root, tmp_path)
    assert acquire.call_args.kwargs == {
        "limit": 2,
        "sources": {"openalex", "arxiv"},
        "paper_ids": {"a", "b"},
        "max_candidates": 4,
        "retry_failed": True,
    }
    assert json.loads(capsys.readouterr().out) == {"available": 2}


def test_no_data_still_prints_json_and_exits_two(monkeypatch, capsys):
    monkeypatch.setattr(
        "paper_acquisition.workflow.run_search", AsyncMock(return_value={"status": "NO_DATA"})
    )
    monkeypatch.setattr("sys.argv", ["paper-acquisition", "run", "--topic", "empty", "--offline"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out) == {"status": "NO_DATA"}


def test_file_hash_does_not_read_whole_pdf_into_memory(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    body = b"sample data" * 100000
    path.write_bytes(body)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("Whole-file read is not allowed"))
    assert file_hash(path) == hashlib.sha256(body).hexdigest()
