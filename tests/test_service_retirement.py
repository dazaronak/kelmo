"""Service retirement must succeed before a replacement daemon can be activated."""
import os
import subprocess
from types import SimpleNamespace

import pytest

from kelmo.core import daemon_admin
from kelmo.errors import KelmoError
from kelmo.install import setup as installer


SERVICE_CASES = [
    ('darwin', 'Library/LaunchAgents/org.kelmo.worker.plist'),
    ('linux', '.config/systemd/user/kelmo.service'),
]
RETIREMENT_FAILURE_CASES = [(True, failure) for failure in ('nonzero', 'oserror', 'timeout')]
RETIREMENT_FAILURE_CASES += [(False, failure) for failure in ('oserror', 'timeout')]


def manager_failure(monkeypatch, module, failure):
    if failure == 'nonzero':
        monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs:
                            subprocess.CompletedProcess(args[0], 1, b'', b'unknown manager failure'))
    elif failure == 'oserror':
        def fail(*args, **kwargs):
            raise OSError('manager unavailable')
        monkeypatch.setattr(module.subprocess, 'run', fail)
    else:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs.get('timeout', 1))
        monkeypatch.setattr(module.subprocess, 'run', timeout)


@pytest.mark.parametrize('platform,relative', SERVICE_CASES)
@pytest.mark.parametrize('legacy_file_exists,failure', RETIREMENT_FAILURE_CASES)
def test_setup_does_not_activate_daemon_when_worker_retirement_is_uncertain(
        tmp_path, monkeypatch, platform, relative, failure, legacy_file_exists):
    home = tmp_path / 'home'
    legacy = home / relative
    if legacy_file_exists:
        legacy.parent.mkdir(parents=True)
        legacy.write_text('legacy worker unit', encoding='utf-8')
    new_service = home / ('Library/LaunchAgents/org.kelmo.daemon.plist' if platform == 'darwin'
                          else '.config/systemd/user/kelmod.service')
    daemon_state = tmp_path / 'vault' / '.state'
    daemon = {'state': daemon_state, 'spec': daemon_state / 'kelmod.spec.json', 'port': 47821,
              'runtime': tmp_path / 'runtime', 'kelmod': tmp_path / 'bin' / 'kelmod',
              'url': 'http://127.0.0.1:47821/mcp'}
    activated = []

    monkeypatch.setattr(installer.sys, 'platform', platform)
    monkeypatch.setattr('kelmo.core.spec.http_token_at', lambda *_: None)
    monkeypatch.setattr('kelmo.core.daemon_admin.write_options', lambda *_: None)
    monkeypatch.setattr('kelmo.core.daemon_admin.build_spec', lambda *_: {})
    monkeypatch.setattr('kelmo.core.daemon_admin.write_private', lambda *_: None)
    monkeypatch.setattr('kelmo.install.shared.memory_limits', lambda *_: (0, 0))
    monkeypatch.setattr('kelmo.install.shared.service_definition',
                        lambda *args: (new_service, b'new service definition'))
    monkeypatch.setattr('kelmo.install.shared.activate', lambda *args: activated.append(args))
    manager_failure(monkeypatch, installer, failure)

    with pytest.raises(KelmoError):
        installer.finish_local_daemon(
            SimpleNamespace(no_start=False), home, tmp_path / 'bin' / 'kelmo', tmp_path / 'vault', daemon,
            None, None, {}, {}, {'session_memory_mode': 'off', 'native_hooks_verified': True}, {}, True, None)

    assert legacy.exists() is legacy_file_exists
    assert activated == []
    assert not new_service.exists()


@pytest.mark.parametrize('platform,relative', SERVICE_CASES)
def test_successful_worker_retirement_removes_unit_and_no_start_preserves_it(
        tmp_path, monkeypatch, platform, relative):
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs: calls.append(command)
                        or subprocess.CompletedProcess(command, 0, b'', b''))
    unit = tmp_path / relative
    unit.parent.mkdir(parents=True)
    unit.write_text('legacy worker unit', encoding='utf-8')

    assert installer.retire_worker_service(tmp_path, platform, stop=False) is None
    assert unit.exists()
    assert calls == []

    assert installer.retire_worker_service(tmp_path, platform, stop=True) == str(unit)
    assert not unit.exists()
    assert len(calls) == 1


