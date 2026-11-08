"""Tests for database layer."""
import os
import tempfile

import pytest

from kelmo.errors import KelmoError
from kelmo.store.db import open_connection, init_schema


def test_python_without_sqlite_extensions_names_the_reinstall(monkeypatch):
    class NoExtensions:
        closed = False

        def close(self):
            self.closed = True

    connection = NoExtensions()
    monkeypatch.setattr("kelmo.store.db.sqlite3.connect", lambda *args, **kwargs: connection)
    with pytest.raises(KelmoError) as caught:
        open_connection(":memory:")
    assert caught.value.code == "E_STATE"
    assert "uv tool install --managed-python" in str(caught.value)
    assert connection.closed


def test_open_connection_loads_sqlite_vec():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        row = conn.execute("SELECT vec_version()").fetchone()
        assert row is not None
        conn.close()
    finally:
        os.unlink(path)


def test_open_connection_sets_wal_mode():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()
    finally:
        os.unlink(path)


def test_open_connection_foreign_keys_on():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()
    finally:
        os.unlink(path)


def test_init_schema_creates_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        init_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()}
        assert "memories" in tables
        assert "memories_fts" in tables
        assert "entities" in tables
        assert "edges" in tables
        assert "entities_fts" in tables
        assert "archive_meta" in tables
        conn.close()
    finally:
        os.unlink(path)


def test_init_schema_sets_version():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        init_schema(conn)
        row = conn.execute(
            "SELECT value FROM archive_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row[0] == "1"
        conn.close()
    finally:
        os.unlink(path)


def test_init_schema_idempotent():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        init_schema(conn)
        init_schema(conn)  # must not raise
        conn.close()
    finally:
        os.unlink(path)


def test_context_manager_atomicity():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = open_connection(path)
        init_schema(conn)

        with conn:
            conn.execute(
                "INSERT INTO archive_meta (key, value) VALUES ('test_key', 'test_val')"
            )
        row = conn.execute(
            "SELECT value FROM archive_meta WHERE key = 'test_key'"
        ).fetchone()
        assert row[0] == "test_val"

        try:
            with conn:
                conn.execute(
                    "INSERT INTO archive_meta (key, value) VALUES ('bad_key', 'bad_val')"
                )
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass

        row = conn.execute(
            "SELECT value FROM archive_meta WHERE key = 'bad_key'"
        ).fetchone()
        assert row is None
        conn.close()
    finally:
        os.unlink(path)
