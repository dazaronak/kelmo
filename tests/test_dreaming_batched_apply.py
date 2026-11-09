"""Batched Dreaming apply: the apply pass that groups waiting sources and the editor that settles them.

These cases run the real pass — `apply_pending` over archived nightly notes with `[editor] batch_apply`
on — over the synthetic world of test_editor_scoping, and measure what the feature is about: how many
model conversations and how many commits a pass pays, and what each member source ends with. The shape of
the batch answer itself (attribution, the split, the collision rule) is `tests/test_dreaming_batched.py`;
the single-source pass is `tests/test_dreaming_cost.py`.
"""
import json

import pytest

from test_dreaming_batched import fact_change, fact_entry, page_entry, registry_with
from test_editor_scoping import FEATURE, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


def editor_mode(world, batch_apply=True, max_sources=None, budget=None):
    """Write the `[editor]` table the pass runs under: nightly mode, and the batch flag with its ceiling."""
    from kelmo.config import Config
    lines = ['mode = "nightly"', 'batch_apply = ' + ('true' if batch_apply else 'false')]
    if max_sources is not None:
        lines.append('batch_max_sources = ' + str(max_sources))
    if budget is not None:
        lines.append('budget_tokens = ' + str(budget))
    toml = world.root / 'kelmo.toml'
    text = toml.read_text(encoding='utf-8')
    # The table is replaced, not appended: a case that changes the mode mid-test (a pass under one flag,
    # the next pass under another) would otherwise write the same table twice and the config refuses it.
    section = '[editor]\n' + '\n'.join(lines) + '\n'
    start = text.find('[editor]\n')
    if start < 0:
        text += '\n' + section
    else:
        end = text.find('\n[', start + 1)
        text = text[:start] + section + (text[end + 1:] if end >= 0 else '')
    toml.write_text(text, encoding='utf-8')
    world.service.config = Config.load(world.root)


def capture(world, texts, project='app', selected=(FEATURE,)):
    """Capture and archive notes in capture order under nightly mode; returns their operation ids."""
    ids = []
    for text in texts:
        operation = world.service.create(text, project=project, selected=list(selected))
        world.service.run(operation['id'])
        ids.append(operation['id'])
    return ids


def notes(count, project='app'):
    return [f'{project.title()} note {index}: the {project} pipeline moves to '
            f'{7 + index:02d}:{index:02d} after the audit.' for index in range(count)]


PAD = 'The audit recorded the same reading on every shift of that week. '
SHRINK_BUDGET = 1950


def long_notes(count, pads=12, project='app'):
    """Notes padded with `pads` extra clauses, so the batch view costs more: the two budget tests pick the
    padding and the budget that make three of them too much for one batch of this fixture."""
    return [f'{project.title()} note {index}: the {project} pipeline moves to '
            f'{7 + index:02d}:{index:02d} after the audit. ' + PAD * pads for index in range(count)]


class BatchModel:
    """A fake model that answers a batch the way the nightly one does: one proposal naming every source.

    `broken` names note texts whose proposal round is the executor failure that leaves the batch with no
    proposal; `shared` makes every source propose the same fact key, which is the cross-source collision
    the split resolves in capture order; `decline` answers unresolved, which names no source and therefore
    declines the whole batch. A single-source payload (the flag off, or a source the batch left out) is
    answered the way the single-source replay answers it, so one executor covers both paths.
    """

    def __init__(self, world, broken=(), shared=False, decline=False):
        self.world, self.broken, self.jobs = world, set(broken), []
        self.shared, self.decline = shared, decline

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        if self.decline:
            return ModelOutcome('failure', error_code='E_UNRESOLVED', reason='nothing these pages should record')
        batch = payload.get('sources')
        blocks = list(batch) if batch is not None else [dict(payload['source'], operation_id=None)]
        facts, checks = [], []
        for block in blocks:
            text = block['text'].strip()
            if text in self.broken:
                return ModelOutcome('failure', error_code='E_EXECUTOR', reason='the replay produced no proposal')
            # The note's first sentence is what this replay settles: a long note is still one short fact.
            settled = text.split('. ')[0]
            key = (payload['project'] + '.shared' if self.shared
                   else payload['project'] + '.replay-' + block['evidence']['content_sha256'][:10])
            fact = dict(fact_key=key, target=payload['pages'][0]['target'], text=settled, status='active',
                        expected_revision=None)
            check = {'fact_key': key, 'verified_match': {'result': 'not_found',
                                                         'search_id': self.world.service.reconcile(
                                                             fact['text'], fact['target'])['search_id']}}
            if block['operation_id'] is not None:
                fact['source'] = check['source'] = block['operation_id']
            facts.append(fact)
            checks.append(check)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


