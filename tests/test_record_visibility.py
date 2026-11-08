"""The archive visibility marker: hidden from readers, absent from no byte."""
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest

from kelmo.store.db import init_schema, open_connection
from kelmo.store.memory.list import memory_list
from kelmo.store.memory.search import memory_search
from kelmo.store.memory.store import memory_store
from kelmo import visibility


def vector(text):
    return np.full(384, 1.0, dtype=np.float32).tobytes()


@pytest.fixture
def archive(tmp_path):
    connection = open_connection(str(tmp_path / 'archive.db'))
    init_schema(connection)
    with patch('kelmo.store.memory.store.embed', side_effect=vector), \
         patch('kelmo.store.memory.search.embed', side_effect=vector):
        try:
            yield connection
        finally:
            connection.close()


def stored(connection, content):
    return memory_store(connection, content=content, kind='event', domain='dev',
                        project='fixture', source='synthetic', machine_tag='test-node')['id']


def hidden(connection, record_id):
    visibility.ensure(connection)
    return visibility.hide(connection, record_id)


def test_schema_gives_the_archive_a_visibility_table(archive):
    tables = {row[0] for row in archive.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'record_visibility' in tables


def test_hiding_excludes_the_record_from_every_search_mode_and_from_list(archive):
    hidden_id, visible_id = stored(archive, 'needle hidden passage'), stored(archive, 'needle visible passage')
    assert hidden(archive, hidden_id) is True

    for mode in ('lexical', 'hybrid', 'semantic'):
        ids = [row['id'] for row in memory_search(archive, query='needle', mode=mode, exclude_forgotten=True)]
        assert ids == [visible_id], mode
    assert [row['id'] for row in memory_list(archive, exclude_forgotten=True)['items']] == [visible_id]


def test_hiding_without_the_exclusion_keeps_the_record_readable(archive):
    hidden_id = stored(archive, 'needle still readable passage')
    assert hidden(archive, hidden_id) is True

    assert [row['id'] for row in memory_search(archive, query='needle', mode='hybrid')] == [hidden_id]
    assert [row['id'] for row in memory_list(archive)['items']] == [hidden_id]


def test_exclusion_runs_before_the_limit_so_a_page_stays_full(archive):
    ids = [stored(archive, f'needle page record {index}') for index in range(3)]
    assert hidden(archive, ids[0]) is True

    page = memory_search(archive, query='needle', mode='lexical', limit=2, exclude_forgotten=True)
    assert len(page) == 2 and ids[0] not in [row['id'] for row in page]
    listing = memory_list(archive, limit=2, exclude_forgotten=True)
    assert len(listing['items']) == 2 and listing['total'] == 2


def test_hiding_touches_no_stored_byte(archive):
    record_id = stored(archive, 'needle retained bytes passage')
    before = archive.execute('SELECT content, tags_json, created_at FROM memories WHERE id=?',
                             (record_id,)).fetchone()
    rowid = archive.execute('SELECT rowid FROM memories WHERE id=?', (record_id,)).fetchone()['rowid']
    assert hidden(archive, record_id) is True

    after = archive.execute('SELECT content, tags_json, created_at FROM memories WHERE id=?',
                            (record_id,)).fetchone()
    assert tuple(after) == tuple(before)
    assert archive.execute('SELECT rowid FROM memories_fts WHERE rowid=?', (rowid,)).fetchone()['rowid'] == rowid
    assert archive.execute('SELECT rowid FROM memories_vec WHERE rowid=?', (rowid,)).fetchone()['rowid'] == rowid


def test_a_marker_cannot_exist_without_its_record_and_follows_its_removal(archive):
    with pytest.raises(sqlite3.IntegrityError):
        hidden(archive, '00000000-0000-0000-0000-000000000000')
    record_id = stored(archive, 'needle removed record')
    assert hidden(archive, record_id) is True
    with archive:
        archive.execute('DELETE FROM memories WHERE id=?', (record_id,))
    assert archive.execute('SELECT COUNT(*) FROM record_visibility').fetchone()[0] == 0


def test_hiding_is_idempotent_and_keeps_its_first_timestamp(archive):
    record_id = stored(archive, 'needle hidden twice')
    assert hidden(archive, record_id) is True
    first = archive.execute('SELECT forgotten_at FROM record_visibility WHERE id=?', (record_id,)).fetchone()[0]
    assert hidden(archive, record_id) is False
    assert archive.execute('SELECT forgotten_at FROM record_visibility WHERE id=?', (record_id,)).fetchone()[0] == first
