"""Black-box coverage for concurrent kelmo/2 requests over one client process."""
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
