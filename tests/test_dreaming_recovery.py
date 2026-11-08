import pytest

from kelmo.errors import KelmoError
from kelmo.executor import ModelOutcome
from tests.test_editor_scoping import HUB, NOTES, OTHER, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_evidence_larger_than_budget_skipped(world):
    executor = Scripted()
    result = world.service.dreaming.run(
        executor,
        origin=world.origins['editor'],
        contexts=world.contexts(),
        budget_tokens=10,
    )
    assert result['state'] == 'finished'
    assert result['skipped'] == [{'archive_id': world.seed.archive_id, 'version': world.seed.version}]
    assert result['new'] == []
    assert len(executor.jobs) == 0

    second = world.service.dreaming.run(
        executor,
        origin=world.origins['editor'],
        contexts=world.contexts(),
        budget_tokens=10,
    )
    assert second == {'state': 'idle'}


def test_link_publication_fails_once_stays_open_then_publishes(world, monkeypatch):
    finding = {
        'kind': 'link',
        'pages': [NOTES, OTHER],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive are related.',
    }
    real_edit = world.service.editor.edit
    calls = 0

    def failing_edit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KelmoError('E_BUSY', 'editor is busy')
        return real_edit(*args, **kwargs)

    monkeypatch.setattr(world.service.editor, 'edit', failing_edit)

    first_run = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [finding]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(first_run['new']) == 1
    fp = first_run['new'][0]
    assert first_run['published'][fp]['state'] == 'open'
    assert first_run['published'][fp]['code'] == 'E_BUSY'

    row_before = world.service.dreaming._row(fp)
    assert row_before['state'] == 'open'
    note_op_id = row_before['note_operation_id']
    assert note_op_id is not None

    pub_result = world.service.dreaming.publish(
        fp,
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert pub_result['state'] == 'published'
    row_after = world.service.dreaming._row(fp)
    assert row_after['state'] == 'published'
    assert row_after['note_operation_id'] == note_op_id
    assert 'app-archive' in world.registry()['pages'][NOTES]['links']
    assert calls == 2


def test_two_link_findings_same_first_page_both_published(world):
    finding1 = {
        'kind': 'link',
        'pages': [NOTES, OTHER],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive are related.',
    }
    finding2 = {
        'kind': 'link',
        'pages': [NOTES, HUB],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and hub are related.',
    }
    run_result = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [finding1, finding2]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(run_result['new']) == 2
    fp1, fp2 = run_result['new']
    assert run_result['published'][fp1]['state'] == 'published'
    assert run_result['published'][fp2]['state'] == 'published'

    assert world.service.dreaming._row(fp1)['state'] == 'published'
    assert world.service.dreaming._row(fp2)['state'] == 'published'

    links = world.registry()['pages'][NOTES]['links']
    assert 'app-archive' in links
    assert 'app' in links


def test_link_citing_forgotten_evidence_settles_stale(world, monkeypatch):
    finding = {
        'kind': 'link',
        'pages': [NOTES, OTHER],
        'sources': [{'archive_id': world.seed.archive_id, 'version': world.seed.version}],
        'reason': 'Seed evidence links notes and archive.',
    }
    real_edit = world.service.editor.edit
    calls = 0

    def failing_edit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KelmoError('E_BUSY', 'editor is busy')
        return real_edit(*args, **kwargs)

    monkeypatch.setattr(world.service.editor, 'edit', failing_edit)

    first_run = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [finding]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(first_run['new']) == 1
    fp = first_run['new'][0]
    assert first_run['published'][fp]['state'] == 'open'
    assert world.service.dreaming._row(fp)['state'] == 'open'

    world.service.forget(world.seed.archive_id)

    pub_result = world.service.dreaming.publish(
        fp,
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert pub_result['state'] == 'stale'
    row = world.service.dreaming._row(fp)
    assert row['state'] == 'stale'
    assert 'no longer readable' in row['detail']['reason']
    assert 'app-archive' not in world.registry()['pages'][NOTES]['links']


def test_merge_finding_pages_either_order_recorded_once(world):
    finding_order1 = {
        'kind': 'merge',
        'pages': [NOTES, OTHER],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive cover the same topic.',
    }
    run1 = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [finding_order1]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert len(run1['new']) == 1
    fp = run1['new'][0]
    assert run1['repeated'] == 0

    world.service.dreaming._set_state('cursor', None)
    finding_order2 = {
        'kind': 'merge',
        'pages': [OTHER, NOTES],
        'sources': [{'fact_key': 'app.notes-scope', 'revision': world.fact('app.notes-scope')['revision']}],
        'reason': 'Notes and archive cover the same topic.',
    }
    run2 = world.service.dreaming.run(
        Scripted(ModelOutcome('proposal', {'findings': [finding_order2]})),
        origin=world.origins['editor'],
        contexts=world.contexts(),
    )
    assert run2['state'] == 'finished'
    assert run2['new'] == []
    assert run2['repeated'] == 1
    assert run2['published'] == {}

    row = world.service.dreaming._row(fp)
    assert row['seen'] == 2
    assert row['state'] == 'open'
    assert len(world.service.dreaming.findings()) == 1


def test_publish_unknown_fingerprint_raises_not_found(world):
    with pytest.raises(KelmoError) as exc_info:
        world.service.dreaming.publish(
            'nonexistent-fingerprint-00000000',
            origin=world.origins['editor'],
            contexts=world.contexts(),
        )
    assert exc_info.value.code == 'E_NOT_FOUND'


def test_link_rejects_non_project_first_page():
    from kelmo.dreaming import Dreaming
    shown = {'topics/storage.md': {'page_id': 'topic-storage', 'links': []},
             'projects/app/notes.md': {'page_id': 'app-notes', 'links': []}}
    item = {'kind': 'link', 'pages': ['topics/storage.md', 'projects/app/notes.md'],
            'sources': [{'fact_key': 'app.notes-scope', 'revision': 'r1'}], 'reason': 'Same storage topic.'}
    assert 'project page' in Dreaming._problem(item, shown, {('app.notes-scope', 'r1')}, set())
    item['pages'].reverse()
    assert Dreaming._problem(item, shown, {('app.notes-scope', 'r1')}, set()) is None


def test_malformed_finding_values_are_rejected_not_raised():
    from kelmo.dreaming import Dreaming
    shown, facts, archived = {'projects/demo/a.md': {}, 'projects/demo/b.md': {}}, {('demo.fact', 'r1')}, {('arch', 1)}
    good = {'kind': 'link', 'pages': ['projects/demo/a.md', 'projects/demo/b.md'], 'sources': [{'fact_key': 'demo.fact', 'revision': 'r1'}],
            'reason': 'Both pages describe the same schedule.'}
    for change in ({'pages': [[], 'projects/demo/b.md']}, {'kind': ['link']},
                   {'sources': [{'fact_key': [], 'revision': 'r1'}]},
                   {'sources': [{'archive_id': {'x': 1}, 'version': 1}]}):
        assert isinstance(Dreaming._problem(dict(good, **change), shown, facts, archived), str)
