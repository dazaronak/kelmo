"""kelmod kills the executor groups a crashed state core left behind, before starting the core again."""
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from daemon_harness import KELMOD, SRC, Daemon, wait_for
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core.spec import build as kelmod_spec


def test_kelmod_kills_leftover_executor_groups_before_starting_the_core(world_factory, tmp_path):
    if not KELMOD.is_file():
        pytest.skip(f'kelmod is not built at {KELMOD}; build it with tools/build_kelmod.sh')
    root = world_factory().root
    groups = root / '.state' / 'executors'
    groups.mkdir(parents=True, exist_ok=True)
    leftover = subprocess.Popen(['sleep', '60'], start_new_session=True)
    (groups / str(leftover.pid)).write_text(str(leftover.pid))
    # A Unix socket path must stay short, so the runtime directory goes under /tmp.
    runtime = Path(tempfile.mkdtemp(prefix='kd', dir='/tmp'))
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(kelmod_spec(root, runtime_dir=runtime, source_root=SRC)))
    log = (tmp_path / 'kelmod.log').open('w')
    command = [str(KELMOD), 'serve', '--spec', str(spec_path)]
    proc = subprocess.Popen(command, stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, tmp_path / 'scratch', log)

    def state_ready():
        status = daemon.status()
        return status is not None and any(
            core['name'] == 'state' and core['ready'] for core in status['cores'])

    try:
        wait_for(state_ready, timeout=60, what='the state core to be ready')
        # A negative exit code means the process was ended by a signal.
        assert leftover.wait(timeout=10) < 0
        assert not (groups / str(leftover.pid)).exists()
    finally:
        daemon.stop()
        leftover.kill()
        leftover.wait()
