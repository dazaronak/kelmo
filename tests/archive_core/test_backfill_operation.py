"""The bounded backfill operation and its archive proxy."""
from types import SimpleNamespace

import pytest

from kelmo.archive_admin import ArchiveAdmin
from kelmo.errors import KelmoError
from kelmo.server import ArchiveWorker


def test_worker_bounds_backfill_and_returns_remaining():
    calls = []
    worker = ArchiveWorker.__new__(ArchiveWorker)
    worker.adapter = SimpleNamespace(backfill_vectors=lambda **args: calls.append(args) or {'written': 2})
    assert worker.op_backfill_vectors(batch=2, limit=3) == {'written': 2, 'remaining': None, 'cursor': None}
    assert calls == [{'batch': 2, 'limit': 3, 'after': None}]
    with pytest.raises(KelmoError):
        worker.op_backfill_vectors(limit=0)


def test_worker_backfill_without_a_limit_asks_for_the_whole_missing_set():
    calls = []
    worker = ArchiveWorker.__new__(ArchiveWorker)
    worker.adapter = SimpleNamespace(backfill_vectors=lambda **args: calls.append(args) or {'written': 70})
    assert worker.op_backfill_vectors(batch=16)['written'] == 70
    assert calls == [{'batch': 16, 'limit': None, 'after': None}]


def test_admin_backfill_calls_proxy_under_lock(tmp_path):
    state = tmp_path / '.state'
    state.mkdir()
    calls = []
    proxy = SimpleNamespace(backfill_vectors=lambda **args: calls.append(args) or {'remaining': 0})
    admin = ArchiveAdmin.__new__(ArchiveAdmin)
    admin._require = lambda capability: None
    admin.service = SimpleNamespace(archive_api=proxy)
    admin.config = SimpleNamespace(internal=lambda path: tmp_path / path)
    assert admin.backfill(batch=16, limit=64) == {'remaining': 0}
    assert calls == [{'batch': 16, 'limit': 64, 'after': None}]
