"""`daemon_client.call` when kelmod's channel closes before the answer.

A call whose channel to the daemon closes before its reply is sent again once when that is safe: a read,
or a write under an idempotency key (sent again under the same key). A keyless write is not sent again:
its effect is unknown, and the caller is told so. Whatever happens, the caller hears back inside its
deadline. The daemon is `scripted_daemon.ScriptedDaemon`, which records every call frame it reads.
"""
import socket
import sys
import time
import types
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


def outcome(daemon, op, body=None, *, seconds=5.0, key=None, resend=False):
    """('ok', result, elapsed) or ('error', code, retryable, message, elapsed) of one call."""
    started = time.monotonic()
    try:
        result = daemon_client.call(daemon.directory, op, body, seconds=seconds, key=key, path=daemon.path,
                                    resend=resend)
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


def test_a_late_hello_on_the_second_attempt_still_ends_at_the_deadline(daemon):
    # The second connection answers its hello late and then never replies: the time left bounds the whole
    # second exchange, not each wait on its socket.
    peer = daemon(Connection(CLOSE), Connection(SILENT, hello_delay=1.2))
    got = outcome(peer, 'state.scheduler_status', {}, seconds=2.0)
    assert got[0] == 'error', got
    assert got[1] == 'E_DEADLINE', got
    assert got[-1] < 2.5, got


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


STALL = 8.0


class _Stalling(socket.socket):
    """A connect that does not complete, as against a daemon whose listen queue is full: it waits out its
    timeout (or STALL with none) and fails."""

    def connect(self, target):
        timeout = self.gettimeout()
        time.sleep(STALL if timeout is None else min(timeout, STALL))
        raise TimeoutError('timed out')


def _stall_after(monkeypatch, real):
    """daemon_client's sockets: the first `real` connect for real, every later one stalls."""
    made = []

    def make(*args):
        made.append(1)
        return (socket.socket if len(made) <= real else _Stalling)(*args)
    monkeypatch.setattr(daemon_client, 'socket', types.SimpleNamespace(
        socket=make, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM))


def test_a_connect_that_never_completes_ends_inside_the_deadline_and_its_grace(daemon, monkeypatch):
    peer = daemon(Connection(ANSWER))
    _stall_after(monkeypatch, 0)
    got = outcome(peer, 'state.scheduler_status', {}, seconds=1.0)
    assert got[0] == 'error', got
    assert got[-1] < 1.0 + daemon_client.GRACE_SECONDS + 0.5, got
    assert peer.calls == []


def test_a_connect_that_never_completes_sends_nothing_and_says_so(daemon, monkeypatch):
    # No attempt reached the daemon, so no pass or write runs for this call: E_DEADLINE would tell `kelmo dream`
    # that the pass is still running in a service daemon. It is a transport error that may be tried again.
    peer = daemon(Connection(ANSWER))
    _stall_after(monkeypatch, 0)
    got = outcome(peer, 'state.job_run', {'job': 'dream', 'options': {}}, seconds=0.5)
    assert got[:3] == ('error', 'E_TRANSPORT', True), got
    assert 'nothing was sent' in got[3], got
    assert peer.calls == []


def test_a_resend_whose_connect_never_completes_ends_at_the_deadline(daemon, monkeypatch):
    peer = daemon(Connection(hold(0.2, CLOSE)), Connection(ANSWER))
    _stall_after(monkeypatch, 1)
    got = outcome(peer, 'state.scheduler_status', {}, seconds=2.0)
    assert got[:2] == ('error', 'E_DEADLINE'), got
    assert got[-1] < 2.5, got
    assert len(peer.calls) == 1


def test_a_keyless_write_cut_on_its_second_attempt_reports_its_effect_unknown(daemon):
    # The first connection ends before its hello is answered, so nothing was sent and the call goes again; the
    # second cuts it after it was sent. That write may have taken effect, and the caller must hear so.
    peer = daemon(Connection(hello=False), Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.cli', {'argv': ['create']})
    assert got[0] == 'error', got
    assert got[1] == 'E_TRANSPORT' and got[2] is False, got
    assert 'unknown' in got[3], got
    assert len(peer.calls) == 1


def test_a_cli_call_its_caller_marks_safe_to_resend_is_sent_again_after_a_cut(daemon):
    peer = daemon(Connection(CLOSE), Connection(ANSWER))
    got = outcome(peer, 'state.cli', {'argv': ['search', 'x']}, resend=True)
    assert got[:2] == ('ok', {'answer': 2}), got
    assert [header['op'] for header, _body in peer.calls] == ['state.cli', 'state.cli']


@pytest.mark.parametrize('argv,resend', [
    (['search', 'x'], True),
    (['show', 'm-1'], True),
    (['queue', 'list'], True),
    (['create', '--text', 'A note.', '--project', 'p', '--idempotency-key', 'k-1'], True),
    (['create', '--text', 'A note.', '--project', 'p'], False),
    (['forget', 'm-1'], False),
])
def test_the_cli_lets_a_read_or_a_keyed_write_be_sent_again(tmp_path, monkeypatch, argv, resend):
    """A command that changes nothing, or a write whose own key the server settles once, may go again after a
    cut; any other CLI call may have taken effect."""
    from kelmo import cli
    from kelmo.interface import build_parser
    parser, _commands = build_parser('0')
    args = parser.parse_args([*argv, '--vault', str(tmp_path)])
    calls = []
    monkeypatch.setattr(daemon_client, 'ensure', lambda root, **kwargs: tmp_path / 'kelmod.sock')
    monkeypatch.setattr(daemon_client, 'call', lambda root, op, body, **kwargs: calls.append((op, kwargs))
                        or {'ok': True})
    cli.through_daemon(args, [*argv, '--vault', str(tmp_path)], str(tmp_path / 'kelmo'))
    assert [(op, kwargs.get('resend', False)) for op, kwargs in calls] == [('state.cli', resend)]
