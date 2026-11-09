"""Scoped editor behavior over the real archive, journal, worker lock and publisher.

Every store is synthetic and seeded through the publisher. Executors here replay normalized outcomes; they prove wiring and refusals, never
semantic model quality. No provider process is started except where a test patches its runner.
"""

import json
import time
from contextlib import ExitStack
from dataclasses import replace

import pytest
import vault_template

HUB = 'projects/app/app.md'
FEATURE = 'projects/app/feature.md'
NOTES = 'projects/app/notes.md'
OTHER = 'projects/app/archive-notes.md'
REVIEW = 'projects/app/review.md'
DRAFT = 'projects/app/draft.md'
RULE = 'rule.release-review'
RULE_TEXT = 'Ship a release only after a second reviewer signs off, except documentation-only changes.'
UNMANAGED = 'Hand-written context the editor never owns: keep this sentence.\n'
PAGES = {
    'person.md': dict(page_id='person', layout='person', title='Person'),
    HUB: dict(page_id='app', layout='project-map', title='App'),
    FEATURE: dict(page_id='app-feature', layout='feature', title='Feature', summary='The export feature.',
                  parent=HUB, links=['app-notes']),
    NOTES: dict(page_id='app-notes', layout='topic', title='Notes'),
    OTHER: dict(page_id='app-archive', layout='topic', title='Archive notes', summary='Unrelated notes.'),
    REVIEW: dict(page_id='design-review', layout='topic', title='Design review'),
}
SEED = {RULE: ('person.md', RULE_TEXT),
        'person.desk-plant': ('person.md', 'Waters the desk plant on Mondays.'),
        'app.export': (FEATURE, 'Exports run nightly.'),
        'app.notes-scope': (NOTES, 'Notes cover the export pipeline.'),
        'app.archive': (OTHER, 'Archive notes are frozen.')}


class Scripted:
    """A ModelExecutor replaying normalized outcomes in order and recording every job it receives."""

    def __init__(self, *outcomes):
        self.outcomes, self.jobs = list(outcomes), []

    def execute(self, job):
        self.jobs.append(job)
        # Asked to repair a refused proposal with no scripted reply left, it insists on its last answer.
        outcome = self.outcomes.pop(0) if self.outcomes or 'refusal' not in job.payload else self.last
        self.last = outcome
        return outcome(job) if callable(outcome) else outcome


def proposal(**delta):
    from kelmo.executor import ModelOutcome
    return ModelOutcome('proposal', delta)


class World:
    def archive(self, text, principal='editor', key='source', role='implementer'):
        """Capture and archive one source through Service; returns its operation and exact evidence."""
        from kelmo.contracts import EvidenceRef
        from kelmo.service import Service
        with Service(self.root, bindings=self.bindings, origin=self.origins[principal]) as service:
            operation = service.capture(text, key + '.md', role, idempotency_key=key)
            assert service.run(operation['id'])['durable_stage'] == 'archived'
            return operation['id'], EvidenceRef.from_dict(service.journal.evidence_record(operation['id']))

    def registry(self):
        from kelmo.wiki import engine
        return engine.load_registry(self.root)

    def fact(self, key):
        return self.registry()['facts'][key]

    def page(self, target):
        return (self.service.config.wiki / target).read_text(encoding='utf-8')

    def outcome(self, operation_id):
        return self.service.journal.get(operation_id).get('outcome')

    def change(self, key, text, status='active'):
        target = SEED[key][0]
        return dict(fact_key=key, target=target, text=text, status=status,
                    expected_revision=self.fact(key)['revision'])

    def checks(self, facts):
        """Real reconciliation receipts: the current revision of an existing fact, a search receipt otherwise."""
        result = []
        for fact in facts:
            if fact['expected_revision'] is None:
                match = {'result': 'not_found',
                         'search_id': self.service.reconcile(fact['text'], fact['target'])['search_id']}
            else:
                match = {'result': 'existing', 'fact_key': fact['fact_key'], 'revision': fact['expected_revision']}
            result.append({'fact_key': fact['fact_key'], 'verified_match': match})
        return result

    def propose(self, **delta):
        """A proposal whose facts carry the receipts fact promotion requires."""
        return proposal(**delta, fact_checks=self.checks(delta.get('facts', [])))

    def contexts(self, features=None, actions=(), children=None):
        """Trusted controller enrollment; by default the detail feature pages the creation tests use.

        A created child links back to its parent feature, so `children` join the hub and the feature
        page as the complete set of pages showing app-feature.
        """
        from kelmo.context_binding import ContextBindings
        features = dict({'export-retries': (DETAIL, FEATURE)} if features is None else features)
        features.setdefault('app-feature', (HUB, FEATURE, *((DETAIL,) if children is None else children)))
        return ContextBindings(self.bindings, actions=tuple(actions), repository_remotes={}, worktrees={},
                               feature_pages=features, producer_client_ids=())

    def edit(self, source, executor, *, key='edit', origin='editor', selected=(FEATURE,), budget=60000,
             rule_change=False, contexts=None):
        return self.service.editor.edit(source, origin=self.origins[origin], idempotency_key=key,
                                        project='app', selected=list(selected), executor=executor,
                                        budget_tokens=budget, rule_change=rule_change, contexts=contexts)

    def reopen(self):
        from kelmo.service import Service
        self.service.close()
        self.service = self.stack.enter_context(Service(self.root, bindings=self.bindings))


LEGACY_TEXT = 'Legacy note: archive notes keep their original export dates.\n'
LEGACY_FACT = ('app.legacy', OTHER, 'Archive notes keep their original export dates.')
OPS = 'projects/ops/runbook.md'
OPS_RULE = 'ops.release-freeze'
OPS_TEXT = 'Releases freeze on Fridays, except security fixes approved by the on-call lead.'


