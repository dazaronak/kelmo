"""Tests for memory_store + list + delete CRUD."""
import json
import pytest
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.delete import memory_delete
from kelmo.store.memory.list import memory_list
from kelmo.store.errors import ArchiveValidationError, ArchiveNotFoundError


def test_store_returns_id_and_created_at(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    result = memory_store(conn, content="test memory", kind="decision", domain="dev")
    assert "id" in result
    assert "created_at" in result
    assert len(result["id"]) == 36


def test_store_persists_in_db(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    result = memory_store(conn, content="persisted", kind="milestone", domain="dev",
                          project="demo", tags=["test", "crud"])
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (result["id"],)).fetchone()
    assert row["content"] == "persisted"
    assert row["kind"] == "milestone"
    assert row["domain"] == "dev"
    assert row["project"] == "demo"
    assert json.loads(row["tags_json"]) == ["test", "crud"]
    assert row["machine_tag"] == "test-mac"


def test_store_creates_vec_row(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    result = memory_store(conn, content="vec test", kind="quirk", domain="dev")
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (result["id"],)).fetchone()
    vec_row = conn.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (row["rowid"],)
    ).fetchone()
    assert vec_row is not None


def test_store_creates_fts_row(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="unique searchable term xyzzy", kind="event", domain="dev")
    fts_row = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"xyzzy\"'"
    ).fetchone()
    assert fts_row is not None


def test_store_invalid_kind_raises(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    with pytest.raises(ArchiveValidationError) as exc_info:
        memory_store(conn, content="bad", kind="bogus", domain="dev")
    assert exc_info.value.data["field"] == "kind"


def test_store_invalid_domain_raises(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    with pytest.raises(ArchiveValidationError):
        memory_store(conn, content="bad", kind="decision", domain="work")


def test_store_uuid_uniqueness(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    ids = set()
    for i in range(20):
        r = memory_store(conn, content=f"item {i}", kind="event", domain="dev")
        ids.add(r["id"])
    assert len(ids) == 20


def test_store_optional_fields_default(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    result = memory_store(conn, content="minimal", kind="rule", domain="life")
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (result["id"],)).fetchone()
    assert row["project"] is None
    assert json.loads(row["tags_json"]) == []
    assert row["source"] is None
    assert row["raw_note_path"] is None


# --- memory_delete ---

def test_delete_existing(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="to delete", kind="event", domain="dev")
    result = memory_delete(conn, id=r["id"])
    assert result["deleted"] is True
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (r["id"],)).fetchone()
    assert row is None


def test_delete_nonexistent(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    with pytest.raises(ArchiveNotFoundError):
        memory_delete(conn, id="nonexistent-uuid")


def test_delete_removes_vec_and_fts(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="unique-delete-test-xyzzy", kind="quirk", domain="dev")
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (r["id"],)).fetchone()
    rowid = row["rowid"]
    memory_delete(conn, id=r["id"])
    assert conn.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone() is None


# --- memory_list ---

def test_list_returns_items_and_total(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    for i in range(5):
        memory_store(conn, content=f"item {i}", kind="event", domain="dev")
    result = memory_list(conn)
    assert result["total"] == 5
    assert len(result["items"]) == 5


def test_list_pagination(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    for i in range(10):
        memory_store(conn, content=f"item {i}", kind="event", domain="dev")
    result = memory_list(conn, limit=3, offset=0)
    assert len(result["items"]) == 3
    assert result["total"] == 10


def test_list_filter_by_kind(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="a decision", kind="decision", domain="dev")
    memory_store(conn, content="an event", kind="event", domain="dev")
    result = memory_list(conn, kind="decision")
    assert result["total"] == 1
    assert result["items"][0]["kind"] == "decision"


def test_store_raw_note_twice_returns_the_first_row(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    first = memory_store(conn, content="note v1", kind="event", domain="dev",
                         source="raw_note", raw_note_path="2026-09-04_x.md")
    second = memory_store(conn, content="note v1 again", kind="event", domain="dev",
                          source="raw_note", raw_note_path="2026-09-04_x.md")
    assert second["id"] == first["id"]
    assert second["duplicate"] is True
    assert "duplicate" not in first
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 1


def test_store_other_sources_never_dedupe_on_path(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    a = memory_store(conn, content="digest", kind="event", domain="dev",
                     source="session_digest", raw_note_path="/tmp/same.md")
    b = memory_store(conn, content="digest", kind="event", domain="dev",
                     source="session_digest", raw_note_path="/tmp/same.md")
    assert a["id"] != b["id"]
