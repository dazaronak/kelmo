import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def note(service, text='The archive keeps evidence.', key='fixture.archive', revision=None):
    change = {'fact_key': key, 'target': 'projects/fixture/fixture.md', 'text': text,
                  'status': 'active', 'expected_revision': revision, 'replaces': []}
    receipt = service.reconcile(text, change['target'])
    check = {'fact_key': key, 'verified_match': {'result': 'not_found', 'search_id': receipt['search_id']}}
    return ('---\nproject: fixture\ndomain: dev\nsource: manual\n---\n\n```wiki-changes\n'
            + json.dumps([change]) + '\n```\n```fact-checks\n' + json.dumps([check]) + '\n```\n')


@pytest.fixture
def service(tmp_path):
    from kelmo.service import Service
    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)), Service(root) as value:
        yield value


def test_capture_is_durable_before_worker_and_retries_keep_one_operation(service):
    first = service.capture(note(service), 'source.md', 'lead', 'promote', 'same-request')
    again = service.capture(note(service), 'source.md', 'lead', 'promote', 'same-request')
    assert first['id'] == again['id']
    assert first['state'] == 'queued'
    assert service.status()['archive_records'] == 0
    assert service.trace(first['id'])['events'][-1]['stage'] == 'queued'


def test_worker_cannot_process_a_job_claimed_by_another_attempt(service):
    from kelmo.errors import KelmoError
    op = service.capture(note(service), 'claimed.md', 'lead', 'promote')
    token = service.journal.claim(op['id'])
    with pytest.raises(KelmoError, match='claimed'):
        service.run(op['id'])
    assert service.status()['archive_records'] == 0
    service.journal.release(op['id'], token)
    assert service.run(op['id'])['state'] == 'applied'
    assert service.status()['archive_records'] == 1


def test_idempotency_key_cannot_replace_source_bytes(service):
    from kelmo.errors import KelmoError
    service.capture(note(service), 'source.md', 'lead', 'promote', 'same-request')
    with pytest.raises(KelmoError, match='idempotency'):
        service.capture(note(service, 'Different.'), 'source.md', 'lead', 'promote', 'same-request')


def test_existing_compiler_and_archive_complete_the_local_path(service):
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    result = service.run(op['id'])
    assert result['state'] == 'applied'
    assert result['terminal'] is True
    assert service.status()['archive_records'] == 1
    assert service.status()['facts'] == 1
    assert 'The archive keeps evidence.' in (service.root / 'knowledge/wiki/projects/fixture/fixture.md').read_text()
    assert [e['stage'] for e in service.trace(op['id'])['events']] == ['captured', 'queued', 'archived', 'editor_result',
                                                                       'editor_result', 'applied']
    assert service.run(op['id'])['state'] == 'applied'
    assert service.status()['archive_records'] == 1


def test_reviewer_evidence_cannot_publish_facts(service):
    op = service.capture(note(service), 'review.md', 'reviewer', 'archive')
    assert service.run(op['id'])['state'] == 'archived'
    assert service.status()['facts'] == 0


@pytest.mark.parametrize('role', ['reviewer', 'implementer', 'collector', 'unknown'])
def test_non_lead_promotion_is_rejected_before_queueing(service, role):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError):
        service.capture(note(service), 'child.md', role, 'promote')
    assert service.status()['operations'] == {}


@pytest.mark.parametrize('role', ['collector', 'unknown'])
def test_collectors_and_unknown_sources_stay_local(service, role):
    op = service.capture('Local recovery only.', 'recovery.md', role, 'archive')
    assert op['state'] == 'captured'
    assert service.run(op['id'])['state'] == 'captured'
    assert service.status()['archive_records'] == 0


def test_failed_wiki_commit_keeps_receipt_and_retry_does_not_duplicate_archive(service):
    from kelmo.wiki import engine
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    with patch.object(engine, '_commit_registry', side_effect=RuntimeError('commit failed')):
        assert service.run(op['id'])['state'] == 'failed'
    assert service.status()['archive_records'] == 1
    service.retry(op['id'])
    assert service.run(op['id'])['state'] == 'applied'
    assert service.status()['archive_records'] == 1


