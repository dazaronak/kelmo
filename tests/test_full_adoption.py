import sqlite3
from pathlib import Path
import pytest
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo import snapshot as snap


@pytest.fixture
def probe_setup(tmp_path, monkeypatch):
    for key, value in {
        'GIT_AUTHOR_NAME': 'S',
        'GIT_COMMITTER_NAME': 'S',
        'GIT_AUTHOR_EMAIL': 's@example.invalid',
        'GIT_COMMITTER_EMAIL': 's@example.invalid',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': '/dev/null',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        'kelmo.store.memory.store.embed',
        lambda *a, **k: (_ for _ in ()).throw(
            __import__('kelmo.store.errors', fromlist=['x']).ArchiveEmbeddingError('off')
        ),
    )
    old = tmp_path / 'old'
    Service.initialize(old, 'node')
    with Service(old) as service:
        op = service.capture('Original evidence.', 'one.md', 'implementer')
        service.run(op['id'])
    config = Config.load(old)
    sources = {
        'archive': {'kind': 'sqlite', 'path': config.archive},
        'journal': {'kind': 'sqlite', 'path': config.state / 'journal.sqlite3'},
        'vault': {'kind': 'git', 'path': old},
    }
    return {'old': old, 'config': config, 'sources': sources, 'op': op}


def test_import_proves_records_head_journal_and_hook_byte_identical(tmp_path, probe_setup):
    hook_dir = probe_setup['old'] / '.git' / 'hooks'
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hook_dir / 'pre-commit'
    hook_bytes = b'#!/bin/sh\n# custom hook\nexit 0\n'
    hook_file.write_bytes(hook_bytes)
    hook_file.chmod(0o755)

    snap_path = tmp_path / 'snap'
    manifest = snap.take_snapshot(probe_setup['sources'], snap_path)

    ctl = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    config = probe_setup['config']
    state = snap.import_snapshot(
        snap_path, ctl, archive='archive', journal='journal', vault='vault',
        roots=roots, authority_id=config.vault_id, label=config.label,
    )

    # 1. Archive records verified
    assert 'verify' in state['steps']
    assert state['steps']['verify']['records'] > 0
    after_records = snap.archive_census(roots['archive_root'] / 'archive.db')
    expected_records = manifest['sources']['archive']['archive']
    assert [list(r) for r in after_records] == [list(r) for r in expected_records]

    # 2. Git head verified
    assert state['steps']['verify']['head'] == manifest['sources']['vault']['head']
    assert snap._git(roots['knowledge_git_root'], 'rev-parse', 'HEAD') == manifest['sources']['vault']['head']

    # 3. Journal operations verified
    after_journal = snap.journal_census(roots['state_root'] / 'journal.sqlite3')
    expected_journal = manifest['sources']['journal']['journal']
    assert after_journal['operations'] == expected_journal['operations']
    assert len(after_journal['operations']) > 0

    # 4. Git hook arrived byte-identical
    imported_hook = roots['knowledge_git_root'] / '.git' / 'hooks' / 'pre-commit'
    assert imported_hook.is_file()
    assert imported_hook.read_bytes() == hook_bytes


