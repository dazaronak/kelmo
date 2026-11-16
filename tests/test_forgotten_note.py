"""A note forgotten while it waits for the editor is never published: `kelmo dream apply` passes it, the
worker never edits it, it leaves the startup pending block and the waiting count, and its archive record
keeps every byte."""
import json
import sys

import pytest

from test_editor_scoping import FEATURE, NOTES, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def nightly(world):
    from kelmo.config import Config
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[editor]\nmode = "nightly"\n', encoding='utf-8')
    world.service.config = Config.load(world.root)


def waiting(world, text, pending=None):
    operation = world.service.create(text, project='app', selected=[FEATURE], pending=pending)
    return world.service.run(operation['id'])


def logging_executor(world, tmp_path):
    """An executor config whose script logs each source it edits and proposes the export fact from its text."""
    script, config, log = tmp_path / 'executor.py', tmp_path / 'executor.json', tmp_path / 'edited.log'
    revision = world.fact('app.export')['revision']
    script.write_text(
        'import json,sys\njob=json.load(sys.stdin)\n'
        "if job['task']=='dream':\n    print(json.dumps({'findings': []}))\n    raise SystemExit(0)\n"
        "text=(job.get('source') or {}).get('text')\n"
        'if text is None:\n'
        "    print(json.dumps({'facts': []}))\n    raise SystemExit(0)\n"
        "open(%r,'a',encoding='utf-8').write(text.strip()+'\\n')\n"
        "print(json.dumps({'facts': [{'fact_key': 'app.export', 'target': %r, 'text': text.strip(), "
        "'status': 'active', 'expected_revision': %r}], 'fact_checks': [{'fact_key': 'app.export', "
        "'verified_match': {'result': 'existing', 'fact_key': 'app.export', 'revision': %r}}]}))\n"
        % (str(log), FEATURE, revision, revision))
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))
    return config, log


def cli(world, capsys, *args):
    from kelmo.cli import call
    code = call(['--vault', str(world.root), *map(str, args), '--json'])
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    return output['result']


def dream_apply(world, capsys, config):
    cli(world, capsys, 'dream', 'configure', '--executor-config', config, '--apply')
    cli(world, capsys, 'dream', 'resume')
    result = cli(world, capsys, 'dream', 'apply')
    assert result['state'] == 'finished', result
    return result['apply']


def edited(log):
    return log.read_text(encoding='utf-8').splitlines() if log.exists() else []


def test_dream_apply_never_takes_an_archived_note_that_was_forgotten(world, tmp_path, capsys):
    nightly(world)
    forgotten = waiting(world, 'Exports moved to 14:00.')
    kept = waiting(world, 'Exports moved to 15:00.')
    assert forgotten['state'] == 'archived' and kept['state'] == 'archived'
    world.service.forget(forgotten['archive_id'])
    config, log = logging_executor(world, tmp_path)

    report = dream_apply(world, capsys, config)

    assert forgotten['id'] not in report['applied'] + report['parked'] + report['failed']
    assert report['applied'] == [kept['id']]
    assert 'Exports moved to 14:00.' not in edited(log)
    assert world.fact('app.export')['text'] == 'Exports moved to 15:00.'


def test_dream_apply_never_takes_a_parked_note_that_was_forgotten(world, tmp_path, capsys):
    from kelmo.creation import apply_pending
    from kelmo.executor import ModelOutcome
    nightly(world)
    forgotten = waiting(world, 'Exports moved to 16:00.')
    broken = Scripted(ModelOutcome('failure', error_code='E_EXECUTOR', reason='no answer object'))
    assert apply_pending(world.service, broken)['parked'] == [forgotten['id']]
    assert world.service.trace(forgotten['id'])['operation']['state'] == 'parked'
    kept = waiting(world, 'Exports moved to 17:00.')
    world.service.forget(forgotten['archive_id'])
    config, log = logging_executor(world, tmp_path)

    report = dream_apply(world, capsys, config)

    assert forgotten['id'] not in report['applied'] + report['parked'] + report['failed']
    assert report['applied'] == [kept['id']]
    assert 'Exports moved to 16:00.' not in edited(log)
    assert world.fact('app.export')['text'] == 'Exports moved to 17:00.'


