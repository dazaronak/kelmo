"""Bounded, deterministic navigation over explicit wiki structure."""

import json
import sqlite3
import unicodedata
from collections import deque
from pathlib import PurePosixPath

from kelmo.store.graph.analysis import ALGORITHM, document_graph, hub_threshold, surprising_edges
from kelmo.store.graph.parser import lookup_keys, slugify, target_key


NAVIGATION = ('search', 'neighbors', 'impact', 'path', 'details')
ANALYTICS = ('communities', 'god_nodes', 'surprises')
# community_id, surprise_score and is_god_node are the legacy derived-topology fields every node carried.
NODE_FIELDS = ('id', 'name', 'type', 'file_path', 'line_number', 'status', 'aliases', 'in_degree', 'out_degree',
               'community_id', 'surprise_score', 'is_god_node')
EDGE_FIELDS = ('source_id', 'target_id', 'relation', 'confidence', 'weight', 'source_file', 'source_line', 'created_at')


def _records(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    fields = [column[0] for column in cursor.description]
    return [dict(zip(fields, row)) for row in cursor]


def _aliases(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _nodes(conn):
    # Legacy hand-seeded archives may not yet have the compatible additions.
    result = {}
    for row in _records(conn, 'SELECT * FROM entities ORDER BY id'):
        node = {field: row.get(field) for field in NODE_FIELDS}
        node['aliases'] = _aliases(row.get('aliases', []))
        node['in_degree'] = row.get('in_degree') or 0
        node['out_degree'] = row.get('out_degree') or 0
        result[node['id']] = node
    return result


def _resolve_value(nodes, value):
    raw = unicodedata.normalize('NFC', value or '')
    if raw in nodes:
        return raw, None
    key = target_key(raw)
    if key in nodes:
        return key, None
    if not key and (raw.strip().startswith('/') or '..' in PurePosixPath(raw.strip()).parts):
        return None, None
    keys = {slugify(raw.strip()), slugify(key)} - {''}
    # Shorthand and aliases name pages only, as when the graph was built: tag and unresolved
    # placeholders are reachable by their exact IDs above, never by a suffix or slug.
    matches = sorted(
        entity_id for entity_id, node in nodes.items()
        if node['file_path'] is not None and keys.intersection(lookup_keys(entity_id, node['aliases']))
    )
    if len(matches) > 1:
        return None, {'error': 'ambiguous_entity', 'entity_id': value, 'candidates': matches}
    return (matches[0] if matches else None), None


def resolve_entity(conn, value):
    """Resolve an exact ID or unique convenience key for page reads."""
    return _resolve_value(_nodes(conn), value)


def _edges(conn, relation_filter):
    allowed = {relation.casefold() for relation in relation_filter} if relation_filter is not None else None
    return [
        {field: row.get(field) for field in EDGE_FIELDS}
        for row in _records(conn, 'SELECT * FROM edges ORDER BY source_id,target_id,relation')
        if allowed is None or row['relation'].casefold() in allowed
    ]


def graph_query(
    conn: sqlite3.Connection,
    *,
    action: str,
    entity_id: str | None = None,
    query: str | None = None,
    target_id: str | None = None,
    radius: int = 1,
    relation_filter: list[str] | None = None,
    limit: int = 50,
    community_id: int | None = None,
    exclude_god_nodes: bool = False,
    min_surprise: float = 0.0,
) -> list[dict] | dict:
    """Return ordinary lists or an explicit ambiguity/path-ceiling error.

    communities, god_nodes and surprises read the derived topology published with the snapshot; their
    rows are structural suggestions, never verified factual links."""
    limit = max(1, min(limit, 200))
    if action not in NAVIGATION + ANALYTICS:
        return []
    nodes = _nodes(conn)
    if action == 'communities':
        return _communities(conn, nodes, community_id, limit)
    if action == 'god_nodes':
        return _god_nodes(conn, nodes, limit)
    if action == 'surprises':
        return [
            {**{field: item.get(field) for field in EDGE_FIELDS},
             **{key: item[key] for key in ('score', 'source_community', 'target_community', 'reason')}}
            for item in surprising_edges(conn, relation_filter)
            if item['score'] >= min_surprise
            and (community_id is None or community_id in (item['source_community'], item['target_community']))
        ][:limit]
    if action == 'search':
        return _search(conn, nodes, query or '', limit)
    source, error = _resolve_value(nodes, entity_id)
    if error:
        return error
    if source is None:
        return []
    hubs = _hubs(conn) if exclude_god_nodes else set()
    edges = [edge for edge in _edges(conn, relation_filter)
             if edge['source_id'] in nodes and edge['target_id'] in nodes
             and edge['source_id'] not in hubs and edge['target_id'] not in hubs]
    if action == 'details':
        return [
            {**edge, 'direction': 'outgoing' if edge['source_id'] == source else 'incoming'}
            for edge in edges if source in (edge['source_id'], edge['target_id'])
        ][:limit]
    if action == 'impact':
        ids = sorted({edge['source_id'] for edge in edges if edge['target_id'] == source})
        return [nodes[key] for key in ids[:limit]]
    adjacency = {key: set() for key in nodes}
    for edge in edges:
        adjacency[edge['source_id']].add(edge['target_id'])
        adjacency[edge['target_id']].add(edge['source_id'])
    if action == 'path':
        target, error = _resolve_value(nodes, target_id)
        if error:
            return error
        if target is None:
            return []
        return _path(nodes, adjacency, source, target, limit)
    radius = max(1, min(radius, 2))
    queue, visited, result = deque([(source, 0)]), {source}, []
    while queue and len(result) < limit:
        current, depth = queue.popleft()
        if depth >= radius:
            continue
        for neighbor in sorted(adjacency[current]):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
                result.append({**nodes[neighbor], 'depth': depth + 1})
                if len(result) == limit:
                    break
    return result


def _hubs(conn):
    return {row['id'] for row in _records(conn, 'SELECT id FROM entities WHERE is_god_node=1')}


def _communities(conn, nodes, community_id, limit):
    groups = {}
    for row in _records(conn, 'SELECT id,community_id FROM entities '
                              'WHERE community_id IS NOT NULL AND file_path IS NOT NULL ORDER BY id'):
        if community_id is None or row['community_id'] == community_id:
            groups.setdefault(row['community_id'], []).append(nodes[row['id']])
    return [
        {'community_id': group, 'size': len(members), 'members': members[:limit],
         'members_truncated': len(members) > limit, 'algorithm': ALGORITHM}
        for group, members in sorted(groups.items())
    ][:limit]


def _god_nodes(conn, nodes, limit):
    graph = document_graph(conn)
    threshold = hub_threshold(graph)
    hubs = _hubs(conn)
    ranked = sorted((node for node in graph if node in hubs), key=lambda node: (-graph.degree(node), node))
    return [
        {**nodes[node], 'degree': graph.degree(node), 'hub_degree_threshold': threshold,
         'reason': f'document degree exceeds hub threshold {threshold}'}
        for node in ranked
    ][:limit]


def _search(conn, nodes, query, limit):
    if not query.strip():
        return list(nodes.values())[:limit]
    resolved, error = _resolve_value(nodes, query)
    if error:
        return error
    result = [nodes[resolved]] if resolved is not None else []
    normalized = query.replace('-', ' ').replace('_', ' ')
    # Quoting makes all user punctuation literal FTS text, not FTS operators.
    expression = '"' + normalized.replace('"', '""') + '"'
    rows = _records(
        conn,
        'SELECT e.id FROM entities e JOIN entities_fts fts ON e.rowid=fts.rowid '
        'WHERE entities_fts MATCH ? ORDER BY e.id LIMIT ?',
        (expression, limit),
    )
    seen = {node['id'] for node in result}
    for row in rows:
        if row['id'] in nodes and row['id'] not in seen:
            seen.add(row['id'])
            result.append(nodes[row['id']])
    if not result:
        needle = unicodedata.normalize('NFKC', normalized).casefold()
        for node in nodes.values():
            values = (node['id'], node['name'], *node['aliases'])
            if any(needle in unicodedata.normalize('NFKC', value.replace('-', ' ').replace('_', ' ')).casefold()
                   for value in values):
                result.append(node)
                if len(result) == limit:
                    break
    return result[:limit]


def _path(nodes, adjacency, source, target, limit):
    queue, parent, depth = deque([source]), {source: None}, {source: 0}
    while queue:
        current = queue.popleft()
        if current == target:
            path = []
            while current is not None:
                path.append({**nodes[current], 'depth': depth[current]})
                current = parent[current]
            path.reverse()
            if len(path) > limit:
                return {
                    'error': 'path_limit_exceeded', 'source_id': source, 'target_id': target,
                    'limit': limit, 'required_nodes': len(path),
                }
            return path
        for neighbor in sorted(adjacency[current]):
            if neighbor not in parent:
                parent[neighbor], depth[neighbor] = current, depth[current] + 1
                queue.append(neighbor)
    return []
