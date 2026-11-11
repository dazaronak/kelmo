"""The owner lock of the edge core: reentrant, with urgent callers (hooks) served before ordinary ones.

A waiter is known to be queued through a hook on the condition's `wait`, not by sleeping, so each thread starts
only once the one before it is blocked.
"""

import threading

from kelmo.core.edge_core import OwnerLock


class Context:
    """The CoreLink context of one call: how much of its deadline is left."""

    def __init__(self, remaining):
        self._remaining = remaining

    def remaining(self):
        return self._remaining

    def check(self):
        """Never cancelled, never past its deadline."""


def call_in_thread(function, *args, **kwargs):
    """Runs the call in a daemon thread. The returned `wait` joins the thread and gives the call's result, or the
    exception it raised; a call still blocked after the timeout fails the test."""
    outcome = []

    def run():
        try:
            outcome.append(function(*args, **kwargs))
        except Exception as error:  # noqa: BLE001 - the test asserts on what the call did
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def wait(timeout=10):
        thread.join(timeout)
        assert not thread.is_alive(), 'the call is still blocked'
        return outcome[0]

    return wait


def take(lock, urgent=False, timeout=5):
    """Acquires the lock, gives it back at once, and reports whether it was got."""
    got = lock.acquire(timeout=timeout, urgent=urgent)
    if got:
        lock.release()
    return got


def count_waits(lock, monkeypatch):
    """Returns a semaphore released each time a thread starts to wait on the lock. The waiter is queued before it
    gives up the condition, and it holds the condition while it calls wait, so a release of the lock made after the
    semaphore was taken wakes that waiter."""
    arrived = threading.Semaphore(0)
    wait = lock._condition.wait

    def recording_wait(timeout=None):
        arrived.release()
        return wait(timeout)

    monkeypatch.setattr(lock._condition, 'wait', recording_wait)
    return arrived


def test_reentrant_hold_is_released_only_by_the_outermost_release():
    lock = OwnerLock()
    assert lock.acquire()
    assert lock.acquire(timeout=0)
    lock.release()
    assert call_in_thread(take, lock, timeout=0)() is False
    lock.release()
    assert call_in_thread(take, lock, timeout=0)() is True


def test_urgent_waiter_gets_the_lock_before_an_earlier_ordinary_waiter(monkeypatch):
    lock = OwnerLock()
    arrived = count_waits(lock, monkeypatch)
    order = []

    def take_and_record(name, urgent):
        got = lock.acquire(timeout=5, urgent=urgent)
        if got:
            order.append(name)
            lock.release()
        return got

    lock.acquire()
    ordinary = call_in_thread(take_and_record, 'ordinary', False)
    assert arrived.acquire(timeout=5)
    urgent = call_in_thread(take_and_record, 'urgent', True)
    assert arrived.acquire(timeout=5)
    lock.release()
    assert urgent() is True
    assert ordinary() is True
    assert order == ['urgent', 'ordinary']


def test_timed_out_urgent_waiter_does_not_hold_back_ordinary_waiters(monkeypatch):
    lock = OwnerLock()
    arrived = count_waits(lock, monkeypatch)
    lock.acquire()
    urgent = call_in_thread(take, lock, urgent=True, timeout=0.1)
    assert arrived.acquire(timeout=5)
    assert urgent() is False
    ordinary = call_in_thread(take, lock)
    assert arrived.acquire(timeout=5)
    lock.release()
    assert ordinary() is True


def test_zero_timeout_returns_at_once_on_a_held_or_a_free_lock():
    lock = OwnerLock()
    lock.acquire()
    held = call_in_thread(take, lock, timeout=0)
    assert held(timeout=1) is False
    lock.release()
    assert lock.acquire(timeout=0) is True
    lock.release()


def test_release_by_a_thread_that_does_not_hold_the_lock_raises():
    lock = OwnerLock()
    lock.acquire()
    refused = call_in_thread(lock.release)
    assert isinstance(refused(), RuntimeError)
    assert call_in_thread(take, lock, timeout=0)() is False
    lock.release()


def record_acquires(core, monkeypatch):
    """Lists the `urgent` flag of each owner-lock acquire the calling thread makes from now on."""
    flags = []
    acquire = core._owner.acquire
    caller = threading.get_ident()

    def recording(timeout=None, urgent=False):
        if threading.get_ident() == caller:
            flags.append(urgent)
        return acquire(timeout=timeout, urgent=urgent)

    monkeypatch.setattr(core._owner, 'acquire', recording)
    return flags


def test_handle_hook_takes_the_owner_lock_urgently(edge, monkeypatch):
    core, client, _ = edge
    flags = record_acquires(core, monkeypatch)
    body = {'action': 'session-stop', 'payload': {}, 'context': {'provider': 'claude'}}

    assert core.handle_hook(Context(12.5), body) == {}

    assert flags == [True]
    assert [action for _, action, *_ in client.captured] == ['session-stop']


def test_handle_tool_takes_the_owner_lock_ordinarily(edge, monkeypatch):
    core, _, _ = edge
    flags = record_acquires(core, monkeypatch)
    monkeypatch.setattr(core, 'tools', lambda: {'stub': lambda: {}})

    assert core.handle_tool(Context(12.5), {'tool': 'stub', 'arguments': {}}) == {}

    assert flags == [False]
