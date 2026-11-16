import json
import time
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


def snapshot_cli(capsys, ctl, action):
    from kelmo.cli import call
    code = call(['--vault', str(ctl), 'snapshot', action, '--json'])
    return code, json.loads(capsys.readouterr().out)


def only_record(ctl):
    with Service(ctl) as service:
        return [row['id'] for row in service.list(limit=10)['items']]


def test_a_record_forgotten_after_cutover_makes_the_rollback_check_refuse(cutover_vault, capsys):
    ctl = cutover_vault['ctl_path']
    [record] = only_record(ctl)
    forget(capsys, ctl, record)

    code, answer = snapshot_cli(capsys, ctl, 'rollback-check')
    assert code == 1, answer
    message = answer['error']['message']
    assert answer['error']['code'] == 'E_TAIL' and '1 forgotten records' in message, answer
    assert 'Rollback would lose' in message and 'recover forward instead' in message
    code, tail = snapshot_cli(capsys, ctl, 'tail')
    assert code == 0 and tail['result']['hidden'] == [record] and tail['result']['empty'] is False, tail


def test_a_record_forgotten_before_cutover_leaves_the_store_untouched(tmp_path, monkeypatch, capsys):
    for key, value in {'GIT_AUTHOR_NAME': 'S', 'GIT_COMMITTER_NAME': 'S', 'GIT_AUTHOR_EMAIL': 's@example.invalid',
                       'GIT_COMMITTER_EMAIL': 's@example.invalid', 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_CONFIG_GLOBAL': '/dev/null'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *a, **k: (_ for _ in ()).throw(
        __import__('kelmo.store.errors', fromlist=['x']).ArchiveEmbeddingError('off')))
    old = tmp_path / 'old'
    Service.initialize(old, 'node')
    with Service(old) as service:
        operation = service.run(service.capture('Evidence forgotten before the move.', 'one.md', 'implementer')['id'])
        service.forget(operation['archive_id'])
    # A cutover's `marked_at` has whole seconds; a forget in the same second counts as after it (the safe side).
    time.sleep(1.1)
    config = Config.load(old)
    sources = {'archive': {'kind': 'sqlite', 'path': config.archive},
               'journal': {'kind': 'sqlite', 'path': config.state / 'journal.sqlite3'},
               'vault': {'kind': 'git', 'path': old}}
    snap.take_snapshot(sources, tmp_path / 'snap')
    ctl = tmp_path / 'ctl'
    roots = {name: tmp_path / 'new' / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    snap.import_snapshot(tmp_path / 'snap', ctl, archive='archive', journal='journal', vault='vault', roots=roots,
                         authority_id=config.vault_id, label=config.label)
    snap.mark_cutover(ctl)

    code, answer = snapshot_cli(capsys, ctl, 'rollback-check')
    assert code == 0, answer
    assert answer['result']['hidden'] == [] and answer['result']['empty'] is True, answer
    # A baseline written before markers were recorded: a marker older than the cutover is not new either.
    drop_hidden(ctl)
    code, answer = snapshot_cli(capsys, ctl, 'rollback-check')
    assert code == 0, answer
    assert answer['result']['hidden'] == [] and answer['result']['empty'] is True, answer


def drop_hidden(ctl):
    baseline = json.loads((ctl / snap.CUTOVER).read_text(encoding='utf-8'))
    baseline.pop('hidden', None)
    (ctl / snap.CUTOVER).write_text(json.dumps(baseline), encoding='utf-8')


def forget(capsys, ctl, record):
    from kelmo.cli import call
    code = call(['--vault', str(ctl), 'forget', record, '--json'])
    answer = json.loads(capsys.readouterr().out)
    assert code == 0, answer


def test_a_baseline_without_markers_refuses_when_a_record_is_forgotten(cutover_vault, capsys):
    ctl = cutover_vault['ctl_path']
    drop_hidden(ctl)
    [record] = only_record(ctl)
    forget(capsys, ctl, record)

    code, answer = snapshot_cli(capsys, ctl, 'rollback-check')
    assert code == 1 and answer['error']['code'] == 'E_TAIL', answer


def test_a_baseline_without_markers_counts_a_forget_in_the_second_it_names(cutover_vault, capsys):
    """A baseline written before markers were recorded names its cutover in whole seconds. A record forgotten in
    that second cannot be told from one forgotten just after the cutover, so it counts: the check refuses and the
    operator recovers forward, which loses nothing."""
    ctl = cutover_vault['ctl_path']
    [record] = only_record(ctl)
    forget(capsys, ctl, record)
    [[_, forgotten_at]] = snap.visibility_census(Config.load(ctl).archive)
    baseline = json.loads((ctl / snap.CUTOVER).read_text(encoding='utf-8'))
    baseline.pop('hidden', None)
    baseline['marked_at'] = forgotten_at[:19] + forgotten_at[-6:]  # the same second, without its fraction
    (ctl / snap.CUTOVER).write_text(json.dumps(baseline), encoding='utf-8')

    code, answer = snapshot_cli(capsys, ctl, 'rollback-check')
    assert code == 1 and answer['error']['code'] == 'E_TAIL', answer
    assert '1 forgotten records' in answer['error']['message'], answer


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
