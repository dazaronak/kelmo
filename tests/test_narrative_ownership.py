"""Narrative ownership, staleness and unmanaged bytes through the real editor and publisher.

The synthetic world and the replaying executor come from test_editor_scoping; nothing here
claims semantic model quality.
"""

import copy

import pytest

from test_editor_scoping import FEATURE, HUB, NOTES, REVIEW, UNMANAGED, Scripted, build_world, proposal


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


WHY = 'Exports run at night because the warehouse locks its tables at noon.'


@pytest.mark.parametrize('kind', ['pages', 'narratives'])
def test_existing_restatement_is_dropped_before_new_text_checks(kind):
    from kelmo.editor import ProposalError, normalize
    target = 'projects/app/notes.md'
    text = 'Repository bindings: a retained historical explanation.'
    registry = {'version': 3, 'facts': {}, 'actions': {},
                'pages': {target: {'page_id': 'app-notes', 'layout': 'topic', 'category': 'app', 'revision': 'a' * 64,
                                   'title': 'Notes', 'summary': text}},
                'narratives': {'app.memo': {'page_id': 'app-notes', 'state': 'current', 'text': text,
                                           'revision': 'b' * 64, 'depends_on': {'facts': {}, 'actions': {}}}}}
    change = (dict(target=target, expected_revision='a' * 64, summary=text) if kind == 'pages' else
              dict(narrative_id='app.memo', target=target, expected_revision='b' * 64, text=text,
                   depends_on={'facts': [], 'actions': []}))
    before = copy.deepcopy(registry)
    delta, checks = normalize({kind: [change]}, [target], registry, rule_change=False, project='app')
    assert delta == {'facts': [], 'narratives': [], 'pages': []} and checks == []
    assert registry == before
    field = 'summary' if kind == 'pages' else 'text'
    with pytest.raises(ProposalError):
        normalize({kind: [dict(change, **{field: text + ' Changed.'})]}, [target], registry,
                  rule_change=False, project='app')


@pytest.mark.parametrize('value', [[], {}])
def test_unhashable_narrative_dependency_is_a_proposal_error(value):
    from kelmo.editor import ProposalError, normalize
    target = 'projects/app/notes.md'
    registry = {'version': 3, 'facts': {}, 'actions': {}, 'narratives': {},
                'pages': {target: {'page_id': 'app-notes', 'layout': 'topic', 'category': 'app'}}}
    change = dict(narrative_id='app.memo', target=target, expected_revision=None,
                  text='A normal explanation.', depends_on={'facts': [value], 'actions': []})
    with pytest.raises(ProposalError):
        normalize({'narratives': [change]}, [target], registry, rule_change=False, project='app')


def narrative(text=WHY, *, key='app.export-why', target=FEATURE, facts=('app.export',), actions=(),
              expected_revision=None):
    return dict(narrative_id=key, target=target, text=text, expected_revision=expected_revision,
                depends_on={'facts': list(facts), 'actions': list(actions)})


def explain(world):
    source, ref = world.archive('We run exports at night: the warehouse locks its tables at noon.', key='why')
    receipt = world.edit(source, Scripted(proposal(narratives=[narrative()])), key='why')
    return source, ref, receipt


def review_state(world):
    from kelmo.context_binding import ActionBinding, ContextBindings
    from kelmo.state import ActionState
    contexts = ContextBindings(world.bindings, actions=(ActionBinding('review-one', 'topic', 'design-review', 'review'),),
                               repository_remotes={}, worktrees={}, feature_pages={}, producer_client_ids=())
    return ActionState(world.service, contexts)


def review_update(ref, transition, order, expected_revision=None):
    from kelmo.contracts import ActionUpdate
    return ActionUpdate(action_id='review-one', subject_kind='topic', subject_id='design-review', kind='review',
                        expected_revision=expected_revision, event_order=order, transition=transition,
                        evidence_ref=ref)


def crash(*args, **kwargs):
    raise RuntimeError('synthetic process death')


def test_prepared_editor_intent_publishes_before_a_later_action(world, monkeypatch):
    from kelmo.wiki import engine
    source, _ = world.archive('Exports now run hourly.', key='hourly')
    original = engine.publish_semantic
    monkeypatch.setattr(engine, 'publish_semantic', crash)
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                   key='hourly')
    [(prepared, kind)] = world.service.journal.prepared_intents()
    assert kind == 'semantic'
    monkeypatch.setattr(engine, 'publish_semantic', original)
    world.reopen()
    state = review_state(world)
    _, ref = world.archive('The design review is complete.', principal='requester', key='review-done')
    action = state.apply(review_update(ref, 'completed', 1), world.origins['requester'], 'review-done')
    assert world.service.journal.prepared_intents() == []
    edited = world.edit(source, Scripted(), key='hourly')
    assert edited.operation_id == prepared and edited.outcome == 'published'
    assert edited.generation.publication_id < action.generation.publication_id
    assert world.fact('app.export')['text'] == 'Exports run hourly.'
    assert world.registry()['actions']['review-one']['state'] == 'completed'