@pytest.mark.parametrize('platform,relative', [
    ('darwin', 'Library/LaunchAgents/org.kelmo.daemon.plist'),
    ('linux', '.config/systemd/user/kelmod.service'),
])
@pytest.mark.parametrize('failure', ['nonzero', 'oserror', 'timeout'])
def test_daemon_uninstall_retains_unit_when_manager_unload_fails(
        tmp_path, monkeypatch, platform, relative, failure):
    home = tmp_path / 'home'
    unit = home / relative
    unit.parent.mkdir(parents=True)
    unit.write_text('daemon unit', encoding='utf-8')
    stopped = []
    monkeypatch.setattr(daemon_admin, 'stop', lambda *args: stopped.append(args))
    manager_failure(monkeypatch, installer, failure)

    with pytest.raises(KelmoError):
        daemon_admin.uninstall(tmp_path / 'vault', home, apply=True, platform=platform)

    assert unit.exists()
    assert stopped == []


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_fresh_setup_with_manager_confirmed_missing_worker_is_usable(tmp_path, monkeypatch, platform):
    calls = []

    def manager(command, **kwargs):
        calls.append(command)
        if command[0] == 'launchctl' and command[1] == 'bootout':
            return subprocess.CompletedProcess(command, 1, b'', b'not loaded')
        if command[0] == 'launchctl':
            return subprocess.CompletedProcess(
                command, 113, b'', b'Could not find service "org.kelmo.worker" in domain for user gui')
        if command[0] == 'systemctl' and command[1:4] == ['--user', 'disable', '--now']:
            return subprocess.CompletedProcess(command, 1, b'', b'not loaded')
        if command[0] == 'systemctl':
            return subprocess.CompletedProcess(command, 0, b'not-found\n', b'')
        raise AssertionError(f'unexpected service manager command: {command}')

    monkeypatch.setattr(installer.subprocess, 'run', manager)

    assert installer.retire_worker_service(tmp_path, platform, stop=True) is None
    assert len(calls) == 2
    expected = (f'gui/{os.getuid()}/org.kelmo.worker' if platform == 'darwin' else 'kelmo.service')
    assert calls[0][-1] == expected


@pytest.mark.parametrize('platform,relative', SERVICE_CASES)
def test_unknown_nonzero_unload_without_a_unit_refuses_fresh_install(tmp_path, monkeypatch, platform, relative):
    calls = []

    def manager(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b'', b'unknown manager failure')

    monkeypatch.setattr(installer.subprocess, 'run', manager)

    with pytest.raises(KelmoError):
        installer.retire_worker_service(tmp_path, platform, stop=True)

    assert len(calls) == 2
    assert not (tmp_path / relative).exists()


def test_wrong_label_missing_diagnostic_does_not_allow_darwin_fresh_install(tmp_path, monkeypatch):
    calls = []

    def manager(command, **kwargs):
        calls.append(command)
        if command[1] == 'bootout':
            return subprocess.CompletedProcess(command, 1, b'', b'not loaded')
        return subprocess.CompletedProcess(
            command, 113, b'', b'Could not find service "org.kelmo.other" in domain for user gui')

    monkeypatch.setattr(installer.subprocess, 'run', manager)

    with pytest.raises(KelmoError):
        installer.retire_worker_service(tmp_path, 'darwin', stop=True)

    assert len(calls) == 2


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_uninstall_unloads_loaded_service_even_when_unit_file_is_missing(tmp_path, monkeypatch, platform):
    events = []

    def manager(command, **kwargs):
        events.append(('unload', command))
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(installer.subprocess, 'run', manager)
    monkeypatch.setattr(daemon_admin, 'stop', lambda root: events.append(('stop', root)) or {'stopped': False})
    monkeypatch.setattr(daemon_admin, 'lock_free', lambda root: True)

    result = daemon_admin.uninstall(tmp_path / 'vault', tmp_path / 'home', apply=True, platform=platform)

    assert result['service_file'] is None
    assert events[0][0] == 'unload' and events[1][0] == 'stop'
    expected = (['launchctl', 'bootout', f'gui/{os.getuid()}/org.kelmo.daemon'] if platform == 'darwin'
                else ['systemctl', '--user', 'disable', '--now', 'kelmod.service'])
    assert events[0][1] == expected
