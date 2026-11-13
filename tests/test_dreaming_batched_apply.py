"""Batched Dreaming apply: the apply pass that groups waiting sources and the editor that settles them.

These cases run the real pass — `apply_pending` over archived nightly notes with `[editor] batch_apply`
on — over the synthetic world of test_editor_scoping, and measure what the feature is about: how many
model conversations and how many commits a pass pays, and what each member source ends with. The shape of
the batch answer itself (attribution, the split, the collision rule) is `tests/test_dreaming_batched.py`;
the single-source pass is `tests/test_dreaming_cost.py`.
"""
import json
import os
import sys

import pytest

from test_dreaming_batched import check_entry, fact_change, fact_entry, page_entry, registry_with
from test_editor_scoping import FEATURE, Scripted, build_world


class Died(BaseException):
    """The process ending mid-pass. The apply pass goes on past one note's ordinary fault; nothing outlives this."""


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


def editor_table(world, batch_apply=True, max_sources=None, budget=None, ready=None):
    """Write the `[editor]` table the pass runs under: nightly mode, and the batch flag with its ceiling.

    `ready` sets `[editor] batch_ready_sources`, the number of a project's first-pass notes that make it
    join a `--ready` pass; `max_sources` sets `batch_max_sources`, the ceiling of one batch. Writes only:
    a case that asks the config to refuse a value calls this one and loads the config itself."""
    lines = ['mode = "nightly"', 'batch_apply = ' + ('true' if batch_apply else 'false')]
    if max_sources is not None:
        lines.append('batch_max_sources = ' + str(max_sources))
    if ready is not None:
        lines.append('batch_ready_sources = ' + str(ready))
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


def editor_mode(world, **settings):
    """The same table, read back: the pass runs under the settings this call wrote."""
    from kelmo.config import Config
    editor_table(world, **settings)
    world.service.config = Config.load(world.root)


def capture(world, texts, project='app', selected=(FEATURE,)):
    """Capture and archive notes in capture order under nightly mode; returns their operation ids."""
    ids = []
    for text in texts:
        operation = world.service.create(text, project=project, selected=list(selected))
        world.service.run(operation['id'])
        ids.append(operation['id'])
    return ids


def notes(count, project='app', start=0):
    """`count` distinct note texts of `project`, numbered from `start` so a second capture is not a replay."""
    return [f'{project.title()} note {index}: the {project} pipeline moves to '
            f'{7 + index:02d}:{index:02d} after the audit.' for index in range(start, start + count)]


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


class DeclinedBatchAnswer(BatchModel):
    """A model that answers a batch with `unresolved` and answers a single source with the replay's proposal.

    `unresolved` names no source, so a batch conversation that answers it has said nothing about any note
    of the batch: that is the answer the first production batches ended with, where notes settled with no
    fact, narrative or page. A single-source payload (the flag off, or a note the pass sent alone) is
    answered the way the replay answers it, so a note that holds a fact publishes it on its own turn.
    """

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload:
            self.jobs.append(job)
            return ModelOutcome('failure', error_code='E_UNRESOLVED',
                                reason='nothing these pages should record')
        return super().execute(job)


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


CEILING_ASK = 'The audit closed every open item of that week.'


class CeilingReconciler(BatchModel):
    """The batch replay after one reconcile round whose receipts would pass the batch's receipt ceiling.

    Every source of the batch asks for the same text, so the round names the same candidate facts once
    per search: the ceiling decides the round, and the batch keeps the pages it already had.
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
            return ModelOutcome('reconcile', {'facts': [{'text': CEILING_ASK, 'target': FEATURE}
                                                        for _ in payload['sources']]})
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
    # Measured on this fixture with FEATURE selected at budget 2000 (the batch view's `budget.used_tokens`):
    # the three padded sources cost 1570 and fit, the same view with one reconciliation receipt costs 1788
    # and sends no selected page (the FEATURE page needs 390), and the two-source view with that receipt
    # costs 1857 and sends FEATURE. 2000 also leaves the receipt round (492) under the ceiling of 500, so
    # the round runs and the shrink is the view's, not the ceiling's.
    editor_mode(world, budget=2000)
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


def test_a_batch_reconcile_past_the_receipt_ceiling_runs_no_search_and_keeps_the_batch(world):
    """The batch half of the receipt ceiling: the round is refused before its searches run, the model gets
    the repair, and the batch publishes with the pages it already had — no `E_BUDGET` anywhere in the pass."""
    from kelmo import outcomes
    from kelmo.creation import apply_pending
    from kelmo.fact_checks import binding
    budget = 1800
    editor_mode(world, budget=budget)
    ids = capture(world, notes(3))
    before = commits(world)
    model = counted(CeilingReconciler(world))
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    assert [len(job.payload['sources']) for job in batched] == [3, 3], \
        'the ceiling refused the reconcile round, not the batch: no source was shrunk out'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert commits(world) - before == 1, 'the batch still publishes its three notes in one commit'
    first, second = batched
    assert first.payload['budget']['receipt_ceiling'] == budget // 4
    assert second.payload['reconciliations'] == [] and 'candidates' not in second.payload
    refusal = second.payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and set(refusal) == {'code', 'reason'}
    assert 'may cost at most ' + str(budget // 4) + ' estimated tokens of its ' + str(budget) in refusal['reason']
    assert 'No search was run: ask for fewer facts at once, propose with the receipts you already have' \
        in refusal['reason']
    assert len(refusal['reason']) <= outcomes.REASON_LIMIT
    asked = world.service.journal.conn.execute('SELECT COUNT(*) AS total FROM fact_searches WHERE binding LIKE ?',
                                               (binding(CEILING_ASK, FEATURE) + ':%',)).fetchone()
    assert asked['total'] == 0, 'the refused round ran none of the searches it asked for'


def test_a_batch_budget_that_sends_no_page_refuses_without_a_model_call(world):
    """The batch shape of the failure we could not read from outside: the selected page did not fit,
    nothing was sent, and the model was consulted anyway — then asked to expand the page the budget had
    already omitted. The refusal is the request's, so it is named by the numbers the view measured."""
    from kelmo import outcomes
    from kelmo.contracts import EvidenceRef
    from kelmo.creation import apply_pending
    from kelmo.wiki import views
    ids, texts = [], notes(2)
    for text in texts:
        # budget_tokens=1: `_editor_request` takes the larger of the stored budget and the vault's, which
        # is written below from the measured view, so the notes are captured before the budget is known.
        operation = world.service.create(text, project='app', selected=[FEATURE], budget_tokens=1)
        world.service.run(operation['id'])
        ids.append(operation['id'])
    reader = world.service.knowledge
    blocks = [{'operation_id': operation_id, 'text': text,
               'evidence': EvidenceRef.from_dict(world.service.journal.evidence_record(operation_id))}
              for operation_id, text in zip(ids, texts)]
    # The budget is measured against the one-source view the preflight shrinks to: it must refuse that
    # one too, or the pass would publish each note alone under a budget that holds it.
    view = views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, sources=blocks[:1])
    cost = views._cost(next(block for block in view['payload']['pages'] if block['target'] == FEATURE))
    budget = view['used_tokens'] - cost
    editor_mode(world, budget=budget)
    before = commits(world)
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    assert model.calls == 0, 'an empty scope is refused before the model is asked to expand an omitted page'
    assert sorted(report['parked']) == sorted(ids) and (report['applied'], report['failed']) == ([], [])
    assert commits(world) == before and world.service.journal.prepared_intents() == []
    for operation_id in ids:
        reason = world.outcome(operation_id)['reason']
        assert ('omitted ' + FEATURE + ': needs ' + str(cost) + ' tokens, 0 of ' + str(budget)
                + ' left') in reason, reason
        assert len(reason) <= outcomes.REASON_LIMIT


def test_a_batch_answer_to_partial_context_is_refused_with_the_budget_numbers(world):
    """The batch shape of the settle refusal: the model answered nothing to a context that sent only part
    of what the batch selected, and the refusal names what the omitted page needed."""
    from kelmo import outcomes
    from kelmo.contracts import EvidenceRef
    from kelmo.creation import apply_pending
    from kelmo.executor import ModelOutcome
    from kelmo.wiki import views
    selected = [FEATURE, 'projects/app/notes.md']
    texts = notes(2)
    ids = []
    for text in texts:
        # budget_tokens=1: the effective budget is the larger of the stored one and the vault's, which is
        # written below from the measured view, so the notes are captured before the budget is known.
        operation = world.service.create(text, project='app', selected=selected, budget_tokens=1)
        world.service.run(operation['id'])
        ids.append(operation['id'])
    reader = world.service.knowledge
    full = views.editor_view(reader, reader.generation(), 'app', selected, 60000,
                             sources=[{'operation_id': operation_id, 'text': text,
                                       'evidence': EvidenceRef.from_dict(
                                           world.service.journal.evidence_record(operation_id))}
                                      for operation_id, text in zip(ids, texts)])
    cost = views._cost(next(block for block in full['payload']['pages']
                            if block['target'] == 'projects/app/notes.md'))
    editor_mode(world, budget=full['used_tokens'] - cost)

    class Empty:
        """The model answer of a batch that saw a partial context: it proposed nothing."""

        def __init__(self):
            self.jobs = []

        def execute(self, job):
            self.jobs.append(job)
            return ModelOutcome('proposal', {'facts': [], 'fact_checks': []})

    model = Empty()
    before = commits(world)
    report = apply_pending(world.service, model)
    assert sorted(report['parked']) == sorted(ids) and (report['applied'], report['failed']) == ([], [])
    assert commits(world) == before and world.service.journal.prepared_intents() == []
    for operation_id in ids:
        reason = world.outcome(operation_id)['reason']
        assert 'the model saw incomplete context and proposed nothing' in reason, reason
        assert ('omitted projects/app/notes.md: needs ' + str(cost) + ' tokens, 0 of '
                + str(full['used_tokens'] - cost) + ' left') in reason, reason
        assert len(reason) <= outcomes.REASON_LIMIT


def test_a_declined_batch_answer_declines_every_member_without_a_commit(world):
    """A batch answered `unresolved` settles no member: each one is declined by its own consultation.

    `unresolved` names no source, so it is no note's answer and settles no note: the batch spends its one
    conversation and each member runs the single-source path in the same pass. Nothing is published, so
    there is no commit, and each note ends `archive-only` on the reason *its own* consultation earned —
    with the work page that path opens for a declined source named in its own row, and no attempt spent.
    """
    import json

    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    before = commits(world)
    model = counted(BatchModel(world, decline=True))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == []
    assert commits(world) == before, 'a declined batch publishes nothing'
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [2, 1, 1, 1, 1], \
        'one batch conversation answered unresolved, then each member ran the single-source path'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['archive-only'] * 2
    for index, operation_id in enumerate(ids):
        assert charged_parkings(world, operation_id) == ([], 0), \
            'the batch conversation charges no member: no note is refused under its own key'
        row = member_rows(world, ids)[index]
        observation = json.loads(row['observation'])
        assert 'batch' not in observation, 'the note settled on its own answer, not on the batch\'s'
        assert observation['new_work']['target'].startswith('projects/app/'), \
            'the declined note got the work page the single-source path opens for it'
        assert 'adds no new fact' not in world.outcome(operation_id)['reason'], world.outcome(operation_id)


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
    """Changed on purpose by step 8: a stored work page no longer keeps its note out of the batch.

    The expectation this case pinned (`[[True, 2], [False, 0]]`: the batch of the two plain notes, then
    the note with the opened page alone) is the shape the first production batches lost notes in — the
    single-source path is where a lost conversation leaves a note with no fact, no narrative and no page.
    The note now joins its batch, so the pass pays one conversation for all three and the page is opened
    with the batch's commit. The name is kept because the loop checks this case by name.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    plain = capture(world, notes(2))
    work = world.service.create('App note: rework the export queue.', project='app', selected=[FEATURE],
                                new_work={'slug': 'export-queue', 'title': 'Rework the export queue'})
    world.service.run(work['id'])

    def single(job):
        return world.propose(facts=[dict(fact_key='app.export-queue', target=target,
                                         text='The export queue is reworked.', status='backlog',
                                         expected_revision=None)])

    model = counted(WorkPageBatch(world, {evidence_of(world, work['id'])}, single))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(plain + [work['id']])
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 3]], 'a stored work page joins its batch: one conversation, not a batch plus a single note'
    assert [(item['target'], item['source']) for item in model.inner.jobs[0].payload['new_work']] \
        == [(target, work['id'])], 'the payload names the page with the note whose request opened it'
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
        Died('the pass died between the answer and the settle')))
    with pytest.raises(Died):
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
    def __init__(self, deferred=()):
        self.deferred = set(deferred)

    def deferred_until(self, operation_id):
        return 1234.0 if operation_id in self.deferred else None


class _ShapeService:
    """The two reads `batch_members` makes of the service: the deferral and the owning origin."""

    def __init__(self, origins=None, deferred=()):
        self.origins = origins or {}
        self.editor = _ShapeEditor(deferred)

    def _operation_origin(self, operation_id):
        return self.origins.get(operation_id, 'origin')


def _waiting(*specs):
    return [({'id': operation_id}, dict(project=project, **extra))
            for operation_id, project, extra in specs]


def test_a_batch_never_passes_an_older_note_of_its_own_project():
    from kelmo.creation import batch_members

    # Changed on purpose by step 8: a stored `new_work` is no longer a reason to stay out of the batch,
    # so the note that cannot join here is one whose quota deferral has not reset. The guarantee this
    # case pins is unchanged: a source the batch cannot take holds the notes behind it back.
    waiting = _waiting(('a', 'app', {}), ('b', 'app', {'new_work': {'slug': 'x'}}), ('c', 'app', {}))
    assert [member[0]['id'] for member in batch_members(_ShapeService(), waiting, 8)] == ['a', 'b', 'c'], \
        'a note whose request opened its own work page joins its batch, in capture order'
    held = _waiting(('a', 'app', {}), ('b', 'app', {}), ('c', 'app', {}))
    assert [member[0]['id'] for member in batch_members(_ShapeService(deferred=['b']), held, 8)] == ['a'], \
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
        self.jobs.append(job)
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
        raise Died('the process died after the commit')

    monkeypatch.setattr(Editor, '_member_disposition', crash)
    with pytest.raises(Died):
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
        raise Died('the process died after the commit')

    monkeypatch.setattr(Editor, '_member_disposition', crash)
    with pytest.raises(Died):
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
        raise Died('the process died after the commit, before the source was staged')

    monkeypatch.setattr(Editor, '_complete', died_after_the_commit)
    with pytest.raises(Died):
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
        raise Died('the process died before the commit')

    monkeypatch.setattr(Editor, '_complete', crash)
    with pytest.raises(Died):
        apply_pending(world.service, model)
    assert facts_of(world) == {}, 'nothing committed before the crash'
    calls = len(model.jobs)
    monkeypatch.setattr(Editor, '_complete', real)
    report = apply_pending(world.service, model)
    assert report['parked'] == [] and report['failed'] == [], report
    assert sorted(report['applied']) == sorted(ids), report
    assert len(model.jobs) == calls, f'the recovery paid {len(model.jobs) - calls} more model calls'
    assert len(facts_of(world)) == 3


def test_an_oversized_applied_reason_refuses_the_batch_before_the_commit(world, monkeypatch):
    """The batch path decides its outcome before the commit too, and the sources keep their turn.

    A reason the outcome store refuses must not stop the queue: the batch is refused with nothing
    prepared and nothing committed, every member parks with that code, and the next pass publishes them."""
    from kelmo import editor, outcomes
    from kelmo.creation import apply_pending
    original = editor._summary
    monkeypatch.setattr(editor, '_summary', lambda delta: 'outcome reason ' + 'x' * outcomes.REASON_LIMIT)
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    report = apply_pending(world.service, model)
    assert report['applied'] == [] and report['failed'] == []
    assert sorted(report['parked']) == sorted(ids), report
    assert facts_of(world) == {}, 'nothing was committed'
    assert world.service.journal.prepared_semantic() == [], 'no intent was prepared'
    assert all(world.outcome(operation_id)['reason'].startswith('E_OUTCOME: ') for operation_id in ids)
    monkeypatch.setattr(editor, '_summary', original)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert len(facts_of(world)) == 3, 'the same notes published once the reason was whole'


def test_two_notes_with_the_same_new_work_publish_onto_one_page_in_a_batched_pass(world):
    """The batched pass reads the same stored request: a work page that exists is edited, not opened again.

    Both requests were captured before either note published, so both name the same page. Step 8 changed
    this case on purpose: the two notes no longer run the single-source path one at a time, they join the
    plain notes of their project in one batch, and the payload names the opened page once per note whose
    request opened it. The guarantee is the same one the single-source shape gave: the page is created
    once, under the earlier note, and each note's own fact lands on it with its own evidence."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-rework.md'
    plain = capture(world, notes(2))
    work = []
    for text in ('App note: rework the export queue.', 'App note: the export queue caps retries at three.'):
        operation = world.service.create(text, project='app', selected=[FEATURE],
                                         new_work={'slug': 'export-rework', 'title': 'Rework the export'})
        world.service.run(operation['id'])
        work.append(operation['id'])

    def single(job):
        fact = dict(fact_key='app.rework-' + job.payload['source']['evidence']['content_sha256'][:10],
                    target=target, text=job.payload['source']['text'].split('. ')[0], status='backlog',
                    expected_revision=None)
        return world.propose(facts=[fact])

    model = counted(WorkPageBatch(world, {evidence_of(world, operation_id) for operation_id in work}, single))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(plain + work), report
    assert [len(job.payload['sources']) for job in model.inner.jobs if 'sources' in job.payload] == [4], \
        'the notes that open a work page join the batch of their project'
    assert not [job for job in model.inner.jobs if 'source' in job.payload], \
        'no note of this pass is left to the single-source path'
    assert sorted(item['source'] for item in model.inner.jobs[0].payload['new_work']) == sorted(work), \
        'the payload names the opened page with each note whose request opened it'
    assert {item['target'] for item in model.inner.jobs[0].payload['new_work']} == {target}
    registry = world.registry()
    assert registry['pages'][target]['page_id'] == 'projects-app-export-rework', 'one page, not a second one'
    assert registry['pages'][target]['title'] == 'Rework the export'
    keys = sorted(key for key in registry['facts'] if registry['facts'][key]['target'] == target)
    assert len(keys) == 2, 'a fact from each note'
    evidence = [world.service.journal.evidence_record(operation_id)
                for operation_id in sorted(work)]
    assert sorted(registry['facts'][key]['note_id'] for key in keys) == \
        sorted(ref['content_sha256'] for ref in evidence), 'each fact cites its own note'


