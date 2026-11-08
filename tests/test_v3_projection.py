"""Controller-run page projection cases.

Pure cases render registries built by the test seeding helper and compare the managed
regions with the offline contract parser. Integration cases reuse test_action_state's real
fixture, publisher and doctor. Nothing here edits a registry outside StateAPI or seeding.
"""

import copy

import pytest

from kelmo.contracts import EvidenceRef
from test_action_state import APP_PAGE, WEB_PAGE, PAGES, TOPIC_PAGE, world_factory as world_factory  # noqa: F401


AUTHORITY = '00000000-0000-4000-8000-00000000000a'
SOURCE = {'archive_id': 'archive-one', 'version': 1, 'content_sha256': 'a' * 64, 'source_id': 'demo-source'}
WORK = 'b' * 40
LEGACY_HEADER = '---\r\nupdated: 2026-01-01\r\ncustom: keep  \r\n---\r\n'
LEGACY_BODY = ('# Old title\r\n\r\nHand prose.  \r\n<!-- kb -->\n- old row\n<!-- /kb -->\r\n'
               'Tail bytes.\t\r\n')


@pytest.fixture
def world(world_factory):
    return world_factory()


def adopted(pages_settings, facts=(), bindings=None, protected=(), repositories=('repo-app',), headers=None):
    import registry_seed
    spec = {'facts': {item['fact_key']: item for item in facts},
            'notes': {item['note_id']: {'filename': 'source.md', 'ark_id': item['ark_id']} for item in facts}}
    return registry_seed.build(spec, authority_id=AUTHORITY, evidence_bindings=bindings or {},
                               pages_settings=pages_settings, protected_fact_keys=protected, headers=headers,
                               repositories=repositories)


def with_action(registry, action_id, subject_id, kind, state='completed', subject_kind='feature', **fields):
    from kelmo.state import action_revision
    from kelmo.wiki import engine
    record = {'action_id': action_id, 'subject_kind': subject_kind, 'subject_id': subject_id, 'kind': kind,
              **fields, 'state': state, 'owner': 'state-api', 'previous_revision': None, 'compatibility': {},
              'accepted_event': {'event_id': 'event-' + action_id, 'event_order': 1,
                                 'occurred_at': '2026-10-14T10:00:00.000000Z',
                                 'ingested_at': '2026-10-14T10:00:01.000000Z', 'transition': state,
                                 'evidence_ref': dict(SOURCE), 'evidence_mark': 'REQUESTER',
                                 'correction_of': None, 'operation_receipt_id': 'receipt-' + action_id}}
    if subject_kind == 'feature':
        record['feature_id'] = subject_id
    record['revision'] = action_revision(record)
    registry['actions'][action_id] = record
    return engine.validate_registry(registry)


def shared_feature():
    registry = adopted({APP_PAGE: dict(PAGES[APP_PAGE], repositories=['repo-app']), WEB_PAGE: PAGES[WEB_PAGE]})
    git = dict(repository_id='repo-app', work_revision=WORK)
    with_action(registry, 'push-app', 'shared-feature', 'push', remote_ref='refs/heads/feature/shared', **git)
    with_action(registry, 'merge-app', 'shared-feature', 'merge', 'pending', remote_ref='refs/heads/main', **git)
    return with_action(registry, 'deploy-app', 'shared-feature', 'deploy', 'pending', environment='sandbox',
                       artifact_id='artifact-one', **git)


def region(text, start, end):
    return text[text.index(start):text.index(end) + len(end)]


def projections(text, layout, link_ids=lambda token: set()):
    from kelmo.wiki import file_contract, pages
    tokens = file_contract._markdown().parse(region(text, pages.KB_START, pages.KB_END))
    return {key: (form, values) for key, form, values in file_contract._action_projections(tokens, link_ids, layout)}


