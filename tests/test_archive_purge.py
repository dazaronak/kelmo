"""Physical purge: a separate administrator capability under a coordinated disposition, never a hide."""
import base64
import fcntl
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kelmo.archive_admin import ArchiveAdmin, PurgeDisposition, operator_grant
from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']

TEXT = 'Purge evidence zephyrquartz.'


@pytest.fixture
def dispositions():
    return {}


def admin(service, dispositions, *verbs):
    return ArchiveAdmin(service, operator_grant(service.config, set(verbs or ('purge', 'purge-plan'))),
                        disposition_source=dispositions.get)


def issuer(service):
    """The real durable path: dispositions stored in and read from the vault's journal."""
    return ArchiveAdmin(service, operator_grant(service.config, {'purge', 'purge-plan'}))


def authorize(service, archive_id, disposition_id='disposition-1'):
    tool = issuer(service)
    plan = tool.purge_plan(archive_id)
    return tool, plan, tool.authorize_purge(archive_id, disposition_id, plan['plan_sha256'])


def cli(service, *args):
    """One `kelmo admin` call in a new process: the vault is reopened for every call."""
    done = subprocess.run([sys.executable, '-m', 'kelmo.cli', 'admin', *map(str, args),
                           '--vault', str(service.root), '--json'], capture_output=True, text=True, timeout=120)
    return done.returncode, json.loads(done.stdout)


def archived(service, text=TEXT, name='note.md', **options):
    operation = service.capture(text, name, 'implementer', **options)
    return service.run(operation['id'])


def dispose(tool, dispositions, archive_id, disposition_id='disposition-1', drop=()):
    plan = tool.purge_plan(archive_id)
    actions = tuple(sorted(item for item in plan['required'].items() if item[0] not in drop))
    dispositions[disposition_id] = PurgeDisposition(disposition_id, archive_id, plan['plan_sha256'], actions)
    return plan


def fts_hits(service, token='zephyrquartz'):
    return service.archive.execute('SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?',
                                   (token,)).fetchone()[0]


def intact(service, archive_id):
    assert service.show(archive_id)['content'] == TEXT
    assert fts_hits(service) == 1
    assert service.archive.execute('SELECT COUNT(*) FROM archive_purges').fetchone()[0] == 0


def test_a_disposition_nobody_authorized_refuses_and_removes_nothing(service):
    record = archived(service)
    tool = ArchiveAdmin(service, operator_grant(service.config, {'purge'}))
    with pytest.raises(KelmoError) as refused:
        tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY'
    intact(service, record['archive_id'])


def test_a_grant_without_purge_cannot_purge_or_plan(service, dispositions):
    record = archived(service)
    tool = admin(service, dispositions, 'count', 'stats')
    for call in (lambda: tool.purge(record['archive_id'], 'disposition-1'),
                 lambda: tool.purge_plan(record['archive_id'])):
        with pytest.raises(KelmoError) as refused:
            call()
        assert refused.value.code == 'E_PERMISSION'
    intact(service, record['archive_id'])


def test_incomplete_unknown_or_stale_dispositions_refuse(service, dispositions):
    record = archived(service)
    archive_id = record['archive_id']
    tool = admin(service, dispositions)
    plan = dispose(tool, dispositions, archive_id, drop=('operation:' + record['id'],))
    assert set(plan['required']) == {'receipt:' + record['id'], 'operation:' + record['id']}
    for disposition_id, code in (('disposition-1', 'E_CONFLICT'), ('unknown', 'E_CAPABILITY')):
        with pytest.raises(KelmoError) as refused:
            tool.purge(archive_id, disposition_id)
        assert refused.value.code == code
    dispose(tool, dispositions, archive_id, 'disposition-2')
    service.forget(archive_id)
    with pytest.raises(KelmoError) as refused:
        tool.purge(archive_id, 'disposition-2')
    assert refused.value.code == 'E_CONFLICT'
    assert service.archive.execute('SELECT content FROM memories WHERE id=?', (archive_id,)).fetchone()[0] == TEXT