def test_a_batch_member_the_outcome_store_refuses_parks_past_its_attempts(world, monkeypatch):
    """The batch path shares the bound: a refusal the vault causes does not fail the note it parks.

    Every pass of the batch meets the same E_OUTCOME, so the note would run out of its PARK_ATTEMPTS at
    once; the first FREE_STALE_REFUSALS of them are not charged, and only past that bound the note fails
    and the owner is alerted."""
    import sys

    from kelmo import creation, editor, outcomes
    from kelmo.config import Config
    from kelmo.creation import apply_pending
    editor_mode(world)
    scratch = world.root.parent
    script = scratch / 'alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n')
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[alerts]\ncommand = ["' + sys.executable + '", "'
                      + str(script) + '", "' + str(scratch / 'messages.txt') + '"]\n', encoding='utf-8')
    world.service.config = Config.load(world.root)
    ids = capture(world, notes(2))
    monkeypatch.setattr(editor, '_summary', lambda delta: 'outcome reason ' + 'x' * outcomes.REASON_LIMIT)
    states = []
    for _ in range(creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS):
        report = apply_pending(world.service, counted(BatchModel(world)))
        states.append((report['parked'], report['failed']))
        if report['failed']:
            break
    assert all(not failed for _parked, failed in states[:-1]), states
    assert sorted(states[-1][1]) == sorted(ids), states[-1]
    assert all(sorted(parked) == sorted(ids) for parked, _failed in states[:-1])
    assert len(states) == creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS
    lines = (scratch / 'messages.txt').read_text().splitlines()
    assert len(lines) == 2, lines
    assert all('could not be placed after' in line for line in lines)


def test_a_batch_refusal_sends_the_page_it_names_and_the_batch_publishes_the_repair(world):
    """The batch path gets the same repair: its next round reads the page the refusal named."""
    from kelmo.creation import apply_pending
    from test_editor_scoping import OTHER
    editor_mode(world)
    ids = capture(world, notes(2))

    class Claiming(BatchModel):
        """The batch replay, except its first answer claims a page this vault already registers."""

        def execute(self, job):
            answer = super().execute(job)
            payload = job.payload
            if 'sources' not in payload:
                return answer
            if 'refusal' in payload:
                entry = next(page['entry'] for page in payload['pages'] if page['target'] == OTHER)
                answer.result['pages'] = [dict(target=OTHER, expected_revision=entry['revision'],
                                               summary='Archive notes cover the audit.',
                                               source=payload['sources'][0]['operation_id'])]
            else:
                answer.result['pages'] = [dict(target=OTHER, expected_revision=None, page_id='archive-claim',
                                               layout='topic', title='Archive notes', summary='Unrelated notes.',
                                               parent=FEATURE, source=payload['sources'][0]['operation_id'])]
            return answer

    model = counted(Claiming(world))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2, 2]
    refusal = model.inner.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and OTHER in refusal['reason']
    assert set(refusal) == {'code', 'reason'}, 'the model reads a refusal, not the editor internals'
    assert OTHER in [page['target'] for page in model.inner.jobs[1].payload['pages']], \
        'the page the refusal named is sent with its entities'
    assert model.inner.jobs[1].payload['expansions_left'] == 2, 'the repair spent no expansion of the batch'
    assert world.registry()['pages'][OTHER]['summary'] == 'Archive notes cover the audit.'


def test_a_retried_note_parks_again_under_the_batch_pass_and_not_at_once(world):
    """The batch pass spends the same attempt budget as the single-source one, and it is a real batch.

    `kelmo queue retry` renews that budget there too: a member that had already failed parks on its next
    refusal after the retry and fails only after the full PARK_ATTEMPTS of the renewed budget. The retried
    note is measured inside a batch of two, not alone, so what the case reads is the batch path's own
    accounting."""
    from kelmo import creation
    editor_mode(world)
    texts = notes(2)
    ids = capture(world, texts)
    model = counted(BatchModel(world, broken=texts))
    states = []
    for _ in range(creation.PARK_ATTEMPTS):
        pending_pass(world, model)
        states.append([world.service.journal.get(operation_id)['state'] for operation_id in ids])
    assert states == [['parked', 'parked'], ['parked', 'parked'], ['failed', 'failed']], states
    assert world.service.retry(ids[0])['state'] == 'queued'
    assert world.service.run(ids[0])['state'] == 'archived', 'the retried note is back in the nightly queue'
    late = capture(world, notes(3)[2:])
    asked = len(model.inner.jobs)
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) for job in model.inner.jobs[asked:] if 'sources' in job.payload] == [2], \
        'the retried note was settled inside a batch, not alone'
    assert sorted(report['parked']) == sorted([ids[0]] + late) and report['failed'] == [], report
    assert world.service.journal.get(ids[0])['state'] == 'parked'
    parked, charged, _ = creation._parked(world.service.trace(ids[0]))
    assert (parked, charged) == (1, 1), 'the retried member spent one attempt of its renewed budget'
    for _ in range(creation.PARK_ATTEMPTS - 1):
        pending_pass(world, model)
    assert world.service.journal.get(ids[0])['state'] == 'failed'


def pending_pass(world, model):
    from kelmo.creation import apply_pending
    return apply_pending(world.service, model, sleep=lambda _seconds: None)


def crash_with(message):
    def crash(self, *args, **kwargs):
        raise Died(message)
    return crash


def test_a_batch_refused_during_recovery_reports_no_member_applied(world, monkeypatch):
    """The window between the prepare and the commit, seen from the next pass.

    The process died with the batch intent prepared. The recovery of the next pass tries to publish it
    and refuses it, so only the publisher's row closes. Every other member must settle under its own
    attempt: none of them may report `applied` for a batch that committed nothing, and the pass must
    still run to its end instead of dying on a member row it mistook for the batch lead."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    monkeypatch.setattr(Editor, '_complete', crash_with('the process died before the commit'))
    with pytest.raises(Died):
        pending_pass(world, model)
    assert facts_of(world) == {}

    def refuse(self, operation_id):
        intent = self.journal.action_intent(operation_id)
        self._refuse(operation_id, intent['source_operation_id'], 'E_ORIGIN', 'the authority was lost')

    monkeypatch.setattr(Editor, '_complete', refuse)
    report = pending_pass(world, model)
    assert facts_of(world) == {}, 'nothing was published'
    assert not [operation_id for operation_id in ids if operation_id in report['applied']], report
    pending_pass(world, BatchModel(world))


def test_a_new_source_after_a_died_pass_keeps_its_proposal(world, monkeypatch):
    """A source that joins the pass after the one that died is settled from its own answer.

    The first pass recorded the batch answer and died before settling. The next pass takes a newer note
    of the same project into a batch of its own: it is consulted as the lead of its own answer, and must
    not be settled `archive-only` for being absent from the older batch. Its proposal reaches the wiki in
    this pass or the next one."""
    from kelmo.editor import Editor
    editor_mode(world)
    first = capture(world, notes(2))
    model = BatchModel(world)
    real = Editor._settle_pass
    monkeypatch.setattr(Editor, '_settle_pass', crash_with('the process died after the answer'))
    with pytest.raises(Died):
        pending_pass(world, model)
    monkeypatch.setattr(Editor, '_settle_pass', real)
    late = capture(world, ['App note 9: the late source records its own reading after the audit.'])
    pending_pass(world, model)
    pending_pass(world, model)
    assert world.outcome(late[0])['disposition'] == 'applied', world.outcome(late[0])
    assert len(facts_of(world)) == len(first) + 1, 'every note has its fact'


class BogusLead(BatchModel):
    """The batch replay, with a receipt the journal never issued for the lead: settle refuses the lead alone."""

    revises = False

    def execute(self, job):
        outcome = super().execute(job)
        if 'sources' in job.payload:
            lead = job.payload['sources'][0]['operation_id']
            for check in outcome.result['fact_checks']:
                if check['source'] == lead:
                    check['verified_match'] = {'result': 'not_found', 'search_id': 'never-issued'}
        return outcome


def test_a_crash_after_the_commit_when_the_publisher_is_not_the_lead(world, monkeypatch):
    """The batch publishes on the row of its earliest accepted source, which need not be its lead.

    The lead's own fact check fails, so the two members publish and the process dies while the second one
    settles. The next pass must see that the batch committed even though the lead's row is still open, and
    end every member whose items were committed `applied` without consulting the model again."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BogusLead(world)
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        pending_pass(world, model)
    assert len(facts_of(world)) == 2, 'the two accepted members were committed before the crash'
    calls = len(model.jobs)
    monkeypatch.setattr(Editor, '_member_disposition', real)
    report = pending_pass(world, model)
    assert world.outcome(ids[1])['disposition'] == 'applied'
    assert world.outcome(ids[2])['disposition'] == 'applied', world.outcome(ids[2])
    assert ids[2] not in report['parked'], report
    assert len(model.jobs) == calls, 'the published members paid no model call'


