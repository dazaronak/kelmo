"""Plain-text creation uses the real archive and publisher with a deterministic executor."""
import pytest

from test_editor_scoping import FEATURE, HUB, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_plain_text_is_archived_then_published_with_fresh_reconciliation(world):
    from kelmo.executor import ModelOutcome
    text = 'Exports run nightly at 02:00. Keep the existing retention policy.'
    operation = world.service.create(text, project='app', selected=[FEATURE], idempotency_key='plain')
    assert world.fact('app.export')['text'] == 'Exports run nightly.'
    executor = Scripted(
        ModelOutcome('reconcile', {'facts': [{'text': 'Exports run nightly at 02:00.', 'target': FEATURE}]}),
        lambda job: world.propose(facts=[world.change('app.export', 'Exports run nightly at 02:00.')]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied' and result['terminal']
    assert world.service.source_packet(operation['id']).content == text.encode()
    assert world.fact('app.export')['text'] == 'Exports run nightly at 02:00.'
    assert len(executor.jobs) == 2
    assert world.service.create(text, project='app', selected=[FEATURE],
                                idempotency_key='plain')['id'] == operation['id']


def test_unconfigured_executor_keeps_source_durable_and_scope_bound(world, monkeypatch):
    from kelmo.errors import KelmoError
    operation = world.service.create('A decision awaiting editing.', project='app', idempotency_key='scope')
    request = world.service.journal.source_metadata(operation['id'])['metadata']['editor_request']
    assert request['selected'] == [HUB]
    result = world.service.run(operation['id'])
    assert result['state'] == 'archived' and not result['terminal']
    assert world.service.source_packet(operation['id']).project == 'app'
    with pytest.raises(KelmoError, match='different editor scope'):
        world.service.create('A decision awaiting editing.', project='app', selected=[FEATURE],
                             idempotency_key='scope')
    from kelmo.daemon import tick
    for index in range(19):
        world.service.create(f'Queued source {index}.', project='app', idempotency_key=f'queued-{index}')
    tick(world.service)
    later = world.service.capture('Later archive-only source.', 'later.md', 'implementer', 'archive')
    tick(world.service)
    assert world.service.journal.get(later['id'])['terminal']
    import sys
    import time
    with world.service.journal.conn:
        for operation_id in world.service.queued():
            world.service.journal.event(operation_id, 'editor_deferred', reset_at=time.time() + 60)
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('quota-deferred executor called')))
    later = world.service.capture('Archive during quota wait.', 'quota-wait.md', 'implementer', 'archive')
    tick(world.service, argv=[sys.executable])
    assert world.service.journal.get(later['id'])['terminal']


