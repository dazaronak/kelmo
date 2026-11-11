"""Upgrade rehearsal on a synthetic vault: a 0.1 install is upgraded to the machine daemon by `setup`, and the upgrade
is undone by `daemon uninstall` followed by `kelmo uninstall --apply`.

The service paths are the macOS ones, so sys.platform is pinned to darwin and the rehearsal reads the same on any
host. Doctor is stubbed as in the setup tests, and every launchctl call is recorded instead of run, so no process
is started and no real service is touched."""
import json
import os
import plistlib
import sys
from types import SimpleNamespace

import pytest

from kelmo.core import daemon_admin
from kelmo.install import removal, shared
from kelmo.install import setup as installer
from kelmo.interface import build_parser
from kelmo.service import Service

GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home, an initialized vault, and an installed launcher with its kelmod beside it."""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    base = tmp_path.resolve()
    machine = SimpleNamespace(home=base / 'home', vault=base / 'vault',
                              executable=base / 'bin' / 'kelmo', kelmod=base / 'bin' / 'kelmod')
    for binary in (machine.executable, machine.kelmod):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'')
        binary.chmod(0o755)
    machine.home.mkdir()
    Service.initialize(machine.vault, 'synthetic', project='fixture')
    return machine


def install_0_1(machine):
    """What a 0.1 install left behind: the worker service, and a Claude registration with its ownership receipt."""
    installer.service_file(machine.home, machine.executable, machine.vault, 'darwin', None)
    entry = installer.mcp_entry(machine.executable, machine.vault, reconnect=True)
    (machine.home / '.claude.json').write_text(json.dumps({'mcpServers': {'kelmo': entry}}), encoding='utf-8')
    receipt = machine.home / '.claude' / 'kelmo' / 'ownership.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'claude_mcp_entry': entry}), encoding='utf-8')


def run_setup(machine, monkeypatch, *, start=True):
    """Runs setup; returns its result and the launchctl commands it would have run."""
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *args, **kwargs: {'healthy': True})
    commands = []

    def record(command, **_):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(installer.subprocess, 'run', record)
    monkeypatch.setattr(shared.subprocess, 'run', record)
    parser, _ = build_parser('')
    args = parser.parse_args(['setup', '--vault', str(machine.vault), '--home', str(machine.home),
                              '--executable', str(machine.executable), '--client', 'claude', '--skip-model',
                              '--disable-native-memory', '--non-interactive'] + ([] if start else ['--no-start']))
    return installer.setup(args), commands


def test_setup_upgrades_a_0_1_install_to_the_machine_daemon(machine, monkeypatch):
    install_0_1(machine)

    result, commands = run_setup(machine, monkeypatch)

    assert result['background_started'] is True
    uid = os.getuid()
    assert ['launchctl', 'bootout', f'gui/{uid}/org.kelmo.worker'] in commands
    assert not (machine.home / 'Library/LaunchAgents/org.kelmo.worker.plist').exists()
    plist = plistlib.loads((machine.home / 'Library/LaunchAgents/org.kelmo.daemon.plist').read_bytes())
    spec_path = machine.vault / '.state' / 'kelmod.spec.json'
    assert plist['ProgramArguments'] == [str(machine.executable), 'daemon', 'run', '--vault', str(machine.vault)]

    # The token stays in the 0600 file; the client runs the header helper instead of holding it.
    assert len((machine.vault / '.state' / 'http-token').read_text(encoding='utf-8').strip()) == 64
    registration = json.loads((machine.home / '.claude.json').read_text(encoding='utf-8'))['mcpServers']['kelmo']
    assert registration['type'] == 'http'
    assert registration['url'] == f'http://127.0.0.1:{shared.DEFAULT_HTTP_PORT}/mcp'
    assert 'headers' not in registration
    assert registration['headersHelper'].startswith(str(machine.kelmod) + ' http-headers ')

    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    assert spec['http']['port'] == shared.DEFAULT_HTTP_PORT


def test_setup_without_start_leaves_the_worker_and_the_daemon_refuses_to_run_beside_it(machine, monkeypatch):
    install_0_1(machine)
    _, commands = run_setup(machine, monkeypatch, start=False)

    assert commands == []
    worker = machine.home / 'Library/LaunchAgents/org.kelmo.worker.plist'
    assert worker.exists()
    monkeypatch.setattr(daemon_admin.Path, 'home', classmethod(lambda cls: machine.home))
    with pytest.raises(daemon_admin.KelmoError) as error:
        daemon_admin.run(machine.vault)
    assert error.value.code == 'E_CONFIG'


def test_daemon_uninstall_and_kelmo_uninstall_undo_the_upgrade(machine, monkeypatch):
    install_0_1(machine)
    run_setup(machine, monkeypatch)
    unloads = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs: unloads.append(command)
                        or SimpleNamespace(returncode=0))

    daemon = daemon_admin.uninstall(machine.vault, machine.home, apply=True, platform='darwin')

    assert daemon['lock_free'] is True
    assert unloads == [['launchctl', 'bootout', f'gui/{os.getuid()}/org.kelmo.daemon']]
    assert not (machine.home / 'Library/LaunchAgents/org.kelmo.daemon.plist').exists()

    parser, _ = build_parser('')
    args = parser.parse_args(['uninstall', '--vault', str(machine.vault), '--home', str(machine.home),
                              '--executable', str(machine.executable), '--client', 'claude', '--apply'])
    removal.uninstall(args)

    claude = json.loads((machine.home / '.claude.json').read_text(encoding='utf-8'))
    assert 'kelmo' not in claude.get('mcpServers', {})
    settings = json.loads((machine.home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
    members = [member for groups in settings.get('hooks', {}).values()
               for group in groups for member in group.get('hooks', [])]
    assert all(member.get('statusMessage') != 'kelmo managed hook' for member in members)
    assert daemon_admin.lock_free(machine.vault) is True
