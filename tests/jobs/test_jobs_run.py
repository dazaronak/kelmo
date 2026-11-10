"""Running a job in the state core: an interactive write passes a job's model call, one job per kind
at a time, the claims of a job end with it, stop waits for a running job, an executor's process group
is recorded while its call runs, and a worker pass completes on an empty vault."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from daemon_harness import wait_for
from jobs_support import claim_rows, slow_executor
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core.jobs import JobBusy
from kelmo.daemon import execute, process_start


def record_of(path):
    """The JSON object in an executor record, or None while it is missing or being written."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def test_interactive_write_finishes_during_a_job_model_call(world_factory, core_factory, tmp_path):
    core = core_factory(world_factory().root)
    argv = slow_executor(tmp_path, seconds=5)
    errors = []

    def job():
        try:
            with core.jobs.step():
                execute(argv, {}, timeout=30)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=job)
    thread.start()
    try:
        wait_for(lambda: (tmp_path / 'executor.pid').exists(), timeout=20,
                 what='the model call to start')
        started = time.monotonic()
        core.storage.write(lambda writer: writer.capture('Written during a model call.', 'during.md',
                                                        role='collector'))
        assert time.monotonic() - started < 1
        assert thread.is_alive()
    finally:
        thread.join(30)
    assert errors == []


def test_second_job_of_a_running_kind_is_refused_busy(world_factory, core_factory, monkeypatch):
    core = core_factory(world_factory().root)
    started, release = threading.Event(), threading.Event()

    def graph(options):
        started.set()
        release.wait(10)
        return 'graph-done'

    monkeypatch.setattr(core.jobs, '_graph', graph)
    monkeypatch.setattr(core.jobs, '_reembed', lambda options: 'reembed-done')
    thread = threading.Thread(target=core.jobs.run, args=('graph',))
    thread.start()
    try:
        assert started.wait(10)
        with pytest.raises(JobBusy) as refused:
            core.jobs.run('graph')
        assert refused.value.code == 'E_BUSY'
        assert refused.value.retryable is True
        assert core.jobs.run('reembed') == 'reembed-done'
    finally:
        release.set()
        thread.join(10)


@pytest.mark.parametrize('fails', [False, True], ids=['returns', 'raises'])
def test_claim_taken_by_a_job_is_gone_when_the_job_ends(world_factory, core_factory, monkeypatch,
                                                        fails):
    core = core_factory(world_factory().root)
    operation = core.storage.write(lambda writer: writer.capture(
        'Claimed by a job.', 'claimed.md', role='implementer')['id'])
    tokens, held = [], []

    def graph(options):
        with core.jobs.step():
            tokens.append(core.storage.writer.journal.claim(operation))
        held.append(claim_rows(core, tokens[0]))
        if fails:
            raise RuntimeError('The job failed after its claim.')
        return 'done'

    monkeypatch.setattr(core.jobs, '_graph', graph)
    if fails:
        with pytest.raises(RuntimeError):
            core.jobs.run('graph')
    else:
        assert core.jobs.run('graph') == 'done'
    assert held == [1]
    assert claim_rows(core, tokens[0]) == 0


def test_stop_waits_for_a_running_job(world_factory, core_factory, monkeypatch):
    core = core_factory(world_factory().root)
    started, finished = threading.Event(), threading.Event()

    def graph(options):
        started.set()
        time.sleep(1)
        finished.set()
        return {}

    monkeypatch.setattr(core.jobs, '_graph', graph)
    thread = threading.Thread(target=core.jobs.run, args=('graph',))
    thread.start()
    assert started.wait(10)
    core.jobs.stop()
    assert finished.is_set()
    thread.join(10)


def test_executor_process_group_is_recorded_while_its_call_runs(world_factory, core_factory,
                                                                tmp_path):
    root = world_factory().root
    core_factory(root)  # the state core records its executor groups under its own directory
    groups = root / '.state' / 'executors' / 'state'
    marker = tmp_path / 'executor.pid'
    argv = slow_executor(tmp_path, seconds=2)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pending = pool.submit(execute, argv, {}, timeout=30)
        pid = wait_for(lambda: marker.exists() and marker.read_text(), timeout=20,
                       what='the executor to start')
        # The record is named by the group id and holds the start time of the group's leader.
        record = wait_for(lambda: record_of(groups / pid), timeout=5,
                          what='the process group record')
        assert [entry.name for entry in groups.iterdir()] == [pid]
        assert record == {'pgid': int(pid), 'start': process_start(int(pid))}
        assert isinstance(record['start'], str) and record['start']
        assert not pending.done()
        assert pending.result(timeout=30) == {'done': True}
    finally:
        pool.shutdown(wait=True)
    assert list(groups.iterdir()) == []


def test_worker_pass_on_an_empty_vault_completes_and_is_recorded(world_factory, core_factory):
    core = core_factory(world_factory().root)
    assert core.storage.read(lambda reader: reader.queued()) == []
    result = core.jobs.run('worker')
    assert result['operations'] == 0
    assert result['resolutions'] == 0
    assert core.jobs.history[-1]['kind'] == 'worker'
    assert core.jobs.history[-1]['state'] == 'finished'
    assert core.jobs.history[-1]['result'] == result
