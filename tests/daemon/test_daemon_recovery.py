"""What happens when a core dies, freezes or grows: replay by class, restarts, heartbeats, memory"""
import subprocess
import time

from daemon_harness import KELMOD, error_of, wait_for


def deliveries(daemon):
    """How many times the crash operation reached a core."""
    return sum(1 for entry in daemon.seen() if entry["op"] == "crash")


def test_a_read_call_is_sent_again_once_after_the_core_dies(start):
    daemon = start()
    client = daemon.client()
    header, body = client.reply(client.call("state.crash_once", {"tag": "read"}))
    assert header["ok"] is True
    assert body["recovered"] is True
    assert deliveries(daemon) == 2  # the first delivery died with the core; the replay answered
    assert daemon.core()["restarts"] == 1


def test_a_keyed_call_with_a_key_is_sent_again_once(start):
    daemon = start()
    client = daemon.client()
    header, body = client.reply(client.call("state.crash_keyed", {}, key="order-1"))
    assert header["ok"] is True
    assert body["recovered"] is True
    assert deliveries(daemon) == 2


def test_a_keyed_call_without_a_key_is_refused_as_restarted(start):
    daemon = start()
    client = daemon.client()
    error = error_of(client.reply(client.call("state.crash_keyed", {}))[0])
    assert error["code"] == "E_CORE_RESTARTED"
    assert error["retryable"] is True
    wait_for(lambda: daemon.settled(restarts=1), what="the core restarted")
    assert deliveries(daemon) == 1


def test_a_never_call_is_not_sent_again_even_with_a_key(start):
    daemon = start()
    client = daemon.client()
    error = error_of(client.reply(client.call("state.crash_never", {}, key="order-2"))[0])
    assert error["code"] == "E_CORE_RESTARTED"
    assert error["retryable"] is True
    wait_for(lambda: daemon.settled(restarts=1), what="the core restarted")
    assert deliveries(daemon) == 1


def test_queued_calls_survive_a_restart_and_run_on_the_new_core(start):
    daemon = start()
    client = daemon.client()
    crash = client.call("state.crash_never", {})
    queued = [client.call("state.sleep", {"tag": tag, "seconds": 0}) for tag in ("q1", "q2")]
    error = error_of(client.reply(crash)[0])
    assert error["code"] == "E_CORE_RESTARTED"
    crashed_pid = next(entry["pid"] for entry in daemon.seen() if entry["op"] == "crash")
    replies = []
    for call_id in queued:
        header, body = client.reply(call_id)
        assert header["ok"] is True
        replies.append(body)
    restarted_pid = daemon.core()["pid"]
    assert restarted_pid != crashed_pid
    assert [body["pid"] for body in replies] == [restarted_pid, restarted_pid]


def test_a_spent_restart_budget_exits_kelmod_with_70(start):
    daemon = start(wait_ready=False, restart_max=2, state_argv=["--crash-at-start"])
    assert daemon.wait_exit(timeout=30) == 70


def test_a_second_serve_on_the_same_root_exits_75(start):
    daemon = start()
    result = subprocess.run([str(KELMOD), "serve", "--spec", str(daemon.spec_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 75
    assert "E_BUSY" in result.stderr
    assert daemon.all_ready()


def test_a_frozen_core_is_killed_after_missed_heartbeats_and_restarted(start):
    daemon = start(heartbeat_ms=100, heartbeat_misses=3)
    old_pid = daemon.core()["pid"]
    client = daemon.client()
    began = time.monotonic()
    error = error_of(client.reply(client.call("state.freeze", {}, deadline_ms=10000))[0])
    assert error["code"] == "E_CORE_RESTARTED"
    # The missed heartbeats end the call; its 10 s deadline is the bound this must beat.
    assert time.monotonic() - began < 5
    wait_for(lambda: daemon.settled(restarts=1), what="the frozen core restarted")
    assert daemon.core()["pid"] != old_pid
    assert client.reply(client.call("state.echo", {}))[0]["ok"] is True


def test_soft_budget_sends_admin_trim_to_the_core(start):
    daemon = start(state_budget={"soft_mb": 60}, sample_ms=100)
    client = daemon.client()
    header, _ = client.reply(client.call("state.alloc", {"mb": 100}))
    assert header["ok"] is True
    wait_for(lambda: daemon.trims() >= 1, timeout=10, what="admin.trim reaching the core")
    assert client.reply(client.call("state.echo", {}))[0]["ok"] is True


def test_hard_budget_kills_and_restarts_the_core(start):
    daemon = start(state_budget={"hard_mb": 150}, sample_ms=100)
    old_pid = daemon.core()["pid"]
    client = daemon.client()
    # Answered, or refused as restarted when the sample saw the budget first: both are correct.
    client.reply(client.call("state.alloc", {"mb": 200}))
    wait_for(lambda: daemon.settled(restarts=1), timeout=15,
             what="the core killed over its hard budget")
    assert daemon.core()["pid"] != old_pid
    assert client.reply(client.call("state.echo", {}))[0]["ok"] is True
