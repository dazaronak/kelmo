"""A provider quota answer waits for its reset time and spends no attempt."""
import sys
import time

import pytest
from unittest.mock import patch

from test_resolution import pending
from kelmo.errors import KelmoError
from kelmo.quota import QuotaWait, checked, read

pytest_plugins = ['test_fact_checks']


def executor(tmp_path, body):
    path = tmp_path / 'executor.py'
    path.write_text(body)
    return [sys.executable, str(path)]


def quota_script(reset_at):
    return '''import json,sys,time
print(json.dumps({"outcome":"quota_wait","reset_at":''' + repr(reset_at) + '''}))'''


def row_for(service, operation_id):
    from kelmo.resolution import Resolution
    return [item for item in Resolution(service).list() if item['operation_id'] == operation_id][0]


def test_a_quota_answer_is_read_as_a_reset_time():
    reset_at = time.time() + 30
    assert read({'outcome': 'quota_wait', 'reset_at': reset_at}) == pytest.approx(reset_at)
    assert read({'changes': []}) is None
    with pytest.raises(QuotaWait) as raised:
        checked({'outcome': 'quota_wait', 'reset_at': reset_at})
    assert raised.value.code == 'E_QUOTA'
    assert raised.value.reset_at == pytest.approx(reset_at)
    assert 'no attempt is charged' in str(raised.value)


@pytest.mark.parametrize('answer', [
    {'outcome': 'quota_wait'},
    {'outcome': 'quota_wait', 'reset_at': 'tomorrow'},
    {'outcome': 'quota_wait', 'reset_at': time.time() + 90000},
    {'outcome': 'quota_wait', 'reset_at': time.time() - 3600},
    {'outcome': 'quota_wait', 'reset_at': time.time() + 30, 'note': 'extra'},
])
def test_a_quota_answer_that_cannot_be_scheduled_is_an_executor_failure(answer):
    with pytest.raises(KelmoError) as raised:
        checked(answer)
    assert raised.value.code == 'E_EXECUTOR'


def test_a_quota_wait_returns_the_request_to_pending_without_an_attempt(service):
    operation = pending(service)
    from kelmo.resolution import Resolution
    queue = Resolution(service)
    request = queue.claim()
    reset_at = time.time() + 30
    assert queue.quota_wait(request['id'], request['token'], reset_at) is True
    row = row_for(service, operation['id'])
    assert row['state'] == 'pending'
    assert row['attempts'] == 0
    assert row['error'] == 'E_QUOTA'
    assert row['next_attempt'] == pytest.approx(reset_at)
    settled = service.journal.get(operation['id'])
    assert settled['state'] == 'needs_resolution'
    assert settled['outcome']['reason'].startswith('E_FACT_CHECK:')


def test_a_quota_answer_is_never_taken_as_a_proposal(service):
    operation = pending(service)
    from kelmo.resolution import Resolution
    queue = Resolution(service)
    request = queue.claim()
    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'outcome': 'quota_wait', 'reset_at': time.time() + 30})
    assert raised.value.code == 'E_QUOTA'
    assert row_for(service, operation['id'])['state'] == 'claimed'


def test_a_quota_answer_in_the_worker_tick_waits_instead_of_failing(service, tmp_path):
    from kelmo.daemon import tick
    operation = pending(service)
    result = tick(service, executor(tmp_path, quota_script(time.time() + 30)))
    assert result['resolutions'] == 1
    assert 'failed_attempts' not in result
    wait = result['quota_waits'][0]
    assert wait['operation_id'] == operation['id']
    assert wait['reset_at'] > time.time()
    row = row_for(service, operation['id'])
    assert row['state'] == 'pending' and row['attempts'] == 0 and row['error'] == 'E_QUOTA'
    assert service.journal.get(operation['id'])['outcome']['reason'].startswith('E_FACT_CHECK:')
    assert tick(service, executor(tmp_path, quota_script(time.time() + 30)))['resolutions'] == 0


