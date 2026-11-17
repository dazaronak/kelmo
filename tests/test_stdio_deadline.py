"""A one-request (stdio) channel whose deadline passes before any byte of a request is written refuses it as
Late: nothing reached the server, so a queued write counts no attempt and keeps its earlier refusal."""

import os
import sys
import time

import pytest

from kelmo import client as client_module
from kelmo.client import Late, StdioChannel


def _silent():
    """A server that never reads its standard input."""
    return StdioChannel([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.4)


def _fill(channel):
    descriptor = channel.process.stdin.fileno()
    while True:
        try:
            os.write(descriptor, b'x' * 65536)
        except BlockingIOError:
            return


def test_a_request_whose_pipe_stays_full_past_its_deadline_is_late():
    channel = _silent()
    try:
        _fill(channel)
        with pytest.raises(Exception) as raised:
            channel.request({'protocol': client_module.PROTOCOL, 'request_id': 'r1', 'op': 'status', 'args': {}})
        assert isinstance(raised.value, Late), repr(raised.value)
    finally:
        channel.grace = 0
        channel.close()


def test_a_request_whose_encoding_outlasts_its_deadline_sends_nothing(monkeypatch):
    channel = _silent()
    real = client_module.encode_frame

    def slow(frame):
        time.sleep(0.6)  # past the request's 0.4 s
        return real(frame)

    monkeypatch.setattr(client_module, 'encode_frame', slow)
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as raised:
            channel.request({'protocol': client_module.PROTOCOL, 'request_id': 'r2', 'op': 'status', 'args': {}})
        took = time.monotonic() - started
        assert isinstance(raised.value, Late), f'{raised.value!r} after {took:.2f} s'
    finally:
        channel.grace = 0
        channel.close()