class Dispatch:
    """Answers a batch payload with the batch replay and a single-source payload with `single`."""

    def __init__(self, world, single):
        self.world, self.single, self.jobs = world, single, []
        self.batch = BatchModel(world)

    def execute(self, job):
        self.jobs.append(job)
        return self.batch.execute(job) if 'sources' in job.payload else self.single(job)


class Reconciling(BatchModel):
    """The batch replay, after one reconciliation round for the first source of the batch.

    A reconciliation receipt is part of the view's cost, so the round that carries it can push the batch
    over its budget: this is the shrink that happens mid-conversation, after the rows are already open.
    """

    def __init__(self, world):
        super().__init__(world)
        self.asked = False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and not self.asked and not payload['reconciliations']:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': payload['sources'][0]['text'].split('. ')[0],
                                                         'target': FEATURE}]})
        return super().execute(job)


def counted(inner):
    from test_dreaming_cost import Counted
    return Counted(inner)


def commits(world):
    from kelmo.wiki import engine
    return int(engine.git(world.root, 'rev-list', '--count', 'HEAD'))


def waiting_ids(world):
    from kelmo.creation import pending_operations
    return [operation['id'] for operation, _request in pending_operations(world.service)]


def member_rows(world, ids):
    """Each member's own request row of the batch that published it."""
    from kelmo.editor import StateCoordinator
    keys = [StateCoordinator._request_key('semantic', world.service._operation_origin(operation_id),
                                          f'create:{operation_id}:1') for operation_id in ids]
    return [world.service.journal.action_request(key) for key in keys]


