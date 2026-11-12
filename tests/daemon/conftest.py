"""Fixture for the daemon black-box tests: kelmod started per call, stopped with its stub cores."""
import pytest

from daemon_harness import KELMOD, launch, wait_for


@pytest.fixture
def start(tmp_path):
    """`start(**options)` launches kelmod on a fresh spec and waits until both cores are ready.

    `wait_ready=False` skips the wait. `options` are spec keys or `lanes`, `state_argv`, `state_budget`,
    `archive_argv`."""
    daemons = []

    def launch_daemon(*, wait_ready=True, **options):
        if not KELMOD.is_file():
            message = f"kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh"
            pytest.fail(message, pytrace=False)
        daemon = launch(tmp_path / str(len(daemons)), **options)
        daemons.append(daemon)
        if wait_ready:
            wait_for(daemon.all_ready, timeout=30, what="both cores ready")
        return daemon

    yield launch_daemon
    for daemon in daemons:
        daemon.stop()
