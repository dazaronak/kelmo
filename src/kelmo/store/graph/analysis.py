"""Deterministic structural discovery over explicit wiki document relationships.

Communities, hubs and surprise scores are derived topology suggestions computed from the published
snapshot's explicit links. They are never verified factual relationships and never evidence that one
page depends on another. Construction order is deterministic and Louvain runs with a fixed seed, so the
same snapshot yields the same groups.
"""

import hashlib
import json
import math
import sqlite3

import networkx as nx

ALGORITHM = 'louvain'
SEED = 0
ANALYSIS_VERSION = 1
DEFAULT_RELATIONS = ('links_to', 'child_of')
LABEL = {
    'kind': 'derived_topology', 'verified': False, 'algorithm': ALGORITHM, 'seed': SEED,
    'note': 'Structural suggestion from explicit wiki links; not a verified factual relationship.',
}


def _records(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    fields = [column[0] for column in cursor.description]
    return [dict(zip(fields, row)) for row in cursor]


def _allowed(relation_filter):
    return ({relation.casefold() for relation in relation_filter} if relation_filter is not None
            else set(DEFAULT_RELATIONS))


def document_graph(conn: sqlite3.Connection, relation_filter=None) -> nx.Graph:
    """Undirected graph of wiki documents only: tags and unresolved targets are not structure."""
    graph = nx.Graph()
    for row in _records(conn, 'SELECT * FROM entities WHERE file_path IS NOT NULL ORDER BY id'):
        graph.add_node(row['id'], **row)
    allowed = _allowed(relation_filter)
    for row in _records(conn, 'SELECT * FROM edges ORDER BY source_id,target_id,relation'):
        if row['relation'].casefold() in allowed and row['source_id'] in graph and row['target_id'] in graph:
            # A bidirectional wiki link counts as one structural connection.
            graph.add_edge(row['source_id'], row['target_id'], weight=max(0.0, row['weight'] or 1.0))
    return graph


def hub_threshold(graph: nx.Graph) -> int:
    """Flag nodes far above typical degree, with a minimum to protect small graphs."""
    if not graph:
        return 5
    degrees = sorted(degree for _, degree in graph.degree())
    median = degrees[len(degrees) // 2]
    percentile_99 = degrees[int(0.99 * (len(degrees) - 1))]
    return max(4, math.ceil(1.5 * median), percentile_99)


def analyze_graph(conn: sqlite3.Connection) -> dict:
    """Write community_id, is_god_node and surprise_score for the rows just published.

    The caller owns the transaction (graph publication runs this inside its savepoint)."""
    graph = document_graph(conn)
    threshold = hub_threshold(graph)
    hubs = {node for node, degree in graph.degree() if degree > threshold}
    core = graph.subgraph(set(graph) - hubs).copy()
    if core.number_of_edges():
        communities = nx.community.louvain_communities(core, weight='weight', seed=SEED)
    else:
        # Explicit fallback: without edges every document is its own group.
        communities = [{node} for node in core]
    groups = sorted((sorted(group) for group in communities), key=lambda group: group[0])
    conn.execute('UPDATE entities SET community_id=NULL, surprise_score=0, is_god_node=0')
    for node in sorted(hubs):
        conn.execute('UPDATE entities SET is_god_node=1 WHERE id=?', (node,))
    for index, members in enumerate(groups):
        conn.executemany('UPDATE entities SET community_id=? WHERE id=?', [(index, node) for node in members])
    for item in surprising_edges(conn):
        for node in (item['source_id'], item['target_id']):
            conn.execute('UPDATE entities SET surprise_score=MAX(COALESCE(surprise_score,0),?) WHERE id=?',
                         (item['score'], node))
    return {'algorithm': ALGORITHM, 'seed': SEED, 'analysis_version': ANALYSIS_VERSION,
            'communities': len(groups), 'god_nodes': len(hubs), 'hub_degree_threshold': threshold}


def surprising_edges(conn: sqlite3.Connection, relation_filter=None) -> list[dict]:
    """Rank observed cross-community edges by neighbourhood dissimilarity.

    The score is 1 - Jaccard overlap of the endpoints' other neighbours. It is a structural discovery
    score, not a probability or evidence of causality.
    """
    graph = document_graph(conn, relation_filter)
    nodes = dict(graph.nodes(data=True))
    allowed = _allowed(relation_filter)
    results = []
    for row in _records(conn, 'SELECT * FROM edges ORDER BY source_id,target_id,relation'):
        source, target = row['source_id'], row['target_id']
        if row['relation'].casefold() not in allowed or source not in nodes or target not in nodes:
            continue
        a, b = nodes[source], nodes[target]
        if (a['is_god_node'] or b['is_god_node'] or a['community_id'] is None or b['community_id'] is None
                or a['community_id'] == b['community_id']):
            continue
        left = {n for n in graph.neighbors(source) if n != target and not nodes[n]['is_god_node']}
        right = {n for n in graph.neighbors(target) if n != source and not nodes[n]['is_god_node']}
        union = left | right
        score = 1 - len(left & right) / len(union) if union else 1.0
        results.append({**row, 'score': round(score, 6), 'source_community': a['community_id'],
                        'target_community': b['community_id'],
                        'reason': 'explicit cross-community connection'})
    return sorted(results, key=lambda item: (-item['score'], item['source_id'], item['target_id'],
                                             item['relation']))


def implementation() -> str:
    """The analysis build: this module's version and the networkx release that ran Louvain."""
    return f'{ANALYSIS_VERSION}/networkx-{nx.__version__}'


def assignment_digest(conn: sqlite3.Connection) -> str:
    """SHA-256 of the published assignment: every document's group, hub flag and surprise score."""
    digest = hashlib.sha256(b'kelmo-graph-assignment-v1\0')
    for row in conn.execute('SELECT id, community_id, is_god_node, surprise_score FROM entities '
                            'WHERE file_path IS NOT NULL ORDER BY id'):
        digest.update(json.dumps(list(row), ensure_ascii=False).encode('utf-8') + b'\n')
    return digest.hexdigest()


def analysis_stats(conn: sqlite3.Connection) -> dict:
    """Counts of the published suggestions; read-only."""
    communities, hubs, surprising = conn.execute(
        'SELECT COUNT(DISTINCT community_id), COALESCE(SUM(is_god_node=1),0), '
        'COALESCE(SUM(surprise_score>0),0) FROM entities WHERE file_path IS NOT NULL').fetchone()
    return {**LABEL, 'communities': communities, 'god_nodes': hubs, 'surprising_documents': surprising}
