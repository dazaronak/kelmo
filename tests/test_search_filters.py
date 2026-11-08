"""kelmo search filters: an exact scope narrows a search and never widens it."""
import json
from unittest.mock import patch

import pytest

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.errors import KelmoError


@pytest.fixture
def service(tmp_path):
    from kelmo.service import Service

    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)), Service(root) as value:
        yield value


def note(project, tags, body):
    return f'---\nproject: {project}\ndomain: dev\ntags: [{", ".join(tags)}]\n---\n\n{body}\n'


def add(service, filename, text):
    return service.run(service.capture(text, filename, 'implementer')['id'])['archive_id']


def ids(service, query, **filters):
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        return {item['id'] for item in service.search(query, with_metadata=True, **filters)['items']}


@pytest.fixture
def three(service):
    return {
        'gate': add(service, 'gate.md', note('kelmo', ['gate', 'counts'], 'The gate prints fifteen sections.')),
        'hooks': add(service, 'hooks.md', note('hooks', ['counts'], 'The hooks gate prints eleven sections.')),
        'plain': add(service, '2026-01-02_plain.md', 'The release gate prints nine sections.\n'),
    }


def test_no_filter_returns_every_scope(service, three):
    assert ids(service, 'gate prints sections') == set(three.values())


def test_project_narrows_to_the_frontmatter_project(service, three):
    assert ids(service, 'gate prints sections', project='kelmo') == {three['gate']}
    assert ids(service, 'gate prints sections', project='default') == {three['plain']}


def test_tags_come_from_frontmatter_and_every_given_tag_must_hold(service, three):
    assert ids(service, 'gate prints sections', tags=['counts']) == {three['gate'], three['hooks']}
    assert ids(service, 'gate prints sections', tags=['counts', 'gate']) == {three['gate']}
    assert ids(service, 'gate prints sections', tags=['role:implementer']) == set(three.values())


def test_dates_bound_by_the_filename_date_or_the_storage_moment(service, three):
    assert ids(service, 'gate prints sections', until='2026-01-02') == {three['plain']}
    assert three['plain'] not in ids(service, 'gate prints sections', since='2026-01-03')


def test_the_metadata_names_the_filters_that_were_applied(service, three):
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        result = service.search('gate', with_metadata=True, project='hooks', tags=['counts'])
    assert result['search']['filters'] == {'project': 'hooks', 'tags': ['counts']}
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        assert 'filters' not in service.search('gate', with_metadata=True)['search']


@pytest.mark.parametrize('filters', [{'project': 'two words'}, {'tags': 'counts'}, {'tags': ['%']},
                                     {'since': 'yesterday'}, {'until': '10/10/2026'},
                                     {'since': '2026-10-10T12:00'}, {'until': '2026-10-10+02:00'}])
def test_a_malformed_filter_is_refused(service, filters):
    with pytest.raises(KelmoError) as refused:
        service.search('gate', **filters)
    assert refused.value.code == 'E_INPUT'


def test_an_unsafe_frontmatter_tag_is_not_stored(service):
    record = add(service, 'odd.md', note('x', ['ok', 'not ok', '%'], 'A record with odd tags.'))
    assert json.loads(service.show(record)['tags_json']) == ['test-node', 'role:implementer', 'ok']


def test_a_source_cannot_claim_a_role_or_the_node_label(service):
    record = add(service, 'claim.md', note('x', ['role:lead', 'test-node', 'kept'], 'A collector posing as lead.'))
    assert json.loads(service.show(record)['tags_json']) == ['test-node', 'role:implementer', 'kept']


def test_a_tag_matches_exactly_not_as_a_pattern(service):
    exact = add(service, 'a.md', note('p', ['ops_review'], 'Review the ops gate weekly.'))
    add(service, 'b.md', note('p', ['opsXreview'], 'Review the ops gate monthly.'))
    add(service, 'c.md', note('p', ['OPS_REVIEW'], 'Review the ops gate yearly.'))
    assert ids(service, 'review ops gate', tags=['ops_review']) == {exact}


def vector(first):
    import struct
    return struct.pack('384f', first, *([0.0] * 383))


def test_a_rare_scope_is_found_past_the_vector_candidate_window(service):
    """The vector branch keeps 200 nearest candidates; the filter must apply inside that search, not after it."""
    with patch('kelmo.store.memory.store.embed', return_value=vector(1.0)):
        for number in range(210):
            add(service, f'filler-{number}.md', note('bulk', ['filler'], f'Filler record number {number}.'))
    with patch('kelmo.store.memory.store.embed', return_value=vector(-1.0)):
        rare = add(service, 'rare.md', note('rare-project', ['rare'], 'The only record in its project.'))
    with patch('kelmo.store.memory.search.embed', return_value=vector(1.0)):
        hybrid = service.search('unrelated words', with_metadata=True, project='rare-project')['items']
    assert [item['id'] for item in hybrid] == [rare]


def test_filters_through_the_cli(service, three, capsys):
    from kelmo.cli import main

    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        code = main(['search', 'gate prints sections', '--project', 'hooks', '--tag', 'counts',
                     '--vault', str(service.root), '--json'])
    assert code == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert [item['id'] for item in result['items']] == [three['hooks']]


def test_pages_by_offset_cover_every_visible_record_once(service):
    stored = {add(service, f'page-{index}.md', note('p', ['paged'], f'Paged record {index}.')) for index in range(7)}
    first = service.list(3)
    seen = [item['id'] for offset in range(0, first['total'], 3) for item in service.list(3, offset)['items']]
    assert first['total'] == len(stored) == 7
    assert sorted(seen) == sorted(stored)


def test_a_negative_or_fractional_offset_is_refused(service):
    for offset in (-1, 1.5):
        with pytest.raises(KelmoError) as refused:
            service.list(3, offset)
        assert refused.value.code == 'E_INPUT'


def test_a_tags_iterator_is_kept_after_validation():
    from kelmo.service import Service
    assert Service._filters(None, iter(['ferry', 'schedule']), None, None)['tags'] == ['ferry', 'schedule']