def test_feature_page_has_contract_frontmatter_headings_and_parsable_panel():
    from kelmo.wiki import file_contract, pages
    registry = shared_feature()

    text = pages.render_v3_page(APP_PAGE, None, registry)

    assert pages.leading_header(text).splitlines() == [
        '---', 'wiki_contract: wiki-file-contract/3.0-proposal.2', 'page_id: shared-feature',
        'target: ' + APP_PAGE, 'kind: work', 'category: projects', 'parent: ""',
        'title: "Shared feature"', 'summary: "One feature shared by the app and web repositories."',
        'priority: ""', 'aliases: []', 'tags: []', '---']
    kb = region(text, pages.KB_START, pages.KB_END)
    assert [line for line in kb.splitlines() if line.startswith('## ')] == [
        '## Behavior', '## Action state', '## Related pages']
    found = projections(text, 'feature')
    assert set(found) == {'push-app', 'merge-app', 'deploy-app'}
    for key, (form, values) in found.items():
        assert form == 'table'
        assert values == file_contract._expected_action_fields(registry['actions'][key], 'feature', 'table')
    assert '[Shared feature in web](../web/shared-feature.md)' in kb
    assert pages.NARRATIVE_START not in text


OTHER_WORK = 'c' * 40


def mixed_feature(*, deploys=(('deploy-app', 'artifact-one', OTHER_WORK),)):
    """push and merge share one work revision; deployments carry their own revision and artifact."""
    registry = adopted({APP_PAGE: dict(PAGES[APP_PAGE], repositories=['repo-app'])})
    with_action(registry, 'push-app', 'shared-feature', 'push', repository_id='repo-app', work_revision=WORK,
                remote_ref='refs/heads/feature/shared')
    with_action(registry, 'merge-app', 'shared-feature', 'merge', 'pending', repository_id='repo-app',
                work_revision=WORK, remote_ref='refs/heads/main')
    for action_id, artifact, revision in deploys:
        git = dict(repository_id='repo-app', work_revision=revision) if revision else {}
        with_action(registry, action_id, 'shared-feature', 'deploy', 'pending', environment='sandbox',
                    artifact_id=artifact, **git)
    return registry


def assert_panel_matches_registry(text, registry):
    from kelmo.wiki import file_contract
    found = projections(text, 'feature')
    assert set(found) == set(registry['actions'])
    for key, (form, values) in found.items():
        assert (form, values) == ('table', file_contract._expected_action_fields(
            registry['actions'][key], 'feature', 'table')), key


@pytest.mark.parametrize('deploys', [
    (('deploy-app', 'artifact-one', OTHER_WORK),),
    (('deploy-app', 'artifact-one', WORK), ('deploy-web', 'artifact-two', WORK)),
    (('deploy-app', 'artifact-one', OTHER_WORK), ('deploy-web', 'artifact-one', WORK)),
    (('deploy-app', 'artifact-one', None),),
], ids=['mixed-revisions', 'mixed-artifacts', 'shared-and-distinct', 'non-git-deploy'])
def test_each_displayed_action_resolves_its_own_revision_and_artifact(deploys):
    from kelmo.wiki import pages
    registry = mixed_feature(deploys=deploys)

    text = pages.render_v3_page(APP_PAGE, None, registry)

    assert_panel_matches_registry(text, registry)
    kb = region(text, pages.KB_START, pages.KB_END)
    revisions = {a.get('work_revision') for a in registry['actions'].values()}
    if len(revisions) > 1:
        assert '\nWork revision: ' not in kb
        for key, action in registry['actions'].items():
            if 'work_revision' in action:
                assert f'Work revision of {key}: {action["work_revision"]}.' in kb, key
            else:
                assert f'Work revision of {key}:' not in kb, key
    artifacts = {a['artifact_id'] for a in registry['actions'].values() if a['kind'] == 'deploy'}
    if len(artifacts) > 1:
        assert '\nDeployment artifact: ' not in kb
    else:
        assert f'\nDeployment artifact: {artifacts.pop()}.' in kb


