"""What a later call does with a batch a pass died on.

A batch answer is settled by the call that recorded it. When that call dies, a later one does not finish
its work. It asks one question of every row the dead pass left answered: did the batch commit this note?
A note the commit carries is finished from the stored intent, with no model call. Any other note has its
attempt ended, spends none of its attempts and is asked again. That rule is `Editor._leave_batch`.

These tests write by hand the rows a dead pass leaves and call the rule itself: no model, no commit.
What a real pass leaves when it dies, and what the next pass does with it, is in
`test_dreaming_batched_apply.py`.
"""
import json
import time
import uuid

import pytest

from test_dreaming_batched_apply import capture, charged_parkings, editor_mode, notes
from test_editor_scoping import build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


class DeadBatch:
    """The request rows of one batch as a pass that died left them: the lead's row holds the answer, every
    other row says which batch it belongs to. `commit`, `refused_commit` and `prepare` put the batch's
    intent on one of those rows."""

    def __init__(self, world, ids):
        from kelmo.editor import StateCoordinator
        self.world, self.ids, self.journal = world, ids, world.service.journal
        self.idem = {operation_id: 'create:' + operation_id + ':1' for operation_id in ids}
        self.keys = {operation_id: StateCoordinator._request_key('semantic', world.origins['editor'],
                                                                self.idem[operation_id]) for operation_id in ids}
        self.answer(ids)

    def answer(self, batch):
        """Record one answer over `batch`, oldest first; a row that is there already is consulted again."""
        mark = {'batch': list(batch), 'generation': self.world.service.knowledge.generation().to_dict()}
        for operation_id in batch:
            observation = dict(mark, result={}) if operation_id == batch[0] else mark
            row = self.row(operation_id)
            if row is None:
                self.journal.action_begin(self.keys[operation_id], 'semantic', 'fingerprint',
                                          self.world.origins['editor'], operation_id, observation=observation)
            else:
                token = uuid.uuid4().hex
                assert self.journal.action_lease(row['operation_id'], token, time.time() + 900)
                assert self.journal.action_observe(row['operation_id'], token, observation)

    def row(self, operation_id):
        return self.journal.action_request(self.keys[operation_id])

    def prepare(self, carrier, carried, recorded=None, requests=None):
        """Store the batch's intent on `carrier`'s row. It carries `carried`; a note named in `recorded`
        has no item of its own in it, only the fact keys the answer said already state it. Each source entry
        names the request row it was prepared for: the note's row here, or the one `requests` gives."""
        recorded, requests = recorded or {}, requests or {}
        delta = {'facts': [{'fact_key': 'app.note-' + source[:8], 'source_operation_id': source}
                           for source in carried if source not in recorded], 'narratives': [], 'pages': []}
        self.journal.action_prepare(self.row(carrier)['operation_id'], {
            'batch': list(carried), 'delta': delta,
            'sources': {source: {'applied_reason': None, 'recorded': recorded.get(source, []),
                                 'request': requests.get(source, self.row(source)['operation_id'])}
                        for source in carried}})

    def commit(self, carrier, carried, recorded=None, requests=None):
        self.prepare(carrier, carried, recorded, requests)
        self.journal.action_finish(self.row(carrier)['operation_id'], {'published': list(carried)})

    def refused_commit(self, carrier, carried):
        self.prepare(carrier, carried)
        self.journal.action_refuse(self.row(carrier)['operation_id'], 'E_FACT_CHECK',
                                   'a receipt no longer matches the registry')

    def published_alone(self, operation_id):
        """The note left its batch, was consulted on the single-source path and published there."""
        self.journal.action_prepare(self.row(operation_id)['operation_id'],
                                    {'delta': {'facts': [{'fact_key': 'app.alone'}], 'narratives': [], 'pages': []}})
        self.journal.action_finish(self.row(operation_id)['operation_id'], {'published': [operation_id]})

    def leave(self, operation_id):
        return self.world.service.editor._leave_batch(self.keys[operation_id], self.idem)

    def asked_again(self, operation_id):
        """The row is back in the form a released lease leaves, and its note was charged nothing."""
        row = self.row(operation_id)
        observation = json.loads(row['observation'])
        return (row['state'] == 'open' and set(observation) == {'consulting', 'expires_at'}
                and observation['expires_at'] == 0 and charged_parkings(self.world, operation_id) == ([], 0))


