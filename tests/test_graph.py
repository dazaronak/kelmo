"""Derived topology on the selected-wiki graph: communities, hubs, surprises and the name index."""
import json

import pytest

from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']

CLUSTERS = {
    'a/one': '[[a/two]] [[a/three]] [[b/four]]',
    'a/two': '[[a/one]] [[a/three]]',
    'a/three': '[[a/one]] [[a/two]]',
    'b/four': '[[b/five]] [[b/six]]',
    'b/five': '[[b/four]] [[b/six]]',
    'b/six': '[[b/four]] [[b/five]]',
}


def write(service, pages):
    for page, body in pages.items():
        path = service.config.wiki / (page + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'# {page}\n\n{body}\n', encoding='utf-8')


def groups(result):
    return sorted(sorted(member['id'] for member in group['members']
                         if not member['id'].startswith(('tag:', 'unresolved:')))
                  for group in result['result'])


def test_communities_are_deterministic_suggestions_bound_to_their_snapshot(service):
    write(service, CLUSTERS)
    first = service.graph_query(action='communities')
    expected = [['a/one', 'a/three', 'a/two'], ['b/five', 'b/four', 'b/six']]
    assert [group for group in groups(first) if group[0].startswith(('a/', 'b/'))] == expected
    assert first['analysis']['kind'] == 'derived_topology'
    assert first['analysis']['verified'] is False
    assert first['analysis']['snapshot'] == first['graph']['source_digest']
    assert all(group['algorithm'] == 'louvain' for group in first['result'])
    rebuilt = service.graph_rebuild()
    assert rebuilt['analysis']['verified'] is False
    assert groups(service.graph_query(action='communities')) == groups(first)


def test_surprises_rank_the_explicit_bridge_with_its_source_line(service):
    write(service, CLUSTERS)
    result = service.graph_query(action='surprises')
    bridge = result['result'][0]
    assert (bridge['source_id'], bridge['target_id'], bridge['relation']) == ('a/one', 'b/four', 'links_to')
    assert bridge['score'] == 1.0
    assert bridge['source_file'] == 'a/one.md' and type(bridge['source_line']) is int
    assert bridge['reason'] == 'explicit cross-community connection'
    assert bridge['source_community'] != bridge['target_community']
    assert service.graph_query(action='surprises', min_surprise=1.0)['result'][0]['source_id'] == 'a/one'


def test_hubs_exceed_the_threshold_and_can_be_excluded_from_navigation(service):
    write(service, {'hub': ' '.join(f'[[leaf{index}]]' for index in range(6)),
                    **{f'leaf{index}': 'Plain leaf.' for index in range(6)}})
    hubs = service.graph_query(action='god_nodes')['result']
    assert [(item['id'], item['degree'], item['hub_degree_threshold']) for item in hubs] == [('hub', 6, 4)]
    assert [node['id'] for node in service.graph_query(action='neighbors', entity_id='leaf0')['result']] == ['hub']
    assert service.graph_query(action='neighbors', entity_id='leaf0', exclude_god_nodes=True)['result'] == []


def test_a_named_snapshot_is_refused_once_the_wiki_changed(service):
    write(service, CLUSTERS)
    first = service.graph_query(action='communities')
    digest = first['graph']['source_digest']
    assert service.graph_query(action='surprises', snapshot=digest)['analysis']['snapshot'] == digest
    write(service, {'b/six': '[[b/four]] [[b/five]] [[a/two]]'})
    with pytest.raises(KelmoError) as refused:
        service.graph_query(action='communities', snapshot=digest)
    assert refused.value.code == 'E_GRAPH_STALE'
    current = service.graph_query(action='communities')
    assert current['analysis']['snapshot'] != digest
    assert current['graph']['status'] == 'current'


def test_stats_report_analysis_only_for_a_current_snapshot_and_create_nothing(service):
    write(service, CLUSTERS)
    before = service.graph_stats()
    assert before['graph']['status'] == 'absent' and 'analysis' not in before
    assert service.archive.execute("SELECT 1 FROM archive_meta WHERE key='graph.snapshot'").fetchone() is None
    service.graph_query(action='search', query='one')
    after = service.graph_stats()
    assert after['analysis']['communities'] >= 2
    assert after['analysis']['verified'] is False
    assert after['analysis']['snapshot'] == after['graph']['source_digest']


def test_a_snapshot_published_before_analysis_is_rebuilt_not_served(service):
    write(service, CLUSTERS)
    service.graph_query(action='search', query='one')
    row = service.archive.execute("SELECT value FROM archive_meta WHERE key='graph.snapshot'").fetchone()
    old = json.loads(row[0])
    del old['analysis_version']
    with service.archive:
        service.archive.execute("UPDATE archive_meta SET value=? WHERE key='graph.snapshot'", (json.dumps(old),))
    assert service.graph_stats()['graph']['status'] == 'stale'
    refreshed = service.graph_query(action='god_nodes')
    assert refreshed['graph']['status'] == 'current'
    assert refreshed['graph']['analysis_version'] == 1


