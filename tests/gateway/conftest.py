"""Fixtures for the HTTP MCP tests: kelmod serves the real state core and its HTTP endpoint on a fresh,
synthetic vault with no server profile. The daemon harness is imported by module name, so its directory
goes on sys.path here, as tests/state_core does. Every path lives under the test's temp directory, except the
runtime directory, which sits under /tmp so the Unix socket path stays short."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS / 'daemon') not in sys.path:
    sys.path.insert(0, str(TESTS / 'daemon'))

from daemon_harness import KELMOD, SRC, Daemon, wait_for  # noqa: E402

from kelmo.core import spec  # noqa: E402

GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


@pytest.fixture(scope='module')
def git_identity():
    """The vault's commits need an identity; kelmod passes these variables on to its cores."""
    with pytest.MonkeyPatch.context() as patch:
        for key, value in GIT_IDENTITY.items():
            patch.setenv(key, value)
        yield


@pytest.fixture(scope='module')
def vault_root(tmp_path_factory, git_identity):
    """A new vault made by the product's own initialization."""
    from kelmo.service import Service
    root = tmp_path_factory.mktemp('gateway') / 'vault'
    Service.initialize(root, 'synthetic', project='fixture')
    return root


def _ready(daemon):
    if daemon.proc.poll() is not None:
        raise AssertionError(
            f'kelmod exited with status {daemon.proc.returncode}; see {daemon.log.name}')
    status = daemon.status()
    return (status is not None and (daemon.runtime / 'endpoint.json').is_file()
            and any(core['name'] == 'state' and core['ready'] for core in status['cores']))


@pytest.fixture(scope='module')
def gateway(vault_root):
    """(url, token, runtime_dir) of a kelmod that serves the state core and the HTTP endpoint on port 0.

    The URL is the one kelmod published in endpoint.json; the token is the vault's `.state/http-token`."""
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh',
                    pytrace=False)
    workdir = vault_root.parent
    runtime = Path(tempfile.mkdtemp(prefix='kg', dir='/tmp'))
    built = spec.build(vault_root, runtime_dir=runtime, source_root=SRC, http_port=0)
    spec_path = workdir / 'spec.json'
    spec_path.write_text(json.dumps(built))
    log = (workdir / 'kelmod.log').open('w')
    command = [str(KELMOD), 'serve', '--spec', str(spec_path)]
    proc = subprocess.Popen(command, stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, workdir, log)
    try:
        wait_for(lambda: _ready(daemon), timeout=60, what='the state core and the HTTP endpoint')
        endpoint = json.loads((runtime / 'endpoint.json').read_text())
        token = (vault_root / '.state' / 'http-token').read_text().strip()
        yield endpoint['url'], token, runtime
    finally:
        daemon.stop()