def test_an_authorized_purge_removes_archive_copies_and_declares_what_remains(service, dispositions):
    record = archived(service)
    archive_id = record['archive_id']
    digest = hashlib.sha256(TEXT.encode()).hexdigest()
    rowid = service.archive.execute('SELECT rowid FROM memories WHERE id=?', (archive_id,)).fetchone()[0]
    tool = admin(service, dispositions)
    dispose(tool, dispositions, archive_id)
    report = tool.purge_report(archive_id, 'disposition-1')
    receipt = report['receipt']
    assert receipt['outcome'] == 'purged' and receipt['idempotency_key'] == 'disposition-1'
    assert [ref['content_sha256'] for ref in receipt['evidence_refs']] == [digest]
    connection = service.archive
    for sql in ('SELECT COUNT(*) FROM memories WHERE id=?', 'SELECT COUNT(*) FROM memory_versions WHERE memory_id=?',
                'SELECT COUNT(*) FROM chunks WHERE memory_id=?',
                'SELECT COUNT(*) FROM archive_receipts WHERE archive_id=?',
                'SELECT COUNT(*) FROM archive_sources WHERE archive_id=?',
                'SELECT COUNT(*) FROM archive_version_sources WHERE archive_id=?'):
        assert connection.execute(sql, (archive_id,)).fetchone()[0] == 0, sql
    assert connection.execute('SELECT COUNT(*) FROM memories_vec WHERE rowid=?', (rowid,)).fetchone()[0] == 0
    assert fts_hits(service) == 0
    assert report['effects']['memories'] == 1 and report['effects']['memories_vec'] == 1
    assert report['whole_system_erasure'] is False
    assert report['retained_spool'] == [f'.state/sources/{digest}.md']
    assert service.config.internal(f'.state/sources/{digest}.md').read_text() == TEXT
    assert any('Git history' in item for item in report['retained'])
    with pytest.raises(KelmoError) as refused:
        service.show(archive_id)
    assert refused.value.code == 'E_PURGED'
    assert service.search('zephyrquartz') == []
    operation = service.journal.get(record['id'])
    assert operation['state'] == 'purged' and operation['terminal'] is True
    assert operation['durable_stage'] == 'purged'
    stages = [event['stage'] for event in service.trace(record['id'])['events']]
    assert 'purge' in stages and 'purged' in stages


def test_replays_and_unfinished_captures_cannot_resurrect_a_purged_record(service, dispositions):
    record = archived(service)
    archive_id = record['archive_id']
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    tool = admin(service, dispositions)
    plan = dispose(tool, dispositions, archive_id)
    assert 'operation:' + waiting['id'] in plan['required']
    first = tool.purge(archive_id, 'disposition-1')
    assert service.run(waiting['id'])['state'] == 'purged'
    later = service.run(service.capture(TEXT, 'third-copy.md', 'implementer')['id'])
    assert (later['state'], later['error_code']) == ('failed', 'E_PURGED')
    with pytest.raises(KelmoError) as refused:
        service.archive_api.capture(service.source_packet(record['id']), service._operation_origin(record['id']),
                                    record['id'])
    assert refused.value.code == 'E_PURGED'
    with pytest.raises(KelmoError) as refused:
        service.retry(record['id'])
    assert refused.value.code == 'E_STATE'
    assert service.archive.execute('SELECT COUNT(*) FROM memories WHERE content=?', (TEXT,)).fetchone()[0] == 0
    assert tool.purge(archive_id, 'disposition-1') == first
    assert tool.purge_report(archive_id, 'disposition-1')['replayed'] is True
    with pytest.raises(KelmoError) as refused:
        tool.purge(archive_id, 'disposition-other')
    assert refused.value.code == 'E_IDEMPOTENCY'


def test_a_failed_purge_rolls_back_every_archive_row(service, dispositions):
    record = archived(service)
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    with patch('kelmo.store.memory.search_log.forget_record', side_effect=RuntimeError('fault after deletes')):
        with pytest.raises(RuntimeError):
            tool.purge(record['archive_id'], 'disposition-1')
    intact(service, record['archive_id'])
    assert service.archive.execute('SELECT COUNT(*) FROM archive_receipts').fetchone()[0] == 1
    assert service.journal.get(record['id'])['state'] == 'archived'


def test_purge_refuses_inside_a_caller_transaction_and_keeps_it_open(service, dispositions):
    record = archived(service)
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    journal = service.journal.conn
    with journal:
        journal.execute("INSERT INTO claims (operation_id, token, expires_at, local_worker) VALUES (?, 't', 0, 1)",
                        (record['id'],))
    connection = service.archive
    connection.execute('BEGIN')
    connection.execute("INSERT INTO archive_meta (key, value) VALUES ('caller.probe', '1')")
    with pytest.raises(KelmoError) as refused:
        tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_STATE' and connection.in_transaction
    connection.rollback()
    intact(service, record['archive_id'])
    # Refused before any journal change: the stale local claim the purge would hand over is still there.
    assert journal.execute('SELECT COUNT(*) FROM claims WHERE operation_id=?', (record['id'],)).fetchone()[0] == 1


