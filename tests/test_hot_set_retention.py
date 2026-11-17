"""A shared client keeps its recent HOT sets and removes the old ones when it installs a new set."""

import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from kelmo.client import SharedClient, Unbound

DAY = 86400
AUTHORITY = '00000000-0000-0000-0000-000000000001'
BINDING = {'repository': 'repo', 'work': 'task', 'work_project': 'alpha', 'branch': 'feature',
           'binding_status': 'bound'}


def _snapshot(generation):
    files = {'mandatory.md': f'<!-- kelmo generation {generation} -->\nmandatory',
             'alpha.md': f'<!-- kelmo generation {generation} -->\nhub alpha',
             'alpha.full.md': f'<!-- kelmo generation {generation} -->\nmandatory\nhub alpha\nfull alpha'}
    return {'generation': generation, 'enrollment_revision': 1, 'manifest_sha256': 'a' * 64, 'projects': ['alpha'],
            'files': {name: {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
                      for name, text in files.items()}}


class Client(SharedClient):
    """The real context path over a server answer built here: every call delivers `generation`'s HOT set."""

    def __init__(self, state):
        self.profile = SimpleNamespace(state=state, authority_id=AUTHORITY)
        self._deadline = None
        self.generation = 2

    def call(self, operation, args):
        header = (f'Kelmo knowledge generation {self.generation} (manifest aaaaaaaaaaaa): current, complete; '
                  'mandatory pages: person.md.')
        context = header + '\n\n## person.md\n\nmandatory'
        return {'configured': True, 'generation': {'authority_id': AUTHORITY, 'publication_id': self.generation,
                                                   'manifest_sha256': 'a' * 64},
                'context': context, 'context_sha256': hashlib.sha256(context.encode()).hexdigest(),
                'pages': [{'path': 'person.md', 'bytes': 9, 'sha256': hashlib.sha256(b'mandatory').hexdigest()}],
                'mandatory': ['person.md'], 'project': 'alpha', 'hub_set': _snapshot(self.generation),
                'hub_work': 'bound work', 'pending': [], 'freshness': 'current'}


def _old_set(hot, index, days):
    directory = hot / f'.set-{index}-0-{index:064x}'
    directory.mkdir(parents=True)
    (directory / 'mandatory.md').write_text('old\n')
    stamp = time.time() - days * DAY
    os.utime(directory / 'mandatory.md', (stamp, stamp))
    os.utime(directory, (stamp, stamp))
    return directory.name


def _sets(hot):
    return {path.name for path in hot.iterdir() if path.name.startswith('.set-')}


def test_installing_a_set_removes_old_sets_and_keeps_the_recent_ones(tmp_path):
    hot = tmp_path / 'hot'
    old = [_old_set(hot, 100 + index, days=3 + index) for index in range(1, 13)]  # 4 to 15 days old
    recent = _old_set(hot, 99, days=0.5)
    Client(tmp_path).context(binding=BINDING)
    left = _sets(hot)
    assert os.readlink(hot / 'current') in left and recent in left
    # The newest eight stay whatever their age: the current set, the recent one and the six youngest old ones.
    assert set(old[:6]) <= left, 'a set among the newest eight was removed'
    assert not set(old[6:]) & left, f'old sets were kept: {sorted(set(old[6:]) & left)}'


def test_a_set_a_cached_context_names_is_kept_and_still_serves_offline(tmp_path):
    hot = tmp_path / 'hot'
    client = Client(tmp_path)
    online = client.context(binding=BINDING)
    named = os.path.basename(online['hub_directory'])
    stamp = time.time() - 30 * DAY
    os.utime(hot / named, (stamp, stamp))
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    (hot / '.pruned').unlink(missing_ok=True)
    client.generation = 3
    client.context(binding=dict(BINDING, work='other-task'))
    assert named in _sets(hot), 'the set a cached context names was removed'
    offline = client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert offline['freshness'] == 'stale' and offline['hub_set']['generation'] == 2


def test_a_delivery_with_no_sweep_due_reads_no_cached_context(tmp_path):
    class Counting(Client):
        reads = 0

        def _cached_set_names(self):
            Counting.reads += 1
            return super()._cached_set_names()

    client = Counting(tmp_path)
    client.context(binding=BINDING)
    assert Counting.reads == 1, 'the first install sweeps and reads the cached contexts once'
    client.generation = 3
    client.context(binding=BINDING)
    client.context(binding=dict(BINDING, work='other-task'))
    assert Counting.reads == 1, f'deliveries with no sweep due read the cached contexts {Counting.reads - 1} times'


def test_old_sets_are_swept_at_most_once_a_day(tmp_path):
    hot = tmp_path / 'hot'
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    client = Client(tmp_path)
    client.context(binding=BINDING)
    later = [_old_set(hot, 200 + index, days=30) for index in range(12)]
    client.generation = 3
    client.context(binding=BINDING)
    assert set(later) <= _sets(hot), 'a second sweep ran within a day'
    assert json.loads((hot / os.readlink(hot / 'current') / 'set.json').read_text())['generation'] == 3


def test_a_sweep_killed_part_way_leaves_no_fresh_looking_half_set(tmp_path, monkeypatch):
    """Removing a set's files touches its folder; a sweep killed then must not leave a set that looks new."""
    import pytest

    from kelmo import hub_delivery
    hot = tmp_path / 'hot'
    old = [_old_set(hot, 100 + index, days=10 + index) for index in range(1, 13)]
    real = hub_delivery.shutil.rmtree

    def killed(path, *args, **kwargs):
        for child in list(os.scandir(path))[:1]:
            os.unlink(child.path)
        raise SystemExit('killed')

    monkeypatch.setattr(hub_delivery.shutil, 'rmtree', killed)
    client = Client(tmp_path)
    with pytest.raises(SystemExit):
        client.context(binding=BINDING)
    monkeypatch.setattr(hub_delivery.shutil, 'rmtree', real)
    (hot / '.pruned').unlink(missing_ok=True)
    client.generation = 3
    client.context(binding=BINDING)
    left = {path.name for path in hot.iterdir() if path.is_dir() and not path.is_symlink()}
    assert not set(old[6:]) & left, f'a half-removed set was kept: {sorted(set(old[6:]) & left)}'
    assert not [name for name in left if not name.startswith('.set-')], sorted(left)


def test_a_set_installed_again_counts_as_new_for_the_sweep(tmp_path):
    """A delivery that reuses an existing set folder has not written its cache record when another sweeps."""
    hot = tmp_path / 'hot'
    client = Client(tmp_path)
    named = os.path.basename(client.context(binding=BINDING)['hub_directory'])
    stamp = time.time() - 30 * DAY
    os.utime(hot / named, (stamp, stamp))
    client.context(binding=dict(BINDING, work='other-task'))
    assert time.time() - (hot / named).stat().st_mtime < DAY, 'installing an existing set kept its old age'


def test_a_sweep_marker_dated_in_the_future_does_not_stop_the_sweep(tmp_path):
    hot = tmp_path / 'hot'
    old = [_old_set(hot, 100 + index, days=10 + index) for index in range(1, 13)]
    (hot / '.pruned').touch()
    ahead = time.time() + 10 * DAY
    os.utime(hot / '.pruned', (ahead, ahead))
    Client(tmp_path).context(binding=BINDING)
    # The current set and the seven youngest old ones are the newest eight.
    assert not set(old[7:]) & _sets(hot), 'a marker from a clock set back stopped the sweep'


def test_a_stale_delivery_whose_set_was_removed_says_it_has_no_cached_generation(tmp_path, monkeypatch):
    import shutil

    import pytest

    from kelmo import hub_delivery
    from kelmo.errors import KelmoError
    hot = tmp_path / 'hot'
    client = Client(tmp_path)
    named = os.path.basename(client.context(binding=BINDING)['hub_directory'])
    client.generation = 3
    client.context(binding=dict(BINDING, work='other-task'))
    real = hub_delivery.read_set

    def removed_while_read(root, name='current'):
        if name == named and (hot / named).exists():
            shutil.rmtree(hot / named)
        return real(root, name)

    monkeypatch.setattr(hub_delivery, 'read_set', removed_while_read)
    with pytest.raises(KelmoError) as during:
        client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert during.value.code == 'E_TRANSPORT' and 'no cached generation' in str(during.value), during.value
    with pytest.raises(KelmoError) as before:
        client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert before.value.code == 'E_TRANSPORT' and 'no cached generation' in str(before.value), before.value


def test_a_set_a_session_was_pointed_at_stays_while_the_session_is_recent(tmp_path, monkeypatch):
    """Oversized mandatory pages reach a session as a path into the set; the session may read it days later."""
    import hashlib

    from kelmo import hub_delivery, hub_embed
    monkeypatch.setattr(hub_embed, 'sync', lambda *args, **kwargs: {'verified': True, 'changed': False})
    hot, cache = tmp_path / 'hot', tmp_path / 'cache'
    client = Client(tmp_path)
    named = os.path.basename(client.context(binding=BINDING)['hub_directory'])
    snapshot = hub_delivery.read_set(hot, named)
    content = '<!-- kelmo generation 2 -->\n' + 'M' * 10500
    snapshot['files']['mandatory.md'] = {'text': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
    acknowledged = []
    output = hub_delivery.prepare(SimpleNamespace(state=tmp_path), {'session_id': 'long-session'}, snapshot,
                                  'alpha', '', [], provider='claude', current=hot / named, stale=False,
                                  acknowledge=acknowledged.append)
    assert str(hot / named) in output
    for commit in acknowledged:
        commit()
    for record in cache.glob('context-*.json'):
        record.unlink()  # no cached context holds the set: only the session's pointer does
    stamp = time.time() - 30 * DAY
    os.utime(hot / named, (stamp, stamp))
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    (hot / '.pruned').unlink()
    client.generation = 3
    client.context(binding=dict(BINDING, work='other-task'))
    assert named in _sets(hot), 'a set a recent session was pointed at was removed'
    for record in (tmp_path / 'hub-sessions').glob('*.json'):
        os.utime(record, (time.time() - 31 * DAY,) * 2)
    (hot / '.pruned').unlink()
    client.generation = 4
    client.context(binding=dict(BINDING, work='third-task'))
    assert named not in _sets(hot), 'a set only a month-old session record named was kept'


def test_an_installed_kelmo_hook_on_a_local_vault_records_its_delivery_once_the_output_is_out(tmp_path, capsys,
                                                                                            monkeypatch):
    """A vault with a server profile installs `kelmo hook`, not `kelmod hook`. With kelmod beside the launcher
    it still runs in process, so the delivery record that keeps a session's set is written after the output."""
    import io
    from kelmo import cli, integration
    from kelmo.core import daemon_client
    from test_hot import config
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    launcher = tmp_path / 'bin' / 'kelmo'
    launcher.parent.mkdir()
    (launcher.parent / 'kelmod').write_text('')
    monkeypatch.setattr(daemon_client, 'ensure', lambda *a, **k: pytest.fail('kelmo hook started a daemon'))
    monkeypatch.setattr(daemon_client, 'call', lambda *a, **k: pytest.fail('kelmo hook went through the daemon'))
    written = []

    def hook(service, action, payload, *, acknowledge=None):
        acknowledge(lambda: written.append(capsys.readouterr().out))
        return {'hookSpecificOutput': {'additionalContext': 'delivered-context'}}

    monkeypatch.setattr(integration, 'hook', hook)
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    assert cli.main(['hook', 'start', '--provider', 'claude', '--vault', str(selected.root)],
                    executable=str(launcher)) == 0
    assert len(written) == 1 and 'delivered-context' in written[0], written


def test_a_sweep_that_fails_part_way_still_waits_a_day_before_the_next(tmp_path, monkeypatch):
    """A set the sweep cannot rename must not make every later delivery read the cached contexts again."""
    from kelmo import hub_delivery
    hot = tmp_path / 'hot'
    for index in range(12):
        _old_set(hot, index + 10, days=5 + index)

    def refuse(*_args):
        raise OSError('rename refused')

    monkeypatch.setattr(hub_delivery.os, 'rename', refuse)
    reads = []
    for _ in range(2):
        try:
            hub_delivery.prune_sets(hot, keep=lambda: reads.append(1) or ())
        except OSError:
            pass
    assert len(reads) == 1, f'the sweep ran {len(reads)} times in one day'


def _local_vault(tmp_path):
    root = tmp_path / 'vault'
    wiki = root / 'knowledge/wiki'
    wiki.mkdir(parents=True)
    (root / 'kelmo.toml').write_text('schema_version = 1\nwriter = "local"\n'
                                     'vault_id = "00000000-0000-0000-0000-000000000001"\nnode_label = "test"\n'
                                     '[hot]\npages = ["global.md"]\n')
    (wiki / 'global.md').write_text('global page')
    from kelmo.config import Config
    return root, Config.load(root).state


def test_a_local_vault_sweeps_its_old_sets_and_records_though_no_set_is_installed(tmp_path):
    """On a local vault whose context fits, no hook installs a set, which is where the daily sweep ran: the hook
    runs the sweep itself, once a day."""
    from kelmo.integration import hook
    root, state = _local_vault(tmp_path)
    hot = state / 'hot'
    old = [_old_set(hot, 100 + index, days=3 + index) for index in range(1, 11)]  # 4 to 13 days old
    reference = state / 'hub-context' / ('0' * 64 + '.json')
    reference.parent.mkdir(parents=True)
    reference.write_text('{}')
    stamp = time.time() - 40 * DAY
    os.utime(reference, (stamp, stamp))
    hook(SimpleNamespace(root=root), 'start', {})
    left = _sets(hot)
    assert set(old[:8]) <= left, 'a set among the newest eight was removed'
    assert not set(old[8:]) & left, f'old sets were kept: {sorted(set(old[8:]) & left)}'
    assert not reference.exists(), 'a context reference unused for 40 days was kept'


def test_a_local_vault_sweeps_at_most_once_a_day(tmp_path):
    from kelmo.integration import hook
    root, state = _local_vault(tmp_path)
    hot = state / 'hot'
    hot.mkdir(parents=True)
    hook(SimpleNamespace(root=root), 'start', {})
    late = _old_set(hot, 300, days=20)
    for index in range(8):
        _old_set(hot, 200 + index, days=1)
    hook(SimpleNamespace(root=root), 'prompt', {})
    assert late in _sets(hot), 'a second sweep ran within a day'
