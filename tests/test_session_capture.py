"""Session input is owned durably before a lifecycle callback returns."""
import json

import pytest

from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.service import Service


def lines(provider, count=1):
    records = []
    for index in range(count):
        if provider == 'claude':
            row = {'type': 'user', 'message': {'role': 'user', 'content': f'Request {index}'}}
        else:
            row = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': f'Request {index}'}]}}
        records.append(json.dumps(row).encode() + b'\n')
    return b''.join(records)


def setup(tmp_path):
    from kelmo.sessions import Sessions
    root = tmp_path / 'vault'
    Service.initialize(root, 'test')
    enrolled = tmp_path / 'provider-sessions'
    enrolled.mkdir()
    source = enrolled / 'session.jsonl'
    source.write_bytes(lines('claude'))
    return Sessions(Config.load(root)), enrolled, source


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_snapshot_survives_provider_file_removal_and_reopen(tmp_path, provider):
    from kelmo.sessions import Sessions
    sessions, enrolled, source = setup(tmp_path)
    raw = lines(provider)
    source.write_bytes(raw)
    first = sessions.capture(provider, 'native-1', source, enrolled, 'PreCompact')
    job = sessions.job(first['job_id'])
    assert job['state'] == 'pending'
    assert sessions.snapshot(job['snapshot_hash']) == raw
    config = sessions.config
    sessions.close()
    source.unlink()
    resumed = Sessions(config)
    assert resumed.snapshot(job['snapshot_hash']) == raw
    assert resumed.job(first['job_id'])['state'] == 'pending'
    resumed.close()


