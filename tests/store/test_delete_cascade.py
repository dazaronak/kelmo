"""Tests for delete cascading: verify vec + fts cleanup."""
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.delete import memory_delete
from kelmo.store.memory.search import memory_search


def test_delete_makes_unsearchable(tmp_db, mock_embed, monkeypatch):
    """After delete, memory should not appear in search results."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="unique-deletable-xyzzy", kind="event", domain="dev")
    results_before = memory_search(conn, query="xyzzy", mode="semantic")
    assert len(results_before) >= 1

    memory_delete(conn, id=r["id"])
    results_after = memory_search(conn, query="xyzzy", mode="semantic")
    assert len(results_after) == 0


def test_delete_one_preserves_others(tmp_db, mock_embed, monkeypatch):
    """Deleting one memory doesn't affect others."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r1 = memory_store(conn, content="keep this one", kind="event", domain="dev")
    r2 = memory_store(conn, content="delete this one", kind="event", domain="dev")

    memory_delete(conn, id=r2["id"])

    row = conn.execute("SELECT * FROM memories WHERE id = ?", (r1["id"],)).fetchone()
    assert row is not None
    assert row["content"] == "keep this one"

    row = conn.execute("SELECT * FROM memories WHERE id = ?", (r2["id"],)).fetchone()
    assert row is None


def test_fts_not_stale_after_delete(tmp_db, mock_embed, monkeypatch):
    """FTS index doesn't return deleted content via search."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="stale check qqqqq", kind="event", domain="dev")
    memory_delete(conn, id=r["id"])

    # FTS5 external-content mode: the trigger fires on DELETE but the content
    # table rowid is gone. Search should return no results.
    results = memory_search(conn, query="qqqqq", mode="hybrid")
    assert len(results) == 0
