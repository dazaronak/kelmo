"""kelmod runs the real state core as its `state` core. A `state.tool` call answers through it with the
vault's own status. The archive core stays the daemon harness's stub, since only the state core is
under test."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from daemon_harness import KELMOD, SRC, Daemon, build_spec, wait_for

from kelmo.config import Config
from test_action_state import world_factory as world_factory  # noqa: F401

LANES = {'handler': 32, 'read': 4, 'write': 8}


def state_spec(root, runtime, scratch):
    spec = build_spec(root, runtime, scratch)
    spec['origin'] = {'client_id': 'local'}
    lanes = {name: {'limit': limit, 'queue': 256, 'order': 'fifo'}
             for name, limit in LANES.items()}
    state = {
        'name': 'state',
        'argv': [sys.executable, '-m', 'kelmo.core.state_core', '--vault', str(root)],
        'env': {'PYTHONPATH': str(SRC)},
        'lanes': lanes,
        'budget': None,
    }
    spec['cores'] = [state, spec['cores'][1]]
    archive = [item for item in spec['operations'] if item['name'].startswith('archive.')]
    tool = {'name': 'state.tool', 'lanes': ['handler'], 'replay': 'never'}
    spec['operations'] = archive + [tool]
    return spec


def test_kelmod_serves_the_real_state_core_and_answers_memory_status(world_factory, tmp_path):
    if not KELMOD.is_file():
        pytest.skip(f'kelmod is not built at {KELMOD}; build it with tools/build_kelmod.sh')
    root = world_factory().root
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    # A Unix socket path must stay short, so the runtime directory goes under /tmp, as in the harness.
    runtime = Path(tempfile.mkdtemp(prefix='kd', dir='/tmp'))
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(state_spec(root, runtime, scratch)))
    log = (tmp_path / 'kelmod.log').open('w')
    command = [str(KELMOD), 'serve', '--spec', str(spec_path)]
    proc = subprocess.Popen(command, stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, scratch, log)
    try:
        wait_for(daemon.all_ready, timeout=60, what='both cores ready')
        body = json.dumps({'tool': 'memory_status', 'arguments': {}})
        result = subprocess.run(
            [str(KELMOD), 'call', 'state.tool', '--runtime-dir', str(runtime), '--body', body],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)['vault_id'] == Config.load(root).vault_id
    finally:
        daemon.stop()
