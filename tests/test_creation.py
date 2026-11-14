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


def test_a_worker_tick_stops_at_a_vault_that_takes_no_publication_and_tells_the_owner(world, monkeypatch):
    """In immediate mode the worker publishes each note as it comes. A failure under which no note can be
    published refuses none of them: the tick tries no further note, names the one it met, and alerts once."""
    from kelmo.alerts import VaultWatch
    from kelmo.daemon import tick
    from kelmo.errors import VaultFailure
    ids = [world.service.create(f'Exports move to 0{index}:00.', project='app', selected=[FEATURE])['id']
           for index in range(2)]
    tried, sent = [], []

    def blocked(operation_id, executor=None):
        tried.append(operation_id)
        raise VaultFailure('E_CONFLICT', 'Knowledge paths have uncommitted local changes: feature.md')

    monkeypatch.setattr(world.service, 'run', blocked)
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    result = tick(world.service)
    assert tried == ids[:1], 'the tick tried no note after the failure'
    assert result['skipped'] == [{'operation_id': ids[0], 'error_code': 'E_CONFLICT'}], result
    assert len(sent) == 1 and 'no note can be published' in sent[0], sent
    for operation_id in ids:
        assert not world.service.journal.get(operation_id)['terminal']


@pytest.mark.parametrize('stops', [True, False], ids=['then-a-vault-failure', 'and-nothing-stops'])
def test_a_worker_tick_closes_an_outage_only_when_nothing_stopped_it(world, monkeypatch, stops):
    """An outage is on record. The tick's first note is refused for its own reason after the vault was read
    taking a publication. If the next note then meets a vault failure, the outage is not announced over;
    if nothing stops the tick, it is."""
    from kelmo.alerts import VaultWatch
    from kelmo.daemon import tick
    from kelmo.errors import VaultFailure
    ids = [world.service.create(f'Exports move to 0{index}:00.', project='app', selected=[FEATURE])['id']
           for index in range(2)]
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_IO', 'the old outage')

    def run(operation_id, executor=None):
        if operation_id == ids[0]:
            world.service.editor.vault_passed = True
            return {'state': 'parked'}
        if stops:
            raise VaultFailure('E_IO', 'the wiki repository could not be written')
        return {'state': 'parked'}

    monkeypatch.setattr(world.service, 'run', run)
    tick(world.service)
    if stops:
        assert not [text for text in sent if 'takes publications again' in text], sent
        assert VaultWatch.for_service(world.service).marker.exists()
    else:
        assert len(sent) == 2 and 'takes publications again' in sent[1], sent


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
                                                         'batch_apply': False, 'batch_max_sources': 8,
                                                         'batch_ready_sources': 8}
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


def test_two_notes_with_the_same_new_work_publish_onto_the_page_the_first_one_opened(world):
    """A work page that already exists is used, not created again.

    Both requests were captured before either note published, so each carries the same `new_work`.
    The second note must find the page the first one opened, edit it, and keep its own evidence."""
    from kelmo.contracts import EvidenceRef
    first = world.service.create('First: rework the export queue.', project='app',
                                 new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                 idempotency_key='same-work-first')
    second = world.service.create('Second: the queue needs a retry cap.', project='app',
                                  new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                  idempotency_key='same-work-second')
    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export queue.',
                 status='backlog', expected_revision=None)
    opened = Scripted(lambda job: world.propose(facts=[queue]))
    assert world.service.run(first['id'], executor=opened)['state'] == 'applied'
    assert world.registry()['pages'][WORK]['page_id'] == 'projects-app-export-rework'

    cap = dict(fact_key='app.export-rework-cap', target=WORK, text='The queue caps retries at three.',
               status='backlog', expected_revision=None)
    seen = []
    later = Scripted(lambda job: seen.append(job.payload) or world.propose(facts=[cap]))
    result = world.service.run(second['id'], executor=later)
    assert result['state'] == 'applied', result.get('outcome')
    assert 'new_work' not in seen[0], 'the page is sent, not opened again'
    assert WORK in [page['target'] for page in seen[0]['pages']], 'the model reads the page it must edit'
    assert seen[0]['omitted'] == []
    assert world.registry()['pages'][WORK]['page_id'] == 'projects-app-export-rework', 'one page, not a second one'
    def evidence(operation_id):
        return EvidenceRef.from_dict(world.service.journal.evidence_record(operation_id)).content_sha256

    assert world.fact('app.export-rework')['note_id'] == evidence(first['id'])
    assert world.fact('app.export-rework-cap')['note_id'] == evidence(second['id'])