def build_world(tmp_path, monkeypatch, *, legacy=False, other_project=False, protected=()):
    """Shared synthetic world; each test module registers its own `world` fixture over it.

    With `legacy`, one archived note is adopted without an evidence binding, so its fact keeps
    unresolved legacy evidence until an edit cites that same archived source. With
    `other_project`, a page of project `ops` holds a protected rule.

    The world is built once per process for each keyword set and every caller gets its own copy
    under tmp_path with a fresh Service on it (vault_template.py).
    """
    from kelmo.store.errors import ArchiveEmbeddingError
    from kelmo.service import Service

    def unavailable(*args, **kwargs):
        raise ArchiveEmbeddingError('Synthetic offline embedding provider')

    def forbidden(*args, **kwargs):
        raise AssertionError('Editor fixtures never start a provider process')

    monkeypatch.setattr('kelmo.store.memory.store.embed', unavailable)
    monkeypatch.setattr('kelmo.store.memory.search.embed', unavailable)
    monkeypatch.setattr('kelmo.executor.provider_execute', forbidden)
    for key, value in {'GIT_AUTHOR_NAME': 'Synthetic Fixture', 'GIT_COMMITTER_NAME': 'Synthetic Fixture',
                       'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                       'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / 'vault'
    if not vault_template.enabled():
        yield from _make_world(root, legacy, other_project, protected)
        return

    def build(path):
        builder = _make_world(path, legacy, other_project, protected)
        built = next(builder)
        state = {name: value for name, value in vars(built).items() if name not in ('root', 'stack', 'service')}
        builder.close()
        return state

    state = vault_template.copy(root, ('editor-world', legacy, other_project, tuple(protected)), build)
    with ExitStack() as stack:
        world = World()
        world.__dict__.update(state, root=root, stack=stack)
        world.service = stack.enter_context(Service(root, bindings=world.bindings))
        yield world


def _make_world(root, legacy, other_project, protected):
    """Build the synthetic world at root and yield it with its Service open."""
    import registry_seed
    from kelmo.config import Config
    from kelmo.server import Enrollment, OriginBindings, WriterIdentity
    from kelmo.service import Service
    from kelmo.wiki import engine, pages

    Service.initialize(root, 'fixture-node')
    config = Config.load(root)
    # Unmanaged prose exists before any managed region, as in a hand-maintained wiki.
    # DRAFT stays an unregistered hand-written page that no edit may claim.
    for target, title in ((HUB, 'App'), (FEATURE, 'Feature'), (DRAFT, 'Draft')):
        engine.atomic_write(config.wiki / target, '# ' + title + '\n\n' + UNMANAGED)
    engine.git(root, 'add', '--', 'knowledge/wiki')
    engine.git(root, 'commit', '-q', '-m', 'docs: Add hand-written pages')
    authority = config.vault_id
    trusted = OriginBindings(authority, WriterIdentity(authority, 'local', 1),
                             (Enrollment('local', 'fixture-node'), Enrollment('requester', 'fixture-node', True),
                              Enrollment('editor', 'fixture-node')))
    with ExitStack() as stack:
        world = World()
        world.__dict__.update(root=root, bindings=trusted, stack=stack,
                              origins={name: trusted.origin(name) for name in ('local', 'requester', 'editor')})
        _, seed = world.archive('Seed evidence for the synthetic fixture.\n', principal='requester', key='seed')
        seeded = list(SEED.items()) + ([(OPS_RULE, (OPS, OPS_TEXT))] if other_project else [])
        sources = [(seed, 'seed.md', seeded)]
        world.legacy = None
        if legacy:
            world.legacy = world.archive(LEGACY_TEXT, key='legacy')
            sources.append((world.legacy[1], 'legacy.md', [(LEGACY_FACT[0], LEGACY_FACT[1:])]))
        facts, notes = {}, {}
        for reference, filename, rows in sources:
            notes[reference.content_sha256] = {'filename': filename, 'ark_id': reference.archive_id}
            for key, (target, text) in rows:
                fact = dict(fact_key=key, target=target, text=text, status='active', expected_revision=None,
                            replaces=[], id=engine.digest(key)[:12], note_id=reference.content_sha256,
                            ark_id=reference.archive_id)
                fact['revision'] = pages.fact_revision(fact)
                facts[key] = fact
        settings = dict(PAGES, **({OPS: dict(page_id='ops-runbook', layout='topic', title='Runbook'),
                                   'projects/ops/ops.md': dict(page_id='ops', layout='project-map', title='Ops')}
                                  if other_project else {}))
        registry_seed.seed(root, {'facts': facts, 'notes': notes}, evidence_bindings={seed.content_sha256: seed},
                           pages_settings=settings,
                           protected_fact_keys=(RULE, *protected) + ((OPS_RULE,) if other_project else ()))
        world.seed = seed
        world.service = stack.enter_context(Service(root, bindings=trusted))
        yield world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_only_selected_pages_are_sent_and_linked_pages_wait_in_the_index(world):
    from kelmo.wiki import views
    source, ref = world.archive('The export now runs hourly.', key='hourly')
    executor = Scripted(proposal())
    receipt = world.edit(source, executor)
    job = executor.jobs[0]
    # The feature's parent map and the page it links to are index rows the model may ask for.
    assert {HUB, NOTES} <= set(views.dependencies(world.registry(), FEATURE))
    assert [page['target'] for page in job.payload['pages']] == [FEATURE]
    assert [page.path for page in job.context.pages] == [FEATURE]
    assert job.context.complete and job.context.omissions == () and job.context.freshness == 'current'
    assert job.context.source_mappings == (ref,)
    assert job.payload['source'] == {'evidence': ref.to_dict(), 'text': 'The export now runs hourly.'}
    assert 'Archive notes are frozen.' not in json.dumps(job.payload)
    assert 'Notes cover the export pipeline.' not in json.dumps(job.payload)
    assert {HUB, NOTES, OTHER} <= set(job.payload['expandable']) and FEATURE not in job.payload['expandable']
    assert [rule['target'] for rule in job.payload['mandatory']] == ['person.md']
    assert RULE_TEXT in job.context.mandatory_rules[0]
    assert receipt.outcome == 'unchanged' and receipt.evidence_refs == (ref,)
    assert world.outcome(source)['disposition'] == 'archive-only'


def test_batch_view_carries_every_source_under_sources(world):
    from kelmo.wiki import views
    first, first_ref = world.archive('Notes now also cover imports.', key='batch-one')
    second, second_ref = world.archive('Notes also cover exports.', key='batch-two')
    reader = world.service.knowledge
    view = views.editor_view(reader, reader.generation(), 'app', [NOTES], 60000, sources=[
        {'operation_id': first, 'text': 'Notes now also cover imports.', 'evidence': first_ref},
        {'operation_id': second, 'text': 'Notes also cover exports.', 'evidence': second_ref}])
    payload = view['payload']
    assert 'source' not in payload and [item['operation_id'] for item in payload['sources']] == [first, second]
    assert view['bundle'].source_mappings == (first_ref, second_ref)


def test_a_batch_view_refuses_a_source_that_names_no_text_or_operation_id(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    first, first_ref = world.archive('Notes now also cover imports.', key='batch-guard')
    reader = world.service.knowledge
    for broken in ({'operation_id': first, 'text': None, 'evidence': first_ref},
                   {'operation_id': None, 'text': 'Notes also cover exports.', 'evidence': first_ref}):
        with pytest.raises(KelmoError) as error:
            views.editor_view(reader, reader.generation(), 'app', [NOTES], 60000, sources=[broken])
        assert error.value.code == 'E_INPUT'


def test_budget_omission_is_named_scoped_and_never_complete(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    text = 'Notes now also cover imports.'
    source, ref = world.archive(text, key='imports')
    reader = world.service.knowledge
    full = views.editor_view(reader, reader.generation(), 'app', [FEATURE, NOTES], 60000, source_text=text,
                             evidence=ref)
    notes = next(block for block in full['payload']['pages'] if block['target'] == NOTES)
    before = world.registry()
    executor = Scripted(proposal(facts=[world.change('app.notes-scope', 'Notes cover exports and imports.')]))
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, selected=(FEATURE, NOTES), budget=full['used_tokens'] - views._cost(notes))
    assert error.value.code == 'E_PROPOSAL'
    job = executor.jobs[0]
    assert job.context.omissions == (NOTES,) and not job.context.complete
    assert job.payload['omitted'] == [NOTES] and NOTES in job.payload['expandable']
    assert world.registry() == before
    assert world.outcome(source)['disposition'] == 'unresolved'


def test_exact_expansion_is_wired_into_execution(world):
    from kelmo.executor import ModelOutcome
    source, ref = world.archive('The archive notes were reopened for the audit.', key='audit')
    executor = Scripted(ModelOutcome('expand', {'pages': [OTHER]}),
                        lambda job: world.propose(facts=[world.change('app.archive',
                                                                      'Archive notes are open for the audit.')]))
    receipt = world.edit(source, executor)
    first, second = executor.jobs
    assert OTHER not in [page['target'] for page in first.payload['pages']]
    assert [page['target'] for page in second.payload['pages']] == [FEATURE, OTHER]
    assert second.context.generation == first.context.generation
    # Both rounds belong to one conversation; the second names only what the expansion changed.
    assert first.conversation['turn'] == 0 and first.conversation['message'] is None
    assert second.conversation['key'] == first.conversation['key'] and second.conversation['turn'] == 1
    update = second.conversation['message']
    assert [page['target'] for page in update['appended']['pages']] == [OTHER]
    assert 'source' not in update['changed'] and 'mandatory' not in update['changed']
    fact = world.fact('app.archive')
    assert fact['text'] == 'Archive notes are open for the audit.'
    assert fact['evidence_refs'] == [ref.to_dict()] and fact['owner'] == 'semantic-editor'
    assert receipt.outcome == 'published' and receipt.evidence_refs == (ref,)
    assert 'Archive notes are open for the audit.' in world.page(OTHER)


def test_a_requested_page_is_never_crowded_out_by_pages_nobody_asked_for(world):
    """A real project map is linked from every page of its project. When linked pages were sent whole,
    they filled the budget first, the page the model asked for was omitted on every round, and the
    expansion limit refused the job."""
    from kelmo.executor import ModelOutcome
    from kelmo.wiki import views
    text = 'The archive notes were reopened for the audit.'
    source, ref = world.archive(text, key='starved')
    reader = world.service.knowledge
    exact = views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, source_text=text,
                              evidence=ref, expansions=[OTHER])['used_tokens']
    executor = Scripted(ModelOutcome('expand', {'pages': [OTHER]}),
                        lambda job: world.propose(facts=[world.change('app.archive',
                                                                      'Archive notes are open for the audit.')]))
    receipt = world.edit(source, executor, budget=exact)
    first, second = executor.jobs
    assert [block['target'] for block in first.payload['pages']] == [FEATURE]
    assert [block['target'] for block in second.payload['pages']] == [FEATURE, OTHER]
    assert second.context.complete and second.payload['omitted'] == []
    assert receipt.outcome == 'published'


def test_a_sent_page_carries_each_fact_once_and_its_own_text(world):
    from kelmo.wiki import views
    text = 'The export now runs hourly.'
    source, ref = world.archive(text, key='once')
    reader = world.service.knowledge
    [block] = views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, source_text=text,
                                evidence=ref)['payload']['pages']
    assert 'content' not in block and UNMANAGED.strip() in block['unmanaged_text']
    assert '<!-- kb -->' not in block['unmanaged_text'] and 'Exports run nightly.' not in block['unmanaged_text']
    assert [fact['fact_key'] for fact in block['facts']] == ['app.export']
    assert json.dumps(block).count('Exports run nightly.') == 1
    assert block['entry']['revision'] == world.registry()['pages'][FEATURE]['revision']


def test_unmanaged_text_is_what_the_publisher_does_not_render(world):
    from kelmo.wiki import pages
    owned = ('---\nwiki_contract: ' + pages.CONTRACT + '\ntitle: "Synthetic"\n---\n\n# Synthetic\n\n'
             '<!-- kb -->\n- A fact.\n<!-- /kb -->\n\n<!-- narrative -->\nWhy.\n<!-- /narrative -->\n')
    assert pages.unmanaged_text(owned) == ''
    assert pages.unmanaged_text(world.page(NOTES)) == ''
    assert pages.unmanaged_text(world.page(HUB)) == '# App\n\n' + UNMANAGED.strip()
    # An unpaired marker is not a region: its text stays visible rather than guessed away.
    assert pages.unmanaged_text('Prose.\n<!-- kb -->\nUnpaired.\n') == 'Prose.\n<!-- kb -->\nUnpaired.'


def test_an_expansion_refusal_names_its_cause(world):
    from kelmo.editor import MAX_EXPANSIONS, _expansion_refusal
    payload = {'expandable': [OTHER, NOTES], 'omitted': [NOTES]}
    assert 'names no pages' in _expansion_refusal([], payload, 0)
    assert 'not expandable: ' + FEATURE in _expansion_refusal([FEATURE], payload, 0)
    assert _expansion_refusal([OTHER], payload, 0) is None
    assert 'the budget still leaves out ' + NOTES in _expansion_refusal([NOTES], payload, MAX_EXPANSIONS)


@pytest.mark.parametrize('pages', [['projects/other/page.md'], ['person.md'], [FEATURE]])
def test_expansion_cannot_reach_unlisted_or_already_sent_pages(world, pages):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    source, _ = world.archive('Please look elsewhere.', key='elsewhere')
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(ModelOutcome('expand', {'pages': pages})))
    assert error.value.code == 'E_PROPOSAL'
    assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'


def test_unarchived_source_is_refused_before_any_model_call(world):
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    with Service(world.root, bindings=world.bindings, origin=world.origins['editor']) as service:
        operation = service.capture('Not archived yet.', 'pending.md', 'collector', idempotency_key='pending')
    executor = Scripted()
    with pytest.raises(KelmoError) as error:
        world.edit(operation['id'], executor)
    assert error.value.code == 'E_PERMISSION' and executor.jobs == []