def test_single_shared_values_keep_the_existing_global_declarations():
    from kelmo.wiki import pages
    registry = shared_feature()
    text = pages.render_v3_page(APP_PAGE, None, registry)
    kb = region(text, pages.KB_START, pages.KB_END)
    assert f'\nWork revision: {WORK}.\n' in kb and '\nDeployment artifact: artifact-one.\n' in kb
    assert ' of push-app:' not in kb
    assert_panel_matches_registry(text, registry)


@pytest.mark.parametrize('mutation', ['substituted', 'unknown-action', 'missing', 'extra-global',
                                      'code-identity', 'malformed', 'code-block', 'artifact-for-push'])
def test_substituted_or_conflicting_annotations_never_bind_to_another_action(mutation):
    from kelmo.wiki import file_contract, pages
    registry = mixed_feature()
    text = pages.render_v3_page(APP_PAGE, None, registry)
    assert_panel_matches_registry(text, registry)
    push = f'Work revision of push-app: {WORK}.'
    merge = f'Work revision of merge-app: {WORK}.'
    assert push in text and merge in text
    if mutation == 'substituted':
        # Same value under a repeated identity: neither copy may bind.
        changed = text.replace(merge, push)
    elif mutation == 'unknown-action':
        changed = text.replace(merge, f'Work revision of other-app: {WORK}.')
    elif mutation == 'missing':
        changed = text.replace('\n' + merge + '\n', '\n')
    elif mutation == 'extra-global':
        changed = text.replace(push, f'Work revision: {WORK}.\n\n' + push)
    elif mutation == 'code-identity':
        changed = text.replace(push, f'Work revision of `push-app`: {WORK}.')
    elif mutation == 'malformed':
        changed = text.replace(push, f'Work revision of push-app = {WORK}.')
    elif mutation == 'code-block':
        changed = text.replace(push, '```\n' + push + '\n```')
    else:
        changed = text.replace(push, push + '\n\nDeployment artifact of push-app: artifact-one.')
    assert changed != text
    found = projections(changed, 'feature')
    mismatched = {key for key, (_, values) in found.items()
                  if values != file_contract._expected_action_fields(registry['actions'][key], 'feature', 'table')}
    assert mismatched, mutation
    for key, (_, values) in found.items():
        action = registry['actions'][key]
        for name in ('work_revision', 'artifact_id'):
            # No displayed value may come from a declaration naming a different action.
            assert values.get(name) in (None, action.get(name)), (mutation, key, name)


def test_linked_topic_shows_the_same_feature_actions_as_a_map_table():
    from kelmo.wiki import file_contract, pages
    registry = shared_feature()

    text = pages.render_v3_page(WEB_PAGE, None, registry)

    kb = region(text, pages.KB_START, pages.KB_END)
    assert [line for line in kb.splitlines() if line.startswith('## ')] == [
        '## Current scope', '## Feature boxes', '## Legacy evidence']
    assert '- [Shared feature](../app/shared-feature.md): One feature shared' in kb
    found = projections(text, 'topic', lambda token: {'shared-feature'})
    assert set(found) == {'push-app', 'merge-app', 'deploy-app'}
    for key, (_, values) in found.items():
        assert values == file_contract._expected_action_fields(registry['actions'][key], 'topic', 'table')


def test_general_review_and_booking_render_on_their_subject_without_git_fields():
    from kelmo.wiki import file_contract, pages
    registry = adopted({'person.md': PAGES['person.md'], TOPIC_PAGE: PAGES[TOPIC_PAGE]})
    with_action(registry, 'review-one', 'design-review', 'review', subject_kind='topic')
    with_action(registry, 'book-one', 'appointment', 'book', 'pending', subject_kind='task')

    person = pages.render_v3_page('person.md', None, registry)
    topic = pages.render_v3_page(TOPIC_PAGE, None, registry)

    assert set(projections(person, 'person')) == {'book-one'}
    assert set(projections(topic, 'topic')) == {'review-one'}
    for text, layout, key in ((person, 'person', 'book-one'), (topic, 'topic', 'review-one')):
        form, values = projections(text, layout)[key]
        assert form == 'general'
        assert values == file_contract._expected_action_fields(registry['actions'][key], layout, 'general')
        assert 'Repository' not in text and 'Destination' not in text
    assert [line for line in region(person, pages.KB_START, pages.KB_END).splitlines()
            if line.startswith('## ')] == ['## Mandatory rules', '## Person facts', '## General actions']


