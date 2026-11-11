"""Drain end: a core that outlives the drain is killed; every waiting call is answered."""
import os
import signal

from daemon_harness import error_of, wait_for

# Calls that must outlive the drain: the drain, not their own deadline, ends them.
LONG_MS = 60000


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def sleep(client, tag):
    return client.call("state.sleep", {"tag": tag, "seconds": 60}, deadline_ms=LONG_MS)


def test_a_core_that_outlives_the_drain_is_killed_with_its_daemon(start):
    daemon = start(drain_timeout_ms=1000)
    try:
        client = daemon.client()
        sleep(client, "stalled")
        wait_for(lambda: "stalled" in daemon.tags(), what="the sleep running on the core")
        core_pid = daemon.core("state")["pid"]
        assert isinstance(core_pid, int), core_pid
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=15) == 0
        wait_for(lambda: gone(core_pid), timeout=5, what="the state core to be gone")
    finally:
        daemon.stop()


def test_a_drain_past_its_deadline_answers_every_waiting_call(start):
    daemon = start(drain_timeout_ms=1000)
    try:
        client = daemon.client()
        running = sleep(client, "drain-running")
        wait_for(lambda: "drain-running" in daemon.tags(), what="the sleep running on the core")
        queued = sleep(client, "drain-queued")
        wait_for(lambda: daemon.lane_counts() == (1, 1), what="the second sleep queued")
        daemon.proc.send_signal(signal.SIGTERM)
        # Read replies while the daemon is up; after its exit they may not have been flushed.
        queued_error = error_of(client.reply(queued, timeout=15)[0])
        assert queued_error["code"] == "E_UNAVAILABLE"
        assert queued_error["retryable"] is True
        assert isinstance(queued_error.get("retry_after_ms"), int), queued_error
        # state.sleep is a read in the stub's table: a dispatched one may be sent again.
        running_error = error_of(client.reply(running, timeout=15)[0])
        assert running_error["code"] == "E_UNAVAILABLE"
        assert running_error["retryable"] is True
        assert isinstance(running_error.get("retry_after_ms"), int), running_error
        assert daemon.wait_exit(timeout=15) == 0
    finally:
        daemon.stop()


def test_a_dispatched_call_without_a_key_is_refused_as_not_retryable(start):
    # state.freeze is never replayed and keyless: its effect is unknown, not safe to resend.
    daemon = start(drain_timeout_ms=1000)
    try:
        client = daemon.client()
        frozen = client.call("state.freeze", {}, deadline_ms=LONG_MS)
        wait_for(lambda: daemon.lane_counts("fast") == (0, 1), what="the freeze call dispatched")
        daemon.proc.send_signal(signal.SIGTERM)
        error = error_of(client.reply(frozen, timeout=15)[0])
        assert error["code"] == "E_UNAVAILABLE"
        assert error["retryable"] is False
        assert "retry_after_ms" not in error, error
        assert daemon.wait_exit(timeout=15) == 0
    finally:
        daemon.stop()
