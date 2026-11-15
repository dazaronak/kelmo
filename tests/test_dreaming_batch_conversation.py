"""The batch conversation: what a batch keeps of an answer, what it tells the model, and what it records.

The first batch that ran in production carried 8 notes, made 5 model calls and published nothing, with no
word of why. Its conversation ran in one note's budget, lost a repair to a reconcile round over its ceiling,
and ended on an answer that carried only the items of the notes the proposal before it had left out: that
answer replaced the proposal, could not be read, and every note went alone. These cases run the real pass
over the synthetic world of `test_dreaming_batched_apply` and pin each link of that chain.
"""
import json

import pytest

from test_dreaming_batched_apply import (
    FEATURE,
    BatchModel,
    capture,
    charged_parkings,
    commits,
    counted,
    editor_mode,
    editor_table,
    facts_of,
    long_notes,
    notes,
    pending_pass,
)
from test_editor_scoping import build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


def batch_jobs(model):
    return [job for job in model.jobs if 'sources' in job.payload]


def events_of(world, operation_id, stage):
    return [event.get('details') or event for event in world.service.trace(operation_id)['events']
            if event['stage'] == stage]


class AnswersTheRestLater(BatchModel):
    """The batch replay that leaves `later` without an item, and once refused sends only their items.

    The first proposal answers every other note. Each proposal after a refusal carries nothing but the
    items of `later`: the answer the first production batch ended on. `spoil` makes those items cite a
    receipt no search returned, on every round; `give_up` answers `unresolved` instead of sending them.
    """

    def __init__(self, world, later, spoil=False, give_up=False):
        super().__init__(world)
        self.later, self.spoil, self.give_up = set(later), spoil, give_up

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        answer = super().execute(job)
        payload = job.payload
        if 'sources' not in payload or answer.kind != 'proposal':
            return answer
        refused = bool(payload.get('refusal'))
        if refused and self.give_up:
            return ModelOutcome('failure', error_code='E_UNRESOLVED', reason='nothing more these pages should record')
        answer.result['facts'] = [fact for fact in answer.result['facts']
                                  if (fact['source'] in self.later) == refused]
        answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                        if (check['source'] in self.later) == refused]
        if refused and self.spoil:
            for check in answer.result['fact_checks']:
                check['verified_match'] = {'result': 'not_found', 'search_id': '0' * 32}
        return answer


@pytest.mark.parametrize('left_out', [0, 2])
def test_an_answer_that_carries_only_the_missing_notes_is_added_to_the_proposal(world, left_out):
    """The proposal answered two notes of three and the repair sent only the third: one batch, one commit."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = AnswersTheRestLater(world, {ids[left_out]})
    before = commits(world)
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) for job in model.jobs if 'sources' in job.payload] == [3, 3], \
        'the proposal and the round that added the note it left out'
    assert len(model.jobs) == 2, 'no note was consulted alone'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    assert commits(world) == before + 1, 'the proposal and what was added to it publish as one commit'
    assert len(facts_of(world)) == 3, 'the items of the first proposal were kept, the missing one added'
    refusal = model.jobs[1].payload['refusal']
    assert ids[left_out] in refusal['reason'], refusal
    assert 'Answer only for the sources named here' in refusal['reason'], refusal
    assert 'do not send it again' in refusal['reason'], 'the refusal says what to send next'
    assert 'unsettled' not in report, 'a batch that settled every note leaves none to explain'


@pytest.mark.parametrize('left_out', [0, 2])
def test_an_addition_refused_for_its_own_check_leaves_the_standing_proposal_to_settle(world, left_out):
    """The items added for the missing note fail their fact check on both repairs. The batch settles the
    proposal that stood: the note it left out is consulted alone and charged nothing, and no note captured
    after it is published ahead of it."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = AnswersTheRestLater(world, {ids[left_out]}, spoil=True)
    report = pending_pass(world, model)
    first = batch_jobs(model)[:3]
    assert [len(job.payload['sources']) for job in first] == [3, 3, 3], 'the proposal and its two repairs'
    assert 'Send again only the items of your last answer' in first[2].payload['refusal']['reason'], \
        'the second repair says the proposal before it is kept'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0), 'a repair round that went wrong charges no note'
    assert world.service.journal.get(ids[left_out])['error_code'] is None
    alone = [job for job in model.jobs if 'sources' not in job.payload]
    assert len(alone) == 1, 'only the note the standing proposal left out was consulted alone'
    order = [job.payload['source']['text'].strip() if 'sources' not in job.payload else 'batch'
             for job in model.jobs]
    if left_out == 0:
        assert order.index(notes(3)[0]) < len(order) - 1 and order[-1] == 'batch', \
            'the notes after the one left out waited for it and then went as a batch'
    assert len(facts_of(world)) == 3


