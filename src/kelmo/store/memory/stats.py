"""memory_stats: storage statistics."""
import json
import os
import sqlite3



def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
                        (name,)).fetchone() is not None


def memory_stats(conn: sqlite3.Connection, *, db_path: str | None = None, profile_id: str | None = None,
                 space_id: str | None = None) -> dict:
    """Return storage statistics.

    db_path names the database whose file size is reported (default: the legacy configured path).
    With profile_id and space_id (a configured chunk index) pending vectors are current chunks of that
    profile without a verified vector of that space; without them, records without a record vector.
    Read-only; no deployment path or provider setting is returned."""
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    by_kind = {}
    for row in conn.execute("SELECT kind, COUNT(*) AS cnt FROM memories GROUP BY kind"):
        by_kind[row[0]] = row[1]

    by_domain = {}
    for row in conn.execute("SELECT domain, COUNT(*) AS cnt FROM memories GROUP BY domain"):
        by_domain[row[0]] = row[1]

    by_project = {}
    for row in conn.execute(
        "SELECT COALESCE(project, '(none)') AS p, COUNT(*) AS cnt FROM memories GROUP BY project"
    ):
        by_project[row[0]] = row[1]

    latest = conn.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]

    meta = {}
    for row in conn.execute("SELECT key, value FROM archive_meta"):
        meta[row[0]] = row[1]

    if db_path is None:
        main = [row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
        db_path = main[0] if main else ""
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    result = {
        "total": total,
        "by_kind": by_kind,
        "by_domain": by_domain,
        "by_project": by_project,
        "db_size_bytes": db_size,
        "latest_created_at": latest,
        "embedding_model": meta.get("embedding_model", ""),
        "embedding_dim": int(meta.get("embedding_dim", "0")),
        "embedding_library": meta.get("embedding_library", ""),
        "schema_version": int(meta.get("schema_version", "0")),
    }
    result.update(_index_stats(conn, profile_id, space_id))
    if _table(conn, "search_log"):
        searches, last_search = conn.execute("SELECT COUNT(*), MAX(at) FROM search_log").fetchone()
        result.update(searches_logged=searches, last_search_at=last_search)
    return result


def _index_stats(conn, profile_id, space_id):
    if not _table(conn, "chunks"):
        return {}
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if profile_id is None:
        stats = {"chunks": chunks, "vector_index": "record"}
        if _table(conn, "memories_vec"):
            try:
                stats["vectors_pending"] = conn.execute(
                    "SELECT COUNT(*) FROM memories m WHERE NOT EXISTS "
                    "(SELECT 1 FROM memories_vec v WHERE v.rowid = m.rowid)").fetchone()[0]
            except sqlite3.OperationalError:
                # A read connection without the vector extension cannot count vec0 rows.
                stats["vectors_pending"] = None
        return stats
    from kelmo.store.memory.chunk import current_chunks

    current = sorted(current_chunks(conn, profile_id, "1 = 1", ()))
    chunked = conn.execute(
        "SELECT COUNT(DISTINCT memory_id) FROM chunks WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(current),)).fetchone()[0]
    covered = 0
    if space_id is not None:
        covered = conn.execute(
            "SELECT COUNT(*) FROM chunks c WHERE c.id IN (SELECT value FROM json_each(?)) AND EXISTS ("
            "SELECT 1 FROM chunk_vectors v JOIN embedding_spaces s ON s.space_id = v.space_id AND s.verified = 1 "
            "WHERE v.space_id = ? AND v.chunk_id = c.id AND v.text_sha256 = c.text_sha256)",
            (json.dumps(current), space_id)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    return {"chunks": chunks, "vector_index": "chunk", "profile_id": profile_id, "space_id": space_id,
            "current_chunks": len(current), "chunks_without_vectors": len(current) - covered,
            "vectors_pending": len(current) - covered, "memories_without_chunks": total - chunked}
