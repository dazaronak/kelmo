import io
import json
import sys

import pytest

from kelmo.cli import main
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.session_control import configure,inspect,capture_hook
from kelmo.service import Service
from test_session_capture import lines


def configured(tmp_path):
    root=tmp_path/'vault'
    Service.initialize(root,'fixture')
    provider=tmp_path/'native'
    provider.mkdir()
    source=provider/'chat.jsonl'
    source.write_bytes(lines('claude'))
    configure(root,{'mode':'auto','sources':{'claude':[str(provider)]}})
    return root,source


def test_hook_cli_capture_inspect_pause_resume(tmp_path,monkeypatch,capsys):
    root,source=configured(tmp_path)
    payload={'session_id':'native','transcript_path':str(source)}
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(payload)))
    assert main(['hook','session-end','--provider','claude','--vault',str(root)])==0
    capsys.readouterr()
    assert len(inspect(root)['jobs'])==1
    assert main(['sessions','pause','--vault',str(root),'--json'])==0
    capsys.readouterr()
    assert Config.load(root).sessions['paused'] is True
    capture_hook(root,'claude','session-end',{'session_id':'other','transcript_path':str(source)})
    assert len(inspect(root)['jobs'])==1
    assert main(['sessions','resume','--vault',str(root),'--json'])==0
    capsys.readouterr()
    assert Config.load(root).sessions['paused'] is False
    assert inspect(root)['native_hooks_verified'] is False


def test_internal_model_session_does_not_capture(tmp_path,monkeypatch):
    root,source=configured(tmp_path)
    monkeypatch.setenv('KELMO_INTERNAL_JOB','1')
    assert capture_hook(root,'claude','session-end',{})=={}
    assert inspect(root)['jobs']==[]


def test_unenrolled_and_missing_transcripts_fail_without_job(tmp_path):
    root,source=configured(tmp_path)
    for payload in ({},{'session_id':'x','transcript_path':str(tmp_path/'other.jsonl')}):
        with pytest.raises(KelmoError):
            capture_hook(root,'claude','session-end',payload)
    assert inspect(root)['jobs']==[]


def test_mode_configuration_preserves_other_tables_and_hot(tmp_path):
    root,source=configured(tmp_path)
    with (root/'kelmo.toml').open('a') as file:
        file.write('\n[other]\nkeep=true\n[hot]\npages=[]\n')
    configure(root,{'mode':'archive-only'})
    assert Config.load(root).sessions['mode']=='archive-only'
    assert '[other]\nkeep=true' in (root/'kelmo.toml').read_text()
    configure(root,{'mode':'off'})
    assert Config.load(root).hot['pages']==[]


def test_inspection_does_not_migrate_journal(tmp_path):
    root=tmp_path/'vault'
    Service.initialize(root,'fixture')
    assert inspect(root)['jobs']==[]
    assert not (root/'.state/backups').exists()


def test_checkout_rejects_hook_conflict_before_runtime_creation(tmp_path,monkeypatch):
    from test_installer_wiki import load_installer
    module=load_installer()
    home=tmp_path/'home'
    (home/'.codex').mkdir(parents=True)
    (home/'.codex/hooks.json').write_text('{"hooks":{"Stop":"invalid"}}')
    created=[]
    class Builder:
        def __init__(self,**kwargs):
            pass
        def create(self,path):
            created.append(path)
            raise RuntimeError('runtime must not be touched')
    monkeypatch.setattr(module.venv,'EnvBuilder',Builder)
    monkeypatch.setattr(sys,'argv',['install.py','--prefix',str(tmp_path/'runtime'),
        '--home',str(home),'--vault',str(tmp_path/'vault'),'--disable-native-memory'])
    with pytest.raises(SystemExit):
        module.main()
    assert created==[]
    assert not (tmp_path/'runtime').exists()


@pytest.mark.parametrize('settings',[
    {'mode':'unknown'},{'paused':1},{'checkpoint_turns':False},{'checkpoint_turns':0},
    {'max_snapshot_bytes':67108865},{'max_input_bytes':100},{'extra':True},{'extraction':'all'},
    {'sources':[]},{'sources':{'other':['/tmp']}},{'sources':{'claude':[]}},
    {'sources':{'claude':[None]}},{'executor_config':False},
])
def test_invalid_session_settings_fail_before_writing(tmp_path,settings):
    root,_=configured(tmp_path)
    before=(root/'kelmo.toml').read_bytes()
    with pytest.raises(KelmoError):
        configure(root,settings)
    assert (root/'kelmo.toml').read_bytes()==before


def test_session_run_exposes_failure_in_json_envelope(tmp_path,monkeypatch,capsys):
    root,source=configured(tmp_path)
    capture_hook(root,'claude','session-end',{'session_id':'native','transcript_path':str(source)})
    profile=tmp_path/'executor.json'
    profile.write_text(json.dumps({'argv':[sys.executable,'-c','print("not-json")']}))
    assert main(['sessions','run','--vault',str(root),'--executor-config',str(profile),'--json'])==1
    result=json.loads(capsys.readouterr().out)
    assert result['ok'] is False
    assert result['error']['code']=='E_EXECUTOR'


def test_resume_disabled_mode_requires_setup(tmp_path,capsys):
    root,_=configured(tmp_path)
    configure(root,{'mode':'off'})
    assert main(['sessions','resume','--vault',str(root),'--json'])!=0
    assert json.loads(capsys.readouterr().out)['error']['code']=='E_STATE'
