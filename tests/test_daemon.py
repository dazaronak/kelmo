import json
import fcntl
import sys
import time

import pytest

from test_fact_checks import content
from test_resolution import pending, resolved_changes
from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']


def executor(tmp_path, body):
    path = tmp_path / 'executor.py'
    path.write_text(body)
    return [sys.executable, str(path)]


def test_tick_calls_executor_only_for_pending_resolution(service, tmp_path):
    from kelmo.daemon import tick
    argv = executor(tmp_path, '''import json,sys
p=json.load(sys.stdin)
for c in p["changes"]:
    c["expected_revision"] = p["facts"].get(c["fact_key"], {}).get("revision")
    print(json.dumps({"changes":p["changes"]}))''')
    assert tick(service, argv)['resolutions'] == 0
    op = pending(service)
    result = tick(service, argv)
    assert result['resolutions'] == 1
    assert 'failed_attempts' not in result
    assert service.journal.get(op['id'])['state'] == 'applied'
    assert tick(service, argv)['resolutions'] == 0


def test_lost_operation_claim_defers_only_that_operation(service, monkeypatch):
    from kelmo.daemon import tick
    first = service.capture('First race evidence.', 'first-race.md', 'lead')
    second = service.capture('Second race evidence.', 'second-race.md', 'lead')
    run = service.run
    calls = []

    def raced(operation_id):
        calls.append(operation_id)
        if operation_id == first['id']:
            raise KelmoError('E_CLAIM', 'Another attempt owns this operation.')
        return run(operation_id)

    monkeypatch.setattr(service, 'run', raced)
    result = tick(service)
    assert calls == [first['id'], second['id']]
    assert result['operations'] == 1
    assert result['skipped'] == [{'operation_id': first['id'], 'error_code': 'E_CLAIM'}]
    assert service.journal.get(first['id'])['state'] == 'queued'
    assert service.journal.get(second['id'])['state'] == 'archived'


def test_a_stop_request_ends_the_pass_before_the_next_operation(service, monkeypatch):
    from kelmo.daemon import tick
    first = service.capture('First stop evidence.', 'first-stop.md', 'lead')
    second = service.capture('Second stop evidence.', 'second-stop.md', 'lead')
    run, calls = service.run, []
    monkeypatch.setattr(service, 'run', lambda operation_id, **kwargs: calls.append(operation_id)
                        or run(operation_id, **kwargs))
    assert tick(service, should_stop=lambda: bool(calls)) == {'operations': 1, 'resolutions': 0, 'stopped': True}
    assert calls == [first['id']]
    assert service.journal.get(second['id'])['state'] == 'queued'


def test_a_stopping_command_executor_makes_no_further_model_call(monkeypatch):
    from kelmo.creation import CommandExecutor
    from kelmo.errors import Stopping
    monkeypatch.setattr('kelmo.creation.execute',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('executor called')))
    with pytest.raises(Stopping):
        CommandExecutor([sys.executable], 120, should_stop=lambda: True).execute(None)


def test_executor_failure_backoff_and_exhaustion(service, tmp_path):
    from kelmo.daemon import tick
    argv = executor(tmp_path, 'raise SystemExit(1)')
    op = pending(service)
    first = tick(service, argv, max_attempts=2)
    row = service.journal.conn.execute('SELECT * FROM resolutions').fetchone()
    assert first['failed_attempts'] == [{
        'resolution_id': row['id'], 'operation_id': op['id'],
        'error_code': 'E_EXECUTOR', 'exhausted': False,
    }]
    assert row['state'] == 'pending'
    assert row['next_attempt'] > 0
    assert tick(service, argv, max_attempts=2)['resolutions'] == 0
    with service.journal.conn:
        service.journal.conn.execute('UPDATE resolutions SET next_attempt=0')
    exhausted = tick(service, argv, max_attempts=2)
    assert exhausted['failed_attempts'][0]['exhausted'] is True
    assert service.journal.get(op['id'])['state'] == 'needs_review'


