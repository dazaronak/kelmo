"""Synthetic scanner, publisher and navigation acceptance tests."""

import json
import os
import sqlite3

import pytest

from kelmo.store.graph import build
from kelmo.store.graph.build import ensure_schema, publish_graph, rebuild_graph, scan_wiki
from kelmo.store.graph.parser import extract_wikilinks, parse_file, parse_text
from kelmo.store.graph.query import graph_query
from kelmo.store.graph.stats import graph_stats


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))
    return path


def _ids(conn):
    return [node['id'] for node in graph_query(conn, action='search', query='', limit=200)]


def test_parser_compatibility_code_exclusion_and_source_lines(tmp_path):
    raw = (
        '---\r\ntitle: Child title\r\naliases: [Kid, "小孩+"]\r\n'
        'tags: ["C++", "C#"]\r\nstatus: draft\r\nparent: "../Root.md"\r\n---\r\n'
        '# Heading\r\n`[[Inline]]` ![[Embed]] \\[[Escaped]]\r\n\r\n'
        '```md\r\n[[Fence]]\r\n```\r\n\r\n'
        '    [[Indented]]\r\n\r\n'
        '- **[[Root#Section|label]]**\r\n'
    )
    path = _write(tmp_path, 'topics/Child.md', raw)
    _write(tmp_path, 'Root.md', '# Root\n')
    parsed = parse_text(raw.encode('utf-8'))
    assert parsed == parse_file(path)
    assert parsed['error'] is None
    assert parsed['wikilinks'] == ['Root']
    assert parsed['parent'] == '../Root'
    assert parsed['parent_relative'] is True
    assert parsed['aliases'] == ['Kid', '小孩+']
    assert parsed['tags'] == ['C++', 'C#']
    assert parsed['status'] == 'draft'
    assert parsed['line_number'] == 8
    assert extract_wikilinks('`[[No]]` ![[No]] [[Yes]]') == ['Yes']
    snapshot = scan_wiki(tmp_path)
    assert snapshot['errors'] == []
    assert snapshot['documents']['topics/Child']['content'] == raw
    edges = {(edge['target_id'], edge['relation']): edge for edge in snapshot['edges']}
    assert ('Root', 'child_of') in edges
    assert ('Root', 'links_to') in edges
    assert ('tag:C++', 'tagged_with') in edges
    assert ('tag:C#', 'tagged_with') in edges
    for edge in edges.values():
        assert edge['source_file'] == 'topics/Child.md'
        line = raw.splitlines()[edge['source_line'] - 1]
        marker = {'child_of': 'parent:', 'tagged_with': 'tags:', 'links_to': '[[Root'}[edge['relation']]
        assert marker in line
    assert parse_text(b'\xff')['error'] == 'Invalid UTF-8'
    assert parse_file(tmp_path / 'missing.md')['error']


def test_path_identities_unicode_punctuation_and_alias_collisions(tmp_db, tmp_path):
    conn, _ = tmp_db
    _write(tmp_path, 'a/Topic.md', '---\naliases: Shared\n---\n# Topic A\n')
    _write(tmp_path, 'b/Topic.md', '---\naliases: shared\n---\n# Topic B\n')
    _write(tmp_path, 'Résumé+.md', '# Plus\nPRIVATE BODY CONTENT\n')
    _write(tmp_path, 'Résumé!.md', '# Bang\n')
    _write(tmp_path, '研究/工具 & API.md', '[[a/Topic]] [[Shared]]\n')
    _write(tmp_path, '_template.md', '# Included template\n')
    snapshot = scan_wiki(tmp_path)
    assert snapshot['errors'] == []
    assert {'a/Topic', 'b/Topic', 'Résumé+', 'Résumé!', '研究/工具 & API', '_template'} <= snapshot['documents'].keys()
    assert snapshot['report']['ambiguous_links'] == 1
    ambiguous_edge = next(edge for edge in snapshot['edges'] if edge['confidence'] == 'AMBIGUOUS')
    assert ambiguous_edge['target_id'] == 'unresolved:Shared'
    assert all('content' not in node for node in snapshot['nodes'].values())
    publish_graph(conn, snapshot)
    for action, value in [('neighbors', 'Topic'), ('details', 'SHARED'), ('search', 'résumé')]:
        kwargs = {'query': value} if action == 'search' else {'entity_id': value}
        result = graph_query(conn, action=action, **kwargs)
        assert result['error'] == 'ambiguous_entity'
        assert len(result['candidates']) == 2
        assert result['candidates'] == sorted(result['candidates'])
    assert graph_query(conn, action='search', query='Résumé+')[0]['id'] == 'Résumé+'
    assert graph_query(conn, action='search', query='研究/工具 & API')[0]['file_path'] == '研究/工具 & API.md'
    for action in ('search', 'neighbors', 'impact', 'path', 'details'):
        result = graph_query(conn, action=action, entity_id='Résumé+', target_id='Résumé+', query='')
        assert 'PRIVATE BODY CONTENT' not in json.dumps(result)