def test_publication_receipt_replays_and_differs_from_archive_receipt(world):
    from kelmo.contracts import KnowledgeProposal
    from kelmo.errors import KelmoError
    source, ref = world.archive('Exports run hourly since the scheduler change.', key='hourly')
    archived = world.service.journal.get(source)
    assert archived['durable_stage'] == 'archived' and archived['outcome']['disposition'] == 'archive-only'
    executor = Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')]))
    receipt = world.edit(source, executor, key='hourly-edit')
    assert receipt.outcome == 'published' and receipt.generation is not None
    assert receipt.operation_id != source
    assert world.service.knowledge.generation() == receipt.generation
    assert world.outcome(source)['disposition'] == 'applied'
    registry = world.registry()
    assert registry['facts']['app.export']['evidence_refs'] == [ref.to_dict()]
    assert registry['notes'][ref.content_sha256]['evidence_refs'] == [ref.to_dict()]
    assert world.edit(source, Scripted(), key='hourly-edit') == receipt
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(), key='hourly-edit', selected=(NOTES,))
    assert error.value.code == 'E_IDEMPOTENCY'
    prepared = KnowledgeProposal.from_dict(world.service.journal.action_intent(receipt.operation_id)['proposal'])
    assert [change.path for change in prepared.changes] == [FEATURE]
    assert all(change.evidence_refs == (ref,) for change in prepared.changes)
    knowledge = world.service.knowledge_api
    assert knowledge.publish(prepared, prepared.baseline) == receipt
    forged = replace(prepared, changes=(replace(prepared.changes[0], content=prepared.changes[0].content + b'x'),))
    with pytest.raises(KelmoError) as error:
        knowledge.publish(forged, prepared.baseline)
    assert error.value.code == 'E_OWNERSHIP'
    assert world.registry() == registry


@pytest.mark.parametrize('stage', ['semantic_projection', 'publish_semantic'])
def test_crash_resumes_the_recorded_answer_without_another_model_call(world, monkeypatch, stage):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('Exports run hourly.', key='hourly')
    executor = Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')]))
    original = getattr(engine, stage)

    def crash(*args, **kwargs):
        raise RuntimeError('synthetic process death')

    monkeypatch.setattr(engine, stage, crash)
    # A render failure before preparation is a retryable conflict; after it, the intent stays prepared.
    with pytest.raises(KelmoError if stage == 'semantic_projection' else RuntimeError) as error:
        world.edit(source, executor, key='crash')
    if stage == 'semantic_projection':
        assert error.value.code == 'E_CONFLICT'
        assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['open']
    assert len(executor.jobs) == 1 and world.fact('app.export')['text'] == 'Exports run nightly.'
    prepared = world.service.journal.prepared_semantic()
    assert len(prepared) == (stage == 'publish_semantic')
    assert world.service.journal.prepared_actions() == []
    monkeypatch.setattr(engine, stage, original)
    world.reopen()
    receipt = world.edit(source, Scripted(), key='crash')
    assert receipt.outcome == 'published' and world.fact('app.export')['text'] == 'Exports run hourly.'
    assert world.outcome(source)['disposition'] == 'applied'


def test_quota_deferral_is_durable_and_typed_failure_keeps_the_source_unresolved(world, monkeypatch):
    from types import SimpleNamespace
    from kelmo import editor as module
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    from kelmo.quota import QuotaWait
    source, _ = world.archive('Exports run hourly.', key='hourly')
    before = world.registry()
    reset_at = time.time() + 60
    with pytest.raises(QuotaWait):
        world.edit(source, Scripted(ModelOutcome('deferred', reset_at=reset_at)), key='later')
    assert world.registry() == before
    assert world.outcome(source)['disposition'] == 'unresolved' and 'E_QUOTA' in world.outcome(source)['reason']
    world.reopen()
    status = world.service.editor.status(source)
    assert status['deferred_until'] == reset_at
    assert [(row['state'], row['error_code']) for row in status['requests']] == [('open', None)]
    blocked = Scripted()
    with pytest.raises(QuotaWait) as wait:
        world.edit(source, blocked, key='later')
    assert blocked.jobs == [] and wait.value.reset_at == reset_at
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: reset_at + 1))
    assert world.service.editor.status(source)['deferred_until'] is None
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='synthetic outage')),
                   key='later')
    assert error.value.code == 'E_EXECUTOR' and world.outcome(source)['disposition'] == 'unresolved'
    receipt = world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                         key='later')
    assert receipt.outcome == 'published' and world.outcome(source)['disposition'] == 'applied'


def test_stale_answer_is_refused_replayed_and_requeued(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports run hourly.', key='hourly')
    other, _ = world.archive('Notes now cover imports.', key='imports')
    stale_change = world.change('app.export', 'Exports run hourly.')

    def race(job):
        inner = Scripted(world.propose(facts=[world.change('app.notes-scope', 'Notes cover exports and imports.')]))
        assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
        return proposal(facts=[stale_change])

    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(race), key='outer')
    assert error.value.code == 'E_STALE'
    assert world.outcome(source)['disposition'] == 'unresolved'
    assert world.fact('app.export')['text'] == 'Exports run nightly.'
    with pytest.raises(KelmoError) as again:
        world.edit(source, Scripted(), key='outer')
    assert again.value.code == 'E_STALE'
    receipt = world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                         key='requeue')
    assert receipt.outcome == 'published' and world.outcome(source)['disposition'] == 'applied'


def test_malformed_proposal_stays_repairable_requeues_or_is_dismissed(world):
    from kelmo.errors import KelmoError
    editor = world.service.editor
    source, _ = world.archive('Exports run hourly.', key='hourly')
    before = world.registry()
    cases = (('owner', dict(owner='semantic-editor'), 'E_PROPOSAL'),
             ('evidence', dict(facts=[dict(world.change('app.export', 'Exports run hourly.'),
                                           evidence_refs=[])]), 'E_PROPOSAL'),
             ('revision', dict(facts=[dict(world.change('app.export', 'Exports run hourly.'),
                                           expected_revision='0' * 64)]), 'E_FACT_CHECK'))
    for key, delta, code in cases:
        with pytest.raises(KelmoError) as error:
            world.edit(source, Scripted(world.propose(**delta)), key=key)
        assert error.value.code == code
        assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'
    assert [(row['state'], row['error_code']) for row in editor.status(source)['requests']] == [
        ('refused', code) for _, _, code in cases]
    request = dict(origin=world.origins['editor'], project='app', selected=[FEATURE], budget_tokens=60000)
    receipt = editor.requeue(source, idempotency_key='repaired', executor=Scripted(
        world.propose(facts=[world.change('app.export', 'Exports run hourly.')])), **request)
    assert receipt.outcome == 'published' and world.outcome(source)['disposition'] == 'applied'
    with pytest.raises(KelmoError) as settled:
        editor.requeue(source, idempotency_key='again', executor=Scripted(), **request)
    assert settled.value.code == 'E_STATE'
    other, _ = world.archive('A duplicate of the hourly note.', key='duplicate')
    with pytest.raises(KelmoError):
        world.edit(other, Scripted(proposal(owner='semantic-editor')), key='duplicate')
    applied = world.registry()
    editor.dismiss(other, origin=world.origins['editor'], reason='duplicate of an earlier note')
    assert world.outcome(other)['disposition'] == 'archive-only' and 'dismissed' in world.outcome(other)['reason']
    with pytest.raises(KelmoError) as dismissed:
        editor.requeue(other, idempotency_key='after-dismiss', executor=Scripted(), **request)
    assert dismissed.value.code == 'E_STATE' and world.registry() == applied


def test_normalized_executor_is_provider_independent(world, monkeypatch):
    import sys
    from kelmo import executor as module
    from kelmo.editor import EDITING, EDITING_SCHEMA
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    # Model content never schedules a retry: only a provider-level QuotaWait defers.
    assert module.normalize_answer({'outcome': 'quota_wait', 'reset_at': time.time() + 30}) == ModelOutcome(
        'failure', error_code='E_PROPOSAL', reason='Model output cannot declare provider quota or a retry time.')
    assert module.normalize_answer({'reconcile': [{'text': 'x', 'target': NOTES}]}) == ModelOutcome(
        'reconcile', {'facts': [{'text': 'x', 'target': NOTES}]})
    assert module.normalize_answer({'unresolved': True}).error_code == 'E_UNRESOLVED'
    assert module.normalize_answer({'expand': [NOTES]}) == ModelOutcome('expand', {'pages': [NOTES]})
    # A structured answer carries every schema field, the unused ones empty: they ask for nothing.
    assert module.normalize_answer({'expand': [NOTES], 'facts': []}) == ModelOutcome('expand', {'pages': [NOTES]})
    fact = {'fact_key': 'app.export', 'target': FEATURE, 'text': 'x', 'status': 'active', 'expected_revision': None}
    assert module.normalize_answer({'facts': [fact], 'expand': [], 'reconcile': [], 'unresolved': False}) == \
        ModelOutcome('proposal', {'facts': [fact]})
    # A request is served before anything proposed beside it; pages come before reconciliation.
    check = {'text': 'x', 'target': FEATURE}
    assert module.normalize_answer({'reconcile': [check], 'facts': [fact]}) == ModelOutcome(
        'reconcile', {'facts': [check]})
    assert module.normalize_answer({'expand': [NOTES], 'reconcile': [check]}) == ModelOutcome(
        'expand', {'pages': [NOTES]})
    assert module.normalize_answer({'expand': NOTES}).error_code == 'E_PROPOSAL'
    assert module.normalize_answer('plain text').kind == 'failure'
    calls = []
    answer = world.propose(facts=[world.change('app.export', 'Exports run hourly.')]).result

    def provider(argv, request, **kwargs):
        calls.append((argv, request))
        return {'structured_output': dict(answer, expand=[], reconcile=[], unresolved=False)}

    monkeypatch.setattr(module, 'provider_execute', provider)
    cli = module.CliModelExecutor('claude', 'synthetic-model', executable=sys.executable)
    source, ref = world.archive('Exports run hourly.', key='cli')
    receipt = world.edit(source, cli, key='cli')
    argv, request = calls[0]
    assert request['instructions'] == EDITING and request['input']['task'] == 'edit_pages'
    assert json.loads(argv[argv.index('--json-schema') + 1]) == EDITING_SCHEMA
    # The model is told how many expand answers remain instead of counting its own turns, and the
    # prompt names the registry rules it is refused on (expansion budget, plain text, protected rules).
    assert request['input']['expansions_left'] == 2
    for sentence in ('"expansions_left" is the count', 'no HTML tags (not even copied from the source)',
                     'change only when the source is marked as a rule change'):
        assert sentence in EDITING, sentence
    assert receipt.outcome == 'published' and world.fact('app.export')['evidence_refs'] == [ref.to_dict()]

    def failing(argv, request, **kwargs):
        raise KelmoError('E_EXECUTOR', 'synthetic provider failure')

    monkeypatch.setattr(module, 'provider_execute', failing)
    other, _ = world.archive('Exports pause on holidays.', key='holiday')
    with pytest.raises(KelmoError) as error:
        world.edit(other, cli, key='holiday')
    assert error.value.code == 'E_EXECUTOR' and world.outcome(other)['disposition'] == 'unresolved'


