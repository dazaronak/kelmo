"""`SharedClient(multiplex=True)`, the client of the edge core, against a small peer process: reads overlap on one
kelmo/2 channel and do not wait for the owner of the outbox; a kelmo-client/1 server is still served, one request
at a time."""
import sys
import threading
import time
from pathlib import Path

import pytest

from kelmo.client import Busy, MuxChannel, SharedClient, StdioChannel, Unbound, prepare_profile
from kelmo.config import ClientProfile

PEER = Path(__file__).with_name('mux_peer.py')
AUTHORITY = '00000000-0000-4000-8000-0000000c11e7'


@pytest.fixture
def client_for(tmp_path):
    """Builds a multiplexing client whose channel command is the peer with the given arguments."""
    opened = []

    def build(*arguments):
        root = tmp_path / f'client-{len(opened)}'
        prepare_profile(root, AUTHORITY, [sys.executable, str(PEER), '--authority', AUTHORITY, *arguments])
        client = SharedClient(ClientProfile.load(root), multiplex=True)
        opened.append(client)
        return client

    yield build
    for client in opened:
        client.close()


def in_thread(function, *args):
    """Starts the call in a thread; the returned `wait` gives its result or the exception it raised."""
    outcome = []

    def run():
        try:
            outcome.append(function(*args))
        except Exception as error:  # noqa: BLE001 - the test asserts on what the call did
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def wait(timeout=10):
        thread.join(timeout)
        assert not thread.is_alive(), 'the call is still blocked'
        return outcome[0]

    wait.thread = thread
    return wait


def test_two_reads_overlap_on_one_channel(client_for):
    client = client_for()
    client.call('search', {})  # the channel is open before the clock starts
    channel = client._channel
    assert isinstance(channel, MuxChannel)

    started = time.monotonic()
    first = in_thread(client.call, 'search', {'delay': 0.6, 'n': 1})
    second = in_thread(client.call, 'search', {'delay': 0.6, 'n': 2})
    answers = [first()['echo']['n'], second()['echo']['n']]
    elapsed = time.monotonic() - started

    assert answers == [1, 2]
    # One after the other they take 1.2 s; together about 0.6 s. The margin covers a loaded machine.
    assert elapsed < 1.0
    assert client._channel is channel


def test_a_read_does_not_wait_for_the_owner_of_the_outbox(client_for):
    client = client_for()
    with client._lock:
        read = in_thread(client.call, 'search', {'n': 1})
        assert read(timeout=5)['echo'] == {'n': 1}


def test_a_flush_with_nothing_queued_does_not_wait_for_the_owner_either(client_for):
    client = client_for()
    with client._lock:
        flushed = in_thread(client.flush)(timeout=5)
    assert flushed == {'delivered': 0, 'refused': [], 'pending': 0, 'stopped': None, 'reached': None}


def test_a_flush_refused_by_a_busy_owner_leaves_the_entry_pending(client_for):
    client = client_for()

    class Refusing:
        def __enter__(self):
            raise Busy('The edge core is busy with another write; retry.')

        def __exit__(self, *_):
            return False

    client.outbox.enqueue('capture', {'content': 'synthetic'}, 'key-1')
    owner, client._lock = client._lock, Refusing()
    try:
        flushed = client.flush()
    finally:
        client._lock = owner
    assert (flushed['stopped'], flushed['pending'], flushed['delivered']) == ('E_BUSY', 1, 0)

    assert client.flush()['delivered'] == 1


def test_a_read_past_its_deadline_fails_alone_and_the_channel_stays(client_for):
    client = client_for()
    client.call('search', {})
    channel = client._channel

    with pytest.raises(Unbound) as late, client.deadline(0.3):
        client.call('search', {'delay': 3})

    assert late.value.code == 'E_TRANSPORT'
    assert client._channel is channel
    assert client.call('search', {'n': 2})['echo'] == {'n': 2}


def test_a_channel_that_stops_answering_is_replaced(client_for):
    client = client_for()
    client.call('search', {})
    channel = client._channel

    for _ in range(MuxChannel.STALL_TIMEOUTS):
        with pytest.raises(Unbound), client.deadline(0.2):
            client.call('search', {'delay': 30})

    assert client._channel is None
    assert not channel.alive
    assert client.call('search', {'n': 3})['echo'] == {'n': 3}
    assert client._channel is not channel


def test_a_channel_whose_process_ended_is_replaced_on_the_next_read(client_for):
    client = client_for('--die-after', '2')  # the hello and one read
    assert client.call('search', {'n': 1})['echo'] == {'n': 1}
    channel = client._channel
    channel.process.wait(timeout=5)

    assert client.call('search', {'n': 2})['echo'] == {'n': 2}
    assert client._channel is not channel


def test_a_write_whose_channel_dies_stays_queued_and_goes_out_again_under_its_key(client_for, tmp_path):
    client = client_for('--die-once', str(tmp_path / 'died'))

    first = client.submit('capture', {'content': 'synthetic'}, 'key-1')

    assert (first['queued'], first['accepted'], first['pending_reason']) == (True, False, 'E_TRANSPORT')
    assert client.outbox.summary()['pending'] == 1

    assert client.flush()['delivered'] == 1
    delivered = client.submit('capture', {'content': 'synthetic'}, 'key-1')
    assert delivered == {'echo': {'content': 'synthetic'}, 'key': 'key-1'}
    assert client.outbox.summary()['delivered'] == 1


def test_a_kelmo_client_1_server_is_served_one_request_at_a_time(client_for):
    client = client_for('--legacy')

    assert client.call('search', {'n': 1})['echo'] == {'n': 1}
    assert isinstance(client._channel, StdioChannel)
    assert client._legacy_until > time.monotonic()  # kelmo/2 is not offered again on every reconnect

    with client._lock:
        read = in_thread(client.call, 'search', {'n': 2})
        read.thread.join(0.5)
        assert read.thread.is_alive(), 'a read on a one-request channel must wait for the owner'
    assert read(timeout=5)['echo'] == {'n': 2}