def test_resumed_import_after_each_step_verifies_without_overwriting(tmp_path, monkeypatch, probe_setup):
    snap_path = tmp_path / 'snap'
    snap.take_snapshot(probe_setup['sources'], snap_path)

    ctl = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    config = probe_setup['config']

    copytree_calls = 0
    real_copytree = snap.shutil.copytree

    def counting_copytree(source, target, *args, **kwargs):
        # Count the root snapshot copy, independently of its private staging location.
        nonlocal copytree_calls
        if Path(source) == snap_path / 'vault':
            copytree_calls += 1
        return real_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr('kelmo.snapshot.shutil.copytree', counting_copytree)

    steps = ('vault', 'archive', 'journal', 'config', 'open')
    for step_name in steps:
        with pytest.raises(KelmoError) as exc_info:
            snap.import_snapshot(
                snap_path, ctl, archive='archive', journal='journal', vault='vault',
                roots=roots, authority_id=config.vault_id, label=config.label,
                stop_after=step_name,
            )
        assert exc_info.value.code == 'E_INTERRUPTED'
        assert f'Import stopped after {step_name}.' in str(exc_info.value)
        current_state = snap._state(ctl)
        assert step_name in current_state['steps']

    # copytree was executed during 'vault' and never re-invoked on subsequent resumed steps
    assert copytree_calls == 1

    # Final resumption runs to completion
    final_state = snap.import_snapshot(
        snap_path, ctl, archive='archive', journal='journal', vault='vault',
        roots=roots, authority_id=config.vault_id, label=config.label,
    )
    assert 'verify' in final_state['steps']
    assert final_state['steps']['verify']['records'] > 0
    assert copytree_calls == 1

    # Compare to a clean one-shot import to ensure the state matches exactly
    clean_ctl = tmp_path / 'clean_ctl'
    clean_roots = {name: tmp_path / 'clean' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    clean_state = snap.import_snapshot(
        snap_path, clean_ctl, archive='archive', journal='journal', vault='vault',
        roots=clean_roots, authority_id=config.vault_id, label=config.label,
    )
    assert final_state['steps']['verify'] == clean_state['steps']['verify']


def test_import_into_root_with_different_content_refused_e_import(tmp_path, probe_setup):
    snap_path = tmp_path / 'snap'
    snap.take_snapshot(probe_setup['sources'], snap_path)

    ctl = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    git_root = roots['knowledge_git_root']
    git_root.mkdir(parents=True, exist_ok=True)
    (git_root / 'conflict.txt').write_text('conflicting foreign content')

    config = probe_setup['config']
    with pytest.raises(KelmoError) as exc_info:
        snap.import_snapshot(
            snap_path, ctl, archive='archive', journal='journal', vault='vault',
            roots=roots, authority_id=config.vault_id, label=config.label,
        )
    assert exc_info.value.code == 'E_IMPORT'
    assert 'exists with other content' in str(exc_info.value)


def test_inventory_disposition_per_identity_and_blocking_items(tmp_path, probe_setup):
    config = probe_setup['config']
    op = probe_setup['op']

    with sqlite3.connect(config.state / 'journal.sqlite3') as conn:
        conn.execute(
            """
            INSERT INTO action_requests (
                operation_id, request_key, kind, fingerprint, origin,
                source_operation_id, observation, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'req-open-1',
                'key-open-1',
                'state_api',
                'fp-open-1',
                '{}',
                op['id'],
                None,
                'open',
                '2026-10-15T12:00:00Z',
            ),
        )

    queue_dir = tmp_path / 'named_queue'
    queue_dir.mkdir()
    (queue_dir / 'spool_job_001.json').write_text('{"job": 1}')

    sources = {
        'archive': {'kind': 'sqlite', 'path': config.archive},
        'journal': {'kind': 'sqlite', 'path': config.state / 'journal.sqlite3'},
        'vault': {'kind': 'git', 'path': probe_setup['old']},
        'spool_queue': {'kind': 'tree', 'path': queue_dir},
    }

    snap_path = tmp_path / 'snap'
    snap.take_snapshot(sources, snap_path)

    inv = snap.inventory(snap_path, queues=('spool_queue',))

    identities = [(item['source'], item['kind'], item['id']) for item in inv['items']]
    assert len(identities) == len(set(identities))
    assert len(inv['items']) > 0
    assert all('disposition' in item for item in inv['items'])

    blocking = inv['blocking']
    assert len(blocking) == 2

    state_req = next(item for item in blocking if item['kind'] == 'request:state_api')
    assert state_req['id'] == 'req-open-1'
    assert state_req['state'] == 'open'
    assert state_req['disposition'] == 'settle-before-cutover'

    queue_job = next(item for item in blocking if item['kind'] == 'queued-job')
    assert queue_job['id'] == 'spool_job_001.json'
    assert queue_job['source'] == 'spool_queue'
    assert queue_job['disposition'] == 'drain-before-cutover'

    assert inv['counts']['settle-before-cutover'] == 1
    assert inv['counts']['drain-before-cutover'] == 1
