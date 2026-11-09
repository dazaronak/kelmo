"""Batched Dreaming apply: the view that carries a batch, the answer that names its sources and the
split that settles each source on its own.

These cases measure the shape of the batch path — payload, attribution, per-source limits and the
capture-order collision rule — over the synthetic registry of test_editor_scoping. The apply pass that
uses them is `tests/test_dreaming_batched_apply.py`.
"""

import pytest

from test_editor_scoping import FEATURE, NOTES, SEED, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def batch_items(world, count=2, key='batch'):
    """Archived sources in capture order, each with its own operation id and evidence reference."""
    items = []
    for index in range(count):
        text = f'App note {index}: the app pipeline moves to 0{index}:1{index} after the audit.'
        operation, reference = world.archive(text, key=f'{key}{index}')
        items.append({'operation_id': operation, 'text': text, 'evidence': reference})
    return items


def registry_with(facts=(), pages=(), narratives=()):
    """A v3 registry shaped like the engine's, from entries given one per call."""
    def merged(items):
        registry = {}
        for item in [items] if isinstance(items, dict) else items:
            registry.update(item)
        return registry

    return {'version': 3, 'facts': merged(facts), 'actions': {}, 'narratives': merged(narratives),
            'notes': {}, 'pages': merged(pages)}


def page_entry(target='projects/app/notes.md', page_id='app-notes', revision='a' * 64):
    return {target: {'page_id': page_id, 'layout': 'topic', 'category': 'app', 'revision': revision,
                     'title': 'Notes', 'summary': 'Notes cover the export pipeline.', 'links': [],
                     'parent': None, 'priority': 'H2'}}


def fact_entry(key='app.notes-scope', target='projects/app/notes.md', revision='b' * 64, **extra):
    return {key: {'fact_key': key, 'target': target, 'text': 'Notes cover the export pipeline.',
                  'status': 'active', 'revision': revision, 'previous_revision': None, 'owner': 'semantic-editor',
                  'protected': False, 'mandatory': False, 'evidence_resolution': 'exact',
                  'evidence_refs': [], 'note_id': 'n' * 64, 'ark_id': 'a' * 64, 'id': key[:12],
                  'replaces': [], 'compatibility': {}, **extra}}


def fact_change(key='app.notes-scope', target='projects/app/notes.md', text='Notes cover imports too.',
                expected_revision='b' * 64, **extra):
    return dict(fact_key=key, target=target, text=text, status='active',
                expected_revision=expected_revision, **extra)


def check_entry(key='app.notes-scope', **extra):
    return dict(fact_key=key, verified_match={'result': 'not_found', 'search_id': 'c' * 32}, **extra)


def test_the_batch_view_names_its_sources_and_keeps_every_evidence_reference(world):
    from kelmo.wiki import views

    reader = world.service.knowledge
    items = batch_items(world, 3)
    out = views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, sources=items)
    payload = out['payload']
    assert 'source' not in payload
    assert [block['operation_id'] for block in payload['sources']] == [item['operation_id'] for item in items]
    assert [block['evidence']['content_sha256'] for block in payload['sources']] == [
        item['evidence'].content_sha256 for item in items]
    assert out['bundle'].source_mappings == tuple(item['evidence'] for item in items)
    assert out['used_tokens'] == payload['budget']['used_tokens']


def test_a_batch_view_carries_each_source_hint_and_refuses_an_unnamed_source(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import views

    reader = world.service.knowledge
    items = batch_items(world, 2)
    hinted = [{**items[0], 'hint': {'code': 'E_PROPOSAL', 'reason': 'the last proposal was malformed'}},
              items[1]]
    payload = views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, sources=hinted)['payload']
    assert payload['sources'][0]['hint'] == {'code': 'E_PROPOSAL', 'reason': 'the last proposal was malformed'}
    assert 'hint' not in payload['sources'][1]
    with pytest.raises(KelmoError) as error:
        views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000,
                          sources=[{**items[0], 'text': None}])
    assert error.value.code == 'E_INPUT'


def test_the_single_source_payload_is_unchanged_by_the_batch_branch(world):
    from kelmo.wiki import views

    reader = world.service.knowledge
    text = 'The export now runs hourly.'
    _source, reference = world.archive(text, key='single-shape')
    payload = views.editor_view(reader, reader.generation(), 'app', [FEATURE, NOTES], 60000,
                                source_text=text, evidence=reference)['payload']
    assert payload['source'] == {'evidence': reference.to_dict(), 'text': text}
    assert 'sources' not in payload