def test_unresolved_once_a_proposal_stands_settles_that_proposal(world):
    """The model answered two notes, then `unresolved` for the third: the two publish, the third runs alone."""
    editor_mode(world)
    ids = capture(world, notes(3))
    model = AnswersTheRestLater(world, {ids[2]}, give_up=True)
    before = commits(world)
    report = pending_pass(world, model)
    assert len(batch_jobs(model)) == 2, 'the proposal, then the answer that gave up on the rest'
    assert [job.payload['source']['text'].strip() for job in model.jobs if 'sources' not in job.payload] == \
        [notes(3)[2]], 'only the note the proposal left out was consulted alone'
    assert sorted(report['applied']) == sorted(ids), report
    assert commits(world) == before + 2, 'the standing proposal in one commit, the note sent alone in its own'
    reason = events_of(world, ids[2], 'editor_batch')[-1]['reason']
    assert 'no item and no recorded entry' in reason, reason


# ---------------------------------------------------------------------------
# Refusals that refuse no proposal spend no repair
# ---------------------------------------------------------------------------

CEILING_ASK = 'The audit closed every open item of that week.'


class AsksTooMuch(BatchModel):
    """The batch replay behind reconcile rounds that would pass the receipt ceiling.

    The first `rounds` batch calls each ask for one search per source of the same text. Then `silent` gets
    no item for `withhold` proposals, and the proposal after those answers every note.
    """

    def __init__(self, world, rounds=1, silent=None, withhold=0):
        super().__init__(world)
        self.rounds, self.silent, self.withhold = rounds, silent, withhold

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        payload = job.payload
        if 'sources' not in payload:
            return super().execute(job)
        if self.rounds > 0:
            self.rounds -= 1
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': CEILING_ASK, 'target': FEATURE}
                                                        for _ in payload['sources']]})
        answer = super().execute(job)
        if self.withhold > 0:
            self.withhold -= 1
            answer.result['facts'] = [fact for fact in answer.result['facts'] if fact['source'] != self.silent]
            answer.result['fact_checks'] = [check for check in answer.result['fact_checks']
                                            if check['source'] != self.silent]
        return answer


def test_a_note_left_out_after_a_round_over_the_ceiling_is_not_asked_for_and_runs_alone(world):
    """The round over the ceiling tells the model to leave out, whole, the notes whose facts do not fit.
    The proposal that then leaves one out is taken as it is: asking for that note would ask for what the
    model was just told it cannot have. The round spent no repair, the batch publishes what it answered,
    and the note left out is consulted alone in the same pass, uncharged."""
    editor_mode(world, budget=2000)
    ids = capture(world, notes(3))
    model = AsksTooMuch(world, rounds=1, silent=ids[2], withhold=2)
    before = commits(world)
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) for job in batch_jobs(model)] == [3, 3], \
        'the refused reconcile round, then the proposal that was taken'
    assert [job.payload['source']['text'].strip() for job in model.jobs if 'sources' not in job.payload] == \
        [notes(3)[2]], 'only the note left out was consulted alone'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    assert commits(world) == before + 2, 'the batch in one commit, the note sent alone in its own'
    assert charged_parkings(world, ids[2]) == ([], 0)
    told = model.jobs[1].payload['refusal']
    assert told['code'] == 'E_PROPOSAL' and 'No search was run' in told['reason'], told
    assert 'leave every other source out of your answer whole' in told['reason']