def test_prepared_action_publishes_before_a_later_editor_publication(world, monkeypatch):
    from kelmo.wiki import engine
    state = review_state(world)
    _, ref = world.archive('The design review is complete.', principal='requester', key='review-done')
    update = review_update(ref, 'completed', 1)
    original = engine.publish_action
    monkeypatch.setattr(engine, 'publish_action', crash)
    with pytest.raises(RuntimeError):
        state.apply(update, world.origins['requester'], 'review-done')
    [(prepared, kind)] = world.service.journal.prepared_intents()
    assert kind == 'apply'
    monkeypatch.setattr(engine, 'publish_action', original)
    world.reopen()
    source, _ = world.archive('The review closed, so nightly exports stay.', key='decision')
    text = 'The design review closed; nightly exports stay.'
    executor = Scripted(proposal(narratives=[narrative(text, key='review.decision', target=REVIEW, facts=(),
                                                       actions=('review-one',))]))
    edited = world.edit(source, executor, key='decision', selected=(REVIEW,))
    [shown] = executor.jobs[0].payload['pages'][0]['actions']
    assert shown['action_id'] == 'review-one' and shown['state'] == 'completed'
    assert edited.outcome == 'published'
    stored = world.registry()['narratives']['review.decision']
    assert stored['state'] == 'current'
    assert stored['depends_on']['actions'] == {'review-one': world.registry()['actions']['review-one']['revision']}
    replay = review_state(world).apply(update, world.origins['requester'], 'review-done')
    assert replay.operation_id == prepared and replay.generation.publication_id < edited.generation.publication_id


def test_narrative_carries_exact_evidence_and_dependencies(world):
    revision = world.fact('app.export')['revision']
    source, ref, receipt = explain(world)
    stored = world.registry()['narratives']['app.export-why']
    assert receipt.outcome == 'published' and receipt.evidence_refs == (ref,)
    assert stored['evidence_refs'] == [ref.to_dict()] and stored['page_id'] == 'app-feature'
    assert stored['depends_on'] == {'facts': {'app.export': revision}, 'actions': {}}
    assert stored['state'] == 'current' and stored['previous_revision'] is None
    page = world.page(FEATURE)
    assert 'Narrative: app.export-why. State: current.' in page and '\n' + WHY + '\n' in page
    assert ref.source_id in page
    assert world.outcome(source)['disposition'] == 'applied'


def test_stale_narrative_is_not_presented_as_current(world):
    explain(world)
    source, _ = world.archive('Exports now run hourly.', key='hourly')
    receipt = world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                         key='hourly')
    stored = world.registry()['narratives']['app.export-why']
    assert stored['state'] == 'stale'
    page = world.page(FEATURE)
    assert 'Narrative: app.export-why. State: stale.' in page and 'This explanation is stale' in page
    assert '> ' + WHY in page and '\n' + WHY + '\n' not in page
    committed = world.service.knowledge_api.page(FEATURE, receipt.generation).content.decode('utf-8')
    assert committed == page
    # Reconciliation is an explicit new narrative revision against the current dependency.
    fixed = 'Exports moved to hourly runs once the warehouse stopped locking at noon.'
    later, _ = world.archive('The warehouse no longer locks at noon, so exports run hourly.', key='reconcile')
    world.edit(later, Scripted(proposal(narratives=[narrative(fixed, expected_revision=stored['revision'])])),
               key='reconcile')
    current = world.registry()['narratives']['app.export-why']
    assert current['state'] == 'current' and current['depends_on']['facts'] == {
        'app.export': world.fact('app.export')['revision']}
    assert 'State: current.' in world.page(FEATURE) and '\n' + fixed + '\n' in world.page(FEATURE)


