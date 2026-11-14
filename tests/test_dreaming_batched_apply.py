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
from pathlib import Path

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
    the split resolves in capture order; `decline` answers unresolved to every payload, a batch
    and a single source alike. A single-source payload (the flag off, or a source the batch left out) is
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


class SharedKeyBatch(BatchModel):
    """The batch replay where the notes in `claiming` all propose one fact key: the cross-source collision.

    Only the batch answer collides; a note asked alone gets the replay's own key. `revises` is off, so the
    collision reaches the settle as the production batches met it, with no repair round in between. Pass it
    to the pass unwrapped: `Counted` would hide the attribute. `bad_receipt` names notes whose own fact check
    in the batch answer cites a receipt no search returned."""

    revises = False

    def __init__(self, world, claiming, bad_receipt=(), extra=()):
        super().__init__(world)
        self.claiming, self.bad_receipt, self.extra = set(claiming), set(bad_receipt), set(extra)

    def execute(self, job):
        outcome = super().execute(job)
        if 'sources' in job.payload:
            for fact, check in list(zip(outcome.result['facts'], outcome.result['fact_checks'])):
                if fact.get('source') in self.extra:
                    # A second fact of its own, which no other note claims.
                    text = 'A second reading: ' + fact['text']
                    receipt = self.world.service.reconcile(text, fact['target'])['search_id']
                    outcome.result['facts'].append(dict(fact, fact_key=fact['fact_key'] + '-second', text=text))
                    outcome.result['fact_checks'].append(dict(check, fact_key=fact['fact_key'] + '-second',
                                                              verified_match=dict(check['verified_match'],
                                                                                  search_id=receipt)))
            for name in ('facts', 'fact_checks'):
                for item in outcome.result[name]:
                    if item.get('source') in self.claiming and not item['fact_key'].endswith('-second'):
                        item['fact_key'] = job.payload['project'] + '.shared'
            for check in outcome.result['fact_checks']:
                if check.get('source') in self.bad_receipt:
                    check['verified_match'] = dict(check['verified_match'], search_id='0' * 32)
        return outcome


def single_texts(model):
    """The note text of every single-source job the model answered, in order."""
    return [job.payload['source']['text'] for job in model.jobs if 'source' in job.payload]


def test_a_cross_source_collision_sends_the_later_note_alone_in_the_same_pass_uncharged(world):
    """Two notes of a batch claim one fact key. The earlier one publishes it; the later one never had a turn
    that would have shown it that claim, so it is no refusal of it: the same pass consults it alone, and it
    spends no attempt."""
    texts = notes(2)
    editor_mode(world)
    ids = capture(world, texts)
    before = commits(world)
    model = SharedKeyBatch(world, claiming=ids)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert single_texts(model) == [texts[1]], 'the later note was consulted alone after the batch'
    assert charged_parkings(world, ids[1]) == ([], 0), 'the collision is the answer, not the note'
    assert commits(world) - before == 2, 'the batch published the earlier note, the later one its own commit'


def test_a_collision_goes_to_the_earlier_note_whatever_order_the_answer_lists_them_in(world):
    """The batch answer lists the newer note's fact first, and both claim one fact key. The key still goes to
    the earlier note by capture order: it publishes with the batch, and the newer note is the one consulted
    alone after it, uncharged, so a newer note never takes a claim ahead of an older one."""
    texts = notes(2)
    editor_mode(world)
    ids = capture(world, texts)

    class ListsNewerFirst(SharedKeyBatch):
        def execute(self, job):
            outcome = super().execute(job)
            if 'sources' in job.payload:
                for name in ('facts', 'fact_checks'):
                    outcome.result[name].reverse()
            return outcome

    model = ListsNewerFirst(world, claiming=ids)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert single_texts(model) == [texts[1]], 'the newer note was the one consulted alone'
    assert charged_parkings(world, ids[1]) == ([], 0)


@pytest.mark.parametrize('answer_order', [('a', 'b'), ('b', 'a')], ids=['earlier-first', 'later-first'])
def test_a_created_page_two_notes_propose_keeps_the_earlier_note_s_page_whatever_the_answer_order(answer_order):
    """Both notes of a batch propose one new page, and the earlier one puts a fact on it. The page goes to the
    earlier note by capture order and the later note collides; the page the commit creates is the earlier
    note's own, never the collided note's, whichever of the two the answer lists first."""
    from kelmo.editor import _reattribute_created_pages, split_delta
    target = 'projects/app/exports.md'

    def page(source):
        return {'target': target, 'expected_revision': None, 'page_id': 'page-' + source,
                'title': 'Exports by ' + source, 'source_operation_id': source}

    fact = {'fact_key': 'app.exports', 'target': target, 'text': 'Exports run at 13:00.', 'status': 'active',
            'expected_revision': None, 'source_operation_id': 'a'}
    delta = {'facts': [fact], 'narratives': [], 'pages': [page(source) for source in answer_order]}
    slices, refusals = split_delta(delta, [], ['a', 'b'])
    assert list(refusals) == ['b'], 'the later note collides on the page'
    _reattribute_created_pages(slices, ['a'])
    assert [item['title'] for item in slices['a']['delta']['pages']] == ['Exports by a']
    assert slices['b']['delta']['pages'] == []


def test_a_collided_note_with_a_fact_of_its_own_leaves_the_commit_whole(world):
    """The later note claims the earlier one's fact key and has a second fact no one else claims. It does not
    publish that second fact with the batch while its other claim is silently dropped: it leaves the commit
    whole and is consulted alone, so the model reads its whole note against the wiki the batch left."""
    texts = notes(2)
    editor_mode(world)
    ids = capture(world, texts)
    model = SharedKeyBatch(world, claiming=ids, extra=ids[1:])
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert single_texts(model) == [texts[1]], 'the later note was consulted alone'
    assert not [key for key in world.registry()['facts'] if key.endswith('-second')], \
        'nothing of the collided note was published with the batch'
    assert charged_parkings(world, ids[1]) == ([], 0)


def test_a_collision_stands_aside_when_the_earlier_note_is_refused_for_its_own_check(world):
    """The earlier note's own receipt is bad, so it is refused for that, charged. The later note collided with
    it: alone it publishes, and it pays nothing for the earlier note's refusal."""
    texts = notes(2)
    editor_mode(world)
    ids = capture(world, texts)
    model = SharedKeyBatch(world, claiming=ids, bad_receipt=ids[:1])
    report = pending_pass(world, model)
    assert report['parked'] == ids[:1] and report['applied'] == ids[1:], report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_FACT_CHECK'
    assert charged_parkings(world, ids[0])[1] == 1, 'the earlier note pays for its own receipt'
    assert charged_parkings(world, ids[1]) == ([], 0), 'the later note pays nothing for it'
    assert single_texts(model) == [texts[1]]


def test_a_collided_note_whose_batch_refused_every_other_note_is_asked_alone_not_rebatched(world):
    """[A, B, C] form a batch and D waits past its ceiling. A and C are refused for their own receipts and B
    collides with A, so the batch publishes nothing. B is consulted alone next, as a collided note always is,
    not folded into a new batch with D."""
    texts = notes(4)
    editor_mode(world, max_sources=3)
    ids = capture(world, texts)
    model = SharedKeyBatch(world, claiming=ids[:2], bad_receipt=[ids[0], ids[2]])
    report = pending_pass(world, model)
    assert sorted(report['parked']) == sorted([ids[0], ids[2]]), report
    assert sorted(report['applied']) == sorted([ids[1], ids[3]]), report
    assert single_texts(model) == [texts[1], texts[3]], 'B was asked alone, then D'


def test_the_notes_after_a_collided_note_wait_for_it_and_publish_after_it(world):
    """[A, B, C]: B collides with A. A publishes with the batch; C is newer than B, so it waits for B's turn
    alone and publishes after it, in the same pass, and nobody is charged."""
    texts = notes(3)
    editor_mode(world)
    ids = capture(world, texts)
    model = SharedKeyBatch(world, claiming=ids[:2])
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert [len(job.payload['sources']) for job in model.jobs if 'sources' in job.payload] == [3]
    assert single_texts(model) == [texts[1], texts[2]], 'B is consulted before C, never after'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_the_notes_behind_a_collided_note_rejoin_one_batch_after_it(world):
    """[A, B, C, D]: B collides with A. C and D wait behind B, which is asked alone; then they share one batch
    conversation again instead of one conversation each."""
    texts = notes(4)
    editor_mode(world)
    ids = capture(world, texts)
    model = SharedKeyBatch(world, claiming=ids[:2])
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert [len(job.payload['sources']) for job in model.jobs if 'sources' in job.payload] == [4, 2]
    assert single_texts(model) == [texts[1]], 'only the collided note is asked alone'


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


def test_a_shrink_inside_the_conversation_takes_the_dropped_note_s_own_pages_out_of_the_view(world):
    """The same mid-batch shrink, where the newest note also selected the notes page: once that note is
    dropped, the two notes the batch keeps read only the page they selected. (The notes page, 239 tokens on
    this fixture, fits neither after the receipt nor beside FEATURE, so it changes nothing about the shrink.)"""
    from test_editor_scoping import NOTES
    editor_mode(world, budget=2000)
    texts = long_notes(3, pads=2)
    ids = capture(world, texts[:2]) + capture(world, texts[2:], selected=(FEATURE, NOTES))
    model = counted(Reconciling(world))
    report = pending_pass(world, model)
    batched = [job.payload for job in model.inner.jobs if 'sources' in job.payload]
    assert [len(payload['sources']) for payload in batched] == [3, 2], [len(p['sources']) for p in batched]
    assert NOTES in [page['target'] for page in batched[0]['pages']] + batched[0]['omitted'], \
        'the fixture: the first round carries the page only the newest note selected'
    assert [page['target'] for page in batched[1]['pages']] + batched[1]['omitted'] == [FEATURE], \
        'after the shrink the view holds only the page the kept notes selected'
    assert sorted(report['applied']) == sorted(ids), report


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
    # The head is refused on the batch's own view, measured above. The note behind it was dropped from that
    # batch unread and is refused on its own turn, by the numbers its own single-source view measured.
    head = world.outcome(ids[0])['reason']
    assert ('omitted ' + FEATURE + ': needs ' + str(cost) + ' tokens, 0 of ' + str(budget) + ' left') in head, head
    for operation_id in ids:
        reason = world.outcome(operation_id)['reason']
        assert 'omitted ' + FEATURE + ': needs ' + str(cost) + ' tokens, ' in reason, reason
        assert ' of ' + str(budget) + ' left' in reason, reason
        assert len(reason) <= outcomes.REASON_LIMIT
        assert len(charged_parkings(world, operation_id)[0]) == 1, 'each note is refused once, for its own budget'


@pytest.mark.parametrize('answer', ['nothing', 'recorded'])
def test_a_batch_answer_to_partial_context_sends_each_note_to_its_own_budget_refusal(world, answer):
    """The model answered nothing to a context that sent only part of what the batch selected, or said every
    note is already stated. What it did not read is the batch's selection, so the batch settles and refuses no
    note on it: each one is consulted alone, and the refusal it meets there names what the omitted page needed
    against that note's own context."""
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

    known = next(fact for fact in world.registry()['facts'].values() if fact.get('target') == FEATURE)

    class Empty:
        """The model answer of a batch that saw a partial context: it proposed nothing, and with `recorded` it
        reconciles a fact the feature page states and names every source as already stating it."""

        def __init__(self):
            self.jobs = []

        def execute(self, job):
            self.jobs.append(job)
            payload = job.payload
            if answer == 'recorded' and 'sources' in payload and not payload['reconciliations']:
                return ModelOutcome('reconcile', {'facts': [{'text': known['text'], 'target': FEATURE}]})
            result = {'facts': [], 'fact_checks': []}
            if answer == 'recorded' and 'sources' in payload:
                keys = [key for receipt in payload['reconciliations'] for key in receipt['candidate_keys']]
                result['recorded'] = [{'source': block['operation_id'], 'fact_keys': keys}
                                      for block in payload['sources']]
            return ModelOutcome('proposal', result)

    model = Empty()
    before = commits(world)
    report = apply_pending(world.service, model)
    assert sorted(report['parked']) == sorted(ids) and (report['applied'], report['failed']) == ([], [])
    assert commits(world) == before and world.service.journal.prepared_intents() == []
    # Two repair rounds when the answer left the notes unaccounted for; a reconcile round before a recorded one.
    rounds = [True, True, True] if answer == 'nothing' else [True, True]
    assert ['sources' in job.payload for job in model.jobs] == [*rounds, False, False], \
        'one batch conversation, then each note judged on its own context'
    for operation_id in ids:
        reason = world.outcome(operation_id)['reason']
        assert 'the model saw incomplete context and proposed nothing' in reason, reason
        assert 'omitted projects/app/notes.md: needs ' + str(cost) + ' tokens, ' in reason, reason
        assert ' of ' + str(full['used_tokens'] - cost) + ' left' in reason, reason
        assert len(reason) <= outcomes.REASON_LIMIT
        assert len(charged_parkings(world, operation_id)[0]) == 1, 'refused once, on its own turn'


def test_a_batch_answered_unresolved_settles_no_member_and_commits_nothing(world):
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


def test_a_source_that_opens_its_work_page_joins_its_batch(world):
    """A stored work page does not keep its note out of the batch.

    Sent alone, the note with the opened page would meet the single-source path, where a lost conversation
    leaves a note with no fact, no narrative and no page. It joins its batch instead, so the pass pays one
    conversation for all three and the page is opened with the batch's commit.
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


def test_a_pass_that_died_after_the_answer_asks_its_notes_again_and_publishes_them_once(world, monkeypatch):
    """The answer is recorded and the pass dies before the settle: nothing was committed, so the next pass
    does not finish that answer. It asks the batch again, and no note pays for the pass that died."""
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
    assert [len(job.payload['sources']) for job in settled.inner.jobs] == [2], 'one conversation, as a batch'
    assert commits(world) == before + 1, 'the batch is published once'
    assert len(facts_of(world)) == 2
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


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

    # A stored `new_work` is no reason to stay out of the batch, so the note that cannot join here is one
    # whose quota deferral has not reset. The guarantee this case pins: a source the batch cannot take
    # holds the notes behind it back.
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
    assert (report['applied'], report['parked']) == ([], ids[:1]), report
    assert 'E_ORIGIN' in world.outcome(ids[0])['reason'], world.outcome(ids[0])
    assert [entry['operation_id'] for entry in report['skipped']] == ids[1:], \
        'the newer note waits for the one the refused commit parked'
    assert charged_parkings(world, ids[1]) == ([], 0)
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
    """The first and the last source of the batch propose the same fact key; the middle one its own. A note
    asked alone gets the replay's own answer."""

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' not in payload:
            return super().execute(job)
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
    """The collision the precheck names stops it recording any slice; the settle split still knows each owner.
    The collided last note is asked alone in the same pass, uncharged."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    report = apply_pending(world.service, CollidingFirst(world))
    assert report['applied'] == ids and report['parked'] == [], report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    assert charged_parkings(world, ids[2]) == ([], 0)


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

    A reason the outcome store refuses must not stop the queue: the batch prepares and commits nothing, the
    oldest note meets the same refusal on its own turn and parks with that code (free), the notes after it
    wait for it uncharged, and the next pass publishes them all in order."""
    from kelmo import editor, outcomes
    from kelmo.creation import apply_pending
    original = editor._summary
    monkeypatch.setattr(editor, '_summary', lambda delta: 'outcome reason ' + 'x' * outcomes.REASON_LIMIT)
    editor_mode(world)
    ids = capture(world, notes(3))
    model = BatchModel(world)
    report = apply_pending(world.service, model)
    assert report['applied'] == [] and report['failed'] == []
    assert report['parked'] == ids[:1], report
    assert facts_of(world) == {}, 'nothing was committed'
    assert world.service.journal.prepared_semantic() == [], 'no intent was prepared'
    assert world.outcome(ids[0])['reason'].startswith('E_OUTCOME: '), world.outcome(ids[0])
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id
    monkeypatch.setattr(editor, '_summary', original)
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert len(facts_of(world)) == 3, 'the same notes published once the reason was whole'


def test_two_notes_with_the_same_new_work_publish_onto_one_page_in_a_batched_pass(world):
    """The batched pass reads the same stored request: a work page that exists is edited, not opened again.

    Both requests were captured before either note published, so both name the same page. The two notes
    do not run the single-source path one at a time: they join the
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
    and the owner is alerted. The newer note waits behind the older one, uncharged, until the older one
    fails; then it meets the same bound on its own passes."""
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
    failed_at, bound = {}, creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS - 1
    for turn in range(2 * (bound + 1)):
        report = apply_pending(world.service, counted(BatchModel(world)))
        failed_at.update({operation_id: turn for operation_id in report['failed']})
        if len(failed_at) == len(ids):
            break
    assert failed_at == {ids[0]: bound, ids[1]: 2 * bound}, failed_at
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
    note goes through a batch of two first, so what the case reads is the batch pass's own accounting: the
    conversation its text breaks charges nobody, the retried note then parks on its own turn, and the note
    that shared the batch with it publishes."""
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
        'the retried note went through a batch conversation before its own turn'
    assert (report['parked'], report['applied'], report['failed']) == ([ids[0]], late, []), report
    assert charged_parkings(world, late[0]) == ([], 0), 'the note that shared its batch is charged nothing'
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
    """A note captured after a pass died joins the notes that pass left, and each publishes its own fact.

    The first pass recorded the batch answer and died before settling. Nothing was committed, so the next
    pass asks those notes again, with the newer note of the same project in the same batch. It must not be
    settled `archive-only` for being absent from the answer that died."""
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
    end every member whose items were committed `applied` without consulting the model again. The lead's
    refusal died with the pass before it was recorded, so the lead spends no attempt and is asked again."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        pending_pass(world, BogusLead(world))
    assert len(facts_of(world)) == 2, 'the two accepted members were committed before the crash'
    monkeypatch.setattr(Editor, '_member_disposition', real)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert world.outcome(ids[1])['disposition'] == 'applied'
    assert world.outcome(ids[2])['disposition'] == 'applied', world.outcome(ids[2])
    assert report['parked'] == [], report
    assert charged_parkings(world, ids[0]) == ([], 0), 'the commit left the lead out: that is no refusal of its own'
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [1], \
        'the published members paid no model call; the lead was asked again, by itself'
    assert sorted(report['applied']) == sorted(ids), report
    assert len(facts_of(world)) == 3


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
    sent back for a repair round the model would only repeat. A single-source payload is answered with the
    plain replay, which asks for nothing the enrollment withholds."""

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


def test_a_batch_whose_creation_the_enrollment_refuses_charges_no_note_and_each_runs_alone(world):
    """A batch answer that creates a page the enrollment withholds is refused as a whole, and costs no note.

    The page is the answer's, not any one note's refusal: nothing of the batch is committed, no member is
    parked for it, and each note is consulted alone in the same pass, where the plain replay publishes it."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    model = counted(UnenrolledFeature(world))
    report = apply_pending(world.service, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [2, 2, 2, 1, 1], \
        'one batch conversation with its two repairs, then each note alone'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert CHILD not in world.registry()['pages'], 'the page the enrollment withholds was never created'
    assert len(facts_of(world)) == 2, 'each note published its own fact on its own turn'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'the refused batch answer charged no note'


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
    """`--ready` belongs to `dream apply`; `dream run` keeps draining everything."""
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
    """The scheduler's cheap question: what waits, and which projects are ready now.

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
    """No newer note of a project is published ahead of the one another attempt holds."""
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
    """A quota deferral of the head holds its project the way a held claim does.

    Were only the head left in `deferred`, the next note of that project would become the head of the
    next batch and be published ahead of the older one."""
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
    """The head met a quota signal inside its own consultation.

    The head opens its work page and joins the batch of its project, so the quota signal meets the
    batch's own consultation. A project whose head met a quota signal is held back for the rest of the pass, and none of
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


def test_a_lead_answered_as_recorded_never_carries_the_commit(world):
    """The batch's lead is named as recorded and the two notes after it publish. The commit is carried by the
    first note that publishes something of its own, so the lead reports archive-only, never applied."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(RecordedFromReceipt(world, ids[0], notes(3)[0], FEATURE))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == \
        ['archive-only', 'applied', 'applied'], [world.outcome(operation_id) for operation_id in ids]


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


def test_a_note_still_named_recorded_with_an_unsearched_key_when_the_repairs_run_out_goes_alone(world):
    """The model names the second note as recorded with a key this conversation never searched for, on every
    round. When the repairs run out the settle does not take that word either: the note is consulted alone in
    the same pass, uncharged, and publishes its own fact there instead of settling archive-only on that key."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(2))
    model = counted(RecordedWithForeignKeys(world, ids[1], notes(2, start=1)[0], FEATURE,
                                            keys=[['app.export']] * 3))
    report = apply_pending(world.service, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied', 'applied'], \
        [world.outcome(operation_id) for operation_id in ids]
    assert any('source' in job.payload for job in model.inner.jobs), 'the note was consulted alone'
    assert charged_parkings(world, ids[1]) == ([], 0)


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
    """The repair budget ran out on the newest note, the commit landed, and the process died in the member loop."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(NeverAccounted(world, {ids[2]}))
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        pending_pass(world, model)
    assert len(facts_of(world)) == 2, 'the two notes the answer accounted for were committed before the crash'
    monkeypatch.setattr(Editor, '_member_disposition', real)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert world.outcome(ids[0])['disposition'] == 'applied', world.outcome(ids[0])
    assert world.outcome(ids[1])['disposition'] == 'applied', world.outcome(ids[1])
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert report['waiting'] == {}, 'the pass left nothing of its own snapshot behind'
    assert 'adds no new fact' not in world.outcome(ids[2])['reason'], world.outcome(ids[2])
    assert [event for event in world.service.trace(ids[2])['events']
            if event['stage'] == 'parked'] == [], 'the note nobody answered spends none of its attempts'
    # The published members settle first, each on its own turn, so the note left is asked alone.
    assert len(model.inner.jobs) == 1 and 'sources' not in model.inner.jobs[0].payload, \
        'the published members paid no model call; the note the commit does not carry was asked again'
    assert len(facts_of(world)) == 3, 'every note of the pass ended with a fact of its own'


# ---------------------------------------------------------------------------
# A note whose request opened its work page joins its batch
#
# The first production batches lost notes exactly this way: the model opened one work page
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
# A batch answer can ask for a work page per note
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
# The consultation opens every page a batch answer names, in one round
#
# The batch has a page-list answer kind; a consultation that received it once answered with a repair,
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
# A created page belongs to the earliest accepted note that puts a fact on it
#
# The page a note's own request opened was attributed to that note, so when the split refused it in capture
# order (a fact key an earlier note of the batch claimed first) the page left the commit with its owner, and
# the batch died over a page a later member had a fact on: the production shape where a note ends with no
# fact, no narrative and no page. This section is the attribution — the page goes to the earliest accepted
# note that puts a fact on it — and the page no accepted note puts a fact on is not created at all.
# ---------------------------------------------------------------------------