def test_one_batch_of_one_project_pays_one_conversation_and_one_commit(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    ops = capture(world, notes(1, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    model, before = counted(BatchModel(world)), commits(world)
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    alone = [job for job in model.inner.jobs if 'source' in job.payload]
    assert sorted(report['applied']) == sorted(ids + ops), 'every waiting source of the pass left the queue'
    assert waiting_ids(world) == []
    assert len(report['applied']) == 4 and (report['parked'], report['failed']) == ([], [])
    assert report['stalled'] == [] and report['skipped'] == [] and report['deferred'] == []
    assert [len(job.payload['sources']) for job in batched] == [3], 'the batch is one conversation'
    assert len(alone) == 1 and alone[0].payload['project'] == 'ops', 'a batch never mixes projects'
    assert model.calls == 2, 'three notes plus one of another project: two conversations, not eight'
    assert commits(world) - before == 2, 'one commit for the batch, one for the source published alone'


def test_the_ceiling_splits_the_waiting_sources_into_batches_of_that_size(world):
    from kelmo.creation import apply_pending
    editor_mode(world, max_sources=2)
    capture(world, notes(3))
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    assert len(report['applied']) == 3
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [2, 1], \
        'the ceiling bounds a batch; the leftover source is published alone by the same pass'
    assert 'source' in model.inner.jobs[1].payload, 'the leftover source is a single-source request'


def test_the_oldest_waiting_source_leads_its_batch_and_other_projects_wait_their_turn(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    app = capture(world, notes(2))
    capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    assert len(report['applied']) == 4
    assert [[job.payload['project'], len(job.payload['sources'])] for job in model.inner.jobs] == \
        [['app', 2], ['ops', 2]], 'one batch per project, in the order the notes were captured'
    assert model.inner.jobs[0].payload['sources'][0]['operation_id'] == app[0]


def test_a_cross_source_collision_parks_only_the_later_source_and_hints_it_next_pass(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    before = commits(world)
    report = apply_pending(world.service, counted(BatchModel(world, shared=True)))
    assert report['applied'] == [ids[0]] and report['parked'] == [ids[1]]
    assert world.service.journal.get(ids[1])['error_code'] == 'E_PROPOSAL'
    assert commits(world) - before == 1, 'the accepted slice publishes; the colliding one does not'
    again = counted(BatchModel(world))
    assert apply_pending(world.service, again)['applied'] == [ids[1]]
    assert again.inner.jobs[0].payload['refusal']['code'] == 'E_PROPOSAL', \
        'the park reaches the model as that source own hint'


def test_the_budget_shrink_drops_the_newest_source_and_it_stays_in_the_pass(world):
    from kelmo.creation import apply_pending
    # Measured on this fixture with FEATURE selected (the batch view's `budget.used_tokens`): the fixed
    # part of the view costs 687, the FEATURE page 272 and each 841-character note 464, so the
    # three-source view needs 2079 and the two-source one 1886. At SHRINK_BUDGET the batch therefore
    # shrinks from the newest source, which the same pass then publishes alone.
    editor_mode(world, budget=SHRINK_BUDGET)
    ids = capture(world, long_notes(3))
    before = commits(world)
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    assert [len(job.payload['sources']) for job in batched] == [2], 'the batch shrank from the newest source'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert model.calls == 2, 'the dropped source spent no part of the batch call; it got its own conversation'
    assert commits(world) - before == 2, 'the shrink splits one batch into a batch and a single source'
    assert ids[2] not in [block['operation_id'] for block in batched[0].payload['sources']]
    assert [event for event in world.service.trace(ids[2])['events'] if event['stage'] == 'parked'] == [], \
        'the dropped source spent no attempt of its own: it was never refused under its own key'
    assert world.outcome(ids[2])['disposition'] == 'applied'


def test_a_shrink_inside_the_conversation_drops_the_newest_source_into_its_own_attempt(world):
    """The shrink the preflight cannot see: a reconciliation round makes the view too big mid-batch."""
    from kelmo.creation import apply_pending
    # Measured on this fixture with FEATURE selected at budget 1950 (the batch view's `budget.used_tokens`):
    # the three padded sources cost 1570 and fit, the same view with one reconciliation receipt costs 1752
    # and sends no selected page, and the two-source view with that receipt costs 1821 and sends FEATURE.
    editor_mode(world, budget=SHRINK_BUDGET)
    texts = long_notes(3, pads=2)
    ids = capture(world, texts)
    before = commits(world)
    model = counted(Reconciling(world))
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    assert [[len(job.payload['sources']), len(job.payload['reconciliations'])] for job in batched] == \
        [[3, 0], [2, 1]], 'the batch shrank from the newest source after the reconciliation round'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert commits(world) - before == 2, 'the shrunk batch publishes its two, the dropped source publishes alone'
    assert ids[2] not in [block['operation_id'] for block in batched[-1].payload['sources']]
    assert [event for event in world.service.trace(ids[2])['events'] if event['stage'] == 'parked'] == [], \
        'the source the batch shrank out spends no attempt and is never refused under its own key'


def test_a_declined_batch_answer_declines_every_member_without_a_commit(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    before = commits(world)
    report = apply_pending(world.service, counted(BatchModel(world, decline=True)))
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == []
    assert commits(world) == before, 'a declined batch publishes nothing'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['archive-only'] * 2


def test_a_batch_the_executor_left_without_a_proposal_charges_every_member_once(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    texts = notes(3)
    ids = capture(world, texts)
    before = commits(world)
    report = apply_pending(world.service, counted(BatchModel(world, broken=[texts[1]])))
    assert (report['applied'], commits(world)) == ([], before), 'the refused batch published nothing'
    assert sorted(report['parked']) == sorted(ids), 'the batch conversation was spent for every member'
    for operation_id in ids:
        parked = [event for event in world.service.trace(operation_id)['events'] if event['stage'] == 'parked']
        assert len(parked) == 1, 'one charged attempt each, not one per source of the batch'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['unresolved'] * 3


def test_publish_batch_stops_at_a_source_another_attempt_holds(world):
    from kelmo.creation import batch_members, pending_operations, publish_batch
    editor_mode(world)
    ids = capture(world, notes(3))
    held = world.service.journal.claim(ids[1], lease_seconds=600)
    assert held is not None
    try:
        batch = batch_members(world.service, pending_operations(world.service),
                              world.service.config.batch_max_sources)
        model = counted(BatchModel(world))
        outcome = publish_batch(world.service, batch, model)
    finally:
        world.service.journal.release(ids[1], held)
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [1]
    assert [operation_id for block in model.inner.jobs[0].payload['sources']
            for operation_id in [block['operation_id']]] == [ids[0]], \
        'the batch stops at the held source: the newer one waits its own turn'
    assert [outcome[operation_id] for operation_id in (ids[1], ids[2])] == \
        [{'state': 'waiting', 'code': 'E_CLAIM'}] * 2, 'neither spent an attempt'
    assert outcome[ids[0]] == {'state': 'applied', 'code': None}
    row = world.service.journal.get(ids[1])
    assert (row['state'], row['terminal']) == ('archived', False), 'a held source is not touched by the batch'
    assert waiting_ids(world) == [ids[1], ids[2]]


def test_a_source_that_opens_its_work_page_stays_single_source_in_a_batched_pass(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    plain = capture(world, notes(2))
    work = world.service.create('App note: rework the export queue.', project='app', selected=[FEATURE],
                                new_work={'slug': 'export-queue', 'title': 'Rework the export queue'})
    world.service.run(work['id'])

    def single(job):
        return world.propose(facts=[dict(fact_key='app.export-queue', target='projects/app/export-queue.md',
                                         text='The export queue is reworked.', status='backlog',
                                         expected_revision=None)])

    model = counted(Dispatch(world, single))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(plain + [work['id']])
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 2], [False, 0]], 'a stored work page belongs to one request, never to a batch'
    assert 'projects/app/export-queue.md' in world.registry()['pages']


def test_the_flag_off_pass_is_the_single_source_pass_it_was(world):
    from kelmo.creation import apply_pending
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids)
    assert all('source' in job.payload and 'sources' not in job.payload for job in model.inner.jobs)
    assert model.calls == 3, 'off is one conversation per source, exactly as before'


def test_a_second_pass_replays_nothing_because_the_batch_settled_every_member(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    capture(world, notes(2))
    before = commits(world)
    apply_pending(world.service, counted(BatchModel(world)))
    second = apply_pending(world.service, counted(BatchModel(world)))
    assert (second['applied'], second['parked'], second['failed'], second['skipped']) == ([], [], [], [])
    assert commits(world) == before + 1


def test_a_pass_that_died_after_the_answer_settles_each_member_from_its_own_row(world, monkeypatch):
    """The window the design note names: the answer is recorded, the pass dies before the settle."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(2))
    before = commits(world)
    monkeypatch.setattr(Editor, '_settle_batch', lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('the pass died between the answer and the settle')))
    with pytest.raises(RuntimeError):
        apply_pending(world.service, counted(BatchModel(world)))
    monkeypatch.undo()
    settled = counted(BatchModel(world))
    report = apply_pending(world.service, settled)
    assert sorted(report['applied']) == sorted(ids)
    assert settled.calls == 0, 'the recorded batch answer is settled, never re-consulted'
    assert commits(world) == before + 1, 'the resumed pass publishes the batch once'


def test_the_batch_shares_its_commit_yet_every_member_keeps_its_own_receipt(world):
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world)
    ids = capture(world, notes(2))
    apply_pending(world.service, counted(BatchModel(world)))
    # The row of each member is keyed under the origin that owns that source, which is what the pass reads.
    keys = [StateCoordinator._request_key('semantic', world.service._operation_origin(operation_id),
                                          f'create:{operation_id}:1') for operation_id in ids]
    rows = [world.service.journal.action_request(key) for key in keys]
    assert [row['state'] for row in rows] == ['finished'] * 2, 'each member settled on its own row'
    assert rows[0]['receipt'] and rows[0]['receipt'] != rows[1]['receipt'], 'the commit is shared, not the receipt'


def test_a_single_source_proposal_still_refuses_the_batch_only_field():
    """`source_operation_id` is the batch's own attribution: with no batch it is malformed, as before."""
    from kelmo.editor import ProposalError, normalize

    registry = registry_with(facts=[fact_entry()], pages=page_entry())
    for name, item in (('facts', fact_change(source_operation_id='someone-else')),
                       ('pages', dict(page_entry('projects/app/notes.md'), title='Notes cover imports',
                                      source_operation_id='someone-else')),
                       ('narratives', dict(narrative_id='app-note-1', target='projects/app/notes.md',
                                           text='The audit moved the pipeline.', expected_revision=None,
                                           depends_on={'facts': [], 'actions': []},
                                           source_operation_id='someone-else'))):
        with pytest.raises(ProposalError) as error:
            normalize({name: [item]}, ['projects/app/notes.md'], registry, rule_change=False, project='app')
        assert 'only one' in str(error.value), name


def test_the_apply_limit_bounds_the_sources_a_batched_pass_handles(world):
    """`limit` is the pass's own ceiling: a batch never reaches past what the pass may still handle."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    capture(world, notes(3))
    report = apply_pending(world.service, counted(BatchModel(world)), limit=1)
    handled = sum(len(report[name]) for name in ('applied', 'parked', 'failed', 'deferred'))
    assert handled <= 1, f'limit=1 handled {handled}'
    assert len(waiting_ids(world)) == 2, 'the notes the limit left behind stay waiting'


def test_a_batched_pass_of_limit_two_publishes_one_batch_of_two(world):
    from kelmo.creation import apply_pending
    editor_mode(world)
    capture(world, notes(3))
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model, limit=2)
    handled = sum(len(report[name]) for name in ('applied', 'parked', 'failed', 'deferred'))
    assert handled == 2, report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2], \
        'the batch is bounded by the limit, not reduced to single sources'
    assert len(waiting_ids(world)) == 1


class _ShapeEditor:
    def deferred_until(self, _operation_id):
        return None


class _ShapeService:
    """The two reads `batch_members` makes of the service: the deferral and the owning origin."""

    editor = _ShapeEditor()

    def __init__(self, origins=None):
        self.origins = origins or {}

    def _operation_origin(self, operation_id):
        return self.origins.get(operation_id, 'origin')


def _waiting(*specs):
    return [({'id': operation_id}, dict(project=project, **extra))
            for operation_id, project, extra in specs]


def test_a_batch_never_passes_an_older_note_of_its_own_project():
    from kelmo.creation import batch_members

    waiting = _waiting(('a', 'app', {}), ('b', 'app', {'new_work': {'slug': 'x'}}), ('c', 'app', {}))
    assert [member[0]['id'] for member in batch_members(_ShapeService(), waiting, 8)] == ['a'], \
        'a note that cannot join holds the later notes of its project back'


def test_a_batch_stops_at_a_same_project_note_of_another_origin_and_skips_other_projects():
    from kelmo.creation import batch_members

    service = _ShapeService({'b': 'other'})
    waiting = _waiting(('a', 'app', {}), ('b', 'app', {}), ('c', 'app', {}))
    assert [member[0]['id'] for member in batch_members(service, waiting, 8)] == ['a']
    other = _waiting(('a', 'app', {}), ('b', 'ops', {}), ('c', 'app', {}))
    assert [member[0]['id'] for member in batch_members(_ShapeService(), other, 8)] == ['a', 'c'], \
        'a note of another project keeps its own turn and never holds this batch'



def facts_of(world):
    """The facts this fixture's replay published, keyed by their fact key."""
    return {key: fact for key, fact in world.registry()['facts'].items() if '.replay-' in key}


def test_each_published_fact_of_a_batch_cites_the_note_it_came_from(world):
    """The wiki's evidence for a fact is the note that proposed it, not the batch's lead."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    report = apply_pending(world.service, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report
    facts = facts_of(world)
    assert len(facts) == 3
    wrong = {key: fact['note_id'][:10] for key, fact in facts.items()
             if not fact['note_id'].startswith(key.split('.replay-')[1])}
    assert not wrong, f'facts cite another note: {wrong}'
    assert all(fact['evidence_refs'][0]['content_sha256'] == fact['note_id'] for fact in facts.values())


class AuthorityBatch(BatchModel):
    """A batch answer where one source proposes a new fact on the person page.

    That source is ordinary editing (no authority) while the other one of the batch carries the
    rule-change authority; which one is which is the caller's choice. A single-source payload is answered
    the way the batch replay answers it, so one executor covers a pass that republishes a source alone.
    """

    def __init__(self, world, person_source):
        super().__init__(world)
        self.person_source = person_source

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' not in payload:
            return super().execute(job)
        facts, checks = [], []
        for block in payload['sources']:
            on_person = block['operation_id'] == self.person_source
            evidence = block['evidence']['content_sha256'][:8]
            key = 'app.habit-' + evidence if on_person else 'app.replay-' + evidence
            target = 'person.md' if on_person else FEATURE
            text = 'Files the weekly report on Fridays.' if on_person else block['text'].split('. ')[0]
            facts.append(dict(fact_key=key, target=target, text=text, status='active', expected_revision=None,
                              source=block['operation_id']))
            checks.append({'fact_key': key, 'source': block['operation_id'],
                           'verified_match': {'result': 'not_found',
                                              'search_id': self.world.service.reconcile(text, target)['search_id']}})
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


def capture_rule_change(world, texts, principal='requester', rule_change_at=0):
    """Notes captured under an origin that may carry a rule-change request, archived in capture order.

    `rule_change_at` is which note asks for the authority; the batch's head is the first note, so
    `rule_change_at=1` puts the authority on a member rather than on the source that publishes the intent.
    """
    from kelmo.service import Service
    ids = []
    with Service(world.root, bindings=world.bindings, origin=world.origins[principal]) as service:
        for index, text in enumerate(texts):
            operation = service.create(text, project='app', selected=[FEATURE, 'person.md'],
                                       rule_change=(index == rule_change_at),
                                       idempotency_key='rule-note-' + str(index))
            world.service.run(operation['id'])
            ids.append(operation['id'])
    return ids


def test_a_rule_change_authority_reaches_only_the_items_of_its_own_source(world):
    """A new person-page fact from a source without the authority is not a protected rule."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture_rule_change(world, notes(2))
    report = apply_pending(world.service, AuthorityBatch(world, ids[1]))
    assert sorted(report['applied']) == sorted(ids), report
    new = [fact for fact in world.registry()['facts'].values() if fact['fact_key'].startswith('app.habit-')]
    assert len(new) == 1, [fact['fact_key'] for fact in new]
    assert new[0]['target'] == 'person.md'
    assert new[0]['protected'] is False and new[0]['owner'] != 'rule-policy', \
        'a source without the authority cannot make its fact a protected rule'
    assert new[0]['note_id'] == world.service.journal.evidence_record(ids[1])['content_sha256'], \
        'the fact cites the note that proposed it'


def test_a_new_person_fact_from_the_authorized_source_of_a_batch_is_still_a_rule(world):
    """The other half: the source that does carry the authority keeps its person fact protected."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture_rule_change(world, notes(2))
    report = apply_pending(world.service, AuthorityBatch(world, ids[0]))
    assert sorted(report['applied']) == sorted(ids), report
    new = [fact for fact in world.registry()['facts'].values() if fact['fact_key'].startswith('app.habit-')]
    assert len(new) == 1 and new[0]['protected'] is True and new[0]['owner'] == 'rule-policy'


def test_a_prepared_batch_rechecks_the_authority_of_every_member(world, monkeypatch):
    """The recheck before a batch commit covers the member's authority, not only the publisher's.

    The batch's head carries no rule-change authority; the later member does, and its person fact is what
    the prepared intent would publish. The member's requester enrollment is withdrawn in the window between
    the batch's prepare and its commit, which is exactly where `_recheck_rule_change` stands.
    """
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture_rule_change(world, notes(2), rule_change_at=1)
    bindings = world.service.bindings
    model = AuthorityBatch(world, ids[1])
    real = Editor._complete
    window = []

    def withdrawn(origin):
        bindings.validate(origin)
        raise ValueError('This binding cannot attest REQUESTER evidence.')

    def publish(self, operation_id):
        """The enrollment is gone by the time the prepared batch reaches its publication window."""
        self.service.bindings.require_requester = withdrawn
        window.append(operation_id)
        try:
            return real(self, operation_id)
        finally:
            del self.service.bindings.require_requester

    monkeypatch.setattr(Editor, '_complete', publish)
    report = apply_pending(world.service, model)
    assert window, 'the batch never reached its publication window'
    assert report['applied'] == [], report
    assert sorted(report['parked']) == sorted(ids), report
    assert all('E_ORIGIN' in world.outcome(operation_id)['reason'] for operation_id in ids), \
        [world.outcome(operation_id) for operation_id in ids]
    assert not [fact for fact in world.registry()['facts'].values() if fact['fact_key'].startswith('app.habit-')], \
        'a batch that cannot attest its member published no protected rule'


class NoRepairs(BatchModel):
    """The batch replay under an executor that asks for no repair round, the shape `revises = False` has."""
    revises = False


def test_every_member_of_a_published_batch_reports_applied_even_with_repairs_off(world):
    """The trace a reader sees: a member whose items were published ends `applied` with a published receipt."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    report = apply_pending(world.service, NoRepairs(world))
    assert sorted(report['applied']) == sorted(ids), report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3, \
        'a member whose facts were published is never reported as archive-only'
    assert [json.loads(row['receipt'])['outcome'] for row in member_rows(world, ids)] == ['published'] * 3
    for operation_id in ids:
        own = 'app.replay-' + world.service.journal.evidence_record(operation_id)['content_sha256'][:10]
        assert own in world.outcome(operation_id)['reason'], world.outcome(operation_id)


class CollidingFirst(BatchModel):
    """The first and the last source of the batch propose the same fact key; the middle one its own."""

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        facts, checks = [], []
        for index, block in enumerate(payload['sources']):
            shared = index != 1
            evidence = block['evidence']['content_sha256'][:10]
            key = payload['project'] + '.shared' if shared else payload['project'] + '.own-' + evidence
            text = block['text'].split('. ')[0] if shared else 'The middle note records its own claim.'
            fact = dict(fact_key=key, target=payload['pages'][0]['target'], text=text, status='active',
                        expected_revision=None, source=block['operation_id'])
            checks.append({'fact_key': key, 'source': block['operation_id'],
                           'verified_match': {'result': 'not_found',
                                              'search_id': self.world.service.reconcile(
                                                  text, fact['target'])['search_id']}})
            facts.append(fact)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


def test_a_member_of_a_batch_the_precheck_refused_before_splitting_still_reports_applied(world):
    """The collision the precheck names stops it recording any slice; the settle split still knows each owner."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    report = apply_pending(world.service, CollidingFirst(world))
    assert report['applied'] == [ids[0], ids[1]] and report['parked'] == [ids[2]], report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == \
        ['applied', 'applied', 'unresolved']
    assert json.loads(member_rows(world, ids)[1]['observation'])['slice'] is None, \
        'the precheck stopped before recording any slice, so that row holds no answer of its own'


def test_a_crash_after_the_commit_does_not_park_published_notes(world, monkeypatch):
    """The window between the batch commit and the settling of the other members."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    real = Editor._member_disposition

    def crash(self, *args, **kwargs):
        raise RuntimeError('the process died after the commit')

    monkeypatch.setattr(Editor, '_member_disposition', crash)
    with pytest.raises(RuntimeError):
        apply_pending(world.service, model)
    assert len(facts_of(world)) == 3, 'the commit landed before the crash'
    calls = len(model.jobs)
    monkeypatch.setattr(Editor, '_member_disposition', real)
    report = apply_pending(world.service, model)
    assert report['parked'] == [] and report['failed'] == [], report
    assert sorted(report['applied']) == sorted(ids), report
    assert len(model.jobs) == calls, f'the recovery paid {len(model.jobs) - calls} more model calls'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3


def test_a_source_dismissed_after_a_batch_committed_is_not_settled_applied(world, monkeypatch):
    """The one member of a committed batch a person can still dismiss: its row is open, so it refuses.

    The pass that settles a committed batch reads each member's own recorded answer, so a dismissed
    member must not be settled `applied` from that answer. Dismissal refuses the source's open request
    rows first, and the next pass replays that refusal instead of the batch answer its row carries.
    """
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    real = Editor._member_disposition

    def crash(self, *args, **kwargs):
        raise RuntimeError('the process died after the commit')

    monkeypatch.setattr(Editor, '_member_disposition', crash)
    with pytest.raises(RuntimeError):
        apply_pending(world.service, model)
    monkeypatch.setattr(Editor, '_member_disposition', real)
    assert len(facts_of(world)) == 3, 'the batch committed before the crash'
    calls = len(model.jobs)
    world.service.editor.dismiss(ids[1], origin=world.origins['editor'], reason='late duplicate')
    report = apply_pending(world.service, model)
    assert report['applied'] == [ids[0], ids[2]], report
    assert report['parked'] == [ids[1]], report
    assert report['failed'] == [] and report['skipped'] == [], report
    assert len(model.jobs) == calls, f'the recovery paid {len(model.jobs) - calls} more model calls'
    assert world.outcome(ids[1])['disposition'] == 'unresolved', world.outcome(ids[1])
    assert 'E_DISMISSED' in world.outcome(ids[1])['reason'], world.outcome(ids[1])
    assert len(facts_of(world)) == 3, 'the dismissal settles the source, not the facts the batch published'


class BogusCheck(BatchModel):
    """The batch replay, with a search receipt the journal never issued for the sources named `bogus_for`.

    `bogus_for` holds those sources' evidence hashes (a single-source payload names no operation id, so the
    evidence is what identifies a source). No reconciliation round was asked for those facts, so
    `rebind_receipts` has no receipt to restore and the fact-check gate refuses them. `revises = False` keeps
    the refused answer as recorded instead of sending it back for a repair round.
    """

    revises = False

    def __init__(self, world, bogus_for):
        super().__init__(world)
        self.bogus_for = set(bogus_for)

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        blocks = list(payload['sources']) if 'sources' in payload else [dict(payload['source'], operation_id=None)]
        facts, checks = [], []
        for block in blocks:
            text = block['text'].strip().split('. ')[0]
            evidence = block['evidence']['content_sha256'][:10]
            target = payload['pages'][0]['target']
            fact = dict(fact_key=payload['project'] + '.replay-' + evidence, target=target, text=text,
                        status='active', expected_revision=None)
            check = {'fact_key': fact['fact_key']}
            if evidence in self.bogus_for:
                check['verified_match'] = {'result': 'not_found', 'search_id': 'never-issued'}
            else:
                if block['operation_id'] is not None:
                    fact['source'] = check['source'] = block['operation_id']
                check['verified_match'] = {'result': 'not_found',
                                           'search_id': self.world.service.reconcile(text, target)['search_id']}
            facts.append(fact)
            checks.append(check)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


def evidence_of(world, operation_id):
    return world.service.journal.evidence_record(operation_id)['content_sha256'][:10]


def test_a_batch_that_accepts_nothing_refuses_with_the_code_its_members_met(world, monkeypatch):
    """A settled member is not a refusal: the code that parks the batch comes from a refused member.

    The first pass published one note and died before its source was staged, so its finished row is still
    a member of the next batch. When that batch's own consultation accepts nothing, the batch's refusal must
    name the code the refused source met: the settled member's disposition carries no code at all, and the
    source that batch already published keeps its own disposition.
    """
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    real = Editor._complete

    def died_after_the_commit(self, operation_id):
        real(self, operation_id)
        raise RuntimeError('the process died after the commit, before the source was staged')

    monkeypatch.setattr(Editor, '_complete', died_after_the_commit)
    with pytest.raises(RuntimeError):
        apply_pending(world.service, BatchModel(world))
    assert len(facts_of(world)) == 1, 'the first note published before the crash'
    assert sorted(waiting_ids(world)) == sorted(ids), 'neither source left the waiting list'
    monkeypatch.setattr(Editor, '_complete', real)
    editor_mode(world)
    report = apply_pending(world.service, BogusCheck(world, {evidence_of(world, ids[1])}))
    assert report['applied'] == [ids[0]], report
    assert report['parked'] == [ids[1]], report
    assert world.outcome(ids[0])['disposition'] == 'applied', world.outcome(ids[0])
    assert 'E_FACT_CHECK' in world.outcome(ids[1])['reason'], world.outcome(ids[1])
    assert len(facts_of(world)) == 1, 'the refused source published nothing'


def test_a_crash_after_the_intent_is_prepared_publishes_the_batch_on_the_next_pass(world, monkeypatch):
    """The other side of the same window: the intent is prepared, nothing committed yet."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    real = Editor._complete

    def crash(self, *args, **kwargs):
        raise RuntimeError('the process died before the commit')

    monkeypatch.setattr(Editor, '_complete', crash)
    with pytest.raises(RuntimeError):
        apply_pending(world.service, model)
    assert facts_of(world) == {}, 'nothing committed before the crash'
    calls = len(model.jobs)
    monkeypatch.setattr(Editor, '_complete', real)
    report = apply_pending(world.service, model)
    assert report['parked'] == [] and report['failed'] == [], report
    assert sorted(report['applied']) == sorted(ids), report
    assert len(model.jobs) == calls, f'the recovery paid {len(model.jobs) - calls} more model calls'
    assert len(facts_of(world)) == 3
