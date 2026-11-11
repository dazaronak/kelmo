"""The channel of a core inside the Linux sandbox, where seccomp refuses every socket syscall.

`socket.socket(fileno=...)` fails there with EPERM (seen on an x86_64 host), so a confined core uses the
descriptor with read, write and poll only. Here socket construction is made to fail the same way.
"""
import socket
import threading

import pytest
from archive_core_support import Peer

from kelmo.core import corelink
from kelmo.core.corelink import CoreLink, DescriptorChannel


def refused(*_args, **_kwargs):
    raise PermissionError(1, 'Operation not permitted')


def echo(_context, body):
    return body


def serving_link(child_fd, monkeypatch):
    """Socket construction fails from here to the end of the test, as under the sandbox's seccomp policy; the
    test's own end of the pair exists already."""
    monkeypatch.setattr(corelink.socket, 'socket', refused)
    link = CoreLink.from_descriptor(child_fd, confined=True, handlers={'archive.echo': (echo, 'read')},
                                    lanes={'read': 2})
    thread = threading.Thread(target=link.serve, daemon=True)
    thread.start()
    return link, thread


def test_a_confined_core_answers_without_any_socket_call_and_stops_on_admin_stop(monkeypatch):
    parent, child = socket.socketpair()
    link, thread = serving_link(child.detach(), monkeypatch)
    assert isinstance(link.sock, DescriptorChannel)
    peer = Peer(parent)
    try:
        hello, _ = peer.frame()
        assert hello['kind'] == 'hello' and hello['operations'] == ['archive.echo']
        large = {'text': 'x' * (3 * 1024 * 1024)}  # larger than any pipe or socket buffer, so writes are partial
        header, body = peer.call('archive.echo', large)
        assert header['ok'] is True and body == large
        header, body = peer.call('admin.stop', {})
        assert header['ok'] is True and body == {'stopping': True}
        thread.join(10)
        assert not thread.is_alive()
    finally:
        parent.close()


def test_a_non_blocking_channel_still_carries_a_large_reply(monkeypatch):
    parent, child = socket.socketpair()
    child.setblocking(False)
    _link, thread = serving_link(child.detach(), monkeypatch)
    peer = Peer(parent)
    try:
        peer.frame()
        large = {'text': 'y' * (2 * 1024 * 1024)}
        header, body = peer.call('archive.echo', large)
        assert header['ok'] is True and body == large
    finally:
        parent.close()
        thread.join(10)


def test_a_confined_core_ends_when_kelmod_closes_the_channel(monkeypatch):
    parent, child = socket.socketpair()
    _link, thread = serving_link(child.detach(), monkeypatch)
    peer = Peer(parent)
    peer.frame()
    parent.close()
    thread.join(10)
    assert not thread.is_alive()


def test_descriptor_channel_closes_every_descriptor_once():
    parent, child = socket.socketpair()
    channel = DescriptorChannel(child.detach())
    fds = (channel.fd, channel._wake_read, channel._wake_write)
    channel.close()
    channel.close()
    for fd in fds:
        with pytest.raises(OSError):
            __import__('os').fstat(fd)
    parent.close()


def test_shutdown_from_another_thread_wakes_a_waiting_reader():
    parent, child = socket.socketpair()
    channel = DescriptorChannel(child.detach())
    got = []
    reader = threading.Thread(target=lambda: got.append(channel.recv(10)), daemon=True)
    reader.start()
    channel.shutdown(socket.SHUT_RD)
    reader.join(5)
    assert got == [b''] and not reader.is_alive()
    channel.close()
    parent.close()


def test_a_write_after_close_fails_instead_of_reaching_a_reused_descriptor():
    parent, child = socket.socketpair()
    channel = DescriptorChannel(child.detach())
    channel.close()
    with pytest.raises(OSError):
        channel.sendall(b'late')
    parent.close()


def test_an_unconfined_core_keeps_its_socket(monkeypatch):
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    try:
        assert isinstance(link.sock, socket.socket)
    finally:
        link.sock.close()
        parent.close()
