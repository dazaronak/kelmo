"""A `kelmo executor` on another build than the worker's is caught before the model is asked anything.

`kelmo executor` builds the instructions and the answer schema from the install it runs from, and the worker
checks the answers against its own. On a server whose executor ran from a runtime of its own, the worker was
upgraded three times while the executor stayed where it was: the model never read the batch rules, a batch
published nothing and nothing reported why. From 0.3.1 the executor names the instructions it gives a model.
Work that would use other instructions than this build's does not start and charges nothing, on every path
that asks a model: an apply pass, a note published at once, the findings pass, a session job, a resolution
request. A command that names nothing (a `kelmo executor` older than 0.3.1 answers so) is let through, and
the owner is told at once and reminded every day until it does.
"""
import io
import json
import sys

import pytest

from kelmo import creation
from kelmo.alerts import ExecutorWatch
from kelmo.creation import (DREAM_TASKS, SESSION_TASKS, CommandExecutor, instructions_standing,
                            require_same_instructions)
from kelmo.errors import VaultFailure
from kelmo.executor import identity
from test_alerts import messages, recorder
from test_dreaming_batched_apply import BatchModel, capture, charged_parkings, editor_mode, facts_of, long_notes, pending_pass
from test_editor_scoping import FEATURE, build_world
from test_resolution import pending

pytest_plugins = ['test_fact_checks']
DAY = 86400


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


def saying(value, name='executor'):
    """The argv of an executor command that prints `value` whatever it is asked. `name` tells two commands
    with the same answer apart."""
    return [sys.executable, '-c', 'print(' + repr(json.dumps(value)) + ')', name]


def answering(value):
    return CommandExecutor(saying(value), 30)


def counting(tmp_path, value, name='calls'):
    """The argv of a command that answers `value` and adds one character to a file for every call."""
    script = ('import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); '
              'p.write_text((p.read_text() if p.exists() else "") + "x"); print(sys.argv[2])')
    return [sys.executable, '-c', script, str(tmp_path / name), json.dumps(value)], tmp_path / name


def failing(tmp_path):
    script = ('import pathlib, sys; p = pathlib.Path(sys.argv[1]); '
              'p.write_text((p.read_text() if p.exists() else "") + "x"); raise SystemExit(3)')
    return [sys.executable, '-c', script, str(tmp_path / 'calls')], tmp_path / 'calls'


def other_build(**changed):
    theirs = identity()
    theirs['version'] = '0.3.2'
    theirs['instructions'].update(changed)
    return theirs


class Settings:
    """What the check reads of a vault's configuration: the state directory and the `[alerts]` table."""

    def __init__(self, tmp_path, alerts=True, coverage=False):
        self.state = tmp_path / 'state'
        self.coverage = coverage
        self.alerts = {'command': recorder(tmp_path), 'label': 'Kelmo'} if alerts else None


def forget():
    """A new process: what this one was told is gone, what the state directory holds stays."""
    creation._STANDINGS.clear()
    creation._UNASKED.clear()


def standings(settings):
    return sorted(entry['standing'] for entry in ExecutorWatch.for_config(settings).entries().values())


def the_entry(settings):
    (entry,) = ExecutorWatch.for_config(settings).entries().values()
    return entry


def test_the_identity_names_the_version_and_a_digest_for_every_task():
    import kelmo
    ours = identity()
    assert ours['version'] == kelmo.__version__ and ours == identity()
    tasks = ours['instructions']
    assert set(tasks) == {'edit_pages', 'edit_pages_covered', 'extract', 'reconcile_session', 'dream', 'resolve'}
    assert set(creation.EDITOR_TASKS + DREAM_TASKS + SESSION_TASKS + creation.RESOLVE_TASKS) == set(tasks)
    assert all(len(digest) == 64 for digest in tasks.values())
    assert tasks['edit_pages'] != tasks['edit_pages_covered'], 'the accounting is other instructions'


