"""Database layer: connection, pragmas, schema init."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import sqlite_vec

from kelmo.errors import KelmoError
from kelmo.store.config import SCHEMA_VERSION, EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_LIBRARY


_SCHEMA_SQL_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def define_function(conn: sqlite3.Connection, name: str, narg: int, func) -> None:
    """Register a deterministic SQL function, keeping one the connection already carries.

    SQLite refuses to redefine a function while a statement on the connection is still
    active; Python 3.13 reports that refusal instead of ignoring it. An existing definition
    is the same function, so it is kept; a missing one still fails here.
    """
    try:
        conn.create_function(name, narg, func, deterministic=True)
    except sqlite3.OperationalError:
        conn.execute("SELECT %s(%s)" % (name, ", ".join(["NULL"] * narg))).fetchone()


def upgrade_history(conn: sqlite3.Connection) -> None:
    """Add version support and only fill fields absent in the earlier archive schema.

    Nullable historical project/validity values are evidence, not missing data.
    The earlier archive kept source/machine/creation/path fixed on the parent record; only
    newly added columns inherit those fields, once, under the write lock.
    """
    if conn.in_transaction:
        raise sqlite3.ProgrammingError("Schema upgrade requires an idle connection")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        if "version" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_versions)")}
        for column in ("source", "machine_tag", "created_at", "raw_note_path"):
            if column not in columns:
                conn.execute(f"ALTER TABLE memory_versions ADD COLUMN {column} TEXT")
                conn.execute(
                    f"UPDATE memory_versions SET {column} = "
                    f"(SELECT {column} FROM memories WHERE id = memory_versions.memory_id)"
                )


_CHUNK_COLUMNS = {
    "profile_id": "TEXT",
    "memory_version": "INTEGER",
    "content_sha256": "TEXT",
    "start_char": "INTEGER",
    "end_char": "INTEGER",
    "text_sha256": "TEXT",
}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def upgrade_retrieval(conn: sqlite3.Connection, *, fts_created: bool) -> None:
    """Give an older chunks table the profile/coordinate columns and index a chunks_fts created just now.

    Additive only: no row, vector or legacy table is rewritten. Existing chunks keep NULL profile
    columns, which makes them history the chunk index never ranks as current.
    """
    if conn.in_transaction:
        raise sqlite3.ProgrammingError("Schema upgrade requires an idle connection")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        for column, definition in _CHUNK_COLUMNS.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE chunks ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_profile ON chunks(profile_id, memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_vectors_chunk ON chunk_vectors(chunk_id)")
        if fts_created:
            # chunks_fts is external-content: rows that predate it are indexed once, here.
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")


OWNER_TABLE = "archive_writer_state"
MANAGED_MESSAGE = ("This archive is managed by Kelmo; standalone archive tools cannot change it. "
                        "Hide a record with `kelmo forget`, remove one with `kelmo admin purge`, and run "
                        "maintenance with `kelmo admin <verb> --vault PATH`.")


def managed_archive(conn: sqlite3.Connection) -> bool:
    """Whether Kelmo owns this archive (its writer identity table exists)."""
    return _table_exists(conn, OWNER_TABLE)


# vec0 tables whose rowid is a chunk id, and the record-level table whose rowid is a memories rowid.
LEGACY_VECTOR_TABLES = ("chunks_vec_m3", "chunks_vec")
RECORD_VECTOR_TABLE = "memories_vec"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preserve_legacy_vectors(conn: sqlite3.Connection, *, chunk_ids=None, memory_rowids=None) -> int:
    """Copy the exact bytes of vectors with no recorded embedding space into unverified_vectors.

    The earlier archive's chunk vec0 tables and memories_vec name no space, so their vectors are history: kept byte for
    byte under a `legacy:<table>` space that is never verified and never ranked. Run inside the caller's
    write transaction before deleting what those tables are keyed by (the earlier archive's chunks_ad trigger and
    update.replace_current drop the rows), so the copy commits or rolls back with that deletion.

    chunk_ids / memory_rowids bound the copy to the rows about to be affected; None for both sweeps
    every row of every such table (orphans included). A rowid is attributed to the record and text it
    points at only when that is an unprofiled legacy chunk or an existing record. This is an observed
    association, never proof of the vector's derivation. Otherwise, and for an
    orphan, the source stays NULL rather than being guessed. Returns the vectors copied now; a repeat of
    the same bytes under the same rowid copies nothing, a different generation is a new row.
    """
    if not conn.in_transaction:
        raise sqlite3.ProgrammingError("Legacy vector preservation requires the caller's write transaction")
    sweep = chunk_ids is None and memory_rowids is None
    targets = [(table, None if sweep else list(chunk_ids or ())) for table in LEGACY_VECTOR_TABLES]
    targets.append((RECORD_VECTOR_TABLE, None if sweep else list(memory_rowids or ())))
    claims = None
    copied = 0
    stamp = now_iso()
    for table, rowids in targets:
        if (rowids is not None and not rowids) or not _table_exists(conn, table):
            continue
        if rowids is None:
            rows = conn.execute(f"SELECT rowid, embedding FROM {table}").fetchall()
        else:
            rows = [row for row in (conn.execute(f"SELECT rowid, embedding FROM {table} WHERE rowid = ?",
                                                 (rowid,)).fetchone() for rowid in rowids) if row is not None]
        if not rows:
            continue
        if claims is None:
            claims = {row[0]: row[1] for row in conn.execute(
                "SELECT key, value FROM archive_meta WHERE key LIKE 'embedding%' OR key LIKE 'chunk%'")}
        space_id = "legacy:" + table
        conn.execute(
            "INSERT OR IGNORE INTO embedding_spaces (space_id, space_json, verified, note, created_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (space_id, json.dumps({"legacy_table": table, "claims": claims}, sort_keys=True),
             "embedding space unknown; preserved history, never ranked", stamp),
        )
        for rowid, embedding in rows:
            memory_id = idx = text_hash = None
            if table == RECORD_VECTOR_TABLE:
                source = conn.execute("SELECT id, content FROM memories WHERE rowid = ?", (rowid,)).fetchone()
                if source is not None:
                    memory_id, text_hash = source[0], _sha256(source[1].encode())
            else:
                # A chunk the current chunker cut never had a vector in these tables: its rowid was reused.
                source = conn.execute(
                    "SELECT memory_id, idx, text FROM chunks WHERE id = ? AND profile_id IS NULL", (rowid,)
                ).fetchone()
                if source is not None:
                    memory_id, idx, text_hash = source[0], source[1], _sha256(source[2].encode())
            copied += conn.execute(
                "INSERT OR IGNORE INTO unverified_vectors (space_id, source_table, source_rowid, "
                "embedding_sha256, embedding, memory_id, idx, text_sha256, preserved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (space_id, table, rowid, _sha256(embedding), embedding, memory_id, idx, text_hash, stamp),
            ).rowcount
    return copied


NO_SQLITE_EXTENSIONS = ("This Python was built without SQLite extension support, which Kelmo needs for vector search. "
                        "Reinstall Kelmo on a Python that has it: uv tool install --managed-python --python 3.13 kelmo")


def open_connection(path: str) -> sqlite3.Connection:
    """Open SQLite connection with pragmas and sqlite-vec loaded."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level="DEFERRED", check_same_thread=False)
    if not hasattr(conn, "enable_load_extension"):
        conn.close()
        raise KelmoError("E_STATE", NO_SQLITE_EXTENSIONS)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = FULL;
        PRAGMA foreign_keys = ON;
        PRAGMA temp_store = MEMORY;
        PRAGMA mmap_size = 268435456;
        PRAGMA cache_size = -64000;
        PRAGMA busy_timeout = 5000;
    """)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql and populate archive_meta if fresh DB."""
    # executescript would first commit whatever the caller has pending.
    if conn.in_transaction:
        raise sqlite3.ProgrammingError("Schema initialization requires an idle connection")
    fts_existed = _table_exists(conn, "chunks_fts")
    with open(_SCHEMA_SQL_PATH, encoding='utf-8') as f:
        conn.executescript(f.read())
    upgrade_history(conn)
    upgrade_retrieval(conn, fts_created=not fts_existed)

    now = datetime.now(timezone.utc).isoformat()
    meta_defaults = {
        "schema_version": str(SCHEMA_VERSION),
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": str(EMBEDDING_DIM),
        "embedding_library": EMBEDDING_LIBRARY,
        "created_at": now,
    }
    for key, value in meta_defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO archive_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()
