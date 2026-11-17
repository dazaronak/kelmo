"""A core's channel when it is told to stop or asked to recycle.

admin.stop is a drain: the core's own calls still get their replies until its work has ended, and only then does
the channel close. admin.recycle is the core's consent: busy work declines. admin.status never holds the reader,
so heartbeats are answered while a status probe waits.
"""
import socket
import threading
import time
import uuid

from archive_core_support import Peer

from kelmo.core import wire
from kelmo.core.context import Context
from kelmo.core.corelink import CoreLink


def serve(link):
    thread = threading.Thread(target=link.serve, daemon=True)
    thread.start()
    return thread


def answer(peer, header, body):
    peer.sock.sendall(wire.encode(*wire.reply(header['id'], body)))


def test_a_stopping_core_still_gets_the_reply_to_its_own_call_and_closes_after_its_work():
    parent, child = socket.socketpair()
    told, replied = threading.Event(), {}
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    def work():
        # Background work of the owner: it ends once told to stop, after one more call to another core.
        told.wait(30)
        future = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
        replied['body'] = future.result(30)

    worker = threading.Thread(target=work, daemon=True)
    link.begin_stop = told.set
    link.wait_stopped = lambda: worker.join(30)
    worker.start()
    serving = serve(link)
    peer = Peer(parent)
    try:
        peer.frame()  # hello
        header, body = peer.call('admin.stop', {})
        assert body == {'stopping': True}
        call, _ = peer.frame()
        assert call['op'] == 'archive.echo'
        assert serving.is_alive(), 'the channel stays open while the core drains'
        answer(peer, call, {'echo': 1})
        serving.join(30)
        assert not serving.is_alive()
        assert replied['body'] == {'echo': 1}
    finally:
        parent.close()


def test_a_call_that_arrives_while_the_core_drains_is_refused_as_retryable():
    parent, child = socket.socketpair()
    release = threading.Event()
    link = CoreLink.from_descriptor(child.detach(), handlers={'state.echo': (lambda context, body: body, 'read')},
                                    lanes={'read': 1})
    link.wait_stopped = lambda: release.wait(30)
    serving = serve(link)
    peer = Peer(parent)
    try:
        peer.frame()
        peer.call('admin.stop', {})
        header, _ = peer.call('state.echo', {'x': 1})
        assert header['ok'] is False
        assert header['error']['code'] == 'E_UNAVAILABLE' and header['error']['retryable'] is True
    finally:
        release.set()
        parent.close()
        serving.join(30)


def test_when_kelmod_closes_the_channel_the_core_hears_of_the_stop_before_its_calls_fail():
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    future.add_done_callback(lambda _: order.append('failed'))
    serving = serve(link)
    Peer(parent).frame()
    parent.close()
    serving.join(30)
    assert order == ['stop', 'failed']


def test_a_call_that_cannot_be_sent_on_a_closed_channel_tells_the_core_of_the_stop_first():
    """kelmod closed the channel and the reader has not reached its end yet: a call sent now fails on the write.
    The core hears of the stop before that call fails, so the failure is counted as the stop's."""
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    parent.close()
    future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    future.add_done_callback(lambda _: order.append('failed'))
    assert order == ['stop', 'failed'], order
    assert future.exception().code == 'E_TRANSPORT'


def test_the_core_hears_of_the_stop_before_the_unsent_call_is_failed(monkeypatch):
    """The order inside `call`: the stop is asked for before the call's future is failed, not only before the
    caller looks at it."""
    import kelmo.core.corelink as corelink
    parent, child = socket.socketpair()
    order = []

    class Recorded(corelink.Future):
        def set_exception(self, error):
            order.append('failed')
            super().set_exception(error)

    monkeypatch.setattr(corelink, 'Future', Recorded)
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    parent.close()
    link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    assert order == ['stop', 'failed'], order


