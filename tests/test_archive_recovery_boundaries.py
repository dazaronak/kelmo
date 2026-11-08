"""Controller regressions for the reviewed archive/journal ownership boundaries."""
from pathlib import Path
import sqlite3
import sys

import kelmo
import pytest

sys.path.insert(0, str(Path(kelmo.__file__).resolve().parents[2]/'tests'))
import test_archive_adoption as fixtures  # noqa: E402
from kelmo.errors import KelmoError  # noqa: E402

archive = fixtures.archive
offline = fixtures.offline
kelmo_service = fixtures.kelmo_service


@pytest.mark.parametrize('method', ['begin', 'prepare', 'finish', 'refuse'])
def test_action_journal_refuses_a_caller_transaction(kelmo_service, method):
    journal = kelmo_service.journal
    source = kelmo_service.capture_source(fixtures.packet(), kelmo_service.origin, 'source')
    request = journal.action_begin('first', 'apply', '0'*64, kelmo_service.origin, source['id'])
    calls = {
        'begin': lambda: journal.action_begin('next', 'apply', '1'*64, kelmo_service.origin, source['id']),
        'prepare': lambda: journal.action_prepare(request['operation_id'], {}),
        'finish': lambda: journal.action_finish(request['operation_id'], {}),
        'refuse': lambda: journal.action_refuse(request['operation_id'], 'E_INPUT', 'fixture'),
    }
    journal.conn.execute('BEGIN IMMEDIATE')
    try:
        journal.event(source['id'], 'caller-pending')
        with pytest.raises(KelmoError) as error:
            calls[method]()
        assert error.value.code == 'E_STATE'
        assert journal.conn.in_transaction
        assert journal.conn.execute("SELECT COUNT(*) FROM events WHERE stage='caller-pending'").fetchone()[0] == 1
    finally:
        journal.conn.rollback()
    assert journal.conn.execute("SELECT COUNT(*) FROM events WHERE stage='caller-pending'").fetchone()[0] == 0


def test_init_schema_does_not_commit_pending_caller_work(archive):
    from kelmo.store.db import init_schema
    adapter, _, _ = archive
    connection = adapter.connection
    connection.execute("INSERT INTO archive_meta VALUES ('pending-schema-caller','preserve')")
    try:
        with pytest.raises(sqlite3.ProgrammingError, match='idle'):
            init_schema(connection)
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM archive_meta WHERE key='pending-schema-caller'").fetchone()[0] == 'preserve'
    finally:
        connection.rollback()
    assert connection.execute("SELECT value FROM archive_meta WHERE key='pending-schema-caller'").fetchone() is None


def test_foreign_authority_refuses_before_creating_archive_tables(archive, tmp_path):
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.server import OriginBindings
    adapter, _, path = archive
    with adapter.connection:
        adapter.connection.execute('DROP TABLE record_visibility')
    before = list(adapter.connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    with pytest.raises(KelmoError) as error:
        LocalArchiveAdapter(path, tmp_path/'other-cache', OriginBindings.local('other-authority', 'other-node'))
    assert error.value.code == 'E_WRITER'
    assert list(adapter.connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name')) == before


@pytest.mark.parametrize('intent', ['archive', 'promote'])
def test_outcome_failure_keeps_a_recoverable_operation_and_original_error(kelmo_service, monkeypatch, intent):
    from kelmo import outcomes
    from test_service import note
    content = note(kelmo_service) if intent == 'promote' else 'Synthetic archive source.'
    operation = kelmo_service.capture(content, 'outcome.md', 'lead', intent)
    original_write = outcomes.write
    def unavailable(*args, **kwargs):
        raise KelmoError('E_OUTCOME', 'synthetic outcome failure')
    monkeypatch.setattr(outcomes, 'write', unavailable)
    failed = kelmo_service.run(operation['id'])
    assert failed['state'] == 'failed' and failed['error_code'] == 'E_OUTCOME'
    monkeypatch.setattr(outcomes, 'write', original_write)
    retried = kelmo_service.run(kelmo_service.retry(operation['id'])['id'])
    assert retried['state'] == ('applied' if intent == 'promote' else 'archived')
    assert kelmo_service.status()['archive_records'] == 1
    assert outcomes.get(kelmo_service.journal.conn, operation['id']) is not None
