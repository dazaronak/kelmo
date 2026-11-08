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
    return {'old': old, 'config': config, 'sources': sources}


def test_source_changed_between_fingerprints_refused_and_destination_removed(tmp_path, monkeypatch, probe_setup):
    real_fingerprint = snap._fingerprint
    target_path = Path(probe_setup['sources']['vault']['path'])
    calls = 0

    def fake_fingerprint(kind, path):
        nonlocal calls
        result = real_fingerprint(kind, path)
        if Path(path) == target_path:
            calls += 1
            if calls == 2:
                return {'mutated': True}
        return result

    monkeypatch.setattr('kelmo.snapshot._fingerprint', fake_fingerprint)
    snap_dir = tmp_path / 'snap'
    with pytest.raises(KelmoError) as exc_info:
        snap.take_snapshot(probe_setup['sources'], snap_dir)
    assert exc_info.value.code == 'E_SNAPSHOT'
    assert 'changed during the snapshot' in str(exc_info.value)
    assert not snap_dir.exists()


def test_git_source_with_uncommitted_changes_refused(tmp_path, probe_setup):
    dirty_file = probe_setup['old'] / 'dirty.txt'
    dirty_file.write_text('uncommitted changes')
    snap_dir = tmp_path / 'snap'
    with pytest.raises(KelmoError) as exc_info:
        snap.take_snapshot(probe_setup['sources'], snap_dir)
    assert exc_info.value.code == 'E_SNAPSHOT'
    assert 'has uncommitted changes' in str(exc_info.value)


def test_symlink_inside_tree_source_refused(tmp_path, probe_setup):
    tree_dir = tmp_path / 'tree_src'
    tree_dir.mkdir()
    real_file = tree_dir / 'real.txt'
    real_file.write_text('content')
    symlink_file = tree_dir / 'sym.txt'
    symlink_file.symlink_to(real_file)

    tree_sources = {'tree_source': {'kind': 'tree', 'path': tree_dir}}
    snap_dir = tmp_path / 'snap'
    with pytest.raises(KelmoError) as exc_info:
        snap.take_snapshot(tree_sources, snap_dir)
    assert exc_info.value.code == 'E_SNAPSHOT'
    assert 'is a symlink' in str(exc_info.value)


def test_git_source_records_tracked_symlink_without_following(tmp_path, probe_setup):
    import hashlib
    import os
    import subprocess
    old = probe_setup['old']
    (old / 'linked.md').symlink_to('../outside/secret.md')
    subprocess.run(['git', '-C', str(old), 'add', 'linked.md'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(old), 'commit', '-q', '-m', 'link'], check=True, capture_output=True)

    snap_dir = tmp_path / 'snap'
    manifest = snap.take_snapshot(probe_setup['sources'], snap_dir)
    expected = 'link:' + hashlib.sha256(b'../outside/secret.md').hexdigest()
    assert manifest['sources']['vault']['files']['linked.md'] == expected
    assert (snap_dir / 'vault' / 'linked.md').is_symlink()
    assert os.readlink(snap_dir / 'vault' / 'linked.md') == '../outside/secret.md'
    snap.verify_snapshot(snap_dir)

    control = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    snap.import_snapshot(snap_dir, control, archive='archive', vault='vault', journal='journal', roots=roots)
    placed = roots['knowledge_git_root'] / 'linked.md'
    assert placed.is_symlink() and os.readlink(placed) == '../outside/secret.md'

    copied = snap_dir / 'vault' / 'linked.md'
    copied.unlink()
    copied.symlink_to('../outside/other.md')
    with pytest.raises(KelmoError) as exc_info:
        snap.verify_snapshot(snap_dir)
    assert 'changed after the snapshot' in str(exc_info.value)


def test_verify_snapshot_refuses_edited_copy(tmp_path, probe_setup):
    snap_dir = tmp_path / 'snap'
    snap.take_snapshot(probe_setup['sources'], snap_dir)
    snap.verify_snapshot(snap_dir)

    target = snap_dir / 'vault' / 'kelmo.toml'
    target.write_text(target.read_text(encoding='utf-8') + '\n# edited\n', encoding='utf-8')

    with pytest.raises(KelmoError) as exc_info:
        snap.verify_snapshot(snap_dir)
    assert exc_info.value.code == 'E_SNAPSHOT'
    assert 'changed after the snapshot' in str(exc_info.value)
