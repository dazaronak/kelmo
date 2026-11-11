"""D58 supervisor lifecycle and child open-file limit regressions."""
import json
import resource
import signal
import time
from pathlib import Path

import daemon_harness
from daemon_harness import error_of, wait_for


STUB = Path(__file__).with_name("stub_d58.py")


def start_d58(start, monkeypatch, **options):
    monkeypatch.setattr(daemon_harness, "STUB", STUB)
    return start(**options)


def test_core_inherits_daemon_limit_capped_for_select(start, monkeypatch):
    original_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    daemon_cap = 10240 if hard == resource.RLIM_INFINITY else min(hard, 10240)
    daemon_soft = max(original_soft, daemon_cap)
    expected = (original_soft if original_soft == resource.RLIM_INFINITY else
                max(original_soft, min(daemon_soft, 1024)))

    daemon = start_d58(start, monkeypatch)
    limits = json.loads((daemon.scratch / "state.limits.json").read_text())
    assert limits[0] == expected


def test_planned_soft_limit_restart_spares_budget_but_crashes_consume_it(start, monkeypatch):
    daemon = start_d58(start, monkeypatch, restart_max=1, sample_ms=100,
                       state_budget={"soft_mb": 80}, state_argv=["--soft-trigger-once"])
    client = daemon.client()
    wait_for(lambda: daemon.settled(restarts=1), what="planned soft-limit restart")

    header, body = client.reply(client.call("state.crash_once"))
    assert header["ok"] is True and body["recovered"] is True
    wait_for(lambda: daemon.settled(restarts=2), what="one crash restart after the planned restart")
    assert daemon.proc.poll() is None

    error = error_of(client.reply(client.call("state.crash_never"))[0])
    assert error["code"] == "E_CORE_RESTARTED"
    assert daemon.wait_exit(timeout=15) == 70


def test_first_stop_during_planned_restart_gets_a_fresh_drain(start, monkeypatch):
    drain_ms = 1000
    daemon = start_d58(start, monkeypatch, sample_ms=100, drain_timeout_ms=drain_ms,
                       state_budget={"soft_mb": 80},
                       state_argv=["--soft-trigger-once", "--hold-admin-stop",
                                   "--linger-after-stop", "0.45"])
    try:
        wait_for(lambda: (daemon.scratch / "state.admin-stop").exists(),
                 what="planned admin.stop reaching the core")
        original_pid = daemon.core()["pid"]
        began = time.monotonic()
        wait_for(lambda: time.monotonic() - began >= 0.7,
                 what="most of the planned drain window to elapse")
        daemon.proc.send_signal(signal.SIGTERM)
        (daemon.scratch / "state.release-admin-stop").touch()
        assert daemon.wait_exit(timeout=10) == 0
        assert (daemon.scratch / "state.finished").read_text() == str(original_pid)
    finally:
        daemon.stop()


def test_stop_closes_writer_so_core_observes_eof_and_cleans_up(start, monkeypatch):
    daemon = start_d58(start, monkeypatch, state_argv=["--ignore-admin-stop"])
    try:
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=10) == 0
        assert (daemon.scratch / "state.finished").exists()
    finally:
        daemon.stop()
