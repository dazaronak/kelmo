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
