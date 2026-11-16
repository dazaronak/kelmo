"""A `kelmo create --wait` sent to the edge core (`edge.cli`) holds the outbox for the whole command and lends it
out between its polls. Taking it back is bounded by the command's deadline, and an urgent hook that waits
meanwhile still goes first.

The core, its owner lock, the CLI and `ClientService` are real. The client stands in for the authority only: it
keeps one monotonic deadline per thread as `SharedClient` does, accepts the capture, and answers each trace poll
from a script. A thread is known to be waiting for the owner lock through a hook on the lock's condition, never
by sleeping.
"""

import threading
import time
from contextlib import contextmanager

from kelmo.client import Unbound
from kelmo.core.edge_core import EdgeCore
from kelmo.errors import KelmoError

OPERATION = 'op-synthetic-1'
SLACK = 0.1  # starting the command's thread and joining it


class Context:
    """The CoreLink context of one call: how much of its deadline is left."""

    def __init__(self, remaining):
        self._remaining = remaining

    def remaining(self):
        return self._remaining

    def check(self):
        """Never cancelled."""


class Authority:
    """The SharedClient surface the edge core, `hook()` and `ClientService` use here: deadlines are real, the
    authority accepts one capture and answers trace polls.

    `applied_at` is the poll whose trace is terminal (None: never). `order` lists, in order, the write the
    command queued, its polls and the captures that hooks queued. `timeout_seconds` caps `remaining()` as the
    profile's timeout caps it in SharedClient (None: no cap)."""

    def __init__(self, profile, applied_at=None, timeout_seconds=None):
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.flushed = threading.Event()
        self.wait_seconds = 5.0  # what remaining() answers outside any deadline
        self.files = threading.RLock()
        self._lock = threading.RLock()  # the edge core replaces it with its owner gate
        self.applied_at = applied_at
        self.order = []
        self.polls = 0
        self.polled = threading.Event()
        self._local = threading.local()

    @contextmanager
    def deadline(self, seconds):
        previous = getattr(self._local, 'deadline', None)
        limit = time.monotonic() + seconds
        self._local.deadline = limit if previous is None else min(previous, limit)
        try:
            yield
        finally:
            self._local.deadline = previous

    def remaining(self):
        limit = getattr(self._local, 'deadline', None)
        if limit is None:
            return self.wait_seconds
        left = limit - time.monotonic()
        if left <= 0:
            raise Unbound('E_TRANSPORT', 'The deadline passed before the shared kelmo server answered.')
        return left if self.timeout_seconds is None else min(self.timeout_seconds, left)

    def time_left(self):
        limit = getattr(self._local, 'deadline', None)
        return None if limit is None else max(0.0, limit - time.monotonic())

    def submit(self, op, args, key):
        with self._lock:  # a write queues in the outbox under the owner gate, as in SharedClient
            self.order.append(op)
        return {'operation': {'id': OPERATION, 'state': 'queued', 'terminal': False}}

    def call(self, op, args):
        assert op == 'trace' and args == {'operation_id': OPERATION}
        self.remaining()  # refused once the deadline passed, as SharedClient refuses it
        self.polls += 1
        self.order.append(f'poll {self.polls}')
        state = 'applied' if self.applied_at is not None and self.polls >= self.applied_at else 'queued'
        self.polled.set()
        return {'operation': {'id': OPERATION, 'state': state, 'terminal': state == 'applied'}, 'events': []}

    def capture_session(self, provider, action, payload, *, deliver=True, restart=True, internal_job=None):
        with self._lock:  # a capture queues in the outbox, as in SharedClient
            self.order.append(f'hook {action}')
        return {'queued': True, 'accepted': False}

    def write(self):
        """What a tool that queues something does to the outbox."""
        with self._lock:
            return {}

    def flush(self):
        self.flushed.set()
        return {'reached': True, 'stopped': None, 'pending': 0, 'delivered': 0}

    def close(self):
        pass


@contextmanager
def edge_core(profile, applied_at=None, timeout_seconds=None):
    client = Authority(profile, applied_at, timeout_seconds)
    core = EdgeCore(profile.root, client=client)
    assert client.flushed.wait(10), 'the deliverer finished no first pass'
    core.wake = lambda: None  # the deliverer must not take the outbox on its own during the test
    try:
        yield core, client
    finally:
        core.close()


def waiters(core, monkeypatch):
    """Returns `arrived(thread)`, an event set once that thread waits on the owner lock's condition."""
    events = {}
    guard = threading.Lock()
    wait = core._owner._condition.wait

    def arrived(thread_ident):
        with guard:
            return events.setdefault(thread_ident, threading.Event())

    def recording_wait(timeout=None):
        arrived(threading.get_ident()).set()
        return wait(timeout)

    monkeypatch.setattr(core._owner._condition, 'wait', recording_wait)
    return arrived