class PageOwnerBatch(BatchModel):
    """The batch answer whose opened work page is owned by a note the batch refuses.

    `opener` is the note whose creation request opened the work page; it proposes a fact on that page.
    `bad_receipt` names the notes whose fact check cites a receipt no search returned, which refuses them
    for that, as their own refusal.
    `on_page` names the notes that put a fact of their own on the opened page; every other note proposes on
    the feature page. `shared` names the notes claiming the same fact key. `revises` is off: the collision
    is settled at the split, not repaired, which is the shape the production batches met.
    """

    revises = False

    def __init__(self, world, opener, on_page=(), shared=(), bad_receipt=()):
        super().__init__(world)
        self.opener, self.on_page, self.shared = opener, set(on_page), set(shared)
        self.bad_receipt = set(bad_receipt)

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' not in payload:
            return super().execute(job)
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
            receipt = ('0' * 32 if operation_id in self.bad_receipt
                       else self.world.service.reconcile(text, target)['search_id'])
            check = {'fact_key': key, 'source': operation_id,
                     'verified_match': {'result': 'not_found', 'search_id': receipt}}
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
    """The note that opened the page is refused: the page is created under the next accepted note.

    Three notes, one batch: the opener's own fact check cites a receipt no search returned, so it is refused
    for that. The third note has a fact of its own on the opened page, so the page is created with the
    batch's commit, under that note, with its fact citing that note.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    texts = notes(3)
    first = capture(world, texts[:1])[0]
    opener = capture_work_page(world, texts[1], 'export-queue', 'Rework the export queue')
    later = capture(world, texts[2:])[0]
    ids = [first, opener, later]
    model = PageOwnerBatch(world, opener, on_page=[later], bad_receipt=[opener])
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
    model = PageOwnerBatch(world, opener, on_page=[], bad_receipt=[opener])
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


class NarratingPageOwnerBatch(PageOwnerBatch):
    """`PageOwnerBatch`, where each note in `narrates` also proposes a narrative on the opened page."""

    def __init__(self, world, opener, narrates=(), **kwargs):
        super().__init__(world, opener, **kwargs)
        self.narrates, self.alone = list(narrates), []

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' not in job.payload:
            self.alone.append(job.payload['source']['text'])
        outcome = super().execute(job)
        if 'sources' not in job.payload:
            return outcome
        page = {item['source']: item['target'] for item in job.payload['new_work']}[self.opener]
        narratives = [dict(narrative_id=f'app-why-{index}', target=page, source=operation_id,
                           text='The export queue is reworked because the nightly run overran.',
                           expected_revision=None, depends_on={'facts': [], 'actions': []})
                      for index, operation_id in enumerate(self.narrates)]
        return ModelOutcome('proposal', {**outcome.result, 'narratives': narratives})


def test_a_narrative_on_a_page_no_accepted_note_puts_a_fact_on_goes_with_the_page(world):
    """A later note narrates on the opened page but puts its fact elsewhere: the page and the narrative go.

    The opener is refused for its own fact check and no accepted note keeps a fact on the page it opened,
    so the page is not created. The later note's narrative on that page leaves with it: the batch publishes
    both accepted facts in its one commit and nothing on a page it never creates. A narrative left behind
    would fail that commit and send each note back to be asked alone.
    """
    from kelmo.creation import apply_pending
    editor_mode(world)
    target = 'projects/app/export-queue.md'
    texts = notes(3)
    first = capture(world, texts[:1])[0]
    opener = capture_work_page(world, texts[1], 'export-queue', 'Rework the export queue')
    later = capture(world, texts[2:])[0]
    model = NarratingPageOwnerBatch(world, opener, narrates=[later], bad_receipt=[opener])
    before = commits(world)
    report = apply_pending(world.service, model)
    assert report['applied'] == [first, later], report
    assert report['parked'] == [opener], report
    assert model.alone == [], 'the batch publishes in its own conversation, no note is asked again alone'
    assert commits(world) == before + 1, 'and in one commit'
    registry = world.registry()
    assert target not in registry['pages'], 'no accepted note has a fact on the page, so no page is created'
    assert not [item for item in registry['narratives'].values() if item['target'] == target], \
        'the narrative on the page that is not created is not published'
    assert not os.path.lexists(world.root / 'knowledge/wiki' / target), 'and the wiki tree gains no file for it'
    assert sorted(fact['target'] for fact in registry['facts'].values()
                  if fact['note_id'].startswith((evidence_of(world, first), evidence_of(world, later)))) \
        == [FEATURE, FEATURE], 'the two accepted notes publish their facts on the page they proposed on'


# ---------------------------------------------------------------------------
# One key for one request, in a batch or alone
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
# the batch. Settling every member on that one answer is a loss of notes: four notes of
# the first batches ended `archive-only` "adds no new fact" although each held real facts. The batch spent
# its conversation, so no note spends an attempt for it, and the same pass consults each note on the
# single-source path, where a note that holds a fact publishes it and a note that holds none is declined by
# the single-source path's own rule (its own work page opened, one more ask).
# ---------------------------------------------------------------------------


def test_a_batch_answer_of_unresolved_settles_no_member_and_each_runs_alone(world, capsys):
    """A batch answered `unresolved` settles nobody and sends each member alone in the same pass. That is the
    settle's own rule, not a fault the pass recovers from: nothing is reported as failed."""
    from kelmo.creation import apply_pending
    from kelmo.editor import StateCoordinator
    editor_mode(world, max_sources=3)
    ids = capture(world, notes(3))
    model = counted(DeclinedBatchAnswer(world))
    before = commits(world)
    report = apply_pending(world.service, model)
    assert 'failed in the apply pass' not in capsys.readouterr().err
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
# Every note of a partly answered batch ends in a state of its own
#
# The guarantee of the batched pass, at the batch sizes the production passes run: whatever part of the batch
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
    answered the way the replay answers a single source. A batch that no longer holds the `recorded` note
    is answered without it.
    """

    def __init__(self, world, recorded=None, silent=(), collide=None):
        super().__init__(world)
        self.recorded, self.silent, self.collide = recorded, set(silent), collide
        self.asked = recorded is None

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' in payload and self.recorded not in {block['operation_id'] for block in payload['sources']}:
            self.recorded, self.asked = None, True
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
    first = [job for job in batched if len(job.payload['sources']) == size]
    assert first and first == batched[:len(first)], 'every note of the pass was in the first batch'
    assert len(first) <= 8, 'the batch stays within its rounds'
    # The notes captured after the collided one wait for its turn alone, then take a batch of their own.
    behind = {operation_id for operation_id in ids[4:] if operation_id not in silent} if collide else set()
    assert all({block['operation_id'] for block in job.payload['sources']} <= behind
               for job in batched[len(first):]), 'a later batch holds only the notes that waited behind it'
    collided = {collide[1]} if collide else set()
    assert len(alone) == len(silent | collided), \
        'each note the answer never accounted for, or collided, got its own consultation'
    assert report['parked'] == [], report
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in collided:
        assert charged_parkings(world, operation_id) == ([], 0), 'a collision is the answer, not the note'
    assert (report['failed'], report['skipped'], report['deferred']) == ([], [], []), report
    facts = facts_of(world)
    for operation_id in ids:
        outcome = world.outcome(operation_id)
        assert 'adds no new fact' not in (outcome or {}).get('reason', ''), outcome
        if operation_id == recorded:
            assert outcome['disposition'] == 'archive-only', outcome
            keys = [key for receipt in first[-1].payload['reconciliations'] for key in receipt['candidate_keys']]
            assert keys and all(key in outcome['reason'] for key in keys), (keys, outcome)
            assert 'app.replay-' + evidence_of(world, operation_id) not in facts
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
# One slug is one page inside a batch
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


# ---------------------------------------------------------------------------
# A batch whose publication met contention keeps its prepared intent, and its members keep their rows
#
# `_complete` leaves an intent prepared when the commit meets contention (a Git lock, a ref race): nothing
# is settled and the intent publishes on its turn. The members of that batch must not be refused under
# their own keys for it, because the recovery then commits their slices with the publisher's: a refused
# row whose fact is in the wiki, and a park charged for a note that published.
# ---------------------------------------------------------------------------


def test_a_batch_whose_commit_meets_contention_publishes_every_member_on_its_turn(world, monkeypatch):
    """Contention at the commit refuses nobody: the same pass, or the next, publishes the prepared batch."""
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    real, met = engine.publish_semantic, []

    def busy_once(root, operation_id):
        if not met:
            met.append(operation_id)
            raise KelmoError('E_BUSY', 'another writer holds the wiki lock')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', busy_once)
    report = apply_pending(world.service, model, sleep=lambda _seconds: None)
    if sorted(report['applied']) != sorted(ids):
        assert report['skipped'], report
        report = apply_pending(world.service, model, sleep=lambda _seconds: None)
    assert met, 'the commit met the contention once'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    assert model.calls == 1, 'the prepared batch published without another model call'
    assert len(facts_of(world)) == 3, 'every member published its own fact once'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'contention charges no member'
        assert world.outcome(operation_id)['disposition'] == 'applied', world.outcome(operation_id)
    assert [row['state'] for row in member_rows(world, ids)] == ['finished'] * 3, \
        'every member row finished: none was refused while its fact was being committed'


# ---------------------------------------------------------------------------
# A held note holds back the notes captured after it, never the older ones
#
# The batch [A, B, C, D] could not claim D. D holds its project back so no newer note overtakes it, but
# the notes ahead of D are older than it: one of them the answer sent alone still owes its single-source
# turn to this same pass, and holding it back behind D would leave a note waiting behind a newer one.
# ---------------------------------------------------------------------------


def test_a_note_sent_alone_keeps_its_turn_when_a_newer_note_of_its_batch_is_held(world):
    """The alone note is older than the held one, so the same pass still consults it alone."""
    editor_mode(world)
    ids = capture(world, notes(4))
    model = counted(NeverAccounted(world, {ids[2]}))
    held = world.service.journal.claim(ids[3], lease_seconds=600)
    try:
        report = pending_pass(world, model)
    finally:
        world.service.journal.release(ids[3], held)
    assert sorted(report['applied']) == sorted(ids[:3]), report
    assert [item['operation_id'] for item in report['skipped']] == [ids[3]], \
        'only the held note is skipped: the alone note ahead of it is older and keeps its turn'
    assert report['skipped'][0]['reason'] == 'held by another attempt', report
    assert 'sources' not in model.inner.jobs[-1].payload, 'the alone note was consulted on the single path'
    assert report['waiting'] == {'app': 1}, 'the held note is what the pass left'
    assert charged_parkings(world, ids[2]) == ([], 0)


def test_a_held_note_still_holds_back_the_notes_captured_after_it(world):
    """A note newer than the held one never publishes ahead of it, in a batch or alone."""
    editor_mode(world, max_sources=2)
    ids = capture(world, notes(4))
    held = world.service.journal.claim(ids[1], lease_seconds=600)
    try:
        report = pending_pass(world, counted(BatchModel(world)))
    finally:
        world.service.journal.release(ids[1], held)
    assert report['applied'] == [ids[0]], report
    assert [item['operation_id'] for item in report['skipped']] == ids[1:], report
    assert [item['reason'] for item in report['skipped']] == \
        ['held by another attempt'] + ['held back by the older note ' + ids[1]] * 2, report


# ---------------------------------------------------------------------------
# Each note pays for its own refusal, never for its batch
#
# A batch is one conversation, and what ends that conversation without an answer for a note is not that
# note's refusal: an executor that produced nothing, a request the conversation could not repair, a head
# the budget cannot carry. The notes of such a batch keep their attempts and take the single-source path
# in the same pass, where a note that cannot be placed is refused under its own key and the others publish.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_head_the_budget_cannot_carry_is_refused_alone_and_the_note_behind_it_publishes(world, batch_apply):
    """The long head does not fit even alone; the short note behind it fits, and is not charged for the head.
    The editor view's refusal is the note's own on both paths: it never stops the pass as the vault's."""
    from kelmo.contracts import EvidenceRef
    from kelmo.wiki import views
    texts = [long_notes(1, pads=40)[0], notes(2)[1]]
    ids = []
    for text in texts:
        # budget_tokens=1: the effective budget is the larger of the stored one and the vault's, written below.
        operation = world.service.create(text, project='app', selected=[FEATURE], budget_tokens=1)
        world.service.run(operation['id'])
        ids.append(operation['id'])
    reader = world.service.knowledge
    blocks = [{'operation_id': operation_id, 'text': text,
               'evidence': EvidenceRef.from_dict(world.service.journal.evidence_record(operation_id))}
              for operation_id, text in zip(ids, texts)]
    used = [views.editor_view(reader, reader.generation(), 'app', [FEATURE], 60000, sources=[block])['used_tokens']
            for block in blocks]
    budget = used[1] + 80
    assert used[0] > budget + 200, 'the fixture: the head alone is well past the budget the short note fits'
    editor_mode(world, budget=budget, batch_apply=batch_apply)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert report['parked'] == [ids[0]] and report.get('blocked') is None, report
    assert report['applied'] == [ids[1]], 'the note behind the head published on its own turn'
    assert world.service.journal.get(ids[0])['error_code'] == 'E_BUDGET'
    assert charged_parkings(world, ids[1]) == ([], 0), 'the short note is charged nothing for the head'
    assert all('sources' not in job.payload for job in model.inner.jobs), 'no batch conversation was spent'


def test_a_head_the_shrink_keeps_reads_only_the_pages_it_selected(world):
    """The newer note selected one more page and the budget dropped it: the head is not sent that page."""
    from kelmo.contracts import EvidenceRef
    from kelmo.wiki import views
    from test_editor_scoping import HUB
    texts = [notes(1)[0], long_notes(2, pads=12)[1]]
    ids = []
    for text, selected in zip(texts, ([FEATURE], [FEATURE, HUB])):
        operation = world.service.create(text, project='app', selected=selected, budget_tokens=1)
        world.service.run(operation['id'])
        ids.append(operation['id'])
    reader = world.service.knowledge
    blocks = [{'operation_id': operation_id, 'text': text,
               'evidence': EvidenceRef.from_dict(world.service.journal.evidence_record(operation_id))}
              for operation_id, text in zip(ids, texts)]

    def used(sources, selected):
        return views.editor_view(reader, reader.generation(), 'app', selected, 60000, sources=sources)['used_tokens']

    alone, both = used(blocks[:1], [FEATURE]), used(blocks, [FEATURE])
    assert both - alone > 200, 'the fixture: the newer note is what the budget cannot add'
    assert used(blocks[:1], [FEATURE, HUB]) - alone > 20, 'the fixture: the newer note selected a page of its own'
    editor_mode(world, budget=alone + 100)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    first = model.inner.jobs[0].payload
    assert [block['operation_id'] for block in first['sources']] == ids[:1], 'the shrink kept the head alone'
    assert [page['target'] for page in first['pages']] + first['omitted'] == [FEATURE], \
        'the head reads its own page, neither sent nor omitted the page only the dropped note selected'
    assert first['complete'] is True, first['omitted']
    assert report['applied'] == ids[:1], report
    assert charged_parkings(world, ids[0]) == ([], 0), 'the head pays nothing for the newer note'
    assert report['parked'] == ids[1:], 'the newer note, too long for the budget alone, is refused on its own turn'
    assert world.service.journal.get(ids[1])['error_code'] == 'E_BUDGET'


def test_an_executor_failure_of_the_batch_sends_each_note_alone_and_only_the_broken_one_parks(world):
    """The conversation failed, not the notes: two of them publish alone, the one that breaks the model parks."""
    editor_mode(world)
    texts = notes(3)
    ids = capture(world, texts)
    model = counted(BatchModel(world, broken=[texts[1]]))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'one batch conversation, then each note alone'
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert report['parked'] == [ids[1]], report
    assert charged_parkings(world, ids[1])[1] == 1, 'the broken note spent one attempt, on its own turn'
    for operation_id in (ids[0], ids[2]):
        assert charged_parkings(world, operation_id) == ([], 0)
        assert world.outcome(operation_id)['disposition'] == 'applied'


class ExpandsSentPage(BatchModel):
    """Answers a batch with an expansion of a page the batch already holds, which no round can serve."""

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload:
            self.jobs.append(job)
            return ModelOutcome('expand', {'pages': [FEATURE]})
        return super().execute(job)


def test_a_batch_request_the_conversation_cannot_serve_charges_no_note_and_each_runs_alone(world):
    """An expansion nobody can serve ends the batch's conversation; every note then publishes on its own."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(ExpandsSentPage(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'one batch conversation, then each note alone'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'the batch conversation charges no note'


def test_a_note_whose_request_cannot_be_read_parks_alone_and_its_neighbours_publish(world, monkeypatch):
    """A stored request the pass cannot build is that note's refusal, as on the single path: the pass goes on."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    editor_mode(world)
    texts = notes(3)
    ids = []
    for index, text in enumerate(texts):
        operation = world.service.create(text, project='app', selected=[FEATURE],
                                         **({'budget_tokens': 77777} if index == 1 else {}))
        world.service.run(operation['id'])
        ids.append(operation['id'])
    real = creation._editor_request

    def unreadable(service, request):
        if request.get('budget_tokens') == 77777:
            raise KelmoError('E_INPUT', 'the stored request names a page that is gone')
        return real(service, request)

    monkeypatch.setattr(creation, '_editor_request', unreadable)
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert report['parked'] == [ids[1]], report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_INPUT'
    for operation_id in (ids[0], ids[2]):
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_note_that_resolves_a_legacy_note_is_sent_alone_and_its_neighbour_settles(world, monkeypatch):
    """A batch does not publish a legacy note's resolution, the single path does: the note goes there uncharged."""
    from kelmo.creation import batch_members, pending_operations, publish_batch
    from kelmo.wiki import engine
    editor_mode(world)
    texts = ['Exports run nightly, as the feature page already states.',
             'Waters the desk plant on Mondays, which the person page already records.']
    ids = capture(world, texts)
    pairs = [(texts[0].split(',')[0], FEATURE), (texts[1].split(',')[0], 'person.md')]
    legacy = world.service.journal.evidence_record(ids[0])['content_sha256']
    monkeypatch.setattr(engine, 'resolvable_note',
                        lambda registry, reference: reference['content_sha256'] == legacy)
    batch = batch_members(world.service, pending_operations(world.service), world.service.config.batch_max_sources)
    outcome = publish_batch(world.service, batch, counted(RecordedEveryNote(world, pairs)))
    assert outcome[ids[0]] == {'state': 'waiting', 'code': 'E_PROPOSAL', 'alone': True}, outcome
    assert outcome[ids[1]] == {'state': 'applied', 'code': None}, outcome
    assert charged_parkings(world, ids[0]) == ([], 0), 'the legacy note spends no attempt for its batch'
    assert member_rows(world, ids)[0]['state'] == 'open', 'its attempt was ended, not refused'
    assert world.outcome(ids[1])['disposition'] == 'archive-only', world.outcome(ids[1])


def test_a_later_source_whose_stored_request_cannot_be_read_ends_the_batch_and_is_named_waiting(world, monkeypatch):
    """The second note's stored request cannot be read (its own refusal). The batch ends before it: the first
    note publishes, and the second and the third are named waiting, unclaimed, for their own turns."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    real = creation._editor_request
    batch = creation.batch_members(world.service, creation.pending_operations(world.service),
                                   world.service.config.batch_max_sources)
    second = next(request for operation, request, _origin in batch if operation['id'] == ids[1])

    def unreadable(service, request):
        if request is second:
            raise KelmoError('E_INPUT', 'the stored request of this note cannot be read')
        return real(service, request)

    monkeypatch.setattr(creation, '_editor_request', unreadable)
    outcome = creation.publish_batch(world.service, batch, counted(BatchModel(world)))
    assert set(outcome) == set(ids), 'every source of the batch is named'
    assert outcome[ids[0]]['state'] == 'applied', outcome
    assert outcome[ids[1]] == outcome[ids[2]] == {'state': 'waiting', 'code': None}, outcome
    for operation_id in ids[1:]:
        assert world.service.journal.claim(operation_id, lease_seconds=1) is not None, 'its claim was given back'


class RecordedAllButFirst(RecordedEveryNote):
    """`RecordedEveryNote`, except that the answer leaves the first source unaccounted for (silent)."""

    def execute(self, job):
        answer = super().execute(job)
        if 'sources' in job.payload and answer.kind == 'proposal':
            first = job.payload['sources'][0]['operation_id']
            answer.result['recorded'] = [entry for entry in answer.result['recorded'] if entry['source'] != first]
        return answer


def test_a_legacy_note_behind_a_silent_one_still_goes_alone_in_the_same_pass(world, monkeypatch):
    """The answer proposes nothing, leaves the oldest note unaccounted for, and the next note resolves a legacy
    note. The legacy note is sent alone (the single path resolves it), not held behind the silent note: it was
    already sent on its way, and waiting behind as a claimed note would hold its project for the pass."""
    from kelmo.creation import batch_members, pending_operations, publish_batch
    from kelmo.wiki import engine
    editor_mode(world)
    texts = ['Deploys freeze on Fridays, which nobody wrote down yet.',
             'Exports run nightly, as the feature page already states.',
             'Waters the desk plant on Mondays, which the person page already records.']
    ids = capture(world, texts)
    pairs = [(texts[0].split(',')[0], FEATURE), (texts[1].split(',')[0], FEATURE),
             (texts[2].split(',')[0], 'person.md')]
    legacy = world.service.journal.evidence_record(ids[1])['content_sha256']
    monkeypatch.setattr(engine, 'resolvable_note',
                        lambda registry, reference: reference['content_sha256'] == legacy)
    batch = batch_members(world.service, pending_operations(world.service), world.service.config.batch_max_sources)
    outcome = publish_batch(world.service, batch, counted(RecordedAllButFirst(world, pairs)))
    assert outcome[ids[0]].get('alone') and outcome[ids[1]].get('alone'), outcome
    assert outcome[ids[1]]['code'] == 'E_PROPOSAL', 'sent alone for the legacy note, not held as claimed'
    assert outcome[ids[2]] == {'state': 'waiting', 'code': None}, 'behind the silent note, it rejoins later'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_head_the_budget_stops_carrying_mid_conversation_is_refused_alone(world, monkeypatch):
    """The shrink that ends at a head too large drops every other source first: only the head is refused."""
    from kelmo.editor import Editor
    from kelmo.errors import HeadRefusal
    editor_mode(world)
    ids = capture(world, notes(3))
    real, calls = Editor._batch_view, []

    def head_too_large_in_the_first_round(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:  # the preflight read the batch; this is the consultation's own first view
            raise HeadRefusal('E_BUDGET', 'the oldest source no longer fits the budget')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_batch_view', head_too_large_in_the_first_round)
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['parked'] == [ids[0]], report
    assert sorted(report['applied']) == sorted(ids[1:]), 'the sources the shrink dropped publish in the same pass'
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), 'a dropped source is charged nothing'


def test_a_source_the_batch_cannot_read_ends_it_there_and_no_newer_note_passes_it(world, monkeypatch):
    """The unreadable middle note is refused on its own turn; the note behind it publishes after, not before."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._source

    def unreadable(self, operation_id, *args, **kwargs):
        if operation_id == ids[1]:
            raise KelmoError('E_STATE', 'the source is owned by an open resolution')
        return real(self, operation_id, *args, **kwargs)

    monkeypatch.setattr(Editor, '_source', unreadable)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [1, 1], 'the batch ended at the unreadable note: no batch carried the third'
    assert (report['applied'], report['parked']) == ([ids[0], ids[2]], [ids[1]]), report
    assert charged_parkings(world, ids[2]) == ([], 0)