def test_a_work_page_that_appears_while_an_attempt_is_open_does_not_meet_the_idempotency_guard(world):
    """An attempt left open under one key still applies after its page appeared.

    The retry carries the same captured request; only the registry moved. Its fingerprint must name the
    request it answers, not the page the editor now finds already there."""
    from kelmo.errors import Stopping
    from kelmo.executor import ModelOutcome
    first = world.service.create('First: rework the export queue.', project='app',
                                 new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                 idempotency_key='same-work-first')
    second = world.service.create('Second: the queue needs a retry cap.', project='app',
                                  new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                  idempotency_key='same-work-second')
    reconcile = ModelOutcome('reconcile', {'facts': [{'text': 'Rework the export queue.', 'target': WORK}]})

    def stop(job):
        raise Stopping()

    with pytest.raises(Stopping):
        world.service.run(first['id'], executor=Scripted(reconcile, stop))
    assert not world.service.journal.get(first['id'])['terminal']

    cap = dict(fact_key='app.export-rework-cap', target=WORK, text='The queue caps retries at three.',
               status='backlog', expected_revision=None)
    assert world.service.run(second['id'], executor=Scripted(
        lambda job: world.propose(facts=[cap])))['state'] == 'applied'
    assert WORK in world.registry()['pages']

    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export queue.',
                 status='backlog', expected_revision=None)
    result = world.service.run(first['id'], executor=Scripted(reconcile, lambda job: world.propose(facts=[queue])))
    assert result['state'] == 'applied', result.get('outcome')
    assert world.fact('app.export-rework')['text'] == 'Rework the export queue.'


def test_a_retried_note_whose_work_page_another_note_opened_loses_no_attempt(world, monkeypatch):
    """A note retried under its own key after another note opened its page spends no attempt on that page.

    The crashed attempt recorded its answer with the work page the request opened. A second note opened
    that page in between, so the retry must not inject the creation again: `normalize` refuses a target
    the registry already holds, and a note that met that refusal would pay a charged attempt of its own
    budget for a page the vault itself opened. What the retry meets instead is the generation moving,
    which is one of the refusals the vault causes and costs nothing."""
    from kelmo import creation
    from kelmo.editor import Editor

    def crash(self, *args, **kwargs):
        raise RuntimeError('the process died after the answer')

    work = {'slug': 'export-rework', 'title': 'Rework the export'}
    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export queue.',
                 status='backlog', expected_revision=None)
    first = world.service.create('First: rework the export queue.', project='app', new_work=dict(work),
                                 idempotency_key='late-page-first')
    real = Editor._settle
    monkeypatch.setattr(Editor, '_settle', crash)
    with pytest.raises(RuntimeError):
        world.service.run(first['id'], executor=Scripted(lambda job: world.propose(facts=[queue])))
    monkeypatch.setattr(Editor, '_settle', real)
    assert not world.service.journal.get(first['id'])['terminal']

    cap = dict(fact_key='app.export-rework-cap', target=WORK, text='The queue caps retries at three.',
               status='backlog', expected_revision=None)
    second = world.service.create('Second: the queue needs a retry cap.', project='app', new_work=dict(work),
                                  idempotency_key='late-page-second')
    assert world.service.run(second['id'], executor=Scripted(
        lambda job: world.propose(facts=[cap])))['state'] == 'applied'
    assert WORK in world.registry()['pages'], 'the second note opened the page'

    retried = world.service.run(first['id'], executor=Scripted(lambda job: world.propose(facts=[queue])))
    reason = (world.outcome(first['id']) or {}).get('reason', '')
    assert 'already registered' not in reason, reason
    assert retried['state'] == 'parked', retried
    assert creation._parked(world.service.trace(first['id']))[1] == 0, reason
    applied = world.service.run(first['id'], executor=Scripted(lambda job: world.propose(facts=[queue])))
    assert applied['state'] == 'applied', world.outcome(first['id'])
    assert world.fact('app.export-rework')['text'] == 'Rework the export queue.'


