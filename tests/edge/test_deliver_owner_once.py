"""The edge deliverer takes the owner of the outbox with one try: while another caller holds it, a pass with entries
pending ends at once and leaves them to that caller, and the deliverer looks again a second later."""

import threading
import time

from kelmo.client import SharedClient
from kelmo.core import edge_core
from kelmo.core.edge_core import EdgeCore, OwnerLock


class Answering:
    """An authority that answers every request at once, so a pass that reaches the channel delivers."""

    def __init__(self, authority):
        self.authority = authority

    def request(self, frame, timeout=None):
        from kelmo.client import PROTOCOL
        result = ({'authority_id': self.authority, 'operations': ['hello', 'session_append']}
                  if frame['op'] == 'hello' else {'ok': True})
        return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

    def close(self):
        pass


def _client(profile):
    return SharedClient(profile, channel_factory=lambda: Answering(profile.authority_id), multiplex=True)


def _passes(core, count, seconds=10):
    for _ in range(count):
        before = core._last.get('at')
        core._wake.set()
        stop = time.monotonic() + seconds
        while core._last.get('at') == before and time.monotonic() < stop:
            time.sleep(0.02)
        assert core._last.get('at') != before, 'the deliverer finished no pass'


def test_a_pass_with_entries_pending_does_not_wait_for_a_held_owner(profile):
    client = _client(profile)
    core = EdgeCore(profile.root, client=client)
    _passes(core, 1)
    taken, release = threading.Event(), threading.Event()
    sent = []
    real = client._send

    def send(channel, op, *args, **kwargs):
        sent.append(op)
        return real(channel, op, *args, **kwargs)

    client._send = send

    def hold():
        core._owner.acquire()
        taken.set()
        release.wait(30)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert taken.wait(5)
        client.outbox.enqueue('session_append', {'content': 'x', 'provider': 'claude'}, 'pending-while-held')
        started = time.monotonic()
        _passes(core, 1)
        took = time.monotonic() - started
        assert took < 1.0, f'the pass waited {took:.2f} s for the held owner'
        assert core._last['result'].get('stopped') == 'E_BUSY', core._last
        assert 'session_append' not in sent, sent
        before = core._last['at']
        stop = time.monotonic() + edge_core.BUSY_RETRY_SECONDS + 1.5
        while core._last.get('at') == before and time.monotonic() < stop:
            time.sleep(0.05)
        assert core._last.get('at') != before, 'the deliverer did not look again a second later'
    finally:
        release.set()
        holder.join(10)
        core.close()


def test_a_pass_that_finds_the_owner_free_delivers_as_before(profile):
    client = _client(profile)
    core = EdgeCore(profile.root, client=client)
    _passes(core, 1)
    delivered = []
    real = client._deliver

    def deliver():
        result = real()
        delivered.append(result)
        return result

    client._deliver = deliver
    try:
        client.outbox.enqueue('session_append', {'content': 'x', 'provider': 'claude'}, 'free-owner')
        _passes(core, 1)
        assert delivered, 'the pass did not try to deliver with the owner free'
        assert client.outbox.get('free-owner')['state'] == 'delivered', client.outbox.get('free-owner')
    finally:
        core.close()


def test_a_pass_whose_flush_lock_is_held_counts_pending_with_the_owner_released(profile):
    """Another process holds the flush lock: the pass gives up, and the pending count it reports is read after the
    owner is released, so a hook queued meanwhile does not wait for that read."""
    from contextlib import contextmanager
    client = _client(profile)
    core = EdgeCore(profile.root, client=client)
    _passes(core, 1)

    @contextmanager
    def held_elsewhere():
        yield False

    client._flush_lock = held_elsewhere
    owner_held = []
    real = client._pending_within

    def pending_within():
        owner_held.append(core._owner.held())
        return real()

    client._pending_within = pending_within
    try:
        client.outbox.enqueue('session_append', {'content': 'x', 'provider': 'claude'}, 'flush-lock-held')
        owner_held.clear()
        _passes(core, 1)
        assert core._last['result'].get('stopped') == 'E_BUSY', core._last
        assert owner_held and not any(owner_held), f'the pending count was read holding the owner: {owner_held}'
    finally:
        core.close()


def test_a_one_try_acquire_refuses_while_a_caller_waits():
    """Nobody holds the owner but a caller waits for it: a background pass's one try is refused, so it never takes
    a turn a waiting hook or tool is owed."""
    lock, ticket = OwnerLock(), object()
    with lock._condition:
        lock._waiting[True].append(ticket)  # a hook waiting, between the holder's release and its own wake-up
    try:
        assert not lock.acquire_if_idle(), 'a one-try acquire took the owner while a caller waited'
    finally:
        with lock._condition:
            lock._waiting[True].remove(ticket)
    assert lock.acquire_if_idle()
    lock.release()
