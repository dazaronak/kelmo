import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kelmo.errors import KelmoError
from kelmo.journal import Journal


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    value = Journal(path, create=True)
    with value.conn:
        value.conn.execute('''INSERT INTO operations
            (id,request_key,source_hash,source_path,filename,role,intent,state,created_at)
            VALUES ('job','key','hash','source.md','source.md','lead','promote','queued','date')''')
    yield path, value
    value.close()


def test_two_connections_cannot_claim_the_same_job(journal):
    path, _ = journal
    barrier = threading.Barrier(2)

    def claim():
        client = Journal(path)
        try:
            barrier.wait(timeout=5)
            return client.claim('job', lease_seconds=60)
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(value is not None for value in results) == 1


def test_restart_preserves_claim_and_expiry_rejects_old_result(journal):
    path, first = journal
    old = first.claim('job', lease_seconds=60)
    second = Journal(path)
    try:
        assert second.claim('job', lease_seconds=60) is None
        with first.conn:
            first.conn.execute("UPDATE claims SET expires_at=0 WHERE operation_id='job'")
        current = second.claim('job', lease_seconds=60)
        assert current != old
        with pytest.raises(KelmoError, match='claim'):
            first.stage('job', 'applied', terminal=True, claim_token=old)
        assert second.get('job')['state'] == 'queued'
        second.stage('job', 'applied', terminal=True, claim_token=current)
        assert second.get('job')['state'] == 'applied'
        assert first.claim('job', lease_seconds=60) is None
    finally:
        second.close()


def test_claim_cannot_be_bypassed_or_released_by_another_attempt(journal):
    _, value = journal
    token = value.claim('job', lease_seconds=60)
    with pytest.raises(KelmoError, match='claim'):
        value.stage('job', 'failed', terminal=True)
    value.release('job', 'wrong-token')
    assert value.claim('job', lease_seconds=60) is None
    value.release('job', token)
    assert value.claim('job', lease_seconds=60) not in (None, token)


def test_expired_attempt_cannot_complete_before_reclaim(journal):
    _, value = journal
    token = value.claim('job', lease_seconds=60)
    with value.conn:
        value.conn.execute("UPDATE claims SET expires_at=0 WHERE operation_id='job'")
    with pytest.raises(KelmoError, match='claim'):
        value.stage('job', 'applied', terminal=True, claim_token=token)


def test_a_claim_whose_owner_died_is_taken_over_and_a_living_one_is_not(journal):
    import subprocess
    import sys
    from kelmo.journal import claim_owner
    _, journal_value = journal
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    host = claim_owner()[0]
    first = journal_value.claim('job', lease_seconds=3600)
    assert journal_value.claim('job', lease_seconds=60) is None, 'this process still runs: its claim holds'
    with journal_value.conn:
        journal_value.conn.execute('UPDATE claims SET owner_pid=? WHERE operation_id=?', (dead.pid, 'job'))
    [stale] = journal_value.stale_claims()
    assert (stale['operation_id'], stale['owner_host'], stale['owner_pid']) == ('job', host, dead.pid)
    second = journal_value.claim('job', lease_seconds=60)
    assert second is not None and second != first
    assert [event['stage'] for event in journal_value.trace('job')['events']] == ['claim_recovered']
    with pytest.raises(KelmoError, match='another attempt'):
        journal_value.stage('job', 'archived', claim_token=first)
    with journal_value.conn:
        journal_value.conn.execute("UPDATE claims SET owner_host='elsewhere', owner_pid=? WHERE operation_id='job'",
                                   (dead.pid,))
    assert journal_value.claim('job', lease_seconds=60) is None, 'an owner on another host keeps the lease rule'
    assert journal_value.stale_claims() == []