def test_entity_index_rebuild_restores_postings_from_entity_rows(service):
    from kelmo.store.graph.index import rebuild_entity_index
    write(service, CLUSTERS)
    service.graph_rebuild()
    connection = service.archive

    def hits():
        return connection.execute("SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH 'four'").fetchone()[0]
    assert hits() == 1
    with connection:
        connection.execute("INSERT INTO entities_fts(entities_fts) VALUES('delete-all')")
    assert hits() == 0
    with connection:
        assert rebuild_entity_index(connection)['entities'] == connection.execute(
            'SELECT COUNT(*) FROM entities').fetchone()[0]
    assert hits() == 1


def test_ambiguous_shorthand_keeps_its_candidates(service):
    write(service, {'x/setup': 'First.', 'y/setup': 'Second.'})
    result = service.graph_query(action='neighbors', entity_id='setup')['result']
    assert result['error'] == 'ambiguous_entity'
    assert result['candidates'] == ['x/setup', 'y/setup']


def test_shorthand_names_pages_only_and_placeholders_keep_exact_ids(service):
    write(service, {'a/setup': 'The live page.', 'b/page': '[[old/setup]]'})
    neighbors = service.graph_query(action='neighbors', entity_id='setup')['result']
    assert isinstance(neighbors, list)
    assert service.graph_read('setup')['id'] == 'a/setup'
    exact = service.graph_query(action='neighbors', entity_id='unresolved:old/setup')['result']
    assert [node['id'] for node in exact] == ['b/page']
    assert service.graph_query(action='neighbors', entity_id='nothing-here')['result'] == []


def test_a_plain_link_resolves_to_the_page_in_its_own_folder_first(service):
    write(service, {'x/setup': 'Local setup.', 'y/setup': 'Other setup.', 'x/one': '[[setup]]'})
    result = service.graph_query(action='neighbors', entity_id='x/one')
    assert [row['id'] for row in result['result']] == ['x/setup']
    # Without a page in its own folder the short name stays ambiguous, as before.
    write(service, {'z/two': '[[setup]]'})
    assert [row['id'] for row in service.graph_query(action='neighbors', entity_id='z/two')['result']] == [
        'unresolved:setup']


def test_navigation_answers_label_the_derived_fields_they_carry(service):
    write(service, CLUSTERS)
    for action, options in (('neighbors', {'entity_id': 'a/one'}), ('impact', {'entity_id': 'a/two'}),
                            ('search', {'query': 'one'}), ('path', {'entity_id': 'a/one', 'target_id': 'b/five'}),
                            ('neighbors', {'entity_id': 'a/one', 'exclude_god_nodes': True})):
        result = service.graph_query(action=action, **options)
        label = result['analysis']
        assert label['verified'] is False and label['snapshot'] == result['graph']['source_digest'], action
        assert len(label['assignment_sha256']) == 64 and label['implementation'].startswith('1/networkx-')


def test_nodes_keep_the_legacy_topology_fields(service):
    write(service, CLUSTERS)
    node = service.graph_query(action='neighbors', entity_id='a/two')['result'][0]
    assert {'community_id', 'surprise_score', 'is_god_node'} <= node.keys()
    assert node['community_id'] is not None and node['is_god_node'] == 0
    member = service.graph_query(action='communities')['result'][0]['members'][0]
    assert 'community_id' in member


def test_template_named_pages_keep_their_identities_and_links(service):
    write(service, {'a/one': '[[a/two]]', 'a/two': '[[a/one]]', '_daily-template': '[[placeholder-target]]'})
    stats = service.graph_rebuild()
    assert service.archive.execute("SELECT COUNT(*) FROM entities WHERE id IN "
                                   "('_daily-template', 'unresolved:placeholder-target')").fetchone()[0] == 2
    assert stats['counts']['documents'] >= 3


def test_a_read_phase_database_error_is_not_recorded_as_a_failed_publication(service):
    import sqlite3
    from unittest.mock import patch
    write(service, CLUSTERS)
    service.graph_rebuild()
    with patch('kelmo.graph.graph_query', side_effect=sqlite3.OperationalError('read fault')):
        with pytest.raises(KelmoError) as refused:
            service.graph_query(action='neighbors', entity_id='a/one')
    assert refused.value.code == 'E_GRAPH_DB'
    assert service.graph_stats()['graph']['status'] == 'current'


def test_analytics_name_their_implementation_and_assignment(service, monkeypatch):
    write(service, CLUSTERS)
    first = service.graph_query(action='communities')
    label = first['analysis']
    assert label['implementation'].startswith('1/networkx-')
    assert len(label['assignment_sha256']) == 64
    assert service.graph_query(action='god_nodes', assignment=label['assignment_sha256'])['graph']['status'] == 'current'
    with pytest.raises(KelmoError) as refused:
        service.graph_query(action='god_nodes', assignment='0' * 64)
    assert refused.value.code == 'E_GRAPH_STALE'
    monkeypatch.setattr('kelmo.graph.implementation', lambda: '1/networkx-other')
    assert service.graph_stats()['graph']['status'] == 'stale'
    assert service.graph_query(action='communities')['analysis']['implementation'] == '1/networkx-other'


def test_analytics_options_are_validated(service):
    for options in ({'community_id': -1}, {'exclude_god_nodes': 'yes'}, {'min_surprise': 2},
                    {'snapshot': 'not-a-digest'}, {'assignment': 'not-a-digest'}):
        with pytest.raises(KelmoError) as refused:
            service.graph_query(action='communities', **options)
        assert refused.value.code == 'E_INPUT'