def test_a_quota_wait_leaves_the_whole_failure_budget_for_real_attempts(service, tmp_path):
    from kelmo.daemon import tick
    operation = pending(service)
    tick(service, executor(tmp_path, quota_script(time.time() - 1)), max_attempts=2)
    row = row_for(service, operation['id'])
    assert row['attempts'] == 0 and row['state'] == 'pending' and row['error'] == 'E_QUOTA'
    broken = executor(tmp_path, 'raise SystemExit(1)')
    assert tick(service, broken, max_attempts=2)['failed_attempts'][0]['exhausted'] is False
    with service.journal.conn:
        service.journal.conn.execute('UPDATE resolutions SET next_attempt=0')
    assert tick(service, broken, max_attempts=2)['failed_attempts'][0]['exhausted'] is True
    assert service.journal.get(operation['id'])['state'] == 'needs_review'


def test_a_session_job_that_hits_quota_keeps_its_attempts(tmp_path):
    from kelmo.session_worker import tick
    from test_session_worker import prepared

    def rate_limited(argv, payload, **kwargs):
        return {'outcome': 'quota_wait', 'reset_at': time.time() - 1}

    def broken(argv, payload, **kwargs):
        raise KelmoError('E_EXTRACTION', 'The extraction never arrived.')

    root, job_id = prepared(tmp_path)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        job = tick(root, ['/synthetic/executor'], execute=rate_limited)['jobs'][0]
        assert job['state'] == 'pending'
        assert job['error_code'] == 'E_QUOTA'
        assert job['attempts'] == 0
        assert job['quota_reset_at'] >= time.time() - 2
        job = tick(root, ['/synthetic/executor'], execute=broken)['jobs'][0]
        assert job['state'] == 'pending'
        assert job['error_code'] == 'E_EXTRACTION'
        assert job['attempts'] == 1


def test_a_session_job_behind_a_held_lock_queues_without_spending_attempts(tmp_path):
    from kelmo import session_worker
    from kelmo.config import Config
    from kelmo.sessions import Sessions
    from test_session_worker import executor, prepared

    def held(argv, payload, **kwargs):
        raise KelmoError('E_BUSY', 'Another worker is processing this vault.')

    root, job_id = prepared(tmp_path)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        for _ in range(5):
            job = session_worker.tick(root, ['/synthetic/executor'], execute=held, max_attempts=3)['jobs'][0]
            assert (job['state'], job['error_code'], job['attempts']) == ('pending', 'E_BUSY', 0)
            sessions = Sessions(Config.load(root))
            try:
                eligible = sessions.journal.conn.execute(
                    'SELECT eligible FROM session_processing WHERE job_id=?', (job_id,)).fetchone()[0]
                assert eligible > time.time() + session_worker.BUSY_RETRY_SECONDS - 5
                sessions.journal.conn.execute('UPDATE session_processing SET eligible=0 WHERE job_id=?', (job_id,))
                sessions.journal.conn.commit()
            finally:
                sessions.close()
        job = session_worker.tick(root, ['/synthetic/executor'], execute=executor([]), max_attempts=3)['jobs'][0]
        assert job['state'] == 'wiki_unchanged'


def test_a_busy_archive_database_fails_the_operation_as_a_held_lock(service, monkeypatch):
    import sqlite3
    operation = service.capture('Busy archive evidence.', 'busy-archive.md', 'lead')

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(service, '_archive', locked)
    result = service.run(operation['id'])
    assert (result['state'], result['error_code']) == ('failed', 'E_BUSY')


