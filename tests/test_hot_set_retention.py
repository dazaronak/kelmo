"""A shared client keeps its recent HOT sets and removes the old ones when it installs a new set."""

import hashlib
import json
import os
import time
from types import SimpleNamespace

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
