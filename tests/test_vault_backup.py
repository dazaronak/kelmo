"""The whole-vault backup: verified snapshot, retention, restore drill and its age in doctor."""
import json
import sqlite3

import pytest

from kelmo import snapshot as snap
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.service import Service
from test_post_cutover_recovery import cutover_vault  # noqa: F401


def _telemetry(config):
    from kelmo import telemetry
    with sqlite3.connect(telemetry.ensure(config)) as connection:
        connection.execute('CREATE TABLE search_log (id INTEGER PRIMARY KEY, query TEXT)')
        connection.execute("INSERT INTO search_log (query) VALUES ('redacted')")


def test_backup_restores_into_fresh_roots_with_every_source(tmp_path, cutover_vault):  # noqa: F811
    control = cutover_vault['ctl_path']
    with Service(control) as service:
        operation = service.capture('Evidence written after cutover.', 'after.md', 'implementer')
        service.run(operation['id'])
    config = Config.load(control)
    _telemetry(config)

    preview = snap.vault_sources(config)
    assert set(preview) >= {'archive', 'journal', 'vault', 'telemetry'}
    result = snap.backup(control, tmp_path / 'backups')
    backup = tmp_path / 'backups' / result['path'].rsplit('/', 1)[1]
    assert snap.verify_snapshot(backup)['id'] == result['snapshot'] and result['records'] >= 2
    assert snap.last_backup(config)['snapshot'] == result['snapshot']

    plan = json.loads((backup / snap.RESTORE_PLAN).read_text())
    assert plan['state_telemetry'] == 'telemetry' and plan['authority_id'] == config.vault_id
    roots = {name: tmp_path / 'drill' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    state = snap.import_snapshot(backup, tmp_path / 'drill-control', roots=roots, **plan)
    assert 'verify' in state['steps'] and 'telemetry' in state['steps']
    restored = Config.load(tmp_path / 'drill-control')
    assert snap.archive_census(restored.archive) == snap.archive_census(config.archive)
    assert (snap.sqlite_digest(restored.state / 'telemetry.sqlite3')
            == snap.sqlite_digest(config.state / 'telemetry.sqlite3'))


def test_a_backup_with_delta_session_snapshots_restores(tmp_path, cutover_vault):  # noqa: F811
    from kelmo.sessions import Sessions
    control = cutover_vault['ctl_path']
    config = Config.load(control)
    root = tmp_path / 'native-sessions'
    root.mkdir()
    transcript = root / 'session.jsonl'
    record = b'{"type":"user","message":{"role":"user","content":"Turn %d."}}\n'
    first = b''.join(record % turn for turn in range(3))
    grown = first + b''.join(record % turn for turn in range(3, 6))
    sessions = Sessions(config)
    try:
        transcript.write_bytes(first)
        sessions.capture('claude', 'growing', transcript, root, 'PreCompact')
        transcript.write_bytes(grown)
        delta = sessions.capture('claude', 'growing', transcript, root, 'PreCompact')['snapshot_hash']
        assert sessions.journal.conn.execute('SELECT base_hash FROM session_snapshot_storage WHERE hash=?',
                                             (delta,)).fetchone()[0]
    finally:
        sessions.close()

    result = snap.backup(control, tmp_path / 'backups')
    plan = json.loads((tmp_path / 'backups' / result['path'].rsplit('/', 1)[1] / snap.RESTORE_PLAN).read_text())
    roots = {name: tmp_path / 'drill' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    snap.import_snapshot(result['path'], tmp_path / 'drill-control', roots=roots, **plan)
    restored = Sessions(Config.load(tmp_path / 'drill-control'))
    try:
        assert restored.snapshot(delta) == grown
    finally:
        restored.close()


def test_a_session_snapshot_needs_its_file_and_a_base_chain_to_a_whole_file():
    whole, delta, other = 'a' * 64, 'b' * 64, 'c' * 64
    files = {f'transcripts/{whole}.jsonl': whole + ':600', f'transcripts/{delta}.jsonl': 'f' * 64 + ':600'}
    hashes = {whole, delta}
    assert snap._snapshot_stored(delta, hashes, {delta: whole}, files)
    assert not snap._snapshot_stored(delta, hashes, {}, files), 'a delta file never hashes to its identity'
    assert not snap._snapshot_stored(delta, hashes, {delta: other}, files), 'its base is not a snapshot'
    assert not snap._snapshot_stored(delta, hashes, {delta: whole, whole: delta}, files), 'a cycle'
    assert not snap._snapshot_stored(whole, hashes, {}, {}), 'no file at all'


def test_retention_keeps_the_newest_and_doctor_reports_the_age(tmp_path, cutover_vault):  # noqa: F811
    from kelmo.doctor import diagnose
    control = cutover_vault['ctl_path']
    first = snap.backup(control, tmp_path / 'backups', keep=1)
    second = snap.backup(control, tmp_path / 'backups', keep=1)
    assert second['rotated'] == [first['path'].rsplit('/', 1)[1]]
    assert sorted(path.name for path in (tmp_path / 'backups').iterdir()) == sorted(
        [snap.BACKUP_INDEX, second['path'].rsplit('/', 1)[1]])
    report = diagnose(control)
    assert report['backup']['snapshot'] == second['snapshot'] and report['backup']['age_hours'] < 1
    assert not any('vault backup' in warning for warning in report['warnings'])

    config = Config.load(control)
    record = json.loads((config.state / snap.LAST_BACKUP).read_text())
    (config.state / snap.LAST_BACKUP).write_text(json.dumps(dict(record, verified_at='2020-01-01T00:00:00+00:00')))
    assert any('vault backup' in warning for warning in diagnose(control)['warnings'])


def test_a_failed_backup_leaves_no_copy_and_no_record(tmp_path, cutover_vault, monkeypatch):  # noqa: F811
    def corrupted(destination):
        raise KelmoError('E_SNAPSHOT', 'The copy fails verification.')

    monkeypatch.setattr(snap, 'verify_snapshot', corrupted)
    with pytest.raises(KelmoError, match='fails verification'):
        snap.backup(cutover_vault['ctl_path'], tmp_path / 'backups')
    assert list((tmp_path / 'backups').iterdir()) == []
    assert snap.last_backup(Config.load(cutover_vault['ctl_path'])) is None


def test_backup_refuses_a_destination_inside_the_vault(cutover_vault):  # noqa: F811
    config = Config.load(cutover_vault['ctl_path'])
    with pytest.raises(KelmoError, match='outside the vault'):
        snap.backup(cutover_vault['ctl_path'], config.state / 'backups')
    assert snap.last_backup(config) is None


def test_cli_previews_and_requires_stopped_writers(tmp_path, cutover_vault, capsys):  # noqa: F811
    from kelmo.cli import call
    control = str(cutover_vault['ctl_path'])
    destination = str(tmp_path / 'backups')
    assert call(['snapshot', 'backup', '--vault', control, '--destination', destination, '--json']) == 0
    preview = json.loads(capsys.readouterr().out)['result']
    assert preview['applied'] is False and preview['last_backup'] is None
    assert call(['snapshot', 'backup', '--vault', control, '--destination', destination, '--apply', '--json']) != 0
    assert 'writers-stopped' in capsys.readouterr().out
    assert call(['snapshot', 'backup', '--vault', control, '--destination', destination, '--apply',
                 '--writers-stopped', '--json']) == 0
    made = json.loads(capsys.readouterr().out)['result']
    assert made['applied'] is True
    # The drill: restore.json plus fresh roots is a complete `snapshot import` specification.
    plan = json.loads((tmp_path / 'backups' / made['path'].rsplit('/', 1)[1] / snap.RESTORE_PLAN).read_text())
    plan['roots'] = {name: str(tmp_path / 'drill' / name) for name in ('state_root', 'archive_root',
                                                                       'knowledge_git_root')}
    (tmp_path / 'spec.json').write_text(json.dumps(plan))
    assert call(['snapshot', 'import', '--vault', str(tmp_path / 'drill-control'), '--snapshot', made['path'],
                 '--spec', str(tmp_path / 'spec.json'), '--apply', '--json']) == 0
    assert 'verify' in json.loads(capsys.readouterr().out)['result']['steps']