def test_normalize_attributes_each_item_to_its_source_and_strips_the_field():
    from kelmo.editor import normalize

    registry = registry_with(facts=[fact_entry()], pages=page_entry())
    first, second = 'op-one', 'op-two'
    delta, checks = normalize(
        {'facts': [fact_change(text='Notes cover imports too.', source=first),
                   fact_change(key='app.archive-scope', target='projects/app/notes.md', text='Archive notes '
                                          'cover the audit.', expected_revision=None, source=second)],
         'fact_checks': [check_entry('app.notes-scope', source=first),
                         dict(fact_key='app.archive-scope', verified_match={'result': 'not_found',
                                                                           'search_id': 'd' * 32}, source=second)]},
        ['projects/app/notes.md'], registry, rule_change=False, project='app',
        sources={first: {'rule_change': False}, second: {'rule_change': False}})
    assert [item['source_operation_id'] for item in delta['facts']] == [first, second]
    assert all('source' not in item for item in delta['facts'])
    assert [check['fact_key'] for check in checks] == ['app.notes-scope', 'app.archive-scope']
    assert all('source' not in check for check in checks)


def test_an_item_naming_a_source_the_batch_did_not_send_is_malformed():
    from kelmo.editor import ProposalError, normalize

    registry = registry_with(facts=[fact_entry()], pages=page_entry())
    with pytest.raises(ProposalError) as error:
        normalize({'facts': [fact_change(source='op-other')], 'fact_checks': [check_entry('app.notes-scope')]},
                  ['projects/app/notes.md'], registry, rule_change=False, project='app',
                  sources={'op-one': {'rule_change': False}})
    assert 'did not send' in str(error.value)


def test_a_batch_item_without_attribution_is_malformed_and_a_single_source_one_is_not():
    from kelmo.editor import ProposalError, normalize

    registry = registry_with(facts=[fact_entry()], pages=page_entry())
    with pytest.raises(ProposalError) as error:
        normalize({'facts': [fact_change()], 'fact_checks': [check_entry('app.notes-scope')]},
                  ['projects/app/notes.md'], registry, rule_change=False, project='app',
                  sources={'op-one': {'rule_change': False}, 'op-two': {'rule_change': False}})
    assert 'names the source' in str(error.value)
    delta, _ = normalize({'facts': [fact_change()], 'fact_checks': [check_entry('app.notes-scope')]},
                         ['projects/app/notes.md'], registry, rule_change=False, project='app',
                         sources={'op-one': {'rule_change': False}})
    assert delta['facts'][0]['source_operation_id'] == 'op-one'
    with pytest.raises(ProposalError):
        normalize({'facts': [fact_change(source='op-one')], 'fact_checks': [check_entry('app.notes-scope')]},
                  ['projects/app/notes.md'], registry, rule_change=False, project='app')


def test_the_per_source_limits_apply_to_each_source_and_not_to_the_batch_answer():
    from kelmo.editor import LIMITS, ProposalError, normalize

    registry = registry_with(pages=page_entry())
    ceiling = LIMITS['facts']
    facts = [fact_change(key=f'app.fact-{index}', text=f'Notes record claim number {index}.',
                         expected_revision=None, source='op-one') for index in range(ceiling)]
    facts.append(fact_change(key='app.fact-two', text='The second source records its own claim.',
                             expected_revision=None, source='op-two'))
    delta, _ = normalize({'facts': facts}, ['projects/app/notes.md'], registry, rule_change=False,
                         project='app', sources={'op-one': {'rule_change': False},
                                                 'op-two': {'rule_change': False}})
    assert len(delta['facts']) == ceiling + 1
    over = facts + [fact_change(key='app.fact-over', text='One claim too many for its source.',
                                expected_revision=None, source='op-one')]
    with pytest.raises(ProposalError) as error:
        normalize({'facts': over}, ['projects/app/notes.md'], registry, rule_change=False,
                  project='app', sources={'op-one': {'rule_change': False}, 'op-two': {'rule_change': False}})
    assert 'for the source it comes from' in str(error.value)