def test_the_identity_is_taken_from_what_a_job_is_sent_and_is_the_same_in_another_process():
    import hashlib
    import os
    import subprocess
    from kelmo import executor

    def sent(payload):
        """The instructions and schema `resolve` hands a custom command for `payload`, by digest."""
        seen = {}

        def provider(command, request, timeout):
            seen.update(request)
            return {'kind': 'failure', 'error_code': 'E_UNRESOLVED', 'reason': 'not asked'}

        original, executor.provider_execute = executor.provider_execute, provider
        try:
            executor.resolve('custom', None, payload, command=[sys.executable])
        except Exception:  # noqa: BLE001 - only the request matters here
            pass
        finally:
            executor.provider_execute = original
        return hashlib.sha256((seen['instructions'] + '\0'
                               + json.dumps(seen['output_schema'], sort_keys=True)).encode()).hexdigest()

    tasks = identity()['instructions']
    assert sent({'task': 'edit_pages', 'source': {'text': 'a note'}}) == tasks['edit_pages']
    assert sent({'task': 'edit_pages', 'sources': [{'text': 'a', 'units': [[1, 'a']]}]}) == tasks['edit_pages_covered']
    assert sent({'task': 'extract_checkpoint'}) == tasks['extract'] == sent({'task': 'extract_final'})
    assert sent({'task': 'reconcile_session'}) == tasks['reconcile_session']
    assert sent({'task': 'dream'}) == tasks['dream']
    assert sent({'task': 'resolve_facts'}) == tasks['resolve'], 'the task a resolution request carries'
    # Another process with another hash seed names the same digests: nothing in them depends on set order.
    for seed in ('1', '2'):
        done = subprocess.run([sys.executable, '-c', 'import json; from kelmo.executor import identity; '
                               'print(json.dumps(identity()))'], capture_output=True, text=True,
                              env={**os.environ, 'PYTHONHASHSEED': seed}, check=True)
        assert json.loads(done.stdout) == identity()


def test_the_executor_command_identifies_itself_without_calling_a_provider(monkeypatch, capsys):
    from kelmo.cli import main
    monkeypatch.setattr('sys.stdin', io.StringIO('{"task": "identify"}'))
    never = json.dumps([sys.executable, '-c', 'raise SystemExit("the provider was called")'])
    assert main(['executor', '--provider', 'custom', '--command-json', never]) == 0
    assert json.loads(capsys.readouterr().out) == {'kelmo': identity()}


def test_an_executor_with_the_same_instructions_passes_whatever_its_version(tmp_path, capsys):
    settings = Settings(tmp_path)
    assert require_same_instructions(answering({'kelmo': identity()}), settings) == 'same'
    assert require_same_instructions(answering({'kelmo': other_build()})) == 'same'
    assert require_same_instructions(object()) is None and require_same_instructions(None) is None
    assert capsys.readouterr().err == '' and messages(tmp_path) == []
    assert standings(settings) == ['same']


@pytest.mark.parametrize('changed, tasks, said', [
    ({'edit_pages': '0' * 64}, None, 'for edit_pages. Its answers'),
    ({'edit_pages': '0' * 64, 'dream': None}, None, 'for dream, edit_pages. Its answers'),
    ({'edit_pages': '0' * 64, 'dream': None}, DREAM_TASKS, 'for dream. Its answers'),
])
def test_an_executor_that_names_other_instructions_is_refused_with_what_differs(changed, tasks, said):
    with pytest.raises(VaultFailure) as refused:
        instructions_standing(saying({'kelmo': other_build(**changed)}), 30, None, tasks)
    text = str(refused.value)
    assert refused.value.code == 'E_EXECUTOR' and said in text, text
    assert 'runs Kelmo 0.3.2' in text and 'nothing was sent to the model and nothing was charged' in text


def test_a_difference_in_instructions_the_caller_does_not_use_is_no_refusal(tmp_path):
    """Session extraction changed between the two builds and the editor's instructions did not: notes are
    edited, session jobs wait. The record says the builds differ either way."""
    settings = Settings(tmp_path)
    argv = saying({'kelmo': other_build(extract='0' * 64)})
    assert instructions_standing(argv, 30, settings, creation.EDITOR_TASKS) == 'same'
    assert instructions_standing(argv, 30, settings, DREAM_TASKS) == 'same'
    assert messages(tmp_path) == []
    with pytest.raises(VaultFailure, match='for extract. Its answers'):
        instructions_standing(argv, 30, settings, SESSION_TASKS, 'session jobs')
    entry = the_entry(settings)
    assert (entry['standing'], entry['executor_version']) == ('differs', '0.3.2')
    assert entry['reason'] == 'other instructions for extract' and list(entry['refused']) == ['session jobs']
    (told,) = messages(tmp_path)
    assert told.startswith('Kelmo: session jobs wait for the executor command') and 'are not charged' in told