def test_explicit_root_paths_win_over_aliases_and_local_fallback(tmp_db, tmp_path):
    conn, _ = tmp_db
    _write(tmp_path, 'Shared.md', '# Root\n')
    _write(tmp_path, 'wiki/Page.md', '# Literal wiki folder\n')
    _write(tmp_path, 'nested/Shared.md', '---\naliases: Shared\n---\n# Nested\n')
    _write(tmp_path, 'nested/Other.md', '---\naliases: Shared\n---\n# Other\n')
    _write(tmp_path, 'nested/Use.md', '[[Shared]] [[wiki/Page]]\n')
    snapshot = scan_wiki(tmp_path)
    targets = {edge['target_id'] for edge in snapshot['edges'] if edge['source_id'] == 'nested/Use'}
    assert targets == {'Shared', 'wiki/Page'}
    publish_graph(conn, snapshot)
    assert [node['id'] for node in graph_query(conn, action='impact', entity_id='Shared')] == ['nested/Use']
    assert graph_query(conn, action='search', query='nested/Shared')[0]['id'] == 'nested/Shared'


def test_ambiguous_short_links_require_an_explicit_relative_path(tmp_path):
    _write(tmp_path, 'a/Topic.md', '# A\n')
    _write(tmp_path, 'b/Topic.md', '# B\n')
    # A plain link from a third folder is ambiguous; from a/ it would resolve to its own folder first.
    _write(tmp_path, 'c/Use.md', '[[Topic]]\n')
    _write(tmp_path, 'a/Use.md', '[[./Topic]]\n')
    snapshot = scan_wiki(tmp_path)
    edges = {edge['target_id']: edge for edge in snapshot['edges']}
    assert edges['unresolved:Topic']['confidence'] == 'AMBIGUOUS'
    assert edges['a/Topic']['confidence'] == 'EXTRACTED'


def test_search_database_failure_is_not_reported_as_no_matches(tmp_db):
    conn, _ = tmp_db
    conn.execute('DROP TABLE entities_fts')
    with pytest.raises(sqlite3.OperationalError):
        graph_query(conn, action='search', query='anything')


def test_nfc_normalization_collisions_are_not_merged(tmp_path):
    _write(tmp_path, 'é.md', '# First\n')
    _write(tmp_path, 'e\u0301.md', '# Second\n')
    if len(list(tmp_path.glob('*.md'))) != 2:
        pytest.skip('Filesystem itself coalesces Unicode normalization variants')
    snapshot = scan_wiki(tmp_path)
    assert any(error['reason'] == 'duplicate_id' for error in snapshot['errors'])
    assert snapshot['source_digest'] is None


@pytest.mark.parametrize('bad', [
    '---\ntags: 3\n---\n',
    '---\naliases: [ok, 7]\n---\n',
    '---\ntitle: 12\n---\n',
    '---\nparent: {page: Other}\n---\n',
    '---\nstatus: [draft]\n---\n',
    '---\n[not, a, mapping]\n---\n',
    '---\ntags: [first]\ntags: [second]\n---\n',
    '---\ntitle: unfinished\n',
])
def test_malformed_metadata_retains_previous_graph(tmp_db, tmp_path, bad):
    conn, _ = tmp_db
    path = _write(tmp_path, 'Old.md', '# Old\n')
    assert rebuild_graph(conn, tmp_path)['status'] == 'ok'
    before = graph_stats(conn)
    path.write_bytes(bad.encode())
    result = rebuild_graph(conn, tmp_path)
    assert result['status'] == 'error'
    assert result['errors'] == [{'file': 'Old.md', 'reason': 'parse_error'}]
    assert graph_stats(conn) == before
    assert _ids(conn) == ['Old']
    with pytest.raises(ValueError, match='error snapshot'):
        publish_graph(conn, scan_wiki(tmp_path))