class ReconcilesNothing(BatchModel):
    """Answers a batch with a reconciliation request whose text no search accepts."""

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload:
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': 'x' * 2000, 'target': FEATURE}]})
        return super().execute(job)


def test_a_reconcile_request_no_search_accepts_charges_no_note_and_each_runs_alone(world):
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(ReconcilesNothing(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'one batch conversation, then each note alone'
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


class NamesAStranger(BatchModel):
    """Answers a batch with a fact attributed to a source the batch never sent, on every round."""

    def execute(self, job):
        answer = super().execute(job)
        if 'sources' in job.payload and answer.kind == 'proposal':
            answer.result['facts'][0]['source'] = 'no-such-source'
        return answer


def test_a_batch_answer_the_editor_cannot_read_charges_no_note_and_each_runs_alone(world):
    """A malformed answer is the conversation's, not a note's: after its repairs each note is consulted alone."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(NamesAStranger(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 3, 3, 1, 1, 1], 'the batch spent its repairs, then each note ran alone'
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed']) == ([], []), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


@pytest.mark.parametrize('dies_at', [1, 2, 3])
def test_a_pass_that_died_while_sending_its_batch_alone_still_consults_every_note(world, monkeypatch, dies_at):
    """The death leaves some attempts ended and the other rows still holding the answer. None of them was
    committed, so every note is asked again: the batch, which answers `unresolved` once more, then each note
    alone. None settles on an answer that never named it, and none is charged."""
    from kelmo.journal import Journal
    editor_mode(world)
    ids = capture(world, notes(3))
    real, ended = Journal.action_end_attempt, []

    def dies(self, operation_id, token):
        ended.append(operation_id)
        if len(ended) == dies_at:
            raise Died('the process died while the batch was sending its notes alone')
        return real(self, operation_id, token)

    monkeypatch.setattr(Journal, 'action_end_attempt', dies)
    with pytest.raises(Died):
        pending_pass(world, counted(DeclinedBatchAnswer(world)))
    monkeypatch.setattr(Journal, 'action_end_attempt', real)
    model = counted(DeclinedBatchAnswer(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1]
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert len(facts_of(world)) == 3, 'every note was consulted and published its own fact'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'a death while sending notes alone charges none'


# ---------------------------------------------------------------------------
# A batch publishes in capture order
#
# A note the answer left out has not had its turn, so nothing captured after it may reach the wiki first:
# a newer note can correct an older one, and publishing them the other way round leaves the older claim
# standing. The batch therefore commits only the notes ahead of the first one it left out. That note is
# consulted alone next, and the notes behind it are consulted again after it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('left_out', [0, 1])
def test_a_note_the_answer_left_out_is_published_before_the_newer_notes_of_its_batch(world, left_out):
    editor_mode(world)
    ids = capture(world, notes(4))
    model = counted(NeverAccounted(world, {ids[left_out]}))
    report = pending_pass(world, model)
    assert report['applied'] == ids, 'the notes reach the wiki in the order they were captured'
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [4, 4, 4, 1, 3 - left_out], \
        'the batch with its two repairs, the note it left out alone, then the notes behind it in one batch'
    assert len(facts_of(world)) == 4
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


# ---------------------------------------------------------------------------
# A refusal of what the batch proposed as a whole is no note's refusal
# ---------------------------------------------------------------------------


def test_a_refusal_at_the_batch_commit_charges_only_the_row_that_carried_it(world, monkeypatch):
    """The commit was refused as a whole: the row that carried it is parked free, the other members wait for it,
    charged nothing, and the next pass publishes all three in one batch."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, refused = engine.publish_semantic, []

    def refuses_the_first_commit(root, operation_id):
        if not refused:
            refused.append(operation_id)
            raise KelmoError('E_FACT_CHECK', 'a receipt no longer matches the registry')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', refuses_the_first_commit)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert report['parked'] == [ids[0]], 'the publisher row carried the refused commit'
    assert report['applied'] == [], 'the newer notes wait for the note parked for the union'
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), 'no member pays for the refused commit'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 3], 'the batch, then the same batch on the next pass'


def test_a_union_the_registry_refuses_charges_no_note_and_each_runs_alone(world, monkeypatch):
    """The three proposals together were refused and none can be named: each note publishes on its own turn."""
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, refused = engine.ownership_guard, []

    def refuses_the_union(*args, **kwargs):
        if not refused:
            refused.append(1)
            raise engine.OwnershipConflict('the proposals together reach a field another owner controls')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'ownership_guard', refuses_the_union)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'one batch conversation, then each note alone'
    assert report['applied'] == ids, report
    assert (report['parked'], report['failed']) == ([], []), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_head_refusal_charges_the_note_the_editor_led_with_not_one_already_published(world, monkeypatch):
    """The oldest note was published by a pass that died before recording it: the budget refusal is the next note's."""
    from kelmo.editor import Editor
    from kelmo.errors import HeadRefusal
    from kelmo.journal import Journal
    editor_mode(world)
    first = capture(world, notes(1))
    real_stage = Journal.stage

    def dies_recording(self, operation_id, stage, **kwargs):
        if stage == 'applied':
            raise Died('the process died before the publication was recorded')
        return real_stage(self, operation_id, stage, **kwargs)

    monkeypatch.setattr(Journal, 'stage', dies_recording)
    with pytest.raises(Died):
        pending_pass(world, counted(BatchModel(world)))
    monkeypatch.setattr(Journal, 'stage', real_stage)
    assert len(facts_of(world)) == 1, 'the oldest note was published before the death'
    rest = capture(world, notes(2, start=1))
    real_view, calls = Editor._batch_view, []

    def too_large_once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HeadRefusal('E_BUDGET', 'the oldest source does not fit the budget')
        return real_view(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_batch_view', too_large_once)
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['parked'] == [rest[0]], report
    assert sorted(report['applied']) == sorted(first + rest[1:]), report
    assert charged_parkings(world, first[0]) == ([], 0), 'the published note is charged nothing'


def test_a_head_refusal_charges_the_note_the_editor_led_with_not_one_refused_before(world, monkeypatch):
    """The oldest note was refused for its own check by a pass that died before parking it. Its refused row
    settles it, so the batch it heads leads with the next note, and a budget refusal of that batch is the next
    note's. The oldest note is parked under its own code, for one attempt."""
    from kelmo.editor import Editor
    from kelmo.errors import HeadRefusal
    from kelmo.journal import Journal
    editor_mode(world, batch_apply=False)
    first = capture(world, notes(1))
    real_stage = Journal.stage

    def dies_parking(self, operation_id, stage, **kwargs):
        if stage == 'parked':
            raise Died('the process died before the refusal was recorded')
        return real_stage(self, operation_id, stage, **kwargs)

    monkeypatch.setattr(Journal, 'stage', dies_parking)
    with pytest.raises(Died):
        pending_pass(world, BogusCheck(world, {evidence_of(world, first[0])}))
    monkeypatch.setattr(Journal, 'stage', real_stage)
    assert charged_parkings(world, first[0]) == ([], 0), 'the refusal was never recorded'
    rest = capture(world, notes(2, start=1))
    editor_mode(world)
    real_view, calls = Editor._batch_view, []

    def too_large_once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HeadRefusal('E_BUDGET', 'the source the batch leads with does not fit the budget')
        return real_view(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_batch_view', too_large_once)
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['parked']) == sorted([first[0], rest[0]]) and report['applied'] == [rest[1]], report
    assert 'E_FACT_CHECK' in world.outcome(first[0])['reason'], world.outcome(first[0])
    assert 'E_BUDGET' in world.outcome(rest[0])['reason'], world.outcome(rest[0])
    assert charged_parkings(world, first[0])[1] == 1, 'the oldest note pays for its own refusal only'


# ---------------------------------------------------------------------------
# A note that comes alone after a pass died on its batch
#
# The rows of a batch a pass died on hold that batch's answer. A later pass can meet them one note at a
# time: the flag was turned off, or the pass takes a single note. Either way the note leaves the batch by
# the one rule (`Editor._leave_batch`, tests/test_dreaming_batch_recovery.py): settled from the commit when
# the batch committed it, consulted again when nothing of it was published.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('how', ['the flag turned off', 'one note per pass'])
def test_a_note_that_comes_alone_after_its_batch_died_unsettled_is_consulted_alone(world, monkeypatch, how):
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    monkeypatch.setattr(Editor, '_settle_batch', lambda *args, **kwargs: (_ for _ in ()).throw(
        Died('the pass died between the answer and the settle')))
    with pytest.raises(Died):
        apply_pending(world.service, counted(BatchModel(world)))
    monkeypatch.undo()
    model = counted(BatchModel(world))
    if how == 'the flag turned off':
        editor_mode(world, batch_apply=False)
        applied = apply_pending(world.service, model)['applied']
    else:
        applied = [operation_id for _pass in ids
                   for operation_id in apply_pending(world.service, model, limit=1)['applied']]
    assert applied == ids, 'each note published on its own turn'
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [1, 1, 1], 'the batch answer was not settled; each note was asked again'
    assert len(facts_of(world)) == 3
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'no note pays for the batch that died'


def test_a_note_that_comes_alone_after_its_batch_committed_settles_from_that_commit(world, monkeypatch):
    """The batch published all three notes and the pass died settling the second: no note is asked again."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        apply_pending(world.service, counted(BatchModel(world)))
    monkeypatch.setattr(Editor, '_member_disposition', real)
    assert len(facts_of(world)) == 3, 'the batch committed before the death'
    editor_mode(world, batch_apply=False)
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model)
    assert report['applied'] == ids, report
    assert model.calls == 0, 'a published note is settled from the commit, never asked again'
    assert [world.outcome(operation_id)['disposition'] for operation_id in ids] == ['applied'] * 3
    assert len(facts_of(world)) == 3


# ---------------------------------------------------------------------------
# What can still settle a batch answer after its pass died
#
# Only its commit. A note the commit carries is finished from it. A note it does not carry, and every note
# of a batch that committed nothing, has its attempt ended without a refusal and is asked again.
# ---------------------------------------------------------------------------


def test_a_note_its_batch_did_not_commit_is_asked_again_when_a_later_pass_holds_part_of_that_batch(world,
                                                                                                    monkeypatch):
    """The batch committed two of four notes and the pass died; the next passes take two notes each.

    The second of them holds neither the lead nor the row that carried the commit. The notes it holds were
    not in that commit, so neither is finished from it: both are asked again and publish their own fact."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(4))
    real = Editor._member_disposition
    monkeypatch.setattr(Editor, '_member_disposition', crash_with('the process died after the commit'))
    with pytest.raises(Died):
        apply_pending(world.service, counted(PartlyAnswered(world, recorded=ids[0], silent={ids[2]})))
    monkeypatch.setattr(Editor, '_member_disposition', real)
    assert len(facts_of(world)) == 1, 'the batch committed the one note ahead of the note it left out'
    model = counted(BatchModel(world))
    applied = [operation_id for _pass in range(2)
               for operation_id in apply_pending(world.service, model, limit=2)['applied']]
    assert applied == ids
    assert world.outcome(ids[0])['disposition'] == 'archive-only', 'the commit carried it as recorded'
    facts = facts_of(world)
    for operation_id in ids[1:]:
        assert world.outcome(operation_id)['disposition'] == 'applied', world.outcome(operation_id)
        assert 'app.replay-' + evidence_of(world, operation_id) in facts, 'a published note has its own fact'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


class DismissesDuringTheConversation(BatchModel):
    """The batch replay, during whose first conversation the note `victim` is dismissed."""

    def __init__(self, world, victim):
        super().__init__(world)
        self.world, self.victim, self.dismissed = world, victim, False

    def execute(self, job):
        if 'sources' in job.payload and not self.dismissed:
            self.dismissed = True
            self.world.service.editor.dismiss(self.victim, origin=self.world.origins['editor'], reason='not wanted')
        return super().execute(job)


def test_a_lead_dismissed_while_the_model_answers_takes_no_other_note_with_it(world):
    """The dismissal is the lead's alone: the other notes are asked again in the same pass, charged nothing."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(DismissesDuringTheConversation(world, ids[0]))
    report = pending_pass(world, model)
    assert report['applied'] == ids[1:], report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3, 2], \
        'the batch the dismissal ended, then the two notes it left'
    assert len(facts_of(world)) == 2
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), 'no note pays for the dismissal of another'


def test_a_refused_commit_the_pass_did_not_live_to_settle_charges_only_the_row_that_carried_it(world, monkeypatch):
    """The commit was refused on the publisher's row and the pass died before it sent the other notes alone. The
    next pass parks the publisher free for the union, the other notes wait for it uncharged and unconsulted, and
    the pass after that publishes all three."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, refused = engine.publish_semantic, []

    def refuses_the_first_commit(root, operation_id):
        if not refused:
            refused.append(operation_id)
            raise KelmoError('E_FACT_CHECK', 'a receipt no longer matches the registry')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', refuses_the_first_commit)
    real_apart = Editor._apart
    monkeypatch.setattr(Editor, '_apart', crash_with('the process died before the notes were sent alone'))
    with pytest.raises(Died):
        pending_pass(world, counted(BatchModel(world)))
    monkeypatch.setattr(Editor, '_apart', real_apart)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert report['parked'] == [ids[0]], 'the refusal stays on the row that carried the commit'
    assert (report['applied'], model.calls) == ([], 0), 'the newer notes wait for it, with no conversation'
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), 'no member pays for the refused commit'
    assert sorted(pending_pass(world, model)['applied']) == sorted(ids)


# ---------------------------------------------------------------------------
# A parked note leaves no open request behind
#
# A parked note takes a new request key on its next attempt, so nothing returns to the request it was
# refused under. The pass closes that request when it parks the note, with the note's own refusal: left
# open, it would read as an unfinished request for ever, and `requeue` refuses a note that has one.
# ---------------------------------------------------------------------------


def test_a_note_refused_in_a_batch_is_closed_under_its_own_code_and_can_be_requeued(world):
    """The last note's own fact check cites a receipt no search returned: it is refused for that, on its row."""
    editor_mode(world)
    ids = capture(world, notes(3))
    report = pending_pass(world, SharedKeyBatch(world, claiming=(), bad_receipt=ids[2:]))
    assert report['parked'] == [ids[2]], report
    assert [(row['state'], row['error_code']) for row in world.service.editor.status(ids[2])['requests']] == \
        [('refused', 'E_FACT_CHECK')], 'the refusal is on the row of the note it belongs to'
    receipt = world.service.editor.requeue(ids[2], origin=world.service._operation_origin(ids[2]),
                                           idempotency_key='requeued', project='app', selected=[FEATURE],
                                           executor=BatchModel(world), budget_tokens=60000)
    assert receipt.outcome == 'published'


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_dirty_working_tree_asks_no_model_and_charges_no_note(world, monkeypatch, batch_apply):
    """The working tree is dirty, so no publication can be committed: the pass asks the model about none of the
    notes, parks none, opens no request row, and stops, naming the failure."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))

    def dirty(*args, **kwargs):
        raise KelmoError('E_CONFLICT', 'Knowledge paths have uncommitted local changes: feature.md')

    monkeypatch.setattr(engine, 'require_clean_baseline', dirty)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert (report['parked'], report['failed'], report['applied'], model.calls) == ([], [], [], 0), report
    assert report['blocked']['error_code'] == 'E_CONFLICT', report
    assert waiting_ids(world) == ids
    for operation_id in ids:
        assert world.service.editor.status(operation_id)['requests'] == []
        assert charged_parkings(world, operation_id) == ([], 0)


# ---------------------------------------------------------------------------
# A fault of the editor on a batch is no note's fault
# ---------------------------------------------------------------------------


def test_a_fault_of_the_editor_on_a_batch_sends_each_note_alone_in_the_same_pass(world, monkeypatch):
    """A fault that is no Kelmo refusal names no note of the batch. Asking the rest as a batch again would
    spend a conversation per note still waiting; each note takes the single-source path instead."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))

    def faulty(self, *args, **kwargs):
        raise RuntimeError('a defect in the split')

    monkeypatch.setattr(Editor, '_settle_batch', faulty)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'the batch once, then each note alone'
    assert report['applied'] == ids, report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_batch_that_meets_a_row_another_request_began_is_busy_and_holds_no_lease(world, monkeypatch):
    """Another request began one member's row while this batch was opening: the batch consults nothing, ends
    the leases it had taken, and its next turn runs as if it were the first."""
    from kelmo.errors import KelmoError
    from kelmo.journal import Journal
    editor_mode(world)
    ids = capture(world, notes(3))
    real, begun = Journal.action_begin, []

    def raced_once(self, *args, **kwargs):
        begun.append(1)
        if len(begun) == 2:
            raise KelmoError('E_IDEMPOTENCY', 'This action request was recorded concurrently.')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Journal, 'action_begin', raced_once)
    model = counted(BatchModel(world))
    clock = [1000.0]
    from kelmo.creation import apply_pending
    report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                           clock=lambda: clock[0])
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['failed'], report['skipped']) == ([], [], []), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3], 'one conversation, on the second turn'


# ---------------------------------------------------------------------------
# A note's own problem ends the batch before it
#
# A stored request that can no longer be used is that note's refusal, as on the single path. In a batch it
# must not reach the notes around it: the batch ends before the note, the note is refused at the head of
# the next batch, and the notes behind it publish after it.
# ---------------------------------------------------------------------------


def three_notes_one_marked(world, marked=1):
    """Three notes; the request of the `marked` one can be told apart (see `changes_the_marked_request`)."""
    ids = []
    for index, text in enumerate(notes(3)):
        operation = world.service.create(text, project='app', selected=[FEATURE],
                                         **({'budget_tokens': 77777} if index == marked else {}))
        world.service.run(operation['id'])
        ids.append(operation['id'])
    return ids


def changes_the_marked_request(monkeypatch, change):
    from kelmo import creation
    real = creation._editor_request

    def changed(service, request):
        built = real(service, request)
        if request.get('budget_tokens') == 77777:
            change(built)
        return built

    monkeypatch.setattr(creation, '_editor_request', changed)


def test_a_note_whose_selection_names_a_page_that_is_gone_parks_alone_and_no_note_passes_it(world, monkeypatch):
    """The second of four notes selects a page that is gone. The batch ends before it, it is refused at the head
    of the next batch, and the two notes after it still go as one batch, not one conversation each."""
    editor_mode(world)
    ids = three_notes_one_marked(world) + capture(world, notes(1, start=3))
    changes_the_marked_request(monkeypatch, lambda request: request.update(
        selected=[*request['selected'], 'projects/app/gone.md']))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 'alone'
            for job in model.inner.jobs] == [1, 2], 'the batch ended before the note; the two after it are a batch'
    assert (sorted(report['applied']), report['parked']) == (sorted([ids[0], *ids[2:]]), [ids[1]]), report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_INPUT'
    for operation_id in (ids[0], *ids[2:]):
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_note_whose_request_changed_under_an_open_row_parks_alone_and_no_note_passes_it(world, monkeypatch):
    """A pass died while the model answered, so each note has an open row. Then one note's budget changes:
    its key names a different request now, which is that note's refusal and nobody else's."""
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = three_notes_one_marked(world)

    class DiesAnswering(BatchModel):
        def execute(self, job):
            raise Died('the pass died while the model answered')

    with pytest.raises(Died):
        apply_pending(world.service, DiesAnswering(world))
    changes_the_marked_request(monkeypatch, lambda request: request.update(budget_tokens=77778))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [1], 'the batch ended before the note: no batch carried the third'
    assert (report['applied'], report['parked']) == ([ids[0]], [ids[1]]), report
    assert [(row['state'], row['error_code']) for row in world.service.editor.status(ids[1])['requests']] == \
        [('refused', 'E_IDEMPOTENCY')], 'the row the dead pass opened is closed with the refusal'
    assert charged_parkings(world, ids[1])[1] == 0, 'the vault changed the request, not the note: the park is free'
    assert world.service.journal.get(ids[2])['state'] != 'applied', 'the third waits for the note it would pass'
    for operation_id in (ids[0], ids[2]):
        assert charged_parkings(world, operation_id) == ([], 0)
    assert sorted(pending_pass(world, model)['applied']) == sorted(ids[1:])


