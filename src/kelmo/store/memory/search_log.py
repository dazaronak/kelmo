"""search_log: opt-in telemetry of ranked searches, so real questions can be re-scored later.

One row per successful search: the query variants (or, redacted, only their hashes and lengths), the
filters, the client, whether vectors took part, the ranked result ids, the duration and the retrieval
identity that produced the ranking (index, embedding space, chunk profile, generation, model). The row
is written through its own short connection and transaction, never through the caller's: a failed log
write is logged and swallowed, and logging never commits or rolls back caller work. A configured
retention (age and row count) is applied at every write and again before an operator reads the log.
Changing the mode never rewrites earlier rows. Only the administrator capability reads the table.
"""
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger('kelmo.store')

MODES = ('off', 'redacted', 'full')
DEFAULTS = {'search_log': 'off', 'retention_days': 30, 'max_rows': 5000}
READ_LIMIT = 500
_FILTER_KEYS = ('kind', 'domain', 'project', 'tags', 'since', 'until', 'machine_tag')
_IDENTITY_COLUMNS = {'privacy': 'TEXT', 'variants_json': 'TEXT', 'retrieval_json': 'TEXT'}
_TABLE = '''CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, machine_tag TEXT, client TEXT,
    query_en TEXT NOT NULL, query_ru TEXT NOT NULL, mode TEXT NOT NULL, "limit" INTEGER NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}', semantic INTEGER NOT NULL,
    results_json TEXT NOT NULL DEFAULT '[]', ms INTEGER,
    privacy TEXT, variants_json TEXT, retrieval_json TEXT)'''


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table, or give an older archive's table the identity columns. Inside the caller's transaction."""
    conn.execute(_TABLE)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(search_log)')}
    for column, definition in _IDENTITY_COLUMNS.items():
        if column not in columns:
            conn.execute(f'ALTER TABLE search_log ADD COLUMN {column} {definition}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_search_log_at ON search_log(at)')


def client_name() -> str | None:
    """`name/version` of the MCP client that opened this session, None outside a request. The protocol
    carries the harness, never the model behind it."""
    try:
        from fastmcp.server.dependencies import get_context

        params = get_context().session.client_params
    except Exception:  # noqa: BLE001 - Outside a request there is simply no client.
        return None
    # mcp 1.x spells it clientInfo, mcp 2.x client_info.
    info = getattr(params, 'client_info', None) or getattr(params, 'clientInfo', None)
    if info is None:
        return None
    return f'{info.name}/{info.version}' if getattr(info, 'version', None) else str(info.name)


def _redact(text):
    return {'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'chars': len(text)}


def _row(settings, *, at, variants, mode, limit, filters, semantic, results, ms, retrieval, client,
         machine_tag):
    privacy = settings['search_log']
    set_filters = {key: filters[key] for key in _FILTER_KEYS if filters.get(key) not in (None, [], ())}
    if privacy == 'full':
        stored_variants = list(variants)
        first, second = (list(variants) + ['', ''])[:2]
        stored_filters = set_filters
    else:
        # Query text and filter values never reach the table in redacted mode.
        stored_variants = [_redact(text) for text in variants]
        first = second = ''
        stored_filters = {key: True for key in set_filters}
    ranked = [dict(PURGED) if item.get('purged') else {'id': item['id'], 'chunk_idx': item.get('chunk_idx')}
              for item in results]
    return (at, machine_tag, client, first, second, mode, limit,
            json.dumps(stored_filters, ensure_ascii=False, sort_keys=True), 1 if semantic else 0,
            json.dumps(ranked), int(round(ms)), privacy,
            json.dumps(stored_variants, ensure_ascii=False), json.dumps(retrieval, sort_keys=True))


def record(db_path, settings, *, variants, mode, limit, filters, semantic, results, ms, retrieval,
           client=None, machine_tag=None, timeout=0.25) -> bool:
    """Append one search and enforce retention. Returns whether a row was written; never raises.

    `settings` is the validated [telemetry] table; mode `off` writes nothing."""
    if not settings or settings.get('search_log', 'off') == 'off':
        return False
    connection = None
    try:
        now = datetime.now(timezone.utc)
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True,
                                     timeout=timeout, isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        try:
            # A record purged after the search ranked it, but before this row, never reaches the log.
            results = _without_purged(connection, results)
            row = _row(settings, at=now.isoformat(), variants=variants, mode=mode, limit=limit, filters=filters,
                       semantic=semantic, results=results, ms=ms, retrieval=retrieval, client=client,
                       machine_tag=machine_tag)
            ensure(connection)
            connection.execute(
                'INSERT INTO search_log (at, machine_tag, client, query_en, query_ru, mode, "limit", '
                'filters_json, semantic, results_json, ms, privacy, variants_json, retrieval_json) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
            prune(connection, settings, now=now)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        return True
    except Exception as error:  # noqa: BLE001 - A failed log line must not fail the search.
        logger.warning('search_log: not written: %s', type(error).__name__)
        return False
    finally:
        if connection is not None:
            connection.close()


PURGED = {'id': None, 'chunk_idx': None, 'purged': True}


def _without_purged(conn, results):
    """Results with every purged record replaced by the purged marker; inside the logging transaction."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_purges'").fetchone() is None:
        return list(results)
    ids = sorted({item['id'] for item in results})
    purged = {row[0] for row in conn.execute(
        'SELECT archive_id FROM archive_purges WHERE archive_id IN (SELECT value FROM json_each(?))',
        (json.dumps(ids),))}
    return [dict(PURGED) if item['id'] in purged else item for item in results]


