"""A daemon stop keeps the routes of a draining core: callers stop first, the cores they call after them."""
import json
import signal


def stop_times(daemon, core):
    path = daemon.scratch / f"{core}.stops"
    if not path.exists():
        return []
    return [json.loads(line)["at"] for line in path.read_text().splitlines()]


def test_a_draining_state_core_reaches_the_archive_and_the_archive_stops_after_it(start):
    daemon = start(state_argv=["--call-archive-on-stop"])
    try:
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=20) == 0
        outcome = json.loads((daemon.scratch / "state.stop-call").read_text())
        assert outcome["ok"] is True, outcome
        assert outcome["result"]["core"] == "archive"
        assert len(stop_times(daemon, "state")) == 1
        archive_stops = stop_times(daemon, "archive")
        assert len(archive_stops) == 1
        assert archive_stops[0] > outcome["at"]
    finally:
        daemon.stop()


def test_without_calls_both_cores_stop_in_order(start):
    daemon = start()
    try:
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=20) == 0
        (state,), (archive,) = stop_times(daemon, "state"), stop_times(daemon, "archive")
        assert archive >= state
    finally:
        daemon.stop()
