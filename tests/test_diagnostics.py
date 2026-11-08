"""Every rejected change names the fact, its length, the page and the repair that is permitted."""
import json

import pytest

from test_fact_checks import content
from kelmo.errors import KelmoError
from kelmo.wiki import engine

pytest_plugins = ['test_fact_checks']

FIELDS = ('fact_key', 'target', 'text', 'status', 'expected_revision', 'replaces')


def change(**overrides):
    value = dict(fact_key='fixture.fact', target='projects/fixture/fixture.md', text='A durable fact.',
                 status='active', expected_revision=None, replaces=[])
    value.update(overrides)
    return value


def test_an_oversized_fact_names_the_fact_its_length_the_page_and_the_repair():
    long_text = 'A durable fact that keeps going. ' * 20
    with pytest.raises(ValueError) as raised:
        engine.validate_changes([change(text=long_text)])
    problem = raised.value
    assert problem.fact_key == 'fixture.fact'
    assert problem.target == 'projects/fixture/fixture.md'
    assert problem.length == len(long_text)
    assert problem.repair.startswith('shorten this fact')
    assert str(problem) == (
        'fact fixture.fact on page projects/fixture/fixture.md (' + str(len(long_text))
        + ' characters): the fact text must be one plain line of at most 400 characters; '
        'permitted repair: shorten this fact to a single line of at most 400 characters without markup')


@pytest.mark.parametrize(('field', 'broken', 'names_page'), [
    ('status', 'done', True),
    ('expected_revision', 'nope', True),
    ('replaces', 'not-a-list', True),
    ('target', '../escape.md', False),
])
def test_every_rejected_field_names_the_fact_and_says_what_is_permitted(field, broken, names_page):
    with pytest.raises(ValueError) as raised:
        engine.validate_changes([change(**{field: broken})])
    problem = raised.value
    assert 'permitted repair: ' in str(problem)
    assert problem.fact_key == 'fixture.fact'
    assert problem.target == ('projects/fixture/fixture.md' if names_page else None)


def test_a_value_that_fails_its_own_shape_check_is_never_echoed():
    for field, broken in (('fact_key', 'Bad Key'), ('target', 'PRIVATE_TARGET_SENTINEL/../escape.md')):
        with pytest.raises(ValueError) as raised:
            engine.validate_changes([change(**{field: broken})])
        assert broken not in str(raised.value)
    with pytest.raises(ValueError) as raised:
        engine.validate_changes([change(target='PRIVATE_TARGET_SENTINEL')])
    assert 'fact fixture.fact: the wiki target is not a relative Markdown page' in str(raised.value)


def test_a_missing_field_is_named_as_part_of_the_six_the_change_must_carry():
    broken = {field: value for field, value in change().items() if field != 'status'}
    with pytest.raises(ValueError) as raised:
        engine.validate_changes([broken])
    assert 'the change must carry exactly ' + ', '.join(sorted(FIELDS)) in str(raised.value)
    assert 'permitted repair: submit the change with those six fields' in str(raised.value)


def registry_facts(service):
    return engine.load_registry(service.root)['facts']


def test_a_stale_revision_conflict_names_the_fact_the_page_and_both_revisions(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'first.md', 'lead', 'promote')
    assert service.run(operation['id'])['state'] == 'applied'
    registry = engine.load_registry(service.root)
    stored = registry['facts']['fixture.fact']
    stale = {name: value for name, value in change(text='A different fact.', expected_revision='0' * 64).items()
             if name != 'replaces'}
    intent = {'evidence_ref': stored['evidence_refs'][0], 'source_operation_id': 'synthetic', 'rule_change': None,
              'filename': 'second.md', 'delta': {'pages': [], 'facts': [stale], 'narratives': []}}
    with pytest.raises(engine.Conflict) as raised:
        engine.apply_semantic_intent(registry, intent)
    problem = raised.value
    assert problem.fact_key == 'fixture.fact'
    assert problem.target == 'projects/fixture/fixture.md'
    assert 'does not match the stored revision ' + stored['revision'][:12] in str(problem)
    assert 'permitted repair: read the page again' in str(problem)