@pytest.fixture
def archived(world):
    editor_mode(world)
    return capture(world, notes(3))


def test_a_note_the_commit_carries_is_finished_from_it_with_its_own_facts(world, archived):
    batch = DeadBatch(world, archived)
    batch.commit(archived[0], archived)
    assert batch.leave(archived[1])['disposition'] == 'applied'
    assert batch.row(archived[1])['state'] == 'finished'
    outcome = world.outcome(archived[1])
    assert (outcome['disposition'], outcome['reason']) == ('applied', 'fact applied: app.note-' + archived[1][:8])
    assert batch.row(archived[2])['state'] == 'open', 'only the row that was asked about is settled'


def test_a_note_the_commit_carries_with_no_item_of_its_own_ends_recorded(world, archived):
    batch = DeadBatch(world, archived)
    batch.commit(archived[0], archived, recorded={archived[2]: ['app.known']})
    assert batch.leave(archived[2])['disposition'] == 'archive-only'
    outcome = world.outcome(archived[2])
    assert outcome['disposition'] == 'archive-only'
    assert 'app.known' in outcome['reason'], 'it names the facts the answer said already state it'


def test_the_lead_settles_from_a_commit_another_row_carried(world, archived):
    """The lead proposed nothing of its own, so the batch committed on the second note's row."""
    batch = DeadBatch(world, archived)
    batch.commit(archived[1], archived, recorded={archived[0]: ['app.known']})
    assert batch.leave(archived[0])['disposition'] == 'archive-only'
    assert batch.leave(archived[2])['disposition'] == 'applied'


def test_a_note_the_commit_does_not_carry_is_asked_again(world, archived):
    batch = DeadBatch(world, archived)
    batch.commit(archived[0], archived[:2])
    assert batch.leave(archived[2]) is None
    assert batch.asked_again(archived[2])
    assert world.service.journal.get(archived[2])['state'] == 'archived', 'it still waits for its turn'


def test_a_commit_prepared_for_an_earlier_request_of_the_note_does_not_settle_this_one(world, archived):
    """The commit carries the note, but under a request of its that came before this row (the note was
    queued again since). It settled that request, not this one: this row's answer was never published, so the
    note is asked again, never finished from facts another answer gave."""
    batch = DeadBatch(world, archived)
    batch.commit(archived[0], archived, requests={archived[1]: 'an-earlier-request-of-the-note'})
    editor = world.service.editor
    assert (editor.published_by_batch(archived[1]), editor.published_by_batch(archived[2])) == (False, True), \
        'the park guard reads the same rule'
    assert batch.leave(archived[1]) is None
    assert batch.asked_again(archived[1])
    assert batch.leave(archived[2])['disposition'] == 'applied', 'the rows it was prepared for settle from it'


def test_a_batch_that_committed_nothing_leaves_every_note_to_be_asked_again(world, archived):
    batch = DeadBatch(world, archived)
    assert [batch.leave(operation_id) for operation_id in archived] == [None, None, None]
    assert all(batch.asked_again(operation_id) for operation_id in archived)


def test_a_refused_commit_settles_no_other_note_and_charges_none(world, archived):
    batch = DeadBatch(world, archived)
    batch.refused_commit(archived[1], archived)
    assert [batch.leave(operation_id) for operation_id in (archived[0], archived[2])] == [None, None]
    assert batch.asked_again(archived[0]) and batch.asked_again(archived[2])
    assert batch.row(archived[1])['state'] == 'refused', 'the refusal stays on the row that carried the commit'


def test_a_commit_still_to_be_recovered_is_busy_and_moves_no_row(world, archived):
    from kelmo.errors import KelmoError
    batch = DeadBatch(world, archived)
    batch.prepare(archived[0], archived)
    before = batch.row(archived[1])
    with pytest.raises(KelmoError) as refusal:
        batch.leave(archived[1])
    assert refusal.value.code == 'E_BUSY'
    assert batch.row(archived[1]) == before


def test_a_note_settles_from_the_commit_that_carries_it_whatever_its_lead_published_since(world, archived):
    """The batch committed on the second note's row and carries the third. The lead was left out, asked
    again and published in a later batch of its own; that later commit is not the third note's."""
    batch = DeadBatch(world, archived)
    batch.commit(archived[1], archived[1:])
    assert batch.leave(archived[0]) is None
    batch.answer(archived[:1])
    batch.commit(archived[0], archived[:1])
    assert batch.leave(archived[2])['disposition'] == 'applied'
    assert world.outcome(archived[2])['reason'] == 'fact applied: app.note-' + archived[2][:8]