@pytest.mark.parametrize('kind,fields', [('Review!', {}), ('re', {}), ('review', {'repository_id': 'repo-app'}),
                                         ('book', {'environment': 'sandbox'})])
def test_malformed_kind_or_invented_binding_is_refused_at_runtime(kind, fields):
    registry = adopted({TOPIC_PAGE: PAGES[TOPIC_PAGE]})
    with pytest.raises(ValueError):
        with_action(registry, 'review-one', 'design-review', kind, subject_kind='topic', **fields)


def test_person_page_keeps_protected_and_unresolved_records_visible():
    from kelmo.wiki import pages
    from test_editor_context import fact
    rule = fact('person.rule', 'person.md', 'Show the exact draft before sending; local drafts are exempt.')
    retired = fact('person.ordinary', 'person.md', 'Uses a paper notebook.', status='retired')
    registry = adopted({'person.md': PAGES['person.md']}, facts=(rule, retired), protected=('person.rule',))

    text = pages.render_v3_page('person.md', None, registry)

    kb = region(text, pages.KB_START, pages.KB_END)
    rules = kb[kb.index('## Mandatory rules'):kb.index('## Person facts')]
    facts = kb[kb.index('## Person facts'):kb.index('## General actions')]
    # Neither note has an exact evidence binding, so both records stay visibly unresolved.
    assert (f"- {rule['text']} [f:{rule['id']}] [unresolved legacy state; retained status: active]"
            in rules)
    assert '[source:person-source]' not in kb
    assert (f"- {retired['text']} [f:{retired['id']}] [unresolved legacy state; retained status: retired]"
            in facts)


def test_narrative_staleness_renders_from_the_same_registry_as_its_panel():
    from kelmo.state import action_revision
    from kelmo.wiki import engine, pages
    registry = adopted({APP_PAGE: PAGES[APP_PAGE]})
    with_action(registry, 'push-app', 'shared-feature', 'push', 'pending', repository_id='repo-app',
                work_revision=WORK, remote_ref='refs/heads/feature/shared')
    action = registry['actions']['push-app']
    narrative = {'narrative_id': 'shared-explanation', 'page_id': 'shared-feature',
                 'text': 'The push waits for review.', 'evidence_refs': [dict(SOURCE)],
                 'depends_on': {'facts': {}, 'actions': {'push-app': action['revision']}},
                 'state': 'current', 'previous_revision': None, 'compatibility': {}}
    narrative['revision'] = engine.entity_revision(narrative)
    registry['narratives']['shared-explanation'] = narrative
    engine.validate_registry(registry)

    current = pages.render_v3_page(APP_PAGE, None, registry)
    block = region(current, pages.NARRATIVE_START, pages.NARRATIVE_END)
    assert 'Narrative: shared-explanation. State: current.' in block
    assert '\nThe push waits for review.\n' in block and '| demo-source | archive-one | 1 |' in block
    assert current.index(pages.KB_END) < current.index(pages.NARRATIVE_START)

    record = copy.deepcopy(action)
    record['state'] = record['accepted_event']['transition'] = 'completed'
    record['previous_revision'] = action['revision']
    record['revision'] = action_revision(record)
    changed = engine.apply_action_intent(registry, {'action_id': 'push-app', 'record': record,
                                                    'expected_revision': action['revision']})
    stale = pages.render_v3_page(APP_PAGE, current, changed)

    block = region(stale, pages.NARRATIVE_START, pages.NARRATIVE_END)
    assert 'Narrative: shared-explanation. State: stale.' in block and 'State: current' not in block
    assert '\n> The push waits for review.\n' in block and '\nThe push waits for review.\n' not in block
    assert '| completed |' in region(stale, pages.KB_START, pages.KB_END)


