import json
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


def test_rollback_check_passes_with_no_writes_after_cutover(cutover_vault):
    ctl = cutover_vault['ctl_path']
    found = snap.rollback_check(ctl)
    assert found['empty'] is True
    assert found['records'] == []
    assert found['commits'] == []
    assert found['operations'] == []


def test_capture_and_git_commit_after_cutover_make_rollback_check_raise_e_tail(cutover_vault):
    ctl = cutover_vault['ctl_path']

    # 1. Capture after cutover
    with Service(ctl) as service:
        op = service.capture('After cutover evidence.', 'two.md', 'implementer')
        service.run(op['id'])

    # 2. Git commit after cutover
    config = Config.load(ctl)
    git_root = config.knowledge_git_root
    extra_file = git_root / 'extra.txt'
    extra_file.write_text('committed after cutover')
    snap._git(git_root, 'add', 'extra.txt')
    snap._git(git_root, 'commit', '-m', 'extra commit after cutover')

    # 3. Verify rollback_check raises E_TAIL
    with pytest.raises(KelmoError) as exc_info:
        snap.rollback_check(ctl)
    assert exc_info.value.code == 'E_TAIL'
    assert 'Rollback would lose' in str(exc_info.value)
    assert 'recover forward instead' in str(exc_info.value)

    # Verify tail contents
    tail_data = snap.tail(ctl)
    assert not tail_data['empty']
    assert len(tail_data['records']) > 0
    assert len(tail_data['commits']) > 0
    assert len(tail_data['operations']) > 0


def test_forward_recover_yields_vault_with_pre_and_post_cutover_records_and_head(tmp_path, cutover_vault):
    ctl = cutover_vault['ctl_path']

    # 1. Post-cutover write
    with Service(ctl) as service:
        op = service.capture('Post-cutover evidence.', 'post.md', 'implementer')
        service.run(op['id'])

    # 2. Post-cutover Git commit
    ctl_config = Config.load(ctl)
    git_root = ctl_config.knowledge_git_root
    post_file = git_root / 'post_commit.txt'
    post_file.write_text('post-cutover commit content')
    snap._git(git_root, 'add', 'post_commit.txt')
    snap._git(git_root, 'commit', '-m', 'post-cutover commit')

    expected_head = snap._git(git_root, 'rev-parse', 'HEAD')
    cutover_baseline = json.loads((ctl / snap.CUTOVER).read_text(encoding='utf-8'))
    expected_pre_cutover_records = cutover_baseline['records']

    # 3. Perform forward_recover
    snap2 = tmp_path / 'snap2'
    ctl2 = tmp_path / 'ctl2'
    roots2 = {name: tmp_path / 'rec' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    result = snap.forward_recover(ctl, snap2, ctl2, roots=roots2)

    # 4. Verify recovered vault
    ctl2_config = Config.load(ctl2)
    recovered_records = snap.archive_census(ctl2_config.archive)
    recovered_record_set = {tuple(r) for r in recovered_records}

    # Holds every pre-cutover record version
    assert len(expected_pre_cutover_records) > 0
    for record in expected_pre_cutover_records:
        assert tuple(record) in recovered_record_set

    # Holds every post-cutover record version
    assert len(result['tail']['records']) > 0
    for record in result['tail']['records']:
        assert tuple(record) in recovered_record_set

    # Holds the current Git head
    recovered_head = snap._git(ctl2_config.knowledge_git_root, 'rev-parse', 'HEAD')
    assert recovered_head == expected_head

    # Verify step completed cleanly
    assert 'verify' in result['state']['steps']
