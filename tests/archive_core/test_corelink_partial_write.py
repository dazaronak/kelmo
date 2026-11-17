"""A core's write to kelmod that fails part-way through a frame ends the channel: a frame never follows a broken
one. A write that fails before any byte went out fails its call alone."""
import errno
import socket
import uuid

from kelmo.core import corelink, wire
from kelmo.core.context import Context
from kelmo.core.corelink import CoreLink


class HalfThenFull(socket.socket):
    """A real socket whose first send writes half the frame and whose next send fails with no buffer space."""
    sends = 0

    def send(self, data, *args):
        type(self).sends += 1
        if type(self).sends == 1:
            return super().send(bytes(data[:len(data) // 2]), *args)
        raise OSError(errno.ENOBUFS, 'No buffer space available')


class FullOnce(socket.socket):
    """A real socket whose first send fails with nothing written; later sends write normally."""
    failed = False

    def send(self, data, *args):
        if not type(self).failed:
            type(self).failed = True
            raise OSError(errno.ENOBUFS, 'No buffer space available')
        return super().send(data, *args)


def _link(cls):
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    raw = link.sock
    link.sock = cls(raw.family, raw.type, fileno=raw.detach())
    return parent, link


def _drain(parent, seconds=1.0):
    parent.settimeout(seconds)
    data = b''
    try:
        while chunk := parent.recv(65536):
            data += chunk
    except TimeoutError:
        pass
    return data


def test_a_frame_cut_part_way_ends_the_channel_and_no_frame_follows_it():
    HalfThenFull.sends = 0
    parent, link = _link(HalfThenFull)
    order = []
    link.begin_stop = lambda: order.append('stop')
    try:
        first = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
        order.append('failed' if first.done() and first.exception() is not None else 'waiting')
        second = link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
        assert second.done() and second.exception().code == 'E_TRANSPORT', 'a call after the broken frame was written and waits for a reply'
        assert order[:2] == ['stop', 'failed'], order
        assert HalfThenFull.sends == 2, f'a call after the broken frame wrote again ({HalfThenFull.sends} sends)'
        received = _drain(parent)
        whole = len(wire.encode({'v': wire.VERSION, 'kind': 'call', 'id': uuid.uuid4().hex, 'op': 'archive.echo',
                                 'body_type': 'json', 'deadline_ms': 1}, {'n': 1}))
        assert len(received) < whole, 'kelmod received more than the broken frame'
    finally:
        parent.close()
        link.sock.close()


def test_a_write_that_fails_before_any_byte_fails_its_call_alone():
    FullOnce.failed = False
    parent, link = _link(FullOnce)
    stops = []
    link.begin_stop = lambda: stops.append('stop')
    try:
        first = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
        assert first.done() and first.exception().code == 'E_TRANSPORT', 'the unsent call was left waiting'
        link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
        assert stops == [] and not link._stopping.is_set(), stops
        header, body = wire.decode(_drain(parent))
        assert (header['op'], body) == ('archive.echo', {'n': 2}), (header, body)
    finally:
        parent.close()
        link.sock.close()


def test_a_confined_channel_frame_cut_part_way_ends_the_channel(monkeypatch):
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), confined=True, handlers={}, lanes={'read': 1})
    channel = link.sock
    order, writes = [], []
    link.begin_stop = lambda: order.append('stop')
    real_write = corelink.os.write

    def write(fd, data):
        if fd != channel.fd:
            return real_write(fd, data)
        writes.append(len(data))
        if len(writes) == 1:
            return real_write(fd, bytes(data[:len(data) // 2]))
        raise OSError(errno.ENOBUFS, 'No buffer space available')

    monkeypatch.setattr(corelink.os, 'write', write)
    try:
        first = link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
        order.append('failed' if first.done() and first.exception() is not None else 'waiting')
        link.call('archive.echo', {'n': 2}, Context.local(uuid.uuid4().hex, 30))
        assert order[:2] == ['stop', 'failed'], order
        assert len(writes) == 2, f'a call after the broken frame wrote again ({writes})'
    finally:
        monkeypatch.undo()
        parent.close()
        channel.close()