def test_a_second_unsent_call_does_not_fail_before_the_core_has_heard_of_the_stop():
    """Two calls meet the closed channel at once: the second must not fail while the first is still telling the
    core of the stop, or the second's failure is counted as an ordinary one."""
    parent, child = socket.socketpair()
    order, entered, release = [], threading.Event(), threading.Event()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    def begin_stop():
        entered.set()
        release.wait(10)
        order.append('stop')

    link.begin_stop = begin_stop
    parent.close()
    first = threading.Thread(target=link.call, args=('archive.echo', {}, Context.local(uuid.uuid4().hex, 30)),
                             daemon=True)
    first.start()
    assert entered.wait(5)

    started = threading.Event()

    def second():
        started.set()
        link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        order.append('second failed')

    other = threading.Thread(target=second, daemon=True)
    other.start()
    assert started.wait(5)
    other.join(0.5)
    assert other.is_alive(), 'the second call returned while the first was still telling the core of the stop'
    release.set()
    other.join(10)
    first.join(10)
    assert order == ['stop', 'second failed'], order


def test_an_os_error_while_encoding_a_call_does_not_stop_the_core():
    """Only a write that fails on the channel means kelmod is gone; an OSError raised while the body is encoded,
    before anything is written, fails the call alone."""
    parent, child = socket.socketpair()
    order = []

    class Odd(dict):
        def values(self):
            raise OSError('not a channel error')

    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    try:
        future = link.call('archive.echo', Odd(x=1), Context.local(uuid.uuid4().hex, 30))
        assert future.exception() is not None
        assert order == [], order
        assert future.exception().code == 'E_TRANSPORT', 'an encoder error that is not a KelmoError fails as before'
    finally:
        parent.close()


def test_an_unsent_call_fails_though_the_stop_request_and_its_report_both_fail(monkeypatch):
    """`begin_stop` raises and stderr is closed: the call still returns a failed future instead of raising."""
    import io
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    def begin_stop():
        raise RuntimeError('synthetic')

    link.begin_stop = begin_stop
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr('sys.stderr', closed)
    parent.close()
    future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    assert future.exception().code == 'E_TRANSPORT'


def test_a_call_that_cannot_be_encoded_does_not_stop_the_core():
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    try:
        future = link.call('archive.echo', {'x': object()}, Context.local(uuid.uuid4().hex, 30))
        assert future.exception() is not None
        assert future.exception().code == 'E_PROTOCOL', future.exception()
        assert order == [], order
    finally:
        parent.close()


def test_a_channel_closed_before_the_hello_still_stops_the_core_and_fails_its_calls():
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    future.add_done_callback(lambda _: order.append('failed'))
    parent.close()
    serving = serve(link)
    serving.join(30)
    assert not serving.is_alive()
    assert order == ['stop', 'failed']


def test_recycle_is_declined_while_the_owner_is_busy_and_agreed_when_it_is_not():
    parent, child = socket.socketpair()
    busy = ['dream']
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1}, recycle=lambda: list(busy))
    serving = serve(link)
    peer = Peer(parent)
    try:
        peer.frame()
        _, body = peer.call('admin.recycle', {})
        assert body == {'recycle': False, 'busy': ['dream']}
        busy.clear()
        _, body = peer.call('admin.recycle', {})
        assert body == {'recycle': True}
    finally:
        parent.close()
        serving.join(30)


def test_a_failing_recycle_probe_is_a_decline():
    parent, child = socket.socketpair()

    def broken():
        raise RuntimeError('probe')

    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1}, recycle=broken)
    serving = serve(link)
    peer = Peer(parent)
    try:
        peer.frame()
        _, body = peer.call('admin.recycle', {})
        assert body['recycle'] is False and body['busy'] == ['probe failed: RuntimeError']
    finally:
        parent.close()
        serving.join(30)