def wiki_budget_refuses(monkeypatch, times=None, added=None, after=None):
    """`check_budget` refuses its first `times` calls (every call when both are None), or, with `added`, every
    wiki that carries at least that many facts more than the published one: the wiki holds what it has and
    not what so many notes add; with `after`, every call after the first `after` ones."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    real, calls = engine.check_budget, []

    def over(root, registry):
        calls.append(1)
        if added is not None:
            refused = len(registry['facts']) >= len(engine.load_registry_view(root)['facts']) + added
        elif after is not None:
            refused = len(calls) > after
        else:
            refused = times is None or len(calls) <= times
        if refused:
            raise KelmoError('E_BUDGET', 'Wiki budget exceeded: the hub page is over its limit')
        return real(root, registry)

    monkeypatch.setattr(engine, 'check_budget', over)


def test_notes_that_together_exceed_the_wiki_budget_run_alone_and_none_is_charged(world, monkeypatch):
    """The wiki holds what it has, and not what the three notes add together: that names none of them."""
    editor_mode(world)
    ids = capture(world, notes(3))
    wiki_budget_refuses(monkeypatch, added=3)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'one batch conversation, then each note alone'
    assert report['applied'] == ids, report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_wiki_already_over_its_budget_charges_no_note_and_stops_the_pass(world, monkeypatch):
    """No note can be published into a wiki that is over its budget as it stands: asking the batch would
    spend a conversation on every pass to learn it, and charging the notes would fail every note of the vault
    for a hub none of them filled. The batch checks the wiki before it asks; the pass stops, the notes wait,
    uncharged, and publish once it is fixed."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over)
        for _ in range(2):
            report = pending_pass(world, model)
            assert (report['parked'], report['failed']) == ([], []), report
            assert report['blocked']['error_code'] == 'E_BUDGET', report
            assert waiting_ids(world) == ids
        assert model.calls == 0, 'the wiki was found over its budget before any conversation'
        for operation_id in ids:
            assert world.service.editor.status(operation_id)['requests'] == []
    report = pending_pass(world, model)
    assert report['applied'] == ids, report
    assert model.calls == 1
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_wiki_that_goes_over_its_budget_during_the_batch_charges_no_note(world, monkeypatch):
    """The wiki fit its budget when the batch checked it, and another publication filled the hub before the
    settle: the wiki is over its budget as it stands, which no note of the batch did. No note is asked alone
    and none is charged; the pass stops and the batch is asked again once the wiki is repaired."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over, after=1)
        report = pending_pass(world, model)
    assert (report['parked'], report['applied'], model.calls) == ([], [], 1), report
    assert report['blocked']['error_code'] == 'E_BUDGET', report
    report = pending_pass(world, model)
    assert report['applied'] == ids, report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3, 3], 'the batch, asked again whole'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_wiki_over_its_budget_costs_a_single_note_one_conversation_and_no_attempt(world, monkeypatch):
    """The single path learns at its settle that the wiki is over its budget as it stands. The note keeps its
    recorded answer: the next pass after the repair publishes it with no second conversation."""
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over)
        for _ in range(2):
            report = pending_pass(world, model)
            assert (report['parked'], report['applied']) == ([], []), report
            assert (report['blocked']['operation_id'], report['blocked']['error_code']) == (ids[0], 'E_BUDGET')
        assert model.calls == 1, 'the second pass settled the recorded answer again, with no conversation'
    report = pending_pass(world, model)
    assert report['applied'] == ids, report
    assert model.calls == 3, 'the first note published from its recorded answer, the other two were asked'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


# ---------------------------------------------------------------------------
# No turn of the pass lets a newer note pass an older one of its project
# ---------------------------------------------------------------------------


def test_a_ready_pass_leaves_a_note_sent_alone_behind_the_older_notes_that_wait(world):
    """Eight notes at ready 8; the answer leaves out the third and the sixth. The third runs alone. The
    project is below the number then, so the pass takes no more batches of it, and the sixth must not run
    alone ahead of the fourth and the fifth."""
    editor_mode(world)
    app = capture(world, notes(8))
    model = counted(NeverAccounted(world, {app[2], app[5]}))
    report = ready_pass(world, model)
    assert sorted(report['applied']) == sorted(app[:3]), report
    assert len([job for job in model.inner.jobs if 'sources' not in job.payload]) == 1, 'only the third ran alone'
    assert report['waiting'] == {'app': 5}, report
    assert waiting_ids(world) == app[3:]
    assert charged_parkings(world, app[5]) == ([], 0)


def test_a_note_that_waits_for_its_quota_holds_back_the_notes_between_it_and_a_newer_hold(world):
    """The fourth note is held by another attempt. The batch leaves the first out, and on its own turn the
    first meets a quota signal. The second and the third are newer than it and must wait with it."""
    import time
    from kelmo import quota
    editor_mode(world)
    ids = capture(world, notes(4))

    class QuotaAlone(NeverAccounted):
        def execute(self, job):
            if 'sources' not in job.payload:
                raise quota.QuotaWait(time.time() + 600, 'login')
            return super().execute(job)

    held = world.service.journal.claim(ids[3], lease_seconds=600)
    try:
        report = pending_pass(world, counted(QuotaAlone(world, {ids[0]})))
    finally:
        world.service.journal.release(ids[3], held)
    assert (report['applied'], report['deferred']) == ([], [ids[0]]), report
    assert {item['operation_id']: item.get('reason') for item in report['skipped']} == {
        ids[3]: 'held by another attempt',
        ids[1]: 'held back by the older note ' + ids[0],
        ids[2]: 'held back by the older note ' + ids[0]}, report


@pytest.mark.parametrize('marked', [0, 1], ids=['the note whose row carried the commit', 'another note of the commit'])
def test_a_note_the_commit_published_is_not_refused_for_a_request_that_changed_since(world, monkeypatch, marked):
    """The batch committed and the pass died before it recorded that. Then the note's budget changes. Its
    facts are in the wiki, so it settles from the commit: refusing it would send it to the model again."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    editor_mode(world)
    ids = three_notes_one_marked(world, marked)
    with monkeypatch.context() as dying:
        dying.setattr(Editor, '_member_disposition', crash_with('the pass died after the commit'))
        with pytest.raises(Died):
            apply_pending(world.service, BatchModel(world))
    assert len(facts_of(world)) == 3, 'the commit landed before the death'
    changes_the_marked_request(monkeypatch, lambda request: request.update(budget_tokens=77778))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], model.calls, len(facts_of(world))) == ([], 0, 3), report


def test_an_environment_failure_at_the_commit_parks_no_note_the_prepared_commit_carries(world, monkeypatch):
    """The wiki cannot be written, so the commit stays prepared. Its notes wait for the recovery. Parking
    them would ask the model about each again while the recovery publishes their facts."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, failed = engine.publish_semantic, []

    def unwritable_once(root, operation_id):
        if not failed:
            failed.append(operation_id)
            raise KelmoError('E_IO', 'the wiki repository could not be written')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', unwritable_once)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
    assert report['blocked']['error_code'] == 'E_IO', report
    assert waiting_ids(world) == ids
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (model.calls, len(facts_of(world))) == (1, 3), 'the recovery published the batch with no model call'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_conversation_that_loses_its_lead_frees_the_rows_it_holds(world, monkeypatch):
    """The lead's lease is lost in the middle of the conversation. The rows the batch still holds are freed,
    so its next turn consults at once and no note waits out a lease nothing will use."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    real, renewed = Editor._renew, []

    def lost_once(self, *args, **kwargs):
        renewed.append(1)
        if len(renewed) == 1:
            raise KelmoError('E_BUSY', 'This attempt no longer holds the consulting lease.')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_renew', lost_once)
    model = counted(BatchModel(world))
    clock = [1000.0]
    report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                           clock=lambda: clock[0])
    assert sorted(report['applied']) == sorted(ids), report
    assert report['skipped'] == [], report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3], 'one conversation, on the second turn'


@pytest.mark.parametrize('stolen', [0, 1], ids=['lead', 'member'])
def test_a_row_another_attempt_takes_during_the_conversation_publishes_nothing_and_charges_nothing(world, stolen):
    """Between two rounds of the batch conversation, another attempt takes the lease of one row. The next round
    finds it gone before it asks the model: nothing is published under two attempts, nothing is charged, and the
    batch publishes once that attempt lets the row go."""
    import time
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    journal, taken = world.service.journal, []

    class Steals(Reconciling):
        def execute(self, job):
            if 'sources' in job.payload and not taken:
                row = member_rows(world, ids)[stolen]
                journal.action_release(row['operation_id'], json.loads(row['observation'])['consulting'])
                assert journal.action_lease(row['operation_id'], 'another-attempt', time.time() + 600)
                taken.append(row['operation_id'])
            return super().execute(job)

    model = counted(Steals(world))
    clock = [1000.0]
    report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                           clock=lambda: clock[0])
    assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
    assert (model.calls, len(facts_of(world))) == (1, 0), 'the round after the theft never reached the model'
    journal.action_release(taken[0], 'another-attempt')
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_head_whose_evidence_cannot_be_read_parks_alone_whatever_the_failure_is(world, monkeypatch):
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._source

    def unreadable(self, operation_id, *args, **kwargs):
        if operation_id == ids[0]:
            raise KelmoError('E_IO', 'the archived evidence could not be read')
        return real(self, operation_id, *args, **kwargs)

    monkeypatch.setattr(Editor, '_source', unreadable)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert (report['parked'], sorted(report['applied'])) == ([ids[0]], sorted(ids[1:])), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2], 'the two notes behind it, as a batch'
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0)


# ---------------------------------------------------------------------------
# A vault that takes no publication refuses no note
#
# A prepared publication the recovery cannot finish, a commit that cannot be written, a dirty working tree, a
# wiki over its budget or unrenderable as it stands: no note can be published until a person repairs it, and
# none of the notes caused it. On both paths the pass stops there, every note keeps its key and its row and
# spends no attempt, a note that published before the failure settles anyway, and the owner is told.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('typed', [True, False], ids=['typed', 'os-error'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_commit_that_cannot_be_written_asks_no_model_again_until_it_is_repaired(world, monkeypatch, batch_apply,
                                                                                  typed):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as broken:
        def unwritable(root, operation_id):
            if typed:
                raise KelmoError('E_IO', 'the wiki repository could not be written')
            raise PermissionError('the wiki repository could not be written')

        broken.setattr(engine, 'publish_semantic', unwritable)
        for _ in range(2):
            report = pending_pass(world, model)
            assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
            assert report['blocked']['error_code'] == 'E_IO', report
            assert (report['blocked']['operation_id'], report['skipped']) == (ids[0], []), \
                'the pass stops at the note whose commit failed, and names no other note a fault'
        assert model.calls == 1, 'the second pass met the stuck publication before asking the model'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert len(facts_of(world)) == 3
    assert model.calls == (1 if batch_apply else 3), 'the recovery published what was prepared, with no model call'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_recovery_that_cannot_finish_a_prepared_publication_names_its_note(world, monkeypatch):
    """A note's publication stays prepared (its commit could not be written), and on the next pass the recovery
    fails before it even reaches the commit. The pass stops, and what the owner reads names that note, so the
    owner knows which publication to repair, not just that some wiki operation is unfinished."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        assert pending_pass(world, counted(BatchModel(world)))['blocked']['operation_id'] == ids[0]
    assert ids[0] in world.service.editor.awaiting_publication()
    from kelmo.editor import Editor
    with monkeypatch.context() as unreadable:
        unreadable.setattr(Editor, '_complete', lambda self, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the registry could not be read')))
        report = pending_pass(world, counted(BatchModel(world)))
    assert 'A prepared publication of note ' + ids[0] in report['blocked']['reason'], report
    assert report['parked'] == [] and charged_parkings(world, ids[0]) == ([], 0)


class RepairThenFails(BatchModel):
    """The batch replay with one note's receipt never issued; the round that should repair it fails instead."""

    def __init__(self, world, bad):
        super().__init__(world)
        self.bad, self.alone = bad, []

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' not in job.payload:
            self.alone.append(job.payload['source']['text'])
            return super().execute(job)
        if job.payload.get('refusal'):
            self.jobs.append(job)
            return ModelOutcome('failure', error_code='E_EXECUTOR', reason='the repair round produced nothing')
        outcome = super().execute(job)
        for check in outcome.result['fact_checks']:
            if check.get('source') == self.bad:
                check['verified_match'] = {'result': 'not_found', 'search_id': '0' * 32}
        return outcome


def test_a_conversation_that_fails_after_a_repair_settles_the_proposal_it_had(world):
    """A batch answer cites a receipt no search returned for one note, the editor asks for a repair, and the
    repair round fails. The conversation settles the proposal it had: the other notes publish in the batch's
    commit, the note whose receipt was never issued is refused for it, and nobody is asked again alone."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = RepairThenFails(world, ids[1])
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]) and report['parked'] == [ids[1]], report
    assert model.alone == [], 'the notes the proposal placed are not asked again alone'
    assert world.service.journal.get(ids[1])['error_code'] == 'E_FACT_CHECK'


def test_a_stuck_publication_is_named_when_another_note_s_turn_meets_it(world, monkeypatch):
    """The note whose turn meets a recovery that cannot finish is not always the note it cannot finish: an older
    note of another project heads the next pass. The pass stops at that head, and the reason names the prepared
    note, so the owner repairs the right publication."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    head = capture(world, notes(1, 'ops'), project='ops', selected=['projects/ops/runbook.md'])[0]
    stuck = capture(world, notes(1))[0]
    held, clock = world.service.journal.claim(head, lease_seconds=600), [1000.0]
    try:
        with monkeypatch.context() as broken:
            broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
                KelmoError('E_IO', 'the wiki repository could not be written')))
            report = apply_pending(world.service, counted(BatchModel(world)),
                                   sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                                   clock=lambda: clock[0])
    finally:
        world.service.journal.release(head, held)
    assert report['blocked']['operation_id'] == stuck and stuck in world.service.editor.awaiting_publication()
    with monkeypatch.context() as unreadable:
        unreadable.setattr(Editor, '_complete', lambda self, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the registry could not be read')))
        report = pending_pass(world, counted(BatchModel(world)))
    assert report['blocked']['operation_id'] == head, 'the pass stops at the note whose turn met it'
    assert 'A prepared publication of note ' + stuck in report['blocked']['reason'], report
    assert head not in report['blocked']['reason'], 'and names the note it cannot finish, not the head'
    for operation_id in (head, stuck):
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_note_its_batch_published_before_a_vault_failure_is_applied_in_that_pass(world, monkeypatch):
    """The batch commit lands, and settling the next member then meets a failure of the vault. The note whose
    row carried the commit is published, so it is applied in that same pass while the pass stops at the
    failure; the others keep their turn, and the next pass settles them from the commit with no model call."""
    from kelmo.editor import Editor
    from kelmo.errors import VaultFailure
    editor_mode(world)
    ids = capture(world, notes(3))

    def fails(self, *args, **kwargs):
        raise VaultFailure('E_IO', 'the journal cannot record the member')

    model = counted(BatchModel(world))
    with monkeypatch.context() as failing:
        failing.setattr(Editor, '_member_disposition', fails)
        report = pending_pass(world, model)
    assert report['applied'] == [ids[0]] and report['blocked']['error_code'] == 'E_IO', report
    assert report['parked'] == [] and len(facts_of(world)) == 3, report
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids[1:]) and model.calls == 1, report


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_note_published_before_the_recovery_failed_still_settles(world, monkeypatch, batch_apply):
    """The batch committed and the pass died before recording it. On the next pass the recovery fails (another
    note's prepared publication cannot be finished), but the commit that carries these notes is finished and
    publishes nothing more: the published notes are staged from it, nothing is asked, nobody is charged."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import VaultFailure
    editor_mode(world)
    ids = capture(world, notes(3))
    with monkeypatch.context() as dying:
        dying.setattr(Editor, '_member_disposition', crash_with('the pass died after the commit'))
        with pytest.raises(Died):
            apply_pending(world.service, BatchModel(world))
    editor_mode(world, batch_apply=batch_apply)

    def fails(self):
        raise VaultFailure('E_IO', 'a prepared publication of another note cannot be finished')

    monkeypatch.setattr(Editor, '_recover', fails)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], model.calls, len(facts_of(world))) == ([], 0, 3), report


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_recovery_that_cannot_run_asks_no_model_and_charges_no_note(world, monkeypatch, batch_apply):
    """The common wiki transaction cannot be finished, so every publication waits behind it: no model is asked."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))

    def stuck(root):
        raise KelmoError('E_GIT', 'the interrupted wiki transaction cannot be finished')

    monkeypatch.setattr(engine, 'recover_transaction', stuck)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert (report['parked'], report['applied'], model.calls) == ([], [], 0), report
    assert report['blocked']['error_code'] == 'E_GIT', report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_note_that_alone_overflows_the_wiki_budget_is_refused_for_it(world, monkeypatch):
    """The wiki fits its budget as it stands and does not with this note's fact: that refusal is the note's."""
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    wiki_budget_refuses(monkeypatch, times=1)
    report = pending_pass(world, counted(BatchModel(world)))
    assert (report['parked'], report['applied']) == ([ids[0]], ids[1:]) and 'blocked' not in report, report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_BUDGET'
    assert charged_parkings(world, ids[0])[1] == 1


