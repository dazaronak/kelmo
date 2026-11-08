"""Controller-only importer and time-order regression cases on synthetic files."""
from pathlib import Path
import sys

import kelmo
import pytest

sys.path.insert(0, str(Path(kelmo.__file__).resolve().parents[2]/'tests'))
import test_archive_adoption as fixtures  # noqa: E402

archive = fixtures.archive
offline = fixtures.offline


def test_list_orders_equal_and_offset_timestamps_as_instants(archive):
    from kelmo.store.memory.list import memory_list
    adapter, origin, _ = archive
    older = adapter.capture(fixtures.packet(b'older\n'), origin, 'older')
    newer = adapter.capture(fixtures.packet(b'newer\n'), origin, 'newer')
    with adapter.connection:
        adapter.connection.execute('UPDATE memories SET created_at=?,valid_from=NULL WHERE id=?',
                                   ('2026-04-18T09:00:00+02:00', older.archive_id))
        adapter.connection.execute('UPDATE memories SET created_at=?,valid_from=NULL WHERE id=?',
                                   ('2026-04-18T08:00:00Z', newer.archive_id))
    result = memory_list(adapter.connection, order_by='created_at')
    assert [row['id'] for row in result['items']] == [newer.archive_id, older.archive_id]
    with adapter.connection:
        adapter.connection.execute('UPDATE memories SET created_at=? WHERE id=?',
                                   ('2026-04-19T00:30:00+02:00', newer.archive_id))
    result = memory_list(adapter.connection, until='2026-04-18')
    assert result['total'] == 2


@pytest.mark.parametrize('field', ['created_at', 'updated_at'])
def test_timestamp_ordering_retains_microsecond_precision(archive, field):
    from kelmo.store.memory.list import memory_list
    adapter, origin, _ = archive
    refs = [adapter.capture(fixtures.packet(text), origin, str(index))
            for index, text in enumerate((b'one\n', b'two\n'))]
    newer, older = sorted(ref.archive_id for ref in refs)
    with adapter.connection:
        adapter.connection.execute(f'UPDATE memories SET {field}=? WHERE id=?',
                                   ('2026-04-18T08:00:00.000001Z', older))
        adapter.connection.execute(f'UPDATE memories SET {field}=? WHERE id=?',
                                   ('2026-04-18T08:00:00.000002Z', newer))
    result = memory_list(adapter.connection, order_by=field)
    assert [row['id'] for row in result['items']] == [newer, older]