def test_a_session_note_behind_a_busy_archive_waits_without_spending_attempts(tmp_path):
    import sqlite3
    from kelmo import session_worker
    from kelmo.service import Service
    from test_session_worker import executor, prepared

    root, job_id = prepared(tmp_path)
    archive = Service._archive

    def locked(self, *args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        with patch.object(Service, '_archive', locked):
            job = session_worker.tick(root, ['/synthetic/executor'], execute=executor([]), max_attempts=1)['jobs'][0]
        assert (job['state'], job['error_code'], job['attempts']) == ('pending', 'E_BUSY', 0)
        assert Service._archive is archive


def test_a_keyless_store_behind_a_held_lock_answers_queued_not_failed(service, monkeypatch):
    import sqlite3
    from kelmo import mcp

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(service, '_archive', locked)
    result = mcp._store(service, {'content': 'Queued record.', 'kind': 'event', 'domain': 'dev',
                                  'project': 'demo', 'tags': ['a', 'b', 'c']}, None)
    assert result['queued'] is True and result['id'] is None and result['operation_id']


def test_a_session_note_waits_for_a_real_lock_held_by_another_process(tmp_path, monkeypatch):
    import subprocess
    import sys
    from kelmo import publication, session_worker
    from kelmo.config import Config
    from test_session_worker import executor, prepared

    root, job_id = prepared(tmp_path)
    lock = Config.load(root).internal('.state/worker.lock')
    holder = subprocess.Popen([sys.executable, '-c', (
        'import fcntl, sys, time\n'
        f'stream = open({str(lock)!r}, "a")\n'
        'fcntl.flock(stream, fcntl.LOCK_EX)\n'
        'print("held", flush=True)\n'
        'sys.stdin.readline()\n')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        monkeypatch.setattr(publication.coordinator_lock, '__defaults__', (0.2,))
        with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
            seen = []
            for _ in range(4):
                job = session_worker.tick(root, ['/synthetic/executor'], execute=executor([]), max_attempts=1)['jobs'][0]
                seen.append((job['state'], job['error_code'], job['attempts']))
                sessions_db = Config.load(root).internal('.state/journal.sqlite3')
                import sqlite3
                with sqlite3.connect(sessions_db) as db:
                    db.execute('UPDATE session_processing SET eligible=0 WHERE job_id=?', (job_id,))
            assert seen == [('pending', 'E_BUSY', 0)] * 4, seen
            holder.stdin.write('\n')
            holder.stdin.flush()
            holder.wait(timeout=10)
            job = session_worker.tick(root, ['/synthetic/executor'], execute=executor([]), max_attempts=1)['jobs'][0]
            assert job['state'] == 'wiki_unchanged'
    finally:
        holder.kill()


def test_a_refund_is_written_once_the_journal_frees(monkeypatch):
    import sqlite3
    from kelmo.journal import when_free
    tries, naps = [], []

    def busy_twice():
        tries.append(True)
        if len(tries) < 3:
            raise sqlite3.OperationalError('database is locked')
        return 'written'

    assert when_free(busy_twice, sleep=naps.append) == 'written' and len(tries) == 3 and naps == [1, 1]
    with pytest.raises(sqlite3.OperationalError):
        when_free(lambda: (_ for _ in ()).throw(sqlite3.OperationalError('no such table: x')), sleep=naps.append)
    with pytest.raises(sqlite3.OperationalError):
        when_free(lambda: (_ for _ in ()).throw(sqlite3.OperationalError('database is locked')),
                  tries=2, sleep=naps.append)


def test_a_session_refund_survives_a_busy_journal_after_the_wait(tmp_path, monkeypatch):
    import sqlite3
    from kelmo import journal, session_worker
    from test_session_worker import prepared

    def held(argv, payload, **kwargs):
        raise KelmoError('E_BUSY', 'Another worker is processing this vault.')

    root, job_id = prepared(tmp_path)
    real = journal.when_free
    busy = [2]

    def flaky(write, **kwargs):
        def first_locked():
            if busy[0]:
                busy[0] -= 1
                raise sqlite3.OperationalError('database is locked')
            return write()
        return real(first_locked, sleep=lambda seconds: None)

    monkeypatch.setattr(session_worker, 'when_free', flaky)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        job = session_worker.tick(root, ['/synthetic/executor'], execute=held, max_attempts=1)['jobs'][0]
    assert (job['state'], job['error_code'], job['attempts']) == ('pending', 'E_BUSY', 0) and busy == [0]