@pytest.mark.parametrize('how', ['in a later batch', 'on the single-source path'])
def test_what_another_note_published_since_is_not_this_notes_commit(world, archived, how):
    """Nothing of the batch was committed. Its lead was asked again and published without the third note."""
    batch = DeadBatch(world, archived)
    assert batch.leave(archived[0]) is None
    if how == 'in a later batch':
        batch.answer(archived[:1])
        batch.commit(archived[0], archived[:1])
    else:
        batch.published_alone(archived[0])
    assert batch.leave(archived[2]) is None
    assert batch.asked_again(archived[2])


def test_a_row_that_holds_no_batch_answer_is_left_as_it_is(world, archived):
    batch = DeadBatch(world, archived)
    batch.commit(archived[0], archived)
    assert batch.leave(archived[0]) is None, 'a finished row'
    assert batch.leave(archived[1])['disposition'] == 'applied'
    assert batch.leave(archived[1]) is None, 'the row it has just finished'
    assert batch.leave(archived[2])['disposition'] == 'applied'
    ended = DeadBatch(world, capture(world, notes(1, start=3)))
    assert ended.leave(ended.ids[0]) is None and ended.leave(ended.ids[0]) is None, 'an ended attempt'
    assert ended.asked_again(ended.ids[0])


def test_a_changed_request_ends_the_batch_before_it_and_a_settled_note_keeps_its_settlement(world, archived):
    """The middle note's request changed under its open row; the newest note has settled already. The batch ends
    at the middle note, and the newest is neither dropped nor reported waiting: it published."""
    batch = DeadBatch(world, archived)
    fingerprints = dict.fromkeys(archived, 'fingerprint')
    fingerprints[archived[1]] = 'another request'
    settled = {archived[2]: {'disposition': 'settled'}}
    kept, behind = world.service.editor._up_to_changed_request(list(archived), batch.keys, fingerprints, settled)
    assert kept == [archived[0], archived[2]]
    assert list(behind) == [archived[1]] and behind[archived[1]]['code'] == 'E_IDEMPOTENCY'


def test_a_note_still_waiting_after_a_changed_request_is_cut_off_with_it(world, archived):
    """The middle note's request changed under its open row and the newest note has settled nothing: the batch
    keeps only the oldest note, and the newest waits behind the middle one instead of publishing ahead of it."""
    batch = DeadBatch(world, archived)
    fingerprints = dict.fromkeys(archived, 'fingerprint')
    fingerprints[archived[1]] = 'another request'
    kept, behind = world.service.editor._up_to_changed_request(list(archived), batch.keys, fingerprints, {})
    assert kept == [archived[0]]
    assert sorted(behind) == sorted(archived[1:])
    assert behind[archived[2]]['disposition'] == 'dropped'


def test_a_note_that_settled_under_a_request_that_changed_since_cuts_nothing(world, archived):
    """The oldest note published, and its budget was raised afterwards. Its row replays what it has become; it is
    no reason to end the batch, and the two notes behind it stay in it."""
    batch = DeadBatch(world, archived)
    fingerprints = dict.fromkeys(archived, 'fingerprint')
    fingerprints[archived[0]] = 'another request'
    kept, behind = world.service.editor._up_to_changed_request(list(archived), batch.keys, fingerprints,
                                                                {archived[0]: {'disposition': 'settled'}})
    assert (kept, behind) == (list(archived), {})


def test_the_first_note_not_settled_is_the_head_a_changed_request_belongs_to(world, archived):
    from kelmo.errors import HeadRefusal
    batch = DeadBatch(world, archived)
    fingerprints = dict.fromkeys(archived, 'fingerprint')
    fingerprints[archived[1]] = 'another request'
    with pytest.raises(HeadRefusal) as raised:
        world.service.editor._up_to_changed_request(list(archived), batch.keys, fingerprints,
                                                    {archived[0]: {'disposition': 'settled'}})
    assert (raised.value.code, raised.value.lead) == ('E_IDEMPOTENCY', archived[1])