def test_a_crash_after_a_precheck_that_wrote_no_slice_keeps_the_published_member(world, monkeypatch):
    """The batch whose precheck refused before splitting records no slice in any member row.

    The settle split still knows each owner and publishes two of the three, and the process dies while the
    second one settles. The next pass has only each row's own answer, which holds no slice, so it must read
    what the batch published from the stored intent instead of calling the member a note that added nothing."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = CollidingFirst(world)
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        pending_pass(world, model)
    monkeypatch.setattr(Editor, '_member_disposition', real)
    pending_pass(world, model)
    assert world.outcome(ids[1])['disposition'] == 'applied', world.outcome(ids[1])


def test_a_held_source_is_not_overtaken_in_its_own_pass(world):
    """A batch that cannot claim its head leaves its project behind that head, as the single path does.

    The middle note is held by another attempt. The first note still publishes, and the pass must not take
    the newer note of the same project into a batch of its own: a batch never passes an older note of its
    own project, and that rule holds across the batches of one pass, not only inside one batch."""
    editor_mode(world)
    ids = capture(world, notes(3))
    held = world.service.journal.claim(ids[1], lease_seconds=600)
    try:
        report = pending_pass(world, BatchModel(world))
    finally:
        world.service.journal.release(ids[1], held)
    assert ids[0] in report['applied'], report
    assert ids[2] not in report['applied'], 'the newer note published before the older held one'


def test_a_page_item_with_only_a_source_is_refused_as_on_the_single_path():
    """The batch's own attribution is not a change a page item can be made of.

    `normalize` counted `source_operation_id` among the fields a page change carries, so a page item naming
    nothing but its source passed the shape check and was dropped as a no-op, while the same item on the
    single-source path is refused. Both paths refuse it."""
    from kelmo.editor import ProposalError, normalize
    registry = registry_with(facts=[], pages=page_entry())
    target = next(iter(registry['pages']))
    item = dict(target=target, expected_revision=registry['pages'][target]['revision'])
    with pytest.raises(ProposalError):
        normalize({'pages': [item]}, [target], registry, rule_change=False, project='app')
    with pytest.raises(ProposalError):
        normalize({'pages': [dict(item, source='op-1')]}, [target], registry, rule_change=False, project='app',
                  sources={'op-1': {'rule_change': False}})


def test_a_budget_refusal_keeps_its_numbers_and_its_repair():
    """A budget refusal is read from the stored reason, so the reason keeps what it measured.

    The number of pages named with their numbers was fixed at four, and with two ordinary paths in front of
    them the reason passed the 400-character limit: the stored text was cut, and the repair sentence and the
    count of the pages that were not named went with it. The detail is now sized to the room the reason has,
    so however many pages were omitted the numbers it names, the count of the rest and the repair are there."""
    from kelmo import outcomes
    from kelmo.editor import _budget_refusal
    omitted = [{'target': 'projects/payments/export-queue-rework-%d.md' % index, 'needs_tokens': 12345,
                'left_tokens': 6789} for index in range(6)]
    stored = outcomes.repair_reason('E_BUDGET', _budget_refusal(omitted, 200000, 190000))
    assert not stored.endswith('...'), stored
    assert '12345' in stored and 'more omitted' in stored and 'Requeue' in stored, stored


CHILD = 'projects/app/export-retry-log.md'


class MissingParent(BatchModel):
    """The batch replay, whose answer hangs a new page from a page the batch never sent.

    `parent` is a registered page of this project that is not among the pages sent in full, which is the
    refusal the editor can repair by itself: it sends that page with the next round instead of spending
    one of the batch's expansions."""

    def __init__(self, world, parent):
        super().__init__(world)
        self.parent = parent

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        facts, checks = [], []
        for block in payload['sources']:
            text = block['text'].split('. ')[0]
            key = payload['project'] + '.replay-' + block['evidence']['content_sha256'][:10]
            fact = dict(fact_key=key, target=CHILD, text=text, status='active', expected_revision=None,
                        source=block['operation_id'])
            checks.append({'fact_key': key, 'source': block['operation_id'],
                           'verified_match': {'result': 'not_found',
                                              'search_id': self.world.service.reconcile(text, CHILD)['search_id']}})
            facts.append(fact)
        page = dict(target=CHILD, expected_revision=None, page_id='export-retry-log', layout='topic',
                    title='Export retry log', summary='How the retry log is kept.', source=facts[0]['source'])
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks,
                                         'pages': [dict(page, parent=self.parent)]})


def test_a_batch_refusal_for_a_missing_parent_sends_that_page_and_publishes_the_repair(world):
    """The batch path gets the same missing-parent repair the single-source path has.

    The first answer hangs a new page from a registered page the batch did not send. The refusal names
    that page and the consult loop sends it itself, so the batch keeps both of its expansions and the
    second answer creates the page under it. Creating a page needs the controller enrollment, so the pass
    runs on a service that carries it, exactly as a bound vault's dreaming pass does."""
    from kelmo.editor import MAX_EXPANSIONS
    from kelmo.creation import apply_pending
    from kelmo.service import Service
    from test_editor_scoping import OTHER
    editor_mode(world)
    ids = capture(world, notes(2))
    model = counted(MissingParent(world, OTHER))
    service = Service(world.root, bindings=world.bindings, context_bindings=world.contexts())
    try:
        report = apply_pending(service, model)
    finally:
        service.close()
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2, 2], \
        'the repair ran inside the same batch, not as two single-source attempts'
    refusal = model.inner.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and OTHER in refusal['reason']
    assert set(refusal) == {'code', 'reason'}, 'the model reads a refusal, not the editor internals'
    assert OTHER in [page['target'] for page in model.inner.jobs[1].payload['pages']], \
        'the page the refusal named is sent with its entities'
    assert model.inner.jobs[1].payload['expansions_left'] == MAX_EXPANSIONS, \
        'the editor sent the page itself: the batch kept its expansions'
    registry = world.registry()
    assert registry['pages'][CHILD]['parent'] == OTHER
    assert len(facts_of(world)) == 2, 'each note kept its own fact on the new page'


class UnenrolledFeature(BatchModel):
    """The batch replay, whose answer creates a feature page the controller enrollment does not authorize.

    `revises = False` keeps that answer as the recorded one, so the refusal is met at settle rather than
    sent back for a repair round the model would only repeat."""

    revises = False

    def execute(self, job):
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload:
            return answer
        answer.result['pages'] = [dict(target=CHILD, expected_revision=None, page_id='export-retry-log',
                                       layout='feature', title='Export retry log',
                                       summary='How the retry log is kept.', parent=payload['pages'][0]['target'],
                                       source=payload['sources'][0]['operation_id'])]
        return answer


def test_a_batch_whose_creation_the_enrollment_refuses_parks_every_member_uncharged(world):
    """A batch meets the enrollment refusal the single-source path meets, and it costs no note its attempts.

    `E_BINDING` is one of the refusals the vault itself causes, so a batch refused for it parks its
    members without spending any of their attempts, commits nothing, and the same notes publish on the
    next pass under a model that asks for nothing the enrollment withholds."""
    from kelmo import creation
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    report = apply_pending(world.service, counted(UnenrolledFeature(world)))
    assert report['applied'] == [] and report['failed'] == [], report
    assert sorted(report['parked']) == sorted(ids), report
    assert facts_of(world) == {}, 'nothing was committed'
    assert CHILD not in world.registry()['pages']
    assert all('E_BINDING' in world.outcome(operation_id)['reason'] for operation_id in ids), \
        [world.outcome(operation_id) for operation_id in ids]
    assert all(creation._parked(world.service.trace(operation_id))[1] == 0 for operation_id in ids), \
        'the vault-caused refusal spent none of the members attempts'
    assert sorted(waiting_ids(world)) == sorted(ids), 'the notes stay in the queue'
    assert apply_pending(world.service, counted(BatchModel(world)))['applied'] == ids


def test_a_batch_that_committed_without_storing_its_receipt_recovers_with_no_model_call(world, monkeypatch):
    """The window between the commit and the receipt: the next pass finishes it from the journal.

    `publish_semantic` committed the batch and the process died before the publisher's receipt was
    stored, so its row is still prepared while the wiki already holds the facts. The recovery every
    pass runs recognizes the committed generation and closes the row; no member is re-consulted and no
    note pays a model call for a publication that already happened."""
    from kelmo.creation import apply_pending
    from kelmo.journal import Journal
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    real, armed = Journal.action_finish, [True]

    def crash(self, *args, **kwargs):
        if armed[0]:
            armed[0] = False
            raise Died('the process died after the commit, before the receipt was stored')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Journal, 'action_finish', crash)
    with pytest.raises(Died):
        apply_pending(world.service, model)
    assert len(facts_of(world)) == 3, 'the commit landed before the crash'
    assert world.service.journal.prepared_semantic(), 'the publisher row is still prepared'
    calls = len(model.inner.jobs)
    monkeypatch.setattr(Journal, 'action_finish', real)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert len(model.inner.jobs) == calls, f'the recovery paid {len(model.inner.jobs) - calls} more model calls'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    assert len(facts_of(world)) == 3, 'the committed facts are not published twice'


# ---------------------------------------------------------------------------
# The per-project ready pass: `kelmo dream apply --ready`
#
# `[editor] batch_ready_sources` decides which projects a pass touches at all: a project joins the pass
# when at least that many of its notes wait for their first pass. Inside a ready project the pass still
# works oldest first, in batches of at most `batch_max_sources`, and stops when fewer than
# `batch_ready_sources` first-pass notes of that project remain. `dream status --json` answers the same
# two questions without starting a broker, so a scheduler can skip a tick it would publish nothing in.
# ---------------------------------------------------------------------------


def ready_pass(world, model, limit=100):
    from kelmo.creation import apply_pending
    return apply_pending(world.service, model, ready=True, sleep=lambda _seconds: None, limit=limit)


def test_batch_ready_sources_defaults_to_batch_max_sources_and_refuses_a_bad_value(world):
    """The readiness number is its own setting, defaulting to the batch ceiling and validated beside it."""
    from kelmo.config import DEFAULT_BATCH_MAX_SOURCES, Config
    from kelmo.errors import KelmoError
    editor_mode(world, max_sources=5)
    assert world.service.config.batch_ready_sources == 5
    assert world.service.config.as_dict()['editor']['batch_ready_sources'] == 5
    editor_mode(world, max_sources=5, ready=11)
    assert world.service.config.batch_ready_sources == 11, 'ready is independent of the batch ceiling'
    editor_mode(world)
    assert world.service.config.batch_ready_sources == DEFAULT_BATCH_MAX_SOURCES
    for value in ('0', '-3', '"8"'):
        editor_table(world, ready=value)
        with pytest.raises(KelmoError, match=r'\[editor\].*batch_ready_sources.*positive integer'):
            Config.load(world.root)
    editor_mode(world)


def test_a_ready_pass_publishes_the_ready_project_and_names_the_other_as_waiting(world):
    """Eight notes of one project make it ready; two of another project wait for a pass of their own."""
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert sorted(report['applied']) == sorted(app), 'only the ready project published'
    assert report['waiting'] == {'ops': 2}, 'the pass names what it left'
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [8], 'one batch for one ready project'
    assert waiting_ids(world) == ops, 'the two notes of the other project still wait'
    assert (report['parked'], report['failed'], report['skipped'], report['deferred']) == ([], [], [], [])


def test_ready_projects_run_in_the_order_of_their_oldest_waiting_note(world):
    editor_mode(world)
    ops = capture(world, notes(8, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    app = capture(world, notes(8))
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert [[job.payload['project'], len(job.payload['sources'])] for job in model.inner.jobs] == \
        [['ops', 8], ['app', 8]], 'both ready projects ran, oldest waiting note first'
    assert sorted(report['applied']) == sorted(ops + app) and report['waiting'] == {}


def test_a_ready_pass_takes_batches_of_the_ceiling_and_leaves_what_is_below_the_number(world):
    """17 notes at 8 and 8: two batches and one note left. 10 notes at ready 10 and 8 per batch: one batch, two left."""
    editor_mode(world)
    capture(world, notes(17))
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [8, 8]
    assert len(report['applied']) == 16 and report['waiting'] == {'app': 1}
    assert len(waiting_ids(world)) == 1, 'what is left waits for the next pass'
    editor_mode(world, ready=10, max_sources=8)
    capture(world, notes(9, start=17))
    second = counted(BatchModel(world))
    report = ready_pass(world, second)
    assert [len(job.payload['sources']) for job in second.inner.jobs] == [8], \
        'a ready project still stops at the batch ceiling'
    assert len(report['applied']) == 8 and report['waiting'] == {'app': 2}
    assert len(waiting_ids(world)) == 2


def test_a_ready_pass_starts_no_batch_and_no_model_call_when_no_project_reaches_the_number(world):
    editor_mode(world, ready=10)
    capture(world, notes(9))
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert model.inner.jobs == [] and model.calls == 0, 'nothing ran: no model call was paid'
    assert (report['applied'], report['parked'], report['failed'], report['skipped'],
            report['deferred']) == ([], [], [], [], [])
    assert report['waiting'] == {'app': 9} and len(waiting_ids(world)) == 9


def test_a_project_that_becomes_ready_only_after_the_pass_began_stays_out_of_the_pass(world, monkeypatch):
    """Readiness is counted once, from the notes captured before the pass began."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(8))
    # The pass begins at the fourth note: only the four up to it were captured before it began.
    monkeypatch.setattr('kelmo.creation.journal_now', lambda: world.service.journal.get(ids[3])['created_at'])
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model, ready=True, sleep=lambda _seconds: None)
    assert model.inner.jobs == [], 'four first-pass notes of eight do not make the project ready'
    assert report['waiting'] == {'app': 4}
    assert waiting_ids(world) == ids


def test_a_parked_note_of_a_ready_project_goes_first_and_counts_towards_nothing(world):
    """Parked notes keep their place in capture order; only first-pass notes make a project ready."""
    editor_mode(world)
    parked = capture(world, notes(1))[0]
    world.service.journal.stage(parked, 'parked', error_code='E_PROPOSAL', phase='editor')
    first_seven = capture(world, notes(7, start=1))
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert model.inner.jobs == [], 'seven first-pass notes and one parked note are not a ready project'
    assert report['waiting'] == {'app': 8}
    later = capture(world, notes(1, start=8))
    second = counted(BatchModel(world))
    report = ready_pass(world, second)
    assert [block['operation_id'] for block in second.inner.jobs[0].payload['sources']] == \
        [parked] + first_seven, 'the parked note keeps its place and goes with its batch'
    assert sorted(report['applied']) == sorted([parked] + first_seven)
    assert report['waiting'] == {'app': 1} and later[0] in waiting_ids(world)


def test_a_plain_apply_pass_still_drains_every_project(world):
    """Without the flag the pass is the one it was: every waiting note of every project is taken."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model, sleep=lambda _seconds: None)
    assert sorted(report['applied']) == sorted(app + ops)
    assert report['waiting'] == {} and waiting_ids(world) == []
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [8, 2]


def test_ready_is_an_option_of_dream_apply_and_of_no_other_dream_action():
    """`--ready` belongs to `dream apply`; `dream run` keeps draining everything (item 7)."""
    from kelmo.interface import UsageError, build_parser
    parser, _commands = build_parser('test')
    assert parser.parse_args(['dream', 'apply', '--ready']).ready is True
    assert parser.parse_args(['dream', 'apply']).ready is False, 'plain apply still drains everything'
    assert not hasattr(parser.parse_args(['dream', 'run']), 'ready'), 'run keeps the pass it had'
    with pytest.raises(UsageError):
        parser.parse_args(['dream', 'run', '--ready'])


def test_dream_apply_passes_ready_to_the_pass_and_run_does_not(world, tmp_path, capsys, monkeypatch):
    """The CLI's `--ready` reaches the engine: `dream apply` runs the ready pass, `dream run` the plain one."""
    from kelmo import creation
    from kelmo.cli import call
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    asked, real = [], creation.apply_pending

    def record(service, executor, **kwargs):
        asked.append(bool(kwargs.get('ready')))
        return dict(real(service, executor, **dict(kwargs, sleep=lambda _seconds: None)))

    monkeypatch.setattr(creation, 'apply_pending', record)
    script, config = tmp_path / 'executor.py', tmp_path / 'executor.json'
    script.write_text(
        'import json,sys\njob=json.load(sys.stdin)\n'
        "if job['task']=='dream':\n    print(json.dumps({'findings': []}))\n    raise SystemExit(0)\n"
        "print(json.dumps({'facts': [], 'fact_checks': []}))\n")
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))

    def invoke(*args):
        code = call(['--vault', str(world.root), 'dream', *map(str, args), '--json'])
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        return output['result']

    invoke('configure', '--executor-config', config, '--apply')
    invoke('resume')
    ready = invoke('apply', '--ready')
    assert sorted(ready['apply']['applied']) == sorted(app) and ready['apply']['waiting'] == {'ops': 2}, \
        'the ready pass took the project at 8 and named the other as waiting'
    assert sorted(invoke('apply')['apply']['applied']) == sorted(ops), 'plain apply drains the rest'
    assert invoke('run')['apply']['waiting'] == {}
    assert asked == [True, False, False], 'only `apply --ready` asked for the ready pass'


