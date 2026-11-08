"""Supersession links: a newer archive record that replaces an older one.

The older record is forgotten (hidden, never removed) when the newer one is archived, so a reader that takes the
top search hit cannot receive the claim it corrects. The link is kept in both directions for `show`.
"""
from kelmo.store.db import now_iso

from .errors import KelmoError

SCHEMA = '''CREATE TABLE IF NOT EXISTS record_supersession (
    id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    supersedes TEXT NOT NULL,
    created_at TEXT NOT NULL
)'''


def ensure(connection):
    """Add the link table to an archive database created before it existed."""
    with connection:
        connection.execute(SCHEMA)
        connection.execute('CREATE INDEX IF NOT EXISTS record_supersession_target ON record_supersession(supersedes)')


def link(connection, record_id, superseded_id):
    """Record that record_id replaces superseded_id. Repeating the same link changes nothing."""
    if connection.in_transaction:
        raise KelmoError('E_STATE', 'Supersession mutation requires an idle connection.')
    with connection:
        connection.execute('INSERT INTO record_supersession (id, supersedes, created_at) VALUES (?,?,?) '
                           'ON CONFLICT(id) DO NOTHING', (record_id, superseded_id, now_iso()))
    row = connection.execute('SELECT supersedes FROM record_supersession WHERE id=?', (record_id,)).fetchone()
    return row['supersedes']


def links(connection, record_id):
    """The record this one replaces and the records that replace it, oldest first."""
    row = connection.execute('SELECT supersedes FROM record_supersession WHERE id=?', (record_id,)).fetchone()
    newer = [item['id'] for item in connection.execute(
        'SELECT id FROM record_supersession WHERE supersedes=? ORDER BY created_at, id', (record_id,))]
    return {'supersedes': row['supersedes'] if row else None, 'superseded_by': newer}


def head(connection, record_id):
    """The newest record of the chain record_id belongs to: follow the first replacement until none is left."""
    seen = {record_id}
    while True:
        newer = links(connection, record_id)['superseded_by']
        if not newer or newer[0] in seen:
            return record_id
        record_id = newer[0]
        seen.add(record_id)


def count(connection):
    return connection.execute('SELECT COUNT(*) FROM record_supersession').fetchone()[0]