def test_unadopted_legacy_header_prose_and_tail_bytes_are_kept_exactly():
    from kelmo.wiki import pages
    registry = adopted({APP_PAGE: PAGES[APP_PAGE]})
    existing = LEGACY_HEADER + LEGACY_BODY

    text = pages.render_v3_page(APP_PAGE, existing, registry)

    assert text.startswith(LEGACY_HEADER + '# Old title\r\n\r\nHand prose.  \r\n<!-- kb -->')
    assert text.endswith('<!-- /kb -->\r\nTail bytes.\t\r\n')
    assert 'wiki_contract' not in text and '- old row' not in text
    assert pages.render_v3_page(APP_PAGE, text, registry) == text


def test_explicitly_adopted_header_is_replaced_and_retained_as_evidence():
    from kelmo.wiki import pages
    settings = {APP_PAGE: dict(PAGES[APP_PAGE], adopt_header=True)}
    registry = adopted(settings, headers={APP_PAGE: LEGACY_HEADER})
    assert registry['pages'][APP_PAGE]['compatibility']['header'] == pages.header_record(LEGACY_HEADER)

    text = pages.render_v3_page(APP_PAGE, LEGACY_HEADER + LEGACY_BODY, registry)

    assert pages.leading_header(text) == pages.frontmatter(APP_PAGE, registry['pages'][APP_PAGE])
    assert text[len(pages.leading_header(text)):].startswith('# Old title\r\n\r\nHand prose.  \r\n')
    assert text.endswith('<!-- /kb -->\r\nTail bytes.\t\r\n')
    # A later publication that changes nothing on this page leaves its bytes as they are.
    assert pages.render_v3_page(APP_PAGE, text, registry) == text
    with pytest.raises(ValueError, match='header changed'):
        pages.render_v3_page(APP_PAGE, LEGACY_HEADER.replace('keep', 'edit') + LEGACY_BODY, registry)


@pytest.mark.parametrize('existing', [
    '# Page\n<!-- kb -->\n<!-- kb -->\n<!-- /kb -->\n',
    '# Page\ntext <!-- kb -->\n<!-- /kb -->\n',
    '# Page\n<!-- /kb -->\n<!-- kb -->\n',
    '# Page\n<!-- narrative -->\n<!-- /narrative -->\n<!-- kb -->\n<!-- /kb -->\n',
    '# Page\n<!-- narrative -->\n',
])
def test_ambiguous_markers_refuse_instead_of_rewriting(existing):
    from kelmo.wiki import pages
    registry = adopted({APP_PAGE: PAGES[APP_PAGE]})
    with pytest.raises(ValueError, match='marker|region'):
        pages.render_v3_page(APP_PAGE, existing, registry)


def test_narrative_without_kb_gets_kb_before_it_and_renders_again():
    from kelmo.wiki import pages
    registry = adopted({TOPIC_PAGE: PAGES[TOPIC_PAGE]})
    existing = '# Design review\n\nHand intro.\n\n<!-- narrative -->\n<!-- /narrative -->\nTail.\n'

    first = pages.render_v3_page(TOPIC_PAGE, existing, registry)
    second = pages.render_v3_page(TOPIC_PAGE, first, registry)

    for text in (first, second):
        assert text.index(pages.KB_END) < text.index(pages.NARRATIVE_START)
        assert text.startswith('# Design review\n\nHand intro.\n\n') and text.endswith('Tail.\n')
    assert pages.render_v3_page(TOPIC_PAGE, second, registry) == second


def test_narrative_text_no_registered_narrative_owns_is_refused_not_emptied():
    from kelmo.wiki import pages
    registry = adopted({TOPIC_PAGE: PAGES[TOPIC_PAGE]})
    for existing in ('# Design review\n\n<!-- narrative -->\nLegacy explanation.\n<!-- /narrative -->\n',
                     '# Design review\n\n<!-- kb -->\n<!-- /kb -->\n\n<!-- narrative -->\nLegacy.\n<!-- /narrative -->\n'):
        with pytest.raises(ValueError, match='narrative'):
            pages.render_v3_page(TOPIC_PAGE, existing, registry)