@pytest.mark.parametrize('link', ['../../Outside', '/outside.md'])
def test_escaping_links_fail_but_internal_parent_traversal_works(tmp_db, tmp_path, link):
    conn, _ = tmp_db
    _write(tmp_path, 'Root.md', '# Root\n')
    child = _write(tmp_path, 'nested/Child.md', '[[../Root.md]]\n')
    assert rebuild_graph(conn, tmp_path)['status'] == 'ok'
    assert [node['id'] for node in graph_query(conn, action='neighbors', entity_id='nested/Child')] == ['Root']
    child.write_text(f'[[{link}]]\n', encoding='utf-8')
    result = rebuild_graph(conn, tmp_path)
    assert result['status'] == 'error'
    assert {'file': 'nested/Child.md', 'reason': 'link_outside_wiki'} in result['errors']
    assert _ids(conn) == ['Root', 'nested/Child']


def test_symlinks_and_reserved_ids_fail_closed(tmp_db, tmp_path):
    conn, _ = tmp_db
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    _write(wiki, 'Old.md', '# Old\n')
    rebuild_graph(conn, wiki)
    outside = _write(tmp_path, 'Outside.md', 'OUTSIDE SECRET\n')
    try:
        (wiki / 'Link.md').symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable')
    snapshot = scan_wiki(wiki)
    assert {'file': 'Link.md', 'reason': 'unsafe_path'} in snapshot['errors']
    assert 'OUTSIDE SECRET' not in json.dumps(snapshot)
    assert rebuild_graph(conn, wiki)['status'] == 'error'
    assert _ids(conn) == ['Old']
    (wiki / 'Link.md').unlink()
    _write(wiki, 'tag:reserved.md', '# Bad\n')
    assert {'file': 'tag:reserved.md', 'reason': 'reserved_id'} in scan_wiki(wiki)['errors']


@pytest.mark.parametrize('change', ['content', 'file_list'])
def test_changes_during_parsing_reject_snapshot(tmp_db, tmp_path, monkeypatch, change):
    conn, _ = tmp_db
    path = _write(tmp_path, 'Old.md', '# Old\n')
    rebuild_graph(conn, tmp_path)
    original = build.parse_text
    changed = False

    def racing_parse(raw):
        nonlocal changed
        parsed = original(raw)
        if not changed:
            changed = True
            if change == 'content':
                path.write_bytes(b'# New\n')
            else:
                _write(tmp_path, 'New.md', '# New\n')
        return parsed

    monkeypatch.setattr(build, 'parse_text', racing_parse)
    result = rebuild_graph(conn, tmp_path)
    assert result['status'] == 'error'
    assert any(error['reason'] == 'scan_changed' for error in result['errors'])
    assert _ids(conn) == ['Old']
    assert all(not error['file'].startswith('/') for error in result['errors'])


def test_read_failure_has_safe_diagnostics_and_retains_index(tmp_db, tmp_path, monkeypatch):
    conn, _ = tmp_db
    _write(tmp_path, 'Old.md', '# Old\n')
    rebuild_graph(conn, tmp_path)
    original = build.os.open

    def unreadable(path, *args, **kwargs):
        if path == 'Old.md':
            raise PermissionError(f'private absolute path: {tmp_path}')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(build.os, 'open', unreadable)
    result = rebuild_graph(conn, tmp_path)
    assert result['status'] == 'error'
    assert result['errors'] == [{'file': 'Old.md', 'reason': 'read_error'}]
    assert str(tmp_path) not in json.dumps(result)
    assert _ids(conn) == ['Old']


