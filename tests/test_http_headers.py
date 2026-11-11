"""`kelmod http-headers` gives the bearer header out only while a daemon serves the named port, and only from a
token file that its owner alone can read. Runs against the gateway's daemon: the real state core of a synthetic
vault, with the HTTP endpoint on port 0."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


def headers(state, runtime, port):
    return subprocess.run(
        [str(KELMOD), 'http-headers', '--state', str(state), '--port', str(port), '--runtime-dir', str(runtime)],
        capture_output=True, text=True, timeout=30)


def served_port(runtime):
    return json.loads((runtime / 'endpoint.json').read_text())['port']


def assert_refused(result, token):
    assert result.returncode != 0
    assert result.stdout == ''
    assert token not in result.stderr
    assert 'E_RUNTIME' in result.stderr


def test_prints_the_bearer_header_of_the_running_daemon(gateway, vault_root):
    _url, token, runtime = gateway
    result = headers(vault_root / '.state', runtime, served_port(runtime))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'Authorization': f'Bearer {token}'}
    assert result.stdout.count('\n') == 1


def test_refuses_a_port_the_daemon_does_not_serve(gateway, vault_root):
    _url, token, runtime = gateway
    result = headers(vault_root / '.state', runtime, served_port(runtime) + 1)
    assert_refused(result, token)


def test_refuses_when_no_daemon_serves_the_runtime_directory(gateway, vault_root):
    _url, token, daemon_runtime = gateway
    runtime = Path(tempfile.mkdtemp(prefix='kh', dir='/tmp'))
    try:
        result = headers(vault_root / '.state', runtime, served_port(daemon_runtime))
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    assert_refused(result, token)


def test_refuses_a_token_file_readable_by_others(gateway, vault_root):
    _url, token, runtime = gateway
    token_file = vault_root / '.state' / 'http-token'
    os.chmod(token_file, 0o644)
    try:
        result = headers(vault_root / '.state', runtime, served_port(runtime))
    finally:
        os.chmod(token_file, 0o600)
    assert result.returncode != 0
    assert result.stdout == ''
    assert token not in result.stderr
    assert 'E_CONFIG' in result.stderr and 'group or others' in result.stderr