def test_a_note_whose_own_fact_overflows_the_wiki_budget_in_a_batch_is_refused_for_it(world, monkeypatch):
    """The wiki fits as it stands, and the union does not because of the second note's fact: each note is asked
    alone and only that note is refused for the budget, under its own key; the others publish uncharged."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    culprit = 'app.replay-' + evidence_of(world, ids[1])
    real = engine.check_budget

    def over(root, registry):
        if culprit in registry['facts']:
            raise KelmoError('E_BUDGET', 'Wiki budget exceeded: the hub page is over its limit')
        return real(root, registry)

    monkeypatch.setattr(engine, 'check_budget', over)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'the batch, then each note alone'
    assert report['parked'] == ids[1:2] and 'blocked' not in report, report
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_BUDGET'
    assert charged_parkings(world, ids[1])[1] == 1
    for operation_id in (ids[0], ids[2]):
        assert charged_parkings(world, operation_id) == ([], 0)


@pytest.mark.parametrize('failure', [ValueError('a page cannot be rendered with the proposed fact'),
                                     'kelmo-error'], ids=['render', 'typed'])
def test_a_proposal_that_breaks_a_page_is_its_own_refusal(world, monkeypatch, failure):
    """The wiki takes a publication as it stands and not with the second note's fact: the union fails, each
    note is asked alone, and only the note whose own proposal breaks the page is refused, under its own key."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    culprit = 'app.replay-' + evidence_of(world, ids[1])
    real = engine.semantic_projection

    def breaks(root, registry, intent, *args, **kwargs):
        if any(item.get('fact_key') == culprit for item in intent['delta'].get('facts') or ()):
            raise (KelmoError('E_IO', 'the page could not be written for the projection')
                   if failure == 'kelmo-error' else failure)
        return real(root, registry, intent, *args, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', breaks)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [3, 1, 1, 1], 'the batch, then each note alone'
    assert report['parked'] == ids[1:2] and 'blocked' not in report, report
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_PROPOSAL'
    assert charged_parkings(world, ids[1])[1] == 1
    for operation_id in (ids[0], ids[2]):
        assert charged_parkings(world, operation_id) == ([], 0)


@pytest.mark.parametrize('failure, code', [(ValueError('a hand-edited page cannot be parsed'), 'E_CONFLICT'),
                                           (PermissionError('feature.md cannot be read'), 'E_IO')],
                         ids=['unparsable', 'unreadable'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_wiki_that_does_not_render_as_it_stands_charges_no_note(world, monkeypatch, failure, code, batch_apply):
    """The batch learns it from the whole-wiki check before it asks; the single path at its settle, after one
    conversation it keeps for the next pass."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(engine, 'semantic_projection', broken)
    monkeypatch.setattr(engine, 'render_v3', broken)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert model.calls == (0 if batch_apply else 1), [job.payload.get('sources') for job in model.inner.jobs]
    assert (report['parked'], report['applied'], report['blocked']['error_code']) == ([], [], code), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_note_with_its_own_recorded_answer_is_not_refused_for_a_vault_failure_in_its_batch(world, monkeypatch):
    """The first note was asked alone and its settle met a wiki over its budget, so its row keeps its own
    answer. The next pass batches it with two newer notes and settles that answer first, before the batch
    checks the wiki: that settle meets a vault failure again (here the working registry, which the batch's
    own check does not read), which is no refusal of the note, so the pass stops and nobody is charged."""
    from kelmo.editor import Editor
    from kelmo.errors import VaultFailure
    editor_mode(world, batch_apply=False)
    first = capture(world, notes(1))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over)
        assert pending_pass(world, model)['blocked']['operation_id'] == first[0]
    editor_mode(world)
    later = capture(world, notes(2, start=1))
    with monkeypatch.context() as broken:
        real, hit = Editor._settle, []

        def vault_once(self, *args, **kwargs):
            if not hit:
                hit.append(1)
                raise VaultFailure('E_CONFLICT', 'The working registry differs from the generation the editor read.')
            return real(self, *args, **kwargs)

        broken.setattr(Editor, '_settle', vault_once)
        report = pending_pass(world, model)
        assert hit, 'the fixture: the own answer was settled inside the batch'
        assert (report['parked'], report['applied'], report['failed']) == ([], [], []), report
        assert report['blocked']['error_code'] == 'E_CONFLICT', report
        assert model.calls == 1, 'no conversation while the vault takes no publication'
    ids = first + later
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert model.calls == 2, 'the first note published from its recorded answer, the two others in one batch'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_contention_at_the_commit_of_a_recorded_answer_refuses_nothing(world, monkeypatch):
    """The first note's own answer is recorded. The next pass settles it inside a batch, and its commit meets
    another writer once. That passes by itself: the answer publishes without another conversation, the two newer
    notes publish in one batch, and nobody is refused or charged."""
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    first = capture(world, notes(1))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over)
        assert pending_pass(world, model)['blocked']['operation_id'] == first[0]
    editor_mode(world)
    later = capture(world, notes(2, start=1))
    real, busy = engine.publish_semantic, []

    def held_once(root, operation_id):
        if not busy:
            busy.append(operation_id)
            raise KelmoError('E_BUSY', 'another writer holds the wiki lock')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', held_once)
    clock = [1000.0]
    report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60),
                           clock=lambda: clock[0])
    if len(report['applied']) < 3:
        report = pending_pass(world, model)
    ids = first + later
    assert busy, 'the fixture: the recorded answer met the contention at its commit'
    assert len(facts_of(world)) == 3, 'every note published its own fact once'
    assert model.calls == 2, 'the recorded answer published with no conversation, the two newer notes in one'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id
        assert world.outcome(operation_id)['disposition'] == 'applied', world.outcome(operation_id)


def test_a_head_that_cannot_carry_what_the_conversation_gathered_is_not_refused_for_it(world, monkeypatch):
    """The first round fits. The model then asks for a reconciliation, and the receipts the conversation
    gathered no longer fit even beside the oldest note alone (the view refuses the head). That is the
    conversation's end, not the head's refusal: every note is consulted alone, with only what is its own,
    and nobody is charged."""
    from kelmo.editor import Editor, HeadRefusal
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._batch_view

    def outgrown(self, reader, generation, project, selected, selections, budget, blocks, expansions,
                 reconciliations, candidates=()):
        if reconciliations:
            raise HeadRefusal('E_BUDGET', 'the editor budget cannot hold the source with its receipts')
        return real(self, reader, generation, project, selected, selections, budget, blocks, expansions,
                    reconciliations, candidates)

    monkeypatch.setattr(Editor, '_batch_view', outgrown)
    model = counted(Reconciling(world))
    report = pending_pass(world, model)
    assert model.inner.asked, 'the fixture: the conversation gathered a receipt before the view refused'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    assert model.calls == 4, 'one batch round, then each note alone'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
def test_a_vault_that_breaks_during_the_batch_conversation_costs_that_one_conversation(world, monkeypatch, typed):
    """The batch checks the wiki and asks the model. During the call a page becomes unreadable, so the settle's
    projection fails and the wiki as it stands no longer fits its check either: that is the vault's. The pass
    stops with no note charged, the next pass asks nothing while the page stays unreadable, and after the
    repair the batch publishes."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    broken = []

    class Breaking(BatchModel):
        def execute(self, job):
            broken.append(1)
            return super().execute(job)

    real_projection, real_budget = engine.semantic_projection, engine.check_budget

    def projection(*args, **kwargs):
        if broken:
            raise (KelmoError('E_IO', 'feature.md cannot be read') if typed
                   else PermissionError('feature.md cannot be read'))
        return real_projection(*args, **kwargs)

    def budget(*args, **kwargs):
        if broken:
            raise KelmoError('E_IO', 'feature.md cannot be read')
        return real_budget(*args, **kwargs)

    model = counted(Breaking(world))
    with monkeypatch.context() as unreadable:
        unreadable.setattr(engine, 'semantic_projection', projection)
        unreadable.setattr(engine, 'check_budget', budget)
        for _ in range(2):
            report = pending_pass(world, model)
            assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
            assert report['blocked']['error_code'] == 'E_IO', report
            assert model.calls == 1, 'the conversation the vault broke under is the only one'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert model.calls == 2, 'the repair costs the batch exactly one more conversation'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_park_refuses_to_park_a_note_its_batch_already_published(world, monkeypatch):
    """The batch committed and the pass died before it settled the members. A member's row is still open, and
    no intent is prepared: the commit is finished. Parking that member would ask the model again for facts the
    wiki already holds, so `park` refuses whatever refusal reaches it."""
    from kelmo import creation
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    with monkeypatch.context() as dying:
        dying.setattr(Editor, '_member_disposition', crash_with('the pass died after the commit'))
        with pytest.raises(Died):
            apply_pending(world.service, BatchModel(world))
    assert len(facts_of(world)) == 3
    assert ids[1] not in world.service.editor.awaiting_publication(), 'the commit is finished, not prepared'
    token = world.service.journal.claim(ids[1], lease_seconds=60)
    try:
        with pytest.raises(KelmoError) as raised:
            creation.park(world.service, ids[1], KelmoError('E_PROPOSAL', 'a refusal from elsewhere'), token, 0)
    finally:
        world.service.journal.release(ids[1], token)
    assert raised.value.code == 'E_BUSY'
    assert charged_parkings(world, ids[1]) == ([], 0)
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report


@pytest.mark.parametrize('refusal', ['origin', 'request'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_note_a_batch_commit_published_settles_whatever_its_next_turn_would_refuse(world, monkeypatch, refusal,
                                                                                      batch_apply):
    """The batch committed every note's fact and the pass died before it settled the members: the row that
    carried the commit is finished, the others are open. Before the next pass the notes' controller origin is
    revoked, or their stored request no longer reads. Either refuses a note's turn before anything else is read
    of it: the note on the finished row would be parked, charged, and asked again under a new key, and `park`
    refuses to park the others, so they would wait behind their own publication on every pass. Each one
    settles from the commit first, with no model call and no charge, whichever way the next pass runs."""
    from kelmo import creation
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    with monkeypatch.context() as dying:
        dying.setattr(Editor, '_member_disposition', crash_with('the pass died after the commit'))
        with pytest.raises(Died):
            apply_pending(world.service, BatchModel(world))
    editor = world.service.editor
    assert len(facts_of(world)) == 3 and all(editor.publication_decided(item) for item in ids)
    assert sum(editor.published_by_batch(item) for item in ids) == 2, 'the fixture: one finished row, two open'
    editor_mode(world, batch_apply=batch_apply)

    def refused(*args, **kwargs):
        raise KelmoError('E_ORIGIN' if refusal == 'origin' else 'E_INPUT', 'refused before the note is read')

    model, clock = counted(BatchModel(world)), [1000.0]
    with monkeypatch.context() as revoked:
        revoked.setattr(*((Editor, '_issued', refused) if refusal == 'origin'
                          else (creation, '_editor_request', refused)))
        report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                               clock=lambda: clock[0])
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    assert model.calls == 0 and len(facts_of(world)) == 3, 'settled from the commit, nothing asked again'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_note_published_alone_is_staged_from_its_finished_row_whatever_its_next_turn_would_refuse(world,
                                                                                                    monkeypatch):
    """A note was published alone and the pass died before it staged the note. Its origin is revoked before the
    next pass. The next turn stages it from its finished row; refused for the origin it would be parked,
    charged, and asked again under a new key for facts the wiki already holds."""
    from kelmo.creation import apply_pending
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    journal = type(world.service.journal)
    real = journal.stage

    def dies(self, operation_id, stage, **kwargs):
        if stage == 'applied':
            raise Died('the pass died after the commit')
        return real(self, operation_id, stage, **kwargs)

    with monkeypatch.context() as dying:
        dying.setattr(journal, 'stage', dies)
        with pytest.raises(Died):
            apply_pending(world.service, BatchModel(world))
    assert len(facts_of(world)) == 1 and world.service.editor.publication_decided(ids[0])

    def refused(*args, **kwargs):
        raise KelmoError('E_ORIGIN', 'the origin was revoked')

    model = counted(BatchModel(world))
    with monkeypatch.context() as revoked:
        revoked.setattr(Editor, '_issued', refused)
        report = pending_pass(world, model)
    assert report['applied'] == ids and (report['parked'], report['failed']) == ([], []), report
    assert model.calls == 0 and len(facts_of(world)) == 1, 'staged from the finished row, nothing asked again'
    assert charged_parkings(world, ids[0]) == ([], 0)


def test_a_member_whose_settle_fails_after_the_commit_settles_from_it_with_no_new_conversation(world, monkeypatch):
    """The batch committed every note's fact, and settling the first member row then failed. Its row stays open;
    the same pass sends it alone, and alone it settles from the commit that carries it."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    real, failed = Editor._member_disposition, []

    def fails_once(self, *args, **kwargs):
        if not failed:
            failed.append(1)
            raise KelmoError('E_IO', 'the evidence record could not be read')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_member_disposition', fails_once)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert failed, 'the fixture: the member settle failed once'
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert (model.calls, len(facts_of(world))) == (1, 3), 'nothing was asked again'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_park_refuses_to_park_a_note_its_prepared_publication_carries(world, monkeypatch):
    """Whatever refusal reaches `park`, a note whose publication is prepared is not parked: the recovery
    finishes it under its present key, and a new key would ask the model again for the same facts."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        pending_pass(world, counted(BatchModel(world)))
    assert ids[1] in world.service.editor.awaiting_publication()
    token = world.service.journal.claim(ids[1], lease_seconds=60)
    try:
        with pytest.raises(KelmoError) as raised:
            creation.park(world.service, ids[1], KelmoError('E_PROPOSAL', 'a refusal from elsewhere'), token, 0)
    finally:
        world.service.journal.release(ids[1], token)
    assert raised.value.code == 'E_BUSY'
    assert charged_parkings(world, ids[1]) == ([], 0)


def test_a_note_published_alone_is_not_asked_again_after_its_request_changed(world, monkeypatch):
    """The single-source path committed a note and the pass died before recording it. Then the note's budget
    changes. Its key is the pass's own, so the finished row replays: the note settles with no model call."""
    from kelmo.journal import Journal
    editor_mode(world, batch_apply=False)
    ids = three_notes_one_marked(world)
    real = Journal.stage

    def dies_recording(self, operation_id, stage, **kwargs):
        if operation_id == ids[1] and stage == 'applied':
            raise Died('the pass died after the commit')
        return real(self, operation_id, stage, **kwargs)

    with monkeypatch.context() as dying:
        dying.setattr(Journal, 'stage', dies_recording)
        with pytest.raises(Died):
            pending_pass(world, BatchModel(world))
    assert len(facts_of(world)) == 2
    changes_the_marked_request(monkeypatch, lambda request: request.update(budget_tokens=77778))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids[1:]), report
    assert (report['parked'], model.calls, len(facts_of(world))) == ([], 1, 3), 'only the third note was asked'


def test_edit_under_the_pass_s_own_key_replays_a_finished_row_whatever_its_request_has_become(world, monkeypatch):
    """The pass settles a published note before it calls `edit`, so this holds for `edit` itself as well: a row
    that finished under the pass's own key replays after the request changed (its budget raised), with no model
    call, instead of meeting `E_IDEMPOTENCY` and sending the note to the model again under a new key."""
    from kelmo import creation
    from kelmo.journal import Journal
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    real = Journal.stage

    def dies(self, operation_id, stage, **kwargs):
        if stage == 'applied':
            raise Died('the pass died after the commit')
        return real(self, operation_id, stage, **kwargs)

    with monkeypatch.context() as dying:
        dying.setattr(Journal, 'stage', dies)
        with pytest.raises(Died):
            pending_pass(world, BatchModel(world))
    service = world.service
    request = next(request for operation, request in creation.pending_operations(service)
                   if operation['id'] == ids[0])
    key, hint, _attempts = creation._attempt(service, ids[0])
    changed = dict(creation._editor_request(service, request), budget_tokens=77778)
    model = counted(BatchModel(world))
    receipt = service.editor.edit(ids[0], origin=service._operation_origin(ids[0]), idempotency_key=key,
                                  executor=model, contexts=service.context_bindings, hint=hint, own_key=True,
                                  **changed)
    assert receipt.outcome == 'published' and model.calls == 0 and len(facts_of(world)) == 1


def test_a_publication_during_the_batch_conversation_parks_every_member_free_and_publishes_nothing_stale(world):
    """Another writer published while the model answered the batch. The answer was read at the generation the
    preflight pinned, so nothing is published on it: every member is parked free (`E_STALE`), and the next pass
    asks again and publishes all of them."""
    from test_editor_scoping import NOTES
    editor_mode(world)
    ids = capture(world, notes(3))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(BatchModel):
        def execute(self, job):
            if 'sources' in job.payload and not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    model = counted(Racing(world))
    report = pending_pass(world, model)
    assert (sorted(report['parked']), report['applied'], model.calls) == (sorted(ids), [], 1), report
    for operation_id in ids:
        assert world.service.journal.get(operation_id)['error_code'] == 'E_STALE'
        assert charged_parkings(world, operation_id)[1] == 0
    assert len(facts_of(world)) == 0, 'no fact of the stale answer was published'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert (model.calls, len(facts_of(world))) == (2, 3)


def test_a_publication_during_a_batch_conversation_that_needs_a_repair_ends_it_without_repairs(world):
    """Another writer published while the model answered a batch, and the answer left one note unaccounted,
    which costs a repair round. No repair can pass once the wiki moved past the generation the conversation
    read, so the conversation ends after its one call instead of spending its repairs. Nobody is charged and
    the next pass publishes every note."""
    from test_editor_scoping import NOTES
    editor_mode(world)
    ids = capture(world, notes(3))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(NeverAccounted):
        def execute(self, job):
            if 'sources' in job.payload and not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    model = counted(Racing(world, {ids[2]}))
    pending_pass(world, model)
    assert [len(job.payload['sources']) for job in model.inner.jobs if 'sources' in job.payload] == [3], \
        'the batch spent no repair round once the wiki had moved'
    for operation_id in ids:
        assert charged_parkings(world, operation_id)[1] == 0, operation_id
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids) and len(facts_of(world)) == 3, report


def test_a_member_published_before_the_batch_stays_applied_when_the_batch_answer_is_stale(world, monkeypatch):
    """The second note was published alone and its pass died before staging it. The next pass leads a batch
    with the first note; the second one's finished row replays inside it. Another writer publishes while the
    model answers, so the answer is stale: the notes it was asked for are parked free, but the published note
    is not refused with them. It is applied, and the pass after that publishes the rest."""
    from kelmo import creation
    from test_editor_scoping import NOTES
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    journal = type(world.service.journal)
    real = journal.stage

    def dies(self, operation_id, stage, **kwargs):
        if stage == 'applied':
            raise Died('the pass died after the commit')
        return real(self, operation_id, stage, **kwargs)

    request = next(request for operation, request in creation.pending_operations(world.service)
                   if operation['id'] == ids[1])
    with monkeypatch.context() as dying:
        dying.setattr(journal, 'stage', dies)
        with pytest.raises(Died):
            creation.publish(world.service, ids[1], request, BatchModel(world), now=True)
    assert len(facts_of(world)) == 1 and world.service.editor.publication_decided(ids[1])
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(BatchModel):
        def execute(self, job):
            if 'sources' in job.payload and not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    editor_mode(world)
    model = counted(Racing(world))
    report = pending_pass(world, model)
    assert raced and report['applied'] == [ids[1]], report
    assert sorted(report['parked']) == sorted([ids[0], ids[2]]), report
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]) and len(facts_of(world)) == 3, report


def test_a_batch_commit_whose_baseline_moved_under_the_lock_is_stale_not_a_union_refusal(world, monkeypatch):
    """The wiki moved between the batch's last check and the publication lock, so its commit is refused as
    stale. That names the generation, not what the notes proposed together: the row that carried it keeps
    `E_STALE` (free, asked again on the next pass) instead of the union's code, the other notes wait behind it
    without being asked, and the next pass publishes all three."""
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, moved = engine.publish_semantic, []

    def conflict(*args, **kwargs):
        if not moved:
            moved.append(1)
            raise engine.Conflict('the knowledge baseline moved before the publication lock')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'publish_semantic', conflict)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert moved and report['applied'] == [] and report['parked'] == [ids[0]], report
    assert model.calls == 1, 'the notes behind the stale commit were not asked in that pass'
    assert world.service.journal.get(ids[0])['error_code'] == 'E_STALE', world.service.journal.get(ids[0])
    assert charged_parkings(world, ids[0])[1] == 0
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and len(facts_of(world)) == 3, report


def test_a_member_dismissed_before_its_row_opens_is_not_published(world, monkeypatch):
    """A member is dismissed after the pass claimed it and before the batch opened its row, so the dismissal
    refused no row of this attempt. The batch's settle still sees the dismissal: that note publishes nothing,
    and the others publish in the batch's commit."""
    from kelmo.editor import Editor
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Editor._answer_batch

    def dismissed_first(self, *args, **kwargs):
        world.service.journal.dismiss_semantic(ids[1], by='owner', reason='no longer needed',
                                               outcome=('archive-only', 'dismissed by owner: no longer needed'))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, '_answer_batch', dismissed_first)
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert not [fact for fact in world.registry()['facts'].values()
                if fact['note_id'].startswith(evidence_of(world, ids[1]))], 'the dismissed note publishes nothing'


def test_a_member_dismissed_during_a_stale_batch_answer_stays_dismissed(world):
    """While the model answers a batch, its second note is dismissed and another writer publishes. The answer
    is stale, so the notes it was asked for are parked free (`E_STALE`), but the dismissal the settle met is
    kept: the dismissed note is refused `E_DISMISSED`, not parked free as stale."""
    from test_editor_scoping import NOTES
    editor_mode(world)
    ids = capture(world, notes(3))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(DismissesDuringTheConversation):
        def execute(self, job):
            if 'sources' in job.payload and not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    report = pending_pass(world, counted(Racing(world, ids[1])))
    assert raced and report['applied'] == [] and len(facts_of(world)) == 0, report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_DISMISSED', world.service.journal.get(ids[1])
    for operation_id in (ids[0], ids[2]):
        assert world.service.journal.get(operation_id)['error_code'] == 'E_STALE', operation_id


def test_a_newer_note_s_own_answer_waits_for_an_older_one_its_batch_just_refused_as_stale(world, monkeypatch):
    """Two notes each hold their own recorded answer, unsettled (a fault after each answer, one at a time), and
    another writer published between the two answers. The next pass leads a batch with both: the older one's
    answer settles first and is refused as stale, a reason the vault caused, so its claim is still to come. The
    newer one's answer, current at the later generation, must not publish ahead of it in that call; it waits.
    The next pass publishes the older note, which makes the newer one's answer stale in turn (free), and the
    pass after that publishes it."""
    from kelmo.editor import Editor
    from test_editor_scoping import NOTES
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    real_settle, faulted, raced = Editor._settle, [], []

    def faults_once(self, row, *args, **kwargs):
        if row['source_operation_id'] in ids and row['source_operation_id'] not in faulted:
            faulted.append(row['source_operation_id'])
            raise RuntimeError('a fault after the answer was recorded')
        return real_settle(self, row, *args, **kwargs)

    class RacingSecond(BatchModel):
        def execute(self, job):
            if faulted and not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    model = counted(RacingSecond(world))
    with monkeypatch.context() as faulty:
        faulty.setattr(Editor, '_settle', faults_once)
        report = pending_pass(world, model)
    assert sorted(faulted) == sorted(ids) and raced and report['applied'] == [], report
    assert len(facts_of(world)) == 0 and model.calls == 2
    editor_mode(world)
    report = pending_pass(world, model)
    assert report['applied'] == [] and report['parked'] == [ids[0]], report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_STALE' and len(facts_of(world)) == 0
    report = pending_pass(world, model)
    assert report['applied'] == [ids[0]] and report['parked'] == [ids[1]], report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_STALE'
    report = pending_pass(world, model)
    assert report['applied'] == [ids[1]] and len(facts_of(world)) == 2, report
    for operation_id in ids:
        assert charged_parkings(world, operation_id)[1] == 0, operation_id


def test_a_batch_ends_at_a_head_the_vault_refused_before_it_was_parked(world, monkeypatch):
    """A note's answer was refused as stale (another writer published meanwhile) and the pass died before it
    parked the note. Two newer notes of its project are captured. The next pass leads a batch with that note:
    its row replays the refusal, which the vault caused, so its claim is still to come. The batch ends there,
    the note is parked free, and the newer notes wait for it instead of being asked and published ahead of
    it. The pass after that publishes all three."""
    from kelmo import creation
    from test_editor_scoping import NOTES
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(BatchModel):
        def execute(self, job):
            if not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    model = counted(Racing(world))
    with monkeypatch.context() as dying:
        dying.setattr(creation, 'park', crash_with('the pass died before it parked the note'))
        with pytest.raises(Died):
            creation.apply_pending(world.service, model)
    assert [row['state'] for row in world.service.journal.semantic_requests(ids[0])] == ['refused']
    ids += capture(world, notes(3)[1:])
    editor_mode(world)
    report = pending_pass(world, model)
    assert (report['parked'], report['applied'], model.calls) == ([ids[0]], [], 1), report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_STALE' and charged_parkings(world, ids[0])[1] == 0
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and model.calls == 2, report


def test_a_publication_during_a_single_conversation_ends_it_without_repairs(world):
    """The same race on the single path: the first note's answer is stale, so it is parked free after one call,
    with no repair round that could not pass; the two notes after it wait for it, and the next pass publishes
    all three in order."""
    from test_editor_scoping import NOTES
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    other, _ = world.archive('Notes now cover imports.', key='imports')
    raced = []

    class Racing(BatchModel):
        def execute(self, job):
            if not raced:
                raced.append(1)
                inner = Scripted(world.propose(facts=[world.change('app.notes-scope',
                                                                   'Notes cover exports and imports.')]))
                assert world.edit(other, inner, key='inner', selected=(NOTES,)).outcome == 'published'
            return super().execute(job)

    model = counted(Racing(world))
    report = pending_pass(world, model)
    assert (report['parked'], report['applied'], model.calls) == (ids[:1], [], 1), report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_STALE'
    assert charged_parkings(world, ids[0])[1] == 0
    report = pending_pass(world, model)
    assert (report['applied'], model.calls) == (ids, 4), report


