"""Tests for memory_search: semantic, hybrid, RRF direction, filters."""
from kelmo.store.memory.store import memory_store
from kelmo.store.memory.search import memory_search


# --- Semantic ---

def test_semantic_returns_results(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="Python is great for data science", kind="decision", domain="dev")
    memory_store(conn, content="Rust is fast for systems", kind="decision", domain="dev")
    results = memory_search(conn, query="Python programming", mode="semantic")
    assert len(results) >= 1
    assert all("id" in r for r in results)


def test_semantic_respects_limit(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    for i in range(20):
        memory_store(conn, content=f"memory number {i}", kind="event", domain="dev")
    results = memory_search(conn, query="memory", mode="semantic", limit=5)
    assert len(results) <= 5


def test_semantic_empty_db_returns_empty(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    results = memory_search(conn, query="anything", mode="semantic")
    assert results == []


# --- Hybrid ---

def test_hybrid_returns_results(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="fastmcp Server configuration guide", kind="quirk", domain="dev")
    memory_store(conn, content="unrelated gardening tips", kind="event", domain="life")
    results = memory_search(conn, query="fastmcp Server", mode="hybrid")
    assert len(results) >= 1


def test_hybrid_fts5_escape_special_chars(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="some content", kind="decision", domain="dev")
    for query in ['"quoted"', "it's", "(parens)", "a*b", "a+b", "NEAR/2", "NOT this",
                  "a-b", "emoji test"]:
        results = memory_search(conn, query=query, mode="hybrid")
        assert isinstance(results, list)


# --- RRF direction ---

def test_rrf_direction_bm25_winner_in_top(tmp_db, mock_embed, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    conn, _ = tmp_db
    memory_store(conn, content="PRAGMA busy_timeout configuration details", kind="quirk", domain="dev")
    for i in range(10):
        memory_store(conn, content=f"unrelated content about topic {i} with various words",
                     kind="event", domain="dev")
    results = memory_search(conn, query="PRAGMA busy_timeout", mode="hybrid", limit=5)
    assert len(results) >= 1
    contents = [r["content"] for r in results]
    assert any("PRAGMA busy_timeout" in c for c in contents)


# --- Filters ---

def _seed(conn, monkeypatch):
    monkeypatch.setenv("KELMO_MACHINE_TAG", "test-mac")
    memory_store(conn, content="arch decision about DB", kind="decision", domain="dev",
                 project="demo", tags=["sqlite", "db"])
    memory_store(conn, content="pain with testing", kind="pain", domain="dev",
                 project="demo-app", tags=["testing"])
    memory_store(conn, content="family event birthday", kind="event", domain="life",
                 project="person", tags=["family"])


def test_filter_by_kind(tmp_db, mock_embed, monkeypatch):
    conn, _ = tmp_db
    _seed(conn, monkeypatch)
    results = memory_search(conn, query="test", mode="semantic", kind="decision")
    assert all(r["kind"] == "decision" for r in results)


def test_filter_by_domain(tmp_db, mock_embed, monkeypatch):
    conn, _ = tmp_db
    _seed(conn, monkeypatch)
    results = memory_search(conn, query="test", mode="semantic", domain="life")
    assert all(r["domain"] == "life" for r in results)


def test_filter_by_project(tmp_db, mock_embed, monkeypatch):
    conn, _ = tmp_db
    _seed(conn, monkeypatch)
    results = memory_search(conn, query="test", mode="semantic", project="demo")
    assert all(r["project"] == "demo" for r in results)
