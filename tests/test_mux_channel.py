"""Black-box coverage for concurrent kelmo/2 requests over one client process."""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from kelmo.client import MuxChannel
from kelmo.errors import KelmoError


PEER = Path(__file__).with_name('mux_peer.py')


def command(*args):
    return [sys.executable, str(PEER), *args]


def request(request_id, **args):
    return {'protocol': 'kelmo-client/1', 'request_id': request_id, 'op': 'search', 'args': args}


def test_requests_overlap_and_replies_match_their_call_ids():
    channel = MuxChannel(command(), timeout=2)
    results = {}
    failures = []
    returned = []

    def call(name, delay):
        try:
            results[name] = channel.request(request(name, delay=delay))
            returned.append(name)
        except Exception as error:  # Preserve thread failures for the test thread.
            failures.append(error)

    started = time.monotonic()
    slow = threading.Thread(target=call, args=('slow', 0.6))
    fast = threading.Thread(target=call, args=('fast', 0.1))
    try:
        slow.start()
        fast.start()
        slow.join(timeout=1.5)
        fast.join(timeout=1.5)
        elapsed = time.monotonic() - started
        assert not slow.is_alive() and not fast.is_alive()
        assert not failures
        assert results['slow']['result']['echo'] == {'delay': 0.6}
        assert results['fast']['result']['echo'] == {'delay': 0.1}
        assert returned[0] == 'fast'
        # 0.6 s is the critical path; 0.2 s margin covers process scheduling and CI load.
        assert elapsed < 0.8
    finally:
        channel.close()


def test_timed_out_call_does_not_disturb_a_later_request():
    channel = MuxChannel(command(), timeout=2)
    try:
        with pytest.raises(KelmoError) as refused:
            channel.request(request('late', delay=2), timeout=0.3)
        assert refused.value.code == 'E_TRANSPORT'
        assert channel.request(request('next'))['request_id'] == 'next'
        assert channel.alive
    finally:
        channel.close()


def test_peer_exit_fails_all_open_requests_and_future_calls():
    channel = MuxChannel(command('--die-after', '0'), timeout=2)
    results = []

    def call(request_id):
        try:
            channel.request(request(request_id), timeout=1)
        except KelmoError as error:
            results.append(error.code)

    threads = [threading.Thread(target=call, args=(name,)) for name in ('one', 'two')]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.5)
        assert all(not thread.is_alive() for thread in threads)
        assert results == ['E_TRANSPORT', 'E_TRANSPORT']
        assert not channel.alive
        with pytest.raises(KelmoError) as refused:
            channel.request(request('later'))
        assert refused.value.code == 'E_TRANSPORT'
    finally:
        channel.close()


@pytest.mark.parametrize('arguments', [('--legacy',), ('--no-client-request',)])
def test_legacy_or_incompatible_peer_is_identified(arguments):
    with pytest.raises(KelmoError) as refused:
        MuxChannel(command(*arguments), timeout=1)
    assert refused.value.code == 'E_PROTOCOL'
    assert refused.value.legacy is True


def test_the_hello_ends_in_a_newline_that_a_kelmo_2_reader_ignores():
    """The peer's `--legacy` mode answers only a finished line, as the 0.1.1 server does; this is the other half:
    the newline is inside the frame, so a kelmo/2 reader decodes the same hello."""
    from kelmo.core import wire
    hello = {'v': wire.VERSION, 'kind': 'hello', 'id': 'ab' * 16, 'body_type': 'json', 'protocols': ['kelmo/2'],
             'operations': []}
    frame = wire.encode(hello, None, line_end=True)
    assert frame.endswith(b'\n')
    assert wire.decode(frame) == (hello, None)
    assert list(wire.FrameReader().feed(frame + wire.encode(hello))) == [(hello, None), (hello, None)]


def test_daemon_refusal_is_bound_to_the_original_request():
    channel = MuxChannel(command('--refuse', 'E_UNAVAILABLE'), timeout=1)
    try:
        response = channel.request(request('refused'))
        assert response == {
            'protocol': 'kelmo-client/1',
            'request_id': 'refused',
            'ok': False,
            'error': {'code': 'E_UNAVAILABLE', 'message': 'refused', 'retryable': True},
        }
    finally:
        channel.close()


def test_idempotency_key_is_sent_in_the_call_header():
    channel = MuxChannel(command(), timeout=1)
    try:
        frame = request('keyed')
        frame['idempotency_key'] = 'write-key'
        response = channel.request(frame)
        assert response['result']['key'] == 'write-key'
    finally:
        channel.close()


