import json
from unittest.mock import patch

import pytest

from kelmo.errors import KelmoError
from kelmo.service import Service


def test_reconcile_distinguishes_registry_size_from_lexical_matches(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'created.md', 'lead', 'promote')
    assert service.run(operation['id'])['state'] == 'applied'
    result = service.reconcile('zzzz', 'projects/fixture/fixture.md')
    assert result['candidates'] == []
    assert result['total_facts'] == 1
    assert result['matched_facts'] == 0

@pytest.fixture
def service(tmp_path):
    Service.initialize(tmp_path / 'vault', 'synthetic', project='fixture')
    with Service(tmp_path / 'vault') as value, patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        yield value


def content(check=None, text='A durable fact.', key='fixture.fact', revision=None):
    change = dict(fact_key=key, target='projects/fixture/fixture.md', text=text,
                  status='active', expected_revision=revision, replaces=[])
    result = '```wiki-changes\n' + json.dumps([change]) + '\n```\n'
    if check is not None:
        result += '```fact-checks\n' + json.dumps([dict(fact_key=key, verified_match=check)]) + '\n```\n'
    return result


def test_missing_and_invented_proof_are_rejected_before_capture(service):
    for value in (content(), content({'result': 'not_found', 'search_id': 'invented'})):
        with pytest.raises(KelmoError):
            service.capture(value, 'note.md', 'lead', 'promote')
    assert service.queue() == []


def test_real_search_receipt_is_bound_to_text_and_survives_reopen(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    check = {'result': 'not_found', 'search_id': receipt['search_id']}
    with pytest.raises(KelmoError):
        service.capture(content(check, text='Different fact.'), 'bad.md', 'lead', 'promote')
    with Service(service.root) as reopened:
        op = reopened.capture(content(check), 'good.md', 'lead', 'promote')
        assert reopened.run(op['id'])['state'] == 'applied'


def test_unrelated_registry_change_keeps_pending_search_receipt_valid(service):
    one = service.reconcile('Alpha basalt.', 'projects/fixture/fixture.md')
    two = service.reconcile('Omega cedar.', 'projects/fixture/fixture.md')
    first = service.capture(content({'result': 'not_found', 'search_id': one['search_id']},
                                    text='Alpha basalt.'),
                            'first.md', 'lead', 'promote')
    second = service.capture(content({'result': 'not_found', 'search_id': two['search_id']},
                                     text='Omega cedar.', key='fixture.second'),
                             'second.md', 'lead', 'promote')
    assert service.run(first['id'])['state'] == 'applied'
    assert service.run(second['id'])['state'] == 'applied'


def test_new_matching_candidate_invalidates_pending_search_receipt(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    check = {'result': 'not_found', 'search_id': receipt['search_id']}
    first = service.capture(content(check), 'first.md', 'lead', 'promote')
    second = service.capture(content(check, key='fixture.duplicate'), 'second.md', 'lead', 'promote')
    assert service.run(first['id'])['state'] == 'applied'
    result = service.run(second['id'])
    assert result['state'] == 'needs_resolution'
    assert result['error_code'] == 'E_FACT_CHECK'


def test_five_independent_queued_facts_all_apply(service):
    operations = []
    for index, (noun, detail) in enumerate((('basalt', 'alpha'), ('cedar', 'bravo'),
                                             ('quartz', 'charlie'), ('willow', 'delta'),
                                             ('marble', 'echo'))):
        text = f'{noun.title()} {detail}.'
        receipt = service.reconcile(text, 'projects/fixture/fixture.md')
        operations.append(service.capture(
            content({'result': 'not_found', 'search_id': receipt['search_id']},
                    text=text, key=f'fixture.independent{index}'),
            f'independent{index}.md', 'lead', 'promote'))
    assert [service.run(item['id'])['state'] for item in operations] == ['applied'] * 5
    assert service.status()['archive_records'] == 5


def test_existing_reference_requires_real_key_and_revision(service):
    from kelmo.wiki import engine
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    op = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                         'first.md', 'lead', 'promote')
    service.run(op['id'])
    fact = engine.load_registry(service.root)['facts']['fixture.fact']
    check = {'result': 'existing', 'fact_key': 'fixture.fact', 'revision': fact['revision']}
    with pytest.raises(KelmoError):
        service.capture(content(check, text='Updated.'), 'wrong.md', 'lead', 'promote')
    op = service.capture(content(check, text='Updated.', revision=fact['revision']),
                         'update.md', 'lead', 'promote')
    assert service.run(op['id'])['state'] == 'applied'
