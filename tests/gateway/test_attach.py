"""`kelmod attach`, the forced command of a remote client. A /1 client runs the package's SharedClient over it and
completes a round (hello, search, capture, create, session append); a /2 connection is pinned to the origin its
attach argument names; a request from an unknown client id gets a refusal, never a result.

The shared-test helpers are imported by module name, as tests/state_core does, so the test directories go on
sys.path before those imports; the imports after that bootstrap carry `noqa: E402`."""
import base64
import hashlib
import json
import os
import select
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

from daemon_harness import KELMOD, SRC, Daemon, new_id, wait_for  # noqa: E402
from state_core_support import profile_path, request  # noqa: E402
from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: E402,F401
from test_shared_context import server_config, shared as shared  # noqa: E402,F401

from kelmo.client import ClientService, SharedClient, prepare_profile  # noqa: E402
from kelmo.config import ClientProfile  # noqa: E402
from kelmo.core import spec as core_spec  # noqa: E402
from kelmo.core import wire  # noqa: E402
from kelmo.server import EMPTY_SHA256  # noqa: E402

ENROLLED = 'mac-a'
STRANGER = 'stranger'
STATUS_LINE = b'{"protocol":"kelmo-client/1","request_id":"r1","op":"status","args":{}}'


def attach_command(runtime, client_id, protocol):
    return [str(KELMOD), 'attach', '--client-id', client_id, '--protocol', str(protocol),
            '--runtime-dir', str(runtime)]


def ready(candidate):
    status = candidate.status()
    return (status is not None and bool(status['cores'])
            and all(core['ready'] for core in status['cores']))


@pytest.fixture
def daemon(shared, tmp_path):
    """kelmod serving the shared vault's state core under its server profile, ready before a test runs.
    The vault's sessions are archive-only, so a session piece is kept on the server."""
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    server_config(shared.world.root, sessions='archive-only')
    # A Unix socket path must stay short, so the runtime directory goes under /tmp, as in the harness.
    runtime = Path(tempfile.mkdtemp(prefix='ka', dir='/tmp'))
    spec = core_spec.build(shared.world.root, server_profile=profile_path(shared), runtime_dir=runtime,
                           source_root=SRC, schedule=False)
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (tmp_path / 'kelmod.log').open('w')
    proc = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    started = Daemon(proc, spec_path, runtime, tmp_path, log)
    try:
        wait_for(lambda: ready(started), timeout=60, what='the state core ready')
        yield started
    finally:
        started.stop()


class Attach:
    """A protocol-2 attach process driven frame by frame. Its output is read against a deadline; frames for other
    calls wait in an inbox until their call asks for them."""

    def __init__(self, runtime, client_id):
        self.process = subprocess.Popen(attach_command(runtime, client_id, 2), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE)
        self.reader = wire.FrameReader()
        self.inbox = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()

    def send(self, header, body=None):
        self.process.stdin.write(wire.encode(header, body))
        self.process.stdin.flush()

    def receive(self, call_id, timeout=30.0):
        end = time.monotonic() + timeout
        while True:
            for index, (header, body) in enumerate(self.inbox):
                if header['id'] == call_id:
                    del self.inbox[index]
                    return header, body
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'no frame answers call {call_id}')
            if select.select([self.process.stdout], [], [], left)[0]:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise ConnectionError('kelmod attach closed its output')
                self.inbox += self.reader.feed(data)


def test_protocol_one_client_completes_hello_search_capture_create_and_append(shared, daemon):
    root = shared.tmp_path / 'client-attach'
    prepare_profile(root, shared.world.bindings.authority_id, attach_command(daemon.runtime, ENROLLED, 1),
                    timeout_seconds=60)
    with SharedClient(ClientProfile.load(root)) as client:
        service = ClientService(client)
        # The first call connects: the hello goes through the attach process before the status is asked.
        assert client.call('status')['client_id'] == ENROLLED
        assert client.server['authority_id'] == shared.world.bindings.authority_id
        assert client.server['client_id'] == ENROLLED
        assert isinstance(service.search('attach check', limit=5), list)

        captured = service.capture('Attach check capture.', 'attach-capture.md', role='collector',
                                   idempotency_key='attach-capture')
        trace = client.call('trace', {'operation_id': captured['id']})
        assert trace['operation']['id'] == captured['id']

        created = service.create('Attach check note.', 'attach-create.md', project='app', selected=[APP_PAGE],
                                 idempotency_key='attach-create')
        trace = client.call('trace', {'operation_id': created['operation']['id']})
        assert trace['operation']['id'] == created['operation']['id']

        payload = b'{"type":"user","message":"attach check"}\n'
        piece = {'provider': 'claude', 'native_id': 'attach-session-1', 'event': 'Stop',
                 'file_identity': 'attach-transcript', 'client_generation': 1, 'offset': 0,
                 'base_sha256': EMPTY_SHA256, 'content': base64.b64encode(payload).decode('ascii'),
                 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'final': False}
        appended = client.submit('session_append', piece, 'attach-append-1')
        assert appended['appended'] is True and appended['range'] == [0, len(payload)]


@pytest.mark.parametrize('stdin', [
    STATUS_LINE,  # no trailing newline, and stdin closes after it
    b'not json\n',
])
def test_protocol_one_ends_the_channel_on_an_unterminated_or_malformed_line(daemon, stdin):
    completed = subprocess.run(attach_command(daemon.runtime, ENROLLED, 1), input=stdin,
                               capture_output=True, timeout=60)
    lines = completed.stdout.splitlines(keepends=True)
    assert completed.returncode == 1
    assert len(lines) == 1 and lines[0].endswith(b'\n')
    reply = json.loads(lines[0])
    assert reply['ok'] is False and reply['error']['code'] == 'E_PROTOCOL'


def test_protocol_two_pins_the_origin_the_attach_argument_names(daemon):
    with Attach(daemon.runtime, ENROLLED) as attach:
        hello = new_id()
        attach.send({'v': 2, 'kind': 'hello', 'id': hello, 'body_type': 'json', 'protocols': ['kelmo/2'],
                     'operations': [], 'origin': {'client_id': STRANGER, 'machine_tag': None, 'session': None}})
        header, _ = attach.receive(hello)
        assert header['kind'] == 'hello' and header['protocols'] == ['kelmo/2']

        named = new_id()
        attach.send({'v': 2, 'kind': 'call', 'id': named, 'op': 'state.client_request', 'body_type': 'json',
                     'deadline_ms': 20000,
                     'origin': {'client_id': 'mac-b', 'machine_tag': None, 'session': None}},
                    {'request': request('status')})
        header, _ = attach.receive(named)
        assert header['kind'] == 'reply' and header['ok'] is False
        assert header['error']['code'] == 'E_PROTOCOL'

        plain = new_id()
        attach.send({'v': 2, 'kind': 'call', 'id': plain, 'op': 'state.client_request', 'body_type': 'json',
                     'deadline_ms': 20000}, {'request': request('status')})
        header, body = attach.receive(plain)
        assert header['ok'] is True and body['ok'] is True
        assert body['result']['client_id'] == ENROLLED and body['result']['writer'] == 'shared'


def test_an_unknown_client_id_gets_a_refusal_not_a_result(daemon):
    completed = subprocess.run(attach_command(daemon.runtime, STRANGER, 1), input=STATUS_LINE + b'\n',
                               capture_output=True, timeout=60)
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    reply = json.loads(lines[0])
    assert reply['ok'] is False and 'result' not in reply
    assert reply['request_id'] == 'r1' and reply['error']['code'] == 'E_ORIGIN'