def test_ready_without_batch_apply_is_refused_naming_the_setting(world, tmp_path, capsys):
    from kelmo.cli import call
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=False)
    with pytest.raises(KelmoError) as error:
        ready_pass(world, counted(BatchModel(world)))
    assert error.value.code == 'E_CONFIG'
    assert 'batch_apply' in str(error.value), str(error.value)
    script, config = tmp_path / 'executor.py', tmp_path / 'executor.json'
    script.write_text("import json\nprint(json.dumps({'findings': []}))\n")
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))
    def invoke(*args):
        code = call(['--vault', str(world.root), 'dream', *map(str, args), '--json'])
        return code, json.loads(capsys.readouterr().out)

    assert invoke('configure', '--executor-config', config, '--apply')[0] == 0
    assert invoke('resume')[0] == 0
    code, output = invoke('apply', '--ready')
    assert (code, output['error']['code']) == (1, 'E_CONFIG'), output
    assert 'batch_apply' in output['error']['message']


def test_dream_status_reports_waiting_and_ready_and_starts_no_broker(world, tmp_path, capsys, monkeypatch):
    """The scheduler's cheap question (item 8): what waits, and which projects are ready now.

    `dream status` answers it from the journal alone, so a tick that would publish nothing costs no
    broker start and no model load."""
    from kelmo.cli import call
    editor_mode(world, ready=8)
    capture(world, notes(8))
    capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])

    def forbidden(*args, **kwargs):
        raise AssertionError('dream status reads the journal alone: no broker, no model load, no provider')

    monkeypatch.setattr('kelmo.broker_launcher.run', forbidden)
    monkeypatch.setattr('kelmo.store.memory.embed.get_model', forbidden)
    monkeypatch.setattr('kelmo.executor.provider_execute', forbidden)
    script, config = tmp_path / 'executor.py', tmp_path / 'executor.json'
    script.write_text("import json\nprint(json.dumps({'findings': []}))\n")
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))
    assert call(['--vault', str(world.root), 'dream', 'configure', '--executor-config', str(config),
                 '--apply', '--json']) == 0
    capsys.readouterr()
    code = call(['--vault', str(world.root), 'dream', 'status', '--json'])
    result = json.loads(capsys.readouterr().out)['result']
    assert code == 0, result
    assert result['waiting'] == {'app': 8, 'ops': 2}, 'first-pass notes per project'
    assert result['ready'] == ['app'], 'only the project that reached the number is ready'
    assert result['ready_at'] == 8, 'the number the readiness was counted against'
    assert result['settings']['max_sources'] > 0 and 'paused' in result, 'the status it already gave is intact'
    assert len(waiting_ids(world)) == 10, 'status publishes nothing'


def test_a_ready_project_whose_oldest_note_is_held_is_skipped_and_the_next_ready_project_runs(world):
    """Item 9: no newer note of a project is published ahead of the one another attempt holds."""
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(8, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    held = world.service.journal.claim(app[0], lease_seconds=600)
    model = counted(BatchModel(world))
    try:
        report = ready_pass(world, model)
    finally:
        world.service.journal.release(app[0], held)
    assert not [operation_id for operation_id in app[1:] if operation_id in report['applied']], \
        'a newer note of that project published ahead of the held one'
    assert sorted(report['applied']) == sorted(ops), 'the other ready project still ran'
    assert [job.payload['project'] for job in model.inner.jobs] == ['ops'], 'no batch of the held project'
    assert report['waiting'] == {'app': 8} and len(waiting_ids(world)) == 8
    assert [item['operation_id'] for item in report['skipped']] == app


def test_a_ready_project_whose_oldest_note_is_deferred_is_skipped_for_the_rest_of_the_pass(world):
    """Item 9: a quota deferral of the head holds its project the way a held claim does.

    Today only the head lands in `deferred`, so the next note of that project becomes the head of the
    next batch and is published ahead of the older one."""
    import time
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(8, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    world.service.journal.event(app[0], 'editor_deferred', reset_at=time.time() + 600)
    model = counted(BatchModel(world))
    report = ready_pass(world, model)
    assert report['deferred'] == [app[0]]
    assert not [operation_id for operation_id in app[1:] if operation_id in report['applied']], \
        'the notes behind a deferred head overtook it in the same pass'
    assert sorted(report['applied']) == sorted(ops)
    assert [job.payload['project'] for job in model.inner.jobs] == ['ops']
    assert report['waiting'] == {'app': 8} and len(waiting_ids(world)) == 8
    assert [item['operation_id'] for item in report['skipped']] == app[1:]


def test_a_ready_project_whose_oldest_note_defers_during_the_consultation_is_held_back(world):
    """Item 9, the third shape: the head met a quota signal inside its own consultation.

    Step 8 changed this case on purpose: the head opens its work page and now joins the batch of its
    project, so the quota signal meets the batch's own consultation. The guarantee is the one kelmo-11
    pinned: a project whose head met a quota signal is held back for the rest of the pass, and none of
    the notes behind it is published in that pass.
    """
    import time
    from kelmo import quota
    editor_mode(world)
    work = world.service.create('App note: rework the export queue.', project='app', selected=[FEATURE],
                                new_work={'slug': 'export-queue', 'title': 'Rework the export queue'})
    world.service.run(work['id'])
    app = capture(world, notes(7, start=1))
    ops = capture(world, notes(8, 'ops'), project='ops', selected=['projects/ops/runbook.md'])

    class QuotaOnApp(BatchModel):
        def execute(self, job):
            if job.payload['project'] == 'app':
                raise quota.QuotaWait(time.time() + 600, 'login')
            return super().execute(job)

    model = counted(QuotaOnApp(world))
    report = ready_pass(world, model)
    assert sorted(report['deferred']) == sorted(app + [work['id']]), report
    assert not [operation_id for operation_id in app if operation_id in report['applied']], \
        'the notes behind a deferred head went into a batch of their own'
    assert sorted(report['applied']) == sorted(ops)
    assert [job.payload['project'] for job in model.inner.jobs] == ['ops']
    assert report['waiting'] == {'app': 8} and len(waiting_ids(world)) == 8
    assert report['skipped'] == []


# ---------------------------------------------------------------------------
# A batch answer can name a source as already recorded
#
# `recorded` is the batch's way of saying "this note states nothing new" without leaving the note with no
# item and no reason: the entry names the source and the fact keys that already state what it says. Its
# shape is checked by `normalize` like every other part of a batch answer, so a malformed entry goes back
# through the repair path instead of settling a note on a name the editor cannot read.
# ---------------------------------------------------------------------------


class RecordedAnswer(BatchModel):
    """The batch replay whose proposal carries a `recorded` entry.

    `first` is what the first answer names; once the editor refuses it, the replay names a source of this
    batch with a non-empty key list, which is the shape the field allows. A single-source payload is
    answered the way the replay answers it, so a source published alone still publishes."""

    def __init__(self, world, first):
        super().__init__(world)
        self.first = first

    def execute(self, job):
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        if 'refusal' in payload:
            answer.result['recorded'] = [{'source': payload['sources'][-1]['operation_id'],
                                          'fact_keys': ['app.replayed-fact']}]
        else:
            answer.result['recorded'] = self.first
        return answer


def test_a_batch_answer_with_a_malformed_recorded_entry_goes_to_repair(world):
    """A `recorded` entry naming a source this batch never sent, or naming no key, is refused whole.

    The refusal reaches the model as the batch's own repair and the second answer, naming a real source
    with its keys, publishes the batch — the same repair round a malformed fact gets."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    model = counted(RecordedAnswer(world, [{'source': 'op-this-batch-never-sent', 'fact_keys': []}]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2, 2], \
        'the malformed entry cost a repair round, not a park'
    refusal = model.inner.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and 'recorded' in refusal['reason'], refusal
    assert set(refusal) == {'code', 'reason'}, 'the model reads a refusal, not the editor internals'
    assert len(facts_of(world)) == 2, 'the repaired answer published both notes'


def test_recorded_is_shape_checked_for_a_batch_and_ignored_by_a_single_source_answer():
    """The field belongs to the batch path: a batch answer may name only a source it sent, with keys.

    A well-shaped entry changes nothing of the delta it rides with; each malformed one is refused with a
    message naming `recorded`, so the consult loop can send it back. A single-source answer keeps its
    present shape and ignores the field."""
    from kelmo.editor import ProposalError, normalize
    registry = registry_with(facts=[fact_entry()], pages=page_entry())
    scope, sources = ['projects/app/notes.md'], {'op-one': {'rule_change': False},
                                                 'op-two': {'rule_change': False}}
    answer = {'facts': [fact_change(source='op-one')],
              'fact_checks': [check_entry('app.notes-scope', source='op-one')]}
    delta, checks = normalize({**answer, 'recorded': [{'source': 'op-two', 'fact_keys': ['app.other-fact']}]},
                              scope, registry, rule_change=False, project='app', sources=sources)
    assert len(delta['facts']) == 1 and len(checks) == 1, 'the entry is not a change of its own'
    for bad in ([{'source': 'op-three', 'fact_keys': ['app.other-fact']}],
                [{'source': 'op-two', 'fact_keys': []}],
                [{'source': 'op-two', 'fact_keys': ['app.other-fact', '']}],
                [{'source': 'op-two'}],
                [{'source': 'op-two', 'fact_keys': 'app.other-fact'}],
                [{'source': 'op-two', 'fact_keys': ['app.other-fact'], 'reason': 'already stated'}],
                [{'source': 'op-two', 'fact_keys': ['app.other-fact']},
                 {'source': 'op-two', 'fact_keys': ['app.another-fact']}],
                ['op-two'],
                {'source': 'op-two'}):
        with pytest.raises(ProposalError) as error:
            normalize({**answer, 'recorded': bad}, scope, registry, rule_change=False, project='app',
                      sources=sources)
        assert 'recorded' in str(error.value), bad
    plain = {'facts': [fact_change()], 'fact_checks': [check_entry('app.notes-scope')]}
    delta, checks = normalize(dict(plain, recorded=[{'source': 'op-two', 'fact_keys': ['app.other-fact']}]),
                              scope, registry, rule_change=False, project='app')
    assert len(delta['facts']) == 1 and len(checks) == 1, 'the single-source answer ignores the field'


# ---------------------------------------------------------------------------
# Every source of a batch is accounted for before anything is prepared
#
# A batch answer that carries an item for only some of its notes used to prepare anyway, and the notes it
# left silent settled `archive-only` with a reason the editor invented. Before the batch is prepared, each
# source now has to hold an item of its own or a `recorded` entry whose keys are ones a reconciliation of
# this conversation returned; a source with neither goes back through the repair round the editor already
# has, naming the operation ids it left unanswered.
# ---------------------------------------------------------------------------


class ReconciledFirst(BatchModel):
    """The batch replay that reconciles one named source before it proposes anything.

    `text` and `target` are that source's own claim, so the receipt this conversation returns for it names
    the registered facts that already state what it says, and the proposal round reads those keys in its
    payload as `candidate_keys`.
    """

    def __init__(self, world, text, target):
        super().__init__(world)
        self.text, self.target, self.asked = text, target, False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and not self.asked:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': self.text, 'target': self.target}]})
        return super().execute(job)


class RecordedFromReceipt(ReconciledFirst):
    """The batch replay that answers two of three notes and names the third as recorded with its own keys.

    The first proposal gives `for_operation` no item and no `recorded` entry, which is the shape the
    production batches ended with; the repair round names that source with the candidate keys its own
    reconciliation returned, read from the payload the editor sent it.
    """

    def __init__(self, world, for_operation, text, target):
        super().__init__(world, text, target)
        self.for_operation = for_operation

    def execute(self, job):
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        answer.result['facts'] = [fact for fact in answer.result['facts']
                                  if fact['source'] != self.for_operation]
        answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                        if check['source'] != self.for_operation]
        if 'refusal' in payload:
            answer.result['recorded'] = [{'source': self.for_operation, 'fact_keys': [
                key for receipt in payload['reconciliations'] for key in receipt['candidate_keys']]}]
        return answer


def test_a_batch_of_3_with_items_for_two_names_the_third_in_a_repair_round(world):
    """The third note goes back in the same conversation and is accounted for, never left silent."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(RecordedFromReceipt(world, ids[2], notes(3, start=2)[0], FEATURE))
    before = commits(world)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3, 3, 3], \
        'the accounting was repaired inside the same batch'
    assert [len(job.payload['reconciliations']) for job in model.inner.jobs] == [0, 1, 1]
    refusal = model.inner.jobs[2].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and 'recorded' in refusal['reason'], refusal
    assert ids[2] in refusal['reason'], refusal
    assert set(refusal) == {'code', 'reason'}, 'the model reads a refusal, not the editor internals'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == \
        ['applied', 'applied', 'archive-only'], [world.outcome(operation_id) for operation_id in ids]
    assert commits(world) == before + 1, 'the accounted batch publishes in one commit'
    assert len(facts_of(world)) == 2, 'the named note published no fact of its own'
    assert 'app.replay-' + evidence_of(world, ids[2]) not in facts_of(world)


class RecordedWithForeignKeys(ReconciledFirst):
    """The batch replay that names a source with keys the conversation never returned.

    `keys` is one list per proposal round, so the caller can name a key that exists in no registry at all,
    then one the registry holds but this conversation never searched for, and only then answer in full.
    Each round that names such a key gives `for_operation` no item of its own.
    """

    def __init__(self, world, for_operation, text, target, keys):
        super().__init__(world, text, target)
        self.for_operation, self.keys, self.round = for_operation, list(keys), 0

    def execute(self, job):
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        if self.round < len(self.keys):
            answer.result['recorded'] = [{'source': self.for_operation, 'fact_keys': self.keys[self.round]}]
            answer.result['facts'] = [fact for fact in answer.result['facts']
                                      if fact['source'] != self.for_operation]
            answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                            if check['source'] != self.for_operation]
        self.round += 1
        return answer