def test_a_note_without_a_priority_edits_the_page_a_note_with_one_opened(world):
    """A priority is an attribute of the page, not part of which page it is."""
    first = world.service.create('First: rework the export queue.', project='app',
                                 new_work={'slug': 'export-rework', 'title': 'Rework the export', 'priority': 'H2'},
                                 idempotency_key='priority-first')
    second = world.service.create('Second: the queue needs a retry cap.', project='app',
                                  new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                  idempotency_key='priority-second')
    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export queue.',
                 status='backlog', expected_revision=None)
    assert world.service.run(first['id'], executor=Scripted(
        lambda job: world.propose(facts=[queue])))['state'] == 'applied'
    cap = dict(fact_key='app.export-rework-cap', target=WORK, text='The queue caps retries at three.',
               status='backlog', expected_revision=None)
    result = world.service.run(second['id'], executor=Scripted(lambda job: world.propose(facts=[cap])))
    assert result['state'] == 'applied', result.get('outcome')
    assert world.registry()['pages'][WORK]['priority'] == 'H2', 'the page keeps the priority it was opened with'


def test_a_request_row_written_under_the_earlier_fingerprint_still_answers_itself(world, monkeypatch):
    """A row a previous release wrote carries the whole `new_work` in its fingerprint: an upgrade must not
    turn it into E_IDEMPOTENCY, which would overwrite the outcome of a note that already published."""
    from kelmo.creation import _attempt
    from kelmo.editor import _fingerprint
    from kelmo.publication import StateCoordinator
    service = world.service
    operation = service.create('Open the export rework.', project='app',
                               new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                               idempotency_key='earlier-fingerprint')
    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export queue.',
                 status='backlog', expected_revision=None)
    executor = Scripted(lambda job: world.propose(facts=[queue]))
    called, real = {}, service.editor.edit

    def recording(source, **arguments):
        called.update(arguments)
        return real(source, **arguments)

    monkeypatch.setattr(service.editor, 'edit', recording)
    assert service.run(operation['id'], executor=executor)['state'] == 'applied'
    key = _attempt(service, operation['id'])[0]
    origin = service._operation_origin(operation['id'])
    earlier = _fingerprint({'request': {'source_operation_id': operation['id'], 'project': called['project'],
                                        'selected': list(called['selected']), 'budget_tokens': called['budget_tokens'],
                                        'rule_change': called['rule_change'], 'new_work': dict(called['new_work'])},
                            'origin': origin.to_dict(), 'key': key})
    with service.journal.conn:
        service.journal.conn.execute('UPDATE action_requests SET fingerprint=? WHERE request_key=?',
                                     (earlier, StateCoordinator._request_key('semantic', origin, key)))
    receipt = real(operation['id'], **called)
    assert receipt.outcome == 'published'
    assert len(executor.jobs) == 1, 'the finished row answered without another model call'


