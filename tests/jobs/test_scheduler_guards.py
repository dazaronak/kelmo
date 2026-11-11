"""Scheduler eligibility, durable apply back-off and fencing."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core.jobs import APPLY_MIN_INTERVAL_SECONDS, APPLY_NO_PROGRESS_SECONDS, Jobs
from kelmo.errors import KelmoError


def scheduler(tmp_path, now, operations):
    (tmp_path / '.state').mkdir(exist_ok=True)
    def execute(_sql):
        rows = [(op['id'], op['state']) for op in operations if op['intent'] == 'create'
                and not op['terminal'] and op['state'] in ('archived', 'parked')]
        return SimpleNamespace(fetchall=lambda: rows)

    reader = SimpleNamespace(journal=SimpleNamespace(conn=SimpleNamespace(execute=execute)))
    writer = SimpleNamespace(config=SimpleNamespace(embedding={}))
    storage = SimpleNamespace(writer=writer, read=lambda fn: fn(reader))
    core = SimpleNamespace(root=tmp_path, storage=storage, profile=None, profile_path=None)
    return Jobs(core, clock=lambda: now[0])


def note(identifier, state='archived', **changes):
    return {'id': identifier, 'state': state, 'intent': 'create', 'terminal': False, **changes}


def test_only_first_pass_create_notes_count(tmp_path):
    operations = [note('first'), note('parked', 'parked'), note('other', intent='update'),
                  note('done', terminal=True)]
    assert scheduler(tmp_path, [0], operations)._waiting_notes() == 1


@pytest.mark.parametrize('state,reason', [({'paused': True}, 'paused'),
                                         ({'paused': False, 'next_attempt': 105}, 'deferred')])
def test_dreaming_paused_or_deferred_is_held(tmp_path, monkeypatch, state, reason):
    from kelmo import dreaming_cli
    jobs = scheduler(tmp_path, [100], [])
    monkeypatch.setattr(dreaming_cli, 'status', lambda root: state)
    assert jobs._dream_hold()[0] == reason
    assert jobs._dream_due() is None


def test_no_progress_survives_restart_and_min_interval_uses_end(tmp_path):
    now = [100]
    notes = [note('a')]
    jobs = scheduler(tmp_path, now, notes)
    jobs._record_apply_start()
    notes[0]['state'] = 'parked'
    now[0] += 50
    jobs._record_apply_end()
    restarted = scheduler(tmp_path, now, notes)
    assert restarted._apply_hold() == ('no_progress', 100 + APPLY_NO_PROGRESS_SECONDS)
    now[0] = 100 + APPLY_NO_PROGRESS_SECONDS
    assert restarted._apply_hold() == (None, None)
    notes[0]['state'] = 'archived'
    restarted._record_apply_start()
    notes.clear()
    now[0] += 10
    restarted._record_apply_end()
    assert restarted._apply_hold() == ('min_interval', now[0] + APPLY_MIN_INTERVAL_SECONDS)


def test_fence_holds_every_job_and_resume(tmp_path, monkeypatch):
    now = [1000]
    jobs = scheduler(tmp_path, now, [])
    jobs.worker_config = tmp_path / 'executor.json'
    jobs.core.profile = SimpleNamespace(fenced=True)
    started = []
    monkeypatch.setattr(jobs, '_dream_due', lambda: 'apply')
    monkeypatch.setattr(jobs, '_background', lambda kind, options: started.append(kind) or True)
    now[0] = jobs._backfill_due
    jobs._tick()
    assert started == []
    assert jobs.status()['schedule']['dream']['held'] == 'fenced'
    jobs.core.profile.fenced = False
    jobs._tick()
    assert started == ['worker', 'dream', 'backfill']


def test_daily_run_records_day_only_after_start(tmp_path, monkeypatch):
    from kelmo import dreaming_cli
    jobs = scheduler(tmp_path, [100], [])
    monkeypatch.setattr(jobs, 'step', nullcontext)
    monkeypatch.setattr(dreaming_cli, 'run_dream', lambda *args, **kwargs: {'state': 'paused'})
    assert jobs._dream({'scheduled': True, 'action': 'run'})['state'] == 'paused'
    assert jobs._last_dream_day() is None

    def failed(*args, **kwargs):
        kwargs['on_start']()
        raise RuntimeError('failure after start')

    monkeypatch.setattr(dreaming_cli, 'run_dream', failed)
    with pytest.raises(RuntimeError):
        jobs._dream({'scheduled': True, 'action': 'run'})
    assert jobs._last_dream_day() == jobs._today()


def test_failed_backfill_is_logged_and_retried(tmp_path, monkeypatch, capsys):
    now = [100]
    jobs = scheduler(tmp_path, now, [])
    monkeypatch.setattr(jobs, 'run', lambda *args: (_ for _ in ()).throw(KelmoError('E_FAILED', 'failure')))
    jobs._quietly('backfill', {'scheduled': True})
    assert 'scheduled backfill job failed: E_FAILED' in capsys.readouterr().err
    assert jobs._backfill_due == now[0] + 600
    started = []
    monkeypatch.setattr(jobs, '_dream_due', lambda: None)
    monkeypatch.setattr(jobs, '_background', lambda kind, options: started.append(kind) or True)
    now[0] = jobs._backfill_due - 1
    jobs._tick()
    assert not started
    now[0] += 1
    jobs._tick()
    assert started == ['backfill']


def test_fence_reads_current_profile_each_time(tmp_path, monkeypatch):
    from kelmo.server import ServerProfile

    jobs = scheduler(tmp_path, [100], [])
    jobs.core.profile_path = tmp_path / 'profile.json'
    fenced = [True]
    monkeypatch.setattr(ServerProfile, 'load', lambda path: SimpleNamespace(fenced=fenced[0]))
    assert jobs._fenced() is True
    fenced[0] = False
    assert jobs._fenced() is False


def test_unreadable_profile_holds_scheduled_work(tmp_path, monkeypatch):
    from kelmo.server import ServerProfile

    def unreadable(path):
        raise PermissionError('synthetic')

    jobs = scheduler(tmp_path, [100], [])
    jobs.core.profile_path = tmp_path / 'profile.json'
    monkeypatch.setattr(ServerProfile, 'load', unreadable)
    assert jobs._fenced() is True


def test_scheduler_loop_survives_a_failing_tick(tmp_path, monkeypatch, capsys):
    jobs = scheduler(tmp_path, [100], [])
    ticks = []

    def tick():
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise OSError('synthetic')
        jobs._stop.set()

    monkeypatch.setattr(jobs, '_tick', tick)
    jobs.poll_seconds = 0.01
    jobs._loop()
    assert len(ticks) == 2
    assert 'scheduler tick failed: OSError' in capsys.readouterr().err


@pytest.mark.parametrize('scheduled,steps,stopped', [(True, 1, 'fenced'), (False, 2, None)])
def test_fence_raised_during_backfill_stops_only_the_scheduled_job(tmp_path, monkeypatch, scheduled, steps, stopped):
    jobs = scheduler(tmp_path, [100], [])
    jobs.core.profile = SimpleNamespace(fenced=False)
    calls = []

    class Admin:
        def backfill(self, **options):
            calls.append(options)
            jobs.core.profile.fenced = True
            return {'written': 1, 'cursor': '5' if len(calls) == 1 else None, 'seconds': 0}

    monkeypatch.setattr(jobs, '_admin', lambda: lambda verb: Admin())
    result = jobs._backfill({'scheduled': scheduled})
    assert len(calls) == steps
    assert result['written'] == steps and result['stopped'] == stopped


def test_session_notes_of_a_server_vault_go_through_the_writer_for_the_capturing_client(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from kelmo import daemon

    jobs = scheduler(tmp_path, [100], [])
    assert jobs._session_service() is None  # a local vault archives its own session notes

    acted = []

    @contextmanager
    def requested_by(origin):
        acted.append(origin)
        yield jobs.storage.writer

    def origin(client_id):
        if client_id != 'mac-a':
            raise ValueError('not enrolled')
        return ('origin', client_id)

    jobs.storage.writer.requested_by = requested_by
    jobs.core.profile = SimpleNamespace(fenced=False, bindings=SimpleNamespace(origin=origin))
    acting = jobs._session_service()
    with acting('mac-a') as service:
        assert service is jobs.storage.writer and acted == [('origin', 'mac-a')]
    with pytest.raises(KelmoError) as error:
        with acting('mac-gone'):
            pass
    assert error.value.code == 'E_ORIGIN'

    passed = {}
    monkeypatch.setattr(daemon, 'tick', lambda service, argv, **options: passed.update(options) or {})
    monkeypatch.setattr(jobs, '_executor', lambda: {'argv': None, 'max_attempts': 3, 'timeout_seconds': 120})
    jobs._worker({})
    assert callable(passed['open_service'])

    # Enrollment is read from the profile file at every note: a client removed since the daemon started gets
    # no more notes filed for it.
    from kelmo import server

    def removed(client_id):
        raise ValueError('not enrolled')

    jobs.core.profile_path = tmp_path / 'server.json'
    monkeypatch.setattr(server.ServerProfile, 'load',
                        classmethod(lambda cls, path: SimpleNamespace(bindings=SimpleNamespace(origin=removed))))
    with pytest.raises(KelmoError) as error:
        with jobs._session_service()('mac-a'):
            pass
    assert error.value.code == 'E_ORIGIN'


def test_manual_apply_is_not_held_by_scheduled_backoff(tmp_path, monkeypatch):
    from kelmo import dreaming_cli

    now = [100]
    jobs = scheduler(tmp_path, now, [note('a')])
    jobs._record_apply_start()
    jobs._record_apply_end()
    assert jobs._apply_hold()[0] == 'no_progress'
    monkeypatch.setattr(jobs, 'step', nullcontext)
    monkeypatch.setattr(dreaming_cli, 'run_dream', lambda *args, **kwargs: {'state': 'finished'})
    assert jobs.run('dream', {'action': 'apply'}) == {'state': 'finished'}


def test_busy_daily_job_does_not_take_day(tmp_path):
    jobs = scheduler(tmp_path, [100], [])
    with jobs._running['dream']:
        with pytest.raises(KelmoError) as error:
            jobs.run('dream', {'action': 'run', 'scheduled': True})
    assert error.value.code == 'E_BUSY'
    assert jobs._last_dream_day() is None


def test_daemon_status_includes_scheduler_status(monkeypatch):
    from kelmo.core import daemon_admin

    calls = []

    def call(root, operation, body, **options):
        calls.append(operation)
        if operation == 'daemon.status':
            return {'cores': [{'name': 'state'}]}
        return {'schedule': {'dream': {'held': 'paused'}}}

    monkeypatch.setattr(daemon_admin.daemon_client, 'call', call)
    result = daemon_admin.status('/synthetic')
    assert calls == ['daemon.status', 'state.scheduler_status']
    assert result['jobs']['schedule']['dream']['held'] == 'paused'


@pytest.mark.parametrize('published,reason,interval', [
    (True, 'min_interval', APPLY_MIN_INTERVAL_SECONDS),
    (False, 'no_progress', APPLY_NO_PROGRESS_SECONDS),
])
def test_unfinished_apply_uses_start_for_backoff_after_restart(tmp_path, published, reason, interval):
    now = [100]
    notes = [note('a'), note('b')]
    first = scheduler(tmp_path, now, notes)
    first._record_apply_start()
    if published:
        notes.pop()
    restarted = scheduler(tmp_path, now, notes)
    assert restarted._apply_hold() == (reason, 100 + interval)
    now[0] = 100 + interval - 1
    assert restarted._apply_hold() == (reason, 100 + interval)
    now[0] += 1
    assert restarted._apply_hold() == (None, None)


def test_queue_notes_uses_only_waiting_create_operations_in_real_journal(world_factory, core_factory, monkeypatch):
    from kelmo.service import Service

    core = core_factory(world_factory().root)
    rows = [('first', 'create', 'archived', 0), ('parked', 'create', 'parked', 0),
            ('terminal', 'create', 'archived', 1), ('failed', 'create', 'failed', 0),
            ('other-intent', 'update', 'archived', 0), ('other-state', 'create', 'captured', 0)]
    with core.storage.baton.hold(), core.storage.writer.journal.conn as connection:
        connection.executemany(
            'INSERT INTO operations (id, request_key, source_hash, source_path, filename, role, intent, '
            'state, terminal, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [(key, key, 'synthetic', 'synthetic', 'synthetic.md', 'local', intent, state, terminal,
              '2026-01-01T00:00:00Z') for key, intent, state, terminal in rows])
    monkeypatch.setattr(Service, 'queue', lambda self: pytest.fail('scheduler must not load the full queue'))
    notes = Jobs(core, clock=lambda: 100)._queue_notes()
    assert notes == {'archived': {'first'}, 'parked': {'parked'}}


def test_last_failure_persists_and_success_keeps_failure(tmp_path, monkeypatch):
    now = [100]
    jobs = scheduler(tmp_path, now, [])
    apply = {'started': 80, 'ids': ['a'], 'finished': 90, 'no_progress': False}
    jobs._write_state(dream_day='2026-01-01', apply=apply)

    def fail(options):
        raise KelmoError('E_SYNTHETIC', 'synthetic failure')

    monkeypatch.setattr(jobs, '_graph', fail)
    now[0] = 110
    with pytest.raises(KelmoError):
        jobs.run('graph')
    state = json.loads((tmp_path / '.state' / 'jobs.json').read_text())
    failure = state['last_failure']['graph']
    assert failure['seq'] == 1
    assert failure['ended_at'] == 110
    assert failure['error_code'] == 'E_SYNTHETIC'
    assert isinstance(failure['seconds'], (int, float)) and failure['seconds'] >= 0
    assert state['dream_day'] == '2026-01-01' and state['apply'] == apply

    restarted = scheduler(tmp_path, now, [])
    assert restarted.status()['last_failure']['graph'] == failure
    monkeypatch.setattr(restarted, '_graph', lambda options: {'state': 'finished'})
    assert restarted.run('graph') == {'state': 'finished'}
    status = restarted.status()
    assert status['last_failure']['graph'] == failure
    assert status['last']['graph']['result_state'] == 'finished'
    assert json.loads((tmp_path / '.state' / 'jobs.json').read_text())['last_failure']['graph'] == failure


def test_concurrent_state_updates_keep_both_keys(tmp_path):
    jobs = scheduler(tmp_path, [100], [])
    gate = threading.Barrier(2)

    def write(key):
        gate.wait()
        for value in range(200):
            jobs._write_state(**{key: value})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, key) for key in ('first', 'second')]
        for future in futures:
            future.result()
    state = json.loads((tmp_path / '.state' / 'jobs.json').read_text())
    assert state['first'] == state['second'] == 199
