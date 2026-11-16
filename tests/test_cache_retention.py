"""A shared client removes cached contexts nobody has refreshed for a month, and with them the hold on their set."""

import os
import time

import pytest

from kelmo.client import Unbound
from kelmo.errors import KelmoError
from test_hot_set_retention import BINDING, DAY, Client, _old_set, _sets


def _age(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_a_cached_context_unused_for_a_month_is_removed_with_its_hold_on_a_set(tmp_path):
    hot, cache = tmp_path / 'hot', tmp_path / 'cache'
    client = Client(tmp_path)
    first = os.path.basename(client.context(binding=BINDING)['hub_directory'])
    [record] = list(cache.glob('context-*.json'))
    _age(record, 40)
    _age(hot / first, 40)
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    (hot / '.pruned').unlink()
    client.generation = 3
    client.context(binding=dict(BINDING, work='other-task'))
    assert not record.exists(), 'a cached context unused for 40 days was kept'
    assert first not in _sets(hot), 'the set only a removed cached context named was kept'
    with pytest.raises(KelmoError) as error:
        client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert 'no cached generation' in str(error.value)


def test_a_cached_context_refreshed_within_a_month_keeps_its_set(tmp_path):
    hot, cache = tmp_path / 'hot', tmp_path / 'cache'
    client = Client(tmp_path)
    first = os.path.basename(client.context(binding=BINDING)['hub_directory'])
    [record] = list(cache.glob('context-*.json'))
    _age(record, 20)
    _age(hot / first, 40)
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    (hot / '.pruned').unlink()
    client.generation = 3
    client.context(binding=dict(BINDING, work='other-task'))
    assert record.exists() and first in _sets(hot)
    assert client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))['freshness'] == 'stale'


def test_an_offline_delivery_from_the_cache_counts_as_a_use(tmp_path):
    cache = tmp_path / 'cache'
    client = Client(tmp_path)
    client.context(binding=BINDING)
    [record] = list(cache.glob('context-*.json'))
    _age(record, 29)
    assert client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))['freshness'] == 'stale'
    assert time.time() - record.stat().st_mtime < DAY, 'an offline delivery left the cache looking unused'


def test_a_cached_context_removed_while_it_is_read_is_no_cached_generation(tmp_path, monkeypatch):
    from pathlib import Path
    cache = tmp_path / 'cache'
    client = Client(tmp_path)
    client.context(binding=BINDING)
    [record] = list(cache.glob('context-*.json'))
    read_bytes = Path.read_bytes

    def swept_first(path):
        if path == record:
            path.unlink()  # another process's sweep, between the reader's look and its read
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', swept_first)
    with pytest.raises(KelmoError) as error:
        client.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert error.value.code == 'E_TRANSPORT' and 'no cached generation' in str(error.value)


def test_a_sweep_waits_for_an_offline_delivery_that_is_reading_its_cache(tmp_path, monkeypatch):
    """Another process's daily sweep must not remove the record and set a stale delivery is reading."""
    import threading

    from kelmo import hub_delivery
    hot, cache = tmp_path / 'hot', tmp_path / 'cache'
    reader = Client(tmp_path)
    first = os.path.basename(reader.context(binding=BINDING)['hub_directory'])
    [record] = list(cache.glob('context-*.json'))
    _age(record, 40)
    _age(hot / first, 40)
    for index in range(1, 13):
        _old_set(hot, 100 + index, days=10 + index)
    (hot / '.pruned').unlink()
    reading, go = threading.Event(), threading.Event()
    real = hub_delivery.read_set

    def paused(root, name='current'):
        if name == first and not reading.is_set():
            reading.set()
            go.wait(5)
        return real(root, name)

    monkeypatch.setattr(hub_delivery, 'read_set', paused)
    answers = {}

    def offline():
        try:
            answers['reader'] = reader.context(binding=BINDING, unreachable=Unbound('E_TRANSPORT', 'offline'))
        except KelmoError as error:
            answers['reader'] = error

    def sweep():
        other = Client(tmp_path)
        other.generation = 3
        other.context(binding=dict(BINDING, work='other-task'))

    threads = [threading.Thread(target=offline)]
    threads[0].start()
    assert reading.wait(5)
    threads.append(threading.Thread(target=sweep))
    threads[1].start()
    time.sleep(0.5)
    go.set()
    for thread in threads:
        thread.join(10)
    assert isinstance(answers['reader'], dict) and answers['reader']['freshness'] == 'stale', answers['reader']
    assert record.exists() and first in _sets(hot), 'the sweep removed what a delivery had just used'