DETAIL = 'projects/app/export-retries.md'


def detail_page(**fields):
    page = dict(target=DETAIL, expected_revision=None, page_id='export-retries', layout='feature',
                title='Export retries', summary='How failed exports are retried.', parent=FEATURE)
    page.update(fields)
    return page


DETAIL_FACT = dict(fact_key='app.export-retries', target=DETAIL, text='Failed exports retry three times.',
                   status='active', expected_revision=None)


def test_linked_detail_page_is_created_through_the_editor_and_publisher(world):
    import hashlib
    from kelmo.contracts import KnowledgeProposal
    source, ref = world.archive('Failed exports now retry three times.', key='retries')
    parent_before, feature_bytes = world.registry()['pages'][FEATURE], world.page(FEATURE)
    receipt = world.edit(source, Scripted(world.propose(pages=[detail_page()], facts=[DETAIL_FACT])), key='retries',
                         contexts=world.contexts())
    registry = world.registry()
    entry, parent = registry['pages'][DETAIL], registry['pages'][FEATURE]
    assert (entry['layout'], entry['kind'], entry['category']) == ('feature', 'work', 'projects')
    assert entry['parent'] == FEATURE and entry['feature_id'] == 'export-retries'
    assert entry['previous_revision'] is None and entry['repositories'] == [] and entry['compatibility'] == {}
    assert parent['links'] == parent_before['links'] + ['export-retries']
    assert parent['previous_revision'] == parent_before['revision']
    assert {name: parent[name] for name in ('title', 'summary', 'parent', 'compatibility')} == {
        name: parent_before[name] for name in ('title', 'summary', 'parent', 'compatibility')}
    fact = registry['facts']['app.export-retries']
    assert fact['evidence_refs'] == [ref.to_dict()] and not fact['mandatory'] and not fact['protected']
    prepared = KnowledgeProposal.from_dict(world.service.journal.action_intent(receipt.operation_id)['proposal'])
    baselines = {change.path: change.baseline_sha256 for change in prepared.changes}
    assert baselines == {DETAIL: hashlib.sha256(b'').hexdigest(),
                         FEATURE: hashlib.sha256(feature_bytes.encode('utf-8')).hexdigest()}
    created = world.service.knowledge_api.page(DETAIL, receipt.generation).content.decode('utf-8')
    assert 'Failed exports retry three times.' in created and 'page_id: export-retries' in created
    assert '[Feature](feature.md)' in created
    linked = world.page(FEATURE)
    assert '[Export retries](export-retries.md)' in linked
    assert linked.split('<!-- kb -->')[0] == feature_bytes.split('<!-- kb -->')[0]


@pytest.mark.parametrize('child_first', [True, False])
def test_parent_edit_and_child_creation_share_one_baseline(world, child_first):
    source, _ = world.archive('The export feature now includes retries.', key='parent-and-child')
    before = world.registry()['pages'][FEATURE]
    parent = dict(target=FEATURE, expected_revision=before['revision'], summary='Exports with retry handling.')
    changes = [detail_page(), parent] if child_first else [parent, detail_page()]
    receipt = world.edit(source, Scripted(world.propose(pages=changes, facts=[DETAIL_FACT])),
                         key='parent-and-child', contexts=world.contexts())
    stored = world.registry()['pages'][FEATURE]
    assert receipt.outcome == 'published'
    assert stored['summary'] == parent['summary']
    assert stored['links'] == before['links'] + ['export-retries']
    assert stored['previous_revision'] == before['revision']


def test_two_new_children_keep_the_published_parent_predecessor(world):
    source, _ = world.archive('The export feature has retry and timeout documentation.', key='two-children')
    before = world.registry()['pages'][FEATURE]
    second = detail_page(target='projects/app/export-timeouts.md', page_id='export-timeouts',
                         title='Export timeouts', layout='topic')
    receipt = world.edit(source, Scripted(proposal(pages=[detail_page(), second])), key='two-children',
                         contexts=world.contexts(children=(DETAIL, second['target'])))
    stored = world.registry()['pages'][FEATURE]
    assert receipt.outcome == 'published'
    assert stored['links'] == before['links'] + ['export-retries', 'export-timeouts']
    assert stored['previous_revision'] == before['revision']


def test_duplicate_page_updates_are_refused_before_publication(world):
    from kelmo.errors import KelmoError

    source, _ = world.archive('The export summary needs a correction.', key='duplicate-page')
    before = world.registry()
    first = dict(target=FEATURE, expected_revision=before['pages'][FEATURE]['revision'], summary='First wording.')
    second = dict(first, summary='Competing wording.')
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(pages=[first, second])), key='duplicate-page')
    assert error.value.code == 'E_PROPOSAL'
    assert world.registry() == before


@pytest.mark.parametrize('page,code', [
    (detail_page(target='projects/other/export-retries.md'), 'E_PROPOSAL'),
    (detail_page(target=DRAFT, page_id='draft-claim', layout='topic'), 'E_OWNERSHIP'),
    (detail_page(target=OTHER, page_id='archive-claim'), 'E_PROPOSAL'),
    (detail_page(parent=OTHER), 'E_PROPOSAL'),
    (detail_page(parent='person.md'), 'E_PROPOSAL'),
    (detail_page(page_id='app-notes'), 'E_PROPOSAL'),
    (detail_page(layout='person'), 'E_PROPOSAL'),
    (detail_page(links=['app-archive']), 'E_PROPOSAL'),
], ids=['other-project', 'unregistered-file', 'registered-unselected', 'unsent-parent', 'person-parent',
        'page-id-collision', 'person-layout', 'unsent-link'])
def test_page_creation_cannot_claim_an_unauthorized_destination(world, page, code):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Create a detail page.', key='claim')
    before, draft = world.registry(), world.page(DRAFT)
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(pages=[page])), key='claim',
                   contexts=world.contexts(children=(page['target'],)))
    assert error.value.code == code
    assert world.registry() == before and world.page(DRAFT) == draft
    assert world.outcome(source)['disposition'] == 'unresolved'


def test_concurrent_or_colliding_creation_is_refused(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('Failed exports retry three times.', key='outer')
    other, _ = world.archive('Exports retry after failures.', key='inner')

    def race(job):
        inner = Scripted(world.propose(pages=[detail_page()], facts=[DETAIL_FACT]))
        assert world.edit(other, inner, key='inner', contexts=world.contexts()).outcome == 'published'
        return proposal(pages=[detail_page()])

    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(race), key='outer', contexts=world.contexts())
    assert error.value.code == 'E_STALE'
    with pytest.raises(KelmoError) as again:
        world.edit(source, Scripted(proposal(pages=[detail_page(page_id='export-retries-two')])), key='requeue',
                   contexts=world.contexts())
    assert again.value.code == 'E_PROPOSAL'
    # A file that appears after preparation is never claimed by the pending publication.
    third, _ = world.archive('Imports also need a detail page.', key='imports')
    target = 'projects/app/import-retries.md'
    original = engine.publish_semantic

    def crash(*args, **kwargs):
        raise RuntimeError('synthetic process death')

    monkeypatch.setattr(engine, 'publish_semantic', crash)
    with pytest.raises(RuntimeError):
        world.edit(third, Scripted(proposal(pages=[detail_page(target=target, page_id='import-retries')])),
                   key='imports', contexts=world.contexts({'import-retries': (target, FEATURE)},
                                                         children=(DETAIL, target)))
    assert world.outcome(third)['disposition'] == 'unresolved'
    assert 'prepared' in world.outcome(third)['reason']
    monkeypatch.setattr(engine, 'publish_semantic', original)
    (world.service.config.wiki / target).write_text('# Hand-written imports\n', encoding='utf-8')
    before = world.registry()
    world.reopen()
    with pytest.raises(KelmoError) as collision:
        world.edit(third, Scripted(), key='imports')
    assert collision.value.code == 'E_OWNERSHIP'
    assert world.registry() == before and target not in before['pages']
    assert (world.service.config.wiki / target).read_text(encoding='utf-8') == '# Hand-written imports\n'
    assert world.outcome(third)['disposition'] == 'unresolved'


IMPORTS = dict(fact_key='app.notes-imports', target=NOTES, text='Notes cover the import pipeline.',
               status='active', expected_revision=None)


def receipt_for(fact, match):
    return [{'fact_key': fact['fact_key'], 'verified_match': match}]


