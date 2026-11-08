"""valid_from: the day a fact became true, separate from the day it was stored."""

import pytest

from kelmo.store.errors import ArchiveValidationError
from kelmo.store.memory.list import memory_list
from kelmo.store.memory.search import memory_search
from kelmo.store.memory.store import memory_store, resolve_valid_from


def test_explicit_valid_from_is_stored_and_returned(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="x", kind="event", domain="dev", valid_from="2026-06-11")
    assert r["valid_from"] == "2026-06-11"
    row = conn.execute("SELECT valid_from FROM memories WHERE id = ?", (r["id"],)).fetchone()
    assert row["valid_from"] == "2026-06-11"


def test_raw_note_derives_valid_from_from_its_filename(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="x", kind="event", domain="dev",
                     source="raw_note", raw_note_path="2026-04-18_004813_demo_plan.md")
    assert r["valid_from"] == "2026-04-18"


def test_other_sources_do_not_derive_a_date(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="x", kind="event", domain="dev",
                     source="session_digest", raw_note_path="/tmp/session-digest-result-abc-XXXXXX.md")
    assert r["valid_from"] is None


def test_malformed_valid_from_is_rejected(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    with pytest.raises(ArchiveValidationError):
        memory_store(conn, content="x", kind="event", domain="dev", valid_from="June 2026")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_resolve_prefers_explicit_over_filename():
    assert resolve_valid_from("2026-01-02", "raw_note", "2026-04-18_004813_x.md") == "2026-01-02"
    assert resolve_valid_from(None, "raw_note", "notes/2026-04-18_004813_x.md") == "2026-04-18"
    assert resolve_valid_from(None, "raw_note", "2026-06-09_export-feature-module.md") == "2026-06-09"
    assert resolve_valid_from(None, "raw_note", "COMPILE_2026-04-09.md") is None
    assert resolve_valid_from(None, None, None) is None


def test_search_since_until_read_valid_from_then_created_at(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="june fact", kind="event", domain="dev", valid_from="2026-06-11")
    memory_store(conn, content="today fact", kind="event", domain="dev")
    june = memory_search(conn, query="fact", mode="hybrid", since="2026-06-01", until="2026-06-30")
    assert [m["content"] for m in june] == ["june fact"]
    # until is inclusive of its whole day
    same_day = memory_search(conn, query="fact", mode="hybrid", since="2026-06-11", until="2026-06-11")
    assert [m["content"] for m in same_day] == ["june fact"]
    later = memory_search(conn, query="fact", mode="hybrid", since="2026-07-01")
    assert [m["content"] for m in later] == ["today fact"]


def test_list_filters_and_orders_by_valid_from(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="b", kind="event", domain="dev", valid_from="2026-05-01")
    memory_store(conn, content="a", kind="event", domain="dev", valid_from="2026-04-01")
    memory_store(conn, content="c", kind="event", domain="dev")  # created now, no valid_from
    r = memory_list(conn, order_by="valid_from", order="asc")
    assert [m["content"] for m in r["items"]] == ["a", "b", "c"]
    r = memory_list(conn, until="2026-05-01")
    assert sorted(m["content"] for m in r["items"]) == ["a", "b"]
