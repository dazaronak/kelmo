"""The hook path from `kelmod hook` to the daemon: a delivered capture leaves no spool copy, the spool takes and
rejects files by their settled state, a start hook is acknowledged only after its context is printed, and
the daemon refuses to start beside a 0.1 worker service."""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kelmo.core import daemon_admin, spool
from kelmo.core.context import Context
from kelmo.errors import KelmoError
from kelmo.install import setup as installer

TESTS = Path(__file__).resolve().parent
if str(TESTS / 'daemon') not in sys.path:
    sys.path.insert(0, str(TESTS / 'daemon'))

from daemon_harness import KELMOD  # noqa: E402


def _conftest(directory):
    """The fixtures of another test directory's conftest.py, loaded under a name of its own."""
    spec = importlib.util.spec_from_file_location(f'{directory}_conftest', TESTS / directory / 'conftest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gateway = _conftest('gateway')
gateway = _gateway.gateway
vault_root = _gateway.vault_root
git_identity = _gateway.git_identity
_edge = _conftest('edge')
edge = _edge.edge
profile = _edge.profile


def _file(path, age=0):
    """A spool file last written `age` seconds ago."""
    path.write_text('{}')
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _must_not_exec(*args, **kwargs):
    raise AssertionError('the daemon was started')


def test_a_delivered_capture_leaves_no_in_flight_copy(gateway, vault_root):
    _url, _token, runtime = gateway
    state = vault_root / '.state'
    result = subprocess.run(
        [str(KELMOD), 'hook', 'session-stop', '--provider', 'claude', '--runtime-dir', str(runtime),
         '--state', str(state)],
        input='{}', capture_output=True, text=True, timeout=60,
        # An internal capture is accepted without a transcript, so the daemon's reply is a plain success.
        env={**os.environ, 'KELMO_INTERNAL_JOB': '1'})
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')
    assert list((state / 'hook-spool').iterdir()) == []


def test_entries_orders_by_name_and_takes_only_settled_in_flight_files(tmp_path):
    _file(tmp_path / '00000000000000000002-101.json')
    # Written just now: the hook that wrote it may still be running.
    _file(tmp_path / '.inflight-00000000000000000001-101.json')
    # Older than a minute: its hook was killed, so the daemon takes it.
    _file(tmp_path / '.inflight-00000000000000000003-101.json', age=120)
    names = [path.name for path in spool.entries(tmp_path)]
    # Ordered by the name without its prefix, so the orphan follows the visible file.
    assert names == ['00000000000000000002-101.json', '.inflight-00000000000000000003-101.json']


def test_entries_removes_temporary_files_older_than_an_hour_only(tmp_path):
    stale = _file(tmp_path / '.write-old.tmp', age=2 * 3600)
    fresh = _file(tmp_path / '.write-new.tmp')
    assert spool.entries(tmp_path) == []
    assert not stale.exists()
    assert fresh.exists()


def test_reject_renames_an_in_flight_file_to_bad(tmp_path):
    inflight = _file(tmp_path / '.inflight-00000000000000000007-101.json')
    spool.reject(inflight)
    assert not inflight.exists()
    assert (tmp_path / '00000000000000000007-101.bad').is_file()


def test_start_hook_is_acknowledged_only_after_the_context_is_printed(edge, monkeypatch):
    core, _client, _wakes = edge
    ran = []

    def hook(client, action, payload, provider=None, *, flusher=None, acknowledge=None, internal_job=None):
        acknowledge(lambda: ran.append(action))
        return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 'context'}}

    monkeypatch.setattr('kelmo.client.hook', hook)
    result = core.handle_hook(Context.local('hook', 30), {'action': 'start', 'payload': {}, 'context': {}})
    assert ran == []
    token = result['_kelmo_ack']
    assert core.handle_ack(Context.local('ack', 30), {'token': token}) == {'acknowledged': True}
    assert ran == ['start']
    assert core.handle_ack(Context.local('ack', 30), {'token': token}) == {'acknowledged': False}
    assert ran == ['start']


@pytest.mark.parametrize('platform, relative', [
    ('darwin', 'Library/LaunchAgents/org.kelmo.worker.plist'),
    ('linux', '.config/systemd/user/kelmo.service'),
])
def test_daemon_run_refuses_while_a_0_1_worker_service_is_installed(tmp_path, monkeypatch, platform, relative):
    home = tmp_path / 'home'
    service = home / relative
    service.parent.mkdir(parents=True)
    service.write_text('synthetic')
    real = installer.worker_service_file
    # worker_service_file takes the host platform as a default bound at import, so the branch is forced here.
    monkeypatch.setattr(installer, 'worker_service_file', lambda home_dir: real(home_dir, platform=platform))
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(os, 'execv', _must_not_exec)
    with pytest.raises(KelmoError) as refused:
        daemon_admin.run(tmp_path / 'vault')
    assert refused.value.code == 'E_CONFIG'
    assert str(service) in str(refused.value)
