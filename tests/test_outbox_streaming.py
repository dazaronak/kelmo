"""A flush walks the outbox one entry at a time and counts it without loading bodies.

The client is a real `SharedClient` on a real profile and SQLite outbox. The server is a stand-in channel
given as the client's channel factory: it answers `hello` with the profile's authority, records the key of
every request in the order it arrives, and answers ok or a scripted refusal. Memory is the peak `tracemalloc`
sees above its size when measuring starts; the entries are queued before that.
"""
import fcntl
import gc
import tracemalloc

import pytest

from kelmo.client import Outbox, SharedClient, prepare_profile
from kelmo.config import ClientProfile
from kelmo.errors import KelmoError
from kelmo.server import PROTOCOL

AUTHORITY = '0b1e6b0c-6f7a-4d4e-9c1a-5a3d2f1e0c9b'
MIB = 1 << 20
LIMIT = 8 * MIB


class Server:
    """The stand-in channel. `refuse` maps a key to a refusal {code, message, retryable}; `during` maps a key
    to a function called while that request is being answered."""

    def __init__(self, refuse=None, during=None):
        self.keys = []
        self.refuse = dict(refuse or {})
        self.during = dict(during or {})

    def __call__(self):
        return self

    def request(self, frame):
        reply = {'protocol': PROTOCOL, 'request_id': frame['request_id']}
        if frame['op'] == 'hello':
            return dict(reply, ok=True, result={'authority_id': AUTHORITY, 'operations': ['hello', 'capture']})
        key = frame.get('idempotency_key')
        self.keys.append(key)
        if key in self.during:
            self.during.pop(key)()
        if key in self.refuse:
            return dict(reply, ok=False, error=self.refuse.pop(key))
        return dict(reply, ok=True, result={'accepted': True, 'key': key})

    def close(self):
        pass


def unreachable():
    raise KelmoError('E_TRANSPORT', 'Synthetic: no server answers.')


@pytest.fixture
def make_client(tmp_path):
    made = []

    def make(factory, multiplex=False):
        root = tmp_path / 'client'
        if not (root / 'client.toml').exists():
            prepare_profile(root, AUTHORITY, ['/bin/false'])
        client = SharedClient(ClientProfile.load(root), channel_factory=factory, multiplex=multiplex)
        made.append(client)
        return client
    yield make
    for client in made:
        client.close()


def queue(client, count, size=0, prefix='e'):
    for number in range(count):
        content = 'x' * size if size else f'note {number}'
        client.outbox.enqueue('capture', {'content': content, 'filename': f'{prefix}{number}.md'},
                              f'{prefix}{number}')
        del content
    gc.collect()


def peak_above_start(action):
    """(result of action, peak bytes above the traced size when measuring started)."""
    gc.collect()
    tracemalloc.start()
    try:
        start = tracemalloc.get_traced_memory()[0]
        result = action()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return result, peak - start


def pending_keys(client):
    return [row['idempotency_key'] for row in client.outbox.pending()]


def test_a_flush_over_large_entries_holds_one_at_a_time(make_client):
    server = Server()
    client = make_client(server)
    queue(client, 50, MIB)
    result, peak = peak_above_start(client.flush)
    assert result['delivered'] == 50 and result['pending'] == 0, result
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'
    assert server.keys == [f'e{n}' for n in range(50)]


def test_a_multiplexed_flush_over_large_entries_holds_one_at_a_time(make_client):
    server = Server()
    client = make_client(server, multiplex=True)
    queue(client, 50, MIB)
    result, peak = peak_above_start(client.flush)
    assert result['delivered'] == 50 and result['pending'] == 0, result
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'


def test_a_flush_that_reaches_no_server_counts_without_bodies(make_client):
    client = make_client(unreachable)
    queue(client, 50, MIB)
    result, peak = peak_above_start(client.flush)
    assert result['delivered'] == 0 and result['pending'] == 50, result
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'


def test_a_flush_that_finds_the_lock_held_counts_without_bodies(make_client):
    client = make_client(Server())
    queue(client, 50, MIB)
    with (client.profile.state / 'flush.lock').open('a') as other:
        fcntl.flock(other, fcntl.LOCK_EX)

        def busy_flush():
            with client.deadline(0.3):
                return client.flush()
        result, peak = peak_above_start(busy_flush)
    assert result['stopped'] == 'E_BUSY' and result['pending'] == 50, result
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'