def test_a_resolution_rejection_carries_the_named_fact_into_the_review_reason(service):
    from kelmo.resolution import Resolution
    from test_resolution import pending
    operation = pending(service)
    queue = Resolution(service)
    request = queue.claim()
    changes = json.loads(json.dumps(request['payload']['changes']))
    changes[0]['text'] = 'x' * 500
    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'changes': changes})
    assert raised.value.code == 'E_PROPOSAL'
    assert 'fact fixture.fact on page projects/fixture/fixture.md (500 characters)' in str(raised.value)
    exhausted = queue.fail(request['id'], request['token'], 'E_PROPOSAL', 1, str(raised.value))
    assert exhausted is True
    reason = service.journal.get(operation['id'])['outcome']['reason']
    assert 'permitted repair: shorten this fact' in reason
    assert len(reason) <= 400


def test_a_session_candidate_rejection_names_the_candidate_and_the_fact(service):
    from kelmo import session_worker
    with pytest.raises(KelmoError) as raised:
        session_worker.proposal(service, 'A note.', [{'text': 'y' * 500, 'target': 'projects/fixture/fixture.md'}],
                                ['/synthetic/executor'], lambda *args, **kwargs: {}, 120)
    assert raised.value.code == 'E_PROPOSAL'
    assert ('Session fact candidate 0: fact session.candidate.0 on page projects/fixture/fixture.md '
            '(500 characters)') in str(raised.value)


def test_a_session_proposal_rejection_reaches_the_operator(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture('Evidence kept as archive only.', 'note.md', 'reviewer', 'archive')
    assert service.run(operation['id'])['state'] == 'archived'
    with pytest.raises(KelmoError) as raised:
        service.propose_archived_note(operation['id'], [change(text='z' * 450)],
                                      [{'fact_key': 'fixture.fact',
                                        'verified_match': {'result': 'not_found',
                                                           'search_id': receipt['search_id']}}],
                                      'session:1')
    assert raised.value.code == 'E_PROPOSAL'
    assert 'Session proposal is invalid or stale: fact fixture.fact' in str(raised.value)
    assert '(450 characters)' in str(raised.value)


def checks(changes, items):
    return ('```wiki-changes\n' + json.dumps(changes) + '\n```\n'
            + '```fact-checks\n' + json.dumps(items) + '\n```\n')


def test_a_note_without_fact_checks_says_which_block_is_missing(service):
    with pytest.raises(KelmoError) as raised:
        service.capture(content(), 'note.md', 'lead', 'promote')
    assert raised.value.code == 'E_FACT_CHECK'
    assert 'the note must carry exactly one fact-checks block' in str(raised.value)
    assert 'permitted repair: add one fact-checks block' in str(raised.value)


def test_a_check_that_belongs_to_no_promoted_fact_is_named(service):
    with pytest.raises(KelmoError) as raised:
        service.capture(checks([change()], [{'fact_key': 'other.fact',
                                             'verified_match': {'result': 'not_found',
                                                                'search_id': 'invented'}}]),
                        'note.md', 'lead', 'promote')
    assert 'fact other.fact: the checks name a fact the note does not promote' in str(raised.value)
    with pytest.raises(KelmoError) as raised:
        service.capture(checks([change()], []), 'note.md', 'lead', 'promote')
    assert 'fact fixture.fact: the note promotes this fact without a check' in str(raised.value)


def test_a_stale_existing_reference_names_the_revision_it_holds_and_the_one_the_registry_holds(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'first.md', 'lead', 'promote')
    assert service.run(operation['id'])['state'] == 'applied'
    stored = registry_facts(service)['fixture.fact']
    with pytest.raises(KelmoError) as raised:
        service.capture(content({'result': 'existing', 'fact_key': 'fixture.fact', 'revision': '0' * 64},
                                revision=stored['revision']), 'second.md', 'lead', 'promote')
    assert ('fact fixture.fact: the check names revision ' + '0' * 12
            + ' while the registry holds ' + stored['revision'][:12]) in str(raised.value)
    assert 'permitted repair: run kelmo reconcile and copy the current revision' in str(raised.value)


def test_a_search_receipt_that_does_not_match_names_the_reconcile_to_rerun(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    with pytest.raises(KelmoError) as raised:
        service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']},
                                text='A different fact.'), 'note.md', 'lead', 'promote')
    assert ('fact fixture.fact: its search receipt does not match this text and target '
            'against the current registry') in str(raised.value)
    assert ('permitted repair: run kelmo reconcile --text this_fact --target '
            'projects/fixture/fixture.md and use the search_id it returns') in str(raised.value)
