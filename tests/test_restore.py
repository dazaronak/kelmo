"""A closed vault keeps immutable work resumable at a new location."""
import shutil
import io
from unittest.mock import patch

import pytest

from kelmo.service import Service


def test_source_round_trip_does_not_depend_on_locale_encoding(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    monkeypatch.setattr(io, 'text_encoding', lambda encoding, stacklevel=2: encoding or 'ascii')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)), Service(root) as service:
        content = 'Zażółć gęślą jaźń. Память сохраняет условия. 日本語.'
        operation = service.capture(content, 'unicode.md', 'lead')
        assert service.run(operation['id'])['state'] == 'archived'
        assert service.capture(content, 'unicode.md', 'lead')['id'] == operation['id']


def test_closed_vault_relocation_resumes_queued_work_without_old_location(tmp_path):
    original = tmp_path / 'original'
    Service.initialize(original, 'fixture', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        with Service(original) as service:
            done = service.capture('Completed synthetic evidence.', 'done.md', 'lead')
            assert service.run(done['id'])['state'] == 'archived'
            queued = service.capture('Queued synthetic evidence.', 'queued.md', 'lead')
        moved = tmp_path / 'moved'
        shutil.copytree(original, moved)
        shutil.rmtree(original)
        with Service(moved) as service:
            assert service.run(queued['id'])['state'] == 'archived'
            assert service.run(done['id'])['state'] == 'archived'
            assert service.status()['archive_records'] == 2
            assert service.run(queued['id'])['state'] == 'archived'
            assert service.status()['archive_records'] == 2


@pytest.mark.parametrize('legacy', [False, True])
def test_source_identity_survives_relocation_for_new_and_legacy_records(tmp_path, legacy):
    original = tmp_path / 'original'
    Service.initialize(original, 'fixture', project='fixture')
    with Service(original) as service:
        queued = service.capture('Synthetic portable evidence.', 'queued.md', 'lead')
        if legacy:
            with service.journal.conn:
                service.journal.conn.execute('UPDATE operations SET source_path=? WHERE id=?',
                    (str(original / '.state/sources' / (queued['source_hash'] + '.md')), queued['id']))
    moved = tmp_path / 'moved'
    original.rename(moved)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)), Service(moved) as service:
        assert service.run(queued['id'])['state'] == 'archived'
        assert service.status()['archive_records'] == 1


@pytest.mark.parametrize('tamper', ['traversal', 'wrong_hash', 'symlink', 'parent_symlink'])
def test_portable_source_identity_rejects_unsafe_paths(tmp_path, tamper):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    with Service(root) as service:
        queued = service.capture('Immutable synthetic evidence.', 'queued.md', 'lead')
        source = service.source_path(queued)
        if tamper in ('traversal', 'wrong_hash'):
            replacement = ('../' + queued['source_path'] if tamper == 'traversal'
                           else '.state/sources/' + '0' * 64 + '.md')
            with service.journal.conn:
                service.journal.conn.execute('UPDATE operations SET source_path=? WHERE id=?',
                                            (replacement, queued['id']))
        elif tamper == 'symlink':
            outside = tmp_path / 'outside.md'
            source.rename(outside)
            source.symlink_to(outside)
        else:
            directory = source.parent
            outside = tmp_path / 'outside'
            directory.rename(outside)
            directory.symlink_to(outside, target_is_directory=True)
        result = service.run(queued['id'])
        assert result['state'] == 'failed'
        assert result['error_code'] == 'E_SOURCE'
        assert service.status()['archive_records'] == 0


def test_relocation_preserves_pending_resolution_and_original_archive_receipt(tmp_path):
    from kelmo.resolution import Resolution
    from test_resolution import pending, resolved_changes
    original = tmp_path / 'original'
    Service.initialize(original, 'fixture', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        with Service(original) as service:
            operation = pending(service)
            receipt = operation['archive_id']
            assert operation['state'] == 'needs_resolution'
        moved = tmp_path / 'moved'
        original.rename(moved)
        with Service(moved) as service:
            queue = Resolution(service)
            request = queue.claim()
            response = {'changes': resolved_changes(request)}
            result = queue.submit(request['id'], request['token'], response)
            assert result['state'] == 'applied'
            assert result['archive_id'] == receipt
            assert service.status()['archive_records'] == 3
            assert queue.submit(request['id'], request['token'], response)['state'] == 'applied'


def test_relocation_recovers_commit_before_resolution_completion(tmp_path, monkeypatch):
    from kelmo.resolution import Resolution
    from kelmo.wiki import engine
    from test_resolution import pending, resolved_changes
    original = tmp_path / 'original'
    Service.initialize(original, 'fixture', project='fixture')
    commit = engine._commit_registry

    def crash_after_commit(*args, **kwargs):
        commit(*args, **kwargs)
        raise SystemExit(99)

    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        with Service(original) as service:
            operation = pending(service)
            queue = Resolution(service)
            request = queue.claim()
            monkeypatch.setattr(engine, '_commit_registry', crash_after_commit)
            with pytest.raises(SystemExit):
                queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})
            with service.journal.conn:
                service.journal.conn.execute('UPDATE resolutions SET expires=0 WHERE id=?', (request['id'],))
        monkeypatch.setattr(engine, '_commit_registry', commit)
        moved = tmp_path / 'moved'
        original.rename(moved)
        with Service(moved) as service:
            queue = Resolution(service)
            reclaimed = queue.claim()
            assert reclaimed['response'] is not None
            result = queue.submit(reclaimed['id'], reclaimed['token'], reclaimed['response'])
            assert result['state'] == 'applied'
            assert result['archive_id'] == operation['archive_id']
            assert service.status()['archive_records'] == 3


@pytest.mark.parametrize('lock_name', ['capture.lock', 'worker.lock'])
def test_operation_lock_cannot_follow_a_symlink(tmp_path, lock_name):
    from kelmo.errors import KelmoError
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    with Service(root) as service:
        queued = service.capture('Synthetic evidence.', 'queued.md', 'lead')
        outside = tmp_path / 'outside-lock'
        outside.write_text('Keep this file.')
        lock = root / '.state' / lock_name
        lock.unlink(missing_ok=True)
        lock.symlink_to(outside)
        with pytest.raises(KelmoError):
            if lock_name == 'capture.lock':
                service.capture('Another synthetic source.', 'another.md', 'lead')
            else:
                service.run(queued['id'])
        assert outside.read_text() == 'Keep this file.'