def test_a_third_refusal_of_that_kind_ends_the_conversation_and_every_note_says_why(world, capsys):
    """The model keeps asking for more receipts than the ceiling allows. The third refusal ends the batch:
    each note runs alone, uncharged, and the reason is on every note, on the lead and in the pass report."""
    editor_mode(world, budget=2000)
    ids = capture(world, notes(3))
    model = AsksTooMuch(world, rounds=99)
    report = pending_pass(world, model)
    assert len(batch_jobs(model)) == 3, 'two refusals the model could answer, and the one that ended it'
    assert sorted(report['applied']) == sorted(ids), report
    for operation_id in ids:
        assert charged_parkings(world, operation_id) == ([], 0)
        noted = events_of(world, operation_id, 'editor_batch')
        assert len(noted) == 1 and noted[0]['lead'] == ids[0] and noted[0]['size'] == 3, noted
        assert noted[0]['disposition'] == 'alone'
        assert 'ended with nothing proposed' in noted[0]['reason'], noted
        assert 'may cost at most' in noted[0]['reason'], 'the note carries what ended its batch'
        assert '\n' not in noted[0]['reason']
    answer = events_of(world, ids[0], 'editor_batch_answer')
    assert len(answer) == 1 and answer[0]['batch'] == ids, answer
    assert [entry['answer'] for entry in answer[0]['rounds']] == ['reconcile'] * 3, answer[0]['rounds']
    assert all('refusal' in entry for entry in answer[0]['rounds'])
    assert 'ended' in answer[0]['rounds'][-1] and 'exhausted' in answer[0]
    assert events_of(world, ids[1], 'editor_batch_answer') == [], 'the trail is on the batch\'s first note'
    assert sorted(item['operation_id'] for item in report['unsettled']) == sorted(ids), report
    assert {item['lead'] for item in report['unsettled']} == {ids[0]}
    assert all('ended with nothing proposed' in item['reason'] for item in report['unsettled'])
    line = [text for text in capsys.readouterr().err.splitlines() if 'the batch of 3 notes' in text]
    assert len(line) == 1 and 'left 3 of them waiting (0 settled' in line[0], line


def test_a_notice_is_not_repeated_once_the_next_answer_is_taken(world):
    """The refusal of a reconcile round is in the next payload and gone from the one after an answer that
    was taken: a notice the model already answered must not read as a new refusal."""
    editor_mode(world, budget=2000)
    ids = capture(world, notes(3))

    class ThenAsksLittle(AsksTooMuch):
        def execute(self, job):
            from kelmo.executor import ModelOutcome
            if 'sources' in job.payload and self.rounds == 0 and not job.payload['reconciliations']:
                self.jobs.append(job)
                return ModelOutcome('reconcile', {'facts': [{'text': notes(3)[0].split('. ')[0],
                                                             'target': FEATURE}]})
            return super().execute(job)

    model = ThenAsksLittle(world, rounds=1)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    payloads = [job.payload for job in batch_jobs(model)]
    assert ['refusal' in payload for payload in payloads] == [False, True, False], \
        'the refused round is told once; the round after a search that ran carries no refusal'


# ---------------------------------------------------------------------------
# The budget of a batch and how a batch is sized
# ---------------------------------------------------------------------------


def test_batch_budget_tokens_is_a_positive_integer_and_absent_by_default(world):
    from kelmo.config import Config, validate_editor
    from kelmo.errors import KelmoError
    assert validate_editor({'batch_apply': True}).get('batch_budget_tokens') is None
    assert validate_editor({'batch_budget_tokens': 500000})['batch_budget_tokens'] == 500000
    for bad in (0, -1, True, '500000', 1.5):
        with pytest.raises(KelmoError) as refused:
            validate_editor({'batch_budget_tokens': bad})
        assert refused.value.code == 'E_CONFIG' and 'batch_budget_tokens' in str(refused.value)
    editor_table(world)
    assert Config.load(world.root).batch_budget is None
    editor_table(world, batch_budget=9000)
    assert Config.load(world.root).batch_budget == 9000


def test_a_batch_runs_in_the_batch_budget_and_a_note_alone_keeps_its_own(world):
    """Three padded notes do not fit one note's budget together. With `batch_budget_tokens` they are one
    conversation in that budget and one commit; without it the batch is sized to what one note's budget
    holds."""
    from test_dreaming_batched_apply import SHRINK_BUDGET
    editor_mode(world, budget=SHRINK_BUDGET, batch_budget=6000)
    ids = capture(world, long_notes(3))
    before = commits(world)
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    batched = batch_jobs(model.inner)
    assert [len(job.payload['sources']) for job in batched] == [3], 'the batch budget carries all three'
    assert batched[0].payload['budget']['max_tokens'] == 6000
    assert batched[0].payload['budget']['receipt_ceiling'] == 6000 // 2, 'the receipts may take half of it'
    assert sorted(report['applied']) == sorted(ids) and commits(world) == before + 1, report