@pytest.mark.parametrize('value', [
    # An executor older than the question answers it as a job.
    {'unresolved': True}, {'kelmo': 'yes'}, {'facts': []}, {'outcome': 'quota_wait', 'reset_at': 1},
    # An answer that names none of this build's tasks names nothing either.
    {'kelmo': {'instructions': {}}}, {'kelmo': {'version': '9', 'instructions': {'edit_pages': 7, 'other': 'x'}}}])
def test_a_command_that_names_no_instructions_is_let_through_and_the_owner_is_told(tmp_path, capsys, value):
    settings = Settings(tmp_path)
    argv, calls = counting(tmp_path, value)
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    assert 'does not name the instructions it gives the model' in capsys.readouterr().err
    told = messages(tmp_path)
    assert len(told) == 1 and 'does not name the instructions it gives the model' in told[0]
    assert 'older than 0.3.1' in told[0] and 'repeated once a day' in told[0]
    # The same process and a later one within the hour: the command is not run again (on an older Kelmo the
    # question costs a model call) and the owner is not told again that day.
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    forget()
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    assert calls.read_text() == 'x' and len(messages(tmp_path)) == 1
    assert standings(settings) == ['unidentified']


def test_an_unnamed_executor_is_told_again_every_day_until_it_names_its_instructions(tmp_path):
    watch = ExecutorWatch.for_config(Settings(tmp_path))
    start = 1_800_000_000
    for later in (0, 3600, DAY - 1, DAY, DAY + 3600, 2 * DAY):
        watch.unidentified('c1', 'wrapper', 'mine', 'it answered the question as a job', now=start + later)
    told = messages(tmp_path)
    assert len(told) == 3, told
    assert told[1].endswith('Day 2.') and told[2].endswith('Day 3.')
    # A clock set back does not silence the reminder.
    watch.unidentified('c1', 'wrapper', 'mine', 'it answered the question as a job', now=start - DAY)
    assert len(messages(tmp_path)) == 4
    watch.same('c1', 'wrapper', 'mine', '0.3.1', now=start + 3 * DAY)
    assert messages(tmp_path)[-1] == 'Kelmo: the executor command wrapper now names the instructions it gives the model.'
    watch.same('c1', 'wrapper', 'mine', '0.3.1', now=start + 4 * DAY)
    assert len(messages(tmp_path)) == 5, 'said once'


def test_with_no_alert_command_the_reminder_is_one_worker_log_line_a_day(tmp_path, capsys):
    watch = ExecutorWatch.for_config(Settings(tmp_path, alerts=False))
    for later in (0, 60, DAY):
        watch.unidentified('c1', 'wrapper', 'mine', 'it answered the question as a job', now=1_800_000_000 + later)
    assert capsys.readouterr().err.count('kelmo worker: alert: Kelmo: the executor command wrapper does not name') == 2


