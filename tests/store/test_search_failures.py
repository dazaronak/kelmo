import sqlite3

import pytest

from kelmo.store.memory.search import memory_search


def test_hybrid_search_does_not_hide_missing_fts_index(tmp_db, mock_embed):
    connection, _ = tmp_db
    connection.execute('DROP TABLE memories_fts')
    connection.commit()
    with pytest.raises(sqlite3.OperationalError):
        memory_search(connection, query='synthetic query', mode='hybrid')
