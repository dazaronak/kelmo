"""Administrator verbs and opt-in search telemetry."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kelmo.archive_admin import AdminGrant, ArchiveAdmin, operator_grant
from kelmo.errors import KelmoError
from kelmo.service import Service

pytest_plugins = ['test_fact_checks']



def admin(service, *verbs, **options):
    return ArchiveAdmin(service, operator_grant(service.config, set(verbs)), **options)


def archived(service, text, name='note.md'):
    operation = service.capture(text, name, 'implementer')
    return service.run(operation['id'])


def configure(service, table):
    path = service.root / 'kelmo.toml'
    path.write_text(path.read_text() + '\n[telemetry]\n' + table)
    return Service(service.root)


def test_grants_name_their_verbs_and_cannot_be_forged(service, tmp_path):
    counted = admin(service, 'count')
    assert counted.count()['count'] == 0
    with pytest.raises(KelmoError) as refused:
        counted.stats()
    assert refused.value.code == 'E_PERMISSION'
    forged = object.__new__(AdminGrant)
    for name, value in (('principal', 'operator'), ('vault_id', service.config.vault_id),
                        ('capabilities', frozenset({'count'}))):
        object.__setattr__(forged, name, value)
    with pytest.raises(KelmoError) as refused:
        ArchiveAdmin(service, forged)
    assert refused.value.code == 'E_PERMISSION'
    Service.initialize(tmp_path / 'other', 'other')
    with Service(tmp_path / 'other') as other, pytest.raises(KelmoError) as refused:
        ArchiveAdmin(service, operator_grant(other.config, {'count'}))
    assert refused.value.code == 'E_PERMISSION'
    with pytest.raises(ValueError):
        operator_grant(service.config, {'drop-everything'})


def test_count_and_stats_show_pending_vectors_without_private_settings(service):
    archived(service, 'Statistics evidence.')
    tool = admin(service, 'count', 'stats')
    assert tool.count() == {'count': 1}
    stats = tool.stats()
    assert stats['total'] == 1 and stats['archive_records'] == 1
    assert stats['vectors_pending'] == 0 and stats['vector_index'] == 'record'
    assert stats['search_log_mode'] == 'off'
    assert str(service.root) not in json.dumps(stats)


def test_doctor_distinguishes_healthy_degraded_and_failed(service, tmp_path):
    from kelmo.store.doctor import archive_checks, overall
    assert overall([{'status': 'PASS'}, {'status': 'SKIP'}]) == 'healthy'
    assert overall([{'status': 'PASS'}, {'status': 'WARN'}]) == 'degraded'
    assert overall([{'status': 'WARN'}, {'status': 'FAIL'}]) == 'failed'
    assert overall(archive_checks(tmp_path / 'absent.db')) == 'failed'
    archived(service, 'Doctor evidence.')
    unpublished = {check['name']: check['status'] for check in admin(service, 'doctor').doctor()['checks']}
    assert unpublished['graph_snapshot'] == 'WARN'
    service.graph_rebuild()
    report = admin(service, 'doctor').doctor()
    checks = {check['name']: check['status'] for check in report['checks']}
    assert checks['archive_readable'] == 'PASS' and checks['embedding_service'] == 'SKIP'
    assert checks['chunks_all_vectored'] == 'PASS' and checks['graph_snapshot'] == 'PASS'
    assert report['status'] == 'healthy'
    with service.archive:
        service.archive.execute('DELETE FROM memories_vec')
    degraded = admin(service, 'doctor').doctor()
    assert degraded['status'] == 'degraded'


def test_backup_is_a_verified_snapshot_and_rotation_keeps_the_newest(service, tmp_path):
    archived(service, 'Backup evidence.')
    tool = admin(service, 'backup', 'dump')
    made = [tool.backup(tmp_path / 'backups', keep=2) for _ in range(3)]
    kept = sorted(path.name for path in (tmp_path / 'backups').glob('*.db'))
    assert len(kept) == 2 and made[-1]['rotated'] == [Path(made[0]['path']).name]
    assert set(kept) == {Path(item['path']).name for item in made[1:]}
    assert Path(made[-1]['path']).name in kept
    copy = sqlite3.connect(made[-1]['path'])
    assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert copy.execute('SELECT content FROM memories').fetchone()[0] == 'Backup evidence.'
    copy.close()
    dumped = tool.dump(tmp_path / 'dump.db')
    assert dumped['bytes'] > 0 and (tmp_path / 'dump.db').stat().st_mode & 0o777 == 0o600


def test_exports_never_replace_a_file_or_write_into_runtime_state(service, tmp_path):
    tool = admin(service, 'backup', 'dump')
    existing = tmp_path / 'existing.db'
    existing.write_bytes(b'user bytes')
    with pytest.raises(KelmoError) as refused:
        tool.dump(existing)
    assert refused.value.code == 'E_EXISTS' and existing.read_bytes() == b'user bytes'
    wal = service.config.archive.with_name(service.config.archive.name + '-wal')
    for target in (service.config.archive, wal, service.root / '.state/journal.sqlite3',
                   service.root / '.state/new-export.db', service.root / 'archive/new-export.db'):
        before = target.read_bytes() if target.exists() else None
        with pytest.raises(KelmoError) as refused:
            tool.dump(target)
        assert refused.value.code == 'E_INPUT'
        assert (target.read_bytes() if target.exists() else None) == before
    default = tool.backup(keep=1)
    assert Path(default['path']).parent == service.root / 'archive/backups'
    unrelated = service.root / 'archive/backups/archive-20200101-000000-deadbeef.db'
    unrelated.write_bytes(b'not made by this tool')
    tool.backup(keep=1)
    assert unrelated.read_bytes() == b'not made by this tool'
    made = [path for path in (service.root / 'archive/backups').glob('archive-*.db') if path != unrelated]
    assert len(made) == 1


def test_index_maintenance_uses_the_configured_index_or_refuses(service, tmp_path):
    tool = admin(service, 'reembed', 'backfill', 'entity-index')
    for call in (tool.reembed, tool.backfill):
        with pytest.raises(KelmoError) as refused:
            call()
        assert refused.value.code == 'E_CAPABILITY'
    assert tool.entity_index() == {'entities': 0}


def test_search_telemetry_is_off_until_configured(service):
    archived(service, 'Telemetry evidence basalt.')
    service.search('basalt')
    assert admin(service, 'search-log').search_log()['searches'] == []


def test_redacted_telemetry_keeps_hashes_identity_and_ranked_ids(service):
    record = archived(service, 'Telemetry evidence cedar.')
    with configure(service, 'search_log = "redacted"\n') as logging_service, \
            patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        logging_service.search('cedar', client='synthetic/1')
        searches = admin(logging_service, 'search-log').search_log()['searches']
    assert len(searches) == 1
    entry = searches[0]
    assert (entry['query_en'], entry['query_ru'], entry['privacy'], entry['client']) == ('', '', 'redacted',
                                                                                        'synthetic/1')
    assert entry['variants'] == [{'sha256': hashlib.sha256(b'cedar').hexdigest(), 'chars': 5}]
    assert entry['retrieval']['index_identity'] == 'core-record-index/1'
    assert entry['retrieval']['contract'] == 'adoption/1' and entry['retrieval']['model']
    assert [item['id'] for item in entry['results']] == [record['archive_id']]
    assert 'cedar' not in json.dumps(entry)


def test_full_telemetry_is_bounded_by_its_row_limit(service):
    archived(service, 'Telemetry evidence quartz.')
    with configure(service, 'search_log = "full"\nmax_rows = 2\n') as logging_service:
        for query in ('quartz', 'evidence', 'telemetry'):
            logging_service.search(query, project='default')
        searches = admin(logging_service, 'search-log').search_log()['searches']
    assert [entry['query_en'] for entry in searches] == ['evidence', 'telemetry']
    assert searches[0]['filters'] == {'project': 'default'}


def test_a_failed_log_write_never_fails_the_search_and_leaves_no_row(service):
    archived(service, 'Telemetry evidence willow.')
    with configure(service, 'search_log = "full"\n') as logging_service:
        with patch('kelmo.store.memory.search_log.prune', side_effect=sqlite3.OperationalError('disk I/O error')):
            found = logging_service.search('willow', with_metadata=True)
        assert [item['content'] for item in found['items']] == ['Telemetry evidence willow.']
        assert admin(logging_service, 'search-log').search_log()['searches'] == []


def test_logging_never_commits_or_ends_the_callers_transaction(service):
    archived(service, 'Telemetry evidence delta.')
    with configure(service, 'search_log = "full"\n') as logging_service:
        connection = logging_service.archive
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("INSERT INTO archive_meta (key, value) VALUES ('caller.probe', '1')")
        logging_service.search('delta')
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT 1 FROM archive_meta WHERE key='caller.probe'").fetchone() is None
        assert admin(logging_service, 'search-log').search_log()['searches'] == []


def test_telemetry_configuration_is_validated(service):
    for table in ('search_log = "everything"\n', 'retention_days = 0\n', 'unknown = 1\n'):
        path = service.root / 'kelmo.toml'
        original = path.read_text()
        path.write_text(original + '\n[telemetry]\n' + table)
        with pytest.raises(KelmoError) as refused:
            Service(service.root)
        assert refused.value.code == 'E_CONFIG'
        path.write_text(original)


def test_cli_admin_keeps_the_old_text_shapes_and_refuses_purge(service):
    record = archived(service, 'CLI administrator evidence.')

    def run(*args):
        return subprocess.run([sys.executable, '-m', 'kelmo.cli', 'admin', *map(str, args),
                               '--vault', str(service.root)], capture_output=True, text=True, timeout=60)
    assert run('count').stdout.strip() == '1'
    dumped = subprocess.run([sys.executable, '-m', 'kelmo.cli', 'admin', 'dump', '--vault', str(service.root)],
                            capture_output=True, timeout=60)
    assert dumped.returncode == 0 and dumped.stdout.startswith(b'SQLite format 3\x00')
    refused = run('purge', record['archive_id'], '--disposition', 'synthetic', '--json')
    assert refused.returncode != 0
    assert json.loads(refused.stdout)['error']['code'] == 'E_CAPABILITY'
    assert service.show(record['archive_id'])['content'] == 'CLI administrator evidence.'


def schema_and_owner(path):
    connection = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    try:
        return (sorted(connection.execute('SELECT type, name FROM sqlite_master').fetchall()),
                connection.execute('SELECT * FROM archive_writer_state').fetchall())
    finally:
        connection.close()


def test_read_verbs_work_without_the_writer_and_change_nothing(service, tmp_path):
    from kelmo.config import Config
    archived(service, 'Foreign epoch evidence.')
    with service.archive:
        service.archive.execute('UPDATE archive_writer_state SET epoch = epoch + 7')
    with pytest.raises(KelmoError) as refused:
        Service(service.root)
    assert refused.value.code == 'E_WRITER'
    before = schema_and_owner(service.config.archive)
    tool = ArchiveAdmin(Config.load(service.root), operator_grant(service.config,
                                                                  {'count', 'stats', 'doctor', 'dump', 'search-log'}))
    assert tool.count() == {'count': 1}
    assert tool.stats()['total'] == 1
    assert tool.doctor()['status'] in ('healthy', 'degraded')
    assert tool.dump(tmp_path / 'foreign.db')['bytes'] > 0
    assert tool.search_log()['searches'] == []
    assert schema_and_owner(service.config.archive) == before
    with pytest.raises(KelmoError) as refused:
        ArchiveAdmin(Config.load(service.root), operator_grant(service.config, {'graph'})).graph()
    assert refused.value.code == 'E_STATE'


def test_a_missing_archive_is_a_typed_diagnostic(tmp_path):
    Service.initialize(tmp_path / 'broken', 'broken')
    (tmp_path / 'broken/archive/archive.db').unlink()

    def run(*args):
        return subprocess.run([sys.executable, '-m', 'kelmo.cli', 'admin', *args, '--vault',
                               str(tmp_path / 'broken'), '--json'], capture_output=True, text=True, timeout=60)
    failed = run('doctor')
    doctor = json.loads(failed.stdout)
    assert failed.returncode == 1
    assert doctor['ok'] is False and doctor['error']['code'] == 'E_HEALTH'
    checks = {check['name']: check['status'] for check in doctor['result']['checks']}
    assert checks['archive_readable'] == 'FAIL'
    for verb in (('count',), ('stats',), ('backup',), ('search-log',),
                 ('dump', '--output', str(tmp_path / 'never.db'))):
        refused = run(*verb)
        assert refused.returncode != 0 and json.loads(refused.stdout)['error']['code'] == 'E_STATE', verb
    assert not (tmp_path / 'never.db').exists()
    assert not (tmp_path / 'broken/archive/archive.db').exists()


def test_configured_retention_applies_when_the_operator_reads(service):
    from datetime import datetime, timedelta, timezone
    archived(service, 'Retention evidence.')
    with configure(service, 'search_log = "full"\nretention_days = 1\n') as logging_service:
        logging_service.search('retention')
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        connection = sqlite3.connect(logging_service.config.archive)
        with connection:
            connection.execute('INSERT INTO search_log (at, query_en, query_ru, mode, "limit", semantic) '
                               "VALUES (?, 'old archive query', '', 'hybrid', 10, 1)", (old,))
        connection.close()
        searches = admin(logging_service, 'search-log').search_log()['searches']
    assert [entry['query_en'] for entry in searches] == ['retention']


def test_a_logged_row_commits_on_its_own_and_survives_a_later_caller_rollback(service):
    archived(service, 'Independent telemetry evidence.')
    with configure(service, 'search_log = "redacted"\n') as logging_service:
        logging_service.search('independent')
        connection = logging_service.archive
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("INSERT INTO archive_meta (key, value) VALUES ('caller.probe', '1')")
        connection.rollback()
        assert len(admin(logging_service, 'search-log').search_log(1)['searches']) == 1


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """The fixture vault: a real local provider over synthetic pinned files, fastembed
    replaced by the bag-of-words FakeBackend. No model is called."""
    import test_retrieval_integration as retrieval
    fake = retrieval.FakeBackend()
    monkeypatch.setattr('kelmo.embeddings.backend_model_description', fake.description)
    monkeypatch.setattr('kelmo.embeddings.load_text_embedding', fake.load)
    retrieval.git_identity(monkeypatch)
    root = tmp_path / 'configured'
    Service.initialize(root, 'fixture-node')
    retrieval.set_embedding(root, retrieval.local_section(retrieval.model_files(root / '.cache/embeddings/custom')))
    with Service(root) as opened:
        yield opened, fake


def test_maintenance_verbs_succeed_on_a_configured_index(configured):
    opened, fake = configured
    archived(opened, 'Configured maintenance alpha evidence.', 'alpha.md')
    fake.down = True
    archived(opened, 'Configured maintenance beta evidence.', 'beta.md')
    fake.down = False
    tool = admin(opened, 'stats', 'backfill', 'reembed', 'doctor')
    pending = tool.stats()
    assert pending['vector_index'] == 'chunk' and pending['vectors_pending'] > 0
    tool.backfill(batch=4)
    assert tool.stats()['vectors_pending'] == 0
    rebuilt = tool.reembed(force=True)
    assert rebuilt['failed'] == 0 and rebuilt['profile_id'] == pending['profile_id']
    assert tool.stats()['vectors_pending'] == 0
    checks = {check['name']: check['status'] for check in tool.doctor(probe_embedding=True)['checks']}
    assert checks['embedding_service'] == 'PASS' and checks['tokenizer_present'] == 'PASS'
    assert checks['chunks_all_vectored'] == 'PASS' and checks['memories_all_chunked'] == 'PASS'


def transported(root):
    """The archive as a server holds it: the same records behind the worker transport, not the local
    adapter."""
    from kelmo.config import Config
    from kelmo.server import open_archive
    config = Config.load(root)
    bindings = config.local_bindings()
    return Service(root, archive_api=open_archive(config, bindings, 'in-process'), bindings=bindings)


def test_a_server_logs_its_searches_beside_the_journal_never_inside_the_archive(service):
    from kelmo import telemetry
    record = archived(service, 'Telemetry evidence juniper.')
    with configure(service, 'search_log = "redacted"\n') as local, \
            patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        with transported(service.root) as server:
            server.search('juniper', client='ignored/1')
        from kelmo.store.memory.search_log import read_recent
        assert read_recent(local.config.archive) == [], 'a server search was logged inside the archive'
        assert telemetry.path(local.config).stat().st_mode & 0o077 == 0, 'the log is owner-only'
        tool = admin(local, 'search-log', 'stats')
        searches = tool.search_log()['searches']
        assert tool.stats()['server_searches_logged'] == 1
    [entry] = searches
    assert entry['client'] == 'local', 'the enrolled client the search came from, not a payload label'
    assert [item['id'] for item in entry['results']] == [record['archive_id']]
    assert 'juniper' not in json.dumps(entry), 'redacted mode keeps no query text'


def test_a_server_logs_nothing_while_telemetry_is_off(service):
    from kelmo import telemetry
    archived(service, 'Telemetry evidence larch.')
    with transported(service.root) as server:
        server.search('larch')
    assert not telemetry.path(service.config).exists()