def test_a_refusal_of_the_batch_conversation_charges_no_member_and_sends_each_alone(world):
    """The executor refused the batch request itself. That names no note: no row is refused, and each note is
    asked alone in the same pass, where it publishes."""
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))

    class RefusesTheBatch(BatchModel):
        def execute(self, job):
            if 'sources' in job.payload:
                self.jobs.append(job)
                raise KelmoError('E_INPUT', 'the adapter refused the batch request')
            return super().execute(job)

    model = counted(RefusesTheBatch(world))
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) if 'sources' in job.payload else 'alone'
            for job in model.inner.jobs] == [3, 'alone', 'alone', 'alone'], report
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_recorded_answer_of_a_newer_note_never_publishes_ahead_of_an_older_note_of_its_batch(world, monkeypatch):
    """The older note was parked for its own unreadable evidence; the newer one then recorded its answer and met
    a wiki over its budget. The next pass batches both. The older note is consulted first, as the single path
    would: the newer one's answer settles only after it, here as a free stale refusal since the wiki moved."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    model = counted(BatchModel(world))
    real = Editor._source
    with monkeypatch.context() as broken:
        broken.setattr(Editor, '_source', lambda self, operation_id, *args, **kwargs: (
            (_ for _ in ()).throw(KelmoError('E_IO', 'the archived evidence could not be read'))
            if operation_id == ids[0] else real(self, operation_id, *args, **kwargs)))
        wiki_budget_refuses(broken)
        report = pending_pass(world, model)
    assert (report['parked'], report['blocked']['operation_id']) == ([ids[0]], ids[1]), report
    editor_mode(world)
    published = []
    real_publish = Editor._complete

    def noting(self, operation_id):
        receipt = real_publish(self, operation_id)
        published.append(self.journal.action_intent(operation_id)['source_operation_id'])
        return receipt

    monkeypatch.setattr(Editor, '_complete', noting)
    report = pending_pass(world, model)
    assert published[:1] == [ids[0]], 'the older note published first'
    assert ids[0] in report['applied'], report
    assert ids[1] in report['parked'] and world.service.journal.get(ids[1])['error_code'] == 'E_STALE', report
    assert charged_parkings(world, ids[1])[1] == 0, 'the newer note pays nothing for waiting its turn'


def test_a_published_member_settles_from_its_commit_even_when_its_evidence_cannot_be_read_again(world, monkeypatch):
    """The batch committed every note; reading the second member's evidence afterwards fails, every time. The
    commit stored that evidence, so the member settles from it and the project is not held."""
    from kelmo.errors import KelmoError
    from kelmo.journal import Journal
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    committed, real_publish, real_record = [], engine.publish_semantic, Journal.evidence_record

    def publishing(root, operation_id):
        result = real_publish(root, operation_id)
        committed.append(operation_id)
        return result

    def unreadable_after_commit(self, operation_id, *args, **kwargs):
        if committed and operation_id == ids[1]:
            raise KelmoError('E_IO', 'the evidence record could not be read')
        return real_record(self, operation_id, *args, **kwargs)

    monkeypatch.setattr(engine, 'publish_semantic', publishing)
    monkeypatch.setattr(Journal, 'evidence_record', unreadable_after_commit)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['skipped'] == [], report
    assert (model.calls, len(facts_of(world))) == (1, 3)
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_batch_member_refused_before_its_request_changed_is_not_charged_for_the_old_refusal(world, monkeypatch):
    """The batch refused the second note for a collision and the pass died while parking it, after its row was
    closed. Then its budget changes. The next batch does not replay the old refusal: the note parks free under a
    new key, the third waits for it, and the pass after that publishes both in order."""
    from kelmo.journal import Journal
    editor_mode(world)
    ids = three_notes_one_marked(world, marked=1)
    real = Journal.stage

    def dies_parking(self, operation_id, stage, **kwargs):
        if stage == 'parked':
            raise Died('the pass died while parking')
        return real(self, operation_id, stage, **kwargs)

    with monkeypatch.context() as dying:
        dying.setattr(Journal, 'stage', dies_parking)
        with pytest.raises(Died):
            pending_pass(world, BatchModel(world, shared=True))
    assert [row['state'] for row in world.service.editor.status(ids[1])['requests']] == ['refused']
    changes_the_marked_request(monkeypatch, lambda request: request.update(budget_tokens=77778))
    report = pending_pass(world, counted(BatchModel(world)))
    assert ids[1] in report['parked'] and ids[2] not in report['applied'], report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_IDEMPOTENCY'
    assert charged_parkings(world, ids[1])[1] == 0, 'the old refusal is not charged under the new request'
    assert sorted(pending_pass(world, counted(BatchModel(world)))['applied']) == sorted(ids[1:])


def test_a_refused_member_is_never_staged_applied_when_a_vault_failure_ends_its_batch(world, monkeypatch):
    """The second note was refused in a batch and the pass died while parking it. The next batch replays that
    refusal and then cannot write its commit: only rows that finished count as published, so the refused note
    is not staged applied; the pass after the repair parks it for its own refusal."""
    from kelmo.errors import KelmoError
    from kelmo.journal import Journal
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real = Journal.stage

    def dies_parking(self, operation_id, stage, **kwargs):
        if stage == 'parked':
            raise Died('the pass died while parking')
        return real(self, operation_id, stage, **kwargs)

    with monkeypatch.context() as dying:
        dying.setattr(Journal, 'stage', dies_parking)
        with pytest.raises(Died):
            # The second and third notes are refused for their own receipts; the pass dies parking the second,
            # so the third's row still holds the batch answer and is asked again by the next pass.
            pending_pass(world, SharedKeyBatch(world, claiming=(), bad_receipt=ids[1:]))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        report = pending_pass(world, counted(BatchModel(world)))
    assert report['applied'] == [] and report['blocked']['error_code'] == 'E_IO', report
    report = pending_pass(world, counted(BatchModel(world)))
    assert ids[1] not in report['applied'] and ids[1] in report['parked'], report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_FACT_CHECK'


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_budget_lowered_while_a_commit_is_prepared_refuses_no_note(world, monkeypatch, batch_apply):
    """The commit could not be written, so it stays prepared. Before the repair the wiki's budget is lowered
    below what the wiki holds as it stands: the recovery's commit is refused by the budget, which is the vault's,
    so the intent stays prepared, nobody is charged, and it publishes with no model call once the budget fits."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        assert pending_pass(world, model)['blocked']['error_code'] == 'E_IO'
    calls = model.calls
    with monkeypatch.context() as lowered:
        lowered.setattr(engine.policy, 'check', lambda *args, **kwargs: {
            'ok': False, 'violations': ['the hub page is over its lowered limit']})
        report = pending_pass(world, model)
        assert (report['parked'], report['applied']) == ([], []), report
        assert report['blocked']['error_code'] == 'E_BUDGET', report
    assert world.service.journal.prepared_semantic(), 'the intent is still prepared'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert model.calls == calls + (0 if batch_apply else 2), 'the prepared commit published with no model call'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_batch_commit_refused_at_a_later_recovery_charges_no_note_for_the_union(world, monkeypatch):
    """The batch's commit could not be written, so it stayed prepared. Before the next pass the budget is lowered:
    the wiki as it stands still fits, the union of the three notes does not, because of the second note's fact.
    The recovery refuses the commit as the union's (`E_UNION`): the note whose row carried it is parked free, and
    the two after it wait for it with no conversation. On the next pass the first and the third publish and the
    second pays for its own fact on its own turn."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        assert pending_pass(world, model)['blocked']['error_code'] == 'E_IO'
    real_check = engine.policy.check

    def lowered(vault, rendered, *args, **kwargs):
        texts = [content.decode('utf-8') if isinstance(content, bytes) else (content or '')
                 for content in rendered.values()]
        if any('App note 1:' in text for text in texts):
            return {'ok': False, 'violations': ['the feature page is over its lowered limit']}
        return real_check(vault, rendered, *args, **kwargs)

    monkeypatch.setattr(engine.policy, 'check', lowered)
    calls = model.calls
    report = pending_pass(world, model)
    assert (report['parked'], report['applied']) == ([ids[0]], []), report
    assert model.calls == calls, 'the notes behind the refused commit are not asked in this pass'
    assert world.service.journal.get(ids[0])['error_code'] == 'E_UNION'
    assert charged_parkings(world, ids[0])[1] == 0, 'the union is no note of it'
    for operation_id in ids[1:]:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id
    report = pending_pass(world, model)
    assert (report['parked'], sorted(report['applied'])) == ([ids[1]], sorted([ids[0], ids[2]])), report
    assert world.service.journal.get(ids[1])['error_code'] == 'E_BUDGET'
    assert charged_parkings(world, ids[1])[1] == 1, 'the second note pays for its own fact, asked alone'


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_read_that_fails_once_during_the_projection_charges_no_note(world, monkeypatch, batch_apply, typed):
    """The first read of the projection meets an unreadable page (the storage's `OSError`, or a reader's
    `E_IO`) and the second does not: the storage's moment, read again, never the proposal's refusal."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    real, failed = engine.semantic_projection, []

    def once(*args, **kwargs):
        if not failed:
            failed.append(1)
            if typed:
                raise KelmoError('E_IO', 'Knowledge pages could not be read for the projection; retry.')
            raise OSError(24, 'Too many open files')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', once)
    report = pending_pass(world, counted(BatchModel(world)))
    assert failed and sorted(report['applied']) == sorted(ids) and report['parked'] == [], report


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
def test_a_budget_check_that_cannot_read_once_charges_no_note(world, monkeypatch, typed):
    """The budget check of a note's result meets an unreadable page once (`E_IO`, or the storage's own
    `OSError`) and reads it on the second try: the note publishes in the same pass, uncharged, and no note is
    named a fault of the pass."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    real, failed = engine.check_budget, []

    def once(*args, **kwargs):
        if not failed:
            failed.append(1)
            if typed:
                raise KelmoError('E_IO', 'Knowledge pages could not be read for the budget check; retry.')
            raise OSError(24, 'Too many open files')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'check_budget', once)
    report = pending_pass(world, counted(BatchModel(world)))
    assert failed and report['applied'] == ids and report['parked'] == [] and report['skipped'] == [], report


@pytest.mark.parametrize('kind', ['os', 'io', 'git'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_working_tree_check_that_cannot_read_once_before_the_model_is_asked_charges_no_note(world, monkeypatch,
                                                                                             batch_apply, kind):
    """The check that the wiki's files are clean, made before any model is asked, fails one read (the storage's
    `OSError`, a reader's `E_IO`, `git status` failing with `E_GIT`) and reads on the second try. It is read
    again in place: the pass does not stop, nobody is charged and every note publishes in the same pass."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(2))
    real, failed = engine.require_clean_baseline, []

    def once(*args, **kwargs):
        if not failed:
            failed.append(1)
            if kind == 'os':
                raise OSError(24, 'Too many open files')
            if kind == 'io':
                raise KelmoError('E_IO', 'Knowledge pages could not be read; retry.')
            raise KelmoError('E_GIT', 'git status failed')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'require_clean_baseline', once)
    report = pending_pass(world, counted(BatchModel(world)))
    assert failed and report.get('blocked') is None, report
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
def test_a_budget_check_that_cannot_read_twice_stops_the_pass_with_nothing_prepared(world, monkeypatch, typed):
    """The storage fails every read of the budget check: the second failure is classified, and since the wiki as
    it stands cannot be read either, the pass stops. No intent was prepared from checks that never ran."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))

    def unreadable(*args, **kwargs):
        if typed:
            raise KelmoError('E_IO', 'Knowledge pages could not be read for the budget check; retry.')
        raise OSError(5, 'Input/output error')

    monkeypatch.setattr(engine, 'check_budget', unreadable)
    report = pending_pass(world, counted(BatchModel(world)))
    assert (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_IO', report
    assert list(world.service.journal.prepared_semantic()) == [], 'nothing is prepared from checks that never ran'
    assert charged_parkings(world, ids[0]) == ([], 0)


class Asks(BatchModel):
    """The batch replay, noting in `asked` that the model was asked: what a test breaks after that breaks at
    the settle of the answer, not before the conversation."""

    def __init__(self, world, asked):
        super().__init__(world)
        self.asked = asked

    def execute(self, job):
        self.asked.append(1)
        return super().execute(job)


@pytest.mark.parametrize('kind', ['io', 'git', 'os'])
@pytest.mark.parametrize('where', ['projection', 'check'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_read_that_fails_twice_and_then_reads_charges_no_note(world, monkeypatch, batch_apply, where, kind):
    """The storage fails two reads in a row of the projection or of the checks (a reader's `E_IO`, a Git
    command that fails, an `OSError`), and the next one passes: the wiki as it stands read whole in between,
    so it was the storage's moment, never what the proposal adds. No note is charged and every note publishes
    in the same pass."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    name = 'semantic_projection' if where == 'projection' else 'check_budget'
    real, asked, failed = getattr(engine, name), [], []

    def flaky(*args, **kwargs):
        if asked and len(failed) < 2:
            failed.append(1)
            if kind == 'io':
                raise KelmoError('E_IO', 'Knowledge pages could not be read; retry.')
            if kind == 'git':
                raise KelmoError('E_GIT', 'Git status failed with exit code 128. Check repository locks, signing, '
                                          'or hooks, then retry.')
            raise OSError(5, 'Input/output error')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, name, flaky)
    report = pending_pass(world, counted(Asks(world, asked)))
    assert len(failed) == 2 and sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report['skipped'], report.get('blocked')) == ([], [], None), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('where', ['projection', 'check'])