def test_the_batch_budget_is_never_less_than_the_largest_budget_of_its_notes(world):
    editor_mode(world, budget=6000, batch_budget=1000)
    ids = capture(world, notes(3))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    assert [job.payload['budget']['max_tokens'] for job in batch_jobs(model.inner)] == [6000]
    assert sorted(report['applied']) == sorted(ids), report


def test_a_batch_is_sized_to_leave_room_for_its_receipts(world):
    """The pages of three notes fit the budget and leave less room than their receipts are given: the batch
    takes the notes that leave that room, and the pass publishes the rest in the same pass. The reason the
    newest note was left out names the room and what was reserved."""
    from kelmo import editor
    # Measured on this fixture with FEATURE selected: the fixed part costs 687, the FEATURE page 272 and
    # each padded note 464, so three notes use 2079 and two 1886 (see SHRINK_BUDGET). At 2600 the reserve
    # is 650 (a quarter of the budget): three notes leave 521, two leave 714.
    budget = 2600
    assert editor._receipt_reserve(budget, [13] * 3) == editor._receipt_reserve(budget, [13] * 2) == 650
    editor_mode(world, budget=budget)
    ids = capture(world, long_notes(3))
    model = counted(BatchModel(world))
    report = pending_pass(world, model)
    batched = batch_jobs(model.inner)
    assert [len(job.payload['sources']) for job in batched] == [2], 'the newest note waits for the next turn'
    assert budget - batched[0].payload['budget']['used_tokens'] >= 650
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], []), report
    assert charged_parkings(world, ids[2]) == ([], 0), 'a note the batch was sized without spends no attempt'
    left = [item for item in report['unsettled'] if item['operation_id'] == ids[2]]
    assert len(left) == 1 and 'for the receipts of 3 sources, which are given 650' in left[0]['reason'], left


def test_the_receipt_reserve_and_the_ceiling_follow_the_room_the_pages_leave():
    from kelmo import editor
    assert editor._receipt_reserve(500000, [1] * 8) == 80000, '10 000 for each short note'
    assert editor._receipt_reserve(500000, [9] * 4) == 72000, '2 000 for each unit of a longer one'
    assert editor._receipt_reserve(200000, [1] * 8) == 50000, 'never more than a quarter of the budget'
    assert editor._receipt_reserve(24000, [1] * 3) == 6000
    # The first view of the discarded production batch: 198515 of 200000 used before any receipt.
    assert editor._receipt_ceiling(200000, 198515) == 1485
    assert editor._receipt_ceiling(500000, 198515) == 250000, 'half of the budget when the pages leave it'


def test_an_expansion_the_budget_leaves_out_is_refused_with_what_the_page_needed():
    from kelmo import editor
    text = editor._starved_expansion(
        ['projects/app/big.md', 'projects/app/other.md'],
        [{'target': 'projects/app/big.md', 'needs_tokens': 52410, 'left_tokens': 12689}], 200000)
    assert 'the budget of 200000 estimated tokens leaves out projects/app/big.md ' \
           '(needs 52410 estimated tokens, 12689 left), projects/app/other.md.' in text, text
    assert 'spent none of your expansions' in text and 'consulted alone afterwards' in text


# ---------------------------------------------------------------------------
# What a later answer may add to the proposal that stands
# ---------------------------------------------------------------------------


def _fact(key, source, text='stated'):
    return {'fact_key': key, 'source': source, 'target': FEATURE, 'text': text}