def broken_wiki(service, monkeypatch):
    from kelmo.wiki import engine
    monkeypatch.setattr(engine, '_commit_registry',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('dirty wiki')))
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'broken.md', 'lead', 'promote')
    assert service.run(operation['id'])['state'] == 'failed'
    return operation


def test_exhausted_source_retries_are_settled_as_unresolved(service, monkeypatch):
    from kelmo.daemon import tick
    operation = broken_wiki(service, monkeypatch)
    tick(service, max_attempts=2)
    with service.journal.conn:
        service.journal.conn.execute('UPDATE worker_retries SET eligible=0')
    tick(service, max_attempts=2)
    # Each failed attempt records why it stopped; exhaustion replaces that with the final reason. A prepared
    # publication the recovery cannot finish stops every later one, so the reason names that note.
    outcome = service.journal.get(operation['id'])['outcome']
    assert outcome['disposition'] == 'unresolved', outcome
    assert outcome['reason'].startswith('E_PROCESSING: A prepared publication of note ' + operation['id']), outcome
    assert outcome['reason'].endswith('(RuntimeError): dirty wiki'), outcome
    settled = tick(service, max_attempts=2)
    assert settled['failed_attempts'] == [{'operation_id': operation['id'], 'error_code': 'E_ATTEMPTS',
                                           'attempts': 2, 'exhausted': True}]
    outcome = service.journal.get(operation['id'])['outcome']
    assert outcome['disposition'] == 'unresolved'
    assert outcome['reason'] == ('E_ATTEMPTS: the worker failed while processing this source '
                                 'for fixture.fact after 2 attempt(s)')
    assert service.status()['outcomes'] == {'applied': 0, 'archive-only': 0, 'unresolved': 1}
    again = tick(service, max_attempts=2)
    assert again['failed_attempts'] == settled['failed_attempts']
    assert service.status()['outcomes']['unresolved'] == 1


def test_timeout_and_invalid_json_are_bounded(tmp_path):
    from kelmo.daemon import execute
    for body in ('import time\ntime.sleep(10)', 'print("not JSON")'):
        with pytest.raises(KelmoError):
            execute(executor(tmp_path, body), {}, timeout=0.1)


def test_raw_output_invalid_utf8_is_executor_error(tmp_path):
    from kelmo.daemon import execute
    argv = executor(tmp_path, 'import os\nos.write(1,b"\\xff")')
    with pytest.raises(KelmoError) as raised:
        execute(argv, {}, raw_output=True)
    assert raised.value.code == 'E_EXECUTOR'


def test_invalid_proposal_calls_executor_again(service, tmp_path, monkeypatch):
    from kelmo.daemon import tick
    from kelmo.wiki import engine
    operation = pending(service)
    calls = []

    def answer(argv, payload, **kwargs):
        calls.append(True)
        changes = resolved_changes({'payload': payload})
        if len(calls) == 1:
            changes[0]['expected_revision'] = '0' * 64
        return {'changes': changes}

    monkeypatch.setattr('kelmo.daemon.execute', answer)
    tick(service, ['/synthetic/executor'])
    with service.journal.conn:
        service.journal.conn.execute('UPDATE resolutions SET next_attempt=0')
    tick(service, ['/synthetic/executor'])
    assert len(calls) == 2
    assert service.journal.get(operation['id'])['state'] == 'applied'
    assert engine.load_registry(service.root)['facts']['fixture.fact']['text'] == 'Second update.'