def test_a_forgotten_note_leaves_the_startup_pending_block_and_the_waiting_count(world, capsys):
    from kelmo.integration import hook
    nightly(world)
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[hot]\npages = []\n', encoding='utf-8')
    forgotten = waiting(world, 'Exports moved to 18:00.', pending='Export time moves to 18:00.')
    waiting(world, 'Exports moved to 19:00.', pending='Export time moves to 19:00.')

    def context():
        return hook(world.service, 'start', {})['hookSpecificOutput']['additionalContext']

    assert '- Export time moves to 18:00. [app]' in context()
    assert cli(world, capsys, 'dream', 'status')['waiting'] == {'app': 2}

    world.service.forget(forgotten['archive_id'])

    assert '- Export time moves to 18:00. [app]' not in context()
    assert '- Export time moves to 19:00. [app]' in context()
    assert cli(world, capsys, 'dream', 'status')['waiting'] == {'app': 1}


def test_forget_on_a_waiting_note_keeps_its_archive_record_and_history(world, tmp_path, capsys):
    nightly(world)
    forgotten = waiting(world, 'Exports moved to 20:00.')
    archive = world.service.archive_api.connection

    def stored():
        record = archive.execute('SELECT * FROM memories WHERE id=?', (forgotten['archive_id'],)).fetchone()
        versions = archive.execute('SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version',
                                   (forgotten['archive_id'],)).fetchall()
        return tuple(record), [tuple(row) for row in versions]

    before, records = stored(), world.service.status()['archive_records']
    world.service.forget(forgotten['archive_id'])
    config, _log = logging_executor(world, tmp_path)
    dream_apply(world, capsys, config)

    assert stored() == before
    assert world.service.status()['archive_records'] == records
    stages = [event['stage'] for event in world.service.trace(forgotten['id'])['events']]
    assert stages.index('archived') < stages.index('forgotten')
    assert 'applied' not in stages


def test_the_worker_never_publishes_a_forgotten_note(world):
    from kelmo.daemon import runnable_operations
    operation = world.service.create('Exports moved to 21:00.', project='app', selected=[FEATURE])
    held = world.service.run(operation['id'])
    assert held['state'] == 'archived', held
    world.service.forget(held['archive_id'])
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 21:00.')]))

    assert operation['id'] not in list(runnable_operations(world.service, ['executor']))
    result = world.service.run(operation['id'], executor=executor)

    assert result['state'] != 'applied' and executor.jobs == []
    assert world.fact('app.export')['text'] != 'Exports run at 21:00.'


def test_a_hide_that_fails_after_forget_began_still_keeps_the_note_unpublished(world, tmp_path, capsys,
                                                                                monkeypatch):
    from kelmo.errors import KelmoError
    nightly(world)
    forgotten = waiting(world, 'Exports moved to 22:00.')
    kept = waiting(world, 'Exports moved to 23:00.')

    def broken_hide(*args, **kwargs):
        raise KelmoError('E_STORE', 'Synthetic archive failure during hide.')

    monkeypatch.setattr(world.service.archive_api, 'hide', broken_hide)
    with pytest.raises(KelmoError):
        world.service.forget(forgotten['archive_id'])
    monkeypatch.undo()
    config, log = logging_executor(world, tmp_path)

    report = dream_apply(world, capsys, config)

    assert forgotten['id'] not in report['applied']
    assert 'Exports moved to 22:00.' not in edited(log)
    assert report['applied'] == [kept['id']]


def test_forget_on_a_note_being_edited_answers_busy_and_changes_nothing(world):
    from kelmo.errors import KelmoError
    nightly(world)
    note = waiting(world, 'Exports moved to 23:30.')
    token = world.service.journal.claim(note['id'], lease_seconds=60)
    assert token is not None
    try:
        with pytest.raises(KelmoError) as refused:
            world.service.forget(note['archive_id'])
    finally:
        world.service.journal.release(note['id'], token)

    assert (refused.value.code, refused.value.retryable) == ('E_BUSY', True)
    assert note['id'] in str(refused.value)
    assert world.service.archive_api.hidden_at(note['archive_id']) is None
    trace = world.service.trace(note['id'])
    assert trace['operation']['state'] == 'archived' and not trace['operation']['terminal']
    assert 'forgotten' not in [event['stage'] for event in trace['events']]


