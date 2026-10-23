from pathlib import Path
import json

def test_atomic_state_write_recovers_transient_windows_sharing_violation(tmp_path,monkeypatch):
    from scanpdf.config import atomic_json
    original=Path.replace
    calls=[]
    def replace(source,target):
        calls.append(source)
        if len(calls)<3:raise PermissionError('simulated Windows reader holds state file')
        return original(source,target)
    monkeypatch.setattr(Path,'replace',replace)
    path=tmp_path/'state.json'
    atomic_json(path,{'status':'busy'})
    assert json.loads(path.read_text())=={'status':'busy'}
    assert len(calls)==3 and not list(tmp_path.glob('*.tmp'))

def test_stale_heartbeat_does_not_start_second_browser_for_live_owner(tmp_path,monkeypatch):
    from scanpdf.config import Config
    import scanpdf.elsevier_broker as module
    broker=module.ElsevierBroker(Config(home=tmp_path))
    monkeypatch.setattr(broker,'running',lambda:False)
    monkeypatch.setattr(broker,'state',lambda:{'pid':123,'process_created_at':'same','status':'busy'})
    monkeypatch.setattr(module,'process_stamp',lambda pid:'same')
    import pytest
    with pytest.raises(RuntimeError,match='心跳过期'):broker.ensure()