def test_duplicate_checkpoint_and_final_events_reuse_jobs(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    first = sessions.capture('claude', 'native', source, enrolled, 'Stop', checkpoint_turns=1)
    forced = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert forced['job_id'] == first['job_id']
    final = sessions.capture('claude', 'native', source, enrolled, 'SessionEnd')
    assert final['job_id'] != first['job_id']
    assert sessions.capture('claude', 'native', source, enrolled, 'SessionEnd')['job_id'] == final['job_id']
    assert len(sessions.list_jobs()) == 2
    sessions.close()


def test_checkpoint_threshold_and_append_coverage(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    assert sessions.capture('claude', 'native', source, enrolled, 'Stop')['job_id'] is None
    source.write_bytes(lines('claude', 30))
    first = sessions.capture('claude', 'native', source, enrolled, 'Stop')
    assert sessions.job(first['job_id'])['start_record'] == 0
    assert sessions.job(first['job_id'])['end_record'] == 30
    source.write_bytes(lines('claude', 31))
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['generation'] == first['generation']
    assert sessions.job(second['job_id'])['start_record'] == 30
    assert sessions.job(second['job_id'])['end_record'] == 31
    sessions.close()


def test_partial_tail_is_preserved_but_not_scheduled_as_a_record(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    complete = lines('claude')
    source.write_bytes(complete + b'{"type":')
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert first['partial_tail'] is True
    assert sessions.job(first['job_id'])['end_record'] == 1
    assert sessions.snapshot(first['snapshot_hash']) == source.read_bytes()
    source.write_bytes(complete + lines('claude'))
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['generation'] == first['generation']
    assert sessions.job(second['job_id'])['start_record'] == 1
    sessions.close()


@pytest.mark.parametrize('change', ['truncate', 'replace'])
def test_nonappend_transcript_starts_new_generation(tmp_path, change):
    sessions, enrolled, source = setup(tmp_path)
    source.write_bytes(lines('claude', 3))
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    if change == 'replace':
        # Keep the old inode allocated until replacement so it cannot be recycled.
        replacement = source.with_suffix('.replacement')
        replacement.write_bytes(lines('claude', 4))
        assert replacement.stat().st_ino != source.stat().st_ino
        replacement.replace(source)
    else:
        source.write_bytes(lines('claude', 1))
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['generation'] == first['generation'] + 1
    assert sessions.job(second['job_id'])['start_record'] == 0
    sessions.close()


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'directory', 'fifo', 'malformed'])
def test_invalid_input_creates_no_job(tmp_path, kind):
    import os
    sessions, enrolled, source = setup(tmp_path)
    if kind == 'outside':
        source = tmp_path / 'outside.jsonl'
        source.write_bytes(lines('claude'))
    elif kind == 'symlink':
        source.unlink()
        source.symlink_to(tmp_path / 'outside.jsonl')
    elif kind == 'directory':
        source.unlink()
        source.mkdir()
    elif kind == 'fifo':
        source.unlink()
        os.mkfifo(source)
    else:
        source.write_bytes(b'not-json\n')
    with pytest.raises(KelmoError):
        sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert sessions.list_jobs() == []
    sessions.close()


def _stored(sessions, checksum):
    return sessions.config.internal('.state/sessions/transcripts/' + checksum + '.jsonl').read_bytes()


def _storage(sessions, checksum):
    row = sessions.journal.conn.execute('SELECT * FROM session_snapshot_storage WHERE hash=?', (checksum,)).fetchone()
    return None if row is None else (row['base_hash'], row['base_bytes'])


def test_a_growing_transcript_stores_each_snapshot_as_its_delta(tmp_path):
    """A transcript has no size ceiling because a capture stores only the bytes it adds; every snapshot
    still reads back whole and checked against its identity, and a partial tail is replaced by its
    completion rather than kept."""
    sessions, enrolled, source = setup(tmp_path)
    first_raw = lines('claude', 3)
    source.write_bytes(first_raw)
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    grown = first_raw + lines('claude', 2)
    partial = grown[:-7]  # the last record is cut; its bytes are not a base for anything
    source.write_bytes(partial)
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    source.write_bytes(grown)
    third = sessions.capture('claude', 'native', source, enrolled, 'SessionEnd')
    assert [first['generation'], second['generation'], third['generation']] == [1, 1, 1]
    assert second['partial_tail'] is True and third['partial_tail'] is False

    assert _stored(sessions, first['snapshot_hash']) == first_raw
    assert _storage(sessions, first['snapshot_hash']) == (None, None)
    assert _stored(sessions, second['snapshot_hash']) == partial[len(first_raw):]
    assert _storage(sessions, second['snapshot_hash']) == (first['snapshot_hash'], len(first_raw))
    complete = len(grown) - len(lines('claude', 2)) + len(lines('claude', 1))  # the second snapshot's complete records
    assert _stored(sessions, third['snapshot_hash']) == grown[complete:]
    assert _storage(sessions, third['snapshot_hash']) == (second['snapshot_hash'], complete)

    for result, raw in ((first, first_raw), (second, partial), (third, grown)):
        assert sessions.snapshot(result['snapshot_hash']) == raw
    config = sessions.config
    sessions.close()
    reopened = Sessions_(config)
    try:
        assert reopened.snapshot(third['snapshot_hash']) == grown
    finally:
        reopened.close()


def Sessions_(config):
    from kelmo.sessions import Sessions
    return Sessions(config)


def test_a_rewritten_transcript_starts_a_generation_stored_whole(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    first_raw = lines('claude', 2)
    source.write_bytes(first_raw)
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    rewritten = lines('claude', 2).replace(b'Request 0', b'Request 9')
    source.write_bytes(rewritten)  # same file, same length, different bytes
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['generation'] == first['generation'] + 1
    assert _stored(sessions, second['snapshot_hash']) == rewritten
    assert _storage(sessions, second['snapshot_hash']) == (None, None)
    assert sessions.snapshot(first['snapshot_hash']) == first_raw
    sessions.close()


def test_a_snapshot_without_a_storage_row_reads_whole_and_still_serves_as_a_base(tmp_path):
    """Snapshots captured before storage rows existed are whole files; a later capture compares against
    their bytes and chains on them."""
    sessions, enrolled, source = setup(tmp_path)
    first_raw = lines('claude', 2)
    source.write_bytes(first_raw)
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    with sessions.journal.conn as db:
        db.execute('DELETE FROM session_snapshot_storage WHERE hash=?', (first['snapshot_hash'],))
    grown = first_raw + lines('claude', 1)
    source.write_bytes(grown)
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['generation'] == 1
    assert _storage(sessions, second['snapshot_hash']) == (first['snapshot_hash'], len(first_raw))
    assert sessions.snapshot(first['snapshot_hash']) == first_raw
    assert sessions.snapshot(second['snapshot_hash']) == grown
    sessions.close()


def test_a_file_left_by_an_interrupted_capture_is_replaced(tmp_path):
    import hashlib
    sessions, enrolled, source = setup(tmp_path)
    first_raw = lines('claude', 2)
    source.write_bytes(first_raw)
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    grown = first_raw + lines('claude', 1)
    checksum = hashlib.sha256(grown).hexdigest()
    sessions.config.internal('.state/sessions/transcripts/' + checksum + '.jsonl').write_bytes(b'torn')
    source.write_bytes(grown)
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert second['snapshot_hash'] == checksum
    assert _stored(sessions, checksum) == grown[len(first_raw):]
    assert sessions.snapshot(checksum) == grown and sessions.snapshot(first['snapshot_hash']) == first_raw
    sessions.close()


def test_a_damaged_delta_file_is_reported_not_served(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    first_raw = lines('claude', 2)
    source.write_bytes(first_raw)
    first = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    source.write_bytes(first_raw + lines('claude', 1))
    second = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    sessions.config.internal('.state/sessions/transcripts/' + first['snapshot_hash'] + '.jsonl').write_bytes(b'x')
    for checksum in (first['snapshot_hash'], second['snapshot_hash']):
        with pytest.raises(KelmoError) as error:
            sessions.snapshot(checksum)
        assert error.value.code == 'E_STATE'
    sessions.close()


def test_schema_upgrade_preserves_old_operations_and_creates_backup(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    config = sessions.config
    sessions.close()
    backups = list((config.state / 'backups').glob('sessions-v1-*.sqlite3'))
    assert len(backups) == 1
    import sqlite3
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    from kelmo.sessions import Sessions
    resumed = Sessions(config)
    assert len(list((config.state / 'backups').glob('sessions-v1-*.sqlite3'))) == 1
    resumed.close()


def test_unknown_schema_version_is_rejected(tmp_path):
    from kelmo.sessions import Sessions
    sessions, _, _ = setup(tmp_path)
    config = sessions.config
    with sessions.journal.conn:
        sessions.journal.conn.execute('UPDATE session_schema SET version=999')
    sessions.close()
    with pytest.raises(KelmoError):
        Sessions(config)


def test_snapshot_tamper_is_reported(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    captured = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    path = sessions.config.state / 'sessions/transcripts' / (captured['snapshot_hash'] + '.jsonl')
    path.write_bytes(b'changed')
    with pytest.raises(KelmoError):
        sessions.snapshot(captured['snapshot_hash'])
    sessions.close()


def test_providers_with_same_native_id_are_distinct(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    a = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    source.write_bytes(lines('codex'))
    b = sessions.capture('codex', 'native', source, enrolled, 'PreCompact')
    assert a['session_key'] != b['session_key']
    sessions.close()


def test_empty_transcript_is_visible_without_extraction_job(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    source.write_bytes(b'')
    result = sessions.capture('claude', 'native', source, enrolled, 'SessionEnd')
    assert result['job_id'] is None
    assert result['records'] == 0
    sessions.close()


def test_snapshot_written_before_journal_crash_is_rewritten_and_recorded(tmp_path, monkeypatch):
    sessions, enrolled, source = setup(tmp_path)
    save = sessions._save_snapshot

    def interrupt(checksum, raw, base):
        save(checksum, raw, base)
        raise SystemExit(99)

    monkeypatch.setattr(sessions, '_save_snapshot', interrupt)
    with pytest.raises(SystemExit):
        sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert sessions.list_jobs() == []
    paths = list((sessions.config.state / 'sessions/transcripts').glob('*.jsonl'))
    assert len(paths) == 1
    monkeypatch.setattr(sessions, '_save_snapshot', save)
    result = sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert result['snapshot_hash'] == paths[0].stem
    assert len(sessions.list_jobs()) == 1
    sessions.close()


def test_expired_deadline_does_not_accept_job(tmp_path, monkeypatch):
    from kelmo import sessions as module
    sessions, enrolled, source = setup(tmp_path)
    times = iter([0, 2, 2, 2])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(times, 2))
    with pytest.raises(KelmoError) as error:
        sessions.capture('claude', 'native', source, enrolled, 'PreCompact', time_budget=1)
    assert error.value.code == 'E_CAPTURE_RETRY'
    assert sessions.list_jobs() == []
    sessions.close()


def test_existing_operation_is_preserved_in_migration_backup(tmp_path):
    from kelmo.sessions import Sessions
    import sqlite3
    root = tmp_path / 'vault'
    Service.initialize(root, 'test')
    with Service(root) as service:
        operation = service.capture('Unprocessed evidence.', 'kept.md', role='collector')
    sessions = Sessions(Config.load(root))
    assert sessions.journal.get(operation['id'])['state'] == 'captured'
    backup = next((root / '.state/backups').glob('sessions-v1-*.sqlite3'))
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT id FROM operations').fetchone()[0] == operation['id']
    sessions.close()


def test_concurrent_intake_lock_is_retryable(tmp_path):
    sessions, enrolled, source = setup(tmp_path)
    with sessions._lock():
        with pytest.raises(KelmoError) as error:
            sessions.capture('claude', 'native', source, enrolled, 'PreCompact')
    assert error.value.code == 'E_BUSY'
    assert sessions.list_jobs() == []
    sessions.close()


def test_busy_journal_does_not_outlive_capture_deadline(tmp_path):
    import sqlite3
    import time
    sessions, enrolled, source = setup(tmp_path)
    writer = sqlite3.connect(sessions.config.state / 'journal.sqlite3')
    writer.execute('BEGIN IMMEDIATE')
    started = time.monotonic()
    try:
        with pytest.raises(KelmoError) as error:
            sessions.capture('claude', 'native', source, enrolled, 'PreCompact', time_budget=0.1)
        assert error.value.code in ('E_BUSY', 'E_CAPTURE_RETRY')
        assert time.monotonic() - started < 1
    finally:
        writer.rollback()
        writer.close()
        sessions.close()
