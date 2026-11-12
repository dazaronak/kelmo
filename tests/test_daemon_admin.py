"""`daemon_admin` against a real `kelmod serve` on a synthetic vault: the instance lock, a stop, and the uninstall of
the machine daemon's service file. Nothing here reaches launchctl or systemctl: uninstall runs with apply=False, or
with subprocess.run replaced by a recorder."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

DAEMON_TESTS = Path(__file__).resolve().parent / 'daemon'
if str(DAEMON_TESTS) not in sys.path:
    sys.path.insert(0, str(DAEMON_TESTS))

from daemon_harness import KELMOD, SRC, Daemon, wait_for  # noqa: E402

from kelmo.core import daemon_admin, daemon_client, spec  # noqa: E402
from kelmo.install import shared  # noqa: E402
from kelmo.service import Service  # noqa: E402

GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An initialized synthetic vault. TMPDIR is a short directory, so the runtime directory and its socket path
    stay under the Unix socket length limit."""
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    short = Path(tempfile.mkdtemp(prefix='kh', dir='/tmp'))
    monkeypatch.setenv('TMPDIR', str(short))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    root = tmp_path / 'vault'
    Service.initialize(root, 'synthetic', project='fixture')
    yield root
    shutil.rmtree(short, ignore_errors=True)


def start_daemon(root, workdir):
    """A real `kelmod serve` for the vault, with the runtime directory `daemon_client` derives for it."""
    runtime = daemon_client.runtime_dir(root)
    spec_path = workdir / 'spec.json'
    daemon_spec = spec.build(root, runtime_dir=runtime, source_root=SRC)
    spec_path.write_text(json.dumps(daemon_spec), encoding='utf-8')
    log = (workdir / 'kelmod.log').open('w')
    proc = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    return Daemon(proc, spec_path, runtime, workdir, log)


def daemon_ready(daemon):
    if daemon.proc.poll() is not None:
        raise AssertionError(f'kelmod exited with status {daemon.proc.returncode}; see {daemon.log.name}')
    status = daemon.status()
    return status is not None and any(core['name'] == 'state' and core['ready'] for core in status['cores'])


def test_lock_is_held_only_while_a_daemon_runs(vault, tmp_path):
    assert daemon_admin.lock_free(vault) is True
    daemon = start_daemon(vault, tmp_path)
    try:
        wait_for(lambda: daemon_ready(daemon), timeout=60, what='the daemon and its state core')
        assert daemon_admin.lock_free(vault) is False
    finally:
        daemon.stop()


def test_stop_sends_sigterm_and_the_daemon_exits_zero(vault, tmp_path):
    daemon = start_daemon(vault, tmp_path)
    try:
        wait_for(lambda: daemon_ready(daemon), timeout=60, what='the daemon and its state core')
        result = daemon_admin.stop(vault)
        assert result['stopped'] is True
        assert result['pid'] == daemon.proc.pid
        assert daemon_admin.lock_free(vault) is True
        assert daemon.wait_exit(timeout=20) == 0
    finally:
        daemon.stop()


def test_uninstall_without_apply_reports_the_service_and_changes_nothing(tmp_path):
    home = tmp_path / 'home'
    service = home / 'Library/LaunchAgents/org.kelmo.daemon.plist'
    service.parent.mkdir(parents=True)
    service.write_text('synthetic', encoding='utf-8')

    result = daemon_admin.uninstall(tmp_path / 'vault', home, apply=False, platform='darwin')

    assert result['service_file'] == str(service.resolve())
    assert result['running'] is False
    assert service.read_text(encoding='utf-8') == 'synthetic'


def test_uninstall_apply_unloads_the_service_and_removes_its_file(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    service = home / 'Library/LaunchAgents/org.kelmo.daemon.plist'
    service.parent.mkdir(parents=True)
    service.write_text('synthetic', encoding='utf-8')
    unloads = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: unloads.append(command)
                        or subprocess.CompletedProcess(command, 0))

    result = daemon_admin.uninstall(tmp_path / 'vault', home, apply=True, platform='darwin')

    assert not service.exists()
    assert result['lock_free'] is True
    assert unloads == [['launchctl', 'bootout', f'gui/{os.getuid()}/org.kelmo.daemon']]


def test_memory_limits_sum_the_core_budgets_and_the_linux_unit_carries_them(tmp_path):
    daemon_spec = {'cores': [{'budget': {'soft_mb': 100, 'hard_mb': 200}},
                             {'budget': {'soft_mb': 50, 'hard_mb': 80}}]}
    high, hard = shared.memory_limits(daemon_spec)
    assert (high, hard) == (214, 344)

    _, data = shared.service_definition(tmp_path, tmp_path / 'bin' / 'kelmod', tmp_path / 'spec.json',
                                        'linux', (high, hard))
    lines = data.decode('utf-8').splitlines()
    for line in ('Type=notify', 'NotifyAccess=main', 'WatchdogSec=30', 'KillMode=mixed',
                 'Restart=on-failure', 'RestartPreventExitStatus=78', 'TimeoutStopSec=90', 'OOMPolicy=continue',
                 f'MemoryHigh={high}M', f'MemoryMax={hard}M'):
        assert line in lines


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_the_service_command_is_one_the_command_line_accepts(tmp_path, platform):
    """The service starts `kelmo daemon run --vault ROOT`: the parser must take the vault after the action."""
    import plistlib
    import shlex

    from kelmo.interface import build_parser
    root = tmp_path / 'vault'
    _, data = shared.service_definition(tmp_path, tmp_path / 'bin' / 'kelmo', root, platform)
    if platform == 'darwin':
        argv = plistlib.loads(data)['ProgramArguments']
    else:
        start = next(line for line in data.decode('utf-8').splitlines() if line.startswith('ExecStart='))
        argv = shlex.split(start[len('ExecStart='):])
    parser, _ = build_parser('')
    args = parser.parse_args(argv[1:])
    assert (args.command, args.action, args.vault) == ('daemon', 'run', root)


@pytest.mark.parametrize('action', ['status', 'stop', 'run', 'uninstall'])
def test_every_daemon_action_takes_the_vault_before_or_after_it(tmp_path, action):
    from kelmo.interface import build_parser
    parser, _ = build_parser('')
    after = parser.parse_args(['daemon', action, '--vault', str(tmp_path), '--json'])
    before = parser.parse_args(['--vault', str(tmp_path), '--json', 'daemon', action])
    assert after.vault == before.vault == tmp_path
    assert after.json is True and before.json is True
