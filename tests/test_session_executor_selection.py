import json
import sys

import pytest

from kelmo.cli import main
from kelmo.errors import KelmoError
from kelmo.session_control import capture_hook, configure
from kelmo.session_worker import tick
from test_session_control import configured


def profiles(tmp_path):
    root, source = configured(tmp_path)
    paths = []
    for name in ('saved', 'manual'):
        path = tmp_path/(name+'.json')
        path.write_text(json.dumps({'argv':[sys.executable, '-c', name], 'max_attempts':1}))
        paths.append(path)
    configure(root, {'executor_config':str(paths[0])})
    capture_hook(root, 'claude', 'session-end', {'session_id':'fixture', 'transcript_path':str(source)})
    return root, paths


@pytest.mark.parametrize('override', [False, True])
def test_manual_run_uses_explicit_profile_or_saved_extractor(tmp_path, monkeypatch, capsys, override):
    root, paths = profiles(tmp_path)
    calls = []
    def unavailable(argv, payload, **kwargs):
        calls.append(argv)
        raise KelmoError('E_EXECUTOR', 'Synthetic outage')
    monkeypatch.setattr('kelmo.daemon.execute', unavailable)
    command = ['sessions', 'run', '--vault', str(root), '--json']
    if override:
        command += ['--executor-config', str(paths[1])]
    assert main(command) == 1
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_EXECUTOR'
    assert calls == [json.loads(paths[int(override)].read_text())['argv']]
    assert not (root/'.state/executor.json').exists()


def test_background_tick_keeps_saved_extractor_over_fallback_argv(tmp_path):
    root, paths = profiles(tmp_path)
    calls = []
    def unavailable(argv, payload, **kwargs):
        calls.append(argv)
        raise KelmoError('E_EXECUTOR', 'Synthetic outage')
    result = tick(root, json.loads(paths[1].read_text())['argv'], execute=unavailable)
    assert result['jobs'][0]['state'] == 'needs_review'
    assert calls == [json.loads(paths[0].read_text())['argv']]