def test_heartbeats_are_answered_while_a_status_probe_waits():
    parent, child = socket.socketpair()
    release = threading.Event()

    def slow_status():
        release.wait(30)
        return {'slow': True}

    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1}, status=slow_status)
    serving = serve(link)
    peer = Peer(parent)
    try:
        peer.frame()
        status_id = peer.send('admin.status', {})
        header, body = peer.call('admin.ping', {}, timeout=5)
        assert header['ok'] is True and 'pid' in body
        release.set()
        _, status = peer.reply(status_id)
        assert status['slow'] is True
    finally:
        release.set()
        parent.close()
        serving.join(30)


def test_a_local_write_error_with_kelmod_still_there_does_not_stop_the_core():
    """Only an error that says the peer is gone stops the core; a local one (no buffer space) fails the call
    alone, as before."""
    import errno
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')

    class Full:
        def __init__(self, sock):
            self.sock = sock

        def sendall(self, _data):
            raise OSError(errno.ENOBUFS, 'No buffer space available')

        def __getattr__(self, name):
            return getattr(self.sock, name)

    link.sock = Full(link.sock)
    try:
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        assert future.exception().code == 'E_TRANSPORT'
        assert order == [] and not link._stopping.is_set(), order
    finally:
        parent.close()


def test_an_unsent_call_already_failed_by_the_stop_returns_its_failed_future(monkeypatch):
    """The reader's end of the channel fails every open call; a call whose write fails just after that returns its
    failed future instead of raising InvalidStateError."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    parent.close()

    class FailedFirst:
        def __init__(self, sock):
            self.sock = sock

        def sendall(self, data):
            with link._state:
                pending = list(link._outgoing.values())
            for future in pending:  # as `_finish` does when the reader reaches the end first
                future.set_exception(RuntimeError('failed by the stop'))
            return self.sock.sendall(data)

        def __getattr__(self, name):
            return getattr(self.sock, name)

    link.sock = FailedFirst(link.sock)
    future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    assert future.exception() is not None


def test_the_stop_does_not_break_on_a_call_failed_while_it_fails_the_open_calls():
    """A call thread fails its own future between the stop's look at it and the stop's own failure of it: the stop
    goes on (closing the channel, stopping the pools) instead of raising out of the reader."""
    from concurrent.futures import InvalidStateError
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    class FailedMeanwhile:
        def done(self):
            return False

        def set_exception(self, error):
            raise InvalidStateError('failed by the call thread meanwhile')

    link._outgoing['racing'] = FailedMeanwhile()
    parent.close()
    try:
        link._finish()
    except InvalidStateError as error:
        raise AssertionError(f'the stop broke on a call failed meanwhile: {error}') from error


def test_any_error_while_encoding_a_call_fails_that_call_alone(monkeypatch):
    import kelmo.core.corelink as corelink
    parent, child = socket.socketpair()
    stops = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: stops.append('stop')

    def deep(header, body):
        raise RecursionError('maximum recursion depth exceeded while encoding')

    monkeypatch.setattr(corelink.wire, 'encode', deep)
    try:
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    except RecursionError as error:
        raise AssertionError('an encoder error escaped the call') from error
    finally:
        parent.close()
    assert future.exception().code == 'E_TRANSPORT'
    assert link._outgoing == {} and stops == []


def test_a_write_error_that_is_not_an_os_error_fails_that_call_alone():
    parent, child = socket.socketpair()
    stops = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: stops.append('stop')

    class Refusing:
        def sendall(self, data):
            raise ValueError('the channel refused the write')

        def close(self):
            pass

    real, link.sock = link.sock, Refusing()
    try:
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    except ValueError as error:
        raise AssertionError('a write error escaped the call') from error
    finally:
        real.close()
        parent.close()
    assert future.exception().code == 'E_TRANSPORT'
    assert link._outgoing == {} and stops == []


def test_a_stop_hook_that_raises_out_of_the_call_leaves_no_call_waiting():
    """A BaseException from begin_stop leaves the call without returning its future; nothing may stay registered
    for a reply that cannot come."""
    import pytest
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    def begin_stop():
        raise SystemExit(3)

    link.begin_stop = begin_stop
    parent.close()
    with pytest.raises(SystemExit):
        link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    assert link._outgoing == {}


def test_a_reply_kelmod_sent_before_closing_still_reaches_its_call_after_a_write_found_it_gone():
    """kelmod answered call A and closed the channel; call B's write then fails on the closed channel. The reader
    still reads A's reply, which is already in the channel, before it reaches the end."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.hold_hello = True  # no hello: the test plays kelmod's side by hand
    peer = Peer(parent)
    serving = None
    try:
        first = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
        header, _ = peer.frame()
        answer(peer, header, {'echo': 1})
        parent.shutdown(socket.SHUT_RDWR)
        parent.close()
        second = link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
        assert second.exception().code == 'E_TRANSPORT'
        serving = serve(link)
        assert first.result(10) == {'echo': 1}, 'a reply already in the channel was lost'
    finally:
        if serving is not None:
            serving.join(10)


