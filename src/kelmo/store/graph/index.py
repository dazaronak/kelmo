"""entities_fts: the wiki name index, and how to rebuild it from the entities table.

entities_fts is an FTS5 external-content table: it keeps no text of its own, only postings that point at
rowids of `entities`. A deletion trigger that cannot name the row's text removes no postings, and a posting
left at a free rowid stays invisible until some other entity takes that rowid; then the dead key answers
with the newer record. graph_query reads this index, so rebuilding it from `entities` is the repair.

The caller owns the transaction, like every other writer here.
"""
import sqlite3


def rebuild_entity_index(conn: sqlite3.Connection) -> dict:
    """Rebuild entities_fts from the entities table: the index ends up holding exactly what `entities`
    holds now. Idempotent, and cheap enough to run on every graph publication."""
    conn.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
    return {'entities': conn.execute('SELECT COUNT(*) FROM entities').fetchone()[0]}
