"""Installing the machine daemon: the HTTP registration in each client, the service definitions, the retirement
of the 0.1 worker service, and the removal of a shared client's integrations."""
import json
import plistlib
import shlex
import tomllib
import uuid
from types import SimpleNamespace

import pytest

from kelmo.client import prepare_profile
from kelmo.config import ClientProfile
from kelmo.core.daemon_client import runtime_dir
from kelmo.errors import KelmoError
from kelmo.install.removal import uninstall
import kelmo.install.setup as install_setup
from kelmo.install.setup import apply_clients, codex_table, http_entry, prepare_clients, retire_worker_service
from kelmo.install.shared import service_definition

RUNTIME = '/tmp/kh-runtime'


def synthetic_binary(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    path.chmod(0o755)
    return path


def endpoint(kelmod, state):
    return {'kelmod': kelmod, 'state': state, 'port': 47821, 'url': 'http://127.0.0.1:47821/mcp',
            'runtime': RUNTIME}


def helper(kelmod, state):
    return shlex.join([str(kelmod), 'http-headers', '--state', str(state), '--port', '47821',
                       '--runtime-dir', RUNTIME])


def managed_commands(path):
    hooks = json.loads(path.read_text()).get('hooks', {})
    return [member['command'] for groups in hooks.values() for group in groups for member in group.get('hooks', [])]


def test_http_entries_use_each_clients_keys_and_codex_table_parses_back():
    codex = http_entry('codex', endpoint('/bin/kelmod', '/state'))
    # No client file holds the token: each client runs the header helper when it connects.
    assert http_entry('claude', endpoint('/bin/kelmod', '/state')) == {
        'type': 'http', 'url': 'http://127.0.0.1:47821/mcp', 'headersHelper': helper('/bin/kelmod', '/state')}
    assert codex == {'url': 'http://127.0.0.1:47821/mcp', 'http_headers_helper': helper('/bin/kelmod', '/state')}
    assert tomllib.loads(codex_table(codex))['mcp_servers']['kelmo'] == codex

    command = {'command': '/opt/kelmo/bin/kelmo', 'args': ['mcp', '--vault', '/vault']}
    text = codex_table(command)
    assert text.startswith('[mcp_servers.kelmo]\ncommand = ')
    assert tomllib.loads(text)['mcp_servers']['kelmo'] == command


def test_edge_registration_is_written_once_and_a_plain_setup_cannot_replace_it(tmp_path):
    home, vault = tmp_path / 'home', tmp_path / 'vault'
    kelmod = synthetic_binary(tmp_path / 'bin' / 'kelmod')
    executable = synthetic_binary(tmp_path / 'bin' / 'kelmo')
    state = tmp_path / 'client' / '.client'
    edge = endpoint(kelmod, state)
    apply_clients(prepare_clients(home, executable, vault, 'claude', False, None, edge=edge))

    claude_json, settings = home / '.claude.json', home / '.claude' / 'settings.json'
    assert json.loads(claude_json.read_text())['mcpServers']['kelmo'] == http_entry('claude', edge)
    start = json.loads(settings.read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert start == shlex.join([str(kelmod), 'hook', 'start', '--provider', 'claude',
                                '--runtime-dir', RUNTIME, '--state', str(state)])

    before = [claude_json.read_bytes(), settings.read_bytes()]
    again = apply_clients(prepare_clients(home, executable, vault, 'claude', False, None, edge=edge))
    assert again['backups'] == []
    assert [claude_json.read_bytes(), settings.read_bytes()] == before

    with pytest.raises(KelmoError) as error:
        prepare_clients(home, executable, vault, 'claude', False, None)
    assert error.value.code == 'E_CONFIG'


def test_uninstall_removes_the_http_registration_and_kelmod_hooks_from_both_clients(tmp_path):
    home, root = tmp_path / 'home', tmp_path / 'client'
    kelmod = synthetic_binary(tmp_path / 'bin' / 'kelmod')
    executable = synthetic_binary(tmp_path / 'bin' / 'kelmo')
    prepare_profile(root, str(uuid.uuid4()), ['/bin/false'])
    # Setup names the runtime directory kelmod derives for the root; removal recomputes the same one.
    edge = dict(endpoint(kelmod, ClientProfile.load(root).state), runtime=runtime_dir(root))
    apply_clients(prepare_clients(home, executable, root, 'both', False, None, edge=edge))

    args = SimpleNamespace(vault=root, home=home, executable=executable, client='both', apply=True)
    result = uninstall(args)

    assert result['applied'] is True and result['files']
    assert 'kelmo' not in json.loads((home / '.claude.json').read_text()).get('mcpServers', {})
    assert 'kelmo' not in tomllib.loads((home / '.codex' / 'config.toml').read_text()).get('mcp_servers', {})
    assert managed_commands(home / '.claude' / 'settings.json') == []
    assert managed_commands(home / '.codex' / 'hooks.json') == []


def test_darwin_service_runs_the_daemon_through_the_launcher(tmp_path):
    launcher, root = tmp_path / 'bin' / 'kelmo', tmp_path / 'vault'
    path, data = service_definition(tmp_path, str(launcher), str(root), 'darwin')

    assert path == tmp_path / 'Library' / 'LaunchAgents' / 'org.kelmo.daemon.plist'
    plist = plistlib.loads(data)
    assert plist['Label'] == 'org.kelmo.daemon'
    assert plist['ProgramArguments'] == [str(launcher), 'daemon', 'run', '--vault', str(root)]
    assert plist['KeepAlive'] == {'SuccessfulExit': False}


def test_linux_unit_quotes_the_daemon_command_and_does_not_restart_on_status_78(tmp_path):
    launcher, root = tmp_path / 'bin' / 'kelmo', tmp_path / 'vault'
    path, data = service_definition(tmp_path, str(launcher), str(root), 'linux')

    text = data.decode()
    assert f'ExecStart="{launcher}" "daemon" "run" "--vault" "{root}"' in text
    assert 'RestartPreventExitStatus=78' in text


@pytest.mark.parametrize('platform, relative', [
    ('darwin', 'Library/LaunchAgents/org.kelmo.worker.plist'),
    ('linux', '.config/systemd/user/kelmo.service'),
])
def test_retiring_the_worker_service_removes_its_file_and_reports_the_path(tmp_path, platform, relative,
                                                                         monkeypatch):
    calls = []
    monkeypatch.setattr(install_setup.subprocess, 'run', lambda command, **_: calls.append(command)
                        or SimpleNamespace(returncode=0))
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('synthetic')

    # Without stopping (setup --no-start) the unit stays where `kelmo daemon run` finds it and refuses to start.
    assert retire_worker_service(tmp_path, platform, stop=False) is None
    assert path.exists() and calls == []
    assert retire_worker_service(tmp_path, platform, stop=True) == str(path)
    assert not path.exists() and len(calls) == 1
    assert retire_worker_service(tmp_path, platform, stop=True) is None
