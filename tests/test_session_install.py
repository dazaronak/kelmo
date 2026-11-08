"""Session hooks upgrade and removal preserve both client profiles."""
import json
from types import SimpleNamespace
import pytest
from kelmo.errors import KelmoError
from kelmo.install.setup import configure_clients


def hook_members(home,client,event):
    path = home / ('.claude/settings.json' if client=='claude' else '.codex/hooks.json')
    return [h for group in json.loads(path.read_text())['hooks'].get(event,[]) for h in group.get('hooks',[])]


@pytest.mark.parametrize('client',['claude','codex'])
def test_session_upgrade_replaces_capture_and_uninstall_removes_new_hooks(tmp_path,client):
    from kelmo.install.removal import uninstall
    exe,vault,home = tmp_path / 'kelmo',tmp_path/'vault',tmp_path/'home'
    configure_clients(home,exe,vault,client,True)
    assert 'capture' in hook_members(home,client,'Stop')[0]['command']
    configure_clients(home,exe,vault,client,True,session_mode='auto')
    for event in ('Stop','PreCompact','SessionEnd'):
        members = hook_members(home,client,event)
        assert len(members)==1
        assert '--provider '+client in members[0]['command']
        assert 'capture' not in members[0]['command']
    configure_clients(home,exe,vault,client,True,session_mode='auto')
    assert len(hook_members(home,client,'Stop'))==1
    result = uninstall(SimpleNamespace(home=home,executable=exe,vault=vault,client=client,apply=True))
    assert result['applied'] is True
    for event in ('SessionStart','Stop','PreCompact','SessionEnd'):
        assert hook_members(home,client,event)==[]


def test_off_removes_session_hooks_but_keeps_hot(tmp_path):
    home,exe,vault = tmp_path/'home',tmp_path / 'kelmo',tmp_path/'vault'
    configure_clients(home,exe,vault,'both',True,session_mode='auto')
    configure_clients(home,exe,vault,'both',True,session_mode='off')
    for client in ('claude','codex'):
        assert len(hook_members(home,client,'SessionStart'))==1
        for event in ('Stop','PreCompact','SessionEnd'):
            assert hook_members(home,client,event)==[]


def test_modified_managed_hook_stops_upgrade_before_write(tmp_path):
    home,exe,vault = tmp_path/'home',tmp_path / 'kelmo',tmp_path/'vault'
    configure_clients(home,exe,vault,'both',True)
    path=home/'.codex/hooks.json'
    data=json.loads(path.read_text())
    data['hooks']['Stop'][0]['hooks'][0]['command']='user changed command'
    path.write_text(json.dumps(data))
    before={p:p.read_bytes() for p in home.rglob('*') if p.is_file()}
    with pytest.raises(KelmoError):
        configure_clients(home,exe,vault,'both',True,session_mode='auto')
    assert before=={p:p.read_bytes() for p in home.rglob('*') if p.is_file()}


def test_setup_enables_and_preserves_mode_then_disables(tmp_path,monkeypatch):
    import sys
    from kelmo.service import Service
    from kelmo.interface import build_parser
    from kelmo.config import Config
    from kelmo.install import setup, session_setup
    vault=tmp_path/'vault'
    Service.initialize(vault,'fixture')
    (vault/'.cache/embeddings/model').mkdir(parents=True)
    profile=tmp_path/'executor.json'
    profile.write_text(json.dumps({'argv':[sys.executable,'-c','print()']}))
    monkeypatch.setattr('kelmo.cli.dispatch',lambda *a,**k:{'healthy':True})
    monkeypatch.setattr('kelmo.daemon.execute',lambda *a,**k:{'ok':True})
    probes=[]
    def probe(*args):
        probes.append(args)
        return {'extraction_ready':True,'session_pipeline_ready':True,'native_hooks_verified':False}
    monkeypatch.setattr(session_setup,'probe',probe)
    base=['setup','--vault',str(vault),'--home',str(tmp_path/'home'),'--executable',sys.executable,
          '--executor-config',str(profile),'--disable-native-memory','--no-start']
    parser=build_parser('')[0]
    result=setup.setup(parser.parse_args(base+['--session-memory','auto']))
    assert result['session_pipeline_ready'] is True
    assert result['native_hooks_verified'] is False
    assert result['complete'] is False
    assert Config.load(vault).sessions['mode']=='auto'
    assert setup.setup(parser.parse_args(base))['session_memory_mode']=='auto'
    assert len(probes)==2
    result=setup.setup(parser.parse_args(base+['--session-memory','off']))
    assert result['session_memory_mode']=='off'
    assert len(probes)==2
    assert hook_members(tmp_path/'home','claude','Stop')==[]


def test_failed_pipeline_probe_leaves_profiles_untouched(tmp_path,monkeypatch):
    import sys
    from kelmo.service import Service
    from kelmo.interface import build_parser
    from kelmo.install import setup,session_setup
    vault=tmp_path/'vault'
    Service.initialize(vault,'fixture')
    (vault/'.cache/embeddings/model').mkdir(parents=True)
    profile=tmp_path/'executor.json'
    profile.write_text(json.dumps({'argv':[sys.executable,'-c','print()']}))
    monkeypatch.setattr('kelmo.cli.dispatch',lambda *a,**k:{'healthy':True})
    monkeypatch.setattr('kelmo.daemon.execute',lambda *a,**k:{'ok':True})
    def fail(*a,**k):
        raise KelmoError('E_SETUP','Synthetic extraction failure')
    monkeypatch.setattr(session_setup,'probe',fail)
    args=build_parser('')[0].parse_args(['setup','--vault',str(vault),'--home',str(tmp_path/'home'),
        '--executable',sys.executable,'--executor-config',str(profile),'--disable-native-memory',
        '--no-start','--session-memory','auto'])
    with pytest.raises(KelmoError):
        setup.setup(args)
    assert not (tmp_path/'home/.claude.json').exists()
    assert not (tmp_path/'home/.codex/config.toml').exists()


@pytest.mark.parametrize('mode',['auto','archive-only'])
def test_real_probe_orchestrates_both_cases_with_synthetic_executor(tmp_path,monkeypatch,mode):
    import hashlib
    import sys
    from kelmo.service import Service
    from kelmo import model_store
    from kelmo.install.session_setup import probe
    from kelmo.session_settings import validate
    from kelmo.daemon import validate_config
    vault=tmp_path/'vault'
    Service.initialize(vault,'fixture')
    cache=vault/'.cache/embeddings/model'
    cache.mkdir(parents=True)
    data=b'Synthetic model'
    (cache/'model.onnx').write_bytes(data)
    monkeypatch.setattr(model_store,'FILES',{'model.onnx':(len(data),hashlib.sha256(data).hexdigest())})
    monkeypatch.setattr('kelmo.store.memory.store.embed',lambda *a,**k:bytes(1536))
    code="""import json,sys
p=json.load(sys.stdin)
if p['task'].startswith('extract_'):
    decision='rejected A' in str(p['messages'])
    print(json.dumps({'skip':False,'note_markdown':'Chose B only after approval.' if decision else 'Inspected logs without a durable finding.',
        'candidate_facts':[{'text':'Use B only after approval.','target':p['pages'][0]}] if decision else []}))
else:
    print(json.dumps({'decisions':[{'index':0,'action':'new','fact_key':'project.decision'}]}))
"""
    result=probe(vault,validate_config({'argv':[sys.executable,'-c',code]}),validate({'mode':mode},vault))
    assert result=={'extraction_ready':True,'session_pipeline_ready':True,'native_hooks_verified':False}
    with Service(vault) as service:
        assert service.status()['archive_records']==0
