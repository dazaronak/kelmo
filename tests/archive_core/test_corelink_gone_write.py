"""A write that finds kelmod gone leaves the stop to the reader: the owner hears of it only once the reader has
delivered the replies kelmod sent before closing (or after a short bound), never before them."""
import socket
import threading
import time
import uuid

from archive_core_support import Peer

from kelmo.core import corelink, wire
from kelmo.core.context import Context
from kelmo.core.corelink import CoreLink


def _answer(peer, header, body):
    peer.sock.sendall(wire.encode(*wire.reply(header['id'], body)))


def _closed_after_a_reply():
    """Call A was answered, then kelmod closed the channel; the reader has not read the reply yet."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    link.hold_hello = True  # no hello: the test plays kelmod's side by hand
    peer = Peer(parent)
    first = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
    header, _ = peer.frame()
    _answer(peer, header, {'echo': 1})
    parent.shutdown(socket.SHUT_RDWR)
    parent.close()
    return link, first


def test_the_owner_hears_of_a_gone_kelmod_only_after_the_replies_already_sent_reach_their_calls():
    link, first = _closed_after_a_reply()
    heard = []
    link.begin_stop = lambda: heard.append(first.done())
    reader = threading.Thread(target=link.serve, daemon=True)
    threading.Timer(0.3, reader.start).start()  # the reader reaches the channel only after B's write failed
    try:
        second = link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
        assert second.exception(10).code == 'E_TRANSPORT'
        assert first.result(10) == {'echo': 1}
        assert heard == [True], f'the owner heard of the stop before the reply to call A arrived: {heard}'
    finally:
        reader.join(10)


def test_with_no_reader_the_owner_still_hears_of_a_gone_kelmod_within_a_short_bound():
    link, _first = _closed_after_a_reply()
    heard = []
    link.begin_stop = lambda: heard.append(True)
    started = time.monotonic()
    second = link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
    took = time.monotonic() - started
    assert second.exception(0).code == 'E_TRANSPORT'
    assert heard == [True], 'the owner never heard of the stop'
    assert took < corelink.STOP_WRITE_WAIT_SECONDS, f'the call waited {took:.2f} s for a reader that never came'
