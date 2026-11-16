"""`kelmo dream` on a shared-client root: Dreaming runs on the server, so every action is refused the way other
server commands are (E_CAPABILITY), not as an uninitialized vault, and none is sent to the client's daemon."""
import json
from types import SimpleNamespace

import pytest

from kelmo.cli import call, routed
from kelmo.config import CLIENT_FILE


def client_root(tmp_path):
    root = tmp_path / 'client'
    root.mkdir()
    (root / CLIENT_FILE).write_text('{}', encoding='utf-8')
    return root


@pytest.mark.parametrize('action', ['status', 'pause'])
def test_dream_on_a_shared_client_root_is_refused_as_a_server_command(tmp_path, capsys, action):
    root = client_root(tmp_path)

    code = call(['--vault', str(root), 'dream', action, '--json'])

    output = json.loads(capsys.readouterr().out)
    assert code != 0, output
    assert output['error']['code'] == 'E_CAPABILITY', output
    assert 'runs on the server' in output['error']['message'], output


@pytest.mark.parametrize('action', ['run', 'apply'])
def test_dream_run_on_a_shared_client_root_is_not_routed_to_the_daemon(tmp_path, action):
    root = client_root(tmp_path)

    assert routed(SimpleNamespace(command='dream', action=action, vault=root, server_profile=None)) is False