def test_close_is_idempotent_and_marks_the_channel_dead():
    channel = MuxChannel(command(), timeout=1)
    channel.close()
    channel.close()
    assert not channel.alive


def test_a_request_on_a_channel_whose_pipe_closed_leaves_no_waiter_behind():
    channel = MuxChannel(command(), timeout=2)

    def closed(data, deadline):
        raise ValueError('I/O operation on closed file.')  # the pipe closed under the write; the peer still runs

    channel._write = closed
    try:
        with pytest.raises(ValueError):
            channel.request(request('after-close'))
        assert channel._waiters == {}, channel._waiters
    finally:
        channel.close()


def test_a_cancel_that_finds_the_pipe_closed_still_ends_the_call_on_its_deadline():
    channel = MuxChannel(command(), timeout=0.3)
    real, writes = channel._write, []

    def write(data, deadline):
        writes.append(data)
        if len(writes) > 1:
            raise ValueError('I/O operation on closed file.')
        return real(data, deadline)

    channel._write = write
    try:
        with pytest.raises(KelmoError) as raised:
            channel.request(request('slow', delay=2))
        assert raised.value.code == 'E_TRANSPORT' and len(writes) == 2, (raised.value, writes)
        assert not channel.alive, 'a channel whose pipe closed under the cancel stayed in use'
    finally:
        channel.close()


def test_a_request_that_cannot_take_the_write_lock_before_its_deadline_is_unsent():
    """Another thread's write holds the pipe past this request's deadline: nothing of this frame was written, so it
    fails as unsent, on its deadline, and the channel stays in use."""
    from kelmo.client import Unsent
    channel = MuxChannel(command(), timeout=0.3)
    held, release = threading.Event(), threading.Event()

    def hold():
        with channel._write_lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert held.wait(5)
        started = time.monotonic()
        with pytest.raises(KelmoError) as refused:
            channel.request(request('behind-a-write'))
        took = time.monotonic() - started
        assert took < 1.0, f'the request waited {took:.2f} s for the write lock past its 0.3 s deadline'
        assert isinstance(refused.value, Unsent), type(refused.value)
        assert channel.alive and channel._waiters == {}, channel._waiters
    finally:
        release.set()
        holder.join(10)
        channel.close()


def test_a_frame_whose_pipe_closes_after_part_of_it_went_out_ends_the_channel(monkeypatch):
    """The pipe closes under a frame after some of its bytes went out: the peer may hold a prefix, so the request
    fails as a transport error and the channel is not used again, never as an unsent request that may go again."""
    import kelmo.client as client_module
    channel = MuxChannel(command(), timeout=2)
    descriptor = channel.process.stdin.fileno()
    real, calls = os.write, []

    def write(fd, data):
        if fd != descriptor:
            return real(fd, data)
        calls.append(len(data))
        if len(calls) == 1:
            return real(fd, bytes(data[:1]))  # one byte of the frame reaches the peer
        raise ValueError('I/O operation on closed file.')

    monkeypatch.setattr(client_module.os, 'write', write)
    try:
        try:
            channel.request(request('cut-mid-frame'))
            outcome = None
        except Exception as error:  # noqa: BLE001 - the kind of failure is what this test checks
            outcome = error
        monkeypatch.undo()
        assert isinstance(outcome, KelmoError), f'a frame cut after {calls} reached the caller as {outcome!r}'
        assert not channel.alive, 'a channel that may hold a prefix of a frame stayed in use'
    finally:
        monkeypatch.undo()
        channel.close()


def test_a_request_on_a_closed_channel_is_refused_as_closed():
    """A channel another thread closed before this request: nothing of it was written, so it is refused as Closed,
    which a delivery counts as no attempt and a read sends again on a new channel."""
    from kelmo.client import Closed
    channel = MuxChannel(command(), timeout=1)
    channel.close()
    with pytest.raises(KelmoError) as refused:
        channel.request(request('after-close'))
    assert isinstance(refused.value, Closed), type(refused.value)