def start(function, *args):
    """Runs the call in a daemon thread; returns the thread and a list that gets its result or exception."""
    outcome = []

    def run():
        try:
            outcome.append(function(*args))
        except Exception as error:  # noqa: BLE001 - the test asserts on what the call did
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def create_wait(core, deadline, wait_timeout=30):
    argv = ['create', '--vault', str(core.root), '--project', 'demo', '--text', 'A synthetic note.',
            '--wait', '--wait-timeout', str(wait_timeout)]
    return core.handle_cli(Context(deadline), {'argv': argv})


def hold_past_the_deadline(core, client, deadline, hold, wait_timeout=30):
    """Starts `create --wait`, takes the owner lock once the command lent it out after its first poll, and holds
    it until the command ends or `hold` seconds pass. Returns the command's outcome and how long it ran."""
    began = time.monotonic()
    thread, outcome = start(create_wait, core, deadline, wait_timeout)
    assert client.polled.wait(5), 'the command made no poll'
    assert core._owner.acquire(timeout=5), 'the command never lent the outbox out'
    try:
        thread.join(hold)
        ended = time.monotonic() - began
    finally:
        core._owner.release()
    thread.join(10)
    assert not thread.is_alive(), 'the command is still blocked'
    return outcome[0], ended


def test_a_paused_cli_wait_that_cannot_take_the_outbox_back_fails_busy_inside_its_deadline(profile):
    with edge_core(profile) as (core, client):
        result, ended = hold_past_the_deadline(core, client, deadline=0.6, hold=3.0)

    assert isinstance(result, KelmoError), result
    assert (result.code, result.retryable) == ('E_BUSY', True)
    assert OPERATION in str(result)
    assert ended < 0.6 + SLACK, ended


def test_after_a_paused_caller_gives_up_the_outbox_is_free_for_the_next_caller(profile, monkeypatch):
    with edge_core(profile) as (core, client):
        result, _ = hold_past_the_deadline(core, client, deadline=0.6, hold=3.0)

        assert isinstance(result, KelmoError) and result.code == 'E_BUSY', result
        assert (core._owner._holder, core._owner._count) == (None, 0)
        monkeypatch.setattr(core, 'tools', lambda: {'stub': client.write})
        thread, outcome = start(core.handle_tool, Context(5), {'tool': 'stub', 'arguments': {}})
        thread.join(5)
        assert outcome == [{}]


def test_a_paused_caller_that_gets_the_outbox_back_in_time_returns_its_result(profile, monkeypatch):
    with edge_core(profile, applied_at=2) as (core, client):
        arrived = waiters(core, monkeypatch)
        thread, outcome = start(create_wait, core, 10.0)
        assert client.polled.wait(5), 'the command made no poll'
        assert core._owner.acquire(timeout=5), 'the command never lent the outbox out'
        try:
            assert arrived(thread.ident).wait(5), 'the command did not wait to take the outbox back'
        finally:
            core._owner.release()
        thread.join(10)

        assert not thread.is_alive(), 'the command is still blocked'
        result = outcome[0]
        assert not isinstance(result, Exception), result
        assert result['operation']['state'] == 'applied'
        assert client.polls == 2
        assert (core._owner._holder, core._owner._count) == (None, 0)


def test_an_urgent_hook_waiting_during_a_pause_goes_before_the_paused_caller(profile, monkeypatch):
    body = {'action': 'session-stop', 'payload': {}, 'context': {'provider': 'claude'}}
    with edge_core(profile, applied_at=2) as (core, client):
        arrived = waiters(core, monkeypatch)
        command, outcome = start(create_wait, core, 10.0)
        assert client.polled.wait(5), 'the command made no poll'
        assert core._owner.acquire(timeout=5), 'the command never lent the outbox out'
        try:
            assert arrived(command.ident).wait(5), 'the command did not wait to take the outbox back'
            hook, hooked = start(core.handle_hook, Context(10.0), body)
            assert arrived(hook.ident).wait(5), 'the hook did not wait for the outbox'
        finally:
            core._owner.release()
        hook.join(10)
        command.join(10)

        assert not command.is_alive() and not hook.is_alive()
        assert hooked == [{}]
        assert not isinstance(outcome[0], Exception), outcome[0]
        assert client.order == ['create', 'poll 1', 'hook session-stop', 'poll 2']


def test_a_tool_call_that_waits_lends_nothing_and_takes_nothing_back(profile):
    arguments = {'content': 'A synthetic note.', 'project': 'demo', 'wait': 30}
    with edge_core(profile, applied_at=2) as (core, client):
        thread, outcome = start(core.handle_tool, Context(30.0), {'tool': 'memory_create', 'arguments': arguments})
        assert client.polled.wait(10), 'the tool made no poll'
        assert core._owner.acquire(timeout=5)
        try:
            thread.join(10)  # the tool polls on while the outbox is held elsewhere
            finished = not thread.is_alive()
        finally:
            core._owner.release()
        thread.join(10)

        assert finished, 'the tool waited for an outbox it never held'
        result = outcome[0]
        assert not isinstance(result, Exception), result
        assert result['operation']['state'] == 'applied'
        assert (core._owner._holder, core._owner._count) == (None, 0)