def enforce_retention(db_path, settings, *, timeout=2.0) -> int:
    """Apply a configured retention now, through its own write transaction; raises when it cannot.

    Called before an operator reads the log, so rows past retention_days or max_rows are never shown
    because no search happened since. With no [telemetry] table (settings None) nothing is pruned: the
    retention of rows an older archive wrote before telemetry was configured is not decided here."""
    if not settings:
        return 0
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True,
                                 timeout=timeout, isolation_level=None)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_log'").fetchone() is None:
            return 0
        connection.execute('BEGIN IMMEDIATE')
        try:
            removed = prune(connection, settings)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        return removed
    finally:
        connection.close()


def prune(conn: sqlite3.Connection, settings, *, now=None) -> int:
    """Delete rows older than retention_days and beyond max_rows, newest kept. Caller's transaction."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=settings.get('retention_days', DEFAULTS['retention_days']))).isoformat()
    removed = conn.execute('DELETE FROM search_log WHERE at < ?', (cutoff,)).rowcount
    removed += conn.execute(
        'DELETE FROM search_log WHERE id NOT IN (SELECT id FROM search_log ORDER BY id DESC LIMIT ?)',
        (settings.get('max_rows', DEFAULTS['max_rows']),)).rowcount
    return removed


def read_recent(db_path, limit=READ_LIMIT) -> list[dict]:
    """The last `limit` searches, oldest first, through a read-only connection; [] before any was logged."""
    if type(limit) is not int or not 1 <= limit <= READ_LIMIT:
        raise ValueError(f'limit must be between 1 and {READ_LIMIT}')
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        connection.row_factory = sqlite3.Row
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_log'").fetchone() is None:
            return []
        rows = connection.execute('SELECT * FROM search_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    finally:
        connection.close()
    records = []
    for row in reversed(rows):
        item = dict(row)
        item['filters'] = json.loads(item.pop('filters_json') or '{}')
        item['results'] = json.loads(item.pop('results_json') or '[]')
        for key in ('variants_json', 'retrieval_json'):
            value = item.pop(key, None)
            item[key[:-5]] = json.loads(value) if value else None
        records.append(item)
    return records


def forget_record(conn: sqlite3.Connection, archive_id: str) -> int:
    """Replace a purged record's id in logged results with a purged marker; caller's write transaction."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_log'").fetchone() is None:
        return 0
    changed = 0
    for row_id, results in conn.execute('SELECT id, results_json FROM search_log WHERE instr(results_json, ?) > 0',
                                        (json.dumps(archive_id),)).fetchall():
        ranked = json.loads(results)
        rewritten = [dict(PURGED) if item.get('id') == archive_id else item for item in ranked]
        if rewritten != ranked:
            conn.execute('UPDATE search_log SET results_json=? WHERE id=?', (json.dumps(rewritten), row_id))
            changed += 1
    return changed