def test_a_protected_fact_changes_only_under_the_source_that_carries_rule_change():
    from kelmo.editor import ProposalError, normalize

    registry = registry_with(facts=[fact_entry('rule.refundable-travel', 'person.md', 'c' * 64,
                                               protected=True, mandatory=True)],
                             pages=page_entry('person.md', 'person'))
    sources = {'op-plain': {'rule_change': False}, 'op-requester': {'rule_change': True}}
    change = fact_change('rule.refundable-travel', 'person.md', 'Book travel with refundable fares, '
                                                                'except trips under one day.', source='op-plain')
    with pytest.raises(ProposalError) as error:
        normalize({'facts': [change]}, ['person.md'], registry, rule_change=False, project='app', sources=sources)
    assert 'protected rule' in str(error.value)
    delta, _ = normalize({'facts': [dict(change, source='op-requester')]}, ['person.md'], registry,
                         rule_change=False, project='app', sources=sources)
    assert delta['facts'][0]['source_operation_id'] == 'op-requester'


def test_split_delta_cuts_in_capture_order_and_parks_the_later_collision():
    from kelmo.editor import split_delta

    order = ['op-one', 'op-two']
    delta = {'facts': [fact_change(text='Notes cover imports too.', source_operation_id='op-one'),
                       fact_change(text='Notes cover imports too.', source_operation_id='op-two'),
                       fact_change(key='app.archive-scope', text='Archive notes cover the audit.',
                                   expected_revision=None, source_operation_id='op-two')],
             'narratives': [], 'pages': []}
    checks = [check_entry('app.notes-scope'), check_entry('app.archive-scope')]
    slices, refusals = split_delta(delta, checks, order)
    assert [item['fact_key'] for item in slices['op-one']['delta']['facts']] == ['app.notes-scope']
    assert [item['fact_key'] for item in slices['op-two']['delta']['facts']] == ['app.archive-scope']
    assert [check['fact_key'] for check in slices['op-one']['fact_checks']] == ['app.notes-scope']
    assert [check['fact_key'] for check in slices['op-two']['fact_checks']] == ['app.archive-scope']
    assert all('source_operation_id' not in item for part in slices.values() for item in part['delta']['facts'])
    assert refusals == {'op-two': ['fact app.notes-scope is also proposed by an earlier source']}


def test_split_delta_gives_a_contested_page_target_and_page_id_to_the_earlier_source():
    from kelmo.editor import split_delta

    order = ['op-one', 'op-two']
    created = {'target': 'projects/app/audit.md', 'expected_revision': None, 'page_id': 'app-audit',
               'layout': 'topic', 'title': 'Audit', 'summary': 'The audit trail.',
               'parent': 'projects/app/notes.md'}
    delta = {'facts': [], 'narratives': [],
             'pages': [dict(created, source_operation_id='op-one'),
                       dict(created, title='Audit log', source_operation_id='op-two'),
                       dict(created, target='projects/app/audit-log.md', page_id='app-audit',
                            title='Audit notes', source_operation_id='op-two'),
                       dict(created, target='projects/app/audit-notes.md', page_id='app-audit-notes',
                            title='Audit entries', source_operation_id='op-two')]}
    slices, refusals = split_delta(delta, [], order)
    assert [page['title'] for page in slices['op-one']['delta']['pages']] == ['Audit']
    assert [page['title'] for page in slices['op-two']['delta']['pages']] == ['Audit entries']
    assert all('source_operation_id' not in page for part in slices.values() for page in part['delta']['pages'])
    assert refusals == {'op-two': ['page projects/app/audit.md is also proposed by an earlier source',
                                   'page ID app-audit is also proposed by an earlier source']}


def test_split_delta_reports_a_source_with_no_slice_as_no_change():
    from kelmo.editor import split_delta

    delta = {'facts': [fact_change(source_operation_id='op-one')], 'narratives': [], 'pages': []}
    slices, refusals = split_delta(delta, [], ['op-one', 'op-two'])
    assert slices['op-two'] == {'delta': {'facts': [], 'narratives': [], 'pages': []}, 'fact_checks': []}
    assert refusals == {}


def test_the_editing_instruction_names_the_batch_shape_the_schema_accepts():
    from kelmo.editor import EDITING, EDITING_SCHEMA

    assert '"sources"' in EDITING
    assert 'operation_id' in EDITING
    for name in ('facts', 'narratives', 'pages', 'fact_checks'):
        assert 'source' in EDITING_SCHEMA['properties'][name]['items']['properties']
        assert 'source' not in EDITING_SCHEMA['properties'][name]['items']['required']
    assert SEED and NOTES