def test_a_command_asked_again_that_now_names_this_build_ends_the_record(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    old, _ = counting(tmp_path, {'unresolved': True})
    assert instructions_standing(old, 30, settings) == 'unidentified'
    forget()
    # An hour later the same command line runs an upgraded executor.
    monkeypatch.setattr(creation, 'UNIDENTIFIED_SECONDS', 0)
    monkeypatch.setattr(creation, 'execute', lambda argv, payload, timeout: {'kelmo': identity()})
    assert instructions_standing(old, 30, settings) == 'same'
    assert standings(settings) == ['same']
    assert 'now names the instructions it gives the model' in messages(tmp_path)[-1]


def test_a_new_build_of_this_kelmo_tells_the_owner_of_an_unnamed_executor_at_once(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    argv = saying({'unresolved': True})
    instructions_standing(argv, 30, settings)
    forget()
    from kelmo import executor
    upgraded = identity()
    upgraded['instructions']['edit_pages'] = '1' * 64
    monkeypatch.setattr(executor, 'identity', lambda: upgraded)
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    assert len(messages(tmp_path)) == 2, 'the worker changed and the executor may not have: said at once'


def test_a_command_that_says_its_instructions_are_its_own_is_not_checked_and_not_told(tmp_path, capsys):
    settings = Settings(tmp_path)
    argv, calls = counting(tmp_path, {'kelmo': {'instructions': 'own'}})
    assert instructions_standing(argv, 30, settings) == 'own'
    forget()
    assert instructions_standing(argv, 30, settings, SESSION_TASKS) == 'own'
    assert calls.read_text() == 'x' and messages(tmp_path) == [] and capsys.readouterr().err == ''
    assert standings(settings) == ['own']


def test_a_question_that_fails_is_put_again_later_and_after_three_failures_the_owner_is_told(tmp_path, capsys):
    settings = Settings(tmp_path)
    argv, calls = failing(tmp_path)
    assert instructions_standing(argv, 30, settings) == 'unasked'
    assert 'could not be asked which instructions it gives the model: the question failed (E_EXECUTOR' \
        in capsys.readouterr().err
    assert instructions_standing(argv, 30, settings) == 'unasked'
    assert calls.read_text() == 'x', 'a worker that looks every few seconds does not start it on every look'
    assert messages(tmp_path) == [] and the_entry(settings)['failures'] == 1
    forget()
    assert instructions_standing(argv, 30, settings) == 'unasked'
    forget()
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    (told,) = messages(tmp_path)
    assert 'the question failed (E_EXECUTOR' in told and '3 times in a row' in told
    assert calls.read_text() == 'xxx' and the_entry(settings)['standing'] == 'unidentified'
    # In the same process the failing command is left alone for a while and the answer stands.
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    assert calls.read_text() == 'xxx'


def test_a_question_that_fails_does_not_lift_a_difference_already_known(tmp_path, monkeypatch):
    """The executor named other instructions and then stops answering the question (a timeout, a crash): it
    was not repaired, so nothing is sent to it, in this process and in a new one, however often it fails."""
    settings = Settings(tmp_path)
    argv = saying({'kelmo': other_build(edit_pages='0' * 64, extract='0' * 64)})
    with pytest.raises(VaultFailure):
        instructions_standing(argv, 30, settings, creation.EDITOR_TASKS)

    def broken(command, payload, timeout):
        from kelmo.errors import KelmoError
        raise KelmoError('E_EXECUTOR', 'Executor timed out.')

    monkeypatch.setattr(creation, 'execute', broken)
    clock = creation.time.monotonic()
    for process in range(5):
        if process % 2:
            forget()   # a new process knows the difference only from the record
        else:          # the same process a long while later: its memory of the answer has expired
            creation._UNASKED.clear()
            monkeypatch.setattr(creation.time, 'monotonic', lambda: clock + 4000 * (process + 1))
        with pytest.raises(VaultFailure, match='for edit_pages. Its answers'):
            instructions_standing(argv, 30, settings, creation.EDITOR_TASKS)
        with pytest.raises(VaultFailure, match='for extract. Its answers'):
            instructions_standing(argv, 30, settings, SESSION_TASKS, 'session jobs')
        # Work whose instructions did not differ is not held by the record.
        assert instructions_standing(argv, 30, settings, DREAM_TASKS) in ('unasked', 'same')
    entry = the_entry(settings)
    assert entry['standing'] == 'differs' and entry['differing'] == ['edit_pages', 'extract']
    assert sorted(entry['refused']) == ['notes', 'session jobs'] and entry['failures'] >= 3
    assert not any('does not name the instructions' in told for told in messages(tmp_path))


def test_a_failure_after_an_answer_counts_from_one_again(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    argv, _ = failing(tmp_path)
    for _ in range(3):
        forget()
        instructions_standing(argv, 30, settings)
    assert the_entry(settings)['failures'] == 3
    forget()
    monkeypatch.setattr(creation, 'UNIDENTIFIED_SECONDS', 0)
    real = creation.execute
    monkeypatch.setattr(creation, 'execute', lambda command, payload, timeout: {'unresolved': True})
    assert instructions_standing(argv, 30, settings) == 'unidentified'
    assert the_entry(settings)['failures'] == 0, 'an answer, even one that names nothing, ends the row'
    monkeypatch.setattr(creation, 'execute', real)
    forget()
    assert instructions_standing(argv, 30, settings) == 'unasked'
    assert the_entry(settings)['failures'] == 1


def test_a_refused_command_that_then_says_its_instructions_are_its_own_ends_the_refusal_aloud(tmp_path):
    watch = ExecutorWatch.for_config(Settings(tmp_path))
    watch.differs('c1', 'adapter', 'mine', '0.3.2', 'other instructions for dream', ['dream'], now=1_800_000_000)
    watch.refused('c1', 'adapter', 'the findings pass', 'The executor command runs Kelmo 0.3.2.', now=1_800_000_000)
    watch.own('c1', 'adapter', 'mine', now=1_800_000_060)
    assert messages(tmp_path)[-1].endswith('now says its instructions are its own; the work that waited for it '
                                           'resumes.')


def test_a_refusal_remembered_by_one_process_says_nothing_over_a_newer_answer(tmp_path):
    """Another process asked after the repair and recorded `same`; this one still remembers the difference
    for the rest of its minute. It refuses once more and neither rewrites the record nor tells the owner."""
    watch = ExecutorWatch.for_config(Settings(tmp_path))
    watch.same('c1', 'wrapper', 'mine', '0.3.1', now=1_800_000_000)
    watch.refused('c1', 'wrapper', 'notes', 'The executor command runs Kelmo 0.3.2.', now=1_800_000_030)
    assert messages(tmp_path) == [] and watch.entry('c1')['standing'] == 'same'


def test_an_equal_answer_is_believed_for_a_while_and_a_refusal_is_repeated_from_memory(tmp_path, monkeypatch):
    same, calls = counting(tmp_path, {'kelmo': identity()})
    assert [instructions_standing(same) for _ in range(3)] == ['same'] * 3
    assert calls.read_text() == 'x'
    other, calls = counting(tmp_path, {'kelmo': other_build(dream='0' * 64)}, 'other')
    for _ in range(3):
        with pytest.raises(VaultFailure):
            instructions_standing(other)
    assert calls.read_text() == 'x'
    # A minute later the refused command is asked again: the repair is a person's and is seen soon.
    clock = creation.time.monotonic()
    monkeypatch.setattr(creation.time, 'monotonic', lambda: clock + 61)
    with pytest.raises(VaultFailure):
        instructions_standing(other)
    assert calls.read_text() == 'xx'


def test_each_command_keeps_its_own_record_and_none_answers_for_another(tmp_path):
    """The worker's command differs for the editor and the one sessions use is this build's: the second
    neither erases the first's record nor tells the owner that the trouble is over."""
    settings = Settings(tmp_path)
    worker = saying({'kelmo': other_build(edit_pages='0' * 64)}, 'worker')
    sessions = saying({'kelmo': identity()}, 'sessions')
    with pytest.raises(VaultFailure):
        instructions_standing(worker, 30, settings, creation.EDITOR_TASKS)
    assert instructions_standing(sessions, 30, settings, SESSION_TASKS) == 'same'
    forget()
    assert instructions_standing(sessions, 30, settings, SESSION_TASKS) == 'same'
    assert standings(settings) == ['differs', 'same']
    told = messages(tmp_path)
    assert len(told) == 1 and told[0].startswith('Kelmo: notes wait for the executor command')


def test_a_refusal_is_told_once_a_day_for_each_kind_of_work_and_once_when_it_is_over(tmp_path):
    watch = ExecutorWatch.for_config(Settings(tmp_path))
    start = 1_800_000_000
    watch.differs('c1', 'wrapper', 'mine', '0.3.2', 'other instructions for edit_pages', ['edit_pages'], now=start)
    for later in (0, 60, 3600):
        watch.refused('c1', 'wrapper', 'notes', 'The executor command runs Kelmo 0.3.2.', now=start + later)
        # The command is asked again every minute and still differs: the reminders are kept.
        watch.differs('c1', 'wrapper', 'mine', '0.3.2', 'other instructions for edit_pages', ['edit_pages'],
                      now=start + later)
    watch.refused('c1', 'wrapper', 'session jobs', 'The executor command runs Kelmo 0.3.2.', now=start + 3600)
    assert len(messages(tmp_path)) == 2
    watch.refused('c1', 'wrapper', 'notes', 'The executor command runs Kelmo 0.3.2.', now=start + DAY)
    assert len(messages(tmp_path)) == 3
    watch.same('c1', 'wrapper', 'mine', '0.3.1', now=start + DAY + 60)
    assert messages(tmp_path)[-1].endswith("gives the model this build's instructions again; the work that "
                                           'waited for it resumes.')


def test_a_refusal_is_told_even_when_the_record_cannot_be_kept(tmp_path):
    settings = Settings(tmp_path)
    settings.state.mkdir()
    (settings.state / 'executor-identity.json').mkdir()   # nothing can be written over a directory
    with pytest.raises(VaultFailure):
        instructions_standing(saying({'kelmo': other_build(extract='0' * 64)}), 30, settings, SESSION_TASKS,
                              'session jobs')
    assert len(messages(tmp_path)) == 1 and ExecutorWatch.for_config(settings).entries() == {}
    # The worker looks again a few seconds later: the refusal stands and the owner is not told again.
    for _ in range(3):
        with pytest.raises(VaultFailure):
            instructions_standing(saying({'kelmo': other_build(extract='0' * 64)}), 30, settings, SESSION_TASKS,
                                  'session jobs')
    assert len(messages(tmp_path)) == 1


def test_a_record_of_another_shape_is_no_record(tmp_path):
    settings = Settings(tmp_path)
    settings.state.mkdir()
    for held in ('[1, 2]', '7', '{"commands": []}', '{"commands": {"c": {"standing": "tomorrow"}}}', '{'):
        (settings.state / 'executor-identity.json').write_text(held)
        assert ExecutorWatch.for_config(settings).entries() == {}
    assert instructions_standing(saying({'kelmo': identity()}), 30, settings) == 'same'
    assert standings(settings) == ['same']


def test_a_pass_stops_before_its_first_note_and_charges_none(world, capsys):
    """The pass meets a `kelmo executor` of another build: nothing is published, no note is parked or
    charged, and the report names the cause the way it names a vault that cannot take a publication."""
    editor_mode(world, batch_apply=False)
    ids = capture(world, long_notes(2, pads=2))
    argv, calls = counting(world.root.parent, {'kelmo': other_build(edit_pages='0' * 64)})
    report = pending_pass(world, CommandExecutor(argv, 30))
    assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
    assert report['blocked']['error_code'] == 'E_EXECUTOR' and report['blocked']['operation_id'] == ids[0]
    assert 'runs Kelmo 0.3.2' in report['blocked']['reason']
    assert facts_of(world) == {} and [charged_parkings(world, note) for note in ids] == [([], 0)] * 2
    assert calls.read_text() == 'x', 'asked once and given no note'
    watch = ExecutorWatch.for_service(world.service)
    assert list(the_entry(world.service.config)['refused']) == ['notes']
    assert 'alert: Kelmo: notes wait for the executor command' in capsys.readouterr().err
    assert not (world.service.config.state / 'vault-outage.json').exists(), 'the vault itself takes publications'
    # The executor is brought to this build; a minute later the same command is asked again and the same
    # notes are published by it.
    clock = creation.time.monotonic()
    creation._STANDINGS.update({key: (clock - 61,) + held[1:] for key, held in creation._STANDINGS.items()})

    class Repaired(CommandExecutor):
        def execute(self, job):
            return BatchModel(world).execute(job)

    original, creation.execute = creation.execute, lambda command, payload, timeout: {'kelmo': identity()}
    try:
        assert pending_pass(world, Repaired(argv, 30))['applied'] == ids
    finally:
        creation.execute = original
    assert watch.entries() and the_entry(world.service.config)['standing'] == 'same'
    assert "gives the model this build's instructions again" in capsys.readouterr().err


def test_a_batch_pass_stops_the_same_way(world):
    editor_mode(world, coverage=True)
    ids = capture(world, long_notes(3, pads=2))
    report = pending_pass(world, answering({'kelmo': other_build(edit_pages_covered='0' * 64)}))
    assert (report['applied'], report['parked'], report['failed']) == ([], [], []), report
    assert report['blocked']['error_code'] == 'E_EXECUTOR'
    assert 'for edit_pages_covered. Its answers' in report['blocked']['reason']
    assert [charged_parkings(world, note) for note in ids] == [([], 0)] * 3


def test_the_accounting_instructions_are_compared_only_where_coverage_sends_them(world, tmp_path):
    """Coverage is off, the default: no job carries the accounting instructions, so an executor that differs
    only in them edits the notes."""
    editor_mode(world, batch_apply=False)
    ids = capture(world, long_notes(1, pads=2))

    class OtherAccounting(CommandExecutor):
        def execute(self, job):
            return BatchModel(world).execute(job)

    report = pending_pass(world, OtherAccounting(saying({'kelmo': other_build(edit_pages_covered='0' * 64)}), 30))
    assert report['applied'] == ids, report
    off, on = Settings(tmp_path), Settings(tmp_path, coverage=True)
    command = answering({'kelmo': other_build(edit_pages_covered='0' * 64)})
    assert require_same_instructions(command, off) == 'same'
    with pytest.raises(VaultFailure, match='for edit_pages_covered. Its answers'):
        require_same_instructions(command, on)


def test_a_known_difference_is_asked_about_again_within_a_minute_however_often_the_question_failed(tmp_path,
                                                                                                    monkeypatch):
    settings = Settings(tmp_path)
    argv = saying({'kelmo': other_build(edit_pages='0' * 64)})
    with pytest.raises(VaultFailure):
        instructions_standing(argv, 30, settings, creation.EDITOR_TASKS)
    calls = []

    def answer(command, payload, timeout):
        from kelmo.errors import KelmoError
        calls.append(1)
        if len(calls) <= 6:
            raise KelmoError('E_EXECUTOR', 'Executor timed out.')
        return {'kelmo': identity()}   # repaired

    monkeypatch.setattr(creation, 'execute', answer)
    clock = creation.time.monotonic()
    for minute in range(1, 7):
        monkeypatch.setattr(creation.time, 'monotonic', lambda: clock + 61 * minute)
        with pytest.raises(VaultFailure):
            instructions_standing(argv, 30, settings, creation.EDITOR_TASKS)
    assert len(calls) == 6, 'one question a minute, not one an hour'
    monkeypatch.setattr(creation.time, 'monotonic', lambda: clock + 61 * 7)
    assert instructions_standing(argv, 30, settings, creation.EDITOR_TASKS) == 'same'
    assert the_entry(settings)['standing'] == 'same'


def test_a_pass_goes_on_with_a_command_that_names_no_instructions(world, capsys):
    """A `kelmo executor` older than the question answers it as a job and then answers the jobs."""
    editor_mode(world, batch_apply=False)
    ids = capture(world, long_notes(1, pads=2))

    class Older(CommandExecutor):
        def execute(self, job):
            return BatchModel(world).execute(job)

    report = pending_pass(world, Older(saying({'unresolved': True}), 30))
    assert report['applied'] == ids, report
    assert 'does not name the instructions it gives the model' in capsys.readouterr().err
    assert the_entry(world.service.config)['standing'] == 'unidentified'


def test_a_note_published_at_once_is_not_edited_by_an_executor_of_another_build(world):
    """`immediate` mode, the default: the worker's tick edits a note as soon as it is archived. With an
    executor of another build it edits none, charges none, starts only the first of them and still does the
    work that needs no model."""
    from kelmo.daemon import tick
    ids = []
    for text in long_notes(3, pads=2):
        operation = world.service.create(text, project='app', selected=[FEATURE])
        world.service.run(operation['id'])
        ids.append(operation['id'])
    # A note captured and not yet archived: its archive needs no model.
    fresh = world.service.create('Exports run at 16:00 after the audit of that week.', project='app',
                                 selected=[FEATURE])
    argv, calls = counting(world.root.parent, {'kelmo': other_build(edit_pages='0' * 64)})
    result = tick(world.service, argv)
    assert [entry for entry in result['skipped'] if entry.get('operation_id') in ids] == [
        {'operation_id': note, 'error_code': 'E_EXECUTOR'} for note in ids], result
    assert facts_of(world) == {} and [charged_parkings(world, note) for note in ids] == [([], 0)] * 3
    assert calls.read_text() == 'x', 'asked once, and never given a note'
    assert world.service.journal.get(fresh['id'])['state'] == 'archived', 'the new note was still archived'
    assert not (world.service.config.state / 'vault-outage.json').exists()
    assert list(the_entry(world.service.config)['refused']) == ['notes']


def test_the_findings_pass_is_not_given_to_an_executor_of_another_build(world, tmp_path):
    """No note waits, so the findings pass is the first thing that would ask the model. It does not run, the
    owner is told, and the answer is not a day's run done, so the daemon tries again."""
    from kelmo.core.jobs import DREAM_DAY_STATES
    from kelmo.dreaming_cli import run_dream, status
    assert 'executors' not in status(world.root), 'no command was asked yet'
    config = tmp_path / 'editor.json'
    config.write_text(json.dumps({'argv': saying({'kelmo': other_build(dream='0' * 64)}), 'timeout_seconds': 30}),
                      encoding='utf-8')
    world.service.dreaming._set_state('settings', {'executor_config': str(config), 'max_sources': 10,
                                                   'max_pages': 10, 'budget_tokens': 100000})
    world.service.dreaming._set_state('paused', False)
    vars(world.service).pop('_dreaming', None)
    result = run_dream(world.service, 'run')
    assert result['state'] == 'blocked' and result['state'] not in DREAM_DAY_STATES
    assert result['blocked']['error_code'] == 'E_EXECUTOR' and 'for dream. Its answers' in result['blocked']['reason']
    assert list(the_entry(world.service.config)['refused']) == ['the findings pass']
    (said,) = status(world.root)['executors']
    assert (said['standing'], said['executor_version'], said['name']) == ('differs', '0.3.2', 'python' + said['name'][6:])


def test_a_session_job_waits_uncharged_for_an_executor_of_another_build(tmp_path):
    from unittest.mock import patch
    from kelmo.config import Config
    from kelmo.daemon import tick
    from kelmo.service import Service
    from kelmo.sessions import Sessions
    from test_session_worker import prepared
    root, job_id = prepared(tmp_path)
    argv = saying({'kelmo': other_build(extract='0' * 64)})
    with Service(root) as service, patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        first = tick(service, argv)
        second = tick(service, argv)
    assert first['sessions'] == {'jobs': [], 'executor_ready': False, 'error_code': 'E_EXECUTOR'}, first
    assert 'failed_attempts' not in first and 'failed_attempts' not in second
    sessions = Sessions(Config.load(root))
    try:
        assert sessions.job(job_id)['attempts'] == 0, 'the job was never started'
    finally:
        sessions.close()
    entry = the_entry(Config.load(root))
    assert entry['standing'] == 'differs' and list(entry['refused']) == ['session jobs']


def test_a_resolution_request_goes_back_unanswered_and_uncharged(service):
    from kelmo.daemon import tick
    pending(service)
    before = dict(service.journal.conn.execute('SELECT state, attempts FROM resolutions').fetchone())
    result = tick(service, saying({'kelmo': other_build(resolve='0' * 64)}))
    assert result['resolutions'] == 0, result
    assert [entry['error_code'] for entry in result['skipped'] if 'resolution_id' in entry] == ['E_EXECUTOR']
    after = dict(service.journal.conn.execute('SELECT state, attempts FROM resolutions').fetchone())
    assert after == before == {'state': 'pending', 'attempts': 0}
    assert list(the_entry(service.config)['refused']) == ['resolution requests']
