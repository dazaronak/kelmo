"""kelmod's leftover sweep: before it starts a state core, it kills the executor groups that core
recorded whose leader is gone or is still the process recorded, and it removes every record."""
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from daemon_harness import KELMOD, SRC, Daemon, wait_for
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core.spec import build as kelmod_spec
from kelmo.daemon import process_start


@pytest.fixture(autouse=True)
def kelmod_built():
    if not KELMOD.is_file():
        pytest.skip(f'kelmod is not built at {KELMOD}; build it with tools/build_kelmod.sh')


@pytest.fixture
def leftover_group():
    """Starts a sleeping process that leads its own process group; each is ended after the test."""
    processes = []

    def start():
        process = subprocess.Popen(['sleep', '60'], start_new_session=True)
        processes.append(process)
        return process

    yield start
    for process in processes:
        process.kill()
        process.wait()


def write_record(root, pid, start):
    """The executor record the state core writes for a group: its pid and its leader's start time."""
    groups = root / '.state' / 'executors' / 'state'
    groups.mkdir(parents=True, exist_ok=True)
    record = groups / str(pid)
    record.write_text(json.dumps({'pgid': pid, 'start': start}))
    return record


def start_kelmod(root, tmp_path):
    """Starts kelmod on the vault and returns its daemon once the state core reports ready."""
    # A Unix socket path must stay short, so the runtime directory goes under /tmp.
    runtime = Path(tempfile.mkdtemp(prefix='kd', dir='/tmp'))
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(kelmod_spec(root, runtime_dir=runtime, source_root=SRC)))
    log = (tmp_path / 'kelmod.log').open('w')
    command = [str(KELMOD), 'serve', '--spec', str(spec_path)]
    daemon = Daemon(subprocess.Popen(command, stdout=log, stderr=log), spec_path, runtime,
                    tmp_path / 'scratch', log)

    def state_ready():
        status = daemon.status()
        return status is not None and any(
            core['name'] == 'state' and core['ready'] for core in status['cores'])

    try:
        wait_for(state_ready, timeout=60, what='the state core to be ready')
    except BaseException:
        daemon.stop()
        raise
    return daemon


def test_kelmod_kills_a_leftover_group_whose_leader_is_the_recorded_process(
        world_factory, tmp_path, leftover_group):
    root = world_factory().root
    leftover = leftover_group()
    record = write_record(root, leftover.pid, process_start(leftover.pid))
    daemon = start_kelmod(root, tmp_path)
    try:
        # A negative exit code means the process was ended by a signal.
        assert leftover.wait(timeout=10) < 0
        assert not record.exists()
    finally:
        daemon.stop()


def test_kelmod_keeps_a_group_whose_leader_started_at_another_time(
        world_factory, tmp_path, leftover_group):
    root = world_factory().root
    leftover = leftover_group()
    record = write_record(root, leftover.pid, '1.000000')
    daemon = start_kelmod(root, tmp_path)
    try:
        # poll() reaps an exited process, so None means the group is still running.
        assert leftover.poll() is None
        assert not record.exists()
    finally:
        daemon.stop()


def test_kelmod_keeps_a_live_group_whose_record_has_no_start_time(
        world_factory, tmp_path, leftover_group):
    root = world_factory().root
    leftover = leftover_group()
    record = write_record(root, leftover.pid, None)
    daemon = start_kelmod(root, tmp_path)
    try:
        # A missing start time never equals the running one, so a live leader is left alone.
        assert leftover.poll() is None
        assert not record.exists()
    finally:
        daemon.stop()
