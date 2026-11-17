"""A core's own call to another core once its channel has finished: it fails with E_TRANSPORT at once, never at
its deadline, whether it was made before the outgoing calls were failed, after them, or after the socket closed."""
import socket
import threading
import time
import uuid
from concurrent.futures import wait

import pytest

from archive_core_support import Peer

from kelmo.core.context import Context
from kelmo.core.corelink import CoreLink


def finishing_link():
    """A link whose `_finish` is held after it failed the open calls and before it closed the socket."""
    parent, child = socket.socketpair()
    finishing, release = threading.Event(), threading.Event()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})

    def wait_stopped():
        finishing.set()
        release.wait(30)

    link.wait_stopped = wait_stopped
    serving = threading.Thread(target=link.serve, daemon=True)
    serving.start()
    peer = Peer(parent)
    peer.frame()  # hello
    return parent, link, serving, finishing, release


def call(link):
    """`link.call`, which hands back a future and never raises: a raise fails the test."""
    try:
        return link.call('archive.echo', {'n': 1}, Context.local(uuid.uuid4().hex, 30))
    except Exception as error:  # noqa: BLE001 - the contract is a future, failed or not
        pytest.fail(f'call() raised {error!r} instead of returning a future')


def failure(future, seconds=2):
    wait([future], timeout=seconds)
    assert future.done(), 'the call was left to its 30 s deadline'
    error = future.exception()
    return error.code


def test_a_call_made_after_the_channel_finished_fails_at_once():
    parent, link, serving, finishing, release = finishing_link()
    try:
        parent.shutdown(socket.SHUT_WR)  # kelmod's end stops: the reader sees the end and the channel finishes
        assert finishing.wait(30)
        start = time.monotonic()
        assert failure(call(link)) == 'E_TRANSPORT'
        assert time.monotonic() - start < 2
    finally:
        release.set()
        parent.close()
        serving.join(30)


def test_a_call_made_before_the_channel_finished_fails_with_e_transport():
    parent, link, serving, finishing, release = finishing_link()
    try:
        future = call(link)
        parent.shutdown(socket.SHUT_WR)
        assert finishing.wait(30)
        assert failure(future) == 'E_TRANSPORT'
    finally:
        release.set()
        parent.close()
        serving.join(30)


def test_a_call_made_after_the_socket_closed_fails_with_e_transport():
    parent, link, serving, finishing, release = finishing_link()
    try:
        parent.shutdown(socket.SHUT_WR)
        assert finishing.wait(30)
        release.set()
        serving.join(30)
        assert not serving.is_alive()
        assert failure(call(link)) == 'E_TRANSPORT'
    finally:
        release.set()
        parent.close()


def test_a_call_whose_send_fails_after_the_swap_returns_its_failed_future(monkeypatch):
    import kelmo.core.corelink as corelink
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    real = corelink.wire.encode

    def encode(header, body):
        # The channel finishes between the registration and the send.
        link._finish()
        return real(header, body)

    monkeypatch.setattr(corelink.wire, 'encode', encode)
    try:
        try:
            future = call(link)
        except Exception as error:  # noqa: BLE001 - the test's point: call() returns, it never raises
            pytest.fail(f'call() raised {error!r} instead of returning its failed future')
        assert failure(future, 0) == 'E_TRANSPORT'
    finally:
        parent.close()


def test_a_send_failure_racing_the_finish_returns_one_failed_future():
    """The channel finishes on another thread while the send path handles its own failure: whichever of them
    fails the call first, the other's try changes nothing and raises nothing."""
    parent, child = socket.socketpair()
    link = CoreLink.from_descriptor(child.detach(), handlers={}, lanes={'read': 1})
    errors = []

    def finish():
        try:
            link._finish()
        except Exception as error:  # noqa: BLE001 - recorded for the assertion below
            errors.append(error)

    finisher = threading.Thread(target=finish, daemon=True)

    def write(data):
        finisher.start()  # it takes the write lock once this failed write lets it go
        raise OSError('the channel is closed')

    link._write_locked = write
    try:
        future = call(link)
        assert failure(future, 5) == 'E_TRANSPORT'
        finisher.join(10)
        assert errors == [], 'the finish failed on a call the send path had already failed'
    finally:
        parent.close()
