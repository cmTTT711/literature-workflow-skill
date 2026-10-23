import pytest

from paper_acquisition.acquisition.layout import agent_target, initialize_layout
from paper_acquisition.models import Publisher


def test_physical_channel_separation(tmp_path):
    initialize_layout(tmp_path)
    path = agent_target(tmp_path, Publisher.APS, "10.1234/abcdef")
    assert path.is_relative_to(tmp_path / "downloads" / "agent")
    assert not path.is_relative_to(tmp_path / "downloads" / "program")
    assert (tmp_path / "downloads" / "program" / "staging").is_dir()
    assert not (tmp_path / "downloads" / "agent" / "sage").exists()
    with pytest.raises(ValueError):
        agent_target(tmp_path, Publisher.APS, "../../escape")