def test_restated_current_narrative_is_no_change(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    _, _, first = explain(world)
    stored = world.registry()['narratives']['app.export-why']
    restated = narrative(expected_revision=stored['revision'])
    text = 'Exports run at night because of the noon table lock.'
    source, ref = world.archive(text, key='restate')
    reader = world.service.knowledge
    full = views.editor_view(reader, reader.generation(), 'app', [FEATURE, NOTES], 60000, source_text=text,
                             evidence=ref)
    notes = next(block for block in full['payload']['pages'] if block['target'] == NOTES)
    # On partial context a pure restatement is an empty answer, refused instead of reported applied.
    with pytest.raises(KelmoError) as partial:
        world.edit(source, Scripted(proposal(narratives=[restated])), key='partial', selected=(FEATURE, NOTES),
                   budget=full['used_tokens'] - views._cost(notes))
    assert partial.value.code == 'E_BUDGET'
    receipt = world.edit(source, Scripted(proposal(narratives=[restated])), key='complete')
    assert receipt.outcome == 'unchanged' and world.outcome(source)['disposition'] == 'archive-only'
    assert world.registry()['narratives']['app.export-why'] == stored
    assert world.service.knowledge.generation() == first.generation


def test_action_change_marks_dependent_narrative_stale(world):
    state = review_state(world)
    _, pending_ref = world.archive('The design review is scheduled.', principal='requester', key='review-pending')
    pending = state.apply(review_update(pending_ref, 'pending', 1), world.origins['requester'], 'review-pending')
    source, _ = world.archive('Nightly exports stay until the design review closes.', key='decision')
    text = 'Nightly exports stay until the design review closes.'
    world.edit(source, Scripted(proposal(narratives=[narrative(text, key='review.decision', target=REVIEW,
                                                                facts=(), actions=('review-one',))])),
               key='decision', selected=(REVIEW,))
    assert world.registry()['narratives']['review.decision']['state'] == 'current'
    _, done_ref = world.archive('The design review is complete.', principal='requester', key='review-done')
    state.apply(review_update(done_ref, 'completed', 2, pending.new_revision), world.origins['requester'],
                'review-done')
    assert world.registry()['narratives']['review.decision']['state'] == 'stale'
    page = world.page(REVIEW)
    assert 'State: stale.' in page and '> ' + text in page and 'completed' in page


def test_unmanaged_prose_is_preserved(world):
    from kelmo.contracts import KnowledgeProposal
    before, hub = world.page(FEATURE), world.page(HUB)
    prefix = before.split('<!-- kb -->')[0]
    assert UNMANAGED in prefix
    _, _, receipt = explain(world)
    source, _ = world.archive('Exports now run hourly.', key='hourly')
    world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
               key='hourly')
    after = world.page(FEATURE)
    assert after.split('<!-- kb -->')[0] == prefix and after != before
    assert world.page(HUB) == hub
    prepared = KnowledgeProposal.from_dict(world.service.journal.action_intent(receipt.operation_id)['proposal'])
    assert all(change.content.decode('utf-8').startswith(prefix) for change in prepared.changes
               if change.path == FEATURE)


def test_semantic_proposal_cannot_carry_actions_or_hidden_dependencies(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('The review is done.', key='claims')
    before = world.registry()
    for key, delta in (('actions', dict(actions=[{'action_id': 'review-one', 'state': 'completed'}])),
                       ('hidden', dict(narratives=[narrative(facts=('app.archive',))])),
                       ('unknown', dict(narratives=[narrative(actions=('review-one',))]))):
        with pytest.raises(KelmoError) as error:
            world.edit(source, Scripted(proposal(**delta)), key=key)
        assert error.value.code == 'E_PROPOSAL'
        assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'


def test_fresh_revision_cannot_move_action_fields_through_any_semantic_path(world, monkeypatch):
    from kelmo.contracts import KnowledgeChange, KnowledgeProposal
    from kelmo.errors import KelmoError
    from kelmo.state import action_revision
    from kelmo.wiki import engine
    state = review_state(world)
    _, ref = world.archive('The design review is complete.', principal='requester', key='review-done')
    receipt = state.apply(review_update(ref, 'completed', 1), world.origins['requester'], 'review-done')
    before = world.registry()
    forged = copy.deepcopy(before)
    action = forged['actions']['review-one']
    action.update(state='pending', previous_revision=action['revision'])
    action['accepted_event']['transition'] = 'pending'
    action['revision'] = action_revision(action)
    with pytest.raises(KelmoError) as error:
        engine._commit_registry(world.root, forged, 'chore: semantic refresh', world.service.config.wiki)
    assert error.value.code == 'E_OWNERSHIP'
    page = world.service.knowledge_api.page(REVIEW, receipt.generation)
    change = KnowledgeChange(REVIEW, page.content_sha256, page.content.replace(b'completed', b'pending'), (ref,))
    with pytest.raises(KelmoError):
        world.service.knowledge_api.publish(KnowledgeProposal('reopen', receipt.generation, (change,)),
                                            receipt.generation)
    # A prepared editor intent is never an action publication authority.
    source, _ = world.archive('Exports run hourly.', key='hourly')
    monkeypatch.setattr(engine, 'publish_semantic', crash)
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                   key='hourly')
    [prepared] = world.service.journal.prepared_semantic()
    assert world.service.journal.prepared_actions() == []
    with pytest.raises(KelmoError) as error:
        engine.publish_action(world.root, prepared)
    assert error.value.code == 'E_OWNERSHIP'
    assert world.registry() == before