def test_fact_promotion_requires_real_reconciliation_receipts(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    source, _ = world.archive('Notes now cover the import pipeline.', key='imports')
    before = world.registry()
    export = world.change('app.export', 'Exports run hourly.')
    for key, delta in (('missing', dict(facts=[IMPORTS])),
                       ('fabricated', dict(facts=[IMPORTS], fact_checks=receipt_for(
                           IMPORTS, {'result': 'not_found', 'search_id': 'fabricated'}))),
                       ('absent-claim', dict(facts=[export], fact_checks=receipt_for(
                           export, {'result': 'not_found', 'search_id': world.service.reconcile(
                               export['text'], export['target'])['search_id']})))):
        with pytest.raises(KelmoError) as error:
            world.edit(source, Scripted(proposal(**delta)), key=key, selected=(NOTES, FEATURE))
        assert error.value.code == 'E_FACT_CHECK'
        assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'

    def checked(job):
        [found] = job.payload['reconciliations']
        assert (found['text'], found['target']) == (IMPORTS['text'], NOTES) and 'candidates' in found
        return proposal(facts=[IMPORTS], fact_checks=receipt_for(
            IMPORTS, {'result': 'not_found', 'search_id': found['search_id']}))

    executor = Scripted(ModelOutcome('reconcile', {'facts': [{'text': IMPORTS['text'], 'target': NOTES}]}), checked)
    receipt = world.edit(source, executor, key='reconciled', selected=(NOTES,))
    assert receipt.outcome == 'published' and world.fact('app.notes-imports')['text'] == IMPORTS['text']
    assert world.service.journal.action_intent(receipt.operation_id)['fact_checks'] == checked(executor.jobs[1]).result[
        'fact_checks']


def test_a_miscopied_search_id_takes_the_receipt_of_the_same_text(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    source, _ = world.archive('Notes now cover the import pipeline.', key='imports')
    ask = ModelOutcome('reconcile', {'facts': [{'text': IMPORTS['text'], 'target': NOTES}]})

    def miscopied(text):
        def answer(job):
            [found] = job.payload['reconciliations']
            wrong = found['search_id'][:-1] + ('0' if found['search_id'][-1] != '0' else '1')
            return proposal(facts=[dict(IMPORTS, text=text)], fact_checks=receipt_for(
                IMPORTS, {'result': 'not_found', 'search_id': wrong}))
        return answer

    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(ask, miscopied('Notes now cover the import and export pipelines.')),
                   key='other-text', selected=(NOTES,))
    assert error.value.code == 'E_FACT_CHECK' and world.registry() == before

    executor = Scripted(ask, miscopied(IMPORTS['text']))
    receipt = world.edit(source, executor, key='same-text', selected=(NOTES,))
    assert receipt.outcome == 'published' and world.fact('app.notes-imports')['text'] == IMPORTS['text']
    [check] = world.service.journal.action_intent(receipt.operation_id)['fact_checks']
    assert check['verified_match']['search_id'] == executor.jobs[1].payload['reconciliations'][0]['search_id']


def test_prepared_fact_receipts_are_revalidated_before_publication(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('Notes now cover the import pipeline.', key='imports')
    before = world.registry()
    original = engine.publish_semantic
    monkeypatch.setattr(engine, 'publish_semantic', lambda *args: (_ for _ in ()).throw(RuntimeError('death')))
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[IMPORTS])), key='imports', selected=(NOTES,))
    monkeypatch.setattr(engine, 'publish_semantic', original)
    with world.service.journal.conn:
        world.service.journal.conn.execute('DELETE FROM fact_searches')
    world.reopen()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(), key='imports', selected=(NOTES,))
    assert error.value.code == 'E_FACT_CHECK'
    assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'


def test_feature_navigation_follows_only_trusted_enrollment(world):
    from kelmo.context_binding import ContextBindings
    from kelmo.errors import KelmoError
    source, _ = world.archive('Export retries are part of the export feature.', key='navigation')
    before = world.registry()
    linked = detail_page(layout='topic', links=['app-feature'])
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(pages=[linked])), key='unenrolled')
    assert error.value.code == 'E_BINDING' and world.registry() == before

    def contexts(*paths):
        return ContextBindings(world.bindings, actions=(), repository_remotes={}, worktrees={},
                               feature_pages={'app-feature': paths}, producer_client_ids=())

    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(pages=[linked])), key='stale-enrollment',
                   contexts=contexts(HUB, FEATURE))
    assert error.value.code == 'E_BINDING' and world.registry() == before
    receipt = world.edit(source, Scripted(proposal(pages=[linked])), key='enrolled',
                         contexts=contexts(HUB, FEATURE, DETAIL))
    assert receipt.outcome == 'published' and world.registry()['pages'][DETAIL]['links'] == ['app-feature']
    intent = world.service.journal.action_intent(receipt.operation_id)
    assert intent['feature_enrollment'] == {'app-feature': sorted([HUB, FEATURE, DETAIL])}


def test_applied_source_never_downgrades(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    editor = world.service.editor
    source, _ = world.archive('Exports run hourly.', key='hourly')
    assert world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                      key='first').outcome == 'published'
    with pytest.raises(KelmoError):
        world.edit(source, Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='outage')), key='second')
    assert world.edit(source, Scripted(proposal()), key='third').outcome == 'unchanged'
    with pytest.raises(KelmoError) as error:
        editor.dismiss(source, origin=world.origins['editor'], reason='late duplicate')
    assert error.value.code == 'E_STATE'
    assert world.outcome(source)['disposition'] == 'applied'
    # Each request keeps its own result in the journal.
    assert [row['state'] for row in editor.status(source)['requests']] == ['finished', 'open', 'finished']
    assert world.service.journal.latest_event(source, 'editor_result')['disposition'] == 'archive-only'


def test_dismissal_settles_only_eligible_editor_work_and_wins_over_a_late_answer(world):
    from kelmo.errors import KelmoError
    editor = world.service.editor
    untouched, _ = world.archive('Never edited.', key='untouched')
    with pytest.raises(KelmoError) as error:
        editor.dismiss(untouched, origin=world.origins['editor'], reason='not editor work')
    assert error.value.code == 'E_STATE' and world.outcome(untouched)['disposition'] == 'archive-only'
    source, _ = world.archive('Exports run hourly.', key='hourly')
    before = world.registry()
    late = world.propose(facts=[world.change('app.export', 'Exports run hourly.')])

    def answer_after_dismissal(job):
        editor.dismiss(source, origin=world.origins['editor'], reason='superseded while consulting')
        return late

    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(answer_after_dismissal), key='late')
    assert error.value.code == 'E_DISMISSED' and world.registry() == before
    outcome = world.outcome(source)
    assert outcome['disposition'] == 'archive-only' and 'dismissed' in outcome['reason']
    with pytest.raises(KelmoError) as again:
        world.edit(source, Scripted(late), key='after')
    assert again.value.code == 'E_STATE' and world.registry() == before


def test_incomplete_context_never_settles_an_empty_answer(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    text = 'Notes now also cover imports.'
    source, ref = world.archive(text, key='imports')
    reader = world.service.knowledge
    full = views.editor_view(reader, reader.generation(), 'app', [FEATURE, NOTES], 60000, source_text=text,
                             evidence=ref)
    notes = next(block for block in full['payload']['pages'] if block['target'] == NOTES)
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal()), selected=(FEATURE, NOTES),
                   budget=full['used_tokens'] - views._cost(notes))
    assert error.value.code == 'E_BUDGET' and NOTES in str(error.value)
    assert world.outcome(source)['disposition'] == 'unresolved'


def test_restatement_keeps_predecessor_owner_and_evidence(world):
    source, _ = world.archive('Exports run nightly; notes cover exports and imports.', key='restate')
    before = world.registry()
    feature = before['pages'][FEATURE]
    restated = dict(target=FEATURE, expected_revision=feature['revision'], summary=feature['summary'])
    receipt = world.edit(source, Scripted(world.propose(
        pages=[restated], facts=[world.change('app.export', 'Exports run nightly.'),
                                 world.change('app.notes-scope', 'Notes cover exports and imports.')])),
        key='restate', selected=(FEATURE, NOTES))
    after = world.registry()
    assert receipt.outcome == 'published'
    assert after['facts']['app.export'] == before['facts']['app.export']
    assert after['pages'][FEATURE] == feature
    assert after['facts']['app.notes-scope']['previous_revision'] == before['facts']['app.notes-scope']['revision']
    only, _ = world.archive('Exports run nightly.', key='only-restate')
    assert world.edit(only, Scripted(world.propose(facts=[world.change('app.export', 'Exports run nightly.')])),
                      key='only-restate').outcome == 'unchanged'
    assert world.registry() == after


@pytest.fixture
def legacy_world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch, legacy=True)


def test_legacy_note_resolves_only_from_its_own_archived_source(legacy_world):
    world = legacy_world
    operation, ref = world.legacy
    before = world.fact(LEGACY_FACT[0])
    assert before['evidence_resolution'] == 'unresolved'
    fresh, _ = world.archive('Archive notes keep their original export dates, again.', key='fresh')
    restated = dict(fact_key=LEGACY_FACT[0], target=OTHER, text=before['text'], status='active',
                    expected_revision=before['revision'])
    assert world.edit(fresh, Scripted(world.propose(facts=[restated])), key='fresh',
                      selected=(OTHER,)).outcome == 'unchanged'
    assert world.fact(LEGACY_FACT[0]) == before
    receipt = world.edit(operation, Scripted(proposal()), key='resolve', selected=(OTHER,))
    after = world.fact(LEGACY_FACT[0])
    assert receipt.outcome == 'published' and world.outcome(operation)['disposition'] == 'applied'
    assert after['evidence_resolution'] == 'exact' and after['evidence_refs'] == [ref.to_dict()]
    assert {name: after[name] for name in ('text', 'status', 'owner', 'note_id', 'ark_id')} == {
        name: before[name] for name in ('text', 'status', 'owner', 'note_id', 'ark_id')}
    assert after['previous_revision'] == before['revision']