def test_a_read_the_vault_also_failed_stays_the_vault_s_when_the_vault_reads_again(world, monkeypatch, where):
    """The second failure to read is classified while the wiki as it stands cannot be read either, and the
    storage recovers right after. The failure stays the vault's: the pass stops and nobody is charged. A later
    look at a vault that reads again must not turn it into the proposal's refusal."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    asked = []
    # Two failed reads of the step, then the one look at the wiki as it stands fails too.
    failures = {'semantic_projection': 2, 'check_budget': 1} if where == 'projection' else {'check_budget': 3}

    def failing(name, real):
        def call(*args, **kwargs):
            if asked and failures.get(name, 0) > 0:
                failures[name] -= 1
                raise OSError(5, 'Input/output error')
            return real(*args, **kwargs)
        return call

    for name in ('semantic_projection', 'check_budget'):
        monkeypatch.setattr(engine, name, failing(name, getattr(engine, name)))
    report = pending_pass(world, counted(Asks(world, asked)))
    assert not any(failures.values()), failures
    assert (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_IO', report
    assert charged_parkings(world, ids[0]) == ([], 0)
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['applied'] == ids, report


def test_a_read_that_fails_only_with_the_proposal_is_its_own_once_the_vault_reads_whole(world, monkeypatch):
    """Every read of one note's projection fails while the wiki as it stands reads whole. After the third read
    the failure is what that proposal adds: it alone is refused (`E_PROPOSAL`), and the next note publishes."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    real, reads = engine.semantic_projection, []

    def failing(root, registry, intent, **kwargs):
        if intent.get('source_operation_id') == ids[0]:
            reads.append(1)
            raise OSError(36, 'File name too long')
        return real(root, registry, intent, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', failing)
    report = pending_pass(world, counted(BatchModel(world)))
    assert (report['parked'], report['applied']) == ([ids[0]], [ids[1]]), report
    assert len(reads) == 3, 'read again at once, then once more after the vault read whole'
    assert world.service.journal.get(ids[0])['error_code'] == 'E_PROPOSAL'
    assert charged_parkings(world, ids[0])[1] == 1


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_check_that_cannot_render_the_wiki_as_it_stands_stops_the_pass(world, monkeypatch, batch_apply):
    """The renderer fails (`ValueError`) on every registry the checks render, the wiki as it stands included:
    no publication can succeed, so the pass stops with an alert, nobody is charged and nothing is prepared."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    real, asked = engine.check_budget, []

    def broken(*args, **kwargs):
        if asked:
            raise ValueError('a page cannot be rendered')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'check_budget', broken)
    model = counted(Asks(world, asked))
    report = pending_pass(world, model)
    assert (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_CONFLICT', report
    assert model.calls == 1, 'the pass stops at the first settle'
    assert list(world.service.journal.prepared_semantic()) == []
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_check_that_cannot_render_only_what_the_note_adds_refuses_that_note(world, monkeypatch):
    """The renderer fails (`ValueError`) only on the registry with the first note's fact: that is the note's
    own refusal (`E_PROPOSAL`), and the next note publishes in the same pass."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(2))
    real = engine.check_budget

    def broken(vault, registry, *args, **kwargs):
        if 'App note 0:' in json.dumps(registry, default=str):
            raise ValueError('the page with this fact cannot be rendered')
        return real(vault, registry, *args, **kwargs)

    monkeypatch.setattr(engine, 'check_budget', broken)
    report = pending_pass(world, counted(BatchModel(world)))
    assert (report['parked'], report['applied']) == ([ids[0]], [ids[1]]), report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_PROPOSAL'
    assert charged_parkings(world, ids[0])[1] == 1


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_committed_page_the_settle_cannot_read_stops_the_pass_and_charges_no_note(world, monkeypatch,
                                                                                  batch_apply, typed):
    """The committed bytes of a page the publication rewrites cannot be read for its baseline. No note wrote
    them: the pass stops, nobody is charged and nothing is prepared, and once the page reads every note
    publishes."""
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    reader = world.service.knowledge
    real, asked = reader.page, []

    def unreadable(*args, **kwargs):
        if asked:
            if typed:
                raise KelmoError('E_IO', 'a committed page could not be read')
            raise OSError(5, 'Input/output error')
        return real(*args, **kwargs)

    model = counted(Asks(world, asked))
    with monkeypatch.context() as patched:
        patched.setattr(reader, 'page', unreadable)
        report = pending_pass(world, model)
    assert asked and (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_IO', report
    assert model.calls == 1
    assert list(world.service.journal.prepared_semantic()) == []
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_working_registry_the_settle_cannot_read_stops_the_pass_and_charges_no_note(world, monkeypatch,
                                                                                    batch_apply, typed):
    """The working registry cannot be read once the model has answered (missing, or the storage fails). That is
    the vault's: the pass stops, nobody is charged, and once it reads every note publishes."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    real, asked = engine.load_registry_view, []

    def unreadable(*args, **kwargs):
        if asked:
            if typed:
                raise KelmoError('E_STATE', 'Bound registry is missing; opening a store cannot initialize it.')
            raise OSError(5, 'Input/output error')
        return real(*args, **kwargs)

    model = counted(Asks(world, asked))
    with monkeypatch.context() as patched:
        patched.setattr(engine, 'load_registry_view', unreadable)
        report = pending_pass(world, model)
    assert asked and (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == ('E_STATE' if typed else 'E_IO'), report
    assert model.calls == 1
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_contention_reading_the_vault_s_own_state_at_the_settle_is_waited_out(world, monkeypatch, batch_apply):
    """The settle reads the committed registry's fingerprint once the model has answered and meets contention
    (another writer holds the repository). That is neither the vault's failure nor any note's: the pass does not
    stop, nobody is charged, and every note publishes."""
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    knowledge = type(world.service.knowledge)
    real, asked, busy = knowledge.registry_fingerprint, [], []

    def held(self, *args, **kwargs):
        if asked and not busy:
            busy.append(1)
            raise KelmoError('E_BUSY', 'Git rev-parse found the repository locked by another Git process.')
        return real(self, *args, **kwargs)

    model, clock = counted(Asks(world, asked)), [1000.0]
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, 'registry_fingerprint', held)
        report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60),
                               clock=lambda: clock[0])
    assert busy and report.get('blocked') is None and report['parked'] == [] and report['failed'] == [], report
    pending_pass(world, model)
    for operation_id in ids:
        assert world.service.journal.get(operation_id)['state'] == 'applied', operation_id
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('typed', [True, False], ids=['kelmo-error', 'os-error'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_working_registry_that_cannot_be_read_before_any_note_is_asked_stops_the_pass(world, monkeypatch,
                                                                                      batch_apply, typed):
    """The pass has listed its notes, and then the registry a note's stored request is read against cannot be
    read: no model is asked, the pass stops, and no note is charged for a registry it never wrote."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    real, listed = engine.load_registry_view, []

    def unreadable(*args, **kwargs):
        if not listed:
            listed.append(1)  # the pass lists its waiting notes against the registry first
            return real(*args, **kwargs)
        if typed:
            raise KelmoError('E_STATE', 'Bound registry is missing; opening a store cannot initialize it.')
        raise OSError(5, 'Input/output error')

    model = counted(BatchModel(world))
    with monkeypatch.context() as patched:
        patched.setattr(engine, 'load_registry_view', unreadable)
        report = pending_pass(world, model)
    assert (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == ('E_STATE' if typed else 'E_IO'), report
    assert model.calls == 0
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_recovery_stopped_by_an_unfinished_project_rename_names_the_wiki_operation(world, monkeypatch):
    """Nothing is prepared and the recovery stops at a project rename it cannot resume: the pass stops, and the
    reason names the wiki's own operation, not a prepared publication of a note. (The report still says which
    note the pass was handling when it stopped; the failure names no note as its cause.)"""
    from kelmo import projects
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=False)
    capture(world, notes(1))

    def stuck(service):
        raise KelmoError('E_GIT', 'the interrupted rename cannot be resumed')

    monkeypatch.setattr(projects, '_resume_rename', stuck)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert model.calls == 0 and report['blocked']['error_code'] == 'E_GIT', report
    reason = report['blocked']['reason']
    assert 'unfinished wiki operation' in reason and 'of note' not in reason, reason


def test_a_member_dismissed_while_the_model_answers_is_left_out_and_the_rest_publish(world):
    """A note of the batch other than its lead is dismissed during the conversation. The settle refuses it
    alone (`E_DISMISSED`) and publishes nothing of it; the notes around it publish in the same commit."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(DismissesDuringTheConversation(world, ids[1]))
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [3], 'one conversation, no note asked again'
    facts = facts_of(world)
    assert 'app.replay-' + evidence_of(world, ids[1]) not in facts, 'nothing of the dismissed note is published'
    assert len(facts) == 2
    assert world.service.journal.get(ids[1])['error_code'] == 'E_DISMISSED', world.service.journal.get(ids[1])


def test_a_claimed_path_at_the_check_is_the_note_s_whatever_the_vault_is(world, monkeypatch):
    """The check of a note's result finds a page it creates whose path holds a file (`E_OWNERSHIP`), while the
    wiki as it stands does not fit its budget either. The claimed path is the note's own: it is refused for it,
    not stopped as the vault's."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    real_check, real_budget, met = engine.require_clean_baseline, engine.check_budget, []

    def claimed(*args, **kwargs):
        if not met and 'previous' in kwargs:
            met.append(1)
            raise engine.OwnershipConflict('page projects/app/new.md: a created page cannot claim an existing file')
        return real_check(*args, **kwargs)

    def over(*args, **kwargs):
        if met:
            raise KelmoError('E_BUDGET', 'the wiki is over its budget as it stands')
        return real_budget(*args, **kwargs)

    monkeypatch.setattr(engine, 'require_clean_baseline', claimed)
    monkeypatch.setattr(engine, 'check_budget', over)
    report = pending_pass(world, counted(BatchModel(world)))
    assert met and report['parked'] == ids and report.get('blocked') is None, report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_OWNERSHIP'


def test_a_vault_failure_reading_the_head_s_source_charges_no_head(world, monkeypatch):
    """The batch reads its head's evidence and meets a failure of the vault. That is no refusal of the head:
    the pass stops, nobody is charged, and once the vault reads the batch publishes."""
    from kelmo.editor import Editor
    from kelmo.errors import VaultFailure
    editor_mode(world)
    ids = capture(world, notes(3))
    real, failed = Editor._source, []

    def unreadable(self, operation_id, *args, **kwargs):
        if not failed and operation_id == ids[0]:
            failed.append(1)
            raise VaultFailure('E_IO', 'the archive could not be read')
        return real(self, operation_id, *args, **kwargs)

    monkeypatch.setattr(Editor, '_source', unreadable)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert failed and (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_IO' and model.calls == 0, report
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def _storage_fault(kind):
    import sqlite3
    if kind == 'busy':
        return sqlite3.OperationalError('database is locked')
    if kind == 'sqlite':
        return sqlite3.OperationalError('disk I/O error')
    return OSError(5, 'Input/output error')


def _archive_fails(patched, world, failing):
    """While `failing()` holds, the archive database cannot be read either (its writer row, the look's read)."""
    adapter = type(world.service.archive_api)
    real = adapter.assert_writer

    def unreadable(self):
        if failing():
            raise _storage_fault('sqlite')
        return real(self)

    patched.setattr(adapter, 'assert_writer', unreadable)


@pytest.mark.parametrize('batch_apply, lasting, member, kind', [
    (True, True, 'head', 'os'), (True, True, 'later', 'os'), (True, False, 'later', 'os'),
    (True, True, 'later', 'sqlite'), (False, True, 'later', 'os'), (False, False, 'head', 'os'),
    (False, True, 'head', 'sqlite')])
def test_an_archive_the_storage_cannot_read_charges_no_note(world, monkeypatch, batch_apply, lasting, member, kind):
    """The storage cannot read a note's captured source (an I/O error, or the archive database's), and the
    archive as a whole does not read either. No note is at fault. A failure that lasts stops the pass where it
    is met, with nobody charged: before any model is asked when a batch reads its members, after the older
    notes when they go one at a time. One that clears is read again, and every note publishes in the same
    pass."""
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    target = ids[0] if member == 'head' else ids[1]
    service = type(world.service)
    real, failed, broken = service.source_packet, [], [True]

    def unreadable(self, operation_id):
        if operation_id == target and (lasting or not failed):
            failed.append(1)
            raise _storage_fault(kind)
        return real(self, operation_id)

    model = counted(BatchModel(world))
    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', unreadable)
        _archive_fails(patched, world, lambda: lasting and broken[0])
        report = pending_pass(world, model)
    assert failed and report['parked'] == [] and report['failed'] == [], report
    if lasting:
        assert report['blocked']['error_code'] == 'E_IO', report
        assert report['applied'] == ([] if batch_apply or member == 'head' else [ids[0]]), report
        assert model.calls == (0 if batch_apply or member == 'head' else 1), model.calls
        report = pending_pass(world, model)
    assert report.get('blocked') is None and sorted(report['applied']) == sorted(
        ids if not lasting or batch_apply or member == 'head' else ids[1:]), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('failures', [1, 2])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_captured_source_that_fails_to_read_and_then_reads_charges_no_note(world, monkeypatch, batch_apply,
                                                                            failures):
    """The storage fails to read the head's captured source once, or twice, and then reads it. The archive as a
    whole is looked at only after the second failure: one failure is read again whatever a look would say,
    and after two the archive reads, so the source is read a third time. Either way every note publishes in
    the same pass, nobody is charged and the pass does not stop."""
    from kelmo.editor import Editor
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    service = type(world.service)
    real, real_look, failed, looked = service.source_packet, Editor._archive_readable, [], []

    def flaky(self, operation_id):
        if operation_id == ids[0] and len(failed) < failures:
            failed.append(1)
            raise _storage_fault('os')
        return real(self, operation_id)

    def look(self):
        # Two failures meet an archive that reads; a look after one failure would meet one that does not.
        looked.append(1)
        return real_look(self) if failures == 2 else False

    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', flaky)
        patched.setattr(Editor, '_archive_readable', look)
        report = pending_pass(world, counted(BatchModel(world)))
    assert len(failed) == failures, 'the fixture: the head\'s source failed to read'
    assert report.get('blocked') is None and sorted(report['applied']) == sorted(ids), report
    assert len(looked) == failures - 1, 'the archive is looked at after the second failure, never after the first'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_third_failure_to_read_a_source_is_the_vault_s_when_the_archive_no_longer_reads(world, monkeypatch,
                                                                                         batch_apply):
    """The head's captured source fails to read three times. After the second the archive as a whole reads,
    so the source is read again; after the third it no longer does. The failure is the storage's, not that
    note's record: the pass stops with nobody charged, and the next pass publishes every note."""
    from kelmo.editor import Editor
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    service = type(world.service)
    real, real_look, failed, looked = service.source_packet, Editor._archive_readable, [], []

    def flaky(self, operation_id):
        if operation_id == ids[0] and len(failed) < 3:
            failed.append(1)
            raise _storage_fault('os')
        return real(self, operation_id)

    def look(self):
        looked.append(1)
        return real_look(self) if len(looked) == 1 else False

    model = counted(BatchModel(world))
    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', flaky)
        patched.setattr(Editor, '_archive_readable', look)
        report = pending_pass(world, model)
    assert len(failed) == 3 and len(looked) == 2, (failed, looked)
    assert report['parked'] == [] and report['failed'] == [], 'the storage failed, not the note'
    assert report['blocked']['error_code'] == 'E_IO', report
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason='root reads a directory without permission')
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_directory_of_captured_sources_that_cannot_be_read_stops_the_pass(world, batch_apply):
    """The directory that holds every captured source cannot be read. Every note's evidence fails to read and
    the archive does not read as a whole: the pass stops before any model is asked, nobody is charged, and the
    notes publish once the directory reads again."""
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    sources = world.service.config.internal(Path('.state/sources'))
    mode = sources.stat().st_mode
    model = counted(BatchModel(world))
    sources.chmod(0)
    try:
        report = pending_pass(world, model)
    finally:
        sources.chmod(mode)
    assert report['blocked']['error_code'] == 'E_IO' and model.calls == 0, report
    assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('member', ['head', 'later'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_source_whose_own_record_alone_cannot_be_read_is_refused_for_it(world, monkeypatch, batch_apply, member):
    """One note's captured source cannot be read, while the rest of the archive reads. That record is the
    note's own, like a captured source whose bytes no longer match: it is refused for it (`E_SOURCE`, one
    attempt), it is never sent to the model, and the other notes publish in the same pass. One damaged record
    never stops the vault."""
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    target = ids[0] if member == 'head' else ids[1]
    service = type(world.service)
    real = service.source_packet

    def unreadable(self, operation_id):
        if operation_id == target:
            raise _storage_fault('os')
        return real(self, operation_id)

    model = counted(BatchModel(world))
    monkeypatch.setattr(service, 'source_packet', unreadable)
    report = pending_pass(world, model)
    others = [operation_id for operation_id in ids if operation_id != target]
    assert report.get('blocked') is None and report['parked'] == [target], report
    assert sorted(report['applied']) == sorted(others), report
    assert world.service.journal.get(target)['error_code'] == 'E_SOURCE'
    assert charged_parkings(world, target)[1] == 1
    for operation_id in others:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id
    sent = {block['operation_id'] for job in model.inner.jobs for block in job.payload.get('sources') or ()}
    assert target not in sent, 'the note whose record cannot be read is never asked about'
    if batch_apply and member == 'head':
        assert sorted(sent) == sorted(others), \
            'only the head is refused: the other two still share one batch conversation'


@pytest.mark.parametrize('damage', ['evidence', 'text'])
@pytest.mark.parametrize('member', ['head', 'later'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_source_whose_own_record_is_damaged_is_refused_for_it(world, batch_apply, member, damage):
    """One note's own record reads but is damaged: its archived evidence reference lost a field, or its captured
    file is no longer UTF-8 text. That is the note's own record, exactly like one the storage cannot read: it is
    refused for it (`E_SOURCE`, one attempt), never sent to the model, and the other notes publish in the same
    pass. A damage that raises no storage error is not passed by in silence."""
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    target = ids[0] if member == 'head' else ids[1]
    journal = world.service.journal
    if damage == 'evidence':
        row = journal.conn.execute("SELECT sequence, details FROM events WHERE operation_id=? AND stage='archived' "
                                   'ORDER BY sequence DESC LIMIT 1', (target,)).fetchone()
        details = dict(json.loads(row['details']), evidence_ref={'archive_id': 'a damaged reference'})
        with journal.conn:
            journal.conn.execute('UPDATE events SET details=? WHERE sequence=?', (json.dumps(details), row['sequence']))
    else:
        world.service.source_path(journal.get(target)).write_bytes(b'\xff\xfe not text any more')
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    others = [operation_id for operation_id in ids if operation_id != target]
    assert report.get('blocked') is None and report['parked'] == [target], report
    assert sorted(report['applied']) == sorted(others), report
    assert journal.get(target)['error_code'] == 'E_SOURCE'
    assert charged_parkings(world, target)[1] == 1
    sent = {block['operation_id'] for job in model.inner.jobs for block in job.payload.get('sources') or ()}
    assert target not in sent, 'the note whose record is damaged is never asked about'


@pytest.mark.parametrize('member', ['head', 'later'])
@pytest.mark.parametrize('kind', ['sqlite', 'kelmo'])
def test_contention_met_while_looking_at_the_archive_is_waited_out(world, monkeypatch, kind, member):
    """A member's captured source (the head's, or a later one's) fails twice to read, and the look at the
    archive as a whole then meets contention (a busy database, or an archive that answers `E_BUSY`).
    Contention is neither the vault's failure nor that note's record, and never the head's own refusal: the
    pass does not stop, nobody is charged, and every note publishes once the archive is free."""
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))
    service, adapter = type(world.service), type(world.service.archive_api)
    real_packet, looked = service.source_packet, []

    def unreadable(self, operation_id):
        if operation_id == ids[0 if member == 'head' else 1]:
            raise _storage_fault('os')
        return real_packet(self, operation_id)

    def busy(self):
        looked.append(1)
        if kind == 'sqlite':
            raise _storage_fault('busy')
        raise KelmoError('E_BUSY', 'The archive is busy; retry.')

    model, clock = counted(BatchModel(world)), [1000.0]
    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', unreadable)
        patched.setattr(adapter, 'assert_writer', busy)
        report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                               clock=lambda: clock[0])
    assert looked and report.get('blocked') is None and report['failed'] == [], report
    assert report['parked'] == [], 'contention is no refusal of any note'
    pending_pass(world, model)
    for operation_id in ids:
        assert world.service.journal.get(operation_id)['state'] == 'applied', operation_id
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('call', [1, 2], ids=['before-the-ask', 'consultation'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_committed_registry_that_fails_one_read_costs_no_conversation(world, monkeypatch, batch_apply, call):
    """The committed registry fails one read (a Git object read) and then reads: the check before the model is
    asked (the first read of a pass), or the consultation's own read (the second). It is read again in place:
    the pass does not stop, every note publishes in it with the conversations it needs anyway, and no note is
    sent alone for it."""
    from kelmo.errors import KelmoError
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    knowledge = type(world.service.knowledge)
    real, reads = knowledge.registry, []

    def flaky(self, *args, **kwargs):
        reads.append(1)
        if len(reads) == call:
            raise KelmoError('E_GIT', 'Knowledge Git object inspection failed: cat-file')
        return real(self, *args, **kwargs)

    model = counted(BatchModel(world))
    monkeypatch.setattr(knowledge, 'registry', flaky)
    report = pending_pass(world, model)
    assert len(reads) > call and sorted(report['applied']) == sorted(ids), report
    assert (report['parked'], report.get('blocked')) == ([], None), report
    assert model.calls == (1 if batch_apply else 3), model.calls
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('member', ['head', 'later'])
def test_a_busy_archive_database_is_contention_not_a_failure_of_the_vault(world, monkeypatch, member):
    """The archive database is busy while the batch reads a member's source. That is contention: the pass does
    not stop for it, nobody is charged, and the notes publish once it is free."""
    editor_mode(world)
    ids = capture(world, notes(3))
    target = ids[0] if member == 'head' else ids[1]
    service = type(world.service)
    real, busy = service.source_packet, []

    def locked(self, operation_id):
        if operation_id == target:
            busy.append(1)
            raise _storage_fault('busy')
        return real(self, operation_id)

    from kelmo.creation import apply_pending
    model, clock = counted(BatchModel(world)), [1000.0]
    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', locked)
        report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                               clock=lambda: clock[0])
    assert busy and report.get('blocked') is None and report['parked'] == [] and report['failed'] == [], report
    assert model.calls == 0 and ids[0] not in report['applied'], 'the batch ends before any note is asked'
    pending_pass(world, model)
    for operation_id in ids:
        assert world.service.journal.get(operation_id)['state'] == 'applied', operation_id
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('fault', ['vault', 'once', 'own'])
def test_a_source_the_storage_cannot_read_at_the_batch_settle_charges_no_other_note(world, monkeypatch, fault):
    """The model has answered the batch, and the storage then cannot read a member's captured source while the
    commit is built. Nothing is prepared. When the archive does not read as a whole, nobody is charged or asked
    alone: the pass stops, and once the storage reads, the batch is asked once more and publishes. A failure
    that clears is read again and publishes in the same pass, with the one conversation. When only that
    member's record stopped reading, the call names no note: each note goes alone, the readable ones publish,
    and that one is refused for its own record."""
    editor_mode(world)
    ids = capture(world, notes(3))
    service = type(world.service)
    real, asked, failed, broken = service.source_packet, [], [], [True]

    def unreadable(self, operation_id):
        if asked and operation_id == ids[1] and (fault != 'once' or not failed):
            failed.append(1)
            raise _storage_fault('os')
        return real(self, operation_id)

    model = counted(Asks(world, asked))
    with monkeypatch.context() as patched:
        patched.setattr(service, 'source_packet', unreadable)
        _archive_fails(patched, world, lambda: fault == 'vault' and asked and broken[0])
        report = pending_pass(world, model)
    assert failed and report['failed'] == [], report
    if fault == 'own':
        assert report.get('blocked') is None and report['parked'] == [ids[1]], report
        assert sorted(report['applied']) == sorted([ids[0], ids[2]]), report
        assert world.service.journal.get(ids[1])['error_code'] == 'E_SOURCE'
        assert model.calls == 3, 'the batch, then each readable note alone'
        assert [charged_parkings(world, operation_id)[1] for operation_id in ids] == [0, 1, 0]
        return
    assert report['parked'] == [] and model.calls == 1, 'one conversation, and no note is asked alone after it'
    if fault == 'vault':
        assert report['blocked']['error_code'] == 'E_IO' and report['applied'] == [], report
        assert list(world.service.journal.prepared_semantic()) == []
        for operation_id in ids:
            outcome = world.outcome(operation_id)
            assert outcome['disposition'] == 'unresolved', outcome
            assert 'The archive could not be read' in outcome['reason'], 'each note says why it waits'
        report = pending_pass(world, model)
        assert model.calls == 2, 'one more conversation after the repair, never one per note'
    assert report.get('blocked') is None and sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('shape', ['single-source', 'batch-of-one'])
def test_a_commit_only_the_note_s_own_addition_no_longer_fits_refuses_that_note(world, monkeypatch, shape):
    """The commit of an intent carrying one note's facts is refused for the wiki budget while the wiki as it
    stands fits (a budget lowered after the prepare). That is the note's own size against the vault's budget:
    it is refused `E_BUDGET` and charged, as its consultation would refuse it. In a batch where it is the only
    note the commit carries, the refusal is still its own, not the union's."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=shape == 'batch-of-one')
    ids = capture(world, notes(2 if shape == 'batch-of-one' else 1))
    model = SharedKeyBatch(world, claiming=(), bad_receipt=ids[1:]) if shape == 'batch-of-one' else BatchModel(world)
    real, refused = engine.publish_semantic, []

    def over_once(root, operation_id):
        if not refused:
            refused.append(operation_id)
            raise KelmoError('E_BUDGET', 'the wiki no longer fits its lowered budget with this publication')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', over_once)
    report = pending_pass(world, model)  # unwrapped: `Counted` would hide `revises`
    assert refused and ids[0] in report['parked'] and report.get('blocked') is None, report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_BUDGET'
    assert charged_parkings(world, ids[0])[1] == 1


@pytest.mark.parametrize('kind', ['git', 'io', 'os'])
@pytest.mark.parametrize('failures', [None, 1, 2], ids=['lasting', 'once', 'twice'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_committed_pages_the_editor_view_cannot_read_charge_no_note(world, monkeypatch, batch_apply, failures, kind):
    """The editor view cannot read the committed pages it sends (a Git object read fails, a reader's `E_IO`, or
    the storage).
    No note wrote them. A failure that lasts, or fails the read made again at once, stops the pass before any
    model is asked, with nobody charged; one that clears on that second read lets every note publish in the
    same pass."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    real, failed = views.editor_view, []

    def unreadable(*args, **kwargs):
        if failures is None or len(failed) < failures:
            failed.append(1)
            if kind == 'git':
                raise KelmoError('E_GIT', 'Knowledge Git object inspection failed: cat-file')
            if kind == 'io':
                raise KelmoError('E_IO', 'Knowledge pages could not be read; retry.')
            raise OSError(5, 'Input/output error')
        return real(*args, **kwargs)

    model = counted(BatchModel(world))
    with monkeypatch.context() as patched:
        patched.setattr(views, 'editor_view', unreadable)
        report = pending_pass(world, model)
    assert failed and report['parked'] == [], report
    if failures != 1:
        assert report['applied'] == [] and model.calls == 0, (report, model.calls)
        assert report['blocked']['error_code'] == ('E_GIT' if kind == 'git' else 'E_IO'), report
        report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_contention_met_reading_committed_pages_is_waited_out_not_reported(world, monkeypatch, batch_apply):
    """The editor view meets contention reading the committed pages (`E_BUSY`), on every read of the pass.
    Contention is not an outage of the vault: the pass does not stop on it and alerts nobody, nobody is charged,
    and every note publishes once the reader is free. The consultation that met it ends the lease it holds, so
    the next pass consults at once instead of waiting out a lease nothing uses."""
    import time
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import views
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    met = []

    def busy(*args, **kwargs):
        met.append(1)
        raise KelmoError('E_BUSY', 'The knowledge repository is locked; retry.')

    model, clock = counted(BatchModel(world)), [1000.0]
    with monkeypatch.context() as patched:
        patched.setattr(views, 'editor_view', busy)
        report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                               clock=lambda: clock[0])
    assert met and report.get('blocked') is None, report
    assert (report['parked'], report['failed'], report['applied']) == ([], [], []), report
    held = [row['operation_id'] for row in world.service.journal.conn.execute(
        "SELECT operation_id, observation FROM action_requests WHERE kind='semantic' AND state='open'")
            if json.loads(row['observation'] or '{}').get('expires_at', 0) > time.time()]
    assert held == [], 'the consultation that met contention ends the lease it holds'
    report = apply_pending(world.service, model, sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600),
                           clock=lambda: clock[0])
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_typed_batch_projection_failure_on_a_wiki_that_does_not_render_costs_one_conversation(world, monkeypatch):
    """The batch's projection fails with a typed refusal while the wiki as it stands does not render either.
    That is the vault's, decided at the batch settle: the pass stops after the one conversation, and no note is
    asked alone to meet the same failure again."""
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    capture(world, notes(3))
    real_projection, real_budget, asked = engine.semantic_projection, engine.check_budget, []

    def typed(*args, **kwargs):
        if asked:
            raise KelmoError('E_HOT', 'HOT selection must name an exact published page: projects/app/gone.md')
        return real_projection(*args, **kwargs)

    def broken(*args, **kwargs):
        if asked:
            raise ValueError('a page cannot be rendered')
        return real_budget(*args, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', typed)
    monkeypatch.setattr(engine, 'check_budget', broken)
    model = counted(Asks(world, asked))
    report = pending_pass(world, model)
    assert report['blocked']['error_code'] == 'E_HOT' and report['parked'] == [], report
    assert model.calls == 1, 'stopped at the batch settle, before any note is asked alone'


def test_a_vault_that_breaks_between_the_two_looks_stays_the_vault_s(world, monkeypatch):
    """Every read of the note's projection fails. The read's own look finds the wiki whole, so the failure
    reaches the classification, whose look then finds the wiki broken: that is the vault's, named as the
    storage's (`E_IO`), and nobody is charged."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    real_projection, real_budget, asked, looks = engine.semantic_projection, engine.check_budget, [], []

    def failing(*args, **kwargs):
        if asked:
            raise OSError(5, 'Input/output error')
        return real_projection(*args, **kwargs)

    def breaks_after_one_look(*args, **kwargs):
        if asked:
            looks.append(1)
            if len(looks) > 1:
                raise OSError(5, 'Input/output error')
        return real_budget(*args, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', failing)
    monkeypatch.setattr(engine, 'check_budget', breaks_after_one_look)
    report = pending_pass(world, counted(Asks(world, asked)))
    assert len(looks) == 2 and report['parked'] == [], report
    assert report['blocked']['error_code'] == 'E_IO', report
    assert charged_parkings(world, ids[0]) == ([], 0)


@pytest.mark.parametrize('where', ['second-look', 'projection'])
def test_contention_met_while_classifying_a_failure_neither_stops_the_pass_nor_refuses(world, monkeypatch, where):
    """Contention reaches the classification itself: a Git lock on the second look after the note's own budget
    refusal, or a projection that meets another writer. It passes by itself: no vault failure, no alert, and
    the note meets exactly what is its own (here a budget refusal, or a publication)."""
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(1))
    sent, calls = [], []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    if where == 'second-look':
        wiki_budget_refuses(monkeypatch, added=1)
        real, target, call = engine.require_clean_baseline, 'require_clean_baseline', 3
    else:
        real, target, call = engine.semantic_projection, 'semantic_projection', 1

    def locked_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == call:
            raise KelmoError('E_BUSY', 'Git status found the repository locked by another Git process')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, target, locked_once)
    clock = [1000.0]
    report = apply_pending(world.service, counted(BatchModel(world)),
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60), clock=lambda: clock[0])
    assert len(calls) > call and 'blocked' not in report and sent == [], report
    if where == 'second-look':
        assert report['parked'] == ids and world.service.journal.get(ids[0])['error_code'] == 'E_BUDGET', report
    else:
        assert report['applied'] == ids and charged_parkings(world, ids[0]) == ([], 0), report


def test_a_registry_that_differs_from_the_generation_at_the_settle_stops_the_pass_and_charges_no_note(
        world, monkeypatch):
    """While the model answers the batch, the working registry stops matching the generation the model read
    (a hand edit `git status` cannot see, modelled by the registry the settle reads). That is the vault's: the
    pass stops naming the registry, nobody is charged, and once the registry matches again the batch publishes."""
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    real, asked = engine.load_registry_view, []

    def differing(*args, **kwargs):
        registry = real(*args, **kwargs)
        return dict(registry, edited_by_hand=True) if asked else registry

    class Asked(BatchModel):
        def execute(self, job):
            asked.append(1)
            return super().execute(job)

    model = counted(Asked(world))
    with monkeypatch.context() as edited:
        edited.setattr(engine, 'load_registry_view', differing)
        report = pending_pass(world, model)
    assert asked and (report['applied'], report['parked']) == ([], []), report
    assert report['blocked']['error_code'] == 'E_CONFLICT' and 'registry' in report['blocked']['reason'], report
    assert model.calls == 1, 'the batch settle stops at the registry, before any note is asked alone'
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_code_a_head_refusal_also_uses_raised_by_the_conversation_charges_no_head(world):
    """A library executor raises a Kelmo refusal in the middle of the batch conversation (the command adapter
    returns failures instead). Its code, `E_PERMISSION`, is one a head's own refusal can carry, but ownership
    decides, not the code: the conversation's error names no note, and each one goes alone, uncharged."""
    from kelmo.errors import KelmoError
    editor_mode(world)
    ids = capture(world, notes(3))

    class Raising(BatchModel):
        def execute(self, job):
            if 'sources' in job.payload:
                self.jobs.append(job)
                raise KelmoError('E_PERMISSION', 'the adapter refused the batch payload')
            return super().execute(job)

    model = Raising(world)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['parked'] == [], report
    assert len(model.jobs) == 4, 'one batch call, then each note alone'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_projection_that_cannot_read_while_the_tree_is_dirty_is_the_vault_s(world, monkeypatch, batch_apply):
    """The settle's projection fails every read (the storage's `OSError`) while a person's edit has left the
    wiki's files dirty. The look at the vault as it stands finds the edit: no publication can succeed, so the
    failure is the vault's, the pass stops, and no note is charged for the person's edit."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3 if batch_apply else 1))
    model = counted(BatchModel(world))
    real_projection, real_clean = engine.semantic_projection, engine.require_clean_baseline

    def unreadable(*args, **kwargs):
        if model.calls:
            raise OSError(5, 'Input/output error')
        return real_projection(*args, **kwargs)

    def dirty_after_the_ask(*args, **kwargs):
        # The look at the vault as it stands passes no `previous`; the check of a proposal's registry does.
        if model.calls and 'previous' not in kwargs:
            raise engine.DirtyKnowledge(['projects/app/feature.md'])
        return real_clean(*args, **kwargs)

    monkeypatch.setattr(engine, 'semantic_projection', unreadable)
    monkeypatch.setattr(engine, 'require_clean_baseline', dirty_after_the_ask)
    report = pending_pass(world, model)
    assert report.get('blocked') is not None and report['parked'] == [] and report['failed'] == [], report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
@pytest.mark.parametrize('where', ['projection', 'check'])
def test_a_settle_that_finds_the_tree_dirty_is_the_vault_s_even_once_the_tree_is_clean(world, monkeypatch, where,
                                                                                      batch_apply):
    """The settle's projection, or its check of the registry it would commit, meets uncommitted edits on a page,
    and the person restores the page before the failure is classified (modelled by one `DirtyKnowledge` while
    the real tree stays clean, so a look at the vault now finds it whole). The edits were there while this
    proposal was checked: that is the vault's, so the pass stops, the note is not charged, and the next pass
    publishes the recorded answer with no conversation."""
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3 if batch_apply else 1))
    name = 'semantic_projection' if where == 'projection' else 'require_clean_baseline'
    real, met = getattr(engine, name), []

    def dirty_once(*args, **kwargs):
        # The check passes `previous`; the look at the vault as it stands does not.
        if not met and (where == 'projection' or 'previous' in kwargs):
            met.append(1)
            raise engine.DirtyKnowledge(['projects/app/feature.md'])
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, name, dirty_once)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert met and report['blocked']['error_code'] == 'E_CONFLICT' and report['parked'] == [], report
    assert model.calls == 1, 'the pass stops at the settle, before any note is asked alone'
    report = pending_pass(world, model)
    # A single note's answer resumes with no model call; a batch answer is asked again.
    assert sorted(report['applied']) == sorted(ids) and model.calls == (2 if batch_apply else 1), \
        (report, model.calls)
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_wiki_that_goes_over_its_budget_during_the_batch_conversation_costs_that_one_conversation(
        world, monkeypatch):
    """The batch checks the wiki, then asks the model; meanwhile the wiki goes over its budget as it stands. The
    settle's own check finds it, and so does the second look: that is the vault's. The pass stops with nobody
    charged, the next pass asks nothing while the wiki stays over, and after the repair the batch publishes."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as over:
        wiki_budget_refuses(over, after=1)
        for _ in range(2):
            report = pending_pass(world, model)
            assert (report['applied'], report['parked']) == ([], []), report
            assert report['blocked']['error_code'] == 'E_BUDGET', report
            assert model.calls == 1, 'the conversation the wiki outgrew is the only one'
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert model.calls == 2, 'the repair costs the batch exactly one more conversation'
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id


def test_a_busy_journal_during_the_recovery_is_contention_and_a_broken_invariant_ends_the_pass(world, monkeypatch):
    """The recovery meets a busy SQLite database once: that passes by itself, nothing is blocked or told, and the
    notes publish. A broken invariant raised by the recovery is a defect, not the vault's state: it ends the
    pass as it is, instead of being reported as an outage."""
    import sqlite3

    from kelmo import editor
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    editor_mode(world)
    ids = capture(world, notes(3))
    sent, real, met = [], editor.recover_publications, []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)

    def busy_once(service):
        if not met:
            met.append(1)
            raise sqlite3.OperationalError('database is locked')
        return real(service)

    monkeypatch.setattr(editor, 'recover_publications', busy_once)
    clock = [1000.0]
    report = apply_pending(world.service, counted(BatchModel(world)),
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60), clock=lambda: clock[0])
    assert met and 'blocked' not in report and sorted(report['applied']) == sorted(ids) and sent == [], report

    later = capture(world, notes(1, start=3))

    def broken(service):
        raise AssertionError('the baton was held across the recovery')

    monkeypatch.setattr(editor, 'recover_publications', broken)
    with pytest.raises(AssertionError):
        pending_pass(world, counted(BatchModel(world)))
    assert waiting_ids(world) == later and sent == [], 'a defect is no outage: nothing is told'


def test_without_an_alert_command_the_outage_line_is_printed_once_a_day(tmp_path, capsys):
    from kelmo.alerts import REMINDER_SECONDS, VaultWatch
    watch = VaultWatch(tmp_path / 'vault-outage.json')
    for now in (1000.0, 1600.0, 2200.0, 1000.0 + REMINDER_SECONDS):
        watch.blocked('note-1', 'E_IO', 'the wiki repository could not be written', now=now)
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith('kelmo worker: alert:')]
    assert len(lines) == 2 and lines[1].endswith('Day 2.'), lines


