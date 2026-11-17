"""A core's channel when it is told to stop or asked to recycle.

admin.stop is a drain: the core's own calls still get their replies until its work has ended, and only then does
the channel close. admin.recycle is the core's consent: busy work declines. admin.status never holds the reader,
so heartbeats are answered while a status probe waits.
"""
import socket
import threading
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

    def second():
        link.call('archive.echo', {}, Context.local(uuid.uuid4().hex, 30))
        order.append('second failed')

    other = threading.Thread(target=second, daemon=True)
    other.start()
    other.join(0.5)
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