def test_a_note_named_recorded_with_a_key_that_does_not_exist_counts_as_unaccounted(world):
    """A `recorded` entry accounts for its note only when its keys are ones this conversation returned.

    A key no registry holds is refused, and so is one the registry holds that this note never searched for:
    the keys must come from the receipts of this conversation, not from the model's memory of the wiki."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    model = counted(RecordedWithForeignKeys(world, ids[1], notes(2, start=1)[0], FEATURE,
                                            keys=[['app.note-states-nothing-here'], ['app.export']]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2, 2, 2, 2]
    assert 'app.export' in world.registry()['facts'], 'the key exists; no receipt of this batch named it'
    for index, key in enumerate(['app.note-states-nothing-here', 'app.export']):
        refusal = model.inner.jobs[index + 2].payload['refusal']
        assert refusal['code'] == 'E_PROPOSAL' and 'recorded' in refusal['reason'], refusal
        assert ids[1] in refusal['reason'] and key in refusal['reason'], refusal
        assert set(refusal) == {'code', 'reason'}, 'the model reads a refusal, not the editor internals'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied', 'applied']
    assert len(facts_of(world)) == 2, 'each note ended with its own fact'


# ---------------------------------------------------------------------------
# A source the repairs never accounted for is left out of the batch, not swallowed by it
#
# Two repair rounds is all a batch answer gets. When the model still leaves a note with no item and no
# `recorded` entry, the batch publishes the notes it did answer and names the rest in `alone`: their rows
# are ended the way a released lease is ended, they spend none of their attempts, and the same pass
# consults them again instead of settling them on a reason the editor invented.
# ---------------------------------------------------------------------------


class NeverAccounted(BatchModel):
    """The batch replay that gives `silent_for` no item and no `recorded` entry on every round.

    The shape the production batches ended with, kept through both repair rounds whatever the refusal
    says. A single-source payload is answered the way the replay answers it, so the note the batch left
    alone still publishes when the same pass consults it alone.
    """

    def __init__(self, world, silent_for):
        super().__init__(world)
        self.silent_for = set(silent_for)

    def execute(self, job):
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        answer.result['facts'] = [fact for fact in answer.result['facts']
                                  if fact['source'] not in self.silent_for]
        answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                        if check['source'] not in self.silent_for]
        return answer


def test_a_source_the_model_never_accounts_for_spends_no_attempt_and_the_other_two_publish_once(world):
    """The exhausted repair budget ends that note's turn instead of settling it `archive-only`."""
    from kelmo import creation
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(NeverAccounted(world, {ids[2]}))
    before = commits(world)
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    assert [len(job.payload['sources']) for job in batched] == [3, 3, 3], \
        'the batch spent both of its repair rounds on the note it never answered'
    assert len(model.inner.jobs) == 4, 'the unaccounted note got its own consultation from the same pass'
    assert 'sources' not in model.inner.jobs[-1].payload, 'it was consulted alone, not inside a batch'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    trace = world.service.trace(ids[2])
    assert [event for event in trace['events'] if event['stage'] == 'parked'] == [], \
        'the unaccounted source spends none of its attempts: it was never refused under its own key'
    assert creation._parked(trace)[1] == 0
    assert commits(world) == before + 2, 'the batch publishes its two in one commit, the alone note in its own'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    assert 'adds no new fact' not in world.outcome(ids[2])['reason'], world.outcome(ids[2])
    lead = member_rows(world, ids)[0]
    assert ids[2] in json.loads(world.service.journal.action_row(lead['operation_id'])['observation'])['alone'], \
        'the batch answer names the source it left unaccounted for'
    assert 'alone' not in json.loads(member_rows(world, ids)[2]['observation']), \
        'the alone naming belongs to the batch answer, not to the note it names'
    assert member_rows(world, ids)[2]['state'] == 'finished', \
        'the same attempt key published it: its row was ended open, not refused'
    park_key = StateCoordinator._request_key('semantic', world.service._operation_origin(ids[2]),
                                             f'create:{ids[2]}:1:park1')
    assert world.service.journal.action_request(park_key) is None, 'no new attempt was opened for it'


# ---------------------------------------------------------------------------
# A note the batch sent alone runs the single-source path, never in another batch of the same pass
#
# The batch that ran out of repairs ends the attempts of the notes nobody answered and the pass consults
# them again. That consultation is the single-source one: a note already spent one batch conversation,
# and a batch never passes an older note, so a later batch of the same pass would take the alone note as
# its head and spend a second conversation on it before publishing anything.
# ---------------------------------------------------------------------------


def test_a_note_sent_alone_after_its_batch_makes_a_real_model_call(world):
    """One batch of two, its unanswered note consulted alone, the third note after it: the pass's counts."""
    from kelmo.creation import apply_pending
    editor_mode(world, max_sources=2)
    ids = capture(world, notes(3))
    model = counted(NeverAccounted(world, {ids[1]}))
    before = commits(world)
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    alone = [job for job in model.inner.jobs if 'source' in job.payload]
    assert [len(job.payload['sources']) for job in batched] == [2, 2, 2], \
        'the batch spent its two repair rounds on the note it never answered, and no later batch took it'
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [2, 2, 2, 1, 1], \
        'the alone note and the third note each got their own consultation, in the pass that batched them'
    assert model.calls == 5, 'one batch conversation and two single-source ones'
    assert len(alone) == 2 and 'sources' not in alone[0].payload
    assert alone[0].payload['source']['evidence']['content_sha256'][:10] == evidence_of(world, ids[1]), \
        'the consultation the alone note got is its own, on its own evidence'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped'], report['deferred']) == \
        ([], [], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert commits(world) == before + 3, 'the batch, the alone note and the third note: three commits'
    assert len(facts_of(world)) == 3, 'every note of the pass ended with a fact of its own'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    assert 'adds no new fact' not in world.outcome(ids[1])['reason'], world.outcome(ids[1])
    assert member_rows(world, ids)[1]['state'] == 'finished', \
        'the alone note published under the attempt key its batch opened'


# ---------------------------------------------------------------------------
# A source named as recorded settles with its own keys in its reason
#
# The accounting rule says a note the batch answered with no item of its own is a note whose facts the
# pages already hold, named by key. Its outcome has to say that: `archive-only` with a reason naming those
# fact keys, not the batch-wide text the notes nobody answered used to carry. A batch that settles its
# members `archive-only` gives each one a reason of its own, built from its own part of the answer.
# ---------------------------------------------------------------------------


def recorded_keys(job, text=None):
    """The candidate keys `job`'s payload names; with `text`, only those of the receipt whose text is `text`."""
    return [key for receipt in job.payload['reconciliations']
            if text is None or receipt['text'] == text for key in receipt['candidate_keys']]


def test_a_note_named_recorded_with_a_real_key_settles_archive_only_with_that_key(world):
    """The note a batch answered with a `recorded` entry names the keys it recorded in its own outcome."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(RecordedFromReceipt(world, ids[2], notes(3, start=2)[0], FEATURE))
    before = commits(world)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3, 3, 3], \
        'the accounting was repaired inside the same batch'
    keys = recorded_keys(model.inner.jobs[-1])
    assert keys, 'the round that named the note as recorded named the keys it read'
    outcome = world.outcome(ids[2])
    assert outcome['disposition'] == 'archive-only', outcome
    for key in keys:
        assert key in outcome['reason'], (key, outcome)
    assert 'adds no new fact' not in outcome['reason'], outcome
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == \
        ['applied', 'applied', 'archive-only'], [world.outcome(operation_id) for operation_id in ids]
    assert commits(world) == before + 1, 'the accounted batch publishes in one commit'
    assert len(facts_of(world)) == 2, 'the recorded note published no fact of its own'


class RecordedEveryNote(BatchModel):
    """The batch replay that proposes nothing and names every source as recorded, each with its own keys.

    `pairs` is one (text, target) per source in capture order: the reconcile round asks each source's own
    claim, so each receipt returns the keys that state it and each source records a list of its own. The
    proposal round carries no item at all, which is the shape that ends the whole batch `archive-only`.
    """

    def __init__(self, world, pairs):
        super().__init__(world)
        self.pairs, self.asked = list(pairs), False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and not self.asked and not payload['reconciliations']:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': text, 'target': target}
                                                        for text, target in self.pairs]})
        answer = super().execute(job)
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        answer.result['facts'] = []
        answer.result['fact_checks'] = []
        answer.result['recorded'] = [
            {'source': block['operation_id'],
             'fact_keys': [key for receipt in payload['reconciliations']
                           if receipt['text'] == self.pairs[index][0] for key in receipt['candidate_keys']]}
            for index, block in enumerate(payload['sources'])]
        return answer


def test_a_batch_where_every_note_is_recorded_gives_each_its_own_keys(world):
    """The batch-wide reason is per source: each recorded note names the keys it recorded, not its neighbour's."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    texts = ['Exports run nightly, as the feature page already states.',
             'Waters the desk plant on Mondays, which the person page already records.']
    ids = capture(world, texts)
    pairs = [(texts[0].split(',')[0], FEATURE), (texts[1].split(',')[0], 'person.md')]
    model = counted(RecordedEveryNote(world, pairs))
    before = commits(world)
    report = apply_pending(world.service, model)
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2, 2], model.inner.jobs
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert commits(world) == before, 'a batch that proposed nothing commits nothing'
    assert facts_of(world) == {}, 'the batch published no fact of its own'
    keys = [recorded_keys(model.inner.jobs[-1], text) for text, _ in pairs]
    assert all(keys) and keys[0] != keys[1], keys
    for index, operation_id in enumerate(ids):
        outcome = world.outcome(operation_id)
        assert outcome['disposition'] == 'archive-only', outcome
        for key in keys[index]:
            assert key in outcome['reason'], (index, key, outcome)
        assert 'adds no new fact' not in outcome['reason'], outcome
        for key in keys[1 - index]:
            assert key not in outcome['reason'], (index, key, outcome)


# ---------------------------------------------------------------------------
# The row that carries a batch's commit is a member with an item of its own
#
# The batch prepared its intent on the first source it accepted, which is its lead whenever the lead passed
# its own checks — and a lead whose slice holds no item carried the commit of notes it published nothing of.
# Its own row ended `applied` on the union's reason while its note published no fact, which is the shape the
# first production batches left: the lead of a batch of 7 reported `applied` with nothing published. The
# publisher is the first accepted member whose slice holds an item, and a source with no published item —
# the lead included — settles with a reason of its own, never `applied`.
# ---------------------------------------------------------------------------


def test_the_lead_of_a_batch_with_no_items_is_not_applied_and_the_publisher_has_items(world):
    """The note the batch published nothing of is not the row that carries the batch, and is not applied."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(RecordedFromReceipt(world, ids[0], notes(3, start=0)[0], FEATURE))
    before = commits(world)
    report = apply_pending(world.service, model)
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3, 3, 3], \
        [len(job.payload['sources']) for job in model.inner.jobs]
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == \
        ['archive-only', 'applied', 'applied'], [world.outcome(operation_id) for operation_id in ids]
    keys = recorded_keys(model.inner.jobs[-1])
    assert keys, 'the round that named the lead as recorded named the keys it read'
    outcome = world.outcome(ids[0])
    for key in keys:
        assert key in outcome['reason'], (key, outcome)
    assert 'adds no new fact' not in outcome['reason'], outcome
    assert commits(world) == before + 1, 'the batch publishes its two items in one commit'
    assert len(facts_of(world)) == 2, 'the lead published no fact of its own'
    assert 'app.replay-' + evidence_of(world, ids[0]) not in facts_of(world)
    rows = member_rows(world, ids)
    intents = [world.service.journal.action_intent(row['operation_id']) for row in rows]
    assert [intent is not None for intent in intents] == [False, True, False], \
        'the commit rides on the row of the first member with an item of its own'
    assert intents[1]['source_operation_id'] == ids[1], intents[1]
    assert intents[1]['batch'][0] == ids[1], intents[1]['batch']
    assert sorted(intents[1]['batch']) == sorted(ids), 'the accepted sources of the batch stay one commit'
    assert rows[0]['state'] == 'finished', 'the lead settles its own row with its own outcome'


# ---------------------------------------------------------------------------
# The crash after the commit, on the batch that ran out of repairs
#
# The batch that spent both repair rounds on a note nobody answered publishes the rest and names that note
# in `alone`, so its row is ended open and the pass consults it again. All of that is decided after the
# commit, in the member loop: a process that dies there leaves the commit landed, the published members'
# rows open and the unaccounted note's attempt unended. The next pass has only the rows to go by, so each
# member's own recorded answer must carry what the split needs — its slice, and the batch's naming of the
# source it left out — and the published members must end `applied` with no model call while the note
# nobody answered keeps its turn.
# ---------------------------------------------------------------------------


def test_a_crash_after_the_commit_with_repairs_exhausted_settles_the_published_member_applied(world, monkeypatch):
    """The repair budget ran out on the lead, the commit landed, and the process died in the member loop."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(NeverAccounted(world, {ids[0]}))
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        pending_pass(world, model)
    assert len(facts_of(world)) == 2, 'the two notes the answer accounted for were committed before the crash'
    calls = len(model.inner.jobs)
    monkeypatch.setattr(Editor, '_member_disposition', real)
    report = pending_pass(world, model)
    assert world.outcome(ids[1])['disposition'] == 'applied', world.outcome(ids[1])
    assert world.outcome(ids[2])['disposition'] == 'applied', world.outcome(ids[2])
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert 'adds no new fact' not in world.outcome(ids[0])['reason'], world.outcome(ids[0])
    assert [event for event in world.service.trace(ids[0])['events']
            if event['stage'] == 'parked'] == [], 'the note nobody answered spends none of its attempts'
    assert len(model.inner.jobs) == calls + 1, 'the published members paid no model call; the alone note got its own'
    assert len(facts_of(world)) == 3, 'every note of the pass ended with a fact of its own'


# ---------------------------------------------------------------------------
# A note whose request opened its work page joins its batch (step 8)
#
# The first production batches (2026-11-09) lost notes exactly this way: the model opened one work page
# and a note on a neighbouring topic got nothing, and a note whose own request carried `new_work` was
# sent down the single-source path, where a lost conversation leaves it with no fact, no narrative and no
# page. `_batchable` and `edit_batch` refused a stored `new_work`; this section is the batched guarantee:
# such a note joins its batch in capture order, and the payload names the page with the note that opened it.
# ---------------------------------------------------------------------------


class WorkPageBatch(BatchModel):
    """The batch replay, except the note with an opened work page proposes its open work onto that page.

    `for_note` holds the evidence prefixes of the notes whose creation requests carry `new_work`. In a
    batch payload the opened pages arrive as the `new_work` list, each naming the note whose request
    opened it, so this replay reads its target from there; a single-source payload names its own page as
    a dict and is answered with `single`, exactly as the single-source path answers it today.
    """

    def __init__(self, world, for_note, single):
        super().__init__(world)
        self.for_note, self.single = set(for_note), single

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        if 'sources' not in payload:
            return self.single(job)
        opened = payload.get('new_work')
        work = {item['source']: item['target'] for item in opened} if isinstance(opened, list) else {}
        facts, checks = [], []
        for block in payload['sources']:
            evidence = block['evidence']['content_sha256'][:10]
            text = block['text'].strip().split('. ')[0]
            target = work.get(block['operation_id'], payload['pages'][0]['target'])
            fact = dict(fact_key=payload['project'] + '.replay-' + evidence, target=target, text=text,
                        status='backlog' if block['operation_id'] in work else 'active',
                        expected_revision=None)
            check = {'fact_key': fact['fact_key'], 'source': block['operation_id'],
                     'verified_match': {'result': 'not_found',
                                         'search_id': self.world.service.reconcile(text, target)['search_id']}}
            fact['source'] = block['operation_id']
            facts.append(fact)
            checks.append(check)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