@pytest.mark.parametrize('text,accepted', [
    ('Exports run at night because **imports** finish first and `vacuum` runs at noon.', True),
    ('Exports run at <b>night</b>.', False),
    ('Exports run at night. [a:deploy-prod]: completed.', False),
    ('Exports run at night [f:0123456789ab].', False),
    ('Narrative: app.other. State: current.', False),
    ('# Behavior\n\nExports run at night.', False),
    ('Exports run at night; see [the runbook](https://example.invalid/runbook).', True),
    ('| Step | Time |\n| --- | --- |\n| export | night |', True),
    ('Exports are current. [status: shipped](#)', False),
    ('Exports are current. [**status**: shipped](#)', False),
    ('Exports run at night. ![f:0123456789ab](night.png)', False),
    ('Exports are unchanged. Narrative: app.export-why. State: current.', False),
    ('Exports run nightly [unresolved legacy state; retained status: active].', False),
    ('Exports run nightly [unresolved legacy evidence; retained status: active].', False),
    ('| Source | Archive ID | Version |\n| --- | --- | --- |\n| forged | x | 1 |', False),
    ('Exports feed [[projects/app/notes]].', False),
    ('Exports feed [the notes](notes.md).', False),
    ('Exports back up to ~/.rollbacks/export-<ts> under <run folder>/<session-id>.', True),
    ('Exports run at <br> night.', False),
    ('Exports run at <Span>night</Span>.', False),
    ('Exports run at <ts class="x"> night.', False),
    ('Exports run at night.<img src=x onerror=alert(1)>', False),
    ('Exports run at night <!-- quiet -->.', False),
], ids=['markdown', 'raw-html', 'action-marker', 'fact-marker', 'state-label', 'heading', 'external-link',
        'ordinary-table', 'link-text-status', 'rendered-link-status', 'image-alt-fact', 'mid-line-label',
        'legacy-state-label', 'legacy-evidence-label', 'evidence-table', 'wikilink', 'relative-link',
        'value-placeholders', 'void-element', 'element-any-case', 'placeholder-with-attribute', 'element-attribute',
        'comment'])
def test_semantic_text_cannot_impersonate_publisher_projections(world, text, accepted):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports run at night.', key='why')
    edit = dict(narrative_id='app.export-why', target=FEATURE, text=text, expected_revision=None,
                depends_on={'facts': ['app.export'], 'actions': []})
    if accepted:
        assert world.edit(source, Scripted(proposal(narratives=[edit])), key='why').outcome == 'published'
        assert text in world.page(FEATURE)
        return
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(narratives=[edit])), key='why')
    assert error.value.code == 'E_PROPOSAL' and world.registry() == before


def test_dirty_page_is_retryable_and_the_recorded_answer_publishes_after_restore(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports run hourly.', key='hourly')
    path = world.service.config.wiki / FEATURE
    committed = path.read_bytes()
    path.write_bytes(committed + b'Uncommitted hand edit.\n')
    executor = Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')]))
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='dirty')
    assert error.value.code == 'E_CONFLICT' and world.registry() == before
    assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['open']
    assert world.outcome(source)['disposition'] == 'unresolved'
    path.write_bytes(committed)
    receipt = world.edit(source, Scripted(), key='dirty')
    assert receipt.outcome == 'published' and len(executor.jobs) == 1
    assert world.outcome(source)['disposition'] == 'applied'


def test_title_follows_only_a_publisher_generated_heading(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('The design review is now the architecture review.', key='title')
    review = world.registry()['pages'][REVIEW]
    world.edit(source, Scripted(proposal(pages=[dict(target=REVIEW, expected_revision=review['revision'],
                                                     title='Architecture review')])),
               key='title', selected=(REVIEW,))
    page = world.page(REVIEW)
    assert '\n# Architecture review\n' in page and '# Design review' not in page
    other, _ = world.archive('The feature is now called export.', key='hand-title')
    feature, hand = world.registry()['pages'][FEATURE], world.page(FEATURE)
    with pytest.raises(KelmoError) as error:
        world.edit(other, Scripted(proposal(pages=[dict(target=FEATURE, expected_revision=feature['revision'],
                                                        title='Export')])), key='hand-title')
    assert error.value.code == 'E_OWNERSHIP' and 'H1' in str(error.value)
    assert world.page(FEATURE) == hand and world.registry()['pages'][FEATURE] == feature


def test_concurrent_identical_request_waits_and_never_overwrites_the_winner(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports run hourly.', key='hourly')
    winner = world.propose(facts=[world.change('app.export', 'Exports run hourly.')])
    second = Scripted()

    def racing(job):
        with pytest.raises(KelmoError) as error:
            world.edit(source, second, key='same')
        assert error.value.code == 'E_BUSY'
        return winner

    receipt = world.edit(source, Scripted(racing), key='same')
    assert receipt.outcome == 'published' and second.jobs == []
    assert world.edit(source, second, key='same') == receipt
    assert world.fact('app.export')['text'] == 'Exports run hourly.'


def test_broken_model_link_is_refused_before_a_prepared_intent(world, monkeypatch):
    from kelmo import editor as module
    from kelmo.errors import KelmoError
    text = 'Exports run hourly, see [[projects/app/nonexistent]].'
    source, _ = world.archive(text, key='broken')
    before = world.registry()
    with pytest.raises(KelmoError) as refusal:
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', text)])), key='broken')
    assert refusal.value.code == 'E_PROPOSAL' and world.service.journal.prepared_intents() == []
    # The managed-link audit on the projection refuses it even when the text gate is bypassed.
    original = module.unsafe_text
    monkeypatch.setattr(module, 'unsafe_text', lambda text: None)
    with pytest.raises(KelmoError) as audit:
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', text)])), key='audited')
    assert audit.value.code == 'E_PROPOSAL' and 'wiki links' in str(audit.value)
    assert world.service.journal.prepared_intents() == [] and world.registry() == before
    monkeypatch.setattr(module, 'unsafe_text', original)
    other, _ = world.archive('Exports now run hourly.', key='unrelated')
    assert world.edit(other, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                      key='unrelated').outcome == 'published'


def test_a_poisoned_prepared_intent_is_refused_and_never_blocks_later_work(world, monkeypatch):
    from kelmo import editor as module
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine, links
    text = 'Exports run hourly, see [[projects/app/nonexistent]].'
    source, _ = world.archive(text, key='poison')
    clean = {'broken': [], 'ambiguous': []}
    originals = ((module, 'unsafe_text', module.unsafe_text), (links, 'audit_managed', links.audit_managed),
                 (engine, 'publish_semantic', engine.publish_semantic))
    monkeypatch.setattr(module, 'unsafe_text', lambda text: None)
    monkeypatch.setattr(links, 'audit_managed', lambda *args, **kwargs: dict(clean))
    monkeypatch.setattr(engine, 'publish_semantic', lambda *args: (_ for _ in ()).throw(RuntimeError('death')))
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', text)])), key='poison')
    [(poisoned, _)] = world.service.journal.prepared_intents()
    for owner, name, value in originals:
        monkeypatch.setattr(owner, name, value)
    world.reopen()
    other, _ = world.archive('Notes cover exports and imports.', key='later')
    receipt = world.edit(other, Scripted(world.propose(facts=[world.change(
        'app.notes-scope', 'Notes cover exports and imports.')])), key='later', selected=(NOTES,))
    assert receipt.outcome == 'published' and world.service.journal.prepared_intents() == []
    row = world.service.journal.action_row(poisoned)
    assert (row['state'], row['error_code']) == ('refused', 'E_PROPOSAL')
    assert world.outcome(source)['disposition'] == 'unresolved'
    with pytest.raises(KelmoError) as replay:
        world.edit(source, Scripted(), key='poison')
    assert replay.value.code == 'E_PROPOSAL'


def test_page_creation_needs_enrollment_and_never_claims_an_enrolled_subject(world):
    from kelmo.context_binding import ActionBinding
    from kelmo.errors import KelmoError
    billing = 'projects/app/billing.md'
    source, _ = world.archive('Billing gets its own feature page.', key='billing')
    before = world.registry()
    contexts = world.contexts({'billing': (billing, FEATURE)}, children=(billing,),
                              actions=(ActionBinding('notes-review', 'topic', 'release-notes', 'review'),))
    page = detail_page(target=billing, page_id='billing', title='Billing', summary='Billing exports.')
    for key, change, context in (
            ('no-enrollment', detail_page(layout='topic', page_id='free-topic'), None),
            ('topic-claims-feature', dict(page, layout='topic'), contexts),
            ('feature-elsewhere', dict(page, target='projects/app/other-billing.md'), contexts),
            ('unenrolled-feature', detail_page(page_id='unenrolled', target='projects/app/unenrolled.md'), contexts),
            ('topic-subject', detail_page(layout='topic', page_id='release-notes',
                                          target='projects/app/release-notes.md'), contexts),
            ('parent-not-enrolled', page, world.contexts({'billing': (billing,)}))):
        with pytest.raises(KelmoError) as error:
            world.edit(source, Scripted(proposal(pages=[change])), key=key, contexts=context)
        assert error.value.code == 'E_BINDING', key
        assert world.registry() == before
    receipt = world.edit(source, Scripted(proposal(pages=[page])), key='enrolled', contexts=contexts)
    assert receipt.outcome == 'published' and world.registry()['pages'][billing]['feature_id'] == 'billing'
    intent = world.service.journal.action_intent(receipt.operation_id)
    assert intent['enrolled_subjects'] == {'app-feature': 'feature', 'billing': 'feature', 'release-notes': 'topic'}


def test_request_refusals_during_consultation_settle_and_can_be_requeued(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    editor = world.service.editor
    source, _ = world.archive('Exports now run hourly.', key='budget')
    with pytest.raises(KelmoError) as refusal:
        world.edit(source, Scripted(proposal()), key='small', budget=1)
    assert refusal.value.code == 'E_BUDGET'
    assert [(row['state'], row['error_code']) for row in editor.status(source)['requests']] == [
        ('refused', 'E_BUDGET')]
    with pytest.raises(KelmoError) as replay:
        world.edit(source, Scripted(), key='small', budget=1)
    assert replay.value.code == 'E_BUDGET'
    receipt = editor.requeue(source, origin=world.origins['editor'], idempotency_key='more-context',
                             project='app', selected=[FEATURE], budget_tokens=60000,
                             executor=Scripted(world.propose(facts=[world.change('app.export',
                                                                                 'Exports run hourly.')])))
    assert receipt.outcome == 'published' and world.outcome(source)['disposition'] == 'applied'
    # A source the model declines twice (once more after the editor opened a work page for it) settles as
    # evidence only, with the model's reason; the same key replays the receipt without another call.
    other, _ = world.archive('Nothing for the wiki here.', key='nothing')
    declined = Scripted(ModelOutcome('failure', error_code='E_UNRESOLVED', reason='declined: small talk'),
                        ModelOutcome('failure', error_code='E_UNRESOLVED', reason='declined: still small talk'))
    receipt = world.edit(other, declined, key='declined')
    assert receipt.outcome == 'unchanged' and len(declined.jobs) == 2
    assert declined.jobs[1].payload['new_work']['target'] == 'projects/app/nothing.md'
    assert declined.jobs[1].payload['refusal']['code'] == 'E_UNRESOLVED'
    assert world.outcome(other) == {'disposition': 'archive-only',
                                    'reason': 'the editor applied nothing: declined: still small talk'}
    assert world.edit(other, Scripted(), key='declined') == receipt
    assert 'projects/app/nothing.md' not in world.registry()['pages']


def test_a_lost_lease_stops_the_attempt_before_another_executor_call(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    source, _ = world.archive('The archive notes were reopened.', key='lease')
    conn = world.service.journal.conn

    def taken_over(job):
        with conn:
            conn.execute("UPDATE action_requests SET observation=? WHERE kind='semantic' AND state='open'",
                         (json.dumps({'consulting': 'intruder', 'expires_at': time.time() + 900}),))
        return ModelOutcome('expand', {'pages': [OTHER]})

    executor = Scripted(taken_over, proposal())
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='lease')
    assert error.value.code == 'E_BUSY' and len(executor.jobs) == 1
    [row] = world.service.editor.status(source)['requests']
    assert row['state'] == 'open'
    assert json.loads(world.service.journal.action_row(row['operation_id'])['observation'])['consulting'] == 'intruder'


def test_request_and_source_outcome_commit_together(world, monkeypatch):
    from kelmo import outcomes
    from kelmo.wiki import engine
    source, _ = world.archive('Exports run hourly.', key='atomic')
    original_write = outcomes.write

    def dying(connection, operation_id, disposition, reason):
        if disposition == 'applied':
            raise RuntimeError('synthetic death between the receipt and the outcome')
        return original_write(connection, operation_id, disposition, reason)

    monkeypatch.setattr(outcomes, 'write', dying)
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                   key='atomic')
    [row] = world.service.editor.status(source)['requests']
    # The publication committed, but the receipt rolled back with the outcome: nothing reads as settled.
    assert row['state'] == 'prepared' and world.fact('app.export')['text'] == 'Exports run hourly.'
    assert world.outcome(source)['disposition'] == 'unresolved' and 'prepared' in world.outcome(source)['reason']
    monkeypatch.setattr(outcomes, 'write', original_write)
    world.reopen()
    receipt = world.edit(source, Scripted(), key='atomic')
    assert receipt.outcome == 'published' and world.outcome(source)['disposition'] == 'applied'
    assert engine.load_registry(world.root) == world.service.knowledge.registry(receipt.generation)


def test_dismissal_bounds_are_checked_before_any_request_changes(world):
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    source, _ = world.archive('Exports run hourly.', key='bounds')
    with pytest.raises(KelmoError):
        world.edit(source, Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='outage')), key='open')
    before = world.service.editor.status(source)
    with pytest.raises(KelmoError) as error:
        world.service.editor.dismiss(source, origin=world.origins['editor'], reason='x' * 500)
    assert error.value.code == 'E_OUTCOME' and world.service.editor.status(source) == before


