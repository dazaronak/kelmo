"""The edge core's status while a hook waits for the outbox: it answers at once, says the outbox is busy (not that
status failed), and never goes ahead of the queued hook.

A waiter is known to be queued through a hook on the condition's `wait`, not by sleeping."""
import threading
import time
from types import SimpleNamespace


def queued(lock, monkeypatch):
    """A semaphore released each time a thread starts to wait on the owner lock."""
    arrived = threading.Semaphore(0)
    wait = lock._condition.wait

    def recording_wait(timeout=None):
        arrived.release()
        return wait(timeout)

    monkeypatch.setattr(lock._condition, 'wait', recording_wait)
    return arrived


def hook_in_thread(lock):
    """An urgent caller (a hook) that waits up to 10 s for the lock and gives it back; `wait()` says if it got it."""
    got = []

    def run():
        if lock.acquire(timeout=10, urgent=True):
            got.append(True)
            lock.release()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def wait():
        thread.join(10)
        assert not thread.is_alive(), 'the hook is still waiting'
        return bool(got)

    return wait


def summaries(client, outcome=None):
    """Gives the fake client an outbox whose summary records each call and answers `outcome` (raised if an
    exception)."""
    calls = []

    def summary():
        calls.append(True)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if outcome is not None else {'pending': 0}

    client.outbox = SimpleNamespace(summary=summary, summary_within=lambda _seconds: summary())
    return calls


def test_status_answers_at_once_and_says_busy_while_a_hook_waits(edge, monkeypatch):
    core, client, _ = edge
    summaries(client)
    arrived = queued(core._owner, monkeypatch)
    core._owner.acquire()  # an ordinary caller holds the outbox
    try:
        hook = hook_in_thread(core._owner)
        assert arrived.acquire(timeout=5)
        answers = []
        reader = threading.Thread(target=lambda: answers.append(core.status()), daemon=True)  # the frame reader
        start = time.monotonic()
        reader.start()
        reader.join(5)
        assert time.monotonic() - start < 1, 'status waited for the outbox'
        assert answers[0]['outbox'] == {'busy': True}
    finally:
        core._owner.release()
    assert hook() is True


def test_a_failed_status_read_is_an_error_not_busy(edge):
    core, client, _ = edge
    summaries(client, RuntimeError('the outbox file is unreadable'))
    outbox = core.status()['outbox']
    assert 'busy' not in outbox
    assert 'unreadable' in outbox['error']


def test_status_never_goes_ahead_of_a_queued_hook(edge, monkeypatch):
    core, client, _ = edge
    lock = core._owner
    calls = summaries(client)
    arrived = queued(lock, monkeypatch)
    lock.acquire(urgent=True)  # the last holder is urgent, so an ordinary caller would have the next turn
    hook = hook_in_thread(lock)
    assert arrived.acquire(timeout=5)
    notify_all = lock._condition.notify_all
    monkeypatch.setattr(lock._condition, 'notify_all', lambda: None)
    lock.release()  # free, with the hook queued but not yet woken
    status = core.status()
    monkeypatch.setattr(lock._condition, 'notify_all', notify_all)
    with lock._condition:
        notify_all()
    assert calls == [], 'status read the outbox ahead of the queued hook'
    assert status['outbox'] == {'busy': True}
    assert hook() is True
