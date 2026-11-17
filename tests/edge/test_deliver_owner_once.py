"""The edge deliverer takes the owner of the outbox with one try: while another caller holds it, a pass with entries
pending ends at once and leaves them to that caller, and the deliverer looks again a second later."""

import threading
import time

from kelmo.client import SharedClient
from kelmo.core import edge_core
from kelmo.core.edge_core import EdgeCore


def _passes(core, count, seconds=10):
    for _ in range(count):
        before = core._last.get('at')
        core._wake.set()
        stop = time.monotonic() + seconds
        while core._last.get('at') == before and time.monotonic() < stop:
            time.sleep(0.02)
        assert core._last.get('at') != before, 'the deliverer finished no pass'


def test_a_pass_with_entries_pending_does_not_wait_for_a_held_owner(profile):
    client = SharedClient(profile, multiplex=True)
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
    client = SharedClient(profile, multiplex=True)
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
    finally:
        core.close()
