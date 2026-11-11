"""`kelmod attach` without and with its protocol flag, the runtime directory that the bridges and `mcp-stdio` trust,
and the start-up checks of `kelmod serve` that depend on the HTTP endpoint.

The fixtures are the ones test_attach.py and conftest.py already define: `daemon` (a ready kelmod on the shared
vault), `Attach` (the protocol-2 bridge driven frame by frame), `attach_command`, and `gateway` and `vault_root`."""
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
for directory in (TESTS, TESTS / 'state_core', TESTS / 'daemon'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from daemon_harness import KELMOD, SRC, new_id  # noqa: E402
from state_core_support import request  # noqa: E402
from test_action_state import world_factory as world_factory  # noqa: E402,F401
from test_attach import ENROLLED, STATUS_LINE, Attach, attach_command, daemon as daemon  # noqa: E402,F401
from test_shared_context import shared as shared  # noqa: E402,F401

from kelmo.core import spec as core_spec  # noqa: E402
from kelmo.core import wire  # noqa: E402


def default_attach(runtime):
    """kelmod attach with no --protocol: the protocol-1 channel that is now the default."""
    return [str(KELMOD), 'attach', '--client-id', ENROLLED, '--runtime-dir', str(runtime)]


def hello_frame():
    """A kelmo/2 hello frame, as a protocol-2 client sends it first."""
    header = {'v': 2, 'kind': 'hello', 'id': new_id(), 'body_type': 'json', 'protocols': ['kelmo/2'],
              'operations': [], 'origin': {'client_id': ENROLLED, 'machine_tag': None, 'session': None}}
    return wire.encode(header, None)


def mcp_stdio(runtime):
    return subprocess.run([str(KELMOD), 'mcp-stdio', '--runtime-dir', str(runtime)], input=b'',
                          capture_output=True, timeout=60)


def resume(pid):
    """Resumes a core that a test froze, unless it has already gone."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)


def listen(directory):
    """A listening socket where the daemon's socket would be: a client that connects waits to be accepted."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(directory / 'd.sock'))
    server.listen(4)
    server.setblocking(False)
    return server


def connected(server):
    """Whether a client connected to the listening socket."""
    try:
        connection, _ = server.accept()
    except BlockingIOError:
        return False
    connection.close()
    return True


@pytest.fixture
def short_dir():
    """Makes a directory under /tmp, where a Unix socket path stays short, with the mode the test names."""
    made = []

    def make(mode):
        directory = Path(tempfile.mkdtemp(prefix='kg', dir='/tmp'))
        made.append(directory)
        os.chmod(directory, mode)
        return directory

    yield make
    for directory in made:
        shutil.rmtree(directory, ignore_errors=True)


# --- protocol 1 is the default -------------------------------------------------------------------------------

def test_without_a_protocol_flag_a_protocol_one_request_is_answered_in_protocol_one(daemon):
    completed = subprocess.run(default_attach(daemon.runtime), input=STATUS_LINE + b'\n', capture_output=True,
                               timeout=60)
    lines = completed.stdout.splitlines(keepends=True)
    assert completed.returncode == 0
    assert len(lines) == 1
    reply = json.loads(lines[0])
    assert reply['protocol'] == 'kelmo-client/1' and reply['ok'] is True
    assert reply['result']['client_id'] == ENROLLED


@pytest.mark.parametrize('stdin', [b'not json\n', hello_frame()], ids=['malformed-line', 'kelmo2-frame'])
def test_without_a_protocol_flag_a_bad_line_gets_the_protocol_one_refusal(daemon, stdin):
    completed = subprocess.run(default_attach(daemon.runtime), input=stdin, capture_output=True, timeout=60)
    lines = completed.stdout.splitlines(keepends=True)
    assert completed.returncode == 1
    assert len(lines) == 1 and lines[0].endswith(b'\n')
    reply = json.loads(lines[0])
    assert reply['protocol'] == 'kelmo-client/1' and reply['ok'] is False
    assert reply['error']['code'] == 'E_PROTOCOL'


# --- protocol 2: only state.client_request crosses, and the bridge answers the rest ------------------------

@pytest.mark.parametrize('op', ['daemon.status', 'state.tool'])
def test_protocol_two_answers_another_operation_locally_and_keeps_the_bridge(daemon, op):
    with Attach(daemon.runtime, ENROLLED) as attach:
        hello = new_id()
        attach.send({'v': 2, 'kind': 'hello', 'id': hello, 'body_type': 'json', 'protocols': ['kelmo/2'],
                     'operations': [], 'origin': {'client_id': ENROLLED, 'machine_tag': None, 'session': None}})
        header, _ = attach.receive(hello)
        assert header['operations'] == ['state.client_request']
        refused = new_id()
        attach.send({'v': 2, 'kind': 'call', 'id': refused, 'op': op, 'body_type': 'json',
                     'deadline_ms': 20000}, {})
        header, _ = attach.receive(refused)
        assert header['kind'] == 'reply' and header['ok'] is False
        assert header['error']['code'] == 'E_FORBIDDEN'
        # The bridge's own words, so the daemon never saw the call.
        assert header['error']['message'] == 'A remote client may not call this operation.'
        allowed = new_id()
        attach.send({'v': 2, 'kind': 'call', 'id': allowed, 'op': 'state.client_request', 'body_type': 'json',
                     'deadline_ms': 20000}, {'request': request('status')})
        header, body = attach.receive(allowed)
        assert header['ok'] is True and body['ok'] is True
        assert body['result']['client_id'] == ENROLLED


def test_protocol_two_delivers_the_reply_to_a_call_still_open_at_end_of_input(daemon):
    core = daemon.core()['pid']
    with Attach(daemon.runtime, ENROLLED) as attach:
        hello = new_id()
        attach.send({'v': 2, 'kind': 'hello', 'id': hello, 'body_type': 'json', 'protocols': ['kelmo/2'],
                     'operations': [], 'origin': {'client_id': ENROLLED, 'machine_tag': None, 'session': None}})
        attach.receive(hello)
        call = new_id()
        try:
            # The state core answers nothing until it is resumed, so its reply comes after end of input.
            os.kill(core, signal.SIGSTOP)
            attach.send({'v': 2, 'kind': 'call', 'id': call, 'op': 'state.client_request', 'body_type': 'json',
                         'deadline_ms': 60000}, {'request': request('status')})
            attach.process.stdin.close()
            time.sleep(0.5)
            # The bridge waits for the open call instead of ending at end of input.
            assert attach.process.poll() is None
            resume(core)
            header, body = attach.receive(call)
            assert header['ok'] is True and body['ok'] is True
            assert attach.process.wait(timeout=30) == 0
        finally:
            resume(core)


# --- protocol 1: a bad idempotency key refuses its own request only ----------------------------------------

@pytest.mark.parametrize('key', ['ключ-1', 'k' * 201], ids=['non-ascii', 'over-200-characters'])
def test_protocol_one_refuses_a_bad_idempotency_key_and_answers_the_next_line(daemon, key):
    bad = {'protocol': 'kelmo-client/1', 'request_id': 'bad', 'op': 'status', 'args': {}, 'idempotency_key': key}
    good = {'protocol': 'kelmo-client/1', 'request_id': 'good', 'op': 'status', 'args': {}}
    stdin = b''.join(json.dumps(line).encode('ascii') + b'\n' for line in (bad, good))
    completed = subprocess.run(attach_command(daemon.runtime, ENROLLED, 1), input=stdin, capture_output=True,
                               timeout=60)
    lines = [json.loads(line) for line in completed.stdout.splitlines()]
    assert completed.returncode == 0
    assert len(lines) == 2
    refused, answered = lines
    assert refused['request_id'] == 'bad' and refused['ok'] is False
    assert refused['error']['code'] == 'E_PROTOCOL'
    assert answered['ok'] is True and answered['result']['client_id'] == ENROLLED


# --- runtime directory: nothing connects to a directory another user can open, or to a link to one --------

def test_attach_refuses_a_runtime_directory_other_users_can_open(short_dir):
    directory = short_dir(0o755)
    with contextlib.closing(listen(directory)) as server:
        completed = subprocess.run(attach_command(directory, ENROLLED, 1), input=STATUS_LINE + b'\n',
                                   capture_output=True, timeout=60)
        lines = completed.stdout.splitlines()
        assert completed.returncode == 1
        assert len(lines) == 1
        reply = json.loads(lines[0])
        assert reply['ok'] is False and reply['error']['code'] == 'E_RUNTIME'
        assert not connected(server)


def test_attach_refuses_a_symlink_to_the_private_runtime_directory(daemon, tmp_path):
    link = tmp_path / 'runtime-link'
    os.symlink(daemon.runtime, link)
    refused = subprocess.run(attach_command(link, ENROLLED, 1), input=STATUS_LINE + b'\n', capture_output=True,
                             timeout=60)
    lines = refused.stdout.splitlines()
    assert refused.returncode == 1 and len(lines) == 1
    assert json.loads(lines[0])['error']['code'] == 'E_RUNTIME'
    # Control: the directory the link names still answers, so the refusal is about the link.
    answered = subprocess.run(attach_command(daemon.runtime, ENROLLED, 1), input=STATUS_LINE + b'\n',
                              capture_output=True, timeout=60)
    assert answered.returncode == 0
    assert json.loads(answered.stdout.splitlines()[0])['ok'] is True


def test_mcp_stdio_refuses_a_runtime_directory_other_users_can_open(short_dir):
    directory = short_dir(0o755)
    with contextlib.closing(listen(directory)) as server:
        completed = mcp_stdio(directory)
        assert completed.returncode != 0
        assert b'E_RUNTIME' in completed.stderr
        assert not connected(server)


def test_mcp_stdio_refuses_a_symlink_to_the_private_runtime_directory(daemon, tmp_path):
    link = tmp_path / 'runtime-link'
    os.symlink(daemon.runtime, link)
    completed = mcp_stdio(link)
    assert completed.returncode != 0
    assert b'E_RUNTIME' in completed.stderr


# --- serve: the HTTP endpoint and the exit codes it can cause -----------------------------------------------

def test_an_ephemeral_daemon_with_an_http_endpoint_exits_78_at_once(vault_root, short_dir, tmp_path):
    spec = core_spec.build(vault_root, runtime_dir=short_dir(0o700), source_root=SRC, http_port=0)
    spec_path = tmp_path / 'ephemeral-spec.json'
    spec_path.write_text(json.dumps(spec))
    began = time.monotonic()
    completed = subprocess.run([str(KELMOD), 'serve', '--spec', str(spec_path), '--ephemeral'],
                               capture_output=True, timeout=30)
    assert completed.returncode == 78
    assert time.monotonic() - began < 10
    assert b'an ephemeral daemon serves no HTTP endpoint' in completed.stderr


def test_a_second_daemon_on_the_same_root_and_port_is_busy_before_it_binds(gateway, vault_root, short_dir,
                                                                           tmp_path):
    _, _, runtime = gateway
    # The port is the one the first daemon holds, so a bind before the instance lock would fail as a config error.
    port = json.loads((runtime / 'endpoint.json').read_text())['port']
    spec = core_spec.build(vault_root, runtime_dir=short_dir(0o700), source_root=SRC, http_port=port)
    spec_path = tmp_path / 'second-spec.json'
    spec_path.write_text(json.dumps(spec))
    completed = subprocess.run([str(KELMOD), 'serve', '--spec', str(spec_path)], capture_output=True, timeout=60)
    assert completed.returncode == 75
    assert b'E_BUSY' in completed.stderr
    # The first daemon is untouched.
    status = subprocess.run([str(KELMOD), 'status', '--runtime-dir', str(runtime)], capture_output=True,
                            timeout=30)
    assert status.returncode == 0
