"""Notes reach the wiki without content-level failures: the editor opens the work page a source needs,
a refused source parks for the next pass, nightly mode holds notes for `kelmo dream apply`, and a note's
pending line is delivered at startup until it is applied."""
import json

import pytest

from test_editor_scoping import FEATURE, HUB, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def open_work(slug, title, summary=None, priority=None):
    from kelmo.executor import ModelOutcome
    work = {'slug': slug, 'title': title}
    if summary is not None:
        work['summary'] = summary
    if priority is not None:
        work['priority'] = priority
    return ModelOutcome('open_work', {'work': work})


def nightly(world):
    from kelmo.config import Config
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[editor]\nmode = "nightly"\n', encoding='utf-8')
    world.service.config = Config.load(world.root)


def test_the_editor_opens_the_work_page_the_model_asks_for(world):
    target = 'projects/app/export-rework.md'
    operation = world.service.create('Next: rework the export, it is slow.', project='app')
    fact = dict(fact_key='app.export-rework', target=target, text='Rework the export pipeline.',
                status='backlog', expected_revision=None)
    seen = []
    executor = Scripted(lambda job: seen.append(job.payload.get('new_work')) or open_work(
                            'export-rework', 'Rework the export', 'The export is slow.'),
                        lambda job: seen.append(job.payload.get('new_work')) or world.propose(facts=[fact]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert seen == [None, {'target': target, 'title': 'Rework the export'}]
    page = world.registry()['pages'][target]
    assert (page['kind'], page['layout'], page['page_id'], page['parent'], page['summary']) == (
        'work', 'feature', 'projects-app-export-rework', HUB, 'The export is slow.')
    assert world.fact('app.export-rework')['target'] == target


def test_a_taken_slug_is_allocated_past_and_a_second_request_is_refused_not_failed(world):
    operation = world.service.create('More feature work.', project='app', selected=[FEATURE])
    target = 'projects/app/feature-2.md'
    fact = dict(fact_key='app.feature-more', target=target, text='More feature work is planned.',
                status='backlog', expected_revision=None)
    executor = Scripted(open_work('feature', 'Feature again'), open_work('feature', 'Feature again'),
                        lambda job: world.propose(facts=[fact]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert executor.jobs[1].payload['new_work']['target'] == target
    assert executor.jobs[2].payload['refusal']['code'] == 'E_PROPOSAL'
    assert world.registry()['pages'][target]['page_id'] == 'projects-app-feature-2'


def test_a_work_page_request_for_a_map_less_project_is_a_repair_not_a_refusal(world):
    """A person note: the model asks for a work page, none can exist, it is told so and proposes the
    settled fact instead."""
    operation = world.service.create('Rule: exports are checked by hand.', project='person')
    person = 'person.md'
    fact = dict(fact_key='person.exports-checked', target=person, text='Exports are checked by hand.',
                status='active', expected_revision=None)
    executor = Scripted(open_work('export-checks', 'Export checks'), lambda job: world.propose(facts=[fact]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert executor.jobs[1].payload['refusal']['code'] == 'E_PROPOSAL'
    assert 'has no map or index page' in executor.jobs[1].payload['refusal']['reason']
    assert '--target' not in executor.jobs[1].payload['refusal']['reason']
    assert world.fact('person.exports-checked')['target'] == person


def test_a_declined_note_with_a_long_reason_settles_as_evidence_with_a_clipped_reason(world):
    """A model reason longer than the outcome limit is cut to fit instead of failing the note."""
    from kelmo.executor import ModelOutcome
    operation = world.service.create('Small talk about exports.', 'export-talk.md', project='person')
    executor = Scripted(ModelOutcome('failure', error_code='E_UNRESOLVED', reason='nothing to record ' * 60))
    result = world.service.run(operation['id'], executor=executor)
    assert (result['state'], result['terminal']) == ('applied', True), result.get('outcome')
    outcome = world.outcome(operation['id'])
    assert outcome['disposition'] == 'archive-only'
    assert outcome['reason'].startswith('the editor applied nothing: nothing to record')
    assert len(outcome['reason']) == 400 and outcome['reason'].endswith('...')
    assert len(executor.jobs) == 1


def test_a_declined_source_gets_a_work_page_named_after_it_then_settles_as_evidence(world):
    from kelmo.executor import ModelOutcome
    operation = world.service.create('Plan: migrate the archive.', 'archive-migration.md', project='app')
    target = 'projects/app/archive-migration.md'
    fact = dict(fact_key='app.archive-migration', target=target, text='The archive migration is planned.',
                status='backlog', expected_revision=None)
    executor = Scripted(ModelOutcome('failure', error_code='E_UNRESOLVED', reason='no work page'),
                        lambda job: world.propose(facts=[fact]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert executor.jobs[1].payload['new_work'] == {'target': target, 'title': 'Archive migration'}
    assert world.registry()['pages'][target]['title'] == 'Archive migration'
    again = world.service.create('Small talk only.', 'small-talk.md', project='app')
    executor = Scripted(ModelOutcome('failure', error_code='E_UNRESOLVED', reason='nothing to record'),
                        ModelOutcome('failure', error_code='E_UNRESOLVED', reason='still nothing'))
    result = world.service.run(again['id'], executor=executor)
    assert (result['state'], result['terminal']) == ('applied', True)
    assert world.outcome(again['id']) == {'disposition': 'archive-only',
                                         'reason': 'the editor applied nothing: still nothing'}
    assert 'projects/app/small-talk.md' not in world.registry()['pages']


def test_an_executor_failure_parks_instead_of_failing(world):
    from kelmo.creation import apply_pending
    from kelmo.executor import ModelOutcome
    operation = world.service.create('Exports moved to 08:00.', project='app', selected=[FEATURE])
    broken = Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='no answer object'))
    result = world.service.run(operation['id'], executor=broken)
    assert (result['state'], result['error_code'], result['terminal']) == ('parked', 'E_EXECUTOR', False)
    fixed = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 08:00.')]))
    assert apply_pending(world.service, fixed)['applied'] == [operation['id']]
    assert fixed.jobs[0].payload['refusal']['code'] == 'E_EXECUTOR'


def test_nightly_mode_holds_archived_notes_for_the_apply_pass(world):
    from kelmo.creation import apply_pending, pending_operations
    from kelmo.daemon import runnable_operations
    nightly(world)
    operation = world.service.create('Exports moved to 09:00.', project='app', selected=[FEATURE],
                                     pending='Export time moves to 09:00.')
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 09:00.')]))
    held = world.service.run(operation['id'], executor=executor)
    assert (held['state'], held['terminal'], executor.jobs) == ('archived', False, [])
    events = [event['stage'] for event in world.service.trace(operation['id'])['events']]
    assert events[-1] == 'awaiting_dreaming'
    assert list(runnable_operations(world.service, None)) == [], 'the worker must not consult the model by day'
    assert [(op['id'], request['pending']) for op, request in pending_operations(world.service)] == [
        (operation['id'], 'Export time moves to 09:00.')]
    report = apply_pending(world.service, executor)
    assert report['applied'] == [operation['id']] and len(executor.jobs) == 1
    assert world.fact('app.export')['text'] == 'Exports run at 09:00.'
    assert pending_operations(world.service) == []


def echo_export(world):
    """An editor that writes the note's own text into the export fact, so the last applied note wins."""
    return Scripted(*[lambda job: world.propose(facts=[world.change('app.export', job.payload['source']['text'])])
                      for _ in range(4)])


def test_a_held_note_waits_for_an_older_note_still_in_the_intake(world):
    from kelmo.creation import ORDER_POLL_SECONDS, apply_pending
    nightly(world)
    older = world.service.create('Exports run at 11:00.', project='app', selected=[FEATURE])
    newer = world.service.create('Exports run at 12:00.', project='app', selected=[FEATURE])
    world.service.run(newer['id'])
    executor = echo_export(world)
    naps = []

    def sleep(seconds):
        # The worker archives the older note while the pass waits for it.
        naps.append(seconds)
        world.service.run(older['id'])

    report = apply_pending(world.service, executor, sleep=sleep)
    assert naps == [ORDER_POLL_SECONDS]
    assert report['applied'] == [older['id'], newer['id']] and report['stalled'] == []
    assert [job.payload['source']['text'] for job in executor.jobs] == [
        'Exports run at 11:00.', 'Exports run at 12:00.']
    assert world.fact('app.export')['text'] == 'Exports run at 12:00.'


def test_a_pass_waiting_for_an_older_note_lets_go_of_the_baton(world):
    """The worker that archives the older note, and every capture, write under the same baton as the pass. The
    pass lets the baton go while it waits; holding it would stall them all until its wait budget is spent."""
    import threading
    from kelmo.core import baton as batons
    from kelmo.creation import apply_pending
    nightly(world)
    older = world.service.create('Exports run at 11:00.', project='app', selected=[FEATURE])
    newer = world.service.create('Exports run at 12:00.', project='app', selected=[FEATURE])
    world.service.run(newer['id'])
    baton = batons.Baton()

    def sleep(seconds):
        took = threading.Event()

        def writer():
            with baton.hold():
                took.set()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        if not took.wait(5):
            raise AssertionError('a writer could not take the baton while the pass waited')
        thread.join(5)
        world.service.run(older['id'])  # the worker's write, which needed that baton

    batons.install(baton)
    try:
        report = apply_pending(world.service, echo_export(world), sleep=sleep,
                               step=lambda: baton.hold(interactive=False))
    finally:
        batons.install(None)
    assert report['applied'] == [older['id'], newer['id']] and report['stalled'] == []


@pytest.mark.parametrize('slow', ['semantic_projection', 'publish_semantic'])
def test_a_capture_during_a_publication_does_not_wait_for_it(world, monkeypatch, slow):
    """A note's publication renders pages and commits Git; the journal sees only its prepare and its finish. A
    capture that arrives meanwhile is written at once instead of waiting for the whole publication, and every use
    of the shared journal connection still holds the baton."""
    import threading
    import time
    from kelmo.core import baton as batons
    from kelmo.creation import apply_pending
    from kelmo.service import Service
    from kelmo.wiki import engine
    nightly(world)
    # The state core's writer: one journal connection shared by its threads under the baton.
    world.service = Service(world.root, shared_journal=True)
    note = world.service.create('Exports run at 13:00.', project='app', selected=[FEATURE])
    world.service.run(note['id'])
    baton = batons.Baton()
    entered, release, captured = threading.Event(), threading.Event(), threading.Event()
    real = getattr(engine, slow)

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(30):
            raise AssertionError('the publication was never let go')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, slow, held)
    outcome = {}

    def apply():
        outcome['report'] = apply_pending(world.service, echo_export(world),
                                          step=lambda: baton.hold(interactive=False))

    def capture():
        with baton.hold():
            outcome['capture'] = world.service.create('Exports run at 14:00.', project='app', selected=[FEATURE])
        captured.set()

    world.service.journal.guard = baton.check
    batons.install(baton)
    pass_thread = threading.Thread(target=apply, daemon=True)
    capture_thread = threading.Thread(target=capture, daemon=True)
    try:
        pass_thread.start()
        assert entered.wait(30), 'the publication never started'
        began = time.monotonic()
        capture_thread.start()
        fast = captured.wait(3)
        took = time.monotonic() - began
        release.set()
        capture_thread.join(30)
        pass_thread.join(60)
    finally:
        release.set()
        batons.install(None)
        world.service.journal.guard = None
    assert fast, f'the capture waited for the publication ({took:.1f} s)'
    assert outcome['report']['applied'] == [note['id']]
    assert world.service.journal.get(outcome['capture']['id'])['terminal'] == 0


def test_a_writer_waiting_for_the_worker_lock_during_a_publication_lets_it_finish(world, monkeypatch):
    """A publisher holds the worker lock and publishes with the baton released. An interactive writer that holds the
    baton and asks for the worker lock waits for it without the baton, so the publication takes the baton back to
    write its receipt, and both finish in seconds: no wait until the worker lock times out, no E_BUSY."""
    import threading
    import time
    from kelmo.core import baton as batons
    from kelmo.creation import apply_pending
    from kelmo.publication import coordinator_lock
    from kelmo.service import Service
    from kelmo.wiki import engine
    nightly(world)
    world.service = Service(world.root, shared_journal=True)
    note = world.service.create('Exports run at 15:00.', project='app', selected=[FEATURE])
    world.service.run(note['id'])
    baton = batons.Baton()
    entered, release = threading.Event(), threading.Event()
    real = engine.publish_semantic

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(30):
            raise AssertionError('the publication was never let go')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'publish_semantic', held)
    outcome, order = {}, []

    def apply():
        outcome['report'] = apply_pending(world.service, echo_export(world),
                                          step=lambda: baton.hold(interactive=False))
        order.append('pass')

    def writer():
        try:
            with baton.hold(), coordinator_lock(world.service.config):
                order.append('writer')
        except Exception as error:  # noqa: BLE001 - reported below
            outcome['writer'] = error

    world.service.journal.guard = baton.check
    batons.install(baton)
    pass_thread = threading.Thread(target=apply, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    try:
        pass_thread.start()
        assert entered.wait(30), 'the publication never started'
        writer_thread.start()
        time.sleep(0.3)  # the writer now holds the baton's turn and waits for the worker lock
        began = time.monotonic()
        release.set()
        pass_thread.join(20)
        writer_thread.join(20)
        took = time.monotonic() - began
    finally:
        release.set()
        batons.install(None)
        world.service.journal.guard = None
    assert 'writer' not in outcome, outcome.get('writer')
    assert outcome['report']['applied'] == [note['id']]
    assert sorted(order) == ['pass', 'writer'] and took < 10, (order, took)


def test_a_dream_apply_reaches_the_journal_only_inside_its_steps(world, tmp_path):
    """In the state core every use of the shared journal connection holds the baton. A dream apply on a service
    that has not dreamed yet (as after a start), under a guard that refuses any other use, parks its note when
    the editor fails and raises nothing."""
    import sys
    from kelmo.core import baton as batons
    from kelmo.dreaming_cli import run_dream
    nightly(world)
    operation = world.service.create('Exports run at 16:00.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    stub = tmp_path / 'failing_editor.py'
    stub.write_text('import sys\nsys.stdin.read()\nsys.exit(3)\n', encoding='utf-8')
    config = tmp_path / 'editor.json'
    config.write_text(json.dumps({'argv': [sys.executable, str(stub)], 'timeout_seconds': 30}), encoding='utf-8')
    world.service.dreaming._set_state('settings', {'executor_config': str(config), 'max_sources': 10,
                                                   'max_pages': 10, 'budget_tokens': 100000})
    world.service.dreaming._set_state('paused', False)
    vars(world.service).pop('_dreaming', None)
    baton = batons.Baton()
    world.service.journal.guard = baton.check
    batons.install(baton)
    try:
        result = run_dream(world.service, 'apply', step=lambda: baton.hold(interactive=False))
    finally:
        batons.install(None)
        world.service.journal.guard = None
    assert result['state'] == 'finished'
    assert result['apply']['parked'] == [operation['id']]


def test_an_older_note_the_pass_waited_an_hour_for_is_reported_and_passed(world):
    import time
    from kelmo.creation import ORDER_WAIT_SECONDS, apply_pending
    nightly(world)
    stuck = world.service.create('Exports run at 13:00.', project='app', selected=[FEATURE])
    held = world.service.create('Exports run at 14:00.', project='app', selected=[FEATURE])
    world.service.run(held['id'])
    now = [time.time()]
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        now[0] += ORDER_WAIT_SECONDS / 2

    report = apply_pending(world.service, echo_export(world), clock=lambda: now[0], sleep=sleep)
    assert len(naps) == 2, 'the pass waits its full hour, then passes the note'
    assert report['stalled'] == [stuck['id']] and report['applied'] == [held['id']]
    assert world.fact('app.export')['text'] == 'Exports run at 14:00.'


def test_an_orphan_note_older_than_a_day_is_passed_without_waiting(world):
    import time
    from kelmo.creation import ORPHAN_SECONDS, apply_pending
    nightly(world)
    orphan = world.service.create('Exports run at 13:30.', project='app', selected=[FEATURE])
    held = world.service.create('Exports run at 14:30.', project='app', selected=[FEATURE])
    world.service.run(held['id'])
    report = apply_pending(world.service, echo_export(world), clock=lambda: time.time() + ORPHAN_SECONDS + 1,
                           sleep=lambda seconds: pytest.fail('an orphan must not be waited for'))
    assert report['stalled'] == [orphan['id']] and report['applied'] == [held['id']]


def test_an_older_note_that_meets_a_held_lock_is_retried_before_newer_ones(world, monkeypatch):
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    older = world.service.create('Exports run at 20:00.', project='app', selected=[FEATURE])
    newer = world.service.create('Exports run at 21:00.', project='app', selected=[FEATURE])
    world.service.run(older['id'])
    world.service.run(newer['id'])
    publish = creation.publish
    held = [True]

    def first_busy(service, operation_id, *args, **kwargs):
        if operation_id == older['id'] and held[0]:
            held[0] = False
            raise KelmoError('E_BUSY', 'Another worker is processing this vault.')
        return publish(service, operation_id, *args, **kwargs)

    monkeypatch.setattr(creation, 'publish', first_busy)
    executor = echo_export(world)
    naps = []
    report = creation.apply_pending(world.service, executor, sleep=naps.append)
    assert naps == [creation.PUBLISH_RETRY_SECONDS] and report['skipped'] == []
    assert report['applied'] == [older['id'], newer['id']]
    assert [job.payload['source']['text'] for job in executor.jobs] == [
        'Exports run at 20:00.', 'Exports run at 21:00.']


def test_an_older_note_failed_on_a_held_lock_is_waited_for(world):
    from kelmo.creation import apply_pending, notes_in_intake
    nightly(world)
    older = world.service.create('Exports run at 15:00.', project='app', selected=[FEATURE])
    newer = world.service.create('Exports run at 16:00.', project='app', selected=[FEATURE])
    world.service.run(newer['id'])
    world.service.journal.stage(older['id'], 'failed', terminal=True, error_code='E_BUSY')
    assert [row['id'] for row in notes_in_intake(world.service, newer['created_at'])] == [older['id']]
    world.service.journal.stage(older['id'], 'failed', terminal=True, error_code='E_INPUT')
    assert notes_in_intake(world.service, newer['created_at']) == [], 'a final refusal holds nothing back'
    assert apply_pending(world.service, echo_export(world))['applied'] == [newer['id']]


def test_a_note_captured_after_the_pass_began_waits_for_the_next_pass(world):
    from kelmo.creation import apply_pending
    nightly(world)
    first = world.service.create('Exports run at 17:00.', project='app', selected=[FEATURE])
    world.service.run(first['id'])
    late = []

    def capture_during_the_pass(job):
        if not late:
            late.append(world.service.create('Exports run at 18:00.', project='app', selected=[FEATURE]))
            world.service.run(late[0]['id'])
        return world.propose(facts=[world.change('app.export', job.payload['source']['text'])])

    report = apply_pending(world.service, Scripted(capture_during_the_pass))
    assert report['applied'] == [first['id']]
    assert world.service.journal.get(late[0]['id'])['state'] == 'archived'


def test_a_stale_generation_refusal_parks_without_spending_an_attempt(world):
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    operation = world.service.create('Exports run at 22:00.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    token = world.service.journal.claim(operation['id'], lease_seconds=60)
    for _ in range(creation.PARK_ATTEMPTS + 1):
        parked = creation.park(world.service, operation['id'], KelmoError('E_STALE', 'Generation moved.'), token, 0)
        assert parked['state'] == 'parked'
    world.service.journal.release(operation['id'], token)
    parked_count, charged, last = creation._parked(world.service.trace(operation['id']))
    assert (parked_count, charged, last['error_code']) == (creation.PARK_ATTEMPTS + 1, 0, 'E_STALE')
    assert creation.apply_pending(world.service, echo_export(world))['applied'] == [operation['id']]


def test_one_wait_budget_covers_every_stuck_note_in_a_pass(world):
    import time
    from kelmo.creation import ORDER_WAIT_SECONDS, apply_pending
    nightly(world)
    first = world.service.create('Exports run at 06:00.', project='app', selected=[FEATURE])
    second = world.service.create('Exports run at 06:30.', project='app', selected=[FEATURE])
    held = world.service.create('Exports run at 07:00.', project='app', selected=[FEATURE])
    world.service.run(held['id'])
    now = [time.time()]
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        now[0] += ORDER_WAIT_SECONDS / 2

    report = apply_pending(world.service, echo_export(world), clock=lambda: now[0], sleep=sleep)
    assert len(naps) == 2, 'two stuck notes share one hour, not one hour each'
    assert report['stalled'] == [first['id'], second['id']] and report['applied'] == [held['id']]


def test_stale_refusals_are_free_only_up_to_their_cap(world):
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    operation = world.service.create('Exports run at 23:00.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    token = world.service.journal.claim(operation['id'], lease_seconds=60)
    for count in range(creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS):
        _, charged, _ = creation._parked(world.service.trace(operation['id']))
        result = creation.park(world.service, operation['id'], KelmoError('E_STALE', 'Generation moved.'),
                               token, charged)
        if result['state'] == 'failed':
            break
    assert result['state'] == 'failed'
    assert count + 1 == creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS


@pytest.mark.parametrize('code', ['E_OUTCOME', 'E_BINDING'])
def test_an_outcome_or_binding_refusal_is_free_only_up_to_the_same_cap(world, code):
    """A refusal the vault caused is not the note's fault, but it is not free forever either.

    The first FREE_STALE_REFUSALS of them park without charging the note; past that bound the note runs
    out of its PARK_ATTEMPTS and fails, and the vault owner is alerted."""
    import sys

    from kelmo import creation
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    nightly(world)
    scratch = world.root.parent
    script = scratch / 'alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n')
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[alerts]\ncommand = ["' + sys.executable + '", "'
                      + str(script) + '", "' + str(scratch / 'messages.txt') + '"]\n', encoding='utf-8')
    world.service.config = Config.load(world.root)
    operation = world.service.create('Exports run at 23:30.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    token = world.service.journal.claim(operation['id'], lease_seconds=60)
    try:
        for count in range(creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS):
            _, charged, _ = creation._parked(world.service.trace(operation['id']))
            result = creation.park(world.service, operation['id'], KelmoError(code, 'Synthetic ' + code + '.'),
                                   token, charged)
            if result['state'] == 'failed':
                break
    finally:
        world.service.journal.release(operation['id'], token)
    assert result['state'] == 'failed'
    assert count + 1 == creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS
    assert result['outcome']['reason'].startswith(code + ': ')
    lines = (scratch / 'messages.txt').read_text().splitlines()
    assert len(lines) == 1 and 'could not be placed after' in lines[0], lines


def test_an_outcome_refusal_parks_a_note_without_failing_it(world):
    """Repeated bookkeeping refusals keep the note waiting, exactly as a stale generation does."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    operation = world.service.create('Exports run at 23:45.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    token = world.service.journal.claim(operation['id'], lease_seconds=60)
    for _ in range(creation.PARK_ATTEMPTS + 1):
        parked = creation.park(world.service, operation['id'], KelmoError('E_OUTCOME', 'Synthetic E_OUTCOME.'),
                               token, 0)
        assert parked['state'] == 'parked'
    world.service.journal.release(operation['id'], token)
    parked_count, charged, last = creation._parked(world.service.trace(operation['id']))
    assert (parked_count, charged, last['error_code']) == (creation.PARK_ATTEMPTS + 1, 0, 'E_OUTCOME')


def refused_note(world, text, code='E_PROPOSAL'):
    """One archived note driven through `park` until it fails, with the key of each of its attempts."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    operation = world.service.create(text, project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    token = world.service.journal.claim(operation['id'], lease_seconds=60)
    keys, states = [], []
    while True:
        key, _, charged = creation._attempt(world.service, operation['id'])
        keys.append(key)
        row = creation.park(world.service, operation['id'], KelmoError(code, 'Synthetic ' + code + '.'),
                            token, charged)
        states.append(row['state'])
        if row['state'] != 'parked':
            return operation['id'], token, keys, states


def test_a_retried_note_gets_its_charged_attempts_back(world):
    """`kelmo queue retry` records `retry_budget_reset` and the attempt accounting must honour it: a note
    that had already spent its charged parks gets PARK_ATTEMPTS again, and its new attempts never replay
    an earlier attempt's request row."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    operation_id, token, keys, states = refused_note(world, 'Exports run at 23:50.')
    assert states == ['parked'] * (creation.PARK_ATTEMPTS - 1) + ['failed'], states
    assert len(set(keys)) == len(keys) == creation.PARK_ATTEMPTS, keys
    retried = world.service.retry(operation_id)
    assert retried['state'] == 'queued'
    stages = [event['stage'] for event in world.service.trace(operation_id)['events']]
    assert stages.count('retry_budget_reset') == 1, stages
    assert creation._parked(world.service.trace(operation_id))[:2] == (0, 0), \
        'the parks before the reset are no longer attempts of this note'
    token = world.service.journal.claim(operation_id, lease_seconds=60)
    again, states = [], []
    while True:
        key, _, charged = creation._attempt(world.service, operation_id)
        assert key not in keys, 'a retried attempt reuses an old key and would replay its row'
        assert key not in again
        again.append(key)
        row = creation.park(world.service, operation_id, KelmoError('E_PROPOSAL', 'Synthetic E_PROPOSAL.'),
                            token, charged)
        states.append(row['state'])
        if row['state'] != 'parked':
            break
    assert states == ['parked'] * (creation.PARK_ATTEMPTS - 1) + ['failed'], states
    assert len(set(again)) == creation.PARK_ATTEMPTS, again


def test_a_retry_renews_the_free_cap_of_a_vault_caused_refusal(world):
    """The same reset for the uncharged refusals: the FREE_STALE_REFUSALS a note already spent do not
    carry past its retry, so a note the vault refused again and again is not failed by the old count."""
    from kelmo import creation
    from kelmo.errors import KelmoError
    nightly(world)
    operation_id, token, _keys, states = refused_note(world, 'Exports run at 23:55.', code='E_STALE')
    assert states == ['parked'] * (creation.FREE_STALE_REFUSALS + creation.PARK_ATTEMPTS - 1) + ['failed'], states
    world.service.retry(operation_id)
    token = world.service.journal.claim(operation_id, lease_seconds=60)
    _, charged, _ = creation._parked(world.service.trace(operation_id))
    assert charged == 0
    row = creation.park(world.service, operation_id, KelmoError('E_STALE', 'Generation moved.'), token, charged)
    assert row['state'] == 'parked', row
    assert creation._parked(world.service.trace(operation_id))[:2] == (1, 0)


def test_pending_lines_are_delivered_at_startup_until_the_note_is_applied(world):
    from kelmo.creation import apply_pending, pending_summaries
    from kelmo.hot import pending_block
    nightly(world)
    first = world.service.create('Exports moved to 10:00.', project='app', selected=[FEATURE],
                                 pending='  Export time  moves to 10:00.  ')
    world.service.create('Quiet note.', project='app', selected=[FEATURE])
    world.service.run(first['id'])
    assert pending_summaries(world.service, 'app') == [
        {'operation_id': first['id'], 'project': 'app', 'state': 'archived', 'pending': 'Export time moves to 10:00.'}]
    assert pending_summaries(world.service, 'other') == []
    block = pending_block(pending_summaries(world.service))
    assert block.startswith('Pending wiki updates (archived, applied by the next Dreaming pass')
    assert '- Export time moves to 10:00. [app]' in block and 'Quiet note' not in block
    assert pending_block([]) == ''
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 10:00.')]))
    assert apply_pending(world.service, executor)['applied'] == [first['id']]
    assert pending_summaries(world.service) == []


def test_a_note_captured_under_a_small_budget_is_edited_under_the_vault_budget(world):
    """The stored request keeps the budget of its day; a raised [editor] budget_tokens applies to it."""
    from kelmo.config import DEFAULT_EDITOR_BUDGET
    operation = world.service.create('Exports moved to 15:00.', project='app', selected=[FEATURE], budget_tokens=1)
    stored = world.service.journal.source_metadata(operation['id'])['metadata']['editor_request']['budget_tokens']
    assert stored == 1
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 15:00.')]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert executor.jobs[0].payload['budget']['max_tokens'] == DEFAULT_EDITOR_BUDGET


def test_a_pending_line_is_validated_at_capture(world):
    from kelmo.errors import KelmoError
    for bad in ('', '   ', 'x' * 241, 7):
        with pytest.raises(KelmoError, match='pending summary'):
            world.service.create('Note.', project='app', pending=bad)


def test_doubled_frontmatter_is_merged_and_an_empty_body_is_refused_at_capture(world):
    from kelmo.errors import KelmoError
    text = ('---\ndate: 2026-10-29\nproject: app\n---\n\n---\ndate: 2026-10-28\nstatus: shipped\n---\n\n'
            'Exports moved to 11:00.\n')
    operation = world.service.create(text, project='app', selected=[FEATURE])
    stored = world.service.source_path(world.service.journal.get(operation['id'])).read_text(encoding='utf-8')
    assert stored == '---\ndate: 2026-10-28\nproject: app\nstatus: shipped\n---\n\nExports moved to 11:00.\n'
    with pytest.raises(KelmoError, match='frontmatter and no text'):
        world.service.create('---\ndate: 2026-10-29\nproject: app\n---\n', project='app')
    plain = world.service.create('No frontmatter at all.', project='app', idempotency_key='plain')
    assert world.service.source_path(world.service.journal.get(plain['id'])).read_text(encoding='utf-8') \
        == 'No frontmatter at all.'


def attempts_charged(world, operation_id):
    from kelmo.creation import _parked
    return _parked(world.service.trace(operation_id))[1]


def test_a_pass_told_to_stop_ends_between_notes_and_holds_none(world):
    from kelmo.creation import apply_pending, pending_operations
    from kelmo.errors import Stopping
    nightly(world)
    first = world.service.create('Exports run at 13:00.', project='app', selected=[FEATURE])
    second = world.service.create('Exports run at 14:00.', project='app', selected=[FEATURE])
    world.service.run(first['id'])
    world.service.run(second['id'])
    executor = echo_export(world)
    stop = []

    def should_stop():
        # Told to stop once the first note is placed.
        return bool(stop) or (len(executor.jobs) == 1 and not stop.append(True))

    with pytest.raises(Stopping):
        apply_pending(world.service, executor, should_stop=should_stop)
    assert [job.payload['source']['text'] for job in executor.jobs] == ['Exports run at 13:00.']
    assert [operation['id'] for operation, _ in pending_operations(world.service)] == [second['id']]
    assert world.service.journal.conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 0


def test_a_failure_the_stop_causes_charges_no_attempt(world):
    from kelmo import stopping
    from kelmo.creation import apply_pending, pending_operations
    from kelmo.errors import KelmoError, Stopping
    nightly(world)
    operation = world.service.create('Exports run at 15:00.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])

    def cut(job):
        # The core is told to stop while the editor waits for the archive, which no longer answers.
        stopping.request()
        raise KelmoError('E_TRANSPORT', 'The channel to kelmod closed.')

    with pytest.raises(Stopping):
        apply_pending(world.service, Scripted(cut))
    assert world.service.journal.get(operation['id'])['state'] == 'archived'
    assert attempts_charged(world, operation['id']) == 0
    assert [op['id'] for op, _ in pending_operations(world.service)] == [operation['id']]


def test_a_fault_on_one_note_skips_that_note_and_the_pass_goes_on(world, capsys):
    from kelmo.creation import apply_pending, pending_operations
    nightly(world)
    broken = world.service.create('Exports run at 16:00.', project='app', selected=[FEATURE])
    healthy = world.service.create('Exports run at 17:00.', project='app', selected=[FEATURE])
    world.service.run(broken['id'])
    world.service.run(healthy['id'])

    def fault(job):
        raise ValueError('a bug in one note')

    def place(job):
        return world.propose(facts=[world.change('app.export', job.payload['source']['text'])])

    report = apply_pending(world.service, Scripted(fault, place))
    assert report['skipped'] == [{'operation_id': broken['id'], 'error_code': 'E_INTERNAL'}]
    assert report['applied'] == [healthy['id']]
    assert world.service.journal.get(broken['id'])['state'] == 'archived', 'its journal state is untouched'
    assert attempts_charged(world, broken['id']) == 0
    assert [op['id'] for op, _ in pending_operations(world.service)] == [broken['id']], 'tried on the next pass'
    assert 'ValueError: a bug in one note' in capsys.readouterr().err
    assert world.fact('app.export')['text'] == 'Exports run at 17:00.'


def test_dream_apply_cli_publishes_waiting_notes_and_run_applies_first(world, tmp_path, capsys):
    import sys
    from kelmo.cli import call
    nightly(world)
    operation = world.service.create('Exports moved to 12:00.', project='app', selected=[FEATURE])
    world.service.run(operation['id'])
    script, config = tmp_path / 'executor.py', tmp_path / 'executor.json'
    receipt = world.service.reconcile('Exports run at 12:00.', FEATURE)['search_id']
    revision = world.fact('app.export')['revision']
    script.write_text(
        'import json,sys\njob=json.load(sys.stdin)\n'
        "if job['task']=='dream':\n    print(json.dumps({'findings': []}))\n    raise SystemExit(0)\n"
        "print(json.dumps({'facts': [{'fact_key': 'app.export', 'target': %r, 'text': 'Exports run at 12:00.', "
        "'status': 'active', 'expected_revision': %r}], 'fact_checks': [{'fact_key': 'app.export', "
        "'verified_match': {'result': 'existing', 'fact_key': 'app.export', 'revision': %r}}]}))\n"
        % (FEATURE, revision, revision))
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))

    def invoke(*args):
        code = call(['--vault', str(world.root), 'dream', *map(str, args), '--json'])
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        return output['result']

    assert invoke('configure', '--executor-config', config, '--apply')['paused']
    assert invoke('apply')['state'] == 'paused'
    invoke('resume')
    result = invoke('apply')
    assert result['state'] == 'finished' and result['apply']['applied'] == [operation['id']]
    assert world.fact('app.export')['text'] == 'Exports run at 12:00.'
    assert receipt
    assert invoke('run')['apply'] == {'applied': [], 'parked': [], 'failed': [], 'deferred': [], 'skipped': [],
                                      'stalled': []}


def test_mcp_wait_stops_at_parked_and_awaiting_states(world):
    from kelmo.mcp import awaited

    class Tracing:
        def __init__(self, service):
            self.service = service

        def trace(self, operation_id):
            return self.service.trace(operation_id)

    nightly(world)
    operation = world.service.create('Exports moved to 13:00.', project='app', selected=[FEATURE])
    held = world.service.run(operation['id'])
    result = awaited(Tracing(world.service), {'operation': dict(held, terminal=False, state='queued')}, 5)
    assert result['operation']['state'] == 'archived' and result['last_event']['stage'] == 'awaiting_dreaming'


def test_mcp_wait_follows_a_note_that_met_a_held_lock(world, monkeypatch):
    from kelmo import mcp
    from kelmo.mcp import awaited
    monkeypatch.setattr(mcp, 'WAIT_POLL_SECONDS', 0)
    nightly(world)
    operation = world.service.create('Exports moved to 19:00.', project='app', selected=[FEATURE])
    world.service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_BUSY')
    calls = []

    class Requeuing:
        def trace(self, operation_id):
            # The worker requeues the note and archives it on the second look.
            calls.append(operation_id)
            if len(calls) == 2:
                world.service.retry(operation_id, reset_budget=False)
                world.service.run(operation_id)
            return world.service.trace(operation_id)

    busy = world.service.journal.get(operation['id'])
    result = awaited(Requeuing(), {'operation': busy}, 5)
    assert result['operation']['state'] == 'archived' and result['last_event']['stage'] == 'awaiting_dreaming'
    timed_out = awaited(type('Stuck', (), {'trace': lambda self, operation_id: {
        'operation': busy, 'events': []}})(), {'operation': busy}, 0.01)
    assert timed_out['waiting_turn'] is True and timed_out['wait_timed_out'] is True


def test_project_of_knows_top_level_projects():
    from kelmo.wiki import pages
    assert pages.project_of('life/health.md') == 'life'
    assert pages.project_of('life/life.md') == 'life'
    assert pages.project_of('projects/app/feature.md') == 'app'
    assert pages.project_of('person.md') is None and pages.project_of('decisions/x.md') is None
    assert pages.project_of('life/deeper/page.md') is None
    assert pages.project_directory('life') == 'life' and pages.project_directory('app') == 'projects/app'
    registry = {'pages': {'life/life.md': {'kind': 'work', 'layout': 'legacy'},
                          'projects/app/app.md': {'kind': 'hub', 'layout': 'project-map'},
                          'projects/app/other.md': {'kind': 'topic', 'layout': 'topic'}}}
    assert pages.project_entry(registry, 'life') == 'life/life.md'
    assert pages.project_entry(registry, 'app') == 'projects/app/app.md'
    assert pages.project_entry(registry, 'none') is None


def test_hook_start_appends_the_pending_block(world, monkeypatch):
    from kelmo.integration import hook
    nightly(world)
    operation = world.service.create('Exports moved to 14:00.', project='app', selected=[FEATURE],
                                     pending='Export time moves to 14:00.')
    world.service.run(operation['id'])
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[hot]\npages = []\n', encoding='utf-8')
    context = hook(world.service, 'start', {})['hookSpecificOutput']['additionalContext']
    assert context.rstrip().endswith('- Export time moves to 14:00. [app]')
    assert 'Pending wiki updates' in context


def test_a_priority_the_model_states_lands_on_the_opened_work_page(world):
    """The hub row shows the page priority, so the model's open_work carries it onto the page."""
    target = 'projects/app/export-rework.md'
    operation = world.service.create('Priority H1: rework the export, it is slow.', project='app')
    fact = dict(fact_key='app.export-rework', target=target, text='Rework the export pipeline.',
                status='in-progress', expected_revision=None)
    executor = Scripted(open_work('export-rework', 'Rework the export', priority='H1'),
                        lambda job: world.propose(facts=[fact]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert world.registry()['pages'][target]['priority'] == 'H1'
    from kelmo.wiki.hot_views import _open_pages, _row
    rows = [_row(item, 'full') for item in _open_pages(world.registry(), 'app')]
    assert rows == ['- **Rework the export** (H1) — [[projects/app/export-rework]]']


def test_an_invalid_priority_in_open_work_is_a_repair_not_a_failure(world):
    target = 'projects/app/export-rework.md'
    operation = world.service.create('Rework the export, it is slow.', project='app')
    fact = dict(fact_key='app.export-rework', target=target, text='Rework the export pipeline.',
                status='backlog', expected_revision=None)
    executor = Scripted(open_work('export-rework', 'Rework the export', priority='urgent'),
                        open_work('export-rework', 'Rework the export'),
                        lambda job: world.propose(facts=[fact]))
    result = world.service.run(operation['id'], executor=executor)
    assert result['state'] == 'applied', result.get('outcome')
    assert executor.jobs[1].payload['refusal']['code'] == 'E_PROPOSAL'
    assert 'H1, H2 or H3' in executor.jobs[1].payload['refusal']['reason']
    assert world.registry()['pages'][target]['priority'] == ''