def test_editor_cannot_race_an_open_promotion_or_resolution_workflow(world):
    from kelmo.errors import KelmoError
    from kelmo.resolution import Resolution
    source, _ = world.archive('Exports run hourly.', key='resolution-owned')
    Resolution(world.service)
    with world.service.journal.conn as conn:
        conn.execute('INSERT INTO resolutions(id,operation_id,payload,snapshot) VALUES (?,?,?,?)',
                     ('synthetic', source, '{}', '0' * 64))
    executor = Scripted()
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='race')
    assert error.value.code == 'E_STATE' and executor.jobs == []
    assert world.service.editor.status(source)['requests'] == []


def test_resolving_a_legacy_note_keeps_the_original_revision_contract(legacy_world):
    world = legacy_world
    operation, ref = world.legacy
    before = world.fact(LEGACY_FACT[0])
    change = dict(fact_key=LEGACY_FACT[0], target=OTHER, text='Archive notes keep their original export days.',
                  status='active', expected_revision=before['revision'])
    receipt = world.edit(operation, Scripted(world.propose(facts=[change])), key='resolve-and-edit',
                         selected=(OTHER,))
    after = world.fact(LEGACY_FACT[0])
    assert receipt.outcome == 'published' and after['text'] == change['text']
    assert after['evidence_refs'] == [ref.to_dict()] and after['previous_revision'] == before['revision']


def test_mandatory_selection_is_the_common_one_across_projects():
    from kelmo.wiki import pages
    rule, other = 'projects/app/rules.md', 'projects/other/rules.md'
    registry = {'pages': {'person.md': {'layout': 'person', 'category': 'you'},
                          'you/habits.md': {'layout': 'legacy', 'category': 'you'},
                          rule: {'layout': 'topic', 'category': 'projects'},
                          NOTES: {'layout': 'topic', 'category': 'projects'},
                          other: {'layout': 'topic', 'category': 'projects'}},
                'facts': {'a': {'target': rule, 'protected': True, 'mandatory': True},
                          'b': {'target': NOTES, 'protected': False, 'mandatory': False},
                          'c': {'target': other, 'protected': False, 'mandatory': True}}}
    paths = ('person.md', 'you/habits.md', rule, NOTES, other)
    assert pages.mandatory_paths(registry, paths) == ('person.md', rule, other, 'you/habits.md')
    # Only pages present in the tree are selected.
    assert pages.mandatory_paths(registry, (rule,)) == (rule,)


@pytest.fixture
def ops_world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch, other_project=True)


def test_protected_page_of_another_project_is_delivered_but_never_editable(ops_world):
    from kelmo.errors import KelmoError
    world = ops_world
    rule = world.fact(OPS_RULE)
    assert rule['protected'] and rule['mandatory']
    before = world.registry()
    attempts = (('ordinary', 'editor', False), ('requester-rule', 'requester', True))
    for key, principal, rule_change in attempts:
        source, _ = world.archive('Releases may ship on Fridays now.', principal=principal, key=key)
        change = dict(fact_key=OPS_RULE, target=OPS, text='Releases ship any day.', status='active',
                      expected_revision=rule['revision'])
        executor = Scripted(world.propose(facts=[change]))
        with pytest.raises(KelmoError) as error:
            world.edit(source, executor, key=key, origin=principal, rule_change=rule_change)
        # Delivered whole as mandatory context, from project ops, to an app request.
        payload = executor.jobs[0].payload
        assert [rule_page['target'] for rule_page in payload['mandatory']] == ['person.md', OPS]
        assert any(OPS_TEXT in text for text in executor.jobs[0].context.mandatory_rules)
        # Delivery is not edit scope, even under an attributable requester rule change.
        assert OPS not in [page['target'] for page in payload['pages']] and OPS not in payload['expandable']
        assert error.value.code == 'E_PROPOSAL' and world.registry() == before
    assert world.fact(OPS_RULE) == rule


def test_journal_outcome_keeps_the_reference_signatures(world):
    """Semantic outcomes settle their cited source; other kinds settle their own operation.
    Positional calls of the reference signatures stay valid."""
    from kelmo import outcomes
    from kelmo.errors import KelmoError
    journal = world.service.journal
    git, _ = world.archive('Git intake evidence.', key='git-intake')
    edited, _ = world.archive('Editor source evidence.', key='editor-source')
    origin = world.origins['editor']
    row = journal.action_begin('git-key', 'git', 'fp', origin, git, operation_id=git, observation={'ok': True})
    assert row['operation_id'] == git
    journal.action_finish(git, {'receipt': 1}, None, ('applied', 'StateAPI accepted action synthetic'))
    assert outcomes.get(journal.conn, git)['disposition'] == 'applied'
    before = outcomes.get(journal.conn, edited)
    refused = journal.action_begin('git-refused', 'git', 'fp', origin, edited, operation_id='intake-refused',
                                   error=('E_INPUT', 'bad'), outcome=('unresolved', 'E_INPUT: bad'))
    # Non-semantic outcomes target the request's own operation_id, never the cited source.
    assert refused['state'] == 'refused' and outcomes.get(journal.conn, 'intake-refused')['reason'] == 'E_INPUT: bad'
    assert outcomes.get(journal.conn, edited) == before
    applied = journal.action_begin('apply-key', 'apply', 'fp', origin, edited)
    journal.action_finish(applied['operation_id'], {'receipt': 2}, None, ('applied', 'synthetic apply outcome'))
    assert outcomes.get(journal.conn, applied['operation_id'])['reason'] == 'synthetic apply outcome'
    assert outcomes.get(journal.conn, edited) == before
    request = journal.action_begin('semantic-key', 'semantic', 'fp', origin, edited)
    assert request['operation_id'] != edited
    assert journal.action_refuse(request['operation_id'], 'E_PROPOSAL', 'refused',
                                 ('unresolved', 'E_PROPOSAL: refused'))
    # The editor outcome lands on its source, never on the request ID, and a replayed refusal writes nothing.
    assert outcomes.get(journal.conn, request['operation_id']) is None
    assert outcomes.get(journal.conn, edited)['reason'] == 'E_PROPOSAL: refused'
    assert not journal.action_refuse(request['operation_id'], 'E_PROPOSAL', 'again', ('archive-only', 'ignored'))
    assert outcomes.get(journal.conn, edited)['reason'] == 'E_PROPOSAL: refused'
    # Bounds are validated before any row changes.
    other = journal.action_begin('semantic-bounds', 'semantic', 'fp', origin, edited)
    with pytest.raises(KelmoError) as error:
        journal.action_refuse(other['operation_id'], 'E_PROPOSAL', 'x', ('unresolved', 'x' * 500))
    assert error.value.code == 'E_OUTCOME' and journal.action_row(other['operation_id'])['state'] == 'open'


