import pytest


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    # Fake downloader tests must not compete with a real acquisition's locks,
    # cooldown files or user data when the suite runs during a batch.
    monkeypatch.chdir(tmp_path)
