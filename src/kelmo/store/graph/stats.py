"""Structural graph counts; freshness belongs to the selected Service."""

import sqlite3


def graph_stats(conn: sqlite3.Connection) -> dict:
    entities = conn.execute('SELECT COUNT(*) FROM entities').fetchone()[0]
    edges = conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
    by_type = dict(conn.execute('SELECT type,COUNT(*) FROM entities GROUP BY type ORDER BY type'))
    by_relation = {}
    for relation, count in conn.execute('SELECT relation,COUNT(*) FROM edges GROUP BY relation ORDER BY relation'):
        key = relation.casefold()
        by_relation[key] = by_relation.get(key, 0) + count
    avg_in, avg_out = conn.execute('SELECT AVG(in_degree),AVG(out_degree) FROM entities').fetchone()
    documents = conn.execute('SELECT COUNT(*) FROM entities WHERE file_path IS NOT NULL').fetchone()[0]
    unresolved = conn.execute("SELECT COUNT(*) FROM entities WHERE substr(id,1,11)='unresolved:'").fetchone()[0]
    return {
        'entities': entities, 'edges': edges, 'documents': documents,
        'by_type': by_type, 'by_relation': by_relation,
        'avg_in_degree': round(avg_in or 0, 2), 'avg_out_degree': round(avg_out or 0, 2),
        'unresolved_targets': unresolved,
    }