def test_unrelated_dirty_registered_page_stays_retryable_before_preparation(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports now run hourly.', key='unrelated-dirty')
    path = world.service.config.wiki / OTHER
    original = path.read_bytes()
    path.write_bytes(original + b'Uncommitted independent context.\n')
    executor = Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')]))
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='unrelated-dirty')
    assert error.value.code == 'E_CONFLICT' and OTHER in str(error.value)
    assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['open']
    assert world.service.journal.prepared_intents() == []
    path.write_bytes(original)
    assert world.edit(source, Scripted(), key='unrelated-dirty').outcome == 'published'
    assert len(executor.jobs) == 1


def test_publication_budget_failure_before_preparation_retries_without_model(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('Exports now run hourly.', key='publication-budget')
    executor = Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')]))
    check_budget = engine.check_budget

    def unavailable(*args, **kwargs):
        raise KelmoError('E_BUDGET', 'The configured publication budget is temporarily insufficient.')

    monkeypatch.setattr(engine, 'check_budget', unavailable)
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='publication-budget')
    assert error.value.code == 'E_BUDGET'
    assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['open']
    assert world.service.journal.prepared_intents() == []
    monkeypatch.setattr(engine, 'check_budget', check_budget)
    assert world.edit(source, Scripted(), key='publication-budget').outcome == 'published'
    assert len(executor.jobs) == 1


def test_dirty_page_after_preparation_is_rechecked_at_recovery(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('Exports now run hourly.', key='prepared-dirty')
    original_publish = engine.publish_semantic
    monkeypatch.setattr(engine, 'publish_semantic', lambda *args: (_ for _ in ()).throw(RuntimeError('death')))
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[world.change('app.export', 'Exports run hourly.')])),
                   key='prepared-dirty')
    monkeypatch.setattr(engine, 'publish_semantic', original_publish)
    path = world.service.config.wiki / OTHER
    original = path.read_bytes()
    path.write_bytes(original + b'Hand edit after preparation.\n')
    world.reopen()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(), key='prepared-dirty')
    # A typed, retryable refusal before the commit; the intent stays prepared, nothing is half-written.
    assert error.value.code == 'E_CONFLICT'
    assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['prepared']
    assert world.fact('app.export')['text'] == 'Exports run nightly.'
    assert world.outcome(source)['disposition'] == 'unresolved'
    path.write_bytes(original)
    assert world.edit(source, Scripted(), key='prepared-dirty').outcome == 'published'
    assert world.outcome(source)['disposition'] == 'applied'


def test_unhashable_fact_check_key_is_a_requeueable_proposal_error(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports now run hourly.', key='bad-check-key')
    changed = world.change('app.export', 'Exports run hourly.')
    check = dict(world.checks([changed])[0], fact_key=[])
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(facts=[changed], fact_checks=[check])), key='bad-check-key')
    assert error.value.code == 'E_PROPOSAL'
    assert [row['state'] for row in world.service.editor.status(source)['requests']] == ['refused']
    assert world.outcome(source)['disposition'] == 'unresolved'
    receipt = world.service.editor.requeue(source, origin=world.origins['editor'], idempotency_key='fixed-key',
                                           project='app', selected=[FEATURE], budget_tokens=60000,
                                           executor=Scripted(world.propose(facts=[changed])))
    assert receipt.outcome == 'published'


@pytest.mark.parametrize('text,accepted', [
    ('Deployment artifact: synthetic-artifact-two.', False),
    ('Deployment artifact of deploy-prod: synthetic-artifact-two.', False),
    ('Work revision: 0123456789abcdef0123456789abcdef01234567.', False),
    ('Work revision of push-main: 0123456789abcdef0123456789abcdef01234567.', False),
    ('Subject: feature/app-feature. Feature ID: app-feature.', False),
    ('Exports declare Feature ID: app-feature.', False),
    ('Repository bindings: repo-a.', False),
    ('The deployment artifact is rebuilt before each nightly export.', True),
], ids=['artifact', 'artifact-of', 'revision', 'revision-of', 'subject', 'feature-id', 'repositories', 'prose'])
def test_semantic_feature_text_cannot_declare_publisher_bindings(world, text, accepted):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Synthetic deployment discussion.', key='declaration')
    changed = world.change('app.export', text)
    if accepted:
        assert world.edit(source, Scripted(world.propose(facts=[changed])), key='declaration').outcome == 'published'
        return
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(world.propose(facts=[changed])), key='declaration')
    assert error.value.code == 'E_PROPOSAL'
    assert world.fact('app.export')['text'] == 'Exports run nightly.'


@pytest.fixture
def rule_world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch, protected=('app.notes-scope',))


def test_mandatory_dependency_is_readable_for_dependencies_but_never_editable(rule_world):
    from kelmo.errors import KelmoError
    world = rule_world
    notes = world.fact('app.notes-scope')
    source, ref = world.archive('Exports run nightly because notes cover the pipeline.', key='why')
    explanation = dict(narrative_id='app.export-why', target=FEATURE, text='Exports run nightly for the notes.',
                       expected_revision=None, depends_on={'facts': ['app.notes-scope'], 'actions': []})
    executor = Scripted(proposal(narratives=[explanation]))
    assert world.edit(source, executor, key='why').outcome == 'published'
    payload = executor.jobs[0].payload
    [block] = [rule for rule in payload['mandatory'] if rule['target'] == NOTES]
    assert block['readable'] and [fact['fact_key'] for fact in block['facts']] == ['app.notes-scope']
    assert block['facts'][0]['revision'] == notes['revision']
    assert NOTES not in [page['target'] for page in payload['pages']] and executor.jobs[0].context.complete
    stored = world.registry()['narratives']['app.export-why']
    assert stored['depends_on']['facts'] == {'app.notes-scope': notes['revision']}
    other, _ = world.archive('Notes now cover imports.', key='edit-notes')
    with pytest.raises(KelmoError) as error:
        world.edit(other, Scripted(world.propose(facts=[world.change('app.notes-scope', 'Notes cover imports.')])),
                   key='edit-notes')
    assert error.value.code == 'E_PROPOSAL' and world.fact('app.notes-scope') == notes


def test_new_identities_stay_outside_the_shared_namespace(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    source, _ = world.archive('An explanation reusing a page identity.', key='collision')
    clash = dict(narrative_id='app-notes', target=FEATURE, text='Exports explained.', expected_revision=None,
                 depends_on={'facts': ['app.export'], 'actions': []})
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, Scripted(proposal(narratives=[clash])), key='collision')
    assert error.value.code == 'E_PROPOSAL' and world.registry() == before
    # The authoritative transform refuses the same collision with a typed refusal.
    intent = {'evidence_ref': world.seed.to_dict(), 'source_operation_id': 'synthetic', 'rule_change': None,
              'filename': 'seed.md', 'delta': {'pages': [], 'facts': [], 'narratives': [clash]}}
    with pytest.raises(engine.SemanticRefusal):
        engine.apply_semantic_intent(before, intent)


def test_editor_does_not_compete_with_a_git_intake(world):
    from kelmo.errors import KelmoError
    source, _ = world.archive('Git intake evidence.', key='git-owned')
    world.service.journal.action_begin('git-owned-key', 'git', 'fp', world.origins['editor'], source,
                                       operation_id=source, observation={'ok': True})
    executor = Scripted()
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, key='git-owned')
    assert error.value.code == 'E_STATE' and executor.jobs == []
    assert world.service.editor.status(source)['requests'] == []


@pytest.mark.parametrize('reset_at', [1.7e12, 'soon', float('inf')], ids=['milliseconds', 'text', 'infinite'])
def test_adapter_quota_metadata_is_bounded_before_conversion(world, reset_at):
    from kelmo import quota
    from kelmo.errors import KelmoError
    from kelmo.executor import ModelOutcome
    assert not quota.within_bounds(reset_at) and quota.within_bounds(time.time() + 60)

    class Adapter:
        def execute(self, job):
            # Bypasses ModelOutcome validation the way a faulty adapter could.
            outcome = ModelOutcome('deferred', reset_at=time.time() + 60)
            object.__setattr__(outcome, 'reset_at', reset_at)
            return outcome

    source, _ = world.archive('Exports run hourly.', key='bounded')
    with pytest.raises(KelmoError) as error:
        world.edit(source, Adapter(), key='bounded')
    assert error.value.code == 'E_EXECUTOR'
    status = world.service.editor.status(source)
    assert status['deferred_until'] is None and [row['state'] for row in status['requests']] == ['open']
    assert world.outcome(source)['disposition'] == 'unresolved'


def test_an_open_request_reads_unresolved_from_its_begin(world):
    source, _ = world.archive('Exports run hourly.', key='open')
    assert world.outcome(source)['disposition'] == 'archive-only'
    seen = []

    def look(job):
        seen.append(world.outcome(source))
        return world.propose(facts=[world.change('app.export', 'Exports run hourly.')])

    assert world.edit(source, Scripted(look), key='open').outcome == 'published'
    assert seen[0]['disposition'] == 'unresolved' and 'open' in seen[0]['reason']
    assert world.outcome(source)['disposition'] == 'applied'
    # A later request never downgrades the applied source at its begin.
    world.edit(source, Scripted(proposal()), key='later')
    assert world.outcome(source)['disposition'] == 'applied'


def test_local_vault_owner_may_create_topic_pages_but_not_enrolled_ones(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    from kelmo.wiki import engine
    root = tmp_path / 'vault'
    Service.initialize(root, 'synthetic', project='app')
    with Service(root) as service:
        contexts = service.context_bindings
        assert contexts is not None and contexts.bindings is service.bindings
        assert not contexts.feature_pages and not contexts.actions
        features, subjects = service.editor._enrollment(contexts)
    assert (features, subjects) == ({}, {})
    engine.creation_problem('app-exports', 'projects/app/exports.md', 'topic', features, subjects)
    with pytest.raises(KelmoError):
        engine.creation_problem('app-exports', 'projects/app/exports.md', 'feature', features, subjects)
