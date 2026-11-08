"""Tests for graph_query and graph_stats."""
from kelmo.store.graph.query import graph_query
from kelmo.store.graph.stats import graph_stats


def _seed_graph(conn):
    """Insert sample entities and edges."""
    now = "2026-04-14T00:00:00Z"
    entities = [
        ("demo", "demo", "PROJECT", now, now),
        ("memory", "memory", "CONCEPT", now, now),
        ("hooks", "hooks", "CONCEPT", now, now),
        ("archive", "archive", "CONCEPT", now, now),
        ("wiki-compiler", "wiki-compiler", "CONCEPT", now, now),
    ]
    for e in entities:
        conn.execute(
            "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", e
        )
    edges = [
        ("demo", "memory", "LINKS_TO", "EXTRACTED", 1.0, now),
        ("demo", "hooks", "LINKS_TO", "EXTRACTED", 1.0, now),
        ("memory", "archive", "LINKS_TO", "EXTRACTED", 1.0, now),
        ("hooks", "wiki-compiler", "LINKS_TO", "EXTRACTED", 1.0, now),
    ]
    for e in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, confidence, weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", e
        )
    conn.commit()


def test_search_finds_entity(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="search", query="memory")
    assert any(r["id"] == "memory" for r in results)


def test_neighbors_returns_connected(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="neighbors", entity_id="demo")
    ids = {r["id"] for r in results}
    assert "memory" in ids
    assert "hooks" in ids


def test_impact_returns_inbound(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="impact", entity_id="memory")
    ids = {r["id"] for r in results}
    assert "demo" in ids


def test_path_finds_route(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="path", entity_id="demo", target_id="archive")
    ids = [r["id"] for r in results]
    assert "demo" in ids
    assert "archive" in ids


def test_search_empty_query_returns_all(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="search", query="")
    assert len(results) == 5


def test_neighbors_nonexistent_returns_empty(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    results = graph_query(conn, action="neighbors", entity_id="nonexistent")
    assert results == []


def test_stats_counts(tmp_db):
    conn, _ = tmp_db
    _seed_graph(conn)
    s = graph_stats(conn)
    assert s["entities"] == 5
    assert s["edges"] == 4
    assert "PROJECT" in s["by_type"]
    assert "CONCEPT" in s["by_type"]
