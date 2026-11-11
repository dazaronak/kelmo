"""`daemon_client.ensure` refuses a running plain daemon when the caller needs a server profile.

The daemon is a real `kelmod serve` on a fresh synthetic vault, started from `spec.build` with no server profile.
Its socket is the one `daemon_client.socket_path` derives, so `ensure` meets it on the CLI's own path. TMPDIR is
pointed at a short private directory for this test only, because a Unix socket path must stay under about 104 bytes.
"""

import json
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

from kelmo.core import daemon_client, spec  # noqa: E402
from kelmo.errors import KelmoError  # noqa: E402
from kelmo.service import Service  # noqa: E402

GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


def _ready(daemon):
    if daemon.proc.poll() is not None:
        raise AssertionError(f'kelmod exited with status {daemon.proc.returncode}; see {daemon.log.name}')
    status = daemon.status()
    return status is not None and any(core['name'] == 'state' and core['ready'] for core in status['cores'])


@pytest.fixture
def plain_daemon(tmp_path, monkeypatch):
    """The root of a started plain daemon (no server profile), stopped after the test."""
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    short = Path(tempfile.mkdtemp(prefix='kelmo-ens-', dir='/tmp'))
    monkeypatch.setenv('TMPDIR', str(short))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    root = tmp_path / 'vault'
    Service.initialize(root, 'synthetic', project='fixture')
    runtime = daemon_client.runtime_dir(root)
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(spec.build(root, runtime_dir=runtime, source_root=SRC)), encoding='utf-8')
    log = (tmp_path / 'kelmod.log').open('w')
    proc = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, tmp_path, log)
    try:
        wait_for(lambda: _ready(daemon), timeout=60, what='the plain daemon and its state core')
        yield root
    finally:
        daemon.stop()
        shutil.rmtree(short, ignore_errors=True)


def test_ensure_refuses_a_plain_daemon_for_a_server_profile(plain_daemon, tmp_path):
    root = plain_daemon
    socket = daemon_client.socket_path(root)
    # Without a profile the plain daemon is accepted, so the refusal below is about the profile alone.
    assert daemon_client.ensure(root) == socket
    profile = tmp_path / 'server.json'
    with pytest.raises(KelmoError) as refused:
        daemon_client.ensure(root, server_profile=profile)
    assert refused.value.code == 'E_RUNTIME'
    message = str(refused.value)
    assert 'no server profile' in message
    assert str(profile.resolve()) in message
