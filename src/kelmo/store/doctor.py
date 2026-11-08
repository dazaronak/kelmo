"""Archive health checks for the administrator doctor.

dependency_checks, archive_checks and overall report PASS, WARN (degraded), FAIL (failed) and SKIP
(not applicable or not probed).
"""

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


def _check(name, fn, hint=""):
    try:
        fn()
        return {"name": name, "status": "PASS", "hint": ""}
    except Exception as e:
        return {"name": name, "status": "FAIL", "hint": hint or str(e)}


def dependency_checks(*, embedding_probe=None, tokenizer_probe=None) -> list[dict]:
    """Local libraries and the selected archive's model dependencies.

    The probes are callables supplied by the selected vault (its configured provider's health and input
    window); a probe left out is reported SKIP, never as a passing service. A probe that raises is FAIL
    for the tokenizer, which every write needs, and WARN for the embedding service, whose outage keeps
    keyword search and pending vectors."""
    results = []

    def check_vec():
        import sqlite3
        import sqlite_vec
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("SELECT vec_version()")
    results.append(_check("sqlite_vec_loads", check_vec, "sqlite-vec is missing, or this Python has no SQLite extension support"))

    def check_fts5():
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _test_fts USING fts5(content)")
        conn.execute("DROP TABLE _test_fts")
    results.append(_check("fts5_available", check_fts5, "SQLite without FTS5"))

    def check_schema():
        from kelmo.store.db import open_connection, init_schema
        conn = open_connection(":memory:")
        init_schema(conn)
        assert conn.execute("SELECT value FROM archive_meta WHERE key='schema_version'").fetchone() is not None
    results.append(_check("schema_valid", check_schema, "schema.sql parse error"))

    def check_taxonomy():
        from kelmo.store.memory.taxonomy import validate_kind, validate_domain
        validate_kind("decision")
        validate_domain("dev")
    results.append(_check("taxonomy_valid", check_taxonomy, "taxonomy validation error"))

    for name, probe, failure, hint in (
            ("tokenizer_present", tokenizer_probe, FAIL,
             "the counting tokenizer is unavailable: nothing can be chunked or stored with chunks"),
            ("embedding_service", embedding_probe, WARN,
             "no vectors: search runs on keywords only and new vectors stay pending")):
        if probe is None:
            results.append({"name": name, "status": SKIP, "hint": "not probed"})
            continue
        result = _check(name, probe, hint)
        if result["status"] == FAIL:
            result["status"] = failure
        results.append(result)
    return results


def archive_checks(db_path, *, profile_id=None, space_id=None) -> list[dict]:
    """State of one archive database, read through a read-only connection without modifying it.

    With profile_id/space_id (a configured chunk index): records without current chunks and current
    chunks without a verified vector are WARN (keyword search still finds them; reembed/backfill repair
    them). Without them, records without a record vector are WARN."""
    import sqlite3
    from pathlib import Path

    names = ("archive_readable", "chunk_tables_exist", "memories_all_chunked", "chunks_all_vectored")
    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        return [{"name": name, "status": FAIL, "hint": "archive database is missing"} for name in names]

    def connect():
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn

    results = []
    try:
        conn = connect()
    except sqlite3.Error as error:
        return [{"name": name, "status": FAIL, "hint": type(error).__name__} for name in names]
    try:
        def readable():
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "integrity check failed"
            assert conn.execute("SELECT value FROM archive_meta WHERE key='schema_version'").fetchone(), \
                "schema version is missing"
        results.append(_check("archive_readable", readable))

        wanted = {"chunks", "chunks_fts", "chunk_vectors", "embedding_spaces", "chunk_profiles"}

        def tables():
            found = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            assert wanted <= found, f"missing: {sorted(wanted - found)}"
        results.append(_check("chunk_tables_exist", tables, "the archive schema is incomplete; run kelmo doctor"))
        if results[-1]["status"] == FAIL:
            results.extend({"name": name, "status": SKIP, "hint": "chunk tables are missing"} for name in names[2:])
            return results
        results.extend(_coverage(conn, profile_id, space_id))
    finally:
        conn.close()
    return results


def _coverage(conn, profile_id, space_id):
    import json
    import sqlite3

    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    if profile_id is None:
        chunked = {"name": "memories_all_chunked", "status": SKIP, "hint": "no chunk index is configured"}
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            pending = conn.execute("SELECT COUNT(*) FROM memories m WHERE NOT EXISTS "
                                   "(SELECT 1 FROM memories_vec v WHERE v.rowid = m.rowid)").fetchone()[0]
        except (ImportError, AttributeError, sqlite3.Error) as error:
            return [chunked, {"name": "chunks_all_vectored", "status": FAIL, "hint": type(error).__name__}]
        vectored = ({"name": "chunks_all_vectored", "status": PASS, "hint": ""} if not pending else
                    {"name": "chunks_all_vectored", "status": WARN,
                     "hint": f"{pending} of {total} records have no record vector yet"})
        return [chunked, vectored]
    from kelmo.store.memory.chunk import current_chunks

    current = sorted(current_chunks(conn, profile_id, "1 = 1", ()))
    encoded = json.dumps(current)
    chunked = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM chunks WHERE id IN (SELECT value FROM json_each(?))",
                           (encoded,)).fetchone()[0]
    results = [{"name": "memories_all_chunked", "status": PASS, "hint": ""} if chunked == total else
               {"name": "memories_all_chunked", "status": WARN,
                "hint": f"{total - chunked} records have no current chunks (run: kelmo admin reembed)"}]
    covered = 0 if space_id is None else conn.execute(
        "SELECT COUNT(*) FROM chunks c WHERE c.id IN (SELECT value FROM json_each(?)) AND EXISTS ("
        "SELECT 1 FROM chunk_vectors v JOIN embedding_spaces s ON s.space_id = v.space_id AND s.verified = 1 "
        "WHERE v.space_id = ? AND v.chunk_id = c.id AND v.text_sha256 = c.text_sha256)",
        (encoded, space_id)).fetchone()[0]
    missing = len(current) - covered
    results.append({"name": "chunks_all_vectored", "status": PASS, "hint": ""} if not missing else
                   {"name": "chunks_all_vectored", "status": WARN,
                    "hint": f"{missing} of {len(current)} current chunks have no verified vector yet "
                            "(run: kelmo admin backfill)"})
    return results


def overall(results) -> str:
    """healthy: nothing failed or degraded; degraded: a WARN only; failed: any FAIL."""
    statuses = {result["status"] for result in results}
    return "failed" if FAIL in statuses else "degraded" if WARN in statuses else "healthy"

