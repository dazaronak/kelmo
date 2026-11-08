"""Atomicity: a failed embedding writes nothing, and a successful store commits the row, vector and FTS entry together."""
from unittest.mock import patch
from kelmo.store.memory.store import memory_store
import pytest


def test_atomicity_embed_failure_no_db_write(tmp_db, monkeypatch):
    """If embed() fails, no DB write should happen."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db

    # Patch at the store module level (where embed is imported)
    with patch("kelmo.store.memory.store.embed", side_effect=Exception("embed crash")):
        with pytest.raises(Exception):
            memory_store(conn, content="should not persist", kind="decision", domain="dev")

    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert row[0] == 0


def test_atomicity_success_commits_all(tmp_db, mock_embed, monkeypatch):
    """Successful store commits memories + vec + fts atomically."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    r = memory_store(conn, content="atomicity test content", kind="event", domain="dev")

    mem = conn.execute("SELECT rowid FROM memories WHERE id = ?", (r["id"],)).fetchone()
    assert mem is not None
    rowid = mem["rowid"]

    vec = conn.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
    assert vec is not None

    fts = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH '\"atomicity\"'"
    ).fetchone()
    assert fts is not None
