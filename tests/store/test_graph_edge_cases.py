"""Edge case tests for graph queries."""
from kelmo.store.graph.query import graph_query
from kelmo.store.graph.stats import graph_stats


def _seed(conn):
    now = "2026-04-14T00:00:00Z"
    conn.execute(
        "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("alpha", "alpha", "CONCEPT", now, now),
    )
    conn.execute(
        "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("beta", "beta", "CONCEPT", now, now),
    )
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        ("alpha", "beta", "LINKS_TO", now),
    )
    conn.commit()


def test_unknown_action_returns_empty(tmp_db):
    conn, _ = tmp_db
    results = graph_query(conn, action="bogus")
    assert results == []


def test_path_no_route_returns_empty(tmp_db):
    conn, _ = tmp_db
    _seed(conn)
    results = graph_query(conn, action="path", entity_id="alpha", target_id="nonexistent")
    assert results == []


def test_radius_clamped_to_2(tmp_db):
    conn, _ = tmp_db
    _seed(conn)
    results = graph_query(conn, action="neighbors", entity_id="alpha", radius=99)
    # Should not crash, radius clamped to 2
    assert isinstance(results, list)


def test_limit_clamped_to_200(tmp_db):
    conn, _ = tmp_db
    _seed(conn)
    results = graph_query(conn, action="search", query="", limit=999)
    assert isinstance(results, list)


def test_stats_empty_graph(tmp_db):
    conn, _ = tmp_db
    s = graph_stats(conn)
    assert s["entities"] == 0
    assert s["edges"] == 0
    assert s["by_type"] == {}


def test_search_special_chars_dont_crash(tmp_db):
    conn, _ = tmp_db
    _seed(conn)
    for q in ['"quoted"', "it's", "(parens)", "NOT this", "a*b"]:
        results = graph_query(conn, action="search", query=q)
        assert isinstance(results, list)
