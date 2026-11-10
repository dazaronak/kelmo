"""What a connection sees when it calls a core: hello, capability, lanes, overload, deadline."""
import time

import pytest

from daemon_harness import error_of, wait_for


def test_lane_runs_one_call_at_a_time_in_fifo_order(start):
    daemon = start()
    client = daemon.client()
    calls = [client.call("state.sleep", {"tag": "a", "seconds": 1.0}),
             client.call("state.sleep", {"tag": "b", "seconds": 0.1}),
             client.call("state.sleep", {"tag": "c", "seconds": 0.1})]
    wait_for(lambda: daemon.lane_counts() == (2, 1), what="one call running and two queued")
    for call_id in calls:
        assert client.reply(call_id)[0]["ok"] is True
    assert daemon.tags() == ["a", "b", "c"]
    starts = [entry["at"] for entry in daemon.seen()]
    assert starts[1] - starts[0] >= 0.9  # the first call holds the lane for its whole second
    assert starts[2] - starts[1] >= 0.09


def test_round_robin_serves_each_origin_in_turn(start):
    daemon = start(lanes={"work": {"limit": 1, "queue": 16, "order": "rr"}})
    alpha = daemon.client(origin={"client_id": "a"})
    beta = daemon.client(origin={"client_id": "b"})
    block = alpha.call("state.sleep", {"tag": "block", "seconds": 1.0})
    wait_for(lambda: daemon.lane_counts() == (0, 1), what="the blocking call in flight")
    alpha_calls = [alpha.call("state.sleep", {"tag": f"a{n}", "seconds": 0.05})
                   for n in (1, 2, 3)]
    wait_for(lambda: daemon.lane_counts() == (3, 1), what="alpha's calls queued")
    beta_calls = [beta.call("state.sleep", {"tag": f"b{n}", "seconds": 0.05})
                  for n in (1, 2, 3)]
    wait_for(lambda: daemon.lane_counts() == (6, 1), what="beta's calls queued")
    assert alpha.reply(block)[0]["ok"] is True
    for call_id in alpha_calls:
        assert alpha.reply(call_id)[0]["ok"] is True
    for call_id in beta_calls:
        assert beta.reply(call_id)[0]["ok"] is True
    assert daemon.tags() == ["block", "a1", "b1", "a2", "b2", "a3", "b3"]


def test_a_full_queue_refuses_the_next_call_as_overloaded(start):
    daemon = start(lanes={"work": {"limit": 1, "queue": 2, "order": "fifo"}})
    client = daemon.client()
    calls = [client.call("state.sleep", {"tag": f"t{n}", "seconds": 0.5}) for n in range(4)]
    error = error_of(client.reply(calls[3])[0])
    assert error["code"] == "E_OVERLOADED"
    assert error["retryable"] is True
    assert isinstance(error["retry_after_ms"], int)
    for call_id in calls[:3]:
        assert client.reply(call_id)[0]["ok"] is True
    assert daemon.tags() == ["t0", "t1", "t2"]


def test_a_call_past_its_deadline_in_the_queue_never_reaches_the_core(start):
    daemon = start()
    client = daemon.client()
    block = client.call("state.sleep", {"tag": "block", "seconds": 1.0})
    late = client.call("state.sleep", {"tag": "late", "seconds": 0}, deadline_ms=200)
    began = time.monotonic()
    error = error_of(client.reply(late)[0])
    assert error["code"] == "E_DEADLINE"
    assert error["retryable"] is True
    assert time.monotonic() - began < 0.9  # answered at its deadline, not when the block ends
    assert client.reply(block)[0]["ok"] is True
    assert daemon.tags() == ["block"]


def test_cancel_answers_a_queued_call_without_running_it(start):
    daemon = start()
    client = daemon.client()
    block = client.call("state.sleep", {"tag": "block", "seconds": 0.5})
    queued = client.call("state.sleep", {"tag": "queued", "seconds": 0})
    client.cancel(queued)
    error = error_of(client.reply(queued)[0])
    assert error["code"] == "E_CANCELLED"
    assert error["retryable"] is True
    assert client.reply(block)[0]["ok"] is True
    assert daemon.tags() == ["block"]


def test_cancel_stops_a_call_in_flight_at_its_next_check(start):
    daemon = start()
    client = daemon.client()
    long_call = client.call("state.sleep", {"tag": "long", "seconds": 10})
    wait_for(lambda: "long" in daemon.tags(), what="the long call reaching the core")
    client.cancel(long_call)
    # The call sleeps 10 s; a reply inside 8 s means the handler stopped at its next check.
    header, _ = client.reply(long_call, timeout=8)
    assert error_of(header)["code"] == "E_CANCELLED"
    assert client.reply(client.call("state.echo", {}))[0]["ok"] is True


def test_a_connection_may_not_call_an_archive_operation(start):
    daemon = start()
    client = daemon.client()
    error = error_of(client.reply(client.call("archive.echo", {}))[0])
    assert error["code"] == "E_FORBIDDEN"
    assert error["retryable"] is False


def test_the_state_core_may_call_archive_but_archive_may_not_call_state(start):
    daemon = start()
    client = daemon.client()
    relayed = {"op": "archive.echo", "body": {"x": 1}}
    header, body = client.reply(client.call("state.relay", relayed))
    assert header["ok"] is True
    assert body["result"]["core"] == "archive"
    # The archive core's own refusal of state.echo comes back through the state core as data.
    relayed = {"op": "archive.relay", "body": {"op": "state.echo", "body": {}}}
    header, body = client.reply(client.call("state.relay", relayed))
    assert header["ok"] is True
    refusal = {"ok": False, "code": "E_FORBIDDEN", "retryable": False}
    assert body == {"ok": True, "result": refusal}


def test_a_call_before_hello_is_refused(start):
    daemon = start()
    client = daemon.client(hello=False)
    error = error_of(client.reply(client.call("state.echo", {}))[0])
    assert error["code"] == "E_PROTOCOL"
    assert error["retryable"] is False


def test_a_call_that_names_its_own_origin_is_refused(start):
    daemon = start()
    client = daemon.client()
    call_id = client.call("state.echo", {}, origin={"client_id": "someone-else"})
    error = error_of(client.reply(call_id)[0])
    assert error["code"] == "E_PROTOCOL"
    assert error["retryable"] is False


@pytest.mark.skip(reason="a peer of another uid needs a second account; one test user has none")
def test_a_peer_of_another_uid_is_not_admitted():
    """kelmod drops a connection whose peer uid differs from its own (ipc::same_user)."""
