"""memory_delete: remove a memory by id; purge_rows: the archive half of an administrator purge."""
import sqlite3

from kelmo.store.db import (MANAGED_MESSAGE, LEGACY_VECTOR_TABLES, _table_exists, managed_archive,
                        preserve_legacy_vectors)
from kelmo.store.errors import ArchiveNotFoundError, ArchiveStorageError


def memory_delete(conn: sqlite3.Connection, *, id: str) -> dict:
    """Delete memory by id. Triggers auto-remove vec + fts rows.

    This physical delete is the existing admin action, not hiding. Vectors that name no embedding space
    (the earlier archive's chunk vec0 tables and memories_vec) are first copied byte for byte into unverified_vectors, in
    the same transaction, so the delete never drops a vector whose source is unknown; vectors of a verified
    space are derived data and leave with their chunks.

    Only an unmanaged standalone archive database: an archive Kelmo owns is refused, because this
    delete writes no receipt or tombstone and would bypass the coordinated purge.
    """
    if conn.in_transaction:
        raise ArchiveStorageError("Delete requires an idle connection", operation="memory_delete")
    if managed_archive(conn):
        raise ArchiveStorageError(MANAGED_MESSAGE, operation="memory_delete")
    row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (id,)).fetchone()
    if row is None:
        raise ArchiveNotFoundError(f"Memory not found: {id}", id=id, entity_type="memory")

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ArchiveNotFoundError(f"Memory not found: {id}", id=id, entity_type="memory")
        chunk_ids = [chunk[0] for chunk in conn.execute("SELECT id FROM chunks WHERE memory_id = ?", (id,))]
        preserve_legacy_vectors(conn, chunk_ids=chunk_ids, memory_rowids=[row[0]])
        conn.execute("DELETE FROM chunks WHERE memory_id = ?", (id,))
        conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    return {"deleted": True}


def purge_rows(conn: sqlite3.Connection, archive_id: str) -> dict:
    """Remove one record and every archive-local copy derived from its text, in the caller's write
    transaction. Returns the rows each effect removed.

    Unlike memory_delete nothing derived from this record is preserved: its record vector, its chunk
    vectors in every space, the unverified copies attributed to it and the legacy vec0 rows of its own
    (unprofiled) chunks all go. A vec0 row keyed by a profiled chunk id was cut from other text (the id
    was reused), so it is copied to unverified_vectors first, unattributed, like any other delete. FTS
    postings leave through the delete triggers and both indexes are then optimized, so the deleted
    entries are merged out of the index segments instead of lingering as delete markers. Rows that
    belong to the coordinator (receipts, source packets) are the caller's to remove first.
    """
    if not conn.in_transaction:
        raise ArchiveStorageError("Purge requires the caller's write transaction", operation="purge_rows")
    record = conn.execute("SELECT rowid FROM memories WHERE id = ?", (archive_id,)).fetchone()
    if record is None:
        raise ArchiveNotFoundError(f"Memory not found: {archive_id}", id=archive_id, entity_type="memory")
    chunks = conn.execute("SELECT id, profile_id FROM chunks WHERE memory_id = ?", (archive_id,)).fetchall()
    own = [row[0] for row in chunks if row[1] is None]
    foreign = [row[0] for row in chunks if row[1] is not None]
    preserve_legacy_vectors(conn, chunk_ids=foreign)
    effects = {}

    def run(name, sql, parameters):
        effects[name] = effects.get(name, 0) + conn.execute(sql, parameters).rowcount

    run("chunk_vectors", "DELETE FROM chunk_vectors WHERE memory_id = ?", (archive_id,))
    run("unverified_vectors", "DELETE FROM unverified_vectors WHERE memory_id = ?", (archive_id,))
    for table in LEGACY_VECTOR_TABLES:
        if _table_exists(conn, table) and own:
            for chunk_id in own:
                run(table, f"DELETE FROM {table} WHERE rowid = ?", (chunk_id,))
    run("chunks", "DELETE FROM chunks WHERE memory_id = ?", (archive_id,))
    run("memory_versions", "DELETE FROM memory_versions WHERE memory_id = ?", (archive_id,))
    run("record_visibility", "DELETE FROM record_visibility WHERE id = ?", (archive_id,))
    if _table_exists(conn, "record_supersession"):
        run("record_supersession", "DELETE FROM record_supersession WHERE id = ? OR supersedes = ?",
            (archive_id, archive_id))
    # memories_ad removes the record's FTS postings and its memories_vec row.
    effects["memories_vec"] = conn.execute("SELECT COUNT(*) FROM memories_vec WHERE rowid = ?",
                                           (record[0],)).fetchone()[0]
    run("memories", "DELETE FROM memories WHERE id = ?", (archive_id,))
    from kelmo.store.memory.search_log import forget_record
    effects["search_log_results"] = forget_record(conn, archive_id)
    for index in ("memories_fts", "chunks_fts"):
        conn.execute(f"INSERT INTO {index}({index}) VALUES('optimize')")
    return effects
