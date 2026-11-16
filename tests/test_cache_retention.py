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