def test_digest_names_bytes_and_paths_not_timestamps(tmp_path):
    path = _write(tmp_path, 'A.md', '[[B]]\n')
    _write(tmp_path, 'B.md', '# B\n')
    _write(tmp_path, 'C.md', '# C\n')
    first = scan_wiki(tmp_path)
    assert first['source_digest'] == scan_wiki(tmp_path)['source_digest']
    info = path.stat()
    path.write_bytes(b'[[C]]\n')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    edited = scan_wiki(tmp_path)
    assert edited['errors'] == []
    assert edited['source_digest'] != first['source_digest']
    assert edited['documents']['A']['content'] == '[[C]]\n'
    assert edited['edges'][0]['target_id'] == 'C'
    path.write_bytes(b'[[B]]\n')
    assert scan_wiki(tmp_path)['source_digest'] == first['source_digest']
    renamed = tmp_path / 'Renamed.md'
    path.rename(renamed)
    assert scan_wiki(tmp_path)['source_digest'] != first['source_digest']
    renamed.rename(path)
    assert scan_wiki(tmp_path)['source_digest'] == first['source_digest']


def test_delete_empty_and_dry_run_rebuilds(tmp_db, tmp_path):
    conn, _ = tmp_db
    path = _write(tmp_path, 'Old.md', '---\ntags: old\n---\n[[Missing]]\n')
    first = rebuild_graph(conn, tmp_path)
    assert first['entities'] == 3
    assert first['edges'] == 2
    path.unlink()
    dry = rebuild_graph(conn, tmp_path, dry_run=True)
    assert dry['status'] == 'dry_run' and dry['files'] == 0
    assert len(_ids(conn)) == 3
    empty = rebuild_graph(conn, tmp_path)
    assert empty['status'] == 'ok'
    assert empty['entities'] == empty['edges'] == empty['files'] == 0
    assert empty['source_digest'] == scan_wiki(tmp_path)['source_digest']
    assert _ids(conn) == []
    stats = graph_stats(conn)
    assert stats['by_type'] == stats['by_relation'] == {}
    assert stats['documents'] == stats['unresolved_targets'] == 0
    assert 'graph_status' not in stats and 'built_at' not in stats
    assert rebuild_graph(conn, tmp_path / 'missing')['status'] == 'error'


def test_implicit_root_is_rejected_without_changing_the_index(tmp_db, tmp_path):
    conn, _ = tmp_db
    _write(tmp_path, 'Selected.md', '# Selected\n')
    rebuild_graph(conn, tmp_path)
    result = rebuild_graph(conn)
    assert result['status'] == 'error'
    assert _ids(conn) == ['Selected']


def test_compatible_schema_and_caller_savepoint_are_preserved(tmp_db, tmp_path):
    conn, _ = tmp_db
    before_meta = dict(conn.execute('SELECT key,value FROM archive_meta'))
    before_memory_columns = list(conn.execute('PRAGMA table_info(memories)'))
    ensure_schema(conn)
    ensure_schema(conn)
    assert {'status', 'aliases'} <= {row[1] for row in conn.execute('PRAGMA table_info(entities)')}
    assert {'source_file', 'source_line'} <= {row[1] for row in conn.execute('PRAGMA table_info(edges)')}
    assert dict(conn.execute('SELECT key,value FROM archive_meta')) == before_meta
    assert list(conn.execute('PRAGMA table_info(memories)')) == before_memory_columns
    old = _write(tmp_path, 'Old.md', '# Old\n')
    rebuild_graph(conn, tmp_path)
    old.unlink()
    _write(tmp_path, 'New.md', '# New\n')
    conn.execute('SAVEPOINT caller')
    publish_graph(conn, scan_wiki(tmp_path))
    assert conn.in_transaction
    assert _ids(conn) == ['New']
    conn.execute('ROLLBACK TO caller')
    conn.execute('RELEASE caller')
    assert _ids(conn) == ['Old']


