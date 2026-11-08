import pytest

from kelmo.errors import KelmoError
from kelmo.executor import ModelOutcome
from tests.test_action_state import world_factory  # noqa: F401
from tests.test_editor_scoping import FEATURE, NOTES, OTHER, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_link_proposal_requires_evidence(world):
    bad_findings = [
        {'kind': 'link', 'pages': [NOTES, OTHER], 'sources': [], 'reason': 'No sources provided.'},
        {'kind': 'link', 'pages': [NOTES, OTHER],
         'sources': [{'fact_key': 'app.notes-scope', 'revision': 'nonexistent-rev'}],
         'reason': 'Fact revision is wrong.'},
        {'kind': 'link', 'pages': [NOTES, OTHER],
         'sources': [{'archive_id': 'nonexistent-archive', 'version': 1}],
         'reason': 'Evidence not shown.'},
    ]
    rejected_run = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': bad_findings})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(rejected_run['rejected']) == 3
    assert rejected_run['new'] == []
    assert rejected_run['published'] == {}
    assert 'app-archive' not in world.registry()['pages'][NOTES]['links']

    world.service.dreaming._set_state('cursor', None)
    cited_finding = {
        'kind': 'link',
        'pages': [NOTES, OTHER],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive are related.',
    }
    published_run = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [cited_finding]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(published_run['new']) == 1
    fp = published_run['new'][0]
    assert published_run['published'][fp]['state'] == 'published'
    assert 'app-archive' in world.registry()['pages'][NOTES]['links']


def test_stale_proposal_cannot_publish(world):
    finding = {
        'kind': 'link',
        'pages': [NOTES, OTHER],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive are related.',
    }
    packet = {
        'generation': world.service.knowledge.generation().to_dict(),
        'pages': [
            {'target': NOTES, 'revision': world.registry()['pages'][NOTES]['revision']},
            {'target': OTHER, 'revision': world.registry()['pages'][OTHER]['revision']},
        ],
    }
    fingerprint, is_new = world.service.dreaming._record(finding, packet)
    assert is_new is True
    assert world.service.dreaming._row(fingerprint)['state'] == 'open'

    source, _ = world.archive('Notes updated through editor.', key='notes-update')
    world.edit(
        source,
        Scripted(world.propose(facts=[world.change('app.notes-scope', 'Notes updated content.')])),
        selected=[NOTES],
    )

    result = world.service.dreaming.publish(fingerprint, origin=world.origins['editor'], contexts=world.contexts())
    assert result['state'] == 'stale'
    assert world.service.dreaming._row(fingerprint)['state'] == 'stale'
    assert 'app-archive' not in world.registry()['pages'][NOTES]['links']


def test_dreaming_cannot_close_work_by_age(world):
    before_registry = world.registry()
    before_fact_statuses = {key: fact['status'] for key, fact in before_registry['facts'].items()}
    before_actions = before_registry.get('actions', {})

    findings = [
        {'kind': 'close', 'pages': [NOTES, OTHER],
         'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
         'reason': 'Close old work due to age.'},
        {'kind': 'link', 'pages': [NOTES, OTHER],
         'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
         'reason': 'Status field injected.', 'status': 'close'},
    ]
    result = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': findings})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(result['rejected']) == 2
    assert result['new'] == []
    assert result['published'] == {}

    after_registry = world.registry()
    assert {key: fact['status'] for key, fact in after_registry['facts'].items()} == before_fact_statuses
    assert after_registry.get('actions', {}) == before_actions


@pytest.fixture
def state_world(world_factory):  # noqa: F811 - the imported fixture, requested by name
    return world_factory()


def test_dreaming_failure_does_not_block_state_updates(state_world):
    world = state_world
    world.archive('Evidence for a Dreaming pass.\n', key='dream-evidence')
    dreaming = world.service.dreaming
    cursor_before = dreaming._state('cursor')

    class FailingExecutor:
        def execute(self, job):
            raise KelmoError('E_EXECUTOR', 'boom')

    for executor in (Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='x')), FailingExecutor()):
        result = dreaming.run(executor, origin=world.origins['editor'])
        assert result['state'] == 'failed' and result['code'] == 'E_EXECUTOR'
        assert dreaming._state('cursor') == cursor_before
    assert world.service.journal.prepared_intents() == []

    ref = world.archive('I pushed the shared feature.\r\n')
    world.state.apply(world.update(ref), world.origins['requester'], 'complete-push')
    assert world.action()['state'] == 'completed'


def test_repeated_finding_is_reported_once(world):
    # A published link stops being a finding; an open review finding repeats and is counted, not re-reported.
    finding = {
        'kind': 'split',
        'pages': [NOTES],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'The notes page covers the export pipeline and unrelated archive notes.',
    }
    answer = Scripted(ModelOutcome('proposal', {'findings': [finding]}))
    run1 = world.service.dreaming.run(answer, origin=world.origins['editor'], contexts=world.contexts())
    assert len(run1['new']) == 1 and run1['published'] == {}
    fp = run1['new'][0]

    source2, _ = world.archive('Exports now run hourly.', key='hourly')
    world.edit(source2, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
               selected=[FEATURE])

    answer = Scripted(ModelOutcome('proposal', {'findings': [finding]}))
    run2 = world.service.dreaming.run(answer, origin=world.origins['editor'], contexts=world.contexts())
    assert run2['state'] == 'finished'
    assert run2['repeated'] == 1 and run2['new'] == [] and run2['published'] == {}
    row = world.service.dreaming._row(fp)
    assert row['seen'] == 2 and row['state'] == 'open'
    assert len(world.service.dreaming.findings()) == 1


def test_paused_dreaming_calls_no_model(world):
    world.service.dreaming.pause()
    executor = Scripted()
    result = world.service.dreaming.run(
        executor,
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert result == {'state': 'paused'}
    assert len(executor.jobs) == 0


def test_add_links_needs_a_dreaming_request(world):
    source, _ = world.archive('Attempting manual link addition.', key='manual-link')
    proposal = {
        'pages': [
            {'target': NOTES, 'expected_revision': world.registry()['pages'][NOTES]['revision'],
             'add_links': ['app-archive']},
        ],
    }
    with pytest.raises(KelmoError) as exc_info:
        world.edit(source, Scripted(ModelOutcome('proposal', proposal)), selected=[NOTES, OTHER])
    assert exc_info.value.code == 'E_PROPOSAL'