def test_a_call_made_once_the_link_is_stopping_fails_at_once_without_being_sent():
    """After the reader reached the channel's end (`_finish` failed the open calls), a new call is never registered
    where nothing will fail it: it fails at once, after the owner has heard of the stop, and writes nothing."""
    parent, child = socket.socketpair()
    order = []
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    link._stopping.set()  # as `_finish` leaves it, its snapshot of the open calls taken
    try:
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        future.add_done_callback(lambda _: order.append('failed'))
        assert future.done(), 'a call made while the link stops waited for a reply that cannot come'
        assert future.exception().code == 'E_TRANSPORT'
        assert order == ['stop', 'failed'], order
        assert link._outgoing == {}
        parent.setblocking(False)
        try:
            leaked = parent.recv(65536)
        except BlockingIOError:
            leaked = b''
        assert leaked == b'', 'the call was written to a stopping channel'
    finally:
        parent.close()


def test_a_call_whose_body_cannot_be_encoded_fails_as_a_stopped_call_once_the_link_is_stopping():
    """Criterion 1 holds whatever the body: once the link stops, a call fails with E_TRANSPORT at once, before its
    body is even encoded."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: None
    link._stopping.set()
    try:
        future = link.call('archive.echo', {'x': object()}, Context.local(uuid.uuid4().hex, 30))
        assert future.done()
        assert future.exception().code == 'E_TRANSPORT', future.exception().code
    finally:
        parent.close()


def test_a_call_made_once_the_link_is_stopping_does_not_wait_for_an_earlier_write():
    """An earlier call still holds the write lock (kelmod stopped reading) when the link stops: a new call fails at
    once instead of queueing behind that write."""
    import threading
    import time
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: None
    held, release = threading.Event(), threading.Event()

    def writer():
        with link._write_lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert held.wait(5)
    link._stopping.set()
    timer = threading.Timer(3.0, release.set)
    timer.start()
    try:
        started = time.monotonic()
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        took = time.monotonic() - started
        assert took < 1.0, f'the call waited {took:.2f} s behind an earlier write on a stopping link'
        assert future.exception().code == 'E_TRANSPORT'
    finally:
        release.set()
        timer.cancel()
        thread.join(10)
        parent.close()


def test_a_call_failed_by_the_stop_before_its_write_is_not_written():
    """The call was registered, then the stop failed it (the reader reached the channel's end) before the call took
    the write lock: the frame is never written, since its reply would go nowhere."""
    import kelmo.core.corelink as corelink
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    real = link._write_lock

    class StopFirst:
        def __enter__(self):
            link._stopping.set()  # what `_finish` does, then it fails every open call
            with link._state:
                pending, link._outgoing = list(link._outgoing.values()), {}
            for future in pending:
                corelink._fail(future, corelink.KelmoError('E_TRANSPORT', 'The channel to kelmod closed.'))
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

    link._write_lock = StopFirst()
    try:
        future = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        assert future.exception().code == 'E_TRANSPORT'
        parent.setblocking(False)
        try:
            leaked = parent.recv(65536)
        except BlockingIOError:
            leaked = b''
        assert leaked == b'', 'a call the stop had already failed was written'
    finally:
        parent.close()


def test_a_call_after_one_that_found_kelmod_gone_does_not_try_to_write():
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    real, writes = link.sock, []

    class Counted:
        def sendall(self, data):
            writes.append(len(data))
            return real.sendall(data)

        def close(self):
            real.close()

    link.sock = Counted()
    parent.close()
    first = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    second = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    try:
        assert first.exception().code == 'E_TRANSPORT' and second.exception().code == 'E_TRANSPORT'
        assert len(writes) == 1, f'{len(writes)} writes: the call after kelmod was found gone still wrote'
    finally:
        real.close()


def test_a_call_refused_while_stopping_tells_the_core_before_its_future_fails(monkeypatch):
    import kelmo.core.corelink as corelink
    parent, child = socket.socketpair()
    order = []

    class Recorded(corelink.Future):
        def set_exception(self, error):
            order.append('failed')
            super().set_exception(error)

    monkeypatch.setattr(corelink, 'Future', Recorded)
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.begin_stop = lambda: order.append('stop')
    link._stopping.set()
    try:
        link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        assert order == ['stop', 'failed'], order
    finally:
        parent.close()


def test_waking_a_closed_confined_channel_writes_into_no_reused_descriptor(tmp_path):
    import os
    from kelmo.core.corelink import DescriptorChannel
    parent, child = socket.socketpair()
    channel = DescriptorChannel(child.detach())
    wake = channel._wake_write
    channel.close()
    opened = []
    try:
        for index in range(16):  # take the freed numbers back, the wake pipe's write end among them
            opened.append(os.open(tmp_path / f'file-{index}', os.O_WRONLY | os.O_CREAT, 0o600))
            if opened[-1] == wake:
                break
        assert wake in opened, 'the descriptor number was not reused in this process'
        channel.shutdown(socket.SHUT_RD)
        sizes = {index: os.path.getsize(tmp_path / f'file-{index}') for index in range(len(opened))}
        assert not any(sizes.values()), f'the wake byte went into a reused descriptor: {sizes}'
    finally:
        for descriptor in opened:
            os.close(descriptor)
        parent.close()


def test_the_stop_flag_waits_for_a_write_already_past_its_check():
    """A call passed the stop check and is writing when the reader reaches the channel's end: `_finish` sets the stop
    only once that write is done, so no call checks the flag, finds it clear and writes after the stop."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    entered, go = threading.Event(), threading.Event()

    class SlowSocket:
        """The link's socket, whose write pauses until the test lets it go."""

        def __init__(self, sock):
            self._sock = sock

        def __getattr__(self, name):
            return getattr(self._sock, name)

        def sendall(self, data):
            entered.set()
            go.wait(10)
            return self._sock.sendall(data)

    link.sock = SlowSocket(link.sock)
    calling = threading.Thread(target=lambda: link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30)),
                               daemon=True)
    finishing = threading.Thread(target=link._finish, daemon=True)
    try:
        calling.start()
        assert entered.wait(5), 'the call never reached its write'
        finishing.start()
        time.sleep(0.2)
        assert not link._stopping.is_set(), 'the stop was set while a call that passed its check was still writing'
    finally:
        go.set()
        calling.join(10)
        finishing.join(10)
        parent.close()
    assert link._stopping.is_set()


def test_a_call_after_a_reply_that_found_kelmod_gone_does_not_try_to_write():
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    real, writes = link.sock, []

    class Counted:
        def sendall(self, data):
            writes.append(len(data))
            return real.sendall(data)

        def close(self):
            real.close()

    link.sock = Counted()
    parent.close()
    link._reply(uuid.uuid4().hex, {})  # a handler's reply finds kelmod gone
    call = link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
    try:
        assert call.exception().code == 'E_TRANSPORT'
        assert len(writes) == 1, f'{len(writes)} writes: the call after a reply found kelmod gone still wrote'
    finally:
        real.close()