def test_only_the_items_of_the_sources_left_out_are_taken_from_a_later_answer():
    from kelmo.editor import _merged_answer
    standing = {'facts': [_fact('app.a', 'one'), _fact('app.b', 'two')],
                'fact_checks': [{'fact_key': 'app.a', 'source': 'one'}, {'fact_key': 'app.b', 'source': 'two'}]}
    later = {'facts': [_fact('app.a', 'one', 'changed'), _fact('app.c', 'three')],
             'fact_checks': [{'fact_key': 'app.a', 'source': 'three'}, {'fact_key': 'app.c', 'source': 'three'}],
             'narratives': [{'narrative_id': 'app.n', 'source': 'three', 'target': FEATURE, 'text': 'opened'}]}
    merged = _merged_answer(standing, later, {'one', 'two'})
    assert merged['facts'] == [*standing['facts'], _fact('app.c', 'three')], \
        'the answered source keeps its item as it stood; the changed copy is not taken'
    assert {'fact_key': 'app.a', 'source': 'one'} in merged['fact_checks'], 'its check is not replaced'
    assert {'fact_key': 'app.a', 'source': 'three'} in merged['fact_checks'], \
        'another source naming that key keeps its own check, for the split to decide'
    assert merged['narratives'] == later['narratives']


def test_a_later_answer_replaces_its_own_earlier_item_and_removes_nothing_by_omission():
    from kelmo.editor import _merged_answer
    standing = {'facts': [_fact('app.a', 'one'), _fact('app.c', 'three', 'first wording')],
                'recorded': [{'source': 'three', 'fact_keys': ['app.unknown']}]}
    merged = _merged_answer(standing, {'facts': [_fact('app.c', 'three', 'second wording')],
                                       'recorded': [{'source': 'three', 'fact_keys': ['app.known']}]}, {'one'})
    assert merged['facts'] == [_fact('app.a', 'one'), _fact('app.c', 'three', 'second wording')]
    assert merged['recorded'] == [{'source': 'three', 'fact_keys': ['app.known']}]
    assert _merged_answer(standing, {}, {'one'})['facts'] == standing['facts'], 'an empty answer removes nothing'
    assert _merged_answer(standing, {'facts': 'not a list'}, {'one'})['facts'] == 'not a list', \
        'a malformed list is left for the proposal check to refuse'


# ---------------------------------------------------------------------------
# The pass report names every note a batch left waiting
# ---------------------------------------------------------------------------


def test_the_report_counts_every_waiting_note_and_keeps_each_reason_on_one_line(capsys):
    from kelmo.creation import _report_unsettled
    batch = [({'id': 'lead'}, {}, None), ({'id': 'second'}, {}, None), ({'id': 'third'}, {}, None),
             ({'id': 'fourth'}, {}, None)]
    outcome = {'lead': {'state': 'applied'},
               'second': {'state': 'waiting', 'code': 'E_PROPOSAL', 'alone': True, 'reason': 'left\n  out'},
               'third': {'state': 'waiting', 'code': 'E_CLAIM'},
               'fourth': {'state': 'deferred', 'code': 'E_QUOTA'}}
    report = {}
    _report_unsettled(report, batch, outcome)
    assert report['unsettled'] == [
        {'operation_id': 'second', 'lead': 'lead', 'error_code': 'E_PROPOSAL', 'reason': 'left out'},
        {'operation_id': 'third', 'lead': 'lead', 'error_code': 'E_CLAIM',
         'reason': 'no reason was recorded for it'}]
    line = capsys.readouterr().err.strip()
    assert line.count('\n') == 0 and 'left 2 of them waiting (2 settled, deferred or parked)' in line, line
    settled = {}
    _report_unsettled(settled, batch[:1], {'lead': {'state': 'applied'}})
    assert settled == {} and capsys.readouterr().err == '', 'a batch that left no note says nothing'


def test_a_trace_line_the_journal_cannot_take_stops_no_settle(world, monkeypatch):
    """The record is for whoever reads the trace: a busy database loses that line and the batch goes on."""
    import sqlite3
    editor_mode(world)
    ids = capture(world, notes(3))
    journal = world.service.journal
    real = journal.action_settle_source

    def busy(source_operation_id, outcome, *, result=None, event=None):
        if event is not None and event[0] in ('editor_batch', 'editor_batch_answer'):
            raise sqlite3.OperationalError('database is locked')
        return real(source_operation_id, outcome, result=result, event=event)

    monkeypatch.setattr(journal, 'action_settle_source', busy)
    model = AnswersTheRestLater(world, {ids[2]}, give_up=True)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids), report
    assert events_of(world, ids[2], 'editor_batch') == [], 'the line was lost, the note was not'
    assert json.dumps(report['unsettled']), 'the pass report still names the note the batch left'
