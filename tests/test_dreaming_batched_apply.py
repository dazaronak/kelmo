"""Batched Dreaming apply: the apply pass that groups waiting sources and the editor that settles them.

These cases run the real pass — `apply_pending` over archived nightly notes with `[editor] batch_apply`
on — over the synthetic world of test_editor_scoping, and measure what the feature is about: how many
model conversations and how many commits a pass pays, and what each member source ends with. The shape of
the batch answer itself (attribution, the split, the collision rule) is `tests/test_dreaming_batched.py`;
the single-source pass is `tests/test_dreaming_cost.py`.
"""
import pytest

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
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[editor]\n' + '\n'.join(lines) + '\n',
                    encoding='utf-8')
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


def counted(inner):
    from test_dreaming_cost import Counted
    return Counted(inner)


def commits(world):
    from kelmo.wiki import engine
    return int(engine.git(world.root, 'rev-list', '--count', 'HEAD'))


def waiting_ids(world):
    from kelmo.creation import pending_operations
    return [operation['id'] for operation, _request in pending_operations(world.service)]


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


def test_publish_batch_leaves_a_source_another_attempt_holds_out_of_the_batch(world):
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
    assert [len(job.payload['sources']) for job in model.inner.jobs] == [2]
    assert [operation_id for block in model.inner.jobs[0].payload['sources']
            for operation_id in [block['operation_id']]] == [ids[0], ids[2]]
    assert outcome[ids[1]] == {'state': 'waiting', 'code': 'E_CLAIM'}
    assert [outcome[operation_id]['state'] for operation_id in (ids[0], ids[2])] == ['applied', 'applied']
    row = world.service.journal.get(ids[1])
    assert (row['state'], row['terminal']) == ('archived', False), 'a held source is not touched by the batch'
    assert waiting_ids(world) == [ids[1]]


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