def test_the_trace_of_a_forgotten_waiting_note_names_its_record_and_time(world):
    nightly(world)
    note = waiting(world, 'Exports moved to 23:45.')
    result = world.service.forget(note['archive_id'])

    events = [event for event in world.service.trace(note['id'])['events'] if event['stage'] == 'forgotten']
    assert [event['details'] for event in events] == [
        {'record_id': note['archive_id'], 'forgotten_at': result['forgotten_at']}]


def test_forget_on_a_note_whose_publication_is_decided_answers_busy(world, monkeypatch):
    """The editor decided the note's publication and its commit failed: the next pass's recovery publishes it
    whatever forget does, so forget says so instead of claiming the note is withdrawn."""
    from kelmo.creation import apply_pending
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    nightly(world)
    note = waiting(world, 'Exports moved to 23:50.')
    executor = Scripted(lambda job: world.propose(facts=[world.change('app.export', 'Exports run at 23:50.')]))
    with monkeypatch.context() as broken:
        broken.setattr(engine, 'publish_semantic', lambda root, operation_id: (_ for _ in ()).throw(
            KelmoError('E_IO', 'the wiki repository could not be written')))
        try:
            apply_pending(world.service, executor)
        except KelmoError:
            pass
    assert note['id'] in world.service.editor.awaiting_publication()

    with pytest.raises(KelmoError) as refused:
        world.service.forget(note['archive_id'])

    assert (refused.value.code, refused.value.retryable) == ('E_BUSY', True)
    assert note['id'] in str(refused.value)
    assert world.service.archive_api.hidden_at(note['archive_id']) is None
    assert not world.service.trace(note['id'])['operation']['terminal']


def test_a_busy_forget_on_a_record_with_two_notes_withdraws_neither(world):
    """Two notes with the same bytes share one archive record; one of them is being edited. The refused forget
    leaves the other note waiting too, not withdrawn behind a call that failed."""
    from kelmo.errors import KelmoError
    nightly(world)
    first = waiting(world, 'Exports moved to 22:10.')
    second = world.service.create('Exports moved to 22:10.', project='app', selected=[FEATURE, NOTES])
    second = world.service.run(second['id'])
    assert second['id'] != first['id'] and second['archive_id'] == first['archive_id'], (first, second)
    token = world.service.journal.claim(second['id'], lease_seconds=60)
    assert token is not None
    try:
        with pytest.raises(KelmoError) as refused:
            world.service.forget(first['archive_id'])
    finally:
        world.service.journal.release(second['id'], token)

    assert (refused.value.code, refused.value.retryable) == ('E_BUSY', True)
    for note in (first, second):
        operation = world.service.trace(note['id'])['operation']
        assert operation['state'] == 'archived' and not operation['terminal'], operation
    assert world.service.archive_api.hidden_at(first['archive_id']) is None


def test_a_failed_note_that_was_forgotten_is_never_retried_into_the_wiki(world, tmp_path, capsys):
    """A note whose editor attempts ran out is `failed`; `kelmo queue retry` would queue it again. Forgotten,
    it is withdrawn: the retry is refused and no pass publishes it."""
    from kelmo.errors import KelmoError
    nightly(world)
    config, log = logging_executor(world, tmp_path)
    note = waiting(world, 'Exports moved to 22:20.')
    world.service.journal.stage(note['id'], 'failed', terminal=True, error_code='E_PROPOSAL')
    world.service.forget(note['archive_id'])

    with pytest.raises(KelmoError) as refused:
        world.service.retry(note['id'])
    report = dream_apply(world, capsys, config)

    assert refused.value.code == 'E_STATE'
    assert note['id'] not in report['applied'] and 'Exports moved to 22:20.' not in edited(log)
    assert world.fact('app.export')['text'] != 'Exports moved to 22:20.'