def test_wiki_runtime_error_is_bounded_and_does_not_stop_other_ticks(service, monkeypatch):
    from kelmo.daemon import tick
    from kelmo.wiki import engine
    operation = pending(service)
    commit = engine._commit_registry
    monkeypatch.setattr(engine, '_commit_registry', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('dirty wiki')))
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda argv, payload, **kwargs: {'changes': resolved_changes({'payload': payload})})
    assert tick(service, ['/synthetic/executor'], max_attempts=2)['resolutions'] == 1
    row = service.journal.conn.execute('SELECT * FROM resolutions').fetchone()
    assert row['state'] == 'pending'
    assert row['error'] == 'E_PROCESSING'
    with service.journal.conn:
        service.journal.conn.execute('UPDATE resolutions SET next_attempt=0')
    assert tick(service, ['/synthetic/executor'], max_attempts=2)['resolutions'] == 1
    assert service.journal.get(operation['id'])['state'] == 'needs_review'
    monkeypatch.setattr(engine, '_commit_registry', commit)


def test_audit_batch_of_five_overlapping_new_facts_all_completes(service, monkeypatch):
    from kelmo.daemon import tick
    operations = []
    for index in range(5):
        text = f'Synthetic independent fact number {index}.'
        receipt = service.reconcile(text, 'projects/fixture/fixture.md')
        operations.append(service.capture(content(
            {'result': 'not_found', 'search_id': receipt['search_id']},
            text=text, key=f'fixture.independent{index}'),
            f'independent{index}.md', 'lead', 'promote'))
    for operation in operations:
        service.run(operation['id'])
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda argv, payload, **kwargs: {'changes': payload['changes']})
    for _ in range(4):
        tick(service, ['/synthetic/executor'])
    states = [service.journal.get(operation['id'])['state'] for operation in operations]
    assert states == ['applied'] * 5
    assert service.status()['archive_records'] == 5


@pytest.mark.parametrize('config', [
    [],
    {'argv': 'not-a-list'},
    {'argv': []},
    {'argv': ['relative-executor']},
    {'argv': [sys.executable, 1]},
    {'poll_seconds': float('nan')},
    {'poll_seconds': float('inf')},
    {'poll_seconds': 0},
    {'timeout_seconds': float('nan')},
    {'timeout_seconds': float('inf')},
    {'timeout_seconds': 0},
    {'max_attempts': 0},
    {'max_attempts': 1.5},
    {'unexpected': True},
])
def test_invalid_daemon_config_is_rejected_before_claim(service, tmp_path, config):
    from kelmo.daemon import run
    pending(service)
    path = tmp_path / 'executor.json'
    path.write_text(json.dumps(config))
    with pytest.raises(KelmoError) as raised:
        run(service, path, once=True)
    assert raised.value.code == 'E_CONFIG'
    row = service.journal.conn.execute('SELECT state,attempts FROM resolutions').fetchone()
    assert dict(row) == {'state': 'pending', 'attempts': 0}


def test_daemon_without_executor_processes_operations_without_claiming_resolution(service, tmp_path):
    from kelmo.daemon import run
    operation = pending(service)
    path = tmp_path / 'executor.json'
    path.write_text(json.dumps({'argv': None, 'max_attempts': 3,
                                'timeout_seconds': 120, 'poll_seconds': 2}))
    assert run(service, path, once=True)['resolutions'] == 0
    assert service.journal.get(operation['id'])['state'] == 'needs_resolution'
    row = service.journal.conn.execute('SELECT state,attempts FROM resolutions').fetchone()
    assert dict(row) == {'state': 'pending', 'attempts': 0}


def test_a_busy_journal_does_not_stop_the_worker(service, tmp_path, monkeypatch):
    import sqlite3
    from kelmo import daemon
    path = tmp_path / 'executor.json'
    path.write_text(json.dumps({'argv': None, 'max_attempts': 3, 'timeout_seconds': 120, 'poll_seconds': 0.1}))
    calls = []

    def busy_then_stop(*args, should_stop, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise sqlite3.OperationalError('database is locked')
        raise SystemExit(0)

    monkeypatch.setattr(daemon, 'tick', busy_then_stop)
    with pytest.raises(SystemExit):
        daemon.run(service, path)
    assert len(calls) == 2, 'the worker took the next pass after a busy journal'
    monkeypatch.setattr(daemon, 'tick', lambda *args, **kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError('no such table: operations')))
    with pytest.raises(sqlite3.OperationalError):
        daemon.run(service, path, once=True)


