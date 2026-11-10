"""The archive core under kelmod.

The state core's archive is the archive core: memory tools reach it through the state core,
a client cannot call it directly, and a killed archive core starts again with its archive.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from archive_core_support import PROJECT
from daemon_harness import KELMOD, SRC, Daemon, kill_group, wait_for

from kelmo.core.spec import build

RECORD = 'Synthetic record stored through the archive core.'


def start_kelmod(root, workdir, *, in_process):
    """kelmod serving `root` with the archive as its own core; returns once both are ready."""
    if not KELMOD.is_file():
        pytest.skip(f'kelmod is not built at {KELMOD}; build it first')
    # A Unix socket path must stay short, so the runtime directory goes under /tmp.
    runtime = Path(tempfile.mkdtemp(prefix='ac', dir='/tmp'))
    scratch = workdir / 'scratch'
    scratch.mkdir()
    spec = build(root, runtime_dir=runtime, source_root=SRC, archive_core=True,
                 archive_in_process=in_process)
    spec_path = workdir / 'spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (workdir / 'kelmod.log').open('w')
    command = [str(KELMOD), 'serve', '--spec', str(spec_path)]
    proc = subprocess.Popen(command, stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, scratch, log)
    try:
        wait_for(daemon.all_ready, timeout=120, what='both cores ready')
    except BaseException:
        daemon.stop()
        raise
    return daemon


def kelmod_call(daemon, op, body):
    command = [str(KELMOD), 'call', op, '--runtime-dir', str(daemon.runtime),
               '--body', json.dumps(body), '--deadline-ms', '120000']
    return subprocess.run(command, capture_output=True, text=True, timeout=180)


def store_record(daemon):
    """Stores RECORD through memory_store and returns the id it was given."""
    arguments = {'content': RECORD, 'kind': 'event', 'domain': 'dev', 'project': PROJECT}
    stored = kelmod_call(daemon, 'state.tool', {'tool': 'memory_store', 'arguments': arguments})
    assert stored.returncode == 0, stored.stdout + stored.stderr
    return json.loads(stored.stdout)['id']


def listed_ids(daemon):
    """The ids memory_list returns for PROJECT."""
    arguments = {'project': PROJECT}
    listed = kelmod_call(daemon, 'state.tool', {'tool': 'memory_list', 'arguments': arguments})
    assert listed.returncode == 0, listed.stdout + listed.stderr
    return [item['id'] for item in json.loads(listed.stdout)['items']]


def exercise_archive(daemon):
    """A memory round trip, then a direct archive call, which a client may not make."""
    assert store_record(daemon) in listed_ids(daemon)
    refused = kelmod_call(daemon, 'archive.counts', {})
    assert refused.returncode == 1
    assert json.loads(refused.stdout)['error']['code'] == 'E_FORBIDDEN'


def test_memory_tools_reach_the_archive_core_and_clients_cannot_call_it(vault, tmp_path):
    daemon = start_kelmod(vault, tmp_path, in_process=True)
    try:
        exercise_archive(daemon)
    finally:
        daemon.stop()


def test_confined_archive_core_serves_the_same_calls(vault, tmp_path):
    if sys.platform == 'darwin':
        reason = None if Path('/usr/bin/sandbox-exec').is_file() else (
            '/usr/bin/sandbox-exec is absent')
    elif sys.platform.startswith('linux'):
        reason = None if shutil.which('bwrap') else 'bubblewrap (bwrap) is missing'
    else:
        reason = f'native confinement is not available on {sys.platform}'
    if reason is not None:
        pytest.skip(reason)
    daemon = start_kelmod(vault, tmp_path, in_process=False)
    try:
        exercise_archive(daemon)
    finally:
        daemon.stop()


def started_again(daemon, before):
    current = daemon.core('archive')
    return current is not None and current['ready'] and current['pid'] != before['pid']


def test_a_killed_archive_core_starts_again_and_the_archive_still_answers(vault, tmp_path):
    daemon = start_kelmod(vault, tmp_path, in_process=True)
    try:
        record = store_record(daemon)
        before = daemon.core('archive')
        kill_group(before['pid'])
        wait_for(lambda: started_again(daemon, before), timeout=120,
                 what='the archive core to start again')
        assert record in listed_ids(daemon)
        assert daemon.core('archive')['restarts'] > before['restarts']
    finally:
        daemon.stop()