def test_accepted_claims_have_no_disposition(service, dispositions):
    from test_fact_checks import content
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    promoted = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                               'fact.md', 'lead', 'promote')
    applied = service.run(promoted['id'])
    tool = admin(service, dispositions)
    plan = tool.purge_plan(applied['archive_id'])
    assert 'claim:fixture.fact' in plan['refused']
    assert any(key.startswith('note:') for key in plan['refused'])
    with pytest.raises(KelmoError) as refused:
        tool.purge(applied['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY'
    with pytest.raises(KelmoError) as refused:
        issuer(service).authorize_purge(applied['archive_id'], 'disposition-1', plan['plan_sha256'])
    assert refused.value.code == 'E_CAPABILITY'
    assert service.journal.purge_disposition('disposition-1') is None


def test_supersession_links_are_unlinked_and_named_while_the_predecessor_stays_hidden(service, dispositions):
    older = archived(service, 'Older purge evidence.', 'older.md')
    newer = archived(service, supersedes=older['archive_id'])
    tool = admin(service, dispositions)
    plan = dispose(tool, dispositions, newer['archive_id'])
    assert plan['required']['predecessor:' + older['archive_id']] == 'unlink'
    report = tool.purge_report(newer['archive_id'], 'disposition-1')
    assert report['effects']['links_removed']['predecessor'] == older['archive_id']
    assert service.archive.execute('SELECT COUNT(*) FROM record_supersession').fetchone()[0] == 0
    with pytest.raises(KelmoError) as refused:
        service.show(older['archive_id'])
    assert refused.value.code == 'E_FORGOTTEN'


def test_hide_stays_distinct_and_ordinary_surfaces_hold_no_purge(service, dispositions):
    record = archived(service)
    service.forget(record['archive_id'])
    assert service.archive.execute('SELECT content FROM memories WHERE id=?',
                                   (record['archive_id'],)).fetchone()[0] == TEXT
    assert service.archive_api.purge_record(record['archive_id']) is None
    assert not hasattr(service, 'purge') and not hasattr(service.archive_api, 'purge')


def test_purge_zeroes_serialized_source_bytes_in_the_main_archive_file(service, dispositions):
    text = 'SyntheticPurgeCanaryQuartzViolet0123456789.' * 180
    markers = (base64.b64encode(text.encode())[48:144], text[:96].encode())
    service.archive.execute('PRAGMA secure_delete=OFF')
    record = archived(service, text)
    path = Path(service.archive.execute('PRAGMA database_list').fetchone()[2])
    service.archive.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    assert all(marker in path.read_bytes() for marker in markers)
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    tool.purge(record['archive_id'], 'disposition-1')
    service.archive.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    remaining = path.read_bytes()
    assert not any(marker in remaining for marker in markers)
    # The caller's own setting is restored after the purge.
    assert service.archive.execute('PRAGMA secure_delete').fetchone()[0] == 0


def test_update_cannot_bring_back_purged_bytes_or_sources(service, dispositions):
    record = archived(service)
    source = service.source_packet(record['id'])
    origin = service._operation_origin(record['id'])
    survivor = archived(service, 'Retained independent evidence.', 'survivor.md')
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    tool.purge(record['archive_id'], 'disposition-1')
    with pytest.raises(KelmoError) as refused:
        service.archive_api.update(survivor['archive_id'], 1, source, origin)
    assert refused.value.code == 'E_PURGED'
    assert service.show(survivor['archive_id'])['content'] == 'Retained independent evidence.'
    assert service.archive.execute('SELECT version FROM memories WHERE id=?',
                                   (survivor['archive_id'],)).fetchone()[0] == 1


def test_unsettled_resolutions_and_any_external_claim_are_refused_dependents(service, dispositions):
    from kelmo.resolution import Resolution
    record = archived(service)
    Resolution(service)
    journal = service.journal.conn
    with journal:
        journal.execute("INSERT INTO resolutions (id, operation_id, payload, snapshot) VALUES ('job-1', ?, '{}', 's')",
                        (record['id'],))
        journal.execute("INSERT INTO claims (operation_id, token, expires_at, local_worker) VALUES (?, 't', 0, 0)",
                        (record['id'],))
    tool = admin(service, dispositions)
    plan = tool.purge_plan(record['archive_id'])
    assert {'resolution:job-1', 'lease:' + record['id']} <= set(plan['refused'])
    with pytest.raises(KelmoError) as refused:
        tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY'
    intact(service, record['archive_id'])
    assert journal.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 1


def test_a_crash_after_the_archive_commit_is_finished_by_the_same_purge(service, dispositions):
    from kelmo.journal import Journal
    record = archived(service)
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    with patch.object(Journal, 'event_once', side_effect=RuntimeError('crash before the journal half')):
        with pytest.raises(RuntimeError):
            tool.purge(record['archive_id'], 'disposition-1')
    assert service.archive_api.purge_record(record['archive_id']) is not None
    assert service.journal.get(record['id'])['state'] == 'archived'
    report = tool.purge_report(record['archive_id'], 'disposition-1')
    assert report['replayed'] is True
    assert service.journal.get(record['id'])['durable_stage'] == 'purged'


def test_telemetry_written_after_a_purge_drops_the_purged_id(service, dispositions):
    from kelmo.store.memory import search_log
    record = archived(service)
    survivor = archived(service, 'Retained independent evidence.', 'survivor.md')
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    tool.purge(record['archive_id'], 'disposition-1')
    settings = {'search_log': 'redacted', 'retention_days': 30, 'max_rows': 10}
    assert search_log.record(service.config.archive, settings, variants=['ranked before the purge'],
                             mode='hybrid', limit=2, filters={}, semantic=True, ms=1.0, retrieval={},
                             results=[{'id': record['archive_id']}, {'id': survivor['archive_id']}])
    results = search_log.read_recent(service.config.archive)[0]['results']
    assert results == [{'id': None, 'chunk_idx': None, 'purged': True},
                       {'id': survivor['archive_id'], 'chunk_idx': None}]


def test_the_storage_delete_refuses_a_managed_archive(service):
    from kelmo.store.errors import ArchiveStorageError
    from kelmo.store.memory.delete import memory_delete
    record = archived(service)
    with pytest.raises(ArchiveStorageError):
        memory_delete(service.archive, id=record['archive_id'])
    intact(service, record['archive_id'])


# --- the durable disposition: purge-authorize, then purge, each its own process ---

def test_the_operator_authorizes_then_purges_through_the_cli_in_separate_processes(service):
    record = archived(service)
    archive_id = record['archive_id']
    code, plan = cli(service, 'purge-plan', archive_id)
    assert code == 0
    digest = plan['result']['plan_sha256']
    code, authorized = cli(service, 'purge-authorize', archive_id, '--disposition', 'erase-1',
                           '--plan-sha256', digest)
    assert code == 0 and authorized['result']['state'] == 'authorized'
    assert authorized['result']['actions'] == plan['result']['required']
    assert authorized['result']['removed'] is False
    # Authorization alone removes nothing; the stored row names the vault, operator, record and plan.
    intact(service, archive_id)
    stored = service.journal.purge_disposition('erase-1')
    assert (stored['vault_id'], stored['operator'], stored['archive_id'], stored['plan_sha256'], stored['state']) == (
        service.config.vault_id, 'operator', archive_id, digest, 'authorized')
    code, purged = cli(service, 'purge', archive_id, '--disposition', 'erase-1')
    assert code == 0 and purged['result']['receipt']['outcome'] == 'purged'
    assert purged['result']['disposition_state'] == 'settled' and purged['result']['replayed'] is False
    settled = service.journal.purge_disposition('erase-1')
    assert settled['state'] == 'settled' and settled['receipt'] == purged['result']['receipt']
    assert service.journal.get(record['id'])['state'] == 'purged'
    with pytest.raises(KelmoError) as refused:
        service.show(archive_id)
    assert refused.value.code == 'E_PURGED'
    code, again = cli(service, 'purge', archive_id, '--disposition', 'erase-1')
    assert code == 0 and again['result']['replayed'] is True
    assert again['result']['receipt'] == purged['result']['receipt']


def test_purge_needs_the_explicit_hash_and_its_own_capability(service):
    record = archived(service)
    # No default plan hash: the operator passes the one purge-plan showed.
    code, envelope = cli(service, 'purge-authorize', record['archive_id'], '--disposition', 'erase-1')
    assert code == 2 and envelope['error']['code'] == 'E_USAGE'
    for verb in ('count', 'purge-plan'):
        with pytest.raises(KelmoError) as refused:
            ArchiveAdmin(service, operator_grant(service.config, {verb})).authorize_purge(
                record['archive_id'], 'erase-1', '0' * 64)
        assert refused.value.code == 'E_PERMISSION'
    for disposition_id, digest in (('', '0' * 64), ('../x', '0' * 64), ('erase-1', 'short')):
        with pytest.raises(KelmoError) as refused:
            issuer(service).authorize_purge(record['archive_id'], disposition_id, digest)
        assert refused.value.code == 'E_INPUT'
    assert service.journal.purge_disposition('erase-1') is None
    intact(service, record['archive_id'])


def test_authorization_replays_refuses_reuse_and_never_approves_a_changed_plan(service):
    record = archived(service)
    other = archived(service, 'Other purge evidence.', 'other.md')
    archive_id = record['archive_id']
    tool, plan, first = authorize(service, archive_id)
    assert first['replayed'] is False
    assert tool.authorize_purge(archive_id, 'disposition-1', plan['plan_sha256']) == dict(first, replayed=True)
    other_plan = tool.purge_plan(other['archive_id'])
    for target, digest in ((archive_id, other_plan['plan_sha256']), (other['archive_id'], other_plan['plan_sha256'])):
        with pytest.raises(KelmoError) as refused:
            tool.authorize_purge(target, 'disposition-1', digest)
        assert refused.value.code == 'E_IDEMPOTENCY'
    with pytest.raises(KelmoError) as refused:
        tool.authorize_purge(archive_id, 'disposition-stale', '0' * 64)
    assert refused.value.code == 'E_CONFLICT'
    assert service.journal.purge_disposition('disposition-stale') is None
    # The plan changes after authorization: the stored disposition is not carried over to it.
    service.forget(archive_id)
    with pytest.raises(KelmoError) as refused:
        tool.purge(archive_id, 'disposition-1')
    assert refused.value.code == 'E_CONFLICT'
    with pytest.raises(KelmoError) as refused:
        tool.authorize_purge(archive_id, 'disposition-2', plan['plan_sha256'])
    assert refused.value.code == 'E_CONFLICT'
    assert service.archive.execute('SELECT content FROM memories WHERE id=?', (archive_id,)).fetchone()[0] == TEXT
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'
    current = tool.purge_plan(archive_id)
    tool.authorize_purge(archive_id, 'disposition-2', current['plan_sha256'])
    assert tool.purge_report(archive_id, 'disposition-2')['disposition_state'] == 'settled'
    with pytest.raises(KelmoError) as refused:
        tool.purge(archive_id, 'disposition-1')
    assert refused.value.code == 'E_IDEMPOTENCY'
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'


def test_a_dependent_that_appears_after_authorization_refuses_the_purge(service):
    record = archived(service)
    tool, plan, _ = authorize(service, record['archive_id'])
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    later = tool.purge_plan(record['archive_id'])
    # The archive-side compare-and-swap value is unchanged; the reviewed identity is not.
    assert later['archive_sha256'] == plan['archive_sha256']
    assert later['plan_sha256'] != plan['plan_sha256'] and 'operation:' + waiting['id'] in later['required']
    with pytest.raises(KelmoError) as refused:
        tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CONFLICT'
    intact(service, record['archive_id'])
    assert service.journal.purge_intent('disposition-1') is None


def test_authorization_never_includes_a_capture_the_operator_did_not_review(service):
    record = archived(service)
    tool = issuer(service)
    plan = tool.purge_plan(record['archive_id'])
    waiting = service.capture(TEXT, 'unreviewed-copy.md', 'implementer')
    assert 'operation:' + waiting['id'] not in plan['required']
    with pytest.raises(KelmoError) as refused:
        tool.authorize_purge(record['archive_id'], 'reviewed-plan', plan['plan_sha256'])
    assert refused.value.code == 'E_CONFLICT'
    assert service.journal.purge_disposition('reviewed-plan') is None
    intact(service, record['archive_id'])


def test_a_disposition_never_purges_or_settles_another_record(service):
    from kelmo.journal import Journal
    first = archived(service)
    second = archived(service, 'Second purge evidence.', 'second.md')
    tool, _, _ = authorize(service, first['archive_id'])
    with pytest.raises(KelmoError) as refused:
        tool.purge(second['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY'
    assert service.show(second['archive_id'])['content'] == 'Second purge evidence.'
    with pytest.raises(KelmoError) as refused:
        service.journal.settle_purge('disposition-1', second['archive_id'], {}, [],
                                     check_writer=service.archive_api.assert_writer)
    assert refused.value.code == 'E_CONFLICT'
    # A concurrent issuer stored the same id for another record between the first read and the insert.
    second_plan = tool.purge_plan(second['archive_id'])
    with patch.object(Journal, 'purge_disposition', return_value=None):
        with pytest.raises(KelmoError) as refused:
            tool.authorize_purge(second['archive_id'], 'disposition-1', second_plan['plan_sha256'])
    assert refused.value.code == 'E_IDEMPOTENCY'
    assert service.journal.purge_disposition('disposition-1')['archive_id'] == first['archive_id']
    intact(service, first['archive_id'])


DIE_AFTER_ARCHIVE_COMMIT = (
    'import os, sys\n'
    'from kelmo import cli, journal\n'
    'journal.Journal.event_once = lambda *args, **kwargs: os._exit(17)\n'
    'sys.exit(cli.main(sys.argv[1:]))\n')


def test_a_process_that_dies_after_the_archive_commit_is_finished_by_the_next_purge(service):
    record = archived(service)
    authorize(service, record['archive_id'])
    died =subprocess.run([sys.executable, '-c', DIE_AFTER_ARCHIVE_COMMIT, 'admin', 'purge', record['archive_id'],
                           '--disposition', 'disposition-1', '--vault', str(service.root), '--json'],
                          capture_output=True, text=True, timeout=120)
    assert died.returncode == 17
    assert service.archive_api.purge_record(record['archive_id']) is not None
    assert service.archive.execute('SELECT COUNT(*) FROM memories WHERE id=?',
                                   (record['archive_id'],)).fetchone()[0] == 0
    assert service.journal.get(record['id'])['state'] == 'archived'
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'
    code, finished = cli(service, 'purge', record['archive_id'], '--disposition', 'disposition-1')
    assert code == 0 and finished['result']['replayed'] is True
    assert finished['result']['disposition_state'] == 'settled'
    assert service.journal.get(record['id'])['durable_stage'] == 'purged'


def test_a_busy_worker_refuses_the_purge_before_any_change(service):
    record = archived(service)
    tool, _, _ = authorize(service, record['archive_id'])
    with service.config.internal('.state/worker.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(KelmoError) as refused:
            tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_BUSY'
    intact(service, record['archive_id'])
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'
    assert service.journal.get(record['id'])['state'] == 'archived'


def write_registry(service, registry, *, commit):
    """A synthetic registry in the vault's data repository, committed like the publisher would."""
    from kelmo.wiki import engine
    path = service.root / 'knowledge/provenance/registry.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(engine.encoded(registry), encoding='utf-8')
    if commit:
        engine.git(service.root, 'add', 'knowledge/provenance/registry.json')
        engine.git(service.root, 'commit', '-q', '-m', 'test: Synthetic accepted registry')


def session_registry(service):
    from kelmo.wiki import engine
    digest = hashlib.sha256(TEXT.encode()).hexdigest()
    registry = engine.load_registry(service.root)
    registry['notes'][digest] = {'filename': 'session-note.md', 'ark_id': 'another-record',
                                 'evidence_resolution': 'unresolved', 'evidence_refs': [], 'compatibility': {}}
    registry['session_applications'] = {'a' * 64: {'note': digest, 'proposal': 'b' * 64}}
    return registry, digest


def test_a_session_application_receipt_is_a_refused_dependent(service):
    record = archived(service)
    registry, digest = session_registry(service)
    write_registry(service, registry, commit=True)
    tool = issuer(service)
    plan = tool.purge_plan(record['archive_id'])
    assert 'application:' + 'a' * 64 in plan['refused'] and 'note:' + digest in plan['refused']
    with pytest.raises(KelmoError) as refused:
        tool.authorize_purge(record['archive_id'], 'disposition-1', plan['plan_sha256'])
    assert refused.value.code == 'E_CAPABILITY'
    assert service.journal.purge_disposition('disposition-1') is None
    intact(service, record['archive_id'])


# --- imported records: the mapping is dispositioned, its payload removed and a tombstone kept ---

IMPORTED = 'Imported purge canary ochremarble.'


# --- claims around the archive commit: an execution intent, never a cleared claim ---

def external_claim(service, operation_id, expires_at):
    with service.journal.conn as journal:
        journal.execute('INSERT INTO claims (operation_id, token, expires_at, local_worker) VALUES (?, ?, ?, 0)',
                        (operation_id, 'external', expires_at))


def test_a_live_external_lease_is_refused_like_an_expired_one(service):
    record = archived(service)
    waiting = service.capture(TEXT, 'leased-copy.md', 'implementer')
    external_claim(service, waiting['id'], time.time() + 3600)
    plan = issuer(service).purge_plan(record['archive_id'])
    assert 'lease:' + waiting['id'] in plan['refused']
    with pytest.raises(KelmoError) as refused:
        issuer(service).authorize_purge(record['archive_id'], 'disposition-1', plan['plan_sha256'])
    assert refused.value.code == 'E_CAPABILITY'
    intact(service, record['archive_id'])


def test_a_claim_taken_after_the_check_refuses_before_the_archive_half(service):
    record = archived(service)
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    tool, _, _ = authorize(service, record['archive_id'])
    original = ArchiveAdmin._check_disposition

    def check_then_claim(self, *args):
        original(self, *args)
        external_claim(service, waiting['id'], time.time() + 3600)
    with patch.object(ArchiveAdmin, '_check_disposition', check_then_claim):
        with pytest.raises(KelmoError) as refused:
            tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY' and 'lease:' + waiting['id'] in str(refused.value)
    intact(service, record['archive_id'])
    assert service.journal.purge_intent('disposition-1') is None
    # The external claim is never cleared by the purge.
    assert service.journal.conn.execute('SELECT token FROM claims WHERE operation_id=?',
                                        (waiting['id'],)).fetchone()[0] == 'external'


def test_no_claim_can_be_taken_while_the_purge_executes(service):
    from kelmo.archive_adapter import LocalArchiveAdapter
    record = archived(service)
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    tool, _, _ = authorize(service, record['archive_id'])
    attempts = []
    original = LocalArchiveAdapter.apply_purge

    def claim_during_apply(self, *args):
        attempts.append(service.journal.claim(waiting['id'], lease_seconds=60))
        return original(self, *args)
    with patch.object(LocalArchiveAdapter, 'apply_purge', claim_during_apply):
        report = tool.purge_report(record['archive_id'], 'disposition-1')
    assert attempts == [None]
    assert report['disposition_state'] == 'settled'
    assert service.journal.get(waiting['id'])['state'] == 'purged'
    assert service.journal.purge_intent('disposition-1')['state'] == 'finished'


DIE_BEFORE_ARCHIVE_COMMIT = (
    'import os, sys\n'
    'from kelmo import archive_adapter, cli\n'
    'archive_adapter.LocalArchiveAdapter.apply_purge = lambda *args, **kwargs: os._exit(19)\n'
    'sys.exit(cli.main(sys.argv[1:]))\n')


def test_an_intent_left_before_the_archive_commit_blocks_claims_until_the_purge_is_repeated(service):
    record = archived(service)
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    authorize(service, record['archive_id'])
    died = subprocess.run([sys.executable, '-c', DIE_BEFORE_ARCHIVE_COMMIT, 'admin', 'purge', record['archive_id'],
                           '--disposition', 'disposition-1', '--vault', str(service.root), '--json'],
                          capture_output=True, text=True, timeout=120)
    assert died.returncode == 19
    intact(service, record['archive_id'])
    assert service.journal.purge_intent('disposition-1')['state'] == 'executing'
    assert service.journal.claim(waiting['id'], lease_seconds=60) is None
    code, finished = cli(service, 'purge', record['archive_id'], '--disposition', 'disposition-1')
    assert code == 0 and finished['result']['replayed'] is False
    assert finished['result']['disposition_state'] == 'settled'
    assert service.journal.get(waiting['id'])['state'] == 'purged'


def test_a_claim_row_older_than_the_intent_is_reported_as_a_lease_not_cleared(service):
    from kelmo.journal import Journal
    record = archived(service)
    waiting = service.capture(TEXT, 'second-copy.md', 'implementer')
    tool, _, _ = authorize(service, record['archive_id'])
    with patch.object(Journal, 'event_once', side_effect=RuntimeError('crash before the journal half')):
        with pytest.raises(RuntimeError):
            tool.purge(record['archive_id'], 'disposition-1')
    # A row written around the intent (not through claim, which the intent refuses).
    external_claim(service, waiting['id'], 0)
    with pytest.raises(KelmoError) as refused:
        tool.purge(record['archive_id'], 'disposition-1')
    assert refused.value.code == 'E_CAPABILITY' and 'lease:' + waiting['id'] in str(refused.value)
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'
    assert service.journal.conn.execute('SELECT COUNT(*) FROM claims WHERE operation_id=?',
                                        (waiting['id'],)).fetchone()[0] == 1
    # Once its owner releases it, the same purge finishes.
    service.journal.release(waiting['id'], 'external')
    assert tool.purge_report(record['archive_id'], 'disposition-1')['disposition_state'] == 'settled'


def test_a_purge_under_a_stale_writer_refuses_before_any_change(service):
    record = archived(service)
    tool, _, _ = authorize(service, record['archive_id'])
    with service.archive:
        service.archive.execute('UPDATE archive_writer_state SET epoch = epoch + 1')
    try:
        with pytest.raises(KelmoError) as refused:
            tool.purge(record['archive_id'], 'disposition-1')
        assert refused.value.code == 'E_WRITER'
    finally:
        with service.archive:
            service.archive.execute('UPDATE archive_writer_state SET epoch = epoch - 1')
    intact(service, record['archive_id'])
    assert service.journal.purge_disposition('disposition-1')['state'] == 'authorized'


def test_a_settled_disposition_never_executes_again_on_a_restored_archive(service):
    record = archived(service)
    archive_id = record['archive_id']
    connection = service.archive
    row = dict(connection.execute('SELECT * FROM memories WHERE id=?', (archive_id,)).fetchone())
    tool, _, _ = authorize(service, archive_id)
    tool.purge(archive_id, 'disposition-1')
    assert service.journal.purge_disposition('disposition-1')['state'] == 'settled'
    # The archive half as an older backup holds it: the record back, no purge receipt or tombstone.
    with connection:
        connection.execute('DELETE FROM archive_purges WHERE archive_id=?', (archive_id,))
        connection.execute('DELETE FROM archive_purged_keys WHERE archive_id=?', (archive_id,))
        connection.execute(f"INSERT INTO memories ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           tuple(row.values()))
    with pytest.raises(KelmoError) as refused:
        tool.purge(archive_id, 'disposition-1')
    assert refused.value.code == 'E_STATE'
    assert connection.execute('SELECT content FROM memories WHERE id=?', (archive_id,)).fetchone()[0] == TEXT


# --- accepted knowledge is the dependency authority ---

def test_a_working_registry_edit_cannot_hide_a_committed_dependent(service):
    record = archived(service)
    registry, _ = session_registry(service)
    write_registry(service, registry, commit=False)
    with pytest.raises(KelmoError) as refused:
        issuer(service).purge_plan(record['archive_id'])
    assert refused.value.code == 'E_CONFLICT'
    write_registry(service, registry, commit=True)
    edited = dict(registry, session_applications={}, notes={})
    write_registry(service, edited, commit=False)
    with pytest.raises(KelmoError) as refused:
        issuer(service).purge_plan(record['archive_id'])
    assert refused.value.code == 'E_CONFLICT'
    intact(service, record['archive_id'])


def test_an_unfinished_wiki_transaction_refuses_planning(service):
    record = archived(service)
    marker = service.root / '.state/wiki-transaction.json'
    marker.write_text('{}', encoding='utf-8')
    try:
        with pytest.raises(KelmoError) as refused:
            issuer(service).purge_plan(record['archive_id'])
        assert refused.value.code == 'E_CONFLICT'
    finally:
        marker.unlink()


def test_an_unsettled_state_or_editor_request_is_a_refused_dependent(service):
    record = archived(service)
    other = archived(service, text='Unrelated evidence for another request.\n', name='other.md')
    journal = service.journal
    cited = journal.action_begin('key-cited', 'semantic', 'fp', service.origin, other['id'])
    journal.action_prepare(cited['operation_id'], {'evidence_refs': [{'archive_id': record['archive_id']}]})
    sourced = journal.action_begin('key-sourced', 'push', 'fp', service.origin, record['id'])
    settled = journal.action_begin('key-settled', 'push', 'fp', service.origin, record['id'])
    journal.action_refuse(settled['operation_id'], 'E_REFUSED', 'settled before the purge')
    plan = issuer(service).purge_plan(record['archive_id'])
    refused = set(plan['refused'])
    assert {'intent:' + cited['operation_id'], 'intent:' + sourced['operation_id']} <= refused
    assert 'intent:' + settled['operation_id'] not in refused
    with pytest.raises(KelmoError) as error:
        issuer(service).authorize_purge(record['archive_id'], 'disposition-1', plan['plan_sha256'])
    assert error.value.code == 'E_CAPABILITY'
    intact(service, record['archive_id'])


def test_a_purge_rewrites_the_server_search_log_as_it_does_the_archive_log(service, dispositions):
    from kelmo.store.memory import search_log
    from kelmo import telemetry
    record = archived(service)
    survivor = archived(service, 'Retained independent evidence.', 'survivor.md')
    settings = {'search_log': 'redacted', 'retention_days': 30, 'max_rows': 10}
    assert search_log.record(telemetry.ensure(service.config), settings, variants=['ranked before the purge'],
                             mode='hybrid', limit=2, filters={}, semantic=True, ms=1.0, retrieval={},
                             results=[{'id': record['archive_id']}, {'id': survivor['archive_id']}])
    tool = admin(service, dispositions)
    dispose(tool, dispositions, record['archive_id'])
    report = tool.purge_report(record['archive_id'], 'disposition-1')
    assert report['effects']['telemetry_search_log_results'] == 1
    results = search_log.read_recent(telemetry.path(service.config))[0]['results']
    assert results == [{'id': None, 'chunk_idx': None, 'purged': True},
                       {'id': survivor['archive_id'], 'chunk_idx': None}]