def test_sql_failure_rolls_back_rows_and_fts(tmp_db, tmp_path):
    conn, _ = tmp_db
    old = _write(tmp_path, 'Old.md', '# Old searchable\n')
    rebuild_graph(conn, tmp_path)
    old.unlink()
    _write(tmp_path, 'Bad.md', '# Bad searchable\n')
    conn.execute(
        "CREATE TEMP TRIGGER reject_graph BEFORE INSERT ON entities WHEN new.id='Bad' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic rejection'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        publish_graph(conn, scan_wiki(tmp_path))
    assert _ids(conn) == ['Old']
    rows = conn.execute(
        'SELECT e.id FROM entities e JOIN entities_fts f ON e.rowid=f.rowid WHERE entities_fts MATCH ?',
        ('"Old"',),
    ).fetchall()
    assert [row[0] for row in rows] == ['Old']
    assert graph_query(conn, action='search', query='Bad') == []


def test_relation_filters_direct_impact_and_result_limits(tmp_db, tmp_path):
    conn, _ = tmp_db
    _write(tmp_path, 'A.md', '[[B]]\n')
    _write(tmp_path, 'B.md', '---\nparent: C\n---\n# B\n')
    _write(tmp_path, 'C.md', '[[D]]\n')
    _write(tmp_path, 'D.md', '# D\n')
    rebuild_graph(conn, tmp_path)
    conn.execute("UPDATE edges SET relation='LINKS_TO' WHERE relation='links_to'")
    links = graph_query(conn, action='neighbors', entity_id='A', radius=99, relation_filter=['links_to'])
    assert [node['id'] for node in links] == ['B']
    assert [node['id'] for node in graph_query(conn, action='neighbors', entity_id='A', radius=99)] == ['B', 'C']
    assert len(graph_query(conn, action='neighbors', entity_id='C', limit=1)) == 1
    assert [node['id'] for node in graph_query(conn, action='impact', entity_id='C')] == ['B']
    assert graph_query(conn, action='impact', entity_id='C', relation_filter=['links_to']) == []
    details = graph_query(conn, action='details', entity_id='C', relation_filter=['CHILD_OF'], limit=1)
    assert details[0]['source_file'] == 'B.md' and details[0]['direction'] == 'incoming'
    assert graph_query(conn, action='path', entity_id='A', target_id='D', relation_filter=['LINKS_TO']) == []
    ceiling = graph_query(conn, action='path', entity_id='A', target_id='D', limit=2)
    assert ceiling == {
        'error': 'path_limit_exceeded', 'source_id': 'A', 'target_id': 'D',
        'limit': 2, 'required_nodes': 4,
    }
    assert graph_query(conn, action='neighbors', entity_id='A', relation_filter=[]) == []
    assert graph_query(conn, action='bogus') == []


def test_cyclic_paths_are_shortest_deterministic_and_not_false_disconnected(tmp_db, tmp_path):
    conn, _ = tmp_db
    for name, text in {'A': '[[C]] [[B]]', 'B': '[[D]]', 'C': '[[D]]', 'D': '[[B]]', 'X': '# Isolated'}.items():
        _write(tmp_path, f'{name}.md', text + '\n')
    rebuild_graph(conn, tmp_path)
    first = graph_query(conn, action='path', entity_id='A', target_id='D')
    assert [node['id'] for node in first] == ['A', 'B', 'D']
    assert first == graph_query(conn, action='path', entity_id='A', target_id='D')
    neighbors = graph_query(conn, action='neighbors', entity_id='A', radius=99)
    assert [(node['id'], node['depth']) for node in neighbors] == [('B', 1), ('C', 1), ('D', 2)]
    assert graph_query(conn, action='path', entity_id='A', target_id='X', limit=1) == []
    assert graph_query(conn, action='path', entity_id='A', target_id='A', limit=0)[0]['id'] == 'A'
    assert len(graph_query(conn, action='neighbors', entity_id='A', radius=0, limit=0)) == 1


def test_legacy_seed_limit_clamping_and_literal_search(tmp_db):
    conn, _ = tmp_db
    now = '2026-10-05T00:00:00Z'
    conn.executemany(
        'INSERT INTO entities(id,name,type,created_at,updated_at) VALUES(?,?,?,?,?)',
        [(f'n{index:03}', f'Node {index}', 'CONCEPT', now, now) for index in range(205)],
    )
    conn.execute(
        'INSERT INTO edges(source_id,target_id,relation,created_at) VALUES(?,?,?,?)',
        ('n000', 'n001', 'LINKS_TO', now),
    )
    assert len(graph_query(conn, action='search', limit=999)) == 200
    assert len(graph_query(conn, action='search', limit=0)) == 1
    assert graph_query(conn, action='neighbors', entity_id='n000', relation_filter=['links_to'])[0]['id'] == 'n001'
    for query in ['"quoted"', "it's", '(parens)', 'NOT this', 'a*b', '%_']:
        assert isinstance(graph_query(conn, action='search', query=query), list)