def test_a_pipe_that_closes_before_a_byte_of_the_frame_goes_out_refuses_it_as_closed(monkeypatch):
    """The peer exited after the channel's checks and before the frame's first byte: nothing was written, so the
    request is refused as Closed (no attempt, no refusal kept) and the channel is not used again."""
    import errno

    import kelmo.client as client_module
    from kelmo.client import Closed
    channel = MuxChannel(command(), timeout=2)
    descriptor = channel.process.stdin.fileno()
    real = os.write

    def write(fd, data):
        if fd != descriptor:
            return real(fd, data)
        raise BrokenPipeError(errno.EPIPE, 'Broken pipe')

    monkeypatch.setattr(client_module.os, 'write', write)
    try:
        try:
            channel.request(request('gone-before-frame'))
            outcome = None
        except Exception as error:  # noqa: BLE001 - the kind of failure is what this test checks
            outcome = error
        monkeypatch.undo()
        assert isinstance(outcome, Closed), f'a frame never written reached the caller as {outcome!r}'
        assert not channel.alive, 'a channel whose peer is gone stayed in use'
    finally:
        monkeypatch.undo()


def test_close_does_not_close_the_pipe_under_a_write_in_progress():
    """The peer stopped reading, so a large request waits inside `_write` for room in the pipe. The close takes the
    pipe away only once that write has stopped, within about one poll slice: its number is never closed under it
    (another open could take the number and receive the rest of the frame)."""
    channel = MuxChannel(command('--stop-reading'), timeout=5)
    failures = []

    def write_big():
        try:
            channel.request(request('big', blob='x' * (1 << 20)))
        except KelmoError as error:
            failures.append(error.code)

    writer = threading.Thread(target=write_big, daemon=True)
    try:
        writer.start()
        stop = time.monotonic() + 5
        while not channel._write_lock.locked() and time.monotonic() < stop:
            time.sleep(0.01)
        assert channel._write_lock.locked(), 'the request never started writing'
        time.sleep(0.2)  # the pipe is full: the write waits for room
        channel.grace = 0
        started = time.monotonic()
        channel.close()
        took = time.monotonic() - started
        still_writing = channel._write_lock.locked()
        assert not still_writing, 'the pipe was closed while a write was still in progress on it'
        assert took < 0.5, f'the close waited {took:.2f} s for the write'
        writer.join(5)
        assert failures == ['E_TRANSPORT'], failures
    finally:
        channel.grace = 0
        channel.close()


def test_a_close_during_the_wait_for_room_stops_the_write_before_its_next_byte(monkeypatch):
    """A close sets its flag while the write waits for room in the pipe, and the pipe then has room: the write stops
    without writing, rather than writing once more before it looks at the flag again."""
    import kelmo.client as client_module
    channel = MuxChannel(command(), timeout=2)
    descriptor = channel.process.stdin.fileno()
    real_write, writes = os.write, []

    class ClosingSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def register(self, *_):
            pass

        def select(self, _timeout):
            channel._closed = True  # a close began while the write waited
            return [('ready', 2)]

    def write(fd, data):
        if fd == descriptor:
            writes.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(client_module.selectors, 'DefaultSelector', ClosingSelector)
    monkeypatch.setattr(client_module.os, 'write', write)
    try:
        try:
            channel.request(request('close-in-the-wait'))
        except Exception:  # noqa: BLE001, S110 - only whether a byte went out matters here
            pass
        monkeypatch.undo()
        assert writes == [], f'the write went on after the close began: {writes}'
    finally:
        monkeypatch.undo()
        channel._closed = False
        channel.close()


def test_a_close_that_lands_between_the_look_and_the_write_lets_no_byte_out_after_it(monkeypatch):
    """A close begins after the write looked at the flag and before it wrote: no byte goes out once the close has
    set its flag; the write either goes before the flag is set or not at all."""
    import kelmo.client as client_module
    channel = MuxChannel(command(), timeout=2)
    channel.grace = 0
    descriptor = channel.process.stdin.fileno()
    real_write, after_close, closer = os.write, [], []

    def write(fd, data):
        if fd == descriptor and not closer:
            thread = threading.Thread(target=channel.close, daemon=True)
            closer.append(thread)
            thread.start()  # the close begins now, after the write's last look at the flag
            stop = time.monotonic() + 0.5
            while not channel._closed and time.monotonic() < stop:
                time.sleep(0.005)
        if fd == descriptor and channel._closed:
            after_close.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(client_module.os, 'write', write)
    try:
        try:
            channel.request(request('close-before-the-write'))
        except Exception:  # noqa: BLE001, S110 - only whether a byte went out after the flag matters here
            pass
        monkeypatch.undo()
        assert after_close == [], f'bytes went out after the close set its flag: {after_close}'
    finally:
        monkeypatch.undo()
        for thread in closer:
            thread.join(5)
        channel.close()
