"""Coverage: what an editor answer says became of every unit of a note, and what the editor does about it.

The first batches that published in production stated about half of what their notes said: 95 claims of 21
notes reached no item, and nothing recorded which. Nothing asked for more, and a batch had room for about 31
new facts whatever its size. Under `[editor] coverage` a note is cut into numbered units, an answer says for
each which item states it or why none does, an answer that leaves one out is sent back, and what stays open
is recorded on the note's trace. An accounting gap costs rounds and never a note.
"""
import pytest

from kelmo import coverage
from test_dreaming_batch_conversation import batch_jobs, events_of
from test_dreaming_batched_apply import (
    FEATURE,
    BatchModel,
    capture,
    charged_parkings,
    commits,
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


# ---------------------------------------------------------------------------
# Units: a pure function of the note's text
# ---------------------------------------------------------------------------

NOTE = '''---
date: 2026-11-15
project: app
---
# Release state

The tag v0.3.0 exists; GitHub has no Release page for it. The wrapper still runs 0.1.1, e.g. on the
server. Next: the executor breaker.

- The backup is `app.toml.bak`.
- Основатель сказал: «Ты выключи». Пауза не останавливает проход.

| key | value |
| --- | --- |
| budget | 500000 |

```sh
kelmo dream resume
kelmo dream status
```
'''


def test_a_note_is_cut_into_sentences_rows_and_blocks_and_its_frontmatter_is_no_unit():
    assert coverage.units(NOTE) == [
        'Release state',
        'The tag v0.3.0 exists;',
        'GitHub has no Release page for it.',
        'The wrapper still runs 0.1.1, e.g. on the server.',
        'Next: the executor breaker.',
        'The backup is `app.toml.bak`.',
        'Основатель сказал: «Ты выключи».',
        'Пауза не останавливает проход.',
        '| key | value |',
        '| budget | 500000 |',
        '```sh\nkelmo dream resume\nkelmo dream status\n```',
    ]
    assert coverage.numbered('One. Two.') == [{'n': 1, 'text': 'One.'}, {'n': 2, 'text': 'Two.'}]


@pytest.mark.parametrize('text, expected', [
    ('', []),
    ('---\ndate: 2026-11-15\n---\n', []),
    ('A fence left open\n```\nstays whole. Even here.', ['A fence left open', '```\nstays whole. Even here.']),
    ('1. First step. 2) not a marker here.\n2. Second step.', ['First step.', '2) not a marker here.', 'Second step.']),
    ('See fig. 3 of the paper, e.g. here.', ['See fig. 3 of the paper, e.g. here.']),
    # A single letter is no abbreviation: two sentences in one unit could hide the loss of one.
    ('Take option A. Then the backup runs nightly.', ['Take option A.', 'Then the backup runs nightly.']),
    ('Answer: no. Keep it off.', ['Answer: no.', 'Keep it off.']),
    # A bracket the paragraph never closes holds nothing together.
    ('It broke again :( The fix is in. Ship it.', ['It broke again :( The fix is in.', 'Ship it.']),
    ('It runs alone. kelmo.toml is unchanged.', ['It runs alone.', 'kelmo.toml is unchanged.']),
    # A lower-case continuation after a semicolon is no unit of its own; a new clause is.
    ('First the alert; then the CI scan; the rest later.', ['First the alert; then the CI scan; the rest later.']),
    ('The pass stopped; DHCP cannot say who asked.', ['The pass stopped;', 'DHCP cannot say who asked.']),
    # A quotation, a bracket and a code span are never cut.
    ('He wrote "ship it. Then stop." and left. Done.', ['He wrote "ship it. Then stop." and left.', 'Done.']),
    ('It failed (twice. Then once more.) and passed.', ['It failed (twice. Then once more.) and passed.']),
    # A note that only opens with a rule has no frontmatter: nothing of it is left out.
    ('---\nA first line.\n---\nA second.', ['A first line.', 'A second.']),
])
def test_the_cut_holds_at_its_edges(text, expected):
    assert coverage.units(text) == expected


def test_the_same_text_always_gives_the_same_units():
    assert coverage.units(NOTE) == coverage.units(NOTE)
    assert coverage.body('no frontmatter here') == 'no frontmatter here'


# ---------------------------------------------------------------------------
# The ledger: an answer read against the units
# ---------------------------------------------------------------------------

TEXTS = ['The tag v0.3.0 exists.', 'GitHub has no Release page.', 'Checked at server.py:123.', 'Summary:',
         'The other project moved to 08:00.']
SHOWN = {'app.release.tag': 'The tag v0.3.0 exists and PyPI holds 0.3.0.'}


def test_every_unit_is_stated_left_with_a_reason_or_open():
    answer = {'facts': [{'fact_key': 'app.release.page', 'text': 'GitHub has no Release page for v0.3.0.',
                         'units': [2]}],
              'left': [{'units': [1], 'reason': 'stated', 'items': ['app.release.tag']},
                       {'units': [3], 'reason': 'evidence', 'items': ['app.release.page']},
                       {'units': [4], 'reason': 'context'},
                       {'units': [5], 'reason': 'elsewhere', 'where': 'the  ops   project'}]}
    books, faults = coverage.ledger(answer, {None: TEXTS}, SHOWN)
    book = books[None]
    assert (faults, book['open'], book['stated'], book['unanchored']) == ([], [], [2], [])
    assert [entry['reason'] for entry in book['left']] == ['stated', 'evidence', 'context', 'elsewhere']
    assert book['left'][3]['where'] == 'the ops project'
    assert coverage.refusal(books, faults, {None: TEXTS}) is None
    assert coverage.record(book) == {
        'cut': coverage.CUT, 'units': 5, 'items': 1, 'stated': 1, 'evidence': 1, 'context': 1, 'elsewhere': 1,
        'open': [], 'unanchored': [], 'thin': [], 'dropped': [], 'left': book['left']}
    assert coverage.totals([coverage.record(book)] * 2) == {
        'notes': 2, 'units': 10, 'items': 2, 'stated': 2, 'evidence': 2, 'context': 2, 'elsewhere': 2,
        'open': 0, 'unanchored': 0, 'thin': 0, 'dropped': 0}


@pytest.mark.parametrize('entry, said', [
    ({'units': [1], 'reason': 'trivia'}, 'needs a reason'),
    ({'units': [9], 'reason': 'evidence', 'items': ['app.release.tag']}, 'must name units its source has'),
    ({'units': ['1'], 'reason': 'evidence', 'items': ['app.release.tag']}, 'must name units its source has'),
    ({'units': [1], 'reason': 'stated'}, 'must name in "items"'),
    ({'units': [1], 'reason': 'stated', 'items': []}, 'must name in "items"'),
    ({'units': [1], 'reason': 'evidence', 'items': ['app.nobody.saw']}, 'neither registered nor proposed'),
    # A fact that mentions nothing the unit names does not state it.
    ({'units': [1], 'reason': 'stated', 'items': ['app.audit']},
     'do not mention what unit 1 names (one of: v0.3.0)'),
    # A unit that names a version makes a claim.
    ({'units': [1], 'reason': 'context'}, 'so they make a claim'),
    ({'units': [1], 'reason': 'elsewhere'}, 'names no page or project in "where"'),
    ({'units': [1], 'reason': 'elsewhere', 'where': 'nowhere'}, 'no such place'),
    ('not an entry', 'needs a reason'),
])
def test_an_entry_that_does_not_hold_accounts_for_nothing_and_says_why(entry, said):
    shown = {**SHOWN, 'app.audit': 'The audit closed every open item of that week.'}
    books, faults = coverage.ledger({'left': [entry]}, {None: TEXTS[:1]}, shown,
                                    place=lambda where: None if where == 'ops' else 'no such place')
    assert books[None]['open'] == [1]
    assert len(faults) == 1 and said in faults[0], faults
    refusal = coverage.refusal(books, faults, {None: TEXTS[:1]})
    assert refusal.startswith('these units of the source are in no item\'s "units" and in no valid "left" '
                              'entry: 1 ("The tag v0.3.0 exists.")')
    assert said in refusal, 'the repair names what was wrong with the entry'


def test_a_quotation_is_never_context_and_a_checked_place_is_kept():
    texts = ['Основатель сказал: «Ты выключи».', 'Summary:', 'The ops runbook changes too.']
    books, faults = coverage.ledger(
        {'left': [{'units': [1, 2], 'reason': 'context'}, {'units': [3], 'reason': 'elsewhere', 'where': 'ops'}]},
        {None: texts}, {}, place=lambda where: None if where == 'ops' else 'no such place')
    assert books[None]['open'] == [1], 'the heading is context, the quotation is not'
    assert len(faults) == 1 and 'unit 1 name a number, a path, code or a quotation' in faults[0]
    assert books[None]['left'] == [{'reason': 'context', 'units': [2]},
                                   {'reason': 'elsewhere', 'units': [3], 'where': 'ops'}]


def test_a_batch_entry_names_its_source_and_the_repair_names_each_source_with_its_units():
    sources = {'a' * 36: TEXTS[:2], 'b' * 36: TEXTS[:3]}
    answer = {'facts': [{'fact_key': 'k1', 'source': 'a' * 36, 'text': 'v0.3.0 is tagged', 'units': [1, 2]},
                        {'fact_key': 'k2', 'source': 'b' * 36, 'text': 'no Release page', 'units': [2]}],
              'left': [{'source': 'c' * 36, 'units': [1], 'reason': 'evidence', 'items': ['k1']},
                       {'units': [1], 'reason': 'evidence', 'items': ['k1']}]}
    books, faults = coverage.ledger(answer, sources, SHOWN)
    assert books['a' * 36]['open'] == [] and books['b' * 36]['open'] == [1, 3]
    assert len(faults) == 2 and all('a source this request did not send' in fault for fault in faults)
    refusal = coverage.refusal(books, faults, sources)
    assert 'units of 1 source are in no item' in refusal
    assert 'source ' + 'b' * 36 + ': 1 ("The tag v0.3.0 exists."), 3 ("Checked at server.py:123.")' in refusal
    assert 'a' * 36 not in refusal, 'a source with nothing open is not named'


def test_a_unit_is_never_kept_by_what_another_note_of_the_batch_proposes():
    """That note may publish later or not at all: the unit would be recorded as stated and stated nowhere."""
    sources = {'a' * 36: TEXTS[:1], 'b' * 36: TEXTS[:1]}
    answer = {'facts': [{'fact_key': 'k.b', 'source': 'b' * 36, 'text': 'The tag v0.3.0 exists.', 'units': [1]}],
              'left': [{'source': 'a' * 36, 'units': [1], 'reason': 'stated', 'items': ['k.b']}]}
    books, faults = coverage.ledger(answer, sources, SHOWN)
    assert books['a' * 36]['open'] == [1] and books['b' * 36]['open'] == []
    assert 'neither registered nor proposed for its own source' in faults[0]


def test_an_item_the_proposal_checks_dropped_states_no_unit_unless_it_restates_a_registered_one():
    answer = {'facts': [{'fact_key': 'k.dropped', 'text': 'The tag v0.3.0 exists.', 'units': [1]},
                        {'fact_key': 'app.release.tag', 'text': 'The tag v0.3.0 exists.', 'units': [2]}]}
    books, faults = coverage.ledger(answer, {None: TEXTS[:2]}, SHOWN, taken={'k.kept'})
    assert books[None]['open'] == [1] and books[None]['stated'] == [2]
    assert faults == ['the fact k.dropped was not taken into the proposal, so it states no unit']


def test_a_unit_stated_by_an_item_that_keeps_none_or_few_of_its_identifiers_is_counted_not_refused():
    texts = [*TEXTS[:3], 'Run 43609710765 built the wheels for 0.3.0 on 2026-11-14 at 16:52.']
    answer = {'facts': [{'fact_key': 'k1', 'text': 'Something was released.', 'units': [1, 2]},
                        {'fact_key': 'k2', 'text': 'It was read in server.py:123.', 'units': [3]},
                        {'fact_key': 'k3', 'text': 'The release run built the wheels for 0.3.0.', 'units': [4]}]}
    books, _faults = coverage.ledger(answer, {None: texts}, SHOWN)
    assert books[None]['open'] == [] and books[None]['unanchored'] == [1], \
        'unit 1 names v0.3.0 and its item does not; unit 2 names nothing to look for'
    assert books[None]['thin'] == [4], 'one of its four identifiers reached the item'
    # What each stated unit's items do not keep is named, so a person reads the lost measurement itself.
    assert books[None]['dropped'] == [[1, ['v0.3.0']], [4, ['43609710765', '2026-11-14', '16:52']]]


def test_a_quotation_reworded_and_a_unit_nothing_could_be_looked_for_in_are_listed_not_refused():
    texts = ['He said «значит, надо включить батчинг» at 00:36.', 'Пауза не останавливает проход.']
    answer = {'facts': [{'fact_key': 'k1', 'text': 'At 00:36 he said batching needs to be turned on.',
                         'units': [1]}],
              'left': [{'units': [2], 'reason': 'stated', 'items': ['k.pause']}]}
    books, faults = coverage.ledger(answer, {None: texts}, {'k.pause': 'A pause does not stop a running pass.'})
    book = books[None]
    assert (faults, book['open']) == ([], [])
    assert book['dropped'] == [[1, ['«значит, надо включить батчинг»']]], 'the time was kept, his words were not'
    assert book['unanchored'] == [2], 'no word of a unit in another script can be looked for: it is listed'


def test_the_accounting_is_no_part_of_the_proposal_the_checks_read():
    answer = {'facts': [{'fact_key': 'k', 'text': 't', 'units': [1]}], 'narratives': 'not a list',
              'left': [{'units': [2], 'reason': 'context'}]}
    assert coverage.strip(answer) == {'facts': [{'fact_key': 'k', 'text': 't'}], 'narratives': 'not a list'}
    plain = {'facts': [{'fact_key': 'k', 'text': 't'}]}
    assert coverage.strip(plain) is plain and coverage.strip('no answer') == 'no answer'
    assert answer['facts'][0]['units'] == [1], 'the answer itself keeps what it accounted for'


def test_a_long_list_of_open_units_is_cut_with_its_count():
    books, faults = coverage.ledger({}, {None: ['x.'] * 45}, {})
    refusal = coverage.refusal(books, faults, {None: ['x.'] * 45})
    assert '29 ("x."), 30 ("x.") and 15 more. State each of them' in refusal, refusal
    long = 'A unit of two hundred characters is shown by its start only, ' * 4
    books, faults = coverage.ledger({}, {None: [long]}, {})
    assert '1 ("' + long[:120] + '…")' in coverage.refusal(books, faults, {None: [long]})


@pytest.mark.parametrize('unit, fact, holds', [
    # A short number must be named whole: 2 is not kept by 12, nor 11 by a date.
    ('Limit 2 notes.', 'Batch of 12 notes.', False),
    ('Epic 11 is next.', 'Decided on 2026-11-14.', False),
    ('Limit 2 notes.', 'The limit is 2 notes a batch.', True),
    # A longer one may sit inside a longer token.
    ('Built from 0.3.0.', 'The tag v0.3.0 exists.', True),
    # A unit that names nothing must at least share a word with the fact said to state it.
    ('None is fixed yet.', 'The camera goal is to recognise the household.', False),
    ('None is fixed yet.', 'No finding of the audit is fixed yet.', True),
    # Slash-joined prose is no path, and a note in another script is not compared word by word.
    ('It recognises the founder/wife pair.', 'It recognises the founder and his wife.', True),
    ('Пауза не останавливает проход.', 'A pause does not stop a running pass.', True),
    ('Evidence is in docs/laptop-dark/DIAGNOSIS.md.', 'The diagnosis is written down.', False),
])
def test_a_fact_said_to_state_a_unit_must_mention_what_the_unit_names(unit, fact, holds):
    books, faults = coverage.ledger({'left': [{'units': [1], 'reason': 'stated', 'items': ['k']}]},
                                    {None: [unit]}, {'k': fact})
    assert (books[None]['open'] == []) is holds, faults
    assert bool(faults) is not holds


def test_the_text_an_answer_proposes_for_a_registered_fact_is_the_one_compared():
    """The registered fact names 03:00; the answer rewrites it without the time and calls the unit stated."""
    answer = {'facts': [{'fact_key': 'k', 'text': 'Backups run nightly.'}],
              'left': [{'units': [1], 'reason': 'stated', 'items': ['k']}]}
    books, faults = coverage.ledger(answer, {None: ['Backups run nightly at 03:00.']},
                                    {'k': 'Backups run nightly at 03:00.'})
    assert books[None]['open'] == [1] and 'one of: 03:00' in faults[0]


def test_a_page_is_no_place_to_keep_a_unit_under_stated_or_evidence():
    answer = {'pages': [{'target': 'projects/app/app.md', 'title': 'Restores are tested monthly'}],
              'left': [{'units': [1], 'reason': 'stated', 'items': ['projects/app/app.md']}]}
    books, faults = coverage.ledger(answer, {None: ['Restores are tested on the first Monday.']}, {})
    assert books[None]['open'] == [1] and 'neither registered nor proposed' in faults[0]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_coverage_is_a_boolean_and_off_by_default(world):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    editor_mode(world)
    assert world.service.config.coverage is False
    editor_mode(world, coverage=True)
    assert world.service.config.coverage is True
    for bad in ('1', '"yes"', '0.5'):
        editor_table(world, coverage=bad)
        with pytest.raises(KelmoError) as refused:
            Config.load(world.root)
        assert refused.value.code == 'E_CONFIG' and 'coverage must be a boolean' in str(refused.value)


class Accounting(BatchModel):
    """The batch replay that accounts for units: one fact a note, stating its first sentence.

    `first` is what the first proposal names: `all` the units of each note or only `one`. `later` is what
    an answer to a refusal does: `rest` lists the units still open as context (a batch sends only that, a
    single note its whole proposal again), `same` repeats the first answer, `spoil` repeats it citing a
    receipt no search returned, `give_up` answers `unresolved`."""

    def __init__(self, world, first='all', later='rest'):
        super().__init__(world)
        self.first, self.later = first, later

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        answer = super().execute(job)
        payload = job.payload
        if answer.kind != 'proposal':
            return answer
        batch = payload.get('sources')
        blocks = list(batch) if batch is not None else [dict(payload['source'], operation_id=None)]
        numbers = {block['operation_id']: [unit['n'] for unit in block.get('units', [])] for block in blocks}
        for fact in answer.result['facts']:
            own = numbers[fact.get('source')]
            fact['units'] = own if self.first == 'all' else own[:1]
        if not payload.get('refusal'):
            return answer
        if self.later == 'give_up':
            return ModelOutcome('failure', error_code='E_UNRESOLVED', reason='nothing more these pages should record')
        if self.later == 'rest':
            left = [{'units': own[1:], 'reason': 'context', **({'source': source} if source else {})}
                    for source, own in numbers.items() if own[1:]]
            if batch is not None:
                return ModelOutcome('proposal', {'left': left})
            answer.result['left'] = left
        elif self.later == 'spoil':
            for check in answer.result['fact_checks']:
                check['verified_match'] = {'result': 'not_found', 'search_id': '0' * 32}
        return answer


def test_without_the_key_a_request_carries_no_units_and_nothing_is_recorded(world):
    editor_mode(world, batch_apply=False)
    ids = capture(world, long_notes(1, pads=2))
    model = BatchModel(world)
    report = pending_pass(world, model)
    assert report['applied'] == ids and 'coverage' not in report
    assert 'units' not in model.jobs[0].payload['source']
    assert events_of(world, ids[0], 'editor_coverage') == []


# ---------------------------------------------------------------------------
# One note alone
# ---------------------------------------------------------------------------

def test_a_note_alone_is_sent_its_units_and_an_answer_that_names_them_all_is_taken_at_once(world):
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    model = Accounting(world)
    report = pending_pass(world, model)
    assert report['applied'] == ids and len(model.jobs) == 1
    sent = model.jobs[0].payload['source']
    assert [unit['n'] for unit in sent['units']] == [1, 2, 3] and sent['units'][0]['text'] in sent['text']
    kept = events_of(world, ids[0], 'editor_coverage')
    assert len(kept) == 1 and {name: value for name, value in kept[0].items() if name != 'dropped'} == {
        'cut': coverage.CUT, 'units': 3, 'items': 3, 'stated': 0, 'evidence': 0, 'context': 0, 'elsewhere': 0,
        'open': [], 'unanchored': [], 'thin': [], 'left': []}
    assert {name: value for name, value in report['coverage'].items() if name != 'dropped'} == {
        'notes': 1, 'units': 3, 'items': 3, 'stated': 0, 'evidence': 0, 'context': 0, 'elsewhere': 0, 'open': 0,
        'unanchored': 0, 'thin': 0}
    assert report['coverage']['dropped'] == len(kept[0]['dropped'])


def test_units_left_open_are_asked_for_by_number_and_the_repair_is_taken(world, capsys):
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    model = Accounting(world, first='one')
    report = pending_pass(world, model)
    assert report['applied'] == ids and len(model.jobs) == 2, 'the proposal and its one repair'
    refusal = model.jobs[1].payload['refusal']
    assert refusal['code'] == 'E_PROPOSAL' and refusal['reason'].startswith(
        'these units of the source are in no item\'s "units" and in no valid "left" entry: '
        '2 ("The audit recorded the same reading on every shift of that week."), 3 ("The audit')
    assert 'Send the whole proposal again with every unit accounted for' in refusal['reason']
    kept = events_of(world, ids[0], 'editor_coverage')[-1]
    assert (kept['items'], kept['context'], kept['open']) == (1, 2, [])
    assert charged_parkings(world, ids[0]) == ([], 0)
    assert 'in no item and under no reason' not in capsys.readouterr().err


def test_units_nobody_accounts_for_cost_two_repairs_and_never_the_note(world, capsys):
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    model = Accounting(world, first='one', later='same')
    report = pending_pass(world, model)
    assert len(model.jobs) == 3, 'the proposal and the two repairs it may have'
    assert report['applied'] == ids and (report['parked'], report['failed']) == ([], []), report
    assert len(facts_of(world)) == 1 and charged_parkings(world, ids[0]) == ([], 0)
    assert events_of(world, ids[0], 'editor_coverage')[-1]['open'] == [2, 3]
    assert report['coverage']['open'] == 2
    assert ('kelmo: the answers of this pass left 2 of 3 units of 1 notes in no item and under no reason'
            in capsys.readouterr().err)


def test_a_repair_that_breaks_the_proposal_leaves_the_one_that_stood_to_settle(world):
    """The first proposal only left units open. Both repairs cite a receipt nobody issued: the note is
    published from the proposal that stood, where it used to be refused for the last answer."""
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    model = Accounting(world, first='one', later='spoil')
    report = pending_pass(world, model)
    assert len(model.jobs) == 3
    assert report['applied'] == ids and (report['parked'], report['failed']) == ([], []), report
    assert len(facts_of(world)) == 1 and charged_parkings(world, ids[0]) == ([], 0)
    assert events_of(world, ids[0], 'editor_coverage')[-1]['open'] == [2, 3]


def test_a_refusal_that_ends_the_conversation_settles_the_proposal_that_stood(world):
    """Asked for the units it left open, the model answers `unresolved`. Without the accounting its first
    proposal was published in the round it was given, so the request for more must not park the note."""
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    model = Accounting(world, first='one', later='give_up')
    report = pending_pass(world, model)
    assert report['applied'] == ids and (report['parked'], report['failed']) == ([], []), report
    assert len(facts_of(world)) == 1 and charged_parkings(world, ids[0]) == ([], 0)
    assert events_of(world, ids[0], 'editor_coverage')[-1]['open'] == [2, 3]


class OnlyTheRest(Accounting):
    """Asked for the units it left open, the model sends only what it was asked for: a proposal with no fact
    at all and one more unit as context. Every unit its first proposal stated is open in this one."""

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        answer = super().execute(job)
        if answer.kind == 'proposal' and job.payload.get('refusal'):
            return ModelOutcome('proposal', {'left': [{'units': [2], 'reason': 'context'}]})
        return answer


def test_an_answer_that_carries_only_the_missing_units_never_replaces_the_proposal_it_completes(world):
    """Without the accounting the first proposal was published in the round it was given. A later answer
    that leaves more units open than the one that stands must not be what settles."""
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    report = pending_pass(world, OnlyTheRest(world, first='one', later='same'))
    assert report['applied'] == ids and (report['parked'], report['failed']) == ([], []), report
    assert len(facts_of(world)) == 1, 'the fact of the first proposal is published'
    kept = events_of(world, ids[0], 'editor_coverage')[-1]
    assert (kept['items'], kept['open']) == (1, [2, 3]), 'the record is the standing proposal\'s'


def test_a_note_declined_whole_is_recorded_with_every_unit_unaccounted_for(world, capsys):
    """`unresolved` for the whole note gives no unit a fate. That is the loudest loss, so it is recorded."""
    editor_mode(world, batch_apply=False, coverage=True)
    ids = capture(world, long_notes(1, pads=2))
    report = pending_pass(world, BatchModel(world, decline=True))
    kept = events_of(world, ids[0], 'editor_coverage')
    assert len(kept) == 1 and (kept[0]['units'], kept[0]['items'], kept[0]['open']) == (3, 0, [1, 2, 3]), kept
    assert facts_of(world) == {}
    if ids[0] in report['applied']:
        assert report['coverage']['open'] == 3
        assert 'left 3 of 3 units of 1 notes' in capsys.readouterr().err


def test_an_elsewhere_page_is_another_project_or_one_the_budget_left_out(world):
    from kelmo import editor
    registry = world.service.knowledge.registry(world.service.knowledge.generation())
    own = sorted(target for target in registry['pages'] if target.startswith('projects/app/'))
    other = sorted(target for target in registry['pages'] if not target.startswith('projects/app/'))
    assert FEATURE in own and len(own) > 1 and other
    unsent = next(target for target in own if target != FEATURE)
    place = editor._Elsewhere(registry, 'app', [FEATURE], omitted=[])
    assert 'a page you were sent and may change' in place(FEATURE)
    assert place(other[0]) is None, 'a page of another project'
    assert 'is sent with the next round' in place(unsent) and place.send == [unsent]
    assert 'neither a registered page nor another project' in place('nowhere')
    assert 'neither a registered page nor another project' in place('app'), 'its own project is not elsewhere'
    assert editor._Elsewhere(registry, 'app', [FEATURE], omitted=[unsent])(unsent) is None, \
        'a page the budget left out cannot be sent'


def test_the_instructions_and_the_schema_follow_what_the_request_carries():
    """Without units the model reads the instructions it always read, and may not answer with accounting."""
    from kelmo import editor
    plain = editor.editing({'source': {'text': 'x'}})
    covered = editor.editing({'sources': [{'text': 'x', 'units': [{'n': 1, 'text': 'x'}]}]})
    assert plain == (editor.EDITING, editor.EDITING_SCHEMA)
    assert covered == (editor.EDITING_COVERED, editor.EDITING_COVERED_SCHEMA)
    assert editor.editing({'source': {'text': 'x', 'units': []}}) == covered
    for text in (editor.EDITING, editor.EDITING_COVERED):
        assert '@@' not in text
    assert '"units"' not in editor.EDITING and '"left"' not in editor.EDITING
    assert editor.EDITING_COVERED.splitlines()[1].startswith('When a source carries "units", your proposal says')
    assert 'left' not in editor.EDITING_SCHEMA['properties']
    assert 'units' not in editor.EDITING_SCHEMA['properties']['facts']['items']['properties']
    assert editor.EDITING_COVERED_SCHEMA['properties']['left']['items']['properties']['reason']['enum'] == \
        list(coverage.REASONS)
    assert all('units' in editor.EDITING_COVERED_SCHEMA['properties'][kind]['items']['properties']
               for kind in coverage.ITEM_KINDS)


def test_without_the_key_an_answer_that_carries_accounting_is_malformed_as_before(world):
    editor_mode(world, batch_apply=False)
    ids = capture(world, long_notes(1, pads=2))

    class Uninvited(BatchModel):
        def execute(self, job):
            answer = super().execute(job)
            answer.result['left'] = [{'units': [2, 3], 'reason': 'context'}]
            return answer

    report = pending_pass(world, Uninvited(world))
    assert report['applied'] == [] and facts_of(world) == {}
    assert world.service.journal.get(ids[0])['error_code'] == 'E_PROPOSAL'


def test_a_note_alone_gets_two_more_rounds_only_under_coverage(world):
    from kelmo import editor
    assert (editor.MAX_ROUNDS, editor.MAX_BATCH_ROUNDS, editor.MAX_COVERAGE_ROUNDS) == (5, 8, 2)


# ---------------------------------------------------------------------------
# A batch
# ---------------------------------------------------------------------------

def test_a_batch_that_accounts_for_every_unit_is_one_call_and_each_note_has_its_record(world):
    editor_mode(world, coverage=True)
    ids = capture(world, long_notes(3, pads=2))
    model = Accounting(world)
    before = commits(world)
    report = pending_pass(world, model)
    assert len(model.jobs) == 1 and sorted(report['applied']) == sorted(ids)
    assert commits(world) == before + 1
    assert all(len(source['units']) == 3 for source in model.jobs[0].payload['sources'])
    for operation_id in ids:
        assert events_of(world, operation_id, 'editor_coverage')[-1]['open'] == []
    assert (report['coverage']['notes'], report['coverage']['units'], report['coverage']['open']) == (3, 9, 0)


def test_a_batch_is_asked_only_for_the_units_left_open_and_what_it_adds_is_laid_over_the_proposal(world):
    editor_mode(world, coverage=True)
    ids = capture(world, long_notes(3, pads=2))
    model = Accounting(world, first='one')
    before = commits(world)
    report = pending_pass(world, model)
    assert [len(job.payload['sources']) for job in batch_jobs(model)] == [3, 3] and len(model.jobs) == 2
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert commits(world) == before + 1 and len(facts_of(world)) == 3, 'the facts of the first proposal were kept'
    reason = model.jobs[1].payload['refusal']['reason']
    assert 'units of 3 sources are in no item' in reason and all(
        'source ' + operation_id + ': 2 ("The audit recorded' in reason for operation_id in ids), reason
    assert 'Answer only for the units named here' in reason and 'do not send it again' in reason
    for operation_id in ids:
        kept = events_of(world, operation_id, 'editor_coverage')[-1]
        assert (kept['items'], kept['context'], kept['open']) == (1, 2, [])


def test_a_batch_whose_units_stay_open_settles_what_it_has_and_sends_no_note_alone(world, capsys):
    editor_mode(world, coverage=True)
    ids = capture(world, long_notes(3, pads=2))
    model = Accounting(world, first='one', later='same')
    before = commits(world)
    report = pending_pass(world, model)
    assert len(batch_jobs(model)) == 3 and len(model.jobs) == 3, 'the proposal, two repairs, and no note alone'
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert commits(world) == before + 1 and 'unsettled' not in report
    for operation_id in ids:
        assert events_of(world, operation_id, 'editor_coverage')[-1]['open'] == [2, 3]
        assert charged_parkings(world, operation_id) == ([], 0)
    assert report['coverage']['open'] == 6
    assert 'left 6 of 9 units of 3 notes' in capsys.readouterr().err


def test_a_repair_of_a_batch_that_breaks_it_leaves_the_standing_proposal_to_settle(world):
    editor_mode(world, coverage=True)
    ids = capture(world, long_notes(3, pads=2))
    model = Accounting(world, first='one', later='spoil')
    report = pending_pass(world, model)
    assert len(model.jobs) == 3
    assert sorted(report['applied']) == sorted(ids) and (report['parked'], report['failed']) == ([], [])
    assert len(facts_of(world)) == 3


# ---------------------------------------------------------------------------
# Room for the facts
# ---------------------------------------------------------------------------

class AsksForMany(BatchModel):
    """The batch replay after one reconcile round that asks for more receipts than one note may."""

    def __init__(self, world, count):
        super().__init__(world)
        self.count, self.asked = count, False

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        if 'sources' in job.payload and not self.asked:
            self.asked = True
            self.jobs.append(job)
            return ModelOutcome('reconcile', {'facts': [{'text': f'Reading {index} was taken that week.',
                                                         'target': FEATURE} for index in range(self.count)]})
        return super().execute(job)


def test_a_batch_reconcile_round_takes_as_many_requests_as_its_proposal_may_carry_facts(world):
    """Cut to one note's 30, the facts of the newest notes got no receipt and no word of it."""
    editor_mode(world)
    ids = capture(world, notes(2))
    model = AsksForMany(world, 45)
    report = pending_pass(world, model)
    assert sorted(report['applied']) == sorted(ids)
    assert len(batch_jobs(model)[1].payload['reconciliations']) == 45


def test_the_receipts_may_take_half_the_budget_and_a_batch_is_reserved_room_by_its_units():
    from kelmo import editor
    assert editor._receipt_ceiling(500_000, 85_000) == 250_000
    assert editor._receipt_ceiling(500_000, 300_000) == 200_000, 'never past the room the pages leave'
    assert editor._receipt_ceiling(200_000, 20_000) == 100_000
    assert editor._receipt_reserve(500_000, [3, 3]) == 20_000, 'a short note keeps the floor'
    assert editor._receipt_reserve(500_000, [13, 3]) == 36_000, '2 000 a unit for the long one'
    assert editor._receipt_reserve(500_000, [19] * 8) == 125_000, 'never more than a quarter of the budget'


def test_a_batch_over_its_receipt_ceiling_is_told_to_leave_whole_sources_out_not_to_thin_them():
    from kelmo import editor, outcomes
    alone = editor._receipt_ceiling_refusal(50_000, 61_000, 200_000)
    batch = editor._receipt_ceiling_refusal(250_000, 261_000, 500_000, batch=True)
    assert 'propose with the receipts you already have' in alone
    assert 'propose with the receipts you already have' not in batch
    assert 'leave every other source out of your answer whole' in batch
    assert 'Never drop facts to make room' in batch and len(batch) <= outcomes.REASON_LIMIT
    assert batch.startswith('the reconciliation receipts of this request may cost at most 250000 estimated '
                            'tokens of its 500000; the searches you asked for would bring them to 261000.')
