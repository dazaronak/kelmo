"""Router behaviour the stub cores make observable: what a core's death sends again, which caller may read the daemon
status, and when a lane whose slot is never released gets its core restarted.

A core that must hold a call without answering it is frozen with SIGSTOP. A frozen process cannot answer the cancel
the router sends it, so what happens to the call is decided by the router alone, and the test kills the frozen core
to end it. The operation table is the stub table of daemon_harness, with a replay class changed where a case needs it."""
import os
import signal
import time

import pytest

from daemon_harness import OPERATIONS, error_of, wait_for

# The time the router gets to take a caller's cancel or close. It handles a local frame in microseconds, and the
# frozen core cannot change the call's state in that time, so only the router's record can.
ROUTER_SETTLE = 0.3


def table(overrides):
    """The stub operation table with the lane or replay class of some operations replaced. `overrides` maps an
    operation name to (lane, replay)."""
    rows = []
    for name, lane, replay in OPERATIONS:
        lane, replay = overrides.get(name, (lane, replay))
        rows.append({"name": name, "lanes": [lane], "replay": replay})
    return rows


def crashes(daemon):
    return sum(1 for entry in daemon.seen() if entry["op"] == "crash")


def delivered(daemon, tag):
    """The sleep invocations the cores recorded under this tag, in order: one per delivery to a core."""
    return [entry for entry in daemon.seen() if entry["op"] == "sleep" and entry["tag"] == tag]


def test_daemon_status_is_answered_to_the_local_origin_only(start):
    meta = {"role": "vault", "marker": "status-origin"}
    daemon = start(meta=meta)
    stranger = daemon.client(origin={"client_id": "someone-else"})
    error = error_of(stranger.reply(stranger.call("daemon.status"))[0])
    assert error["code"] == "E_FORBIDDEN"
    assert error["retryable"] is False
    # The local origin, named in hello, and no origin at all, which the router takes from the spec.
    named = daemon.client(origin={"client_id": "test-cli"})
    header, body = named.reply(named.call("daemon.status"))
    assert header["ok"] is True
    assert body["meta"] == meta
    unnamed = daemon.client()
    header, body = unnamed.reply(unnamed.call("daemon.status"))
    assert header["ok"] is True
    assert body["meta"] == meta


@pytest.mark.parametrize("ending", ["kept", "cancelled", "closed"])
def test_a_keyed_call_is_sent_again_only_while_its_caller_still_waits(start, ending):
    daemon = start(operations=table({"state.sleep": ("work", "keyed")}))
    client = daemon.client()
    tag = f"keyed-{ending}"
    call_id = client.call("state.sleep", {"tag": tag, "seconds": 2}, key=f"key-{ending}", deadline_ms=60000)
    wait_for(lambda: delivered(daemon, tag), what="the call running on the first core")
    frozen = daemon.core()["pid"]
    os.kill(frozen, signal.SIGSTOP)
    if ending == "cancelled":
        client.cancel(call_id)
    elif ending == "closed":
        client.close()
    time.sleep(ROUTER_SETTLE)
    os.kill(frozen, signal.SIGKILL)
    wait_for(lambda: daemon.settled(restarts=1), timeout=20, what="the core restarted")
    if ending == "kept":
        # Control: an uncancelled keyed call is sent once more, and the restarted core answers it.
        header, body = client.reply(call_id)
        assert header["ok"] is True and body["tag"] == tag
        sent = delivered(daemon, tag)
        assert len(sent) == 2 and sent[1]["pid"] != frozen
    elif ending == "cancelled":
        # The restarted core never sees the call, and the cancelled caller gets the restart refusal.
        error = error_of(client.reply(call_id)[0])
        assert error["code"] == "E_CORE_RESTARTED"
        assert error["retryable"] is False
        assert len(delivered(daemon, tag)) == 1
    else:
        # The caller is gone, so the restarted core never sees the call.
        assert len(delivered(daemon, tag)) == 1


@pytest.mark.parametrize("key, retryable", [(None, False), ("order-3", True)], ids=["keyless", "keyed"])
def test_a_never_call_in_flight_at_a_core_exit_is_refused_and_not_sent_again(start, key, retryable):
    daemon = start()
    client = daemon.client()
    error = error_of(client.reply(client.call("state.crash_never", {}, key=key))[0])
    assert error["code"] == "E_CORE_RESTARTED"
    # Only a key makes the refusal retryable, and no replay class sends a never call again.
    assert error["retryable"] is retryable
    wait_for(lambda: daemon.settled(restarts=1), what="the core restarted")
    assert crashes(daemon) == 1


def test_a_lane_whose_slot_is_held_by_an_unanswered_call_restarts_its_core(start):
    daemon = start()
    client = daemon.client()
    first = daemon.core()["pid"]
    call_id = client.call("state.sleep", {"tag": "stuck", "seconds": 60}, deadline_ms=300)
    wait_for(lambda: delivered(daemon, "stuck"), what="the call running on the core")
    # The core never answers the call or the cancel the router sends after its deadline and grace period.
    os.kill(first, signal.SIGSTOP)
    error = error_of(client.reply(call_id, timeout=20)[0])
    assert error["code"] == "E_DEADLINE"
    wait_for(lambda: daemon.settled(restarts=1), timeout=20, what="the core of the stuck lane restarted")
    assert daemon.core()["pid"] != first
    # The lane serves again on the new core.
    header, _ = client.reply(client.call("state.sleep", {"tag": "after", "seconds": 0}))
    assert header["ok"] is True