def test_a_stop_between_model_rounds_lets_go_and_the_next_start_publishes(world):
    from kelmo.errors import Stopping
    from kelmo.executor import ModelOutcome
    operation = world.service.create('Exports now run at 04:00.', project='app', selected=[FEATURE])
    reconcile = ModelOutcome('reconcile', {'facts': [{'text': 'Exports run at 04:00.', 'target': FEATURE}]})

    def stop(job):
        raise Stopping()

    with pytest.raises(Stopping):
        world.service.run(operation['id'], executor=Scripted(reconcile, stop))
    assert not world.service.journal.get(operation['id'])['terminal']
    assert world.service.journal.conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 0
    executor = Scripted(reconcile, lambda job: world.propose(
        facts=[world.change('app.export', 'Exports run at 04:00.')]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert world.fact('app.export')['text'] == 'Exports run at 04:00.'


def test_restart_after_publication_replays_without_another_model_call(world, monkeypatch):
    operation = world.service.create('Exports now run at 03:00.', project='app', selected=[FEATURE])
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 03:00.')]))
    original = world.service.journal.stage

    def interrupted(operation_id, stage, **kwargs):
        if stage == 'applied':
            raise RuntimeError('Synthetic crash after publication')
        return original(operation_id, stage, **kwargs)

    monkeypatch.setattr(world.service.journal, 'stage', interrupted)
    with pytest.raises(RuntimeError, match='Synthetic crash'):
        world.service.run(operation['id'], executor=executor)
    monkeypatch.setattr(world.service.journal, 'stage', original)
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert len(executor.jobs) == 1


def test_shared_create_passes_trusted_page_enrollment_to_the_worker(world):
    from kelmo.server import ClientSession, ServerProfile, shared_service
    from test_editor_scoping import DETAIL, DETAIL_FACT, detail_page
    profile = ServerProfile(world.bindings, world.contexts(), {}, {}, False, 'in-process')
    with ClientSession(world.root, profile, 'editor') as session:
        operation = session.service.create('Failed exports retry three times.', project='app',
                                           selected=[FEATURE], idempotency_key='new-detail')
    executor = Scripted(lambda job: world.propose(pages=[detail_page()], facts=[DETAIL_FACT]))
    with shared_service(world.root, profile) as worker:
        result = worker.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert world.registry()['pages'][DETAIL]['parent'] == FEATURE
    assert world.fact(DETAIL_FACT['fact_key'])['text'] == DETAIL_FACT['text']


@pytest.mark.asyncio
async def test_cli_and_mcp_share_the_same_creation_identity(world, capsys):
    import json
    from fastmcp import Client
    from kelmo.cli import call
    from kelmo.mcp import server
    args = {'content': 'An unchanged original note.', 'project': 'app',
            'targets': [FEATURE], 'idempotency_key': 'interfaces'}
    async with Client(server(world.root)) as client:
        created = (await client.call_tool('memory_create', args)).data['operation']
    assert call(['create', '--vault', str(world.root), '--text', args['content'], '--project', 'app',
                 '--target', FEATURE, '--idempotency-key', 'interfaces', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['result']['operation']['id'] == created['id']


def test_create_without_a_budget_uses_the_vault_editor_setting(world):
    """Clients and the MCP tool name no budget; the server resolves [editor] budget_tokens or the default."""
    from kelmo.config import DEFAULT_EDITOR_BUDGET, Config
    from kelmo.errors import KelmoError
    from kelmo.server import ClientSession, ServerProfile

    def budget_of(operation):
        return world.service.journal.source_metadata(operation['id'])['metadata']['editor_request']['budget_tokens']

    assert budget_of(world.service.create('Default budget.', project='app', idempotency_key='budget-default')) \
        == DEFAULT_EDITOR_BUDGET
    toml = world.root / 'kelmo.toml'
    original = toml.read_text(encoding='utf-8')
    toml.write_text(original + '\n[editor]\nbudget_tokens = 180000\n', encoding='utf-8')
    world.service.config = Config.load(world.root)
    assert world.service.config.as_dict()['editor'] == {'budget_tokens': 180000, 'mode': 'immediate',
                                                         'batch_apply': False, 'batch_max_sources': 8}
    assert budget_of(world.service.create('Configured budget.', project='app', idempotency_key='budget-vault')) \
        == 180000
    assert budget_of(world.service.create('Explicit budget.', project='app', idempotency_key='budget-explicit',
                                          budget_tokens=4096)) == 4096
    profile = ServerProfile(world.bindings, world.contexts(), {}, {}, False, 'in-process')
    with ClientSession(world.root, profile, 'editor') as session:
        shared = session.service.create('Shared client names no budget.', project='app',
                                        idempotency_key='budget-shared')
    assert budget_of(shared) == 180000
    toml.write_text(original + '\n[editor]\nbudget_tokens = 0\n', encoding='utf-8')
    with pytest.raises(KelmoError, match=r'\[editor\].*positive integer'):
        Config.load(world.root)
    toml.write_text(original + '\n[editor]\nmodel = "x"\n', encoding='utf-8')
    with pytest.raises(KelmoError, match=r'\[editor\].*not accepted'):
        Config.load(world.root)
    toml.write_text(original, encoding='utf-8')


WORK = 'projects/app/export-rework.md'


def test_new_work_opens_its_page_with_the_open_work_that_lands_on_it(world):
    operation = world.service.create('Next: rework the export.', project='app',
                                     new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                     idempotency_key='new-work-retry')
    fact = dict(fact_key='app.export-rework', target=WORK, text='Rework the export pipeline.',
                status='backlog', expected_revision=None)
    # The model's own entry for the opened page is replaced by the requested one.
    stray = dict(target=WORK, expected_revision=None, page_id='stray', layout='topic', title='Stray',
                 summary='Stray.', parent=HUB)
    seen = []
    executor = Scripted(lambda job: seen.append(job.payload.get('new_work'))
                        or world.propose(facts=[fact], pages=[stray]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert seen == [{'target': WORK, 'title': 'Rework the export'}]
    page = world.registry()['pages'][WORK]
    assert (page['kind'], page['layout'], page['page_id'], page['parent'], page['title'], page['summary']) == (
        'work', 'feature', 'projects-app-export-rework', HUB, 'Rework the export', 'Rework the export')
    assert world.fact('app.export-rework')['target'] == WORK


    replayed = world.service.create('Next: rework the export.', project='app',
                                     new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                     idempotency_key='new-work-retry')
    assert replayed['id'] == operation['id'] and replayed['state'] == 'applied'
    assert len(executor.jobs) == 1
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match='different input'):
        world.service.create('Changed source.', project='app',
                             new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                             idempotency_key='new-work-retry')
    with pytest.raises(KelmoError):
        world.service.create('Next: rework the export.', project='app',
                             new_work={'slug': 'export-rework', 'title': 'Different work'},
                             idempotency_key='new-work-retry')


def test_new_work_without_a_fact_on_it_creates_no_page(world):
    operation = world.service.create('Exports moved to 01:00.', project='app', selected=[FEATURE],
                                     new_work={'slug': 'export-rework', 'title': 'Rework the export'})
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 01:00.')]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert WORK not in world.registry()['pages']


@pytest.mark.parametrize('work, message', [
    ({'slug': 'feature', 'title': 'Feature'}, 'already registered'),
    ({'slug': 'export-rework'}, 'slug and a title'),
    ({'slug': 'Export_Rework', 'title': 'Rework'}, 'lowercase letters'),
    ({'slug': 'export-rework', 'title': 'x' * 121}, 'title is one plain line'),
    ({'slug': 'export-rework', 'title': 'Rework', 'summary': 'two\nlines'}, 'summary is one plain line'),
])
def test_a_new_work_request_is_checked_before_capture(world, work, message):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match=message):
        world.service.create('Open work.', project='app', new_work=work)


def test_new_work_never_claims_an_existing_unregistered_file(world):
    from kelmo.errors import KelmoError
    (world.service.config.wiki / 'projects/app/legacy-notes.md').write_text('Hand-written notes.\n')
    with pytest.raises(KelmoError, match='never claims it'):
        world.service.create('Open work.', project='app', new_work={'slug': 'legacy-notes', 'title': 'Legacy'})


def test_page_arguments_need_new_work():
    from types import SimpleNamespace
    from kelmo.creation import requested_work
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match='--new-work'):
        requested_work(SimpleNamespace(new_work=None, title='Orphan title', summary=None))
    with pytest.raises(KelmoError, match='--title'):
        requested_work(SimpleNamespace(new_work='export-rework', title=None, summary=None))
    assert requested_work(SimpleNamespace(new_work='export-rework', title='Rework', summary=None)) == {
        'slug': 'export-rework', 'title': 'Rework'}


def long_summary(world):
    revision = world.registry()['pages'][FEATURE]['revision']
    return world.propose(pages=[dict(target=FEATURE, expected_revision=revision, summary='x' * 201)])


def test_a_refused_proposal_is_told_why_and_its_fix_publishes(world):
    operation = world.service.create('Exports moved to 05:00.', project='app', selected=[FEATURE])
    executor = Scripted(lambda job: long_summary(world),
                        lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 05:00.')]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert 'refusal' not in executor.jobs[0].payload
    refusal = executor.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and '200' in refusal['reason']
    assert world.fact('app.export')['text'] == 'Exports run at 05:00.'


def test_a_wrong_receipt_is_repaired_by_the_next_answer(world):
    from kelmo.executor import ModelOutcome
    operation = world.service.create('Exports moved to 06:00.', project='app', selected=[FEATURE])
    change = world.change('app.export', 'Exports run at 06:00.')
    claimed_new = ModelOutcome('proposal', {'facts': [change], 'fact_checks': [
        {'fact_key': 'app.export', 'verified_match': {'result': 'not_found', 'search_id': 'f' * 32}}]})
    executor = Scripted(claimed_new, lambda job: world.propose(facts=[change]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert executor.jobs[1].payload['refusal']['code'] == 'E_FACT_CHECK'


def test_repairs_stop_after_two_and_the_source_is_parked_for_the_next_pass(world):
    """A refusal is never final on its own: the source parks, the next apply pass consults the model again
    with the refusal as its hint, and only the third refused attempt fails the source."""
    from kelmo.creation import PARK_ATTEMPTS, apply_pending, pending_operations
    operation = world.service.create('Exports moved again.', project='app', selected=[FEATURE])
    executor = Scripted(*[lambda job: long_summary(world)] * 3)
    result = world.service.run(operation['id'], executor=executor)
    assert (result['state'], result['error_code'], result['terminal']) == ('parked', 'E_PROPOSAL', False)
    assert len(executor.jobs) == 3
    assert world.outcome(operation['id'])['disposition'] == 'unresolved'
    assert [op['id'] for op, _ in pending_operations(world.service)] == [operation['id']]
    second = Scripted(*[lambda job: long_summary(world)] * 3)
    report = apply_pending(world.service, second)
    assert report['parked'] == [operation['id']] and len(second.jobs) == 3
    assert second.jobs[0].payload['refusal']['code'] == 'E_PROPOSAL', 'the hint reaches the first round'
    third = Scripted(*[lambda job: long_summary(world)] * 3)
    report = apply_pending(world.service, third)
    assert report['failed'] == [operation['id']] and PARK_ATTEMPTS == 3
    final = world.service.journal.get(operation['id'])
    assert (final['state'], final['error_code'], final['terminal']) == ('failed', 'E_PROPOSAL', True)
    assert pending_operations(world.service) == []


def test_a_parked_source_publishes_once_the_model_gets_it_right(world):
    from kelmo.creation import apply_pending
    operation = world.service.create('Exports moved to 07:00.', project='app', selected=[FEATURE])
    assert world.service.run(operation['id'], executor=Scripted(*[lambda job: long_summary(world)] * 3))['state'] \
        == 'parked'
    fixed = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 07:00.')]))
    assert apply_pending(world.service, fixed)['applied'] == [operation['id']]
    assert world.service.journal.get(operation['id'])['state'] == 'applied'
    assert world.fact('app.export')['text'] == 'Exports run at 07:00.'


@pytest.mark.parametrize('spelling', ['projects/app/feature', 'projects/app/feature.md', 'app-feature',
                                      '/projects/app/feature'])
def test_a_target_is_accepted_in_every_spelling_the_read_tools_hand_out(world, spelling):
    operation = world.service.create('A spelled target.', project='app', selected=[spelling],
                                     idempotency_key='spelling-' + spelling.replace('/', '_'))
    request = world.service.journal.source_metadata(operation['id'])['metadata']['editor_request']
    assert request['selected'] == [FEATURE]


def test_an_unknown_target_names_the_nearest_registered_pages(world):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match='nearest registered: projects/app/feature.md'):
        world.service.create('Typo.', project='app', selected=['projects/app/featur'],
                             idempotency_key='typo')


def test_a_project_without_any_map_or_index_is_refused_with_the_repair(world):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match='No project map is registered for notes'):
        world.service.create('No map.', project='notes', idempotency_key='no-map')


def test_a_top_level_project_without_a_map_enters_through_its_own_index(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from kelmo import creation
    from kelmo.errors import KelmoError
    entry = {'page_id': 'life-life', 'layout': 'legacy', 'kind': 'work', 'aliases': []}
    registry = {'version': 3, 'pages': {'life/life.md': entry, 'life/health.md': dict(entry, page_id='life-health')},
                'redirects': {}}
    monkeypatch.setattr(creation.engine, 'load_registry', lambda root: registry)
    service = SimpleNamespace(root='/unused')
    assert creation.request(service, 'life', [], False, 1000)['selected'] == ['life/life.md']
    assert creation.request(service, 'life', ['life-health'], False, 1000)['selected'] == ['life/health.md']
    assert creation.request(service, 'life', ['life/life'], False, 1000)['selected'] == ['life/life.md']
    # A new work page hangs from the index page when the project has no map, under the project's own
    # directory, and the editor's allocation moves past a taken slug.
    full = {**registry, 'narratives': {}, 'actions': {}}
    opened = creation.new_work_page(SimpleNamespace(config=SimpleNamespace(wiki=Path('/unused'))), full, 'life',
                                    {'slug': 'new-thing', 'title': 'New thing'})
    assert opened == {'target': 'life/new-thing.md', 'page_id': 'life-new-thing', 'title': 'New thing',
                      'summary': 'New thing', 'parent': 'life/life.md'}
    with pytest.raises(KelmoError, match='already registered'):
        creation.new_work_page(SimpleNamespace(config=SimpleNamespace(wiki=Path('/unused'))), full, 'life',
                               {'slug': 'health', 'title': 'Health'})
    allocated = creation.new_work_page(SimpleNamespace(config=SimpleNamespace(wiki=Path('/unused'))), full, 'life',
                                       {'slug': 'health', 'title': 'Health'}, allocate=True)
    assert (allocated['target'], allocated['page_id']) == ('life/health-2.md', 'life-health-2')
    with pytest.raises(KelmoError, match='No project map or index page'):
        creation.new_work_page(SimpleNamespace(config=SimpleNamespace(wiki=Path('/unused'))),
                               {**full, 'pages': {}}, 'life', {'slug': 'new-thing', 'title': 'New thing'})
    assert creation.slug_of('Agent: time sink (xhigh reasoning)!') == 'agent-time-sink-xhigh-reasoning'
    assert creation.slug_of('--') is None


def test_requested_work_takes_priority_from_the_flag_or_the_note_frontmatter():
    from types import SimpleNamespace
    from kelmo.creation import requested_work
    from kelmo.errors import KelmoError
    args = SimpleNamespace(new_work='export-rework', title='Rework', summary=None, priority='H2')
    assert requested_work(args)['priority'] == 'H2'
    note = '---\ndate: 2026-11-07\nproject: app\npriority: H3\n---\n\nIn progress: rework the export.\n'
    plain = SimpleNamespace(new_work='export-rework', title='Rework', summary=None, priority=None)
    assert requested_work(plain, note)['priority'] == 'H3'
    assert requested_work(args, note)['priority'] == 'H2'  # the flag wins over the frontmatter
    assert 'priority' not in requested_work(plain, 'No frontmatter here.\n')
    assert 'priority' not in requested_work(plain, '---\npriority: urgent\n---\n\nText.\n')
    with pytest.raises(KelmoError, match='--new-work'):
        requested_work(SimpleNamespace(new_work=None, title=None, summary=None, priority='H1'))


def test_an_oversized_applied_reason_refuses_the_note_before_the_commit(world, monkeypatch):
    """The outcome of a publication is decided before the commit.

    A reason the outcome store refuses must not stop the queue: the note ends refused with nothing
    committed, its facts never reach the wiki, and the next note publishes on its turn."""
    from kelmo import editor, outcomes
    original = editor._summary
    monkeypatch.setattr(editor, '_summary', lambda delta: 'outcome reason ' + 'x' * outcomes.REASON_LIMIT)
    first = world.service.create('Exports moved to 08:00.', project='app', selected=[FEATURE])
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 08:00.')]))
    result = world.service.run(first['id'], executor=executor)
    assert (result['state'], result['error_code'], result['terminal']) == ('parked', 'E_OUTCOME', False)
    assert world.fact('app.export')['text'] == 'Exports run nightly.', 'nothing was committed'
    assert world.service.journal.prepared_semantic() == [], 'no intent was prepared'
    assert world.outcome(first['id'])['reason'].startswith('E_OUTCOME: ')
    monkeypatch.setattr(editor, '_summary', original)
    second = world.service.create('Exports moved to 09:00.', project='app', selected=[FEATURE])
    later = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 09:00.')]))
    assert world.service.run(second['id'], executor=later)['state'] == 'applied'
    assert world.fact('app.export')['text'] == 'Exports run at 09:00.', 'the next note went on'


def test_an_intent_committed_before_its_receipt_finishes_without_a_model_call(world, monkeypatch):
    """A prepared intent whose wiki commit already happened settles through recovery, not a new request."""
    operation = world.service.create('Exports now run at 11:00.', project='app', selected=[FEATURE])
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 11:00.')]))
    original = world.service.journal.action_finish

    def interrupted(*args, **kwargs):
        raise RuntimeError('Synthetic crash after the commit and before the receipt')

    monkeypatch.setattr(world.service.journal, 'action_finish', interrupted)
    with pytest.raises(RuntimeError, match='Synthetic crash'):
        world.service.run(operation['id'], executor=executor)
    monkeypatch.setattr(world.service.journal, 'action_finish', original)
    assert world.fact('app.export')['text'] == 'Exports run at 11:00.', 'the commit already happened'
    assert world.service.journal.prepared_semantic(), 'its receipt is the part still missing'
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert len(executor.jobs) == 1, 'recovery finished it with no second consultation'
    assert world.outcome(operation['id'])['disposition'] == 'applied'


def test_new_work_priority_is_kept_on_the_page_and_an_invalid_one_is_refused_at_capture(world):
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError, match='H1, H2 or H3'):
        world.service.create('Open work.', project='app',
                             new_work={'slug': 'export-rework', 'title': 'Rework', 'priority': 'P1'})
    operation = world.service.create('Next: rework the export.', project='app',
                                     new_work={'slug': 'export-rework', 'title': 'Rework the export',
                                               'priority': 'H1'})
    fact = dict(fact_key='app.export-rework', target=WORK, text='Rework the export pipeline.',
                status='backlog', expected_revision=None)
    executor = Scripted(lambda job: world.propose(facts=[fact]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert world.registry()['pages'][WORK]['priority'] == 'H1'