def test_a_new_work_whose_page_is_registered_under_another_title_publishes_onto_it(world):
    """One slug is one page: a request whose work page appeared under another title edits that page.

    Both notes were captured before either published, so both passed the capture's check that the slug is
    free. The first opened the page; the second names the same target with its own title, the shape a page
    the editor retitled since leaves too. It is the page the request opened, so the note publishes its fact
    there and the page keeps the identity it was opened with, instead of parking on a refusal forever."""
    first = world.service.create('First: rework the export.', project='app',
                                 new_work={'slug': 'export-rework', 'title': 'Rework the export'},
                                 idempotency_key='other-page-first')
    second = world.service.create('Second: the export rework caps retries.', project='app',
                                  new_work={'slug': 'export-rework', 'title': 'A different title'},
                                  idempotency_key='other-page-second')
    queue = dict(fact_key='app.export-rework', target=WORK, text='Rework the export pipeline.',
                 status='backlog', expected_revision=None)
    assert world.service.run(first['id'], executor=Scripted(
        lambda job: world.propose(facts=[queue])))['state'] == 'applied'
    other = dict(fact_key='app.export-rework-retries', target=WORK, text='The export rework caps retries.',
                 status='backlog', expected_revision=None)
    executor = Scripted(lambda job: world.propose(facts=[other]))
    result = world.service.run(second['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert WORK in [page['target'] for page in executor.jobs[0].payload['pages']], \
        'the registered page is sent to the model'
    assert 'new_work' not in executor.jobs[0].payload, 'no page creation is asked for a page that exists'
    registry = world.registry()
    assert registry['pages'][WORK]['title'] == 'Rework the export', 'the page keeps the identity it was opened with'
    assert registry['facts']['app.export-rework-retries']['target'] == WORK


def test_a_registered_target_counts_as_opened_only_for_a_work_page_of_its_own_project():
    """`opened_work_page` resolves a stored request onto a work page of its own project, and nothing else."""
    from kelmo.creation import opened_work_page
    target = 'projects/app/export-rework.md'
    work = {'target': target, 'title': 'Rework the export', 'summary': 'Rework the export',
            'parent': 'projects/app/app.md'}

    def registry(entry):
        return {'pages': {target: entry}}

    renamed = dict(work, kind='work', title='Export rework, phase two', summary='Retitled by the editor')
    assert opened_work_page(registry(renamed), 'app', work) == target
    assert opened_work_page(registry(dict(work, kind='work')), 'app', work) == target
    assert opened_work_page(registry(dict(work, kind='feature')), 'app', work) is None, \
        'a page of another kind at the target is no work page'
    assert opened_work_page(registry(dict(work, kind='work')), 'ops', work) is None, \
        'a target outside the project directory belongs to another project'
    assert opened_work_page({'pages': {}}, 'app', work) is None
    assert opened_work_page(registry(dict(work, kind='work')), 'app', None) is None


def test_a_note_declaring_an_unknown_domain_is_refused_before_the_capture(world):
    """A declared domain is checked when the note is written, not in the archive phase.

    The note that declared `domain: infrastructure` was accepted at capture and failed later, with no
    reason recorded and the session that wrote it already gone."""
    from kelmo.errors import KelmoError
    note = '---\nproject: app\ndomain: infrastructure\n---\n\nExports run nightly at 02:00.\n'
    with pytest.raises(KelmoError) as raised:
        world.service.create(note, 'bad-domain.md', project='app', selected=[FEATURE])
    assert raised.value.code == 'E_INPUT'
    assert 'domain' in str(raised.value) and 'dev' in str(raised.value) and 'life' in str(raised.value)
    assert 'infrastructure' in str(raised.value)
    assert world.service.queue() == [], 'no operation row exists'
    assert world.service.orphan_sources() == 0, 'no orphan source was spooled'


def test_a_note_declaring_an_allowed_domain_keeps_it_through_the_capture(world):
    """The shared reading is not a filter: a domain the archive accepts still reaches the record."""
    note = '---\nproject: app\ndomain: life\n---\n\nExports run nightly at 02:00.\n'
    operation = world.service.create(note, 'life-domain.md', project='app', selected=[FEATURE])
    assert world.service.source_packet(operation['id']).domain == 'life'


def test_a_domain_line_the_packet_cannot_read_is_no_declaration(world):
    """The capture reads the declared domain exactly as the packet does, no more strictly.

    A `domain:` line in a form the packet cannot read is not a declaration, so it keeps the packet's
    default instead of being refused under a rule the archive itself does not have."""
    operation = world.service.create('---\ndomain: [dev, life]\n---\n\nExports run nightly.\n', 'list-domain.md',
                                     project='app', selected=[FEATURE])
    assert world.service.source_packet(operation['id']).domain == 'dev'


def test_an_archive_phase_failure_records_the_reason_it_stopped(world, monkeypatch):
    """A failure inside the archive phase leaves a trace a person can read.

    The worker knows which phase broke and what the archive said; without an outcome the only trace is
    an error code and a phase name in the event log."""
    from kelmo.store.errors import ArchiveValidationError
    operation = world.service.create('Exports run nightly at 02:00.', 'archive-fault.md', project='app',
                                     selected=[FEATURE])

    def refused(*args, **kwargs):
        raise ArchiveValidationError('Synthetic archive refusal.')

    monkeypatch.setattr(world.service.archive_api, 'capture', refused)
    result = world.service.run(operation['id'])
    assert (result['state'], result['error_code'], result['terminal']) == ('failed', 'E_INPUT', True)
    outcome = world.outcome(operation['id'])
    assert outcome['disposition'] == 'unresolved'
    assert outcome['reason'].startswith('E_INPUT: Synthetic archive refusal.')
    failed = [event for event in world.service.trace(operation['id'])['events'] if event['stage'] == 'failed']
    assert failed[0]['details']['phase'] == 'archive'