@pytest.mark.parametrize('call', [1, 2], ids=['before-the-model', 'at-the-settle'])
@pytest.mark.parametrize('batch_apply', [True, False], ids=['batched', 'single-source'])
def test_a_git_lock_met_by_a_check_is_contention_not_a_failure_or_a_refusal(world, monkeypatch, call, batch_apply):
    """Another Git process holds the index for a moment: the clean-tree check meets `E_BUSY`, before the model
    is asked or when the answer's result is checked. That passes by itself: no vault failure is reported, no
    note is refused under that code (a refusal stored as contention would be replayed on every turn), and every
    note publishes, the same pass or the next, with nobody charged."""
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=batch_apply)
    ids = capture(world, notes(3))
    sent, calls = [], []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    real = engine.require_clean_baseline

    def locked_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == call:
            raise KelmoError('E_BUSY', 'Git status found the repository locked by another Git process')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'require_clean_baseline', locked_once)
    clock = [1000.0]
    model = counted(BatchModel(world))
    report = apply_pending(world.service, model,
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60), clock=lambda: clock[0])
    assert len(calls) > call and 'blocked' not in report and report['parked'] == [], report
    assert sorted(report['applied']) == sorted(ids), 'the same pass waited its turn and published every note'
    # The single path asks once per note and resumes a recorded answer. A batch answer is never resumed (only
    # the call that recorded it settles it), so a lock met at the batch's settle costs exactly one more
    # conversation, and the notes are never scattered into conversations of their own.
    assert model.calls == (3 if not batch_apply else 2 if call == 2 else 1), model.calls
    assert len(facts_of(world)) == 3 and sent == [], sent
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), operation_id
        assert world.outcome(operation_id)['disposition'] == 'applied', world.outcome(operation_id)


@pytest.mark.parametrize('code', ['E_BUSY', 'E_CLAIM'])
@pytest.mark.parametrize('where', ['commit', 'recovery'])
def test_contention_at_the_commit_or_the_recovery_is_no_vault_failure(world, monkeypatch, where, code):
    """Another publisher holds the wiki once, at the commit or at the recovery. That passes by itself: nothing is
    blocked, nobody is alerted or charged, and the notes publish."""
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ids = capture(world, notes(3))
    sent, busy = [], []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    name = 'publish_semantic' if where == 'commit' else 'recover_transaction'
    real = getattr(engine, name)

    def held_once(*args, **kwargs):
        if not busy:
            busy.append(1)
            raise KelmoError(code, 'another publisher holds the wiki')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, name, held_once)
    clock = [1000.0]
    report = apply_pending(world.service, counted(BatchModel(world)),
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + 60), clock=lambda: clock[0])
    assert busy and 'blocked' not in report and report['parked'] == [], report
    report = pending_pass(world, counted(BatchModel(world))) if len(report['applied']) < 3 else report
    assert len(facts_of(world)) == 3 and sent == [], sent
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)


def test_a_pass_that_publishes_and_then_meets_a_new_outage_closes_the_old_one_and_tells_the_new(world, monkeypatch):
    """An outage was told and then repaired. The next pass publishes a note of one project, then meets a commit
    that cannot be written on another: the owner hears that the old outage is over, then of the new one."""
    from kelmo.alerts import VaultWatch
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    ops = capture(world, notes(1, 'ops'), project='ops', selected=['projects/ops/runbook.md'])
    app = capture(world, notes(2))
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_IO', 'the old outage')
    real = engine.publish_semantic

    def app_cannot_be_written(root, operation_id):
        if world.service.journal.action_intent(operation_id)['source_operation_id'] in app:
            raise KelmoError('E_IO', 'the wiki repository could not be written')
        return real(root, operation_id)

    monkeypatch.setattr(engine, 'publish_semantic', app_cannot_be_written)
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['applied'] == ops and report['blocked']['error_code'] == 'E_IO', report
    assert len(sent) == 3, sent
    assert 'published again' in sent[1] and 'no note can be published' in sent[2], sent


def test_a_note_refused_alone_is_asked_again_after_its_request_changed(world, monkeypatch):
    """The single path refused the first note for the wiki budget and the pass died before parking it. Then the
    note's budget changes, which may be the very repair. Its refused row does not replay the old refusal: the
    note parks free under a new key, the notes after it wait for it, and the next pass publishes all three in
    order."""
    from kelmo import creation
    editor_mode(world, batch_apply=False)
    ids = three_notes_one_marked(world, marked=0)
    with monkeypatch.context() as dying:
        wiki_budget_refuses(dying, times=1)
        dying.setattr(creation, 'park', lambda *args, **kwargs: (_ for _ in ()).throw(Died('died parking')))
        with pytest.raises(Died):
            pending_pass(world, BatchModel(world))
    assert [row['state'] for row in world.service.editor.status(ids[0])['requests']] == ['refused']
    changes_the_marked_request(monkeypatch, lambda request: request.update(budget_tokens=77778))
    report = pending_pass(world, counted(BatchModel(world)))
    assert (report['parked'], report['applied']) == ([ids[0]], []), report
    assert world.service.journal.get(ids[0])['error_code'] == 'E_IDEMPOTENCY'
    assert charged_parkings(world, ids[0])[1] == 0, 'the old refusal is not charged under the new request'
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['applied'] == ids, report


def test_a_note_whose_own_publication_is_prepared_settles_before_its_batch_is_asked(world, monkeypatch):
    """A single-source pass left the oldest note's publication prepared. The batch that comes next finishes it
    first and asks the model about the other two only, without meeting its own recovery as contention."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        assert pending_pass(world, model)['blocked']['operation_id'] == ids[0]
    editor_mode(world)
    real, batches = Editor.edit_batch, []

    def counting(self, *args, **kwargs):
        batches.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, 'edit_batch', counting)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['skipped'] == [], report
    assert len(batches) == 1, 'one batch call: the prepared row settled before the batch leased its rows'
    assert [len(job.payload['sources']) if 'sources' in job.payload else 1
            for job in model.inner.jobs] == [1, 2], 'the first note alone once, then the two others as a batch'


def test_a_later_member_whose_own_publication_is_prepared_settles_before_its_batch_is_asked(world, monkeypatch):
    """A single-source pass refused the oldest note for its own reason, prepared the next note's publication and
    stopped at the vault. The batch the oldest note leads next finishes that publication first and asks the
    model about the other two only: it never leases a row whose own intent is prepared."""
    from kelmo.editor import Editor
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world, batch_apply=False)
    texts = notes(3)
    ids = capture(world, texts)
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        report = pending_pass(world, BatchModel(world, broken=[texts[0].strip()]))
    assert report['parked'] == [ids[0]] and report['blocked']['operation_id'] == ids[1], report
    editor_mode(world)
    model = counted(BatchModel(world))
    real, batches = Editor.edit_batch, []

    def counting(self, *args, **kwargs):
        batches.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Editor, 'edit_batch', counting)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids) and report['skipped'] == [], report
    assert len(batches) == 1, 'one batch call: the prepared row settled before the batch leased its rows'
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2], 'the oldest and newest notes, asked once'


class LeaseWatcher(Reconciling):
    """The reconciling batch replay, noting how long each member row of the batch is leased in each round."""

    def __init__(self, world, ids):
        super().__init__(world)
        self.ids, self.leases = ids, []

    def execute(self, job):
        if 'sources' in job.payload:
            self.leases.append([json.loads(row['observation'])['expires_at']
                                for row in member_rows(self.world, self.ids[1:])])
        return super().execute(job)


def test_a_conversation_of_several_rounds_keeps_every_row_of_its_batch_leased(world):
    """The lead's lease is renewed before every round, and so is every member's: a member row whose lease lapsed
    mid-conversation could be answered by another attempt while this batch answers it too."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = LeaseWatcher(world, ids)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert len(model.leases) == 2, 'a reconcile round, then the proposal'
    assert all(later > earlier for earlier, later in zip(*model.leases)), model.leases


def test_a_vault_outage_is_told_once_while_it_lasts_and_once_when_it_ends(world, monkeypatch):
    from kelmo.alerts import VaultWatch
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    from kelmo.creation import _attempt
    editor_mode(world)
    ids = capture(world, notes(3))
    keys = [_attempt(world.service, operation_id)[0] for operation_id in ids]
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    with monkeypatch.context() as dirty:
        dirty.setattr(engine, 'require_clean_baseline', lambda *args, **kwargs: (_ for _ in ()).throw(
            KelmoError('E_CONFLICT', 'Knowledge paths have uncommitted local changes: feature.md')))
        outage = [counted(BatchModel(world)), counted(BatchModel(world))]
        for model in outage:
            pending_pass(world, model)
    assert [model.calls for model in outage] == [0, 0], 'a vault that takes no publication costs no conversation'
    assert len(sent) == 1 and 'no note can be published' in sent[0] and 'E_CONFLICT' in sent[0], sent
    assert waiting_ids(world) == ids, 'every note keeps its place'
    assert [_attempt(world.service, operation_id)[0] for operation_id in ids] == keys, 'and its request key'
    assert all(charged_parkings(world, operation_id) == ([], 0) for operation_id in ids)
    report = pending_pass(world, counted(BatchModel(world)))
    assert sorted(report['applied']) == sorted(ids), report
    assert len(sent) == 2 and 'published again after ' in sent[1], 'it says how long the outage lasted'
    pending_pass(world, counted(BatchModel(world)))
    assert len(sent) == 2, 'the outage is over: nothing more is told'


def test_vault_watch_reminds_once_a_day_and_never_reads_executor_answers(tmp_path, monkeypatch):
    from kelmo.alerts import REMINDER_SECONDS, VaultWatch
    watch, sent = VaultWatch(tmp_path / 'vault-outage.json'), []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    watch.blocked('note-1', 'E_IO', 'the wiki repository could not be written', now=1000.0)
    watch.blocked('note-2', 'E_IO', 'the wiki repository could not be written', now=2000.0)
    watch.blocked('note-3', 'E_IO', 'the wiki repository could not be written', now=1000.0 + REMINDER_SECONDS)
    watch.blocked('note-4', 'E_IO', 'the wiki repository could not be written',
                  now=1000.0 + REMINDER_SECONDS + 60)
    assert [text.split(' met ')[0].rsplit(' ', 1)[-1] for text in sent] == ['note-1', 'note-3'], \
        'a day is counted from the last reminder, not from the start of the outage'
    assert sent[1].endswith('Day 2.'), sent
    watch.published(now=1000.0 + 2 * REMINDER_SECONDS)
    assert len(sent) == 3 and not (tmp_path / 'vault-outage.json').exists()
    with pytest.raises(TypeError):
        watch.saw(None)


def test_a_vault_alert_is_sent_although_its_record_cannot_be_written(tmp_path, monkeypatch):
    """The state directory cannot be written (a full disk is one vault failure that does this). The outage is
    told all the same, and with no record kept the next report tells it again instead of going silent."""
    from kelmo.alerts import VaultWatch
    watch, sent = VaultWatch(tmp_path / 'vault-outage.json'), []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)

    def full(self, outage):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(VaultWatch, '_save', full)
    watch.blocked('note-1', 'E_IO', 'the wiki repository could not be written', now=1000.0)
    assert len(sent) == 1 and 'note-1' in sent[0], 'the alert goes before its record'
    watch.blocked('note-2', 'E_IO', 'the wiki repository could not be written', now=1060.0)
    assert len(sent) == 2, 'no record was kept, so the outage is told again'


def test_a_pass_that_finds_the_vault_working_closes_an_outage_although_it_publishes_nothing(world, monkeypatch):
    """An outage was told and repaired. The next pass gets past the vault's checks and refuses its only note for
    the note's own reason: nothing is published, but the vault took a publication check, so the outage is over.
    A new outage the same day is told at once, not at the next daily reminder."""
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    texts = notes(2)
    first = capture(world, texts[:1])[0]
    opener = capture_work_page(world, texts[1], 'export-queue', 'Rework the export queue')
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_IO', 'the old outage')
    report = apply_pending(world.service, PageOwnerBatch(world, opener, bad_receipt=[first, opener]),
                           sleep=lambda _seconds: None)
    assert sorted(report['parked']) == sorted([first, opener]), report
    assert report['applied'] == [] and 'blocked' not in report, report
    assert len(sent) == 2 and 'takes publications again' in sent[1], sent
    with monkeypatch.context() as dirty:
        dirty.setattr(engine, 'require_clean_baseline', lambda *args, **kwargs: (_ for _ in ()).throw(
            KelmoError('E_CONFLICT', 'Knowledge paths have uncommitted local changes: feature.md')))
        pending_pass(world, counted(BatchModel(world)))
    assert len(sent) == 3 and 'no note can be published' in sent[2], 'the new outage is told at once'


def test_a_new_kind_of_failure_while_an_old_outage_is_on_record_is_told_at_once(world, monkeypatch):
    """An outage of a dirty tree was told and the tree was cleaned. The next pass gets past the vault's checks
    and stops at a commit that cannot be written: a failure of another kind is a new outage, told when it
    starts, not at the old one's daily reminder."""
    from kelmo.alerts import VaultWatch
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    editor_mode(world)
    capture(world, notes(2))
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_CONFLICT', 'uncommitted edits')
    monkeypatch.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
        KelmoError('E_IO', 'the wiki repository could not be written')))
    report = pending_pass(world, counted(BatchModel(world)))
    assert report['blocked']['error_code'] == 'E_IO' and report['applied'] == [], report
    assert len(sent) == 2 and 'E_IO' in sent[1] and 'no note can be published' in sent[1], sent
    pending_pass(world, counted(BatchModel(world)))
    assert len(sent) == 2, 'the same failure on the next pass waits for the daily reminder'


@pytest.mark.parametrize('how', ['executor-failed', 'own-budget-refusal'])
def test_a_single_note_pass_closes_an_outage_only_once_the_vault_is_seen_to_take_a_publication(world, monkeypatch,
                                                                                               how):
    """An outage is on record. On the single path a clean working tree alone does not say the wiki renders
    within its budget: a pass whose only note gets no proposal from its executor leaves the outage open. A
    pass whose note is refused for its own budget overflow has read the vault as it stands taking a
    publication, so it closes the outage."""
    from kelmo.alerts import VaultWatch
    editor_mode(world, batch_apply=False)
    texts = notes(1)
    capture(world, texts)
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_BUDGET', 'the wiki is over its budget')
    if how == 'own-budget-refusal':
        wiki_budget_refuses(monkeypatch, times=1)
        model = counted(BatchModel(world))
    else:
        model = counted(BatchModel(world, broken=[texts[0].strip()]))
    report = pending_pass(world, model)
    assert len(report['parked']) == 1 and report['applied'] == [] and 'blocked' not in report, report
    if how == 'executor-failed':
        assert len(sent) == 1, 'nothing showed the vault taking a publication: the outage stays open'
    else:
        assert len(sent) == 2 and 'takes publications again' in sent[1], sent


def test_a_pass_that_never_reaches_the_vault_leaves_an_outage_open(world, monkeypatch):
    """An outage is on record and the pass's only note is held by another attempt: no consultation got past the
    vault's checks, so nothing says the vault works, and the outage stays open with nothing told."""
    from kelmo.alerts import VaultWatch
    from kelmo.creation import apply_pending
    editor_mode(world)
    held_note = capture(world, notes(1))[0]
    sent = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    VaultWatch.for_service(world.service).blocked('an-older-note', 'E_IO', 'the old outage')
    held, clock = world.service.journal.claim(held_note, lease_seconds=600), [1000.0]
    try:
        report = apply_pending(world.service, counted(BatchModel(world)),
                               sleep=lambda seconds: clock.__setitem__(0, clock[0] + 600), clock=lambda: clock[0])
    finally:
        world.service.journal.release(held_note, held)
    assert report['applied'] == [] and 'blocked' not in report, report
    assert len(sent) == 1, 'the outage is neither closed nor told again'
    assert VaultWatch.for_service(world.service).marker.exists()


def test_failures_of_two_kinds_that_alternate_are_each_told_once_a_day(tmp_path, monkeypatch):
    """A lasting outage meets two kinds of failure in turn, pass after pass. Each kind is told the first time it
    is met and then once a day, like the outage itself; alternating does not tell the owner on every pass."""
    from kelmo.alerts import REMINDER_SECONDS, VaultWatch
    watch, sent = VaultWatch(tmp_path / 'vault-outage.json'), []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    for minute, code in enumerate(['E_CONFLICT', 'E_IO', 'E_CONFLICT', 'E_IO', 'E_CONFLICT']):
        watch.blocked('note-1', code, 'a failure of this kind', now=1000.0 + 60 * minute)
    assert [text.split(' met ')[1].split(':')[0] for text in sent] == ['E_CONFLICT', 'E_IO'], sent
    watch.blocked('note-1', 'E_IO', 'a failure of this kind', now=1000.0 + 60 + REMINDER_SECONDS)
    assert len(sent) == 3 and sent[2].endswith('Day 2.'), 'a day later the kind is told again, with the day'


def test_a_corrupt_outage_record_starts_a_new_one_instead_of_silencing_every_alert(tmp_path, monkeypatch):
    import json
    from kelmo.alerts import VaultWatch
    marker = tmp_path / 'vault-outage.json'
    marker.write_text('{"since": ', encoding='utf-8')
    watch, sent = VaultWatch(marker), []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: sent.append(text) or True)
    watch.blocked('note-1', 'E_IO', 'the wiki repository could not be written', now=5000.0)
    assert len(sent) == 1 and 'note-1' in sent[0], sent
    assert json.loads(marker.read_text(encoding='utf-8')) == {'since': 5000.0, 'told': {'E_IO': 5000.0}}
    marker.write_text('[]', encoding='utf-8')
    watch.published(now=6000.0)
    assert len(sent) == 2 and 'published again' in sent[1] and not marker.exists(), sent
    marker.write_text('{"since": 7000.0, "told": {"E_IO": "yesterday"}}', encoding='utf-8')
    watch.blocked('note-2', 'E_IO', 'the wiki repository could not be written', now=7100.0)
    assert len(sent) == 3 and 'note-2' in sent[2], 'an unreadable delivery time never silences the alert'
    assert json.loads(marker.read_text(encoding='utf-8')) == {'since': 7000.0, 'told': {'E_IO': 7100.0}}


def test_an_undelivered_vault_alert_is_sent_again_by_the_next_pass_that_meets_the_outage(tmp_path, monkeypatch):
    """The alert command failed: the outage stays on record with no alert delivered, so the very next report
    of it tries again instead of waiting a day, and a command that cannot even start fails no pass."""
    import json
    from kelmo.alerts import VaultWatch
    marker = tmp_path / 'vault-outage.json'
    watch = VaultWatch(marker, argv=[str(tmp_path / 'no-such-alert-command')])
    watch.blocked('note-1', 'E_IO', 'the wiki repository could not be written', now=1000.0)
    assert json.loads(marker.read_text(encoding='utf-8')) == {'since': 1000.0, 'told': {}}
    delivered = []
    monkeypatch.setattr(VaultWatch, 'send', lambda self, text: delivered.append(text) or True)
    watch.blocked('note-2', 'E_IO', 'the wiki repository could not be written', now=1060.0)
    assert len(delivered) == 1 and 'note-2' in delivered[0], delivered
    assert json.loads(marker.read_text(encoding='utf-8')) == {'since': 1000.0, 'told': {'E_IO': 1060.0}}