def test_a_failure_the_core_stop_caused_leaves_the_operation_waiting_and_uncharged(service):
    from kelmo import stopping
    from kelmo.errors import KelmoError, Stopping
    from kelmo.wiki import engine
    op = service.capture(note(service), 'source.md', 'lead', 'promote')

    def cut_by_the_stop(*args, **kwargs):
        stopping.request()
        raise KelmoError('E_ARCHIVE_PROCESS', 'The archive core is stopping.')

    with patch.object(engine, 'recover_transaction', side_effect=cut_by_the_stop):
        with pytest.raises(Stopping):
            service.run(op['id'])
    after = service.journal.get(op['id'])
    assert after['state'] != 'failed' and not after['terminal']
    assert 'failed' not in [event['stage'] for event in service.trace(op['id'])['events']]


def test_source_tampering_stops_before_archival(service):
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    (service.root / op['source_path']).write_text('replaced')
    assert service.run(op['id'])['state'] == 'failed'
    assert service.status()['archive_records'] == 0


def test_queue_transaction_failure_does_not_strand_a_lead_as_local_only(service):
    original = service.journal.event
    def fail_queue(operation_id, stage, **details):
        if stage == 'queued':
            raise RuntimeError('interrupted before queue commit')
        original(operation_id, stage, **details)
    with patch.object(service.journal, 'event', side_effect=fail_queue), pytest.raises(RuntimeError):
        service.capture(note(service), 'source.md', 'lead', 'promote', 'retry-capture')
    assert service.status()['operations'] == {}
    assert service.orphan_sources() == 1
    assert service.capture(note(service), 'source.md', 'lead', 'promote', 'retry-capture')['state'] == 'queued'
    assert service.orphan_sources() == 0


def test_source_directory_cannot_redirect_capture_outside_vault(service, tmp_path):
    from kelmo.errors import KelmoError
    outside = tmp_path / 'outside'
    outside.mkdir()
    (service.config.state / 'sources').symlink_to(outside, target_is_directory=True)
    with pytest.raises(KelmoError):
        service.capture('Synthetic evidence.', 'source.md')
    assert list(outside.iterdir()) == []


def test_archive_open_failure_closes_the_journal(service):
    from kelmo.service import Service
    with patch('kelmo.service.Journal') as journal:
        with patch('kelmo.service.open_connection', side_effect=OSError), pytest.raises(OSError):
            Service(service.root)
        journal.return_value.close.assert_called_once()


def test_failure_preserves_highest_durable_stage_and_retry_clears_only_current_error(service):
    from kelmo.wiki import engine
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    with patch.object(engine, '_commit_registry', side_effect=RuntimeError('synthetic failure')):
        failed = service.run(op['id'])
    assert failed['state'] == 'failed'
    assert failed['durable_stage'] == 'archived'
    retry = service.retry(op['id'])
    assert retry['durable_stage'] == 'archived'
    assert retry['error_code'] is None
    assert any(e['details'].get('error_code') for e in service.trace(op['id'])['events'])


