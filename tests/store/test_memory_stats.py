"""Tests for memory_stats."""
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.stats import memory_stats


def test_stats_empty_db(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    s = memory_stats(conn)
    assert s["total"] == 0
    assert s["schema_version"] == 1
    assert s["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert s["embedding_dim"] == 384


def test_stats_counts_by_kind(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="d1", kind="decision", domain="dev")
    memory_store(conn, content="d2", kind="decision", domain="dev")
    memory_store(conn, content="e1", kind="event", domain="life")
    s = memory_stats(conn)
    assert s["total"] == 3
    assert s["by_kind"]["decision"] == 2
    assert s["by_kind"]["event"] == 1
    assert s["by_domain"]["dev"] == 2
    assert s["by_domain"]["life"] == 1