def test_a_take_back_waits_past_the_profile_timeout_up_to_the_deadline(profile, monkeypatch):
    """SharedClient caps `remaining()` at the profile's timeout (20 s by default); a request deadline of 900 s
    for `--wait` is longer. Getting the outbox back after that cap but before the deadline is getting it back
    in time."""
    with edge_core(profile, applied_at=2, timeout_seconds=0.3) as (core, client):
        arrived = waiters(core, monkeypatch)
        thread, outcome = start(create_wait, core, 10.0)
        assert client.polled.wait(5), 'the command made no poll'
        assert core._owner.acquire(timeout=5), 'the command never lent the outbox out'
        try:
            assert arrived(thread.ident).wait(5), 'the command did not wait to take the outbox back'
            thread.join(0.8)  # held past the 0.3 s cap, well inside the 10 s deadline
        finally:
            core._owner.release()
        thread.join(10)

        assert not thread.is_alive(), 'the command is still blocked'
        result = outcome[0]
        assert not isinstance(result, Exception), result
        assert result['operation']['state'] == 'applied'


def test_a_take_back_woken_after_the_deadline_fails_busy(profile, monkeypatch):
    """The pause's sleep returns late (a busy scheduler), after the deadline, to a free outbox. Taking it then
    would send the next poll past the deadline; the command fails as busy instead."""
    import kelmo.core.edge_core as edge_core_module

    sleep = time.sleep
    monkeypatch.setattr(edge_core_module.time, 'sleep', lambda seconds: sleep(seconds + 0.3))
    with edge_core(profile) as (core, client):
        try:
            result = create_wait(core, 0.2)
        except KelmoError as error:
            result = error

    assert isinstance(result, KelmoError) and result.code == 'E_BUSY', result
    assert client.polls == 1


def test_a_take_back_that_reaches_a_free_outbox_after_the_deadline_fails_busy(profile, monkeypatch):
    """The pause read time left, then the command thread was held up before it asked for the outbox, past the
    deadline. The outbox is free by then, but taking it would send the next poll past the deadline: the command
    gives it back and fails as busy, naming the accepted operation."""
    with edge_core(profile) as (core, client):
        real = core._owner.acquire
        delayed = []

        def late(*args, **kwargs):
            if client.polls >= 1 and not delayed:  # the take-back after the first poll
                delayed.append(True)
                time.sleep(2.3)  # past the 2.0 s deadline
            return real(*args, **kwargs)

        monkeypatch.setattr(core._owner, 'acquire', late)
        try:
            result = create_wait(core, 2.0)
        except KelmoError as error:
            result = error
        assert delayed, 'the command never took the outbox back'
        assert (core._owner._holder, core._owner._count) == (None, 0)

    assert isinstance(result, KelmoError) and result.code == 'E_BUSY', result
    assert OPERATION in str(result)
    assert client.polls == 1


def test_a_wait_timeout_bounds_the_wait_while_the_outbox_is_held(profile):
    """`--wait-timeout` limits the wait (docs/USAGE.md): a held outbox does not stretch it."""
    with edge_core(profile) as (core, client):
        result, ended = hold_past_the_deadline(core, client, deadline=10.0, hold=3.0, wait_timeout=0.3)

    assert not isinstance(result, Exception), result
    assert result['wait_timed_out'] is True
    assert result['operation']['id'] == OPERATION
    assert ended < 0.3 + SLACK, ended


def test_a_wait_timeout_shorter_than_the_request_deadline_still_ends_timed_out(profile, monkeypatch):
    with edge_core(profile) as (core, client):
        arrived = waiters(core, monkeypatch)
        thread, outcome = start(create_wait, core, 10.0, 0.3)
        assert client.polled.wait(5), 'the command made no poll'
        assert core._owner.acquire(timeout=5), 'the command never lent the outbox out'
        try:
            assert arrived(thread.ident).wait(5), 'the command did not wait to take the outbox back'
            thread.join(1.0)  # held past the 0.3 s wait, well inside the 10 s request deadline
        finally:
            core._owner.release()
        thread.join(10)

        assert not thread.is_alive(), 'the command is still blocked'
        result = outcome[0]
        assert not isinstance(result, Exception), result
        assert result['wait_timed_out'] is True
        assert result['operation']['id'] == OPERATION


def test_the_shared_clients_time_left_is_not_capped_by_the_profile_timeout(profile):
    """What the edge core's take-back reads from the real client: the deadline, past the profile's cap."""
    from kelmo.client import SharedClient

    client = SharedClient(profile)
    try:
        assert client.time_left() is None
        with client.deadline(profile.timeout_seconds + 100):
            assert client.remaining() <= profile.timeout_seconds
            assert client.time_left() > profile.timeout_seconds + 99
    finally:
        client.close()