def test_a_note_with_a_stored_new_work_in_the_middle_of_a_batch_keeps_the_order(world):
    """A note captured with `--new-work` is batched with its neighbours, in capture order, and opens its page.

    Three plain notes, then the note whose creation request carries the work page it opened, then a plain
    note: the pass pays one conversation and one commit for all five, the opened page is created with the
    batch's commit, and the note in the middle of the order keeps its place in it.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    plain = capture(world, notes(3))
    work = world.service.create('App note: rework the export queue.', project='app', selected=[FEATURE],
                                new_work={'slug': 'export-queue', 'title': 'Rework the export queue'})
    world.service.run(work['id'])
    last = capture(world, notes(1, start=3))[0]
    ids = plain + [work['id'], last]

    def single(job):
        return world.propose(facts=[dict(fact_key='app.export-queue', target=target,
                                         text='The export queue is reworked.', status='backlog',
                                         expected_revision=None)])

    model = counted(WorkPageBatch(world, {evidence_of(world, work['id'])}, single))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 5]], 'a stored work page joins its batch: one conversation for the whole pass'
    batch = model.inner.jobs[0].payload['sources']
    assert [block['operation_id'] for block in batch] == ids, 'the batch keeps the capture order'
    assert batch[3]['evidence']['content_sha256'][:10] == evidence_of(world, work['id']), \
        'the note with the stored page sits in the middle of it'
    assert [(item['target'], item['source']) for item in model.inner.jobs[0].payload['new_work']] \
        == [(target, work['id'])], 'the payload names the opened page with the note whose request opened it'
    registry = world.registry()
    assert registry['pages'][target]['page_id'] == 'projects-app-export-queue'
    assert registry['pages'][target]['title'] == 'Rework the export queue'
    facts = facts_of(world)
    assert len(facts) == 5, 'every note of the batch published a fact of its own'
    assert facts['app.replay-' + evidence_of(world, work['id'])]['target'] == target
    assert all(fact['note_id'].startswith(key.split('.replay-')[1]) for key, fact in facts.items())
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 5


# ---------------------------------------------------------------------------
# A batch answer can ask for a work page per note (step 9)
#
# The single-source answer asks for the page it needs with `open_work`: one page, and no naming of the
# source, because the request has only one. A batch conversation answers for several notes at once, so its
# request is a list of pages, each naming the note it is opened for by operation id. This section is that
# request kind: its shape in `ModelOutcome`, its decoding in `decode_custom_outcome`, and that a page list
# the model shaped badly reaches the editor as a proposal to repair, where an E_PROPOSAL failure would park
# every member of the batch and not only the note whose page was malformed.
# ---------------------------------------------------------------------------


def work_page(slug, title, source, **rest):
    """One page of a batch's open-work answer: the slug and title the publisher opens it with, and the
    operation id of the note it is opened for, plus the optional summary and priority."""
    return {'slug': slug, 'title': title, 'source': source, **rest}


class BadPageList(BatchModel):
    """The batch replay, except its first answer asks for a work page that names no note.

    The page list is the batch's request kind, and a page naming no note is a shape the model can fix: the
    answer reaches the editor as a proposal, comes back as a repair round, and the second answer is the
    plain replay the batch publishes with.
    """

    def __init__(self, world):
        super().__init__(world)
        self.asked = False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload and not self.asked:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('proposal', {'open_works': [{'slug': 'export-queue',
                                                              'title': 'Rework the export queue'}]})
        return super().execute(job)


class PageListBatch(BatchModel):
    """The batch replay, except its first answer asks for one work page per note of the batch.

    The list is well shaped — every page names a source of the batch — so it is served as the request kind
    it is. The consultation that receives it opens its pages from each note's own request and from the
    answer's own list, and keeps every member.
    """

    def __init__(self, world, works):
        super().__init__(world)
        self.works, self.asked = works, False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload and not self.asked:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('open_works', {'works': self.works})
        return super().execute(job)


def test_a_batch_answer_opening_two_pages_decodes_into_one_request_per_note(world):
    """Two notes, two pages, one answer: each page names the note it is opened for.

    The batch answer object decodes to one request carrying both pages, a custom adapter's envelope decodes
    to the same answer object, and the single-source `open_work` keeps its own shape. A page list the model
    shaped badly — a page naming no note, a page naming a field it may not name, a page list that is no list
    of pages — is answered as a proposal, so the batch keeps its repair round.
    """
    from kelmo.creation import apply_pending
    from kelmo.executor import decode_custom_outcome, normalize_answer
    editor_mode(world)
    ids = capture(world, notes(2))
    works = [work_page('export-queue', 'Rework the export queue', ids[0], priority='H1'),
             work_page('nightly-audit', 'Nightly audit', ids[1], summary='The audit runs nightly.')]

    outcome = normalize_answer({'open_works': works})
    assert outcome.kind == 'open_works', outcome
    assert outcome.result['works'] == works, 'one request per note, each naming the note it is for'
    assert decode_custom_outcome({'kind': 'open_works', 'result': {'works': works}}) == {'open_works': works}
    # The single-source request keeps its shape: one page, and no note named.
    single = normalize_answer({'open_work': {'slug': 'export-queue', 'title': 'Rework the export queue'}})
    assert single.kind == 'open_work', single
    assert single.result == {'work': {'slug': 'export-queue', 'title': 'Rework the export queue'}}, single
    assert normalize_answer({'open_works': []}).result == {}, 'an empty request asks for nothing'

    for bad in ([work_page('export-queue', 'Rework the export queue', ids[0],
                           target='projects/app/export-queue.md')],
                [work_page('export-queue', 'Rework the export queue', ids[0], priority=1)],
                [{'slug': 'export-queue', 'title': 'Rework the export queue'}],
                'export-queue'):
        answer = normalize_answer({'open_works': bad})
        assert answer.kind == 'proposal', 'a badly shaped page list is the model to repair, not the batch to park'
        assert answer.result == {'open_works': bad}, answer

    model = counted(BadPageList(world))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 2], [True, 2]], 'the bad shape costs the batch a repair round, not its members'
    assert model.inner.jobs[1].payload['refusal']['code'] == 'E_PROPOSAL', model.inner.jobs[1].payload['refusal']
    assert len(facts_of(world)) == 2, 'the repaired answer published a fact for each note'

    # A page list that is well shaped is opened in the round it is asked for: the next round of the same
    # conversation carries every opened page, and no member of the batch is refused.
    later = capture(world, notes(2, start=2))
    asked = [work_page('export-queue', 'Rework the export queue', later[0]),
             work_page('nightly-audit', 'Nightly audit', later[1])]
    pages = counted(PageListBatch(world, asked))
    report = apply_pending(world.service, pages)
    assert sorted(report['applied']) == sorted(later), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in pages.inner.jobs] \
        == [[True, 2], [True, 2]], 'a page list is opened in the round it is asked for, spending no repair'
    opened = [(item['target'], item['source']) for item in pages.inner.jobs[1].payload['new_work']]
    assert opened == [('projects/app/export-queue.md', later[0]), ('projects/app/nightly-audit.md', later[1])], \
        'the next round names each opened page with the note it was opened for'
    assert len(facts_of(world)) == 4, 'the notes of the page-list batch each published a fact of their own'


# ---------------------------------------------------------------------------
# The consultation opens every page a batch answer names, in one round (step 10)
#
# Step 9 gave the batch its page-list answer kind; a consultation that received it answered with a repair,
# so the pages were opened one at a time by the older single-page ask, which is where the production loss
# came from: the batch opened one work page and a note on a neighbouring topic got nothing. This section is
# the handling — every page of the list opened in the round it is asked for, each parent joining the
# selection — and the rule that keeps it one page per note: a second request for a note that already has
# its page, or one naming a note outside the batch, is refused and repaired, never opened twice.
# ---------------------------------------------------------------------------


class PageListAsks(WorkPageBatch):
    """The batch replay, except its first answers are the page lists the caller names, in order.

    Each list is served as the request kind it is; once the payload carries opened pages the replay puts
    each note's fact on its own page, as `WorkPageBatch` does. A list asked after the pages are open is
    answered by the replay, so a case measures the round that follows the opening.
    """

    def __init__(self, world, asks):
        super().__init__(world, set(), None)
        self.asks, self.rounds = list(asks), 0

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and not payload.get('new_work') and self.rounds < len(self.asks):
            works = self.asks[self.rounds]
            self.rounds += 1
            self.jobs.append(job)
            return ModelOutcome('open_works', {'works': works})
        return super().execute(job)


def test_two_notes_each_opening_a_different_page_in_one_answer_use_one_round(world):
    """Two notes, two pages, one answer, one round: each note gets its own page and its own fact.

    The pages are opened in the round the answer asks for them, so the batch pays two conversations for
    the pair (the ask, then the proposal that lands on the opened pages), each note's fact is on the page
    opened for it, and the parent of each opened page is sent with the round that follows.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    works = [work_page('export-queue', 'Rework the export queue', ids[0]),
             work_page('nightly-audit', 'Nightly audit', ids[1])]
    model = counted(PageListAsks(world, [works]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 2], [True, 2]], 'one round opens every page the answer named: the batch pays no repair'
    opened = model.inner.jobs[1].payload['new_work']
    assert [(item['target'], item['source']) for item in opened] == [
        ('projects/app/export-queue.md', ids[0]), ('projects/app/nightly-audit.md', ids[1])], opened
    assert 'projects/app/app.md' in [page['target'] for page in model.inner.jobs[1].payload['pages']], \
        'the parent of each opened page joins the selection, so the next round sends it in full'
    registry = world.registry()
    assert registry['pages']['projects/app/export-queue.md']['title'] == 'Rework the export queue'
    assert registry['pages']['projects/app/nightly-audit.md']['title'] == 'Nightly audit'
    facts = facts_of(world)
    assert len(facts) == 2, 'each note published a fact of its own'
    assert facts['app.replay-' + evidence_of(world, ids[0])]['target'] == 'projects/app/export-queue.md'
    assert facts['app.replay-' + evidence_of(world, ids[1])]['target'] == 'projects/app/nightly-audit.md'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 2


def test_a_second_page_request_for_the_same_note_is_refused_and_repaired(world):
    """A note is opened one page: a second request naming it is refused, repaired, and the batch keeps its members.

    The first answer asks two pages for the same note; the answer comes back naming that note and the page
    it already has, and the second answer (one page per note) is opened. A request naming a note outside the
    batch is refused the same way, and neither shape parks, fails or drops a member of the batch.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    twice = [work_page('export-queue', 'Rework the export queue', ids[0]),
             work_page('nightly-audit', 'Nightly audit', ids[0])]
    fixed = [work_page('export-queue', 'Rework the export queue', ids[0]),
             work_page('nightly-audit', 'Nightly audit', ids[1])]
    model = counted(PageListAsks(world, [twice, fixed]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    sizes = [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs]
    assert sizes == [[True, 2], [True, 2], [True, 2]], \
        'the refused list spends a repair round of the batch conversation, not its members'
    refusal = model.inner.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL', refusal
    assert ids[0] in refusal['reason'] and 'projects/app/export-queue.md' in refusal['reason'], refusal
    opened = [(item['target'], item['source']) for item in model.inner.jobs[2].payload['new_work']]
    assert opened == [('projects/app/export-queue.md', ids[0]), ('projects/app/nightly-audit.md', ids[1])], \
        'the repaired answer opens one page per note'
    facts = facts_of(world)
    assert len(facts) == 2, 'each note published a fact of its own'
    assert facts['app.replay-' + evidence_of(world, ids[0])]['target'] == 'projects/app/export-queue.md'
    assert facts['app.replay-' + evidence_of(world, ids[1])]['target'] == 'projects/app/nightly-audit.md'

    later = capture(world, notes(2, start=2))
    foreign = [work_page('weekly-sweep', 'Weekly sweep', 'op-not-in-this-batch')]
    second_asks = [foreign, [work_page('weekly-sweep', 'Weekly sweep', later[1])]]
    second = counted(PageListAsks(world, second_asks))
    report = apply_pending(world.service, second)
    assert sorted(report['applied']) == sorted(later), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))]
            for job in second.inner.jobs] == [[True, 2], [True, 2], [True, 2]], \
        'a page naming a note outside the batch is refused and repaired in the same conversation'
    refused = second.inner.jobs[1].payload['refusal']
    assert refused['code'] == 'E_PROPOSAL' and 'op-not-in-this-batch' in refused['reason'], refused
    assert [(item['target'], item['source']) for item in second.inner.jobs[2].payload['new_work']] \
        == [('projects/app/weekly-sweep.md', later[1])], 'the repaired request names only notes of this batch'
    assert len(facts_of(world)) == 4, 'the notes of the refused batch each published a fact of their own'


# ---------------------------------------------------------------------------
# A created page belongs to the earliest accepted note that puts a fact on it (step 11)
#
# The page a note's own request opened was attributed to that note, so when the split refused it in capture
# order (a fact key an earlier note of the batch claimed first) the page left the commit with its owner, and
# the batch died over a page a later member had a fact on: the production shape where a note ends with no
# fact, no narrative and no page. This section is the attribution — the page goes to the earliest accepted
# note that puts a fact on it — and the page no accepted note puts a fact on is not created at all.
# ---------------------------------------------------------------------------


class PageOwnerBatch(BatchModel):
    """The batch answer whose opened work page is owned by a note the split refuses.

    `opener` is the note whose creation request opened the work page; it proposes the shared fact key on
    that page, which a note before it claims first, so the split refuses the opener in capture order.
    `on_page` names the notes that put a fact of their own on the opened page; every other note proposes on
    the feature page. `shared` names the notes claiming the same fact key. `revises` is off: the collision
    is settled at the split, not repaired, which is the shape the production batches met.
    """

    revises = False

    def __init__(self, world, opener, on_page=(), shared=()):
        super().__init__(world)
        self.opener, self.on_page, self.shared = opener, set(on_page), set(shared)

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        opened = payload.get('new_work')
        page = {item['source']: item['target'] for item in opened}[self.opener]
        facts, checks = [], []
        for block in payload['sources']:
            operation_id = block['operation_id']
            evidence = block['evidence']['content_sha256'][:10]
            text = block['text'].strip().split('. ')[0]
            key = (payload['project'] + '.shared' if operation_id in self.shared
                   else payload['project'] + '.replay-' + evidence)
            target = page if (operation_id == self.opener or operation_id in self.on_page) \
                else payload['pages'][0]['target']
            fact = dict(fact_key=key, target=target, text=text, status='active', expected_revision=None,
                        source=operation_id)
            check = {'fact_key': key, 'source': operation_id,
                     'verified_match': {'result': 'not_found',
                                         'search_id': self.world.service.reconcile(text, target)['search_id']}}
            facts.append(fact)
            checks.append(check)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


def capture_work_page(world, text, slug, title):
    """Capture one note whose creation request opens its own work page; returns its operation id."""
    operation = world.service.create(text, project='app', selected=[FEATURE],
                                     new_work={'slug': slug, 'title': title})
    world.service.run(operation['id'])
    return operation['id']


def test_a_page_whose_owner_is_refused_at_the_split_is_created_under_the_next_note_with_a_fact(world):
    """The note that opened the page is refused at the split: the page is created under the next accepted note.

    Three notes, one batch: the first claims a fact key the opener claims too, so the split refuses the
    opener in capture order. The third note has a fact of its own on the opened page, so the page is
    created with the batch's commit, under that note, with its fact citing that note.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    texts = notes(3)
    first = capture(world, texts[:1])[0]
    opener = capture_work_page(world, texts[1], 'export-queue', 'Rework the export queue')
    later = capture(world, texts[2:])[0]
    ids = [first, opener, later]
    model = counted(PageOwnerBatch(world, opener, on_page=[later], shared=[first, opener]))
    report = apply_pending(world.service, model)
    assert report['applied'] == [first, later], report
    assert report['parked'] == [opener], report
    registry = world.registry()
    assert target in registry['pages'], \
        'the page is created although the note whose request opened it is refused'
    assert registry['pages'][target]['title'] == 'Rework the export queue'
    key = 'app.replay-' + evidence_of(world, later)
    assert facts_of(world)[key]['target'] == target, "the accepted fact on the page is the later note's"
    assert registry['facts'][key]['note_id'].startswith(evidence_of(world, later)), \
        'the fact on the created page cites the note that put it there'
    assert not [fact for fact in registry['facts'].values()
                if fact['note_id'].startswith(evidence_of(world, opener))], \
        'the refused note publishes no fact of its own'
    intents = [world.service.journal.action_intent(row['operation_id']) for row in member_rows(world, ids)]
    intent = next(item for item in intents if item is not None)
    created = [item for item in intent['delta']['pages'] if item['target'] == target]
    assert len(created) == 1, intent['delta']['pages']
    assert created[0]['source_operation_id'] == later, \
        'the page is attributed to the earliest accepted note that puts a fact on it'


