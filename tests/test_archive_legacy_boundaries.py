"""Controller-only checks of legacy write and date boundaries."""
import json
from pathlib import Path
import sys

import kelmo
import pytest

sys.path.insert(0, str(Path(kelmo.__file__).resolve().parents[2]/'tests'))
import test_archive_adoption as fixtures  # noqa: E402

archive = fixtures.archive
offline = fixtures.offline
kelmo_service = fixtures.kelmo_service


@pytest.mark.parametrize('value,filename', [
    ('2026-02-30', None), ('2026-04-17\n', None), ('٢٠٢٦-04-17', None),
    (None, '2026-13-45_source.md'),
])
def test_archive_validity_date_must_be_a_real_iso_day(value, filename):
    from kelmo.store.errors import ArchiveValidationError
    from kelmo.store.memory.store import resolve_valid_from
    with pytest.raises(ArchiveValidationError):
        resolve_valid_from(value, 'raw_note', filename)


@pytest.mark.parametrize('method', ['store', 'hide', 'link'])
def test_legacy_write_helpers_leave_caller_transactions_owned_by_the_caller(archive, method, monkeypatch):
    from kelmo.store.errors import ArchiveStorageError
    from kelmo.store.memory.store import memory_store
    from kelmo import visibility, supersession
    from kelmo.errors import KelmoError
    adapter, origin, _ = archive
    monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *args, **kwargs: bytes(1536))
    first = adapter.capture(fixtures.packet(), origin, 'first')
    second = adapter.capture(fixtures.packet(b'second\n'), origin, 'second')
    calls = {
        'store': lambda: memory_store(adapter.connection, content='pending', kind='event', domain='dev'),
        'hide': lambda: visibility.hide(adapter.connection, first.archive_id),
        'link': lambda: supersession.link(adapter.connection, second.archive_id, first.archive_id),
    }
    connection = adapter.connection
    connection.execute("INSERT INTO archive_meta VALUES ('pending-helper','preserve')")
    try:
        with pytest.raises(ArchiveStorageError if method == 'store' else KelmoError):
            calls[method]()
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM archive_meta WHERE key='pending-helper'").fetchone()[0] == 'preserve'
    finally:
        connection.rollback()
    assert connection.execute("SELECT value FROM archive_meta WHERE key='pending-helper'").fetchone() is None


def test_supersession_hide_keeps_the_origin_of_the_queued_operation(kelmo_service, monkeypatch):
    from kelmo.server import OriginBindings, Enrollment
    from kelmo.service import Service
    monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *args, **kwargs: bytes(1536))
    root = kelmo_service.root
    original = kelmo_service.bindings
    bindings = OriginBindings(original.authority_id, original.writer,
                              (Enrollment('local', kelmo_service.origin.machine_tag), Enrollment('other', 'other-node')))
    kelmo_service.close()
    with Service(root, bindings=bindings, origin=bindings.origin('other')) as producer:
        old = producer.run(producer.capture('old synthetic source', 'old.md', 'implementer')['id'])
        replacement = producer.capture('replacement synthetic source', 'new.md', 'implementer',
                                       supersedes=old['archive_id'])
    with Service(root, bindings=bindings) as worker:
        result = worker.run(replacement['id'])
        assert result['state'] == 'archived'
        row = worker.archive_api.connection.execute(
            'SELECT origin FROM archive_hides WHERE archive_id=?', (old['archive_id'],)
        ).fetchone()
        assert json.loads(row['origin'])['client_id'] == 'other'


def test_invalid_source_domain_reports_input_error_and_retains_the_original_packet(kelmo_service):
    source = fixtures.packet(domain='invalid-domain')
    operation = kelmo_service.capture_source(source, kelmo_service.origin, 'invalid-domain')
    failed = kelmo_service.run(operation['id'])
    assert failed['state'] == 'failed' and failed['error_code'] == 'E_INPUT'
    assert kelmo_service.source_packet(operation['id']) == source
    assert kelmo_service.status()['archive_records'] == 0


