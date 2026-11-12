"""A state core told to stop: a job's model call ends at once, the job ends as stopped with nothing held and
nothing charged, and a recycle request is declined while a job runs and closes the gate when none does."""
import os
import sys
import threading
import time

import pytest
from daemon_harness import wait_for
from jobs_support import claim_rows, slow_executor
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo import stopping
from kelmo.core.jobs import RECYCLE_GATE_SECONDS
from kelmo.daemon import execute
from kelmo.errors import KelmoError, Stopping


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_stop_ends_a_jobs_model_call_and_the_job_ends_stopped_with_its_claim_released(
        world_factory, core_factory, tmp_path, monkeypatch):
    core = core_factory(world_factory().root)
    operation = core.storage.write(lambda writer: writer.capture(
        'Claimed by a stopping job.', 'stopping.md', role='implementer')['id'])
    argv = slow_executor(tmp_path, seconds=60)
    tokens, results = [], []

    def graph(options):
        with core.jobs.step():
            tokens.append(core.storage.writer.journal.claim(operation))
            execute(argv, {}, timeout=120)
        return 'not reached'

    monkeypatch.setattr(core.jobs, '_graph', graph)
    thread = threading.Thread(target=lambda: results.append(core.jobs.run('graph')))
    thread.start()
    marker = tmp_path / 'executor.pid'
    wait_for(marker.exists, timeout=20, what='the model call to start')
    pid = int(marker.read_text())
    began = time.monotonic()
    core.begin_stop()
    thread.join(20)
    assert not thread.is_alive()
    assert time.monotonic() - began < 10, 'the model call ends at once, not at its timeout'
    assert results == [{'state': 'stopped'}]
    assert core.jobs.last['graph']['state'] == 'stopped'
    assert 'graph' not in core.jobs.last_failure
    assert claim_rows(core, tokens[0]) == 0
    wait_for(lambda: not alive(pid), timeout=10, what='the executor to be killed')


def test_a_stop_ends_a_model_call_whose_provider_closed_its_output_and_kept_running(tmp_path):
    """The stop is checked while the provider ends, not only while it writes."""
    marker = tmp_path / 'quiet.pid'
    script = tmp_path / 'quiet_executor.py'
    script.write_text('import os, sys, time\n'
                      'sys.stdin.read()\n'
                      f'open({str(marker)!r}, "w").write(str(os.getpid()))\n'
                      'os.close(1); os.close(2)\n'
                      'time.sleep(60)\n')
    failures = []

    def call():
        try:
            execute([sys.executable, str(script)], {}, timeout=120)
        except BaseException as error:  # noqa: BLE001 - the test inspects what ended the call
            failures.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    wait_for(marker.exists, timeout=20, what='the provider to close its output')
    pid = int(marker.read_text())
    time.sleep(0.3)  # past the end of its output
    began = time.monotonic()
    stopping.request()
    thread.join(20)
    assert not thread.is_alive()
    assert time.monotonic() - began < 5, 'the call ends at the stop, not at its timeout'
    assert len(failures) == 1 and isinstance(failures[0], Stopping)
    wait_for(lambda: not alive(pid), timeout=10, what='the provider to be killed')


def test_no_job_starts_once_the_stop_is_requested(world_factory, core_factory):
    core = core_factory(world_factory().root)
    stopping.request()
    with pytest.raises(KelmoError) as refused:
        core.jobs.run('graph')
    assert refused.value.code == 'E_BUSY' and refused.value.retryable is True
    assert core.jobs._background('graph', {}) is False


def test_recycle_is_declined_while_a_job_runs(world_factory, core_factory, monkeypatch):
    core = core_factory(world_factory().root)
    started, release = threading.Event(), threading.Event()

    def graph(options):
        started.set()
        release.wait(10)
        return {}

    monkeypatch.setattr(core.jobs, '_graph', graph)
    thread = threading.Thread(target=core.jobs.run, args=('graph',))
    thread.start()
    try:
        assert started.wait(10)
        assert core.recycle() == ['graph']
    finally:
        release.set()
        thread.join(10)
    # Declined, so nothing was gated: the next job starts.
    monkeypatch.setattr(core.jobs, '_graph', lambda options: 'again')
    assert core.jobs.run('graph') == 'again'


def test_an_agreed_recycle_holds_new_jobs_until_the_gate_passes(world_factory, core_factory, monkeypatch):
    core = core_factory(world_factory().root)
    monkeypatch.setattr(core.jobs, '_graph', lambda options: 'ran')
    assert core.recycle() == []
    with pytest.raises(KelmoError) as refused:
        core.jobs.run('graph')
    assert refused.value.code == 'E_BUSY'
    assert core.jobs._background('graph', {}) is False
    assert core.jobs._gate_until - time.monotonic() <= RECYCLE_GATE_SECONDS
    core.jobs._gate_until = time.monotonic() - 1  # the gate has passed, and no stop came
    assert core.jobs.run('graph') == 'ran'
