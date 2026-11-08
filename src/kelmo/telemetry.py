"""The search log of a server whose archive runs confined: a database of its own in the state root.

A local vault logs searches into the archive (kelmo.store.memory.search_log). A server's archive belongs to
its confined child, which stays the only writer of archive.db, so the parent process, which already holds
the ranked result, the requesting client and its machine, writes the same rows to
`<state root>/telemetry.sqlite3` instead. The rows have the archive table's shape and
privacy modes; a purge rewrites a purged record's id there as it does in the archive. Telemetry never
fails a search.
"""
import os
import sqlite3

FILE = 'telemetry.sqlite3'


def path(config):
    return config.state / FILE


def ensure(config):
    """The telemetry database path, created owner-only on first use."""
    target = path(config)
    try:
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        pass
    return target


def forget(config, archive_id):
    """Replace a purged record's id in logged results with the purged marker; 0 without a log."""
    from kelmo.store.memory.search_log import forget_record
    target = path(config)
    if not target.exists():
        return 0
    connection = sqlite3.connect(target.resolve().as_uri() + '?mode=rw', uri=True, timeout=5,
                                 isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        try:
            changed = forget_record(connection, archive_id)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        return changed
    finally:
        connection.close()


def count(config):
    """Logged searches in the telemetry database, 0 without one."""
    target = path(config)
    if not target.exists():
        return 0
    connection = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_log'").fetchone() is None:
            return 0
        return connection.execute('SELECT COUNT(*) FROM search_log').fetchone()[0]
    finally:
        connection.close()
