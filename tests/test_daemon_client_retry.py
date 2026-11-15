"""`daemon_client.call` when kelmod's channel closes before the answer.

A call whose channel to the daemon closes before its reply is sent again once when that is safe: a read,
or a write under an idempotency key (sent again under the same key). A keyless write is not sent again:
its effect is unknown, and the caller is told so. Whatever happens, the caller hears back inside its
deadline. The daemon is `scripted_daemon.ScriptedDaemon`, which records every call frame it reads.
"""
import sys
import time
from pathlib import Path

import pytest

DAEMON_TESTS = Path(__file__).resolve().parent / 'daemon'
if str(DAEMON_TESTS) not in sys.path:
    sys.path.insert(0, str(DAEMON_TESTS))

from scripted_daemon import ANSWER, CLOSE, RESET, SILENT, Connection, ScriptedDaemon, hold  # noqa: E402

from kelmo.core import daemon_client  # noqa: E402
from kelmo.errors import KelmoError  # noqa: E402


@pytest.fixture
def daemon():
    made = []

    def make(*script):
        made.append(ScriptedDaemon(*script))
        return made[-1]
    yield make
    for each in made:
        each.close()


def outcome(daemon, op, body=None, *, seconds=5.0, key=None):
    """('ok', result, elapsed) or ('error', code, retryable, message, elapsed) of one call."""
    started = time.monotonic()
    try:
        result = daemon_client.call(daemon.directory, op, body, seconds=seconds, key=key, path=daemon.path)
    except KelmoError as error:
        return ('error', error.code, getattr(error, 'retryable', None), str(error), time.monotonic() - started)
    except Exception as error:  # noqa: BLE001 - an untyped failure is itself the wrong outcome
        return ('untyped', type(error).__name__, None, str(error), time.monotonic() - started)
    return ('ok', result, time.monotonic() - started)


def test_a_read_cut_before_its_answer_is_sent_again_and_answers(daemon):
    peer = daemon(Connection(hold(0.5, CLOSE)), Connection(ANSWER))
    got = outcome(peer, 'state.scheduler_status', {'probe': 1}, seconds=5.0)
    assert got[:2] == ('ok', {'answer': 2}), got
    assert got[-1] < 5.0
    assert len(peer.calls) == 2
    first, second = peer.calls
    assert first[0]['op'] == second[0]['op'] == 'state.scheduler_status'
    assert first[1] == second[1] == {'probe': 1}
    assert second[0]['deadline_ms'] <= first[0]['deadline_ms'] - 400


def test_a_reset_counts_as_a_cut(daemon):
    peer = daemon(Connection(RESET), Connection(ANSWER))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=5.0)
    assert got[:2] == ('ok', {'answer': 2}), got
    assert len(peer.calls) == 2


def test_a_daemon_that_rebinds_late_is_reached_by_the_resend(daemon):
    peer = daemon(Connection(CLOSE, gone=0.3), Connection(ANSWER))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=5.0)
    assert got[:2] == ('ok', {'answer': 2}), got
    assert got[-1] < 5.0
    assert len(peer.calls) == 2


def test_daemon_status_counts_as_a_read(daemon):
    peer = daemon(Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'daemon.status', {}, seconds=5.0)
    assert got[:2] == ('ok', {'answer': 2}), got
    assert [frame[0]['op'] for frame in peer.calls] == ['daemon.status', 'daemon.status']


def test_a_keyed_write_is_sent_again_under_the_same_key(daemon):
    peer = daemon(Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.client_request', {'request': {'op': 'create'}}, key='k-1')
    assert got[:2] == ('ok', {'answer': 2}), got
    assert [frame[0].get('key') for frame in peer.calls] == ['k-1', 'k-1']
    assert peer.calls[0][1] == peer.calls[1][1]


def test_a_keyless_write_is_not_sent_again(daemon):
    peer = daemon(Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.cli', {'argv': ['status']})
    assert got[0] == 'error', got
    assert len(peer.calls) == 1
    assert peer.connections == 1
    assert got[1] == 'E_TRANSPORT' and got[2] is False, got
    assert 'unknown' in got[3], got


def test_a_keyed_class_write_without_a_key_is_not_sent_again(daemon):
    peer = daemon(Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.client_request', {'request': {'op': 'create'}})
    assert got[0] == 'error', got
    assert len(peer.calls) == 1
    assert got[1] == 'E_TRANSPORT' and got[2] is False, got


def test_a_second_cut_gives_a_typed_error_inside_the_deadline(daemon):
    peer = daemon(Connection(CLOSE), Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=5.0)
    assert got[0] == 'error', got
    assert got[1] == 'E_TRANSPORT', got
    assert got[-1] < 5.0
    assert len(peer.calls) == 2


def test_a_silent_second_attempt_ends_at_the_deadline(daemon):
    peer = daemon(Connection(CLOSE), Connection(SILENT))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=1.5)
    assert got[0] == 'error', got
    assert got[1] == 'E_DEADLINE', got
    assert got[-1] < 2.0
    assert len(peer.calls) == 2


def test_a_daemon_that_never_comes_back_fails_inside_the_reconnect_window(daemon, monkeypatch):
    monkeypatch.setattr(daemon_client, 'RECONNECT_SECONDS', 0.5)
    peer = daemon(Connection(CLOSE, gone=30))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=20.0)
    assert got[0] == 'error', got
    assert got[1] == 'E_TRANSPORT', got
    assert got[-1] < 2.0
    assert len(peer.calls) == 1


def test_a_cut_before_the_call_is_sent_connects_again_for_any_op(daemon):
    peer = daemon(Connection(hello=False), Connection(ANSWER))
    got = outcome(peer, 'state.cli', {'argv': ['status']})
    assert got[:2] == ('ok', {'answer': 1}), got
    assert len(peer.calls) == 1
    assert peer.connections == 2


def test_no_time_left_gives_a_deadline_error(daemon):
    peer = daemon(Connection(hold(1.5, CLOSE)), Connection(ANSWER))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=1.0)
    assert got[0] == 'error', got
    assert got[1] == 'E_DEADLINE', got
    assert len(peer.calls) == 1


def test_a_hello_that_never_comes_is_a_transport_error_with_nothing_sent(daemon):
    # No connection is ever accepted: the client's connect lands in the listen backlog and its hello waits
    # for an answer until the first attempt's socket timeout (the deadline plus its grace).
    peer = daemon()
    got = outcome(peer, 'state.job_run', {'job': 'dream'}, seconds=0.2)
    assert got[0] == 'error', got
    assert got[1] == 'E_TRANSPORT' and got[2] is True, got
    assert 'nothing was sent' in got[3], got
    assert peer.calls == []
