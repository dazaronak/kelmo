"""Shutdown of a stopping core: it finishes its own shutdown after its channel closes, and a core that outlives
the drain deadline is killed together with the daemon. The stub core's `--linger` option stands in for a core
whose `main` keeps working after `serve()` returns."""
import os
import signal

from daemon_harness import wait_for


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_stopping_core_finishes_its_own_shutdown(start):
    """The stub core lingers 1.5 s after its channel closes. The drain deadline is 8 s, so the supervisor
    waits for the core to exit and does not kill it when the channel ends."""
    daemon = start(state_argv=["--linger", "1.5"], drain_timeout_ms=8000)
    try:
        daemon.proc.send_signal(signal.SIGTERM)
        assert daemon.wait_exit(timeout=20) == 0
        assert (daemon.scratch / "state.finished").exists()
    finally:
        daemon.stop()


def test_a_core_that_lingers_past_the_drain_deadline_is_killed(start):
    """The stub core would linger 30 s, but the drain deadline is 1.5 s. The core is killed at the deadline,
    so it never writes its marker, and the daemon exits well within 15 s with no stub process left."""
    daemon = start(state_argv=["--linger", "30"], drain_timeout_ms=1500)
    try:
        daemon.proc.send_signal(signal.SIGTERM)
        # wait_exit raises if kelmod is still running after 15 s.
        assert daemon.wait_exit(timeout=15) == 0
        assert not (daemon.scratch / "state.finished").exists()
        pids = daemon.stub_pids()
        assert pids, "no stub core ever started"
        wait_for(lambda: all(gone(pid) for pid in pids), timeout=5, what="every stub core to be gone")
    finally:
        daemon.stop()