def test_page_repository_references_need_configured_enrollment():
    settings = {APP_PAGE: dict(PAGES[APP_PAGE], repositories=['repo-unknown'])}
    for repositories in (None, ('repo-app',)):
        with pytest.raises(ValueError, match='enrollment'):
            adopted(settings, repositories=repositories)
    assert adopted({APP_PAGE: dict(PAGES[APP_PAGE], repositories=['repo-app'])})


@pytest.mark.parametrize('name', ['aliases', 'tags', 'links', 'repositories'])
@pytest.mark.parametrize('value', ['review', ['review', 3], {'review': True}])
def test_page_lists_must_be_lists_of_strings_never_split_characters(name, value):
    with pytest.raises(ValueError, match=name + ' must be a list of strings'):
        adopted({TOPIC_PAGE: dict(PAGES[TOPIC_PAGE], **{name: value})})
    tupled = adopted({TOPIC_PAGE: dict(PAGES[TOPIC_PAGE], aliases=('design', 'review-notes'))})
    assert tupled['pages'][TOPIC_PAGE]['aliases'] == ['design', 'review-notes']





@pytest.mark.parametrize('settings', [
    {'person.md': dict(page_id='person', layout='legacy', title='Person', kind='topic', category='you')},
    {APP_PAGE: PAGES[APP_PAGE]},
], ids=['person-on-legacy-layout', 'protected-on-feature-page'])
def test_retired_mandatory_records_stay_on_their_page(settings):
    import registry_seed
    from kelmo.wiki import pages
    from test_editor_context import fact
    target = next(iter(settings))
    retired = fact('kept.rule', target, 'Never deploy on Friday; hotfixes are exempt.', status='retired')
    spec = {'facts': {'kept.rule': retired},
              'notes': {retired['note_id']: {'filename': 'source.md', 'ark_id': retired['ark_id']}}}
    binding = EvidenceRef(retired['ark_id'], 1, retired['note_id'], 'rule-source')
    registry = registry_seed.build(spec, authority_id=AUTHORITY, evidence_bindings={retired['note_id']: binding},
                                   pages_settings=settings,
                                   protected_fact_keys=() if target == 'person.md' else ('kept.rule',))
    assert registry['facts']['kept.rule']['evidence_resolution'] == 'exact'

    text = pages.render_v3_page(target, None, registry)

    assert f"- {retired['text']} [f:{retired['id']}] [source:rule-source] [status: retired]" in text








def test_published_pages_share_the_generation_identity_and_doctor_accepts_them(world):
    from kelmo import doctor
    from kelmo.wiki import pages
    ref = world.archive('I pushed the shared feature.')
    receipt = world.state.apply(world.update(ref), world.origins['requester'], 'push')

    for target in PAGES:
        content = world.service.knowledge_api.page(target, receipt.generation).content.decode('utf-8')
        assert 'publication_id' not in pages.leading_header(content)
    report = doctor.diagnose(world.root)
    assert report['checks']['wiki_consistency'] and report['checks']['wiki_committed']
    assert report['issues']['wiki_consistency'] == [] and report['issues']['wiki_uncommitted'] == []

    path = world.service.config.wiki / APP_PAGE
    path.write_bytes(path.read_bytes().replace(b'| completed |', b'| pending |'))
    drifted = doctor.diagnose(world.root)
    assert not drifted['checks']['wiki_consistency']


def test_review_action_publishes_through_stateapi_on_its_topic_page(world):
    ref = world.archive('I completed the design review.', key='review')
    receipt = world.state.apply(world.update(ref, 'review-one'), world.origins['requester'], 'review')
    page = world.service.knowledge_api.page(TOPIC_PAGE, receipt.generation).content
    assert b'Review [a:review-one]: completed. Subject: design-review; kind: review' in page
    assert world.action('review-one')['kind'] == 'review'
