"""Forgotten archive records: hidden from readers, never removed from storage."""
from kelmo.store.db import now_iso

from .errors import KelmoError

TABLE = 'record_visibility'
SCHEMA = '''CREATE TABLE IF NOT EXISTS record_visibility (
    id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    forgotten_at TEXT NOT NULL,
    reason TEXT
)'''


def ensure(connection):
    """Add the marker table to an archive database created before it existed."""
    with connection:
        connection.execute(SCHEMA)


def forgotten_at(connection, record_id):
    row = connection.execute('SELECT forgotten_at FROM record_visibility WHERE id=?',
                             (record_id,)).fetchone()
    return row['forgotten_at'] if row else None


def is_forgotten(connection, record_id):
    return forgotten_at(connection, record_id) is not None


def count(connection):
    return connection.execute('SELECT COUNT(*) FROM record_visibility').fetchone()[0]


def hide(connection, record_id, *, reason=None):
    """Mark one stored record hidden. Returns True only when this call changed the state."""
    if connection.in_transaction:
        raise KelmoError('E_STATE', 'Visibility mutation requires an idle connection.')
    if not isinstance(record_id, str) or not record_id.strip():
        raise KelmoError('E_INPUT', 'Provide the archive record id.', 2)
    with connection:
        cursor = connection.execute(
            'INSERT INTO record_visibility (id, forgotten_at, reason) VALUES (?,?,?) '
            'ON CONFLICT(id) DO NOTHING', (record_id, now_iso(), reason))
        return cursor.rowcount == 1