def test_a_busy_database_during_a_retry_refunds_and_keeps_the_worker_up(service, monkeypatch):
    import sqlite3
    from kelmo.daemon import tick
    operation = service.capture('Busy retry database.', 'busy-db.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    monkeypatch.setattr(service, 'retry', lambda *args, **kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError('database is locked')))
    monkeypatch.setattr(service, 'queued', lambda: [])
    assert tick(service)['operations'] == 0
    retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
    assert dict(retry) == {'attempts': 1, 'eligible': 0.0}


def test_a_git_lock_left_by_a_crash_is_waited_on_and_named_by_doctor(tmp_path):
    import os
    import subprocess
    import time
    from kelmo.doctor import _stale_git_locks
    from kelmo.wiki import engine
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'page.md').write_text('text', encoding='utf-8')
    lock = tmp_path / '.git' / 'index.lock'
    lock.write_text('', encoding='utf-8')
    assert _stale_git_locks(tmp_path) == [], 'a fresh lock is a writer at work'
    old = time.time() - engine.STALE_GIT_LOCK_SECONDS - 60
    os.utime(lock, (old, old))
    with pytest.raises(KelmoError) as held:
        engine.git(tmp_path, 'add', 'page.md')
    assert held.value.code == 'E_BUSY'
    assert _stale_git_locks(tmp_path) == ['index.lock']


def test_a_lost_ref_race_is_contention(tmp_path, monkeypatch):
    import subprocess
    from kelmo.wiki import engine
    refused = subprocess.CompletedProcess([], 1, b'', b"error: cannot lock ref 'HEAD': is at 1 but expected 2\n")
    monkeypatch.setattr(engine.subprocess, 'run', lambda *args, **kwargs: refused)
    with pytest.raises(KelmoError) as raced:
        engine.git(tmp_path, 'update-ref', 'HEAD', 'x')
    assert raced.value.code == 'E_BUSY'


def test_retry_sweep_tolerates_another_worker_requeueing_first(service, monkeypatch):
    from kelmo.daemon import tick
    operation = service.capture('Retry race evidence.', 'race.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))

    def raced_retry(operation_id, *, reset_budget=True):
        service.journal.stage(operation_id, 'queued')
        raise KelmoError('E_STATE', 'Already retried by another worker.')

    monkeypatch.setattr(service, 'retry', raced_retry)
    monkeypatch.setattr(service, 'queued', lambda: [])
    assert tick(service)['operations'] == 0
    assert service.journal.get(operation['id'])['state'] == 'queued'
    retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
    assert dict(retry) == {'attempts': 1, 'eligible': 0.0}


def test_retry_sweep_skips_failed_operation_while_writer_is_busy(service, monkeypatch):
    from kelmo.daemon import tick
    operation = service.capture('Busy retry evidence.', 'busy-retry.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    monkeypatch.setattr(service, 'retry',
                        lambda *args, **kwargs: (_ for _ in ()).throw(KelmoError('E_BUSY', 'Writer busy.')))
    monkeypatch.setattr(service, 'queued', lambda: [])
    assert tick(service)['operations'] == 0
    assert service.journal.get(operation['id'])['state'] == 'failed'
    retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
    assert dict(retry) == {'attempts': 1, 'eligible': 0.0}


def test_operation_failed_on_a_held_lock_requeues_without_spending_its_budget(service, monkeypatch):
    from kelmo import daemon
    operation = service.capture('Held lock evidence.', 'held-lock.md', 'lead')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    requeued = []
    monkeypatch.setattr(service, 'retry', lambda operation_id, *, reset_budget=True: requeued.append(operation_id))
    for _ in range(5):
        service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_BUSY')
        state, attempts = daemon.charge_retry(service, service.journal.get(operation['id']), max_attempts=3)
        assert (state, attempts) == ('requeued', 1)
        retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
        assert retry['attempts'] == 1 and retry['eligible'] > time.time() + daemon.BUSY_RETRY_SECONDS - 5
        assert daemon.charge_retry(service, service.journal.get(operation['id']))[0] == 'waiting'
        with service.journal.conn:
            service.journal.conn.execute('UPDATE worker_retries SET eligible=0')
    assert requeued == [operation['id']] * 5
    from kelmo import outcomes
    assert outcomes.get(service.journal.conn, operation['id']) is None


def test_a_claim_held_by_another_attempt_refunds_the_retry_instead_of_crashing(service, monkeypatch):
    from kelmo.daemon import tick
    operation = service.capture('Claimed retry evidence.', 'claimed-retry.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    monkeypatch.setattr(service, 'retry',
                        lambda *args, **kwargs: (_ for _ in ()).throw(KelmoError('E_CLAIM', 'Claimed elsewhere.')))
    monkeypatch.setattr(service, 'queued', lambda: [])
    assert tick(service)['operations'] == 0
    retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
    assert dict(retry) == {'attempts': 1, 'eligible': 0.0}


def test_a_held_git_lock_is_contention_not_a_git_failure(tmp_path):
    import subprocess
    from kelmo.wiki import engine
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'page.md').write_text('text', encoding='utf-8')
    (tmp_path / '.git' / 'index.lock').write_text('', encoding='utf-8')
    with pytest.raises(KelmoError) as held:
        engine.git(tmp_path, 'add', 'page.md')
    assert held.value.code == 'E_BUSY'
    (tmp_path / '.git' / 'index.lock').unlink()
    with pytest.raises(KelmoError) as failed:
        engine.git(tmp_path, 'rev-parse', '--verify', 'no-such-ref')
    assert failed.value.code == 'E_GIT'