def test_a_page_no_accepted_note_puts_a_fact_on_is_not_created(world):
    """The owner refused and no other accepted note has a fact on the page: the page is not created.

    The same batch as the case above, with the third note proposing on the feature page. Nothing accepted
    puts a fact on the opened page, so the batch publishes its two accepted facts and the page is created
    by no one: no page entry, no registry entry, no file.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    texts = notes(3)
    first = capture(world, texts[:1])[0]
    opener = capture_work_page(world, texts[1], 'export-queue', 'Rework the export queue')
    later = capture(world, texts[2:])[0]
    model = counted(PageOwnerBatch(world, opener, on_page=[], shared=[first, opener]))
    report = apply_pending(world.service, model)
    assert report['applied'] == [first, later], report
    assert report['parked'] == [opener], report
    registry = world.registry()
    assert target not in registry['pages'], 'no accepted note has a fact on the page, so no page is created'
    assert not os.path.lexists(world.root / 'knowledge/wiki' / target), 'and the wiki tree gains no file for it'
    published = {evidence_of(world, operation_id):
                 [fact['target'] for fact in world.registry()['facts'].values()
                  if fact['note_id'].startswith(evidence_of(world, operation_id))]
                 for operation_id in (first, later)}
    assert published == {evidence_of(world, first): [FEATURE], evidence_of(world, later): [FEATURE]}, \
        'the two accepted notes publish their facts on the page they proposed on'


# ---------------------------------------------------------------------------
# One key for one request, in a batch or alone (step 13)
#
# A note's attempt key counts its own queued and parked attempts, and the row that key opens is the row
# its next attempt answers. The row is keyed by a fingerprint of the request, so the fingerprint has to
# name the request the source answers and nothing the attempt carries with it: the refusal an earlier
# attempt ended with reaches the model as a hint, and the journal's copy of that reason moves when a
# batch opens the row, so a fingerprint over the request as stored answers the same note's own retry with
# E_IDEMPOTENCY. The note then parks on the vault's own bookkeeping and the pass publishes none of it —
# the production shape where a consulted note ends with no fact, no narrative and no page.
# ---------------------------------------------------------------------------


class NeverAccountedOnPage(WorkPageBatch):
    """The batch replay on each note's opened page, with `silent_for` left unaccounted on every round.

    The two shapes the production batches met in one answer: every note puts its open work on the page its
    own request opened, and one note is given no item and no `recorded` entry through both repair rounds.
    """

    def __init__(self, world, for_note, silent_for, single):
        super().__init__(world, for_note, single)
        self.silent_for = set(silent_for)

    def execute(self, job):
        answer = super().execute(job)
        if 'sources' not in job.payload or answer.kind != 'proposal':
            return answer
        answer.result['facts'] = [fact for fact in answer.result['facts']
                                  if fact['source'] not in self.silent_for]
        answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                        if check['source'] not in self.silent_for]
        return answer


def single_on_its_page(world, page='projects/app/export-queue.md'):
    """The single-source answer for a note whose own request opened `page`.

    The page is answered where the consultation carries it: as the opened page in `new_work` while it is
    not created yet, and as a sent page once the registry holds it (the retry whose page appeared)."""
    def single(job):
        from kelmo.executor import ModelOutcome
        opened = job.payload.get('new_work')
        target = (opened['target'] if opened
                  else page if any(item['target'] == page for item in job.payload['pages'])
                  else job.payload['pages'][0]['target'])
        fact = dict(fact_key='app.replay-' + job.payload['source']['evidence']['content_sha256'][:10],
                    target=target, text=job.payload['source']['text'].split('. ')[0], status='backlog',
                    expected_revision=None)
        check = {'fact_key': fact['fact_key'], 'verified_match': {
            'result': 'not_found', 'search_id': world.service.reconcile(fact['text'], target)['search_id']}}
        return ModelOutcome('proposal', {'facts': [fact], 'fact_checks': [check]})
    return single


def test_a_note_refused_in_a_batch_and_retried_alone_under_its_park_key_runs_without_E_IDEMPOTENCY(world):
    """The park key's row is the note's own request, in a batch and alone, before and after its page appears.

    The note is refused inside its first batch (a fact key an earlier member claimed first) and parks under
    its own attempt key. Its next batch opens that key's row, publishes its neighbour — which creates the
    work page the note's own request opened — and leaves the note unaccounted, so the same pass consults it
    alone under that same key. The row must answer that request: `E_IDEMPOTENCY` here parks the note on the
    vault's bookkeeping, and the pass publishes none of a note it consulted.
    """
    from kelmo import creation
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world, max_sources=2)
    target = 'projects/app/export-queue.md'
    first = capture(world, notes(1))[0]
    opener = capture_work_page(world, notes(1, start=1)[0], 'export-queue', 'Rework the export queue')
    first_pass = apply_pending(world.service, counted(BatchModel(world, shared=True)))
    assert first_pass['applied'] == [first] and first_pass['parked'] == [opener], first_pass
    parked, charged, _ = creation._parked(world.service.trace(opener))
    assert (parked, charged) == (1, 1), 'the refused member spent one attempt of its own'
    park_key = StateCoordinator._request_key('semantic', world.service._operation_origin(opener),
                                             f'create:{opener}:1:park1')
    assert world.service.journal.action_request(park_key) is None, 'the park key holds no row yet'

    neighbour = capture_work_page(world, notes(1, start=2)[0], 'export-queue', 'Rework the export queue')
    model = counted(NeverAccountedOnPage(
        world, {evidence_of(world, opener), evidence_of(world, neighbour)}, {opener},
        single_on_its_page(world)))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted([opener, neighbour]), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert not [(event.get('details') or event).get('error_code')
                for event in world.service.trace(opener)['events']
                if (event.get('details') or event).get('error_code') == 'E_IDEMPOTENCY'], \
        'the note met its own request row as a different request'
    row = world.service.journal.action_request(park_key)
    assert row is not None and row['state'] == 'finished', 'the park key holds the note\'s own finished request'
    registry = world.registry()
    assert registry['pages'][target]['title'] == 'Rework the export queue', 'the page the retry opened'
    facts = facts_of(world)
    assert sorted(facts) == sorted('app.replay-' + evidence_of(world, operation_id)
                                   for operation_id in (opener, neighbour)), \
        'each note the retried pass consulted ended with a fact of its own'
    assert facts['app.replay-' + evidence_of(world, opener)]['target'] == target, \
        'the retried note published onto the page its own request opened'
    assert [world.outcome(operation_id)['disposition'] for operation_id in (first, opener, neighbour)] \
        == ['applied'] * 3


class RoundsModel:
    """A model that spends `rounds` rounds of a batch on reconciliation and proposes on the round after them.

    A reconcile round is the round shape that spends a conversation without spending a repair: the repair
    budget is untouched, so the rounds are what run out. `propose` off answers a batch with no proposal on
    any round, which is the shape that runs a batch out of its rounds. `single_rounds` is how many a
    single-source conversation spends, so one model measures both counts; at 0 a single-source payload is
    answered with a real proposal at once, which is how a note the pass sends alone publishes in the pass
    that batched it.
    """

    def __init__(self, world, rounds, propose=True, single_rounds=0):
        self.world, self.rounds, self.propose = world, rounds, propose
        self.single_rounds, self.jobs = single_rounds, []

    def _seen(self, batch):
        return len([job for job in self.jobs if ('sources' in job.payload) == batch])

    def _proposal(self, payload, blocks):
        facts, checks = [], []
        for block in blocks:
            key = payload['project'] + '.replay-' + block['evidence']['content_sha256'][:10]
            fact = dict(fact_key=key, target=payload['pages'][0]['target'],
                        text=block['text'].split('. ')[0], status='active', expected_revision=None)
            check = {'fact_key': key, 'verified_match': {
                'result': 'not_found',
                'search_id': self.world.service.reconcile(fact['text'], fact['target'])['search_id']}}
            if 'sources' in payload:
                fact['source'] = check['source'] = block['operation_id']
            facts.append(fact)
            checks.append(check)
        from kelmo.executor import ModelOutcome
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        batch = 'sources' in payload
        spent = self._seen(batch)
        if not batch:
            if spent > self.single_rounds:
                return self._proposal(payload, [dict(payload['source'], operation_id=None)])
            text = payload['source']['text'].split('. ')[0]
            return ModelOutcome('reconcile', {'facts': [{'text': text, 'target': payload['pages'][0]['target']}]})
        if spent <= self.rounds:
            text = payload['sources'][0]['text'].split('. ')[0]
            return ModelOutcome('reconcile', {'facts': [{'text': text, 'target': payload['pages'][0]['target']}]})
        if not self.propose:
            text = payload['sources'][0]['text'].split('. ')[0]
            return ModelOutcome('reconcile', {'facts': [{'text': text, 'target': payload['pages'][0]['target']}]})
        return self._proposal(payload, payload['sources'])


def charged_parkings(world, operation_id):
    """(parked events, charged attempts) of one source: a batch that ran out of rounds must leave both at zero."""
    from kelmo import creation
    trace = world.service.trace(operation_id)
    return ([event for event in trace['events'] if event['stage'] == 'parked'], creation._parked(trace)[1])


def test_a_batch_that_used_7_rounds_still_publishes_and_at_8_each_member_runs_alone(world):
    """A batch gets eight rounds, a single source keeps five, and a batch out of rounds sends each member alone.

    The first production batches lost a third of their notes at the single-source count of five: the lead of
    the batch of seven reported `applied` with nothing published, and four notes settled `archive-only` on
    "adds no new fact" although each held real facts. Eight rounds is what a batch of that size needs to
    answer every member, so a batch that spent seven of them on reconciliation still publishes its facts in
    one commit. A batch that spends all eight with nothing proposed charges no member of it — the
    conversation ran out, not any note's attempt — and the same pass consults each member on the
    single-source path, the way a note the batch left unaccounted for is sent. A single source keeps five.
    """
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world, max_sources=3)
    ids = capture(world, notes(3))
    model = counted(RoundsModel(world, 7))
    before = commits(world)
    report = apply_pending(world.service, model)
    sizes = [len(job.payload['sources']) if 'sources' in job.payload else 1 for job in model.inner.jobs]
    assert sizes == [3] * 8, 'seven rounds of reconciliation and the proposal on the eighth: a batch gets eight'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped'], report['deferred']) == ([], [], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert commits(world) == before + 1, 'the batch that spent its seventh round publishes its facts in one commit'
    assert len(facts_of(world)) == 3, 'every note of the batch ended with a fact of its own'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3

    batched = capture(world, notes(3, start=3))
    spent = counted(RoundsModel(world, 8, propose=False))
    before = commits(world)
    report = apply_pending(world.service, spent)
    sizes = [len(job.payload['sources']) if 'sources' in job.payload else 1 for job in spent.inner.jobs]
    assert sizes == [3] * 8 + [1, 1, 1], \
        'the batch spent its eight rounds with nothing proposed, and each member got its own consultation'
    assert sorted(report['applied']) == sorted(batched), report
    assert (report['parked'], report['failed'], report['skipped'], report['deferred']) == ([], [], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert commits(world) == before + 3, 'the batch published nothing: each note published in its own commit'
    assert len(facts_of(world)) == 6, 'every note of the pass ended with a fact of its own'
    assert [world.outcome(operation_id)['disposition'] for operation_id in batched] == ['applied'] * 3
    for operation_id in batched:
        assert charged_parkings(world, operation_id) == ([], 0), \
            'a batch that ran out of rounds charges no member: no note is refused under its own key'
        assert 'adds no new fact' not in world.outcome(operation_id)['reason'], world.outcome(operation_id)
        row = world.service.journal.action_request(StateCoordinator._request_key(
            'semantic', world.service._operation_origin(operation_id), f'create:{operation_id}:1'))
        assert row['state'] == 'finished', 'the note published under the attempt key its batch opened'
        park_key = StateCoordinator._request_key('semantic', world.service._operation_origin(operation_id),
                                                 f'create:{operation_id}:1:park1')
        assert world.service.journal.action_request(park_key) is None, 'no new attempt was opened for it'

    editor_mode(world, batch_apply=False)
    single = capture(world, notes(1, start=6))[0]
    kept = counted(RoundsModel(world, 8, single_rounds=5))
    before = commits(world)
    report = apply_pending(world.service, kept)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1 for job in kept.inner.jobs] == [1] * 5, \
        'a single source is cut off at five rounds, as it was before a batch got eight'
    assert report['parked'] == [single], report
    assert commits(world) == before, 'the single-source consultation that ran out published nothing'
    assert world.outcome(single)['disposition'] == 'unresolved', world.outcome(single)
    assert 'did not propose within 5 rounds' in world.outcome(single)['reason'], \
        'the single-source refusal names five rounds: a batch got the extra rounds, a single source did not'


def test_a_crash_after_the_commit_with_two_opened_pages_settles_every_member_without_a_model_call(world,
                                                                                                  monkeypatch):
    """Two notes each opened its own work page, the batch committed, and the process died while they settled.

    The next pass has each member's own row and the stored intent: both pages are in the registry, both
    notes settle `applied`, and the pass pays no model call for a batch that already committed.
    """
    from kelmo import editor as editor_module
    from kelmo.creation import apply_pending
    editor_mode(world)
    opened = [capture_work_page(world, text, slug, title) for text, slug, title in
              ((notes(1, start=4)[0], 'export-queue', 'Rework the export queue'),
               (notes(1, start=5)[0], 'nightly-audit', 'Nightly audit'))]
    model = counted(WorkPageBatch(world, {evidence_of(world, operation_id) for operation_id in opened},
                                 single_on_its_page(world)))
    real = editor_module.Editor._member_disposition
    monkeypatch.setattr(editor_module.Editor, '_member_disposition',
                        crash_with('the process died after the commit'))
    with pytest.raises(Died):
        apply_pending(world.service, model)
    registry = world.registry()
    assert [registry['pages'][target]['title'] for target in
            ('projects/app/export-queue.md', 'projects/app/nightly-audit.md')] == \
        ['Rework the export queue', 'Nightly audit'], 'both pages were created by the commit'
    assert len(facts_of(world)) == 2, 'the batch committed both facts'
    calls = len(model.inner.jobs)
    monkeypatch.setattr(editor_module.Editor, '_member_disposition', real)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(opened), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert len(model.inner.jobs) == calls, 'the published members paid no model call'


# ---------------------------------------------------------------------------
# A batch answer of `unresolved` settles no member: each note runs the single-source path
#
# `unresolved` names no source, so a batch conversation that answers it has said nothing about any note of
# the batch. Settling every member on that one answer is the production loss the epic names: four notes of
# the first batches ended `archive-only` "adds no new fact" although each held real facts. The batch spent
# its conversation, so no note spends an attempt for it, and the same pass consults each note on the
# single-source path, where a note that holds a fact publishes it and a note that holds none is declined by
# the single-source path's own rule (its own work page opened, one more ask).
# ---------------------------------------------------------------------------


def test_a_batch_answer_of_unresolved_settles_no_member_and_each_runs_alone(world):
    """A batch answered `unresolved` settles nobody and sends each member alone in the same pass."""
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world, max_sources=3)
    ids = capture(world, notes(3))
    model = counted(DeclinedBatchAnswer(world))
    before = commits(world)
    report = apply_pending(world.service, model)
    sizes = [len(job.payload['sources']) if 'sources' in job.payload else 1 for job in model.inner.jobs]
    assert sizes == [3, 1, 1, 1], \
        'the batch answered unresolved with no proposal, and each of its three notes got its own consultation'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped'], report['deferred']) == ([], [], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert commits(world) == before + 3, 'the batch published nothing: each note published in its own commit'
    assert len(facts_of(world)) == 3, 'every note of the pass ended with a fact of its own'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), \
            'a batch answered unresolved charges no member: no note is refused under its own key'
        assert 'adds no new fact' not in world.outcome(operation_id)['reason'], world.outcome(operation_id)
        row = world.service.journal.action_request(StateCoordinator._request_key(
            'semantic', world.service._operation_origin(operation_id), f'create:{operation_id}:1'))
        assert row['state'] == 'finished', 'the note published under the attempt key its batch opened'
        park_key = StateCoordinator._request_key('semantic', world.service._operation_origin(operation_id),
                                                 f'create:{operation_id}:1:park1')
        assert world.service.journal.action_request(park_key) is None, 'no new attempt was opened for it'


# ---------------------------------------------------------------------------
# Every note of a partly answered batch ends in a state of its own (step 5)
#
# The guarantee the epic exists for, at the batch sizes the production passes run: whatever part of the batch
# the model answers, each note ends published with a fact of its own, recorded with the keys that already
# state it, refused with a code of its own, or sent alone and published by the same pass. No note settles
# `archive-only` on a reason the editor invented for it.
# ---------------------------------------------------------------------------


class PartlyAnswered(BatchModel):
    """The batch replay that answers only part of the batch.

    `recorded` (optional) is the note the answer names as recorded, with the candidate keys its own
    reconciliation returned (the replay reconciles its text first); `silent` the notes it never answers;
    `collide` an (earlier, later) pair where the later note proposes the earlier one's fact key, which the
    split settles in capture order. Every other note gets a fact of its own, and a note consulted alone is
    answered the way the replay answers a single source.
    """

    def __init__(self, world, recorded=None, silent=(), collide=None):
        super().__init__(world)
        self.recorded, self.silent, self.collide = recorded, set(silent), collide
        self.asked = recorded is None

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and not self.asked:
            self.asked = True
            self.jobs.append(job)
            text = next(block['text'] for block in payload['sources'] if block['operation_id'] == self.recorded)
            return ModelOutcome('reconcile', {'facts': [{'text': text, 'target': FEATURE}]})
        answer = super().execute(job)
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        result, quiet = answer.result, self.silent | {self.recorded}
        result['facts'] = [fact for fact in result['facts'] if fact['source'] not in quiet]
        result['fact_checks'] = [check for check in result['fact_checks'] if check['source'] not in quiet]
        if self.recorded is not None and 'refusal' in payload:
            result['recorded'] = [{'source': self.recorded, 'fact_keys': [
                key for receipt in payload['reconciliations'] for key in receipt['candidate_keys']]}]
        if self.collide is not None:
            earlier, later = self.collide
            keys = {fact['source']: fact['fact_key'] for fact in result['facts']}
            if earlier in keys and later in keys:
                for item in [*result['facts'], *result['fact_checks']]:
                    if item['source'] == later:
                        item['fact_key'] = keys[earlier]
        return answer


@pytest.mark.parametrize('size', [2, 5, 8])
def test_every_note_of_a_partly_answered_batch_ends_published_recorded_refused_or_alone(world, size):
    """Batches of 2, 5 and 8 where the model answers only some notes: each note ends in a state of its own."""
    from kelmo.creation import apply_pending
    editor_mode(world, max_sources=size)
    ids = capture(world, notes(size))
    silent = {ids[-1]}
    recorded = ids[1] if size > 2 else None
    collide = (ids[2], ids[3]) if size > 2 else None
    if size >= 8:
        silent.add(ids[-2])
    model = counted(PartlyAnswered(world, recorded=recorded, silent=silent, collide=collide))
    report = apply_pending(world.service, model)
    batched = [job for job in model.inner.jobs if 'sources' in job.payload]
    alone = [job for job in model.inner.jobs if 'source' in job.payload]
    assert batched and all(len(job.payload['sources']) == size for job in batched), \
        'every note of the pass was in the one batch'
    assert len(batched) <= 8, 'the batch stays within its rounds'
    assert len(alone) == len(silent), 'each note the answer never accounted for got its own consultation'
    refused = {collide[1]} if collide else set()
    assert sorted(report['parked']) == sorted(refused), report
    assert sorted(report['applied']) == sorted(set(ids) - refused), report
    assert (report['failed'], report['skipped'], report['deferred']) == ([], [], []), report
    facts = facts_of(world)
    for operation_id in ids:
        outcome = world.outcome(operation_id)
        assert 'adds no new fact' not in (outcome or {}).get('reason', ''), outcome
        if operation_id == recorded:
            assert outcome['disposition'] == 'archive-only', outcome
            keys = [key for receipt in batched[-1].payload['reconciliations'] for key in receipt['candidate_keys']]
            assert keys and all(key in outcome['reason'] for key in keys), (keys, outcome)
            assert 'app.replay-' + evidence_of(world, operation_id) not in facts
        elif operation_id in refused:
            row = world.service.journal.get(operation_id)
            assert (row['state'], row['error_code']) == ('parked', 'E_PROPOSAL'), row
            assert charged_parkings(world, operation_id)[1] == 1, 'refused under its own key, charged once'
        else:
            assert outcome['disposition'] == 'applied', outcome
            assert 'app.replay-' + evidence_of(world, operation_id) in facts, \
                'a published note carries a fact of its own'
            others = ['app.replay-' + evidence_of(world, other) for other in ids if other != operation_id]
            assert not [key for key in others if key in outcome['reason']], \
                ('a published note settles on its own facts, never on its neighbours\'', outcome)
    for operation_id in silent:
        assert charged_parkings(world, operation_id) == ([], 0), 'a note sent alone spends no attempt'


# ---------------------------------------------------------------------------
# One slug is one page inside a batch (step 12)
#
# Two notes asking the page under one slug, or a note asking the slug another note's stored `new_work`
# opens, resolve to one page created once, a fact from each note on it, and no member refused or charged.
# A stored `new_work` whose page is already registered, under whatever title, counts as opened: the note
# joins its batch and edits the page it finds.
# ---------------------------------------------------------------------------


class AsksFirst(WorkPageBatch):
    """The batch replay whose first answers are the page lists the caller names, whatever the payload holds.

    Unlike `PageListAsks` it asks while stored pages are already open, so a case can ask the slug a stored
    `new_work` opened. Once the lists are spent, each note puts its fact on the page named for it.
    """

    def __init__(self, world, asks):
        super().__init__(world, set(), None)
        self.asks, self.rounds = list(asks), 0

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload and self.rounds < len(self.asks):
            works = self.asks[self.rounds]
            self.rounds += 1
            self.jobs.append(job)
            return ModelOutcome('open_works', {'works': works})
        return super().execute(job)


def test_two_notes_asking_one_slug_in_one_answer_share_one_page(world):
    """Two notes, one slug, one answer: one page, a fact from each, no repair round and nobody refused."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    ids = capture(world, notes(2))
    asked = [work_page('export-queue', 'Rework the export queue', ids[0]),
             work_page('export-queue', 'Export queue retries', ids[1])]
    model = counted(AsksFirst(world, [asked]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 2], [True, 2]], 'the shared slug spends no repair round'
    assert 'refusal' not in model.inner.jobs[1].payload, model.inner.jobs[1].payload.get('refusal')
    opened = model.inner.jobs[1].payload['new_work']
    assert [(item['target'], item['source']) for item in opened] == [(target, ids[0]), (target, ids[1])], opened
    assert {item['title'] for item in opened} == {'Rework the export queue'}, \
        'the page keeps the identity it was first asked with'
    registry = world.registry()
    assert registry['pages'][target]['title'] == 'Rework the export queue'
    assert 'projects/app/export-queue-2.md' not in registry['pages'], 'one page, not a second one'
    facts = facts_of(world)
    assert [facts['app.replay-' + evidence_of(world, operation_id)]['target'] for operation_id in ids] \
        == [target, target], 'a fact from each note on the one page'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_note_asking_the_slug_a_stored_new_work_opens_shares_that_page(world):
    """A stored `new_work` and an ask for the same slug in one batch: one page, under the stored identity."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    opener = capture_work_page(world, 'App note: rework the export queue.', 'export-queue', 'Rework the export queue')
    plain = capture(world, notes(1))[0]
    model = counted(AsksFirst(world, [[work_page('export-queue', 'Export queue retries', plain)]]))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted([opener, plain]), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert 'refusal' not in model.inner.jobs[1].payload, model.inner.jobs[1].payload.get('refusal')
    opened = model.inner.jobs[1].payload['new_work']
    assert [(item['target'], item['source']) for item in opened] == [(target, opener), (target, plain)], opened
    registry = world.registry()
    assert registry['pages'][target]['title'] == 'Rework the export queue', 'the stored request opened it first'
    assert 'projects/app/export-queue-2.md' not in registry['pages'], 'one page, not a second one'
    facts = facts_of(world)
    assert facts['app.replay-' + evidence_of(world, opener)]['target'] == target
    assert facts['app.replay-' + evidence_of(world, plain)]['target'] == target


def test_a_stored_new_work_whose_page_was_registered_under_another_title_joins_its_batch(world):
    """The page appeared, under another title, before the pass: the note joins its batch and edits it."""
    from kelmo.creation import apply_pending
    editor_mode(world, batch_apply=False)
    target = 'projects/app/export-queue.md'
    first = capture_work_page(world, 'App note: rework the export queue.', 'export-queue', 'Rework the export queue')
    later = capture_work_page(world, 'App note: the export queue caps retries.', 'export-queue', 'Retry caps')
    plain = capture(world, notes(2))

    def single(job):
        return world.propose(facts=[dict(fact_key='app.export-queue', target=target,
                                         text='The export queue is reworked.', status='backlog',
                                         expected_revision=None)])

    assert apply_pending(world.service, Scripted(single), limit=1)['applied'] == [first]
    assert world.registry()['pages'][target]['title'] == 'Rework the export queue'
    editor_mode(world)
    model = counted(WorkPageBatch(world, set(), None))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted([later, *plain]), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert [[('sources' in job.payload), len(job.payload.get('sources', []))] for job in model.inner.jobs] \
        == [[True, 3]], 'the note whose page exists joins its batch in capture order'
    assert not model.inner.jobs[0].payload.get('new_work'), 'no page creation is asked for a page that exists'
    assert target in [page['target'] for page in model.inner.jobs[0].payload['pages']], \
        'the registered page is sent with the batch'
    registry = world.registry()
    assert registry['pages'][target]['title'] == 'Rework the export queue', 'the page keeps its identity'
    assert 'projects/app/export-queue-2.md' not in registry['pages']
    assert charged_parkings(world, later) == ([], 0)


def test_a_ready_pass_runs_every_note_its_batch_sent_alone_though_the_project_drops_below_the_number(world):
    """The ready pass owes each note its batch sent alone the single-source turn in the same pass.

    Eight notes at ready 8: the batch answers `unresolved`, so all eight go alone. Once the first of them
    publishes, the project has seven first-pass notes left, below the number, and a pass that re-counted
    readiness alone would leave the other seven behind after their batch had already taken them."""
    editor_mode(world)
    app = capture(world, notes(8))
    ops = capture(world, notes(2, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    model = counted(DeclinedBatchAnswer(world))
    report = ready_pass(world, model)
    sizes = [len(job.payload['sources']) if 'sources' in job.payload else 1 for job in model.inner.jobs]
    assert sizes == [8] + [1] * 8, 'one batch, then each of its notes alone in the same pass'
    assert sorted(report['applied']) == sorted(app), report
    assert report['waiting'] == {'ops': 2}, 'the project that was never ready still waits for its own pass'
    assert waiting_ids(world) == ops
    for operation_id in app:
        assert charged_parkings(world, operation_id) == ([], 0)