def test_source_disappearing_during_resolution_creation_becomes_failed(service, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.resolution import Resolution
    operation = service.capture(note(service), 'resolution-source.md', 'lead', 'promote')
    create = Resolution.create

    def disappear(resolution, current, content):
        service.source_path(current).unlink()
        return create(resolution, current, content)

    monkeypatch.setattr('kelmo.service.fact_checks.validate',
                        lambda *args: (_ for _ in ()).throw(KelmoError('E_FACT_CHECK', 'stale')))
    monkeypatch.setattr(Resolution, 'create', disappear)
    result = service.run(operation['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_SOURCE'
    assert result['durable_stage'] == 'archived'
    assert result['archive_id']


def test_safe_resolution_creation_error_becomes_failed(service, monkeypatch):
    from kelmo.errors import KelmoError
    operation = service.capture(note(service), 'resolution-error.md', 'lead', 'promote')
    monkeypatch.setattr('kelmo.service.fact_checks.validate',
                        lambda *args: (_ for _ in ()).throw(KelmoError('E_FACT_CHECK', 'stale')))
    monkeypatch.setattr('kelmo.resolution.Resolution.create',
                        lambda *args: (_ for _ in ()).throw(KelmoError('E_SOURCE', 'missing')))
    result = service.run(operation['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_SOURCE'
    assert result['durable_stage'] == 'archived'


def test_resolution_database_error_remains_fatal(service, monkeypatch):
    import sqlite3
    from kelmo.errors import KelmoError
    operation = service.capture(note(service), 'resolution-database.md', 'lead', 'promote')
    monkeypatch.setattr('kelmo.service.fact_checks.validate',
                        lambda *args: (_ for _ in ()).throw(KelmoError('E_FACT_CHECK', 'stale')))
    monkeypatch.setattr('kelmo.resolution.Resolution.create',
                        lambda *args: (_ for _ in ()).throw(sqlite3.DatabaseError('corrupt')))
    with pytest.raises(sqlite3.DatabaseError):
        service.run(operation['id'])
    assert service.journal.get(operation['id'])['state'] == 'archived'


@pytest.mark.parametrize('limit', [0, -1, 501])
def test_list_rejects_invalid_limits(service, limit):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError):
        service.list(limit)


def crash_at_commit(service, operation_id, after_commit=False):
    from kelmo.wiki import engine
    source_root = Path(engine.__file__).resolve().parents[2]
    program = '''import os,sys
from unittest.mock import patch
sys.path.insert(0,SOURCE)
from kelmo.service import Service
from kelmo.wiki import engine
original=engine.git
def interrupted(vault,*args,**kwargs):
    if args[0]=='commit':
        if AFTER: original(vault,*args,**kwargs)
        os._exit(77)
    return original(vault,*args,**kwargs)
engine.git=interrupted
with patch('kelmo.store.memory.store.embed',return_value=bytes(384*4)), Service(ROOT) as service:
    service.run(OPERATION)
'''.replace('SOURCE', repr(str(source_root))).replace('AFTER', repr(after_commit)).replace('ROOT', repr(str(service.root))).replace('OPERATION', repr(operation_id))
    result = subprocess.run([sys.executable, '-I', '-c', program],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 77, result.stderr


@pytest.mark.parametrize('after_commit', [False, True])
def test_hard_exit_recovery_keeps_one_receipt_and_committed_wiki(service, after_commit):
    from kelmo.wiki import engine
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    crash_at_commit(service, op['id'], after_commit)
    assert service.run(op['id'])['state'] == 'applied'
    assert service.status()['archive_records'] == 1
    assert engine.git(service.root, 'status', '--porcelain', '--', 'knowledge') == ''


def test_hard_exit_recovery_preserves_unexpected_edits(service):
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    crash_at_commit(service, op['id'])
    page = service.root / 'knowledge/wiki/projects/fixture/fixture.md'
    page.write_text('External edit must remain available.\n')
    result = service.run(op['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_CONFLICT'
    assert page.read_text() == 'External edit must remain available.\n'


def test_recovery_reports_conflict_when_new_head_does_not_include_pending_pages(service):
    from kelmo.wiki import engine
    op = service.capture(note(service), 'source.md', 'lead', 'promote')
    crash_at_commit(service, op['id'])
    (service.root / 'unrelated.txt').write_text('Unrelated change')
    engine.git(service.root, 'add', '--', 'unrelated.txt')
    engine.git(service.root, 'commit', '--only', '-m', 'Unrelated change', '--', 'unrelated.txt')
    result = service.run(op['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_CONFLICT'
    assert engine.transaction_path(service.root).is_file()


def test_archival_preserves_source_date_before_hash_prefix(service):
    op = service.capture('Dated synthetic evidence.', '2026-04-10_120000_fixture.md', 'lead')
    archived = service.run(op['id'])
    assert service.show(archived['archive_id'])['valid_from'] == '2026-04-10'
