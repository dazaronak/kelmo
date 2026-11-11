"""Shutdown drain: a stopping kelmod refuses new calls, finishes admitted ones, stops in time."""
import signal
import time

from daemon_harness import error_of, wait_for


def refusal_of(client, op, timeout=5.0):
    """The error of the first refused call to `op`.

    A call answered before the stop began succeeds and is skipped: the signal reaches the
    router a moment after it is sent, so a call sent right after it may still be admitted.
    """
    end = time.monotonic() + timeout
    while True:
        header, _ = client.reply(client.call(op, {}))
        if header["ok"] is False:
            return error_of(header)
        if time.monotonic() >= end:
            raise AssertionError(f"no call to {op} was refused")
        time.sleep(0.01)


def reply_or_close(client, call_id, timeout):
    """The header of the reply to the call, or None when the daemon closed the connection."""
    try:
        return client.reply(call_id, timeout=timeout)[0]
    except ConnectionError:
        return None


def test_a_stopping_daemon_refuses_new_calls_and_finishes_admitted_ones(start):
    daemon = start()
    try:
        admitted, newcomer = daemon.client(), daemon.client()
        call_id = admitted.call("state.sleep", {"tag": "drain-a", "seconds": 2})
        wait_for(lambda: "drain-a" in daemon.tags(), what="the sleep running on the core")
        daemon.proc.send_signal(signal.SIGTERM)
        error = refusal_of(newcomer, "state.echo")
        assert error["code"] == "E_UNAVAILABLE"
        assert error["retryable"] is True
        assert isinstance(error.get("retry_after_ms"), int), error
        header, body = admitted.reply(call_id)
        assert header["ok"] is True, header
        assert body["tag"] == "drain-a"
        assert daemon.wait_exit(timeout=30) == 0
    finally:
        daemon.stop()


def test_a_drain_gives_up_on_a_call_that_outlives_the_drain_timeout(start):
    daemon = start(drain_timeout_ms=1000)
    try:
        client = daemon.client()
        call_id = client.call("state.sleep", {"tag": "drain-long", "seconds": 30},
                              deadline_ms=60000)
        wait_for(lambda: "drain-long" in daemon.tags(), what="the sleep running on the core")
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=15) == 0
        # The daemon has exited. A reply it sent is still readable, or the socket is closed; a
        # socket that stays open means a leaked descriptor, and the reply timeout reports it.
        reply = reply_or_close(client, call_id, timeout=5)
        assert reply is None or reply["ok"] is False, reply
    finally:
        daemon.stop()