def test_status_counts_without_bodies(make_client):
    client = make_client(unreachable)
    queue(client, 50, MIB)
    status, peak = peak_above_start(client.local_status)
    assert status['outbox']['pending'] == 50, status
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'


def test_a_retryable_refusal_keeps_it_and_everything_after_pending_in_order(make_client):
    server = Server(refuse={'e1': {'code': 'E_BUSY', 'message': 'Synthetic: try later.', 'retryable': True}})
    client = make_client(server)
    queue(client, 5)
    first = client.flush()
    assert first['delivered'] == 1 and first['stopped'] == 'E_BUSY' and first['pending'] == 4, first
    assert server.keys == ['e0', 'e1']
    assert pending_keys(client) == ['e1', 'e2', 'e3', 'e4']
    second = client.flush()
    assert second['delivered'] == 4 and second['pending'] == 0, second
    assert server.keys == ['e0', 'e1', 'e1', 'e2', 'e3', 'e4']


def _queue_late(client):
    def late():
        other = Outbox(client.profile.state)
        try:
            other.enqueue('capture', {'content': 'late', 'filename': 'late.md'}, 'late')
        finally:
            other.close()
    return late


def _delivered_once_over_two_flushes(client, server):
    client.flush()
    first = server.keys.count('late')
    state = client.outbox.get('late')['state']
    assert (first, state) in ((1, 'delivered'), (0, 'pending')), (first, state, server.keys)
    client.flush()
    assert server.keys.count('late') == 1, server.keys
    assert client.outbox.get('late')['state'] == 'delivered'
    third = client.flush()
    assert third['delivered'] == 0 and third['pending'] == 0, third
    assert server.keys.count('late') == 1, server.keys


def test_an_entry_queued_during_a_flush_is_delivered_once(make_client):
    server = Server()
    client = make_client(server)
    queue(client, 3)
    server.during['e0'] = _queue_late(client)
    _delivered_once_over_two_flushes(client, server)


def test_an_entry_queued_during_the_last_delivery_is_delivered_once(make_client):
    server = Server()
    client = make_client(server)
    queue(client, 3)
    server.during['e2'] = _queue_late(client)
    _delivered_once_over_two_flushes(client, server)


def test_status_with_refused_entries_counts_without_bodies(make_client):
    server = Server(refuse={f'r{n}': {'code': 'E_INPUT', 'message': 'Synthetic refusal.', 'retryable': False}
                            for n in range(20)})
    client = make_client(server)
    queue(client, 20, MIB, prefix='r')
    assert client.flush()['refused'] and client.outbox.summary()['pending'] == 0
    client._factory = unreachable
    client._drop()
    queue(client, 50, MIB)
    status, peak = peak_above_start(client.local_status)
    assert status['outbox']['pending'] == 50 and len(status['outbox']['refused']) == 20, status
    assert peak < LIMIT, f'peak {peak / MIB:.1f} MiB above the start'


def test_the_count_and_the_head_lookup_use_the_state_index(make_client):
    client = make_client(Server())
    queue(client, 3)
    conn = client.outbox.conn
    plans = [' '.join(str(row[-1]) for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, args))
             for sql, args in (("SELECT COUNT(*) FROM entries WHERE state='pending'", ()),
                               ("SELECT seq, idempotency_key, op FROM entries WHERE state='pending' AND seq>? "
                                'AND seq<=? ORDER BY seq LIMIT 1', (0, 10)))]
    assert all('entries_state' in plan for plan in plans), plans


def test_a_flush_does_not_chase_entries_queued_after_it_started(make_client):
    server = Server()
    client = make_client(server)
    queue(client, 3)
    counter = iter(range(1000))

    def keep_queueing():
        number = next(counter)
        other = Outbox(client.profile.state)
        try:
            other.enqueue('capture', {'content': f'late {number}', 'filename': f'late{number}.md'},
                          f'late{number}')
        finally:
            other.close()
        server.during[f'late{number}'] = keep_queueing
    for key in ('e0', 'e1', 'e2'):
        server.during[key] = keep_queueing
    result = client.flush()
    assert result['delivered'] == 3, result
    assert server.keys == ['e0', 'e1', 'e2'], server.keys
    assert result['pending'] == 3, result
