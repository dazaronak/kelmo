"""Edge case tests for memory_list."""
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.list import memory_list


def test_list_order_asc(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="first", kind="event", domain="dev")
    memory_store(conn, content="second", kind="event", domain="dev")
    result = memory_list(conn, order="asc")
    assert result["items"][0]["content"] == "first"


def test_list_order_desc(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="first", kind="event", domain="dev")
    memory_store(conn, content="second", kind="event", domain="dev")
    result = memory_list(conn, order="desc")
    assert result["items"][0]["content"] == "second"


def test_list_invalid_order_by_defaults(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="test", kind="event", domain="dev")
    result = memory_list(conn, order_by="invalid_column")
    assert result["total"] == 1  # doesn't crash


def test_list_empty_db(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    result = memory_list(conn)
    assert result["total"] == 0
    assert result["items"] == []


def test_list_offset_beyond_total(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="test", kind="event", domain="dev")
    result = memory_list(conn, offset=100)
    assert result["total"] == 1
    assert len(result["items"]) == 0


def test_list_multiple_filters_combined(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="target", kind="decision", domain="dev",
                 project="demo", tags=["sqlite"])
    memory_store(conn, content="decoy1", kind="decision", domain="life")
    memory_store(conn, content="decoy2", kind="event", domain="dev")
    result = memory_list(conn, kind="decision", domain="dev", project="demo",
                         tags=["sqlite"])
    assert result["total"] == 1
    assert result["items"][0]["content"] == "target"


def test_list_limit_clamped(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    for i in range(5):
        memory_store(conn, content=f"item {i}", kind="event", domain="dev")
    result = memory_list(conn, limit=0)
    assert len(result["items"]) == 1  # clamped to min 1
    result = memory_list(conn, limit=999)
    assert len(result["items"]) == 5  # clamped to max 500 but only 5 exist
