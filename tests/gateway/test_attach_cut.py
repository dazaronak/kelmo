"""A shared client over a real `kelmod attach --protocol 1` when the daemon behind the bridge goes away.

The daemon is `scripted_daemon.ScriptedDaemon`: it answers each `state.client_request` with a
`kelmo-client/1` reply body, as the state core does, or cuts the connection where its script says. The
bridge is the `kelmod attach` binary built from this tree; the client is a real `SharedClient` whose
profile command is that bridge, so every reconnect starts a new bridge process.

A read cut after it was sent is sent once more and answers. A keyed write cut after it was sent stays
pending in the outbox, and the next flush sends it under the same key. A request without a key that was
cut after it was sent is refused as having an unknown effect.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

DAEMON_TESTS = Path(__file__).resolve().parents[1] / 'daemon'
if str(DAEMON_TESTS) not in sys.path:
    sys.path.insert(0, str(DAEMON_TESTS))

from daemon_harness import KELMOD  # noqa: E402
from scripted_daemon import ANSWER, CLOSE, Connection, ScriptedDaemon  # noqa: E402

from kelmo.client import SharedClient, prepare_profile  # noqa: E402
from kelmo.config import ClientProfile  # noqa: E402
from kelmo.errors import KelmoError  # noqa: E402
from kelmo.server import PROTOCOL  # noqa: E402

AUTHORITY = '0b1e6b0c-6f7a-4d4e-9c1a-5a3d2f1e0c9b'
CLIENT_ID = 'mac-attach'


def v1_answer(header, body, number):
    """The `/1` reply the state core would give: hello names the authority, every other request echoes its
    op, its key and the call number."""
    request = body['request']
    if request['op'] == 'hello':
        result = {'authority_id': AUTHORITY, 'client_id': CLIENT_ID,
                  'operations': ['hello', 'search', 'create', 'status']}
    else:
        result = {'op': request['op'], 'key': request.get('idempotency_key'), 'call': number}
    return {'protocol': PROTOCOL, 'request_id': request['request_id'], 'ok': True, 'result': result}


def requests(daemon):
    """(op, header key, request key) of every call the daemon read, hellos left out."""
    seen = []
    for header, body in daemon.calls:
        request = body['request']
        if request['op'] != 'hello':
            seen.append((request['op'], header.get('key'), request.get('idempotency_key')))
    return seen


@pytest.fixture
def bridge(tmp_path):
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    made = []

    def make(*script):
        daemon = ScriptedDaemon(*script, answer=v1_answer)
        command = [str(KELMOD), 'attach', '--client-id', CLIENT_ID, '--protocol', '1',
                   '--runtime-dir', str(daemon.directory)]
        root = tmp_path / f'client-{len(made)}'
        prepare_profile(root, AUTHORITY, command, timeout_seconds=20)
        client = SharedClient(ClientProfile.load(root))
        made.append((daemon, client))
        return daemon, client
    yield make
    for daemon, client in made:
        client.close()
        daemon.close()


def test_a_read_cut_after_it_was_sent_is_sent_again_and_answers(bridge):
    daemon, client = bridge(Connection(ANSWER, CLOSE), Connection(ANSWER, ANSWER))
    started = time.monotonic()
    try:
        with client.deadline(10):
            result = client.call('search', {'query': 'q'})
    except KelmoError as error:
        result = {'refused': error.code, 'message': str(error)}
    assert result.get('op') == 'search', result
    assert time.monotonic() - started < 10
    assert requests(daemon) == [('search', None, None), ('search', None, None)]
    assert daemon.connections == 2


def test_a_read_cut_twice_gives_a_typed_error_inside_the_deadline(bridge):
    daemon, client = bridge(Connection(ANSWER, CLOSE), Connection(ANSWER, CLOSE), Connection(ANSWER, ANSWER))
    started = time.monotonic()
    with pytest.raises(KelmoError) as error, client.deadline(6):
        client.call('search', {'query': 'q'})
    assert error.value.code == 'E_TRANSPORT', error.value
    assert time.monotonic() - started < 6.5
    assert requests(daemon) == [('search', None, None), ('search', None, None)]


def test_a_keyed_write_cut_after_it_was_sent_stays_pending_and_goes_again_under_its_key(bridge):
    daemon, client = bridge(Connection(ANSWER, CLOSE), Connection(ANSWER, ANSWER))
    try:
        queued = client.submit('create', {'content': 'Kept across a restart.', 'filename': 'kept.md'}, 'k-1')
    except KelmoError as error:
        queued = {'refused': error.code, 'message': str(error)}
    assert queued.get('queued') is True, queued
    assert client.outbox.get('k-1')['state'] == 'pending'
    flushed = client.flush()
    assert flushed['delivered'] == 1, flushed
    assert client.outbox.get('k-1')['state'] == 'delivered'
    assert requests(daemon) == [('create', 'k-1', 'k-1'), ('create', 'k-1', 'k-1')]


def test_a_keyless_request_cut_after_it_was_sent_is_refused_as_unknown(bridge):
    daemon, _ = bridge(Connection(CLOSE))
    command = [str(KELMOD), 'attach', '--client-id', CLIENT_ID, '--protocol', '1',
               '--runtime-dir', str(daemon.directory)]
    line = json.dumps({'protocol': PROTOCOL, 'request_id': 'r-1', 'op': 'create',
                       'args': {'content': 'No key.', 'filename': 'nokey.md'}}) + '\n'
    finished = subprocess.run(command, input=line.encode(), capture_output=True, timeout=30)
    reply = json.loads(finished.stdout.decode().splitlines()[0])
    assert reply['request_id'] == 'r-1' and reply['ok'] is False, reply
    assert reply['error']['code'] == 'E_TRANSPORT' and reply['error']['retryable'] is False, reply
    assert 'unknown' in reply['error']['message'], reply
    assert daemon.connections == 1 and len(daemon.calls) == 1
