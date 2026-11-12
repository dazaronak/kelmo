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


def recycle_asks(daemon, core="state"):
    """How many times kelmod sent the core admin.recycle."""
    path = daemon.scratch / f"{core}.recycles"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_core_over_soft_budget_that_agrees_to_recycle_is_restarted(start, monkeypatch):
    daemon = start_d58(start, monkeypatch, sample_ms=100, state_budget={"soft_mb": 80},
                       state_argv=["--soft-trigger-once"])
    wait_for(lambda: daemon.settled(restarts=1), what="restart after an agreed recycle")
    assert recycle_asks(daemon) == 1
    assert (daemon.scratch / "state.trim-seen").exists()


def test_core_over_soft_budget_that_declines_recycle_is_kept(start, monkeypatch):
    daemon = start_d58(start, monkeypatch, sample_ms=100, state_budget={"soft_mb": 80},
                       state_argv=["--soft-trigger-once", "--decline-recycle"])
    wait_for(lambda: recycle_asks(daemon) >= 1, what="admin.recycle reaching the core")
    asked = time.monotonic()
    # Fifteen more samples over the soft budget: the core is neither restarted nor asked again within 30 s.
    wait_for(lambda: time.monotonic() - asked >= 1.5, what="more memory samples")
    core = daemon.core()
    assert core["ready"] is True and core["restarts"] == 0
    assert recycle_asks(daemon) == 1
    client = daemon.client()
    header, body = client.reply(client.call("state.echo", {"n": 1}))
    assert header["ok"] is True and body["body"] == {"n": 1}
    assert "declined a recycle (busy: job)" in (daemon.spec_path.parent / "kelmod.log").read_text()


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
    assert recycle_asks(daemon) == 1

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


def test_core_ignoring_admin_stop_is_killed_at_its_drain_deadline(start, monkeypatch):
    # The router's stop keeps the core's channel open (its own calls still need their replies), so a core
    # that ignores admin.stop gets no end of file: it is killed when its drain time is up, and the archive
    # is stopped after it.
    daemon = start_d58(start, monkeypatch, drain_timeout_ms=1000, state_argv=["--ignore-admin-stop"])
    try:
        began = time.monotonic()
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=10) == 0
        assert time.monotonic() - began >= 1.0
        assert (daemon.scratch / "state.admin-stop").exists()
        assert not (daemon.scratch / "state.finished").exists()
        assert (daemon.scratch / "archive.finished").exists()
    finally:
        daemon.stop()
