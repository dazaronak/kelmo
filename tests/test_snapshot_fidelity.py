import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo import snapshot as snap


@pytest.fixture
def cutover_vault(tmp_path, monkeypatch):
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
    snap_path = tmp_path / 'snap'
    snap.take_snapshot(sources, snap_path)
    ctl_path = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    snap.import_snapshot(
        snap_path, ctl_path, archive='archive', journal='journal', vault='vault',
        roots=roots, authority_id=config.vault_id, label=config.label,
    )
    snap.mark_cutover(ctl_path)
    return {
        'old': old,
        'config': config,
        'sources': sources,
        'snap_path': snap_path,
        'ctl_path': ctl_path,
        'roots': roots,
    }


def test_doctor_reads_an_imported_schema_2_vault_through_its_bound_roots(cutover_vault):
    from kelmo.doctor import diagnose
    result = diagnose(cutover_vault['ctl_path'])
    checks = result['checks']
    assert checks['registry_integrity'] and checks['data_repository'], result['warnings']
    assert checks['wiki_available'] and checks['wiki_owner'] and checks['wiki_committed']
    assert checks['wiki_consistency'] and checks['links'] and checks['budget']
    assert not any('Fact registry' in w or 'own Git repository' in w for w in result['warnings'])


def test_tree_files_refuses_a_fifo_instead_of_blocking_on_it(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    os.mkfifo(tree / 'pipe')
    with pytest.raises(KelmoError, match='not a regular file'):
        snap.tree_files(tree)


def test_a_failed_copy_leaves_no_partial_destination(tmp_path, monkeypatch):
    sources = {}
    for name in ('a_first', 'b_second'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'note.md').write_text('kept\n')
        sources[name] = {'kind': 'tree', 'path': tmp_path / name}
    real_copytree = shutil.copytree

    def copytree(source, target, **options):
        if Path(target).name == 'b_second':
            raise OSError(28, 'No space left on device')
        return real_copytree(source, target, **options)

    monkeypatch.setattr(snap.shutil, 'copytree', copytree)
    destination = tmp_path / 'snap'
    with pytest.raises(OSError, match='No space left'):
        snap.take_snapshot(sources, destination)
    assert not destination.exists(), 'a retry would refuse a non-empty destination'
    monkeypatch.setattr(snap.shutil, 'copytree', real_copytree)
    assert set(snap.take_snapshot(sources, destination)['sources']) == set(sources)


def test_tree_files_records_permission_bits_and_verify_refuses_modified_mode(tmp_path):
    tree_src = tmp_path / 'tree_src'
    tree_src.mkdir()
    script = tree_src / 'run.sh'
    script.write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
    os.chmod(script, 0o755)

    sources = {'tree_src': {'kind': 'tree', 'path': tree_src}}
    snap_path = tmp_path / 'snap'
    manifest = snap.take_snapshot(sources, snap_path)
    assert snap.verify_snapshot(snap_path) == manifest

    copy_script = snap_path / 'tree_src' / 'run.sh'
    os.chmod(copy_script, 0o644)

    with pytest.raises(KelmoError) as exc_info:
        snap.verify_snapshot(snap_path)
    assert exc_info.value.code == 'E_SNAPSHOT'
    assert 'changed after the snapshot' in str(exc_info.value)


def test_journal_operation_state_change_after_cutover_raises_e_tail(cutover_vault):
    ctl = cutover_vault['ctl_path']
    config = Config.load(ctl)
    journal_path = config.state / 'journal.sqlite3'

    with sqlite3.connect(journal_path) as conn:
        row = conn.execute("SELECT id, state FROM operations LIMIT 1").fetchone()
        assert row is not None
        op_id, initial_state = row[0], row[1]
        assert initial_state != 'failed'
        conn.execute("UPDATE operations SET state = 'failed' WHERE id = ?", (op_id,))

    tail_data = snap.tail(ctl)
    assert op_id in tail_data['operations']
    assert tail_data['empty'] is False

    with pytest.raises(KelmoError) as exc_info:
        snap.rollback_check(ctl)
    assert exc_info.value.code == 'E_TAIL'
    assert 'Rollback would lose' in str(exc_info.value)
    assert 'recover forward instead' in str(exc_info.value)


def test_session_capture_after_cutover_blocks_rollback(cutover_vault, tmp_path):
    from kelmo.sessions import Sessions
    from test_session_capture import lines
    ctl = cutover_vault['ctl_path']
    assert snap.rollback_check(ctl)['empty']
    transcript = tmp_path / 'pending.jsonl'
    transcript.write_bytes(lines('claude'))
    sessions = Sessions(Config.load(ctl))
    try:
        sessions.capture('claude', 'new-session', transcript, tmp_path, 'PreCompact')
    finally:
        sessions.close()
    with pytest.raises(KelmoError) as error:
        snap.rollback_check(ctl)
    assert error.value.code == 'E_TAIL'
    assert 'session_snapshots' in snap.tail(ctl)['session_tables']


def test_forward_recover_carries_extra_settings_and_captured_source_bytes(cutover_vault, tmp_path):
    from kelmo.sessions import Sessions
    ctl = cutover_vault['ctl_path']
    ctl_config = Config.load(ctl)

    with Service(ctl) as service:
        op = service.capture('Forward recovery evidence content.', 'forward_evidence.md', 'implementer')
        service.run(op['id'])

    assert (ctl_config.state / 'sources').is_dir()
    source_files_before = snap.tree_files(ctl_config.state / 'sources')
    assert len(source_files_before) > 0

    transcript_root = tmp_path / 'native-sessions'
    transcript_root.mkdir()
    transcript = transcript_root / 'session.jsonl'
    raw = b'{"type":"user","message":{"role":"user","content":"Keep this pending session."}}\n'
    transcript.write_bytes(raw)
    sessions = Sessions(ctl_config)
    try:
        captured = sessions.capture('claude', 'pending-session', transcript, transcript_root, 'PreCompact')
        original_job = sessions.job(captured['job_id'])
    finally:
        sessions.close()
    transcript.unlink()

    config_path = ctl / 'kelmo.toml'
    with open(config_path, 'a', encoding='utf-8') as handle:
        handle.write('\n[telemetry]\nsearch_log = "off"\n')

    assert Config.load(ctl).telemetry == {'search_log': 'off', 'retention_days': 30, 'max_rows': 5000}

    snap2 = tmp_path / 'snap2'
    ctl2 = tmp_path / 'ctl2'
    roots2 = {name: tmp_path / 'rec' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    result = snap.forward_recover(ctl, snap2, ctl2, roots=roots2)
    session_items = [item for item in snap.inventory(snap2)['items'] if item['kind'] == 'session-job']
    assert session_items == [{'source': 'journal', 'kind': 'session-job', 'id': captured['job_id'],
                              'state': 'pending', 'disposition': 'resume-after-cutover',
                              'snapshot_hash': captured['snapshot_hash']}]

    dest_toml = (ctl2 / 'kelmo.toml').read_text(encoding='utf-8')
    assert '[telemetry]' in dest_toml
    assert 'search_log = "off"' in dest_toml
    dest_config = Config.load(ctl2)
    assert dest_config.telemetry == {'search_log': 'off', 'retention_days': 30, 'max_rows': 5000}

    dest_sources = roots2['state_root'] / 'sources'
    assert snap.tree_files(dest_sources) == source_files_before
    restored_sessions = Sessions(dest_config)
    try:
        assert restored_sessions.job(captured['job_id']) == original_job
        assert restored_sessions.snapshot(captured['snapshot_hash']) == raw
    finally:
        restored_sessions.close()

    found = snap.rollback_check(ctl2)
    assert found['empty'] is True
    assert 'verify' in result['state']['steps']

    rejected = tmp_path / 'missing-session-bytes'
    with pytest.raises(KelmoError, match='state_sessions'):
        snap.import_snapshot(snap2, rejected, archive='archive', vault='vault', journal='journal',
                             roots={key: tmp_path / 'unused' / key for key in roots2})
    assert not rejected.exists()


def test_import_archive_without_authority_id_keeps_owner_and_refuses_different(cutover_vault, tmp_path):
    ctl = cutover_vault['ctl_path']
    ctl_config = Config.load(ctl)
    owner_authority_id = ctl_config.vault_id

    snap2 = tmp_path / 'snap2'
    sources2 = {
        'archive': {'kind': 'sqlite', 'path': ctl_config.archive},
        'journal': {'kind': 'sqlite', 'path': ctl_config.state / 'journal.sqlite3'},
        'vault': {'kind': 'git', 'path': ctl_config.knowledge_git_root},
    }
    snap.take_snapshot(sources2, snap2)

    ctl_no_auth = tmp_path / 'ctl_no_auth'
    roots_no_auth = {name: tmp_path / 'roots_no_auth' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    snap.import_snapshot(
        snap2, ctl_no_auth, archive='archive', journal='journal', vault='vault',
        roots=roots_no_auth,
    )
    imported_config = Config.load(ctl_no_auth)
    assert imported_config.vault_id == owner_authority_id

    ctl_diff_auth = tmp_path / 'ctl_diff_auth'
    roots_diff_auth = {name: tmp_path / 'roots_diff_auth' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    different_authority_id = str(uuid.uuid4())
    assert different_authority_id != owner_authority_id

    with pytest.raises(KelmoError) as exc_info:
        snap.import_snapshot(
            snap2, ctl_diff_auth, archive='archive', journal='journal', vault='vault',
            roots=roots_diff_auth, authority_id=different_authority_id,
        )
    assert exc_info.value.code == 'E_IMPORT'
    assert 'The archive copy belongs to another authority' in str(exc_info.value)


def test_import_snapshot_refuses_reserved_settings_and_carried_settings_filters_keys(cutover_vault, tmp_path):
    snap_path = cutover_vault['snap_path']
    ctl = tmp_path / 'ctl_reserved'
    roots = {name: tmp_path / 'roots_reserved' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}

    with pytest.raises(KelmoError) as exc_info:
        snap.import_snapshot(
            snap_path, ctl, archive='archive', journal='journal', vault='vault',
            roots=roots, settings='[storage]\nstate_root = "x"\n',
        )
    assert exc_info.value.code == 'E_INPUT'
    assert 'Carried settings may not set storage' in str(exc_info.value)

    raw_toml = (
        'schema_version = 2\n'
        'writer = "local"\n'
        'vault_id = "00000000-0000-0000-0000-000000000001"\n'
        'node_label = "test-node"\n'
        '\n'
        '[storage]\n'
        'state_root = "/var/state"\n'
        'archive_root = "/var/archive"\n'
        'knowledge_git_root = "/var/git"\n'
        '\n'
        '[telemetry]\n'
        'search_log = "off"\n'
        'retention_days = 30\n'
        '\n'
        '[budget]\n'
        'token_limit = 50000\n'
    )
    result = snap.carried_settings(raw_toml)
    assert 'schema_version' not in result
    assert 'writer' not in result
    assert 'vault_id' not in result
    assert 'node_label' not in result
    assert '[storage]' not in result
    assert 'state_root' not in result
    assert '[telemetry]\nsearch_log = "off"\nretention_days = 30' in result
    assert '[budget]\ntoken_limit = 50000' in result


def test_verify_snapshot_refuses_manifest_with_escaping_source_name(cutover_vault, tmp_path):
    snap_path = tmp_path / 'snap_escaping'
    shutil.copytree(cutover_vault['snap_path'], snap_path)

    manifest_file = snap_path / snap.MANIFEST
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    manifest['sources']['../escape'] = {'kind': 'sqlite', 'sha256': '0' * 64}
    manifest['sha256'] = snap._sha(json.dumps(manifest['sources'], sort_keys=True).encode('utf-8'))
    snap._write_json(manifest_file, manifest)

    with pytest.raises(KelmoError) as exc_info:
        snap.verify_snapshot(snap_path)
    assert exc_info.value.code == 'E_INPUT'
    assert "Source name '../escape' must be plain letters, digits, - and _." in str(exc_info.value)


def test_interrupted_vault_tree_import_resumes_and_verifies(cutover_vault, tmp_path):
    snap_path = cutover_vault['snap_path']
    manifest = json.loads((snap_path / snap.MANIFEST).read_text(encoding='utf-8'))
    ctl_path = tmp_path / 'ctl_resumed'
    roots = {name: tmp_path / 'resumed' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    git_root = roots['knowledge_git_root']

    leftover = git_root.with_name(git_root.name + '.importing')
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / 'partial_file.txt').write_text('partial interrupted content', encoding='utf-8')

    snap._write_json(ctl_path / snap.IMPORT_STATE, {
        'snapshot': manifest['id'],
        'authority_id': cutover_vault['config'].vault_id,
        'steps': {},
    })

    assert not git_root.exists()
    assert leftover.exists()

    state = snap.import_snapshot(
        snap_path, ctl_path, archive='archive', journal='journal', vault='vault',
        roots=roots, authority_id=cutover_vault['config'].vault_id, label='resumed',
    )

    assert not leftover.exists()
    assert git_root.is_dir()
    placed = snap.tree_files(git_root)
    # The import claims the wiki for its authority inside the copied Git metadata; the tree is otherwise exact.
    claim = git_root / '.git/kelmo-owner.json'
    assert json.loads(claim.read_text(encoding='utf-8')) == {'version': 1, 'vault_id': state['authority_id']}
    assert placed.pop('.git/kelmo-owner.json') and placed == manifest['sources']['vault']['files']
    assert list(state['steps'])[:7] == ['vault', 'archive', 'journal', 'config', 'wiki', 'open', 'verify']
