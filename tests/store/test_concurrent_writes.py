"""Test concurrent memory_store calls."""
import threading
from unittest.mock import patch
from kelmo.store.memory.store import memory_store


def test_concurrent_replays_return_one_archive_receipt(tmp_db):
    from kelmo.store.db import open_connection
    conn, path = tmp_db
    barrier = threading.Barrier(2)
    results, errors = [], []

    def embedding(_text):
        barrier.wait(timeout=5)
        return bytes(384 * 4)

    def replay():
        connection = open_connection(path)
        try:
            results.append(memory_store(connection, content='Same synthetic note', kind='event',
                                        domain='dev', source='raw_note', raw_note_path='same.md'))
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            connection.close()

    with patch('kelmo.store.memory.store.embed', side_effect=embedding):
        threads = [threading.Thread(target=replay) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert errors == []
    assert len(results) == 2
    assert len({row['id'] for row in results}) == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE raw_note_path='same.md'").fetchone()[0] == 1


def test_concurrent_stores_both_succeed(tmp_db, mock_embed, monkeypatch):
    """Two threads storing simultaneously should both succeed."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    _, path = tmp_db
    results = []
    errors = []

    def store_one(content):
        try:
            from kelmo.store.db import open_connection, init_schema
            c = open_connection(path)
            init_schema(c)
            r = memory_store(c, content=content, kind="event", domain="dev")
            results.append(r)
            c.close()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=store_one, args=("thread 1 content",))
    t2 = threading.Thread(target=store_one, args=("thread 2 content",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0, f"Errors: {errors}"
    assert len(results) == 2
    assert results[0]["id"] != results[1]["id"]


def test_ten_concurrent_writes(tmp_db, mock_embed, monkeypatch):
    """Ten threads writing simultaneously — all succeed, all unique IDs."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    _, path = tmp_db
    results = []
    errors = []

    def store_one(i):
        try:
            from kelmo.store.db import open_connection, init_schema
            c = open_connection(path)
            init_schema(c)
            r = memory_store(c, content=f"thread {i}", kind="event", domain="dev")
            results.append(r)
            c.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=store_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Errors: {errors}"
    assert len(results) == 10
    ids = {r["id"] for r in results}
    assert len(ids) == 10
