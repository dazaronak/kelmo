"""Tests for fts5_escape — the FTS5 query sanitizer."""
from kelmo.store.memory.search import fts5_escape


def test_normal_query():
    assert fts5_escape("hello world") == '"hello" "world"'


def test_empty_string():
    assert fts5_escape("") == '""'


def test_whitespace_only():
    assert fts5_escape("   ") == '""'


def test_double_quotes_escaped():
    result = fts5_escape('say "hello"')
    assert '""hello""' in result


def test_single_quotes():
    result = fts5_escape("it's fine")
    assert '"it\'s"' in result


def test_parentheses():
    result = fts5_escape("(test)")
    assert '"(test)"' in result


def test_asterisk():
    result = fts5_escape("test*")
    assert '"test*"' in result


def test_plus():
    result = fts5_escape("a+b")
    assert '"a+b"' in result


def test_minus():
    result = fts5_escape("-exclude")
    assert '"-exclude"' in result


def test_near():
    result = fts5_escape("NEAR/2 test")
    assert '"NEAR/2"' in result


def test_not_keyword():
    result = fts5_escape("NOT this")
    assert '"NOT"' in result


def test_and_keyword():
    result = fts5_escape("this AND that")
    assert '"AND"' in result


def test_or_keyword():
    result = fts5_escape("this OR that")
    assert '"OR"' in result


def test_cyrillic():
    result = fts5_escape("кириллица тест")
    assert result == '"кириллица" "тест"'


def test_mixed_language():
    result = fts5_escape("sqlite-vec кириллица")
    assert '"sqlite-vec"' in result
    assert '"кириллица"' in result


def test_single_word():
    assert fts5_escape("hello") == '"hello"'


def test_tech_identifiers():
    result = fts5_escape("fastmcp.Server PRAGMA")
    assert '"fastmcp.Server"' in result
    assert '"PRAGMA"' in result
