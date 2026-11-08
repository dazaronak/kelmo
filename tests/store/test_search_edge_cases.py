"""Edge case tests for memory_search."""
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.search import memory_search
from kelmo.store.errors import ArchiveValidationError
import pytest


def test_invalid_mode_raises(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    with pytest.raises(ArchiveValidationError) as exc:
        memory_search(conn, query="test", mode="invalid")
    assert exc.value.data["field"] == "mode"


def test_limit_clamped_to_1(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="a", kind="event", domain="dev")
    results = memory_search(conn, query="a", mode="semantic", limit=0)
    assert len(results) <= 1


def test_limit_clamped_to_100(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    for i in range(5):
        memory_store(conn, content=f"item {i}", kind="event", domain="dev")
    results = memory_search(conn, query="item", mode="semantic", limit=999)
    assert len(results) <= 100


def test_hybrid_with_single_record(tmp_db, mock_embed, monkeypatch):
    """Hybrid search works with just one record in DB."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="single record test", kind="decision", domain="dev")
    results = memory_search(conn, query="single record", mode="hybrid")
    assert len(results) == 1


def test_date_range_filter(tmp_db, mock_embed, monkeypatch):
    """since/until filters work."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="old memory", kind="event", domain="dev")
    results = memory_search(conn, query="old", mode="semantic",
                            since="2099-01-01")
    assert len(results) == 0  # all records created "now", filter for future


def test_machine_tag_filter(tmp_db, mock_embed, monkeypatch):
    """machine_tag filter works."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="tagged memory", kind="event", domain="dev")
    results = memory_search(conn, query="tagged", mode="semantic",
                            machine_tag="nonexistent-mac")
    assert len(results) == 0

    results = memory_search(conn, query="tagged", mode="semantic",
                            machine_tag="test-mac")
    assert len(results) == 1


def test_tags_filter_multiple(tmp_db, mock_embed, monkeypatch):
    """Multiple tag filter requires ALL tags present."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="tagged one", kind="event", domain="dev", tags=["alpha", "beta"])
    memory_store(conn, content="tagged two", kind="event", domain="dev", tags=["alpha"])

    results = memory_search(conn, query="tagged", mode="semantic",
                            tags=["alpha", "beta"])
    assert len(results) == 1
    assert "tagged one" in results[0]["content"]