def test_retry_refund_does_not_overwrite_concurrent_budget_change(service, monkeypatch):
    from kelmo.daemon import tick
    operation = service.capture('Concurrent budget evidence.', 'concurrent-budget.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))

    def changed_budget(*args, **kwargs):
        with service.journal.conn:
            service.journal.conn.execute('UPDATE worker_retries SET attempts=7,eligible=77')
        raise KelmoError('E_BUSY', 'Writer busy.')

    monkeypatch.setattr(service, 'retry', changed_budget)
    monkeypatch.setattr(service, 'queued', lambda: [])
    tick(service, max_attempts=10)
    retry = service.journal.conn.execute('SELECT attempts,eligible FROM worker_retries').fetchone()
    assert dict(retry) == {'attempts': 7, 'eligible': 77.0}


def test_retry_budget_is_charged_before_requeue_crash_and_survives_reopen(service, monkeypatch):
    from kelmo.daemon import tick
    from kelmo.service import Service
    operation = service.capture('Crash retry evidence.', 'crash-retry.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    retry = service.retry

    def crash_after_requeue(operation_id, *, reset_budget=True):
        retry(operation_id, reset_budget=reset_budget)
        raise SystemExit(91)

    monkeypatch.setattr(service, 'retry', crash_after_requeue)
    with pytest.raises(SystemExit):
        tick(service)
    assert service.journal.get(operation['id'])['state'] == 'queued'
    with Service(service.root) as reopened:
        row = reopened.journal.conn.execute(
            'SELECT attempts,eligible FROM worker_retries WHERE operation_id=?',
            (operation['id'],)).fetchone()
        assert row['attempts'] == 2
        assert row['eligible'] > 0


def test_daemon_retry_does_not_reset_lifetime_budget(service, monkeypatch):
    from kelmo.daemon import tick
    operation = service.capture('Retry budget evidence.', 'budget.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    with service.journal.conn:
        service.journal.conn.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL)''')
        service.journal.conn.execute('INSERT INTO worker_retries VALUES (?,1,0)', (operation['id'],))
    calls = []

    def retry(operation_id, *, reset_budget=True):
        calls.append(reset_budget)
        service.journal.stage(operation_id, 'queued')
        return service.journal.get(operation_id)

    monkeypatch.setattr(service, 'retry', retry)
    monkeypatch.setattr(service, 'queued', lambda: [])
    tick(service)
    assert calls == [False]


def test_git_failure_retries_but_git_identity_failure_remains_manual(service, monkeypatch):
    from kelmo.daemon import tick
    transient = service.capture('Git retry evidence.', 'git-retry.md', 'lead')
    identity = service.capture('Git identity evidence.', 'git-identity.md', 'lead')
    service.journal.stage(transient['id'], 'failed', terminal=True, error_code='E_GIT')
    service.journal.stage(identity['id'], 'failed', terminal=True, error_code='E_GIT_IDENTITY')
    retried = []

    def retry(operation_id, *, reset_budget=True):
        retried.append(operation_id)
        service.journal.stage(operation_id, 'queued')
        return service.journal.get(operation_id)

    monkeypatch.setattr(service, 'retry', retry)
    monkeypatch.setattr(service, 'queued', lambda: [])
    tick(service)
    with service.journal.conn:
        service.journal.conn.execute('UPDATE worker_retries SET eligible=0')
    tick(service)
    assert retried == [transient['id']]
    assert service.journal.get(identity['id'])['state'] == 'failed'


def test_a_transient_failure_resumes_under_the_daemon_budget(service):
    """The recovery a shared write retried under its key uses: the daemon's charge, backoff and ceiling."""
    from kelmo.daemon import resume_transient
    from kelmo.server import _retryable
    connection = service.journal.conn
    operation = service.capture('Transient archive process death.', 'transient.md', 'lead')
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_ARCHIVE_PROCESS')
    failed = service.journal.get(operation['id'])
    with pytest.raises(KelmoError) as waiting:
        resume_transient(service, failed)
    assert waiting.value.code == 'E_BUSY' and _retryable(waiting.value) is True
    with connection:
        connection.execute('UPDATE worker_retries SET eligible=0 WHERE operation_id=?', (operation['id'],))
    assert resume_transient(service, failed)['state'] == 'archived'
    charged = connection.execute('SELECT attempts FROM worker_retries WHERE operation_id=?',
                                 (operation['id'],)).fetchone()
    assert charged['attempts'] == 2

    spent = service.capture('Budget already spent.', 'spent.md', 'lead')
    service.journal.stage(spent['id'], 'failed', terminal=True, error_code='E_IO')
    with connection:
        connection.execute('INSERT INTO worker_retries VALUES (?,3,0)', (spent['id'],))
    with pytest.raises(KelmoError) as exhausted:
        resume_transient(service, service.journal.get(spent['id']))
    assert exhausted.value.code == 'E_ATTEMPTS' and _retryable(exhausted.value) is False

    final = service.capture('A definitive refusal.', 'final.md', 'lead')
    service.journal.stage(final['id'], 'failed', terminal=True, error_code='E_SOURCE')
    settled = service.journal.get(final['id'])
    assert resume_transient(service, settled) == settled, 'a definitive failure is never resumed'


def test_a_stage_transition_runs_the_writer_fence_inside_its_transaction(service):
    operation = service.capture('Fenced stage.', 'fenced-stage.md', 'lead')
    assert service.journal.writer_check == service.archive_api.assert_writer

    def stale_writer():
        raise KelmoError('E_WRITER', 'Synthetic stale writer epoch.')

    service.journal.writer_check = stale_writer
    with pytest.raises(KelmoError) as error:
        service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_IO')
    assert error.value.code == 'E_WRITER'
    assert service.journal.get(operation['id'])['state'] == 'queued', 'nothing moved under a stale writer'


def test_busy_writer_does_not_consume_resolution_attempt(service, monkeypatch):
    from kelmo.daemon import tick
    operation = pending(service)
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda argv, payload, **kwargs: {'changes': resolved_changes({'payload': payload})})
    lock_path = service.config.internal('.state/worker.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tick(service, ['/synthetic/executor'])
    row = service.journal.conn.execute('SELECT state,attempts,error FROM resolutions').fetchone()
    assert dict(row) == {'state': 'pending', 'attempts': 0, 'error': 'E_BUSY'}
    assert service.journal.get(operation['id'])['state'] == 'needs_resolution'


def test_execute_checks_final_stderr_size(tmp_path):
    from kelmo.daemon import execute
    argv = executor(tmp_path, 'import os\nos.write(2, b"x" * 1048577)')
    with pytest.raises(KelmoError) as raised:
        execute(argv, {})
    assert raised.value.code == 'E_EXECUTOR'


@pytest.mark.parametrize('stream', [1, 2])
def test_execute_bounds_high_speed_output_per_stream(tmp_path, stream):
    from kelmo.daemon import execute
    argv = executor(tmp_path, f'import os\nos.write({stream}, b"x" * 2097152)')
    with pytest.raises(KelmoError) as raised:
        execute(argv, {}, timeout=2)
    assert raised.value.code == 'E_EXECUTOR'


def test_execute_drains_stdout_and_stderr_without_deadlock(tmp_path):
    from kelmo.daemon import execute
    argv = executor(tmp_path, '''import json,os,threading
def noise():
    os.write(2, b"x" * 900000)
t = threading.Thread(target=noise)
t.start()
os.write(1, json.dumps({"ok": True}).encode())
t.join()''')
    assert execute(argv, {}, timeout=2) == {'ok': True}


def test_execute_uses_pipes_for_bounded_output(tmp_path, monkeypatch):
    import subprocess
    from kelmo.daemon import execute
    popen = subprocess.Popen
    seen = {}

    def inspect(*args, **kwargs):
        seen.update(kwargs)
        return popen(*args, **kwargs)

    monkeypatch.setattr('kelmo.daemon.subprocess.Popen', inspect)
    argv = executor(tmp_path, 'print("{}")')
    assert execute(argv, {}) == {}
    assert seen['stdout'] == subprocess.PIPE
    assert seen['stderr'] == subprocess.PIPE


@pytest.mark.parametrize('kind', ['missing', 'not-executable'])
def test_execute_normalizes_launch_failure(tmp_path, kind):
    from kelmo.daemon import execute
    path = tmp_path / 'executor'
    if kind == 'not-executable':
        path.write_text('#!/bin/sh\n')
        path.chmod(0o600)
    with pytest.raises(KelmoError) as raised:
        execute([str(path)], {})
    assert raised.value.code == 'E_EXECUTOR'
    assert str(path) not in str(raised.value)


def test_execute_timeout_cleans_up_child_process_group(tmp_path):
    import os
    from kelmo.daemon import execute
    pid_file = tmp_path / 'child.pid'
    survived = tmp_path / 'child-survived.txt'
    # The grandchild proves it survived by writing a file: a killed one can stay a zombie until init reaps it,
    # and signal 0 still reaches a zombie.
    child = f"import pathlib,time;time.sleep(1.5);pathlib.Path({str(survived)!r}).write_text('alive')"
    body = ("import subprocess,sys,time\n"
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
            f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
            "time.sleep(30)\n")
    with pytest.raises(KelmoError):
        execute(executor(tmp_path, body), {}, timeout=1.0)
    try:
        assert pid_file.exists(), 'the grandchild was never started'
        __import__('time').sleep(2)
        assert not survived.exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), __import__('signal').SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_a_termination_during_the_spawn_still_kills_the_executor_group(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import time
    from kelmo.daemon import execute
    from kelmo.executor import termination_forwarding
    survived = tmp_path / 'survived.txt'
    body = f"import pathlib,time;time.sleep(1.0);pathlib.Path({str(survived)!r}).write_text('alive')\n"
    real_popen = subprocess.Popen

    def popen_then_terminate(*args, **kwargs):
        # The executor is running, but the spawn has not returned yet: the window a terminated parent hits when
        # it is slow to read the exec result.
        process = real_popen(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.1)
        return process

    monkeypatch.setattr(subprocess, 'Popen', popen_then_terminate)
    with pytest.raises(KelmoError) as raised:
        with termination_forwarding():
            execute(executor(tmp_path, body), {}, timeout=10)
    assert raised.value.code == 'E_EXECUTOR'
    time.sleep(1.5)
    assert not survived.exists()


def test_a_member_left_in_the_group_after_its_leader_is_reaped_is_killed(tmp_path):
    import os
    import subprocess
    from kelmo.daemon import _empty_group
    pid_file = tmp_path / 'member.pid'
    leader = subprocess.Popen(
        [sys.executable, '-c',
         "import subprocess,sys,time\n"
         "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
         f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
         "time.sleep(30)\n"],
        start_new_session=True)
    try:
        end = time.monotonic() + 10
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < end, 'the member was never started'
            time.sleep(0.02)
        member = int(pid_file.read_text())
        leader.kill()
        leader.wait()
        os.kill(member, 0)  # still running: only the leader was killed
        _empty_group(leader.pid)
        end = time.monotonic() + 5
        while True:
            try:
                os.killpg(leader.pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < end, 'the group still has a member'
            time.sleep(0.02)
    finally:
        try:
            os.killpg(leader.pid, __import__('signal').SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("successful", [False, True])
def test_execute_cleans_group_after_parent_exits(tmp_path, successful):
    import os
    from kelmo.daemon import execute
    pid_file = tmp_path / 'orphan-child.pid'
    survived = tmp_path / 'child-survived.txt'
    child = f"import pathlib,time;time.sleep(0.8);pathlib.Path({str(survived)!r}).write_text('alive')"
    streams = ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL" if successful else ""
    body = ("import subprocess,sys\n"
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]{streams})\n"
            f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
            "print('{}')\n")
    if successful:
        assert execute(executor(tmp_path, body), {}, timeout=0.2) == {}
    else:
        with pytest.raises(KelmoError) as raised:
            execute(executor(tmp_path, body), {}, timeout=0.2)
        assert raised.value.code == 'E_EXECUTOR'
    child_pid = int(pid_file.read_text())
    try:
        __import__('time').sleep(1)
        assert not survived.exists()
    finally:
        try:
            os.kill(child_pid, __import__('signal').SIGKILL)
        except ProcessLookupError:
            pass


def test_execute_timeout_includes_wait_after_child_closes_pipes(tmp_path):
    from kelmo.daemon import execute
    argv = executor(tmp_path, 'import os,time\nos.close(1)\nos.close(2)\ntime.sleep(1)')
    started = __import__('time').monotonic()
    with pytest.raises(KelmoError) as raised:
        execute(argv, {}, timeout=0.2)
    assert raised.value.code == 'E_EXECUTOR'
    assert __import__('time').monotonic() - started < 0.5


def test_sigterm_interrupts_idle_poll_wait(tmp_path):
    import os
    import subprocess
    import time
    script = ("import kelmo.daemon as daemon\n"
              "daemon.tick=lambda *a,**k: (print('ready',flush=True) or {})\n"
              "daemon.run(None,None)\n")
    process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        started = time.monotonic()
        os.kill(process.pid, __import__('signal').SIGTERM)
        assert process.wait(timeout=2) == 0
        assert time.monotonic() - started < 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_a_ref_name_conflict_stays_a_git_failure(tmp_path, monkeypatch):
    import subprocess
    from kelmo.wiki import engine
    refused = subprocess.CompletedProcess([], 1, b'', b"error: cannot lock ref 'refs/heads/a/b': "
                                                     b"'refs/heads/a' exists; cannot create 'refs/heads/a/b'\n")
    monkeypatch.setattr(engine.subprocess, 'run', lambda *args, **kwargs: refused)
    with pytest.raises(KelmoError) as failed:
        engine.git(tmp_path, 'update-ref', 'refs/heads/a/b', 'x')
    assert failed.value.code == 'E_GIT'