def legacy_row(adapter, record_id, **fields):
    """A row as an older archive could store it, with no typed source attestation."""
    row = dict(id=record_id, content='legacy synthetic bytes \n', kind='event', domain='dev',
               project='fixture', tags_json='["kept"]', source='raw_note', machine_tag='legacy-node',
               created_at=fixtures.STORED, updated_at=fixtures.STORED,
               raw_note_path='notes/' + record_id + '.md', valid_from='2026-04-17', version=1)
    row.update(fields)
    with adapter.connection:
        adapter.connection.execute(f"INSERT INTO memories ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                                   tuple(row.values()))
    return row


@pytest.mark.parametrize('field,value', [
    ('valid_from', '2026-02-30'), ('valid_from', '2026-04-17\n'), ('project', ''),
    ('tags_json', '[""]'), ('machine_tag', ''), ('created_at', '2026-02-30T09:00:00+00:00'),
])
def test_unrepresentable_legacy_version_is_named_and_its_raw_row_kept(archive, field, value):
    from kelmo.store.memory.get import memory_get
    from kelmo.errors import KelmoError
    adapter, _, _ = archive
    legacy_row(adapter, 'legacy-bad', **{field: value})
    before = memory_get(adapter.connection, id='legacy-bad', full=True)
    for read in (lambda: adapter.get('legacy-bad'), lambda: adapter.get('legacy-bad', 1)):
        with pytest.raises(KelmoError) as refused:
            read()
        assert refused.value.code == 'E_CAPABILITY'
        assert 'legacy-bad' in str(refused.value) and 'version 1' in str(refused.value)
    assert adapter.public_record('legacy-bad')[field] == value
    assert memory_get(adapter.connection, id='legacy-bad', full=True) == before
    assert adapter.connection.execute('SELECT COUNT(*) FROM archive_version_sources').fetchone()[0] == 0


def test_unrepresentable_replaced_version_refuses_history_and_keeps_the_valid_head(archive):
    from kelmo.store.memory.get import memory_get
    from kelmo.errors import KelmoError
    adapter, _, _ = archive
    legacy_row(adapter, 'legacy-history', version=2, updated_at=fixtures.UPDATED)
    with adapter.connection:
        adapter.connection.execute(
            '''INSERT INTO memory_versions (memory_id,version,content,kind,domain,project,tags_json,valid_from,
               written_at,replaced_at,source,machine_tag,created_at,raw_note_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('legacy-history', 1, 'old synthetic bytes\n', 'event', 'dev', 'fixture', '["kept"]', '2026-02-30',
             fixtures.STORED, fixtures.UPDATED, 'raw_note', 'legacy-node', fixtures.STORED, 'notes/legacy-history.md'))
    assert adapter.get('legacy-history').evidence.version == 2
    for read in (lambda: adapter.get('legacy-history', 1), lambda: adapter.history('legacy-history', None, 10)):
        with pytest.raises(KelmoError) as refused:
            read()
        assert refused.value.code == 'E_CAPABILITY'
        assert 'legacy-history' in str(refused.value) and 'version 1' in str(refused.value)
    assert memory_get(adapter.connection, id='legacy-history', version=1)['valid_from'] == '2026-02-30'


def test_dated_listing_names_malformed_legacy_rows_without_dropping_them(archive):
    from kelmo.store.errors import ArchiveLegacyRecordError, ArchiveValidationError
    from kelmo.store.memory.list import memory_list
    adapter, origin, _ = archive
    valid = adapter.capture(fixtures.packet(), origin, 'valid')
    legacy_row(adapter, 'legacy-impossible', valid_from='2026-02-30')
    legacy_row(adapter, 'legacy-newline', valid_from='2026-04-17\n')
    legacy_row(adapter, 'legacy-elsewhere', valid_from='2026-02-30', project='elsewhere')
    for call in (lambda: memory_list(adapter.connection, since='2026-01-01'),
                 lambda: memory_list(adapter.connection, until='2026-12-31'),
                 lambda: memory_list(adapter.connection, order_by='valid_from')):
        with pytest.raises(ArchiveLegacyRecordError) as named:
            call()
        assert named.value.data['records'] == ['legacy-elsewhere', 'legacy-impossible', 'legacy-newline']
        assert 'legacy-impossible' in str(named.value)
    with pytest.raises(ArchiveLegacyRecordError) as scoped:
        memory_list(adapter.connection, project='elsewhere', since='2026-01-01')
    assert scoped.value.data['records'] == ['legacy-elsewhere']
    dated = memory_list(adapter.connection, kind='decision', since='2026-01-01', order_by='valid_from')
    assert [row['id'] for row in dated['items']] == [valid.archive_id] and dated['total'] == 1
    assert memory_list(adapter.connection)['total'] == 4
    assert adapter.list_records(10, 0)['total'] == 4
    assert adapter.public_record('legacy-newline')['valid_from'] == '2026-04-17\n'
    with pytest.raises(ArchiveValidationError):
        memory_list(adapter.connection, since='2026-02-30')


def test_record_listing_reports_a_malformed_creation_time_as_a_capability_gap(archive):
    from kelmo.errors import KelmoError
    adapter, origin, _ = archive
    adapter.capture(fixtures.packet(), origin, 'valid')
    legacy_row(adapter, 'legacy-created', created_at='2026-02-30T09:00:00+00:00')
    with pytest.raises(KelmoError) as refused:
        adapter.list_records(10, 0)
    assert refused.value.code == 'E_CAPABILITY' and 'legacy-created' in str(refused.value)
    assert not adapter.connection.in_transaction


@pytest.mark.parametrize('filename', ['2026-13-45_note.md', '2026-02-30_note.md'])
def test_legacy_capture_refuses_an_invalid_filename_date_before_writing_its_source(kelmo_service, filename):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as refused:
        kelmo_service.capture('synthetic dated source', filename, 'implementer')
    assert refused.value.code == 'E_INPUT'
    assert kelmo_service.orphan_sources() == 0
    assert not any((kelmo_service.root/'.state/sources').glob('*.md'))
    assert kelmo_service.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


def test_old_operation_without_representable_metadata_keeps_its_source_and_replay(kelmo_service):
    from kelmo.errors import KelmoError
    from kelmo.server import Enrollment, OriginBindings
    from kelmo.service import Service
    service = kelmo_service
    content = 'old journal synthetic source\n'
    operation = service.capture(content, 'old-source.md', 'implementer', idempotency_key='old-operation')
    # A journal written before typed metadata, whose filename date older checks accepted.
    with service.journal.conn:
        service.journal.conn.execute('DELETE FROM operation_sources WHERE operation_id=?', (operation['id'],))
        service.journal.conn.execute("UPDATE operations SET filename='2026-02-30_old.md' WHERE id=?",
                                     (operation['id'],))
    with pytest.raises(KelmoError) as refused:
        service.source_packet(operation['id'])
    assert refused.value.code == 'E_CAPABILITY' and operation['id'] in str(refused.value)
    replay = service.capture(content, '2026-02-30_old.md', 'implementer', idempotency_key='old-operation')
    assert replay['id'] == operation['id']
    failed = service.run(operation['id'])
    assert failed['state'] == 'failed' and failed['error_code'] == 'E_CAPABILITY'
    assert service.source_path(failed).read_text(encoding='utf-8') == content
    assert service.journal.source_metadata(operation['id']) is None
    assert service.status()['archive_records'] == 0
    root, original = service.root, service.bindings
    bindings = OriginBindings(original.authority_id, original.writer,
                              (Enrollment('local', service.origin.machine_tag), Enrollment('other', 'other-node')))
    service.close()
    with Service(root, bindings=bindings, origin=bindings.origin('other')) as other:
        with pytest.raises(KelmoError) as foreign:
            other.capture(content, '2026-02-30_old.md', 'implementer', idempotency_key='old-operation')
        assert foreign.value.code == 'E_IDEMPOTENCY'
