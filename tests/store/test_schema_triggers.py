"""Tests for schema triggers: verify FTS/vec sync on INSERT/UPDATE/DELETE."""
from kelmo.store.memory.store import memory_store
from kelmo.store.db import now_iso


def test_fts_trigger_on_insert(tmp_db, mock_embed, monkeypatch):
    """INSERT into memories auto-populates memories_fts."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="trigger test insert zzzzz", kind="event", domain="dev")
    row = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"zzzzz\"'"
    ).fetchone()
    assert row is not None


def test_fts_trigger_on_update(tmp_db, mock_embed, monkeypatch):
    """UPDATE memories content updates memories_fts (delete-sentinel pattern)."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="before update yyyyy", kind="event", domain="dev")

    # Direct UPDATE (bypassing store.py — tests the trigger)
    conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        ("after update xxxxx", now_iso(), r["id"]),
    )
    conn.commit()

    # Old content gone from FTS
    old = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"yyyyy\"'"
    ).fetchone()
    assert old is None

    # New content in FTS
    new = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"xxxxx\"'"
    ).fetchone()
    assert new is not None


def test_fts_trigger_on_delete(tmp_db, mock_embed, monkeypatch):
    """DELETE from memories — search returns no results."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    from kelmo.store.memory.delete import memory_delete
    r = memory_store(conn, content="delete trigger wwwww", kind="event", domain="dev")
    memory_delete(conn, id=r["id"])

    from kelmo.store.memory.search import memory_search
    results = memory_search(conn, query="wwwww", mode="hybrid")
    assert len(results) == 0


def test_vec_trigger_on_delete(tmp_db, mock_embed, monkeypatch):
    """DELETE from memories removes from memories_vec."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="vec delete test", kind="event", domain="dev")
    rowid = conn.execute("SELECT rowid FROM memories WHERE id = ?", (r["id"],)).fetchone()["rowid"]

    conn.execute("DELETE FROM memories WHERE id = ?", (r["id"],))
    conn.commit()

    vec = conn.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
    assert vec is None


def test_entity_fts_trigger_on_insert(tmp_db):
    """INSERT into entities auto-populates entities_fts."""
    conn, _ = tmp_db
    now = "2026-04-14T00:00:00Z"
    conn.execute(
        "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("test-ent", "TestEntity", "CONCEPT", now, now),
    )
    conn.commit()

    row = conn.execute(
        "SELECT rowid FROM entities_fts WHERE entities_fts MATCH 'TestEnt'"
    ).fetchone()
    assert row is not None


def test_deleted_memory_has_no_fts_postings(tmp_db, mock_embed):
    from kelmo.store.memory.delete import memory_delete
    conn, _ = tmp_db
    stored = memory_store(conn, content='uniquedeletedtoken', kind='event', domain='dev')
    memory_delete(conn, id=stored['id'])
    assert conn.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'uniquedeletedtoken'").fetchall() == []
    conn.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")


def test_deleted_entity_has_no_fts_postings(tmp_db):
    conn, _ = tmp_db
    conn.execute("INSERT INTO entities(id,name,type,created_at,updated_at) VALUES('ghost','GhostEntity','CONCEPT','2026-01-01','2026-01-01')")
    conn.commit()
    conn.execute("DELETE FROM entities WHERE id='ghost'")
    conn.commit()
    assert conn.execute("SELECT rowid FROM entities_fts WHERE entities_fts MATCH 'GhostEntity'").fetchall() == []
    conn.execute("INSERT INTO entities_fts(entities_fts, rank) VALUES('integrity-check', 1)")
