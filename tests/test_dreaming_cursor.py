"""The findings pass of `kelmo dream run` follows the journal's archive events: a capture that rests never holds it
back, a late archive is read next, nothing is read twice, and a 0.3.1 cursor goes on from where it stood."""
import json
import sys

import pytest

from test_editor_scoping import build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


@pytest.fixture
def dream(world, tmp_path, capsys):
    """Runs `kelmo dream run --json` with an executor that logs each findings pass's evidence; returns
    (result, archive ids read by that run's findings pass or None when it read nothing)."""
    from kelmo.cli import call
    script, config, log = tmp_path / 'executor.py', tmp_path / 'executor.json', tmp_path / 'read.log'
    script.write_text(
        'import json,sys\njob=json.load(sys.stdin)\n'
        "if job['task']=='dream':\n"
        "    open(%r,'a',encoding='utf-8').write(json.dumps([e['archive_id'] for e in job.get('evidence',[])])+'\\n')\n"
        "    print(json.dumps({'findings': []}))\n    raise SystemExit(0)\n"
        "print(json.dumps({'facts': []}))\n" % str(log))
    config.write_text(json.dumps({'argv': [sys.executable, str(script)], 'timeout_seconds': 30}))

    def cli(*args):
        code = call(['--vault', str(world.root), 'dream', *map(str, args), '--json'])
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        return output['result']

    cli('configure', '--executor-config', config, '--apply')
    cli('resume')

    def run():
        before = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
        result = cli('run')
        after = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
        read = json.loads(after[-1]) if len(after) > len(before) else None
        return result, read

    def drain():
        for _ in range(10):
            result, _read = run()
            if result['state'] == 'idle':
                return
        raise AssertionError('the seeded evidence never drained')

    run.drain = drain
    return run


def resting(world, text, key):
    """A collector's capture: it rests in `captured`, never scheduled for the archive."""
    operation = world.service.capture(text + '\n', key + '.md', 'collector', idempotency_key=key)
    assert world.service.trace(operation['id'])['operation']['state'] == 'captured'
    return operation['id']


def waiting(world, text, key):
    """A capture queued for the archive and not run yet: it is archived when the test runs it."""
    operation = world.service.capture(text + '\n', key + '.md', 'implementer', idempotency_key=key)
    assert world.service.trace(operation['id'])['operation']['state'] == 'queued'
    return operation['id']


def archive_id(world, operation_id):
    return world.service.journal.get(operation_id)['archive_id']


def test_a_resting_capture_does_not_hold_back_newer_evidence(world, dream):
    dream.drain()
    resting(world, 'A capture nobody submits for archival.', 'resting-one')
    newer, _ = world.archive('Newer evidence: exports moved to 18:00.', key='newer-one')

    result, read = dream()

    assert result['state'] != 'idle', result
    assert read is not None and archive_id(world, newer) in read


def test_a_late_archive_is_read_next_and_nothing_is_read_twice(world, dream):
    dream.drain()
    late = waiting(world, 'Evidence archived late: the export owner changed.', 'late-one')
    newer, _ = world.archive('Evidence archived first: exports moved to 19:00.', key='newer-two')

    first, read_first = dream()
    assert first['state'] != 'idle', first
    assert read_first == [archive_id(world, newer)]

    assert world.service.run(late)['durable_stage'] == 'archived'
    second, read_second = dream()
    assert second['state'] != 'idle', second
    assert read_second == [archive_id(world, late)]

    third, read_third = dream()
    assert third['state'] == 'idle' and read_third is None, third


def test_a_cursor_written_by_0_3_1_goes_on_from_where_it_stood(world, dream):
    dream.drain()
    first = waiting(world, 'Evidence A, archived after the others.', 'a-evidence')
    b, _ = world.archive('Evidence B: the export runs nightly.', key='b-evidence')
    d, _ = world.archive('Evidence D: the export writes CSV.', key='d-evidence')
    assert world.service.run(first)['durable_stage'] == 'archived'
    b_row = world.service.journal.get(b)
    # What 0.3.1 leaves after a pass that read A and B (in creation order) and stopped before D.
    world.service.dreaming._set_state('cursor', [b_row['created_at'], b_row['id']])

    result, read = dream()
    assert result['state'] != 'idle', result
    assert read == [archive_id(world, d)]

    after, read_after = dream()
    assert after['state'] == 'idle' and read_after is None, after


def purge(world, record):
    from kelmo.archive_admin import ArchiveAdmin, operator_grant
    tool = ArchiveAdmin(world.service, operator_grant(world.service.config, {'purge', 'purge-plan'}))
    plan = tool.purge_plan(record)
    tool.authorize_purge(record, 'erase-one', plan['plan_sha256'])
    tool.purge(record, 'erase-one')


def test_a_forgotten_or_purged_record_does_not_stop_the_pass(world, dream):
    dream.drain()
    hidden, _ = world.archive('Evidence later forgotten: exports paused.', key='hidden-one')
    erased, _ = world.archive('Evidence later purged: exports resumed.', key='erased-one')
    world.service.forget(archive_id(world, hidden))
    purge(world, archive_id(world, erased))
    plain, _ = world.archive('Plain evidence: exports moved to 20:00.', key='plain-one')

    result, read = dream()

    assert result['state'] == 'finished', result
    assert read == [archive_id(world, plain)]


def test_a_failed_note_before_a_0_3_1_cursor_is_read_once_it_is_archived(world, dream):
    dream.drain()
    failed = waiting(world, 'Evidence F: failed before its archive, retried later.', 'f-evidence')
    world.service.journal.stage(failed, 'failed', terminal=True, error_code='E_IO')
    b, _ = world.archive('Evidence B: the export runs hourly.', key='b-two')
    b_row = world.service.journal.get(b)
    # What 0.3.1 leaves after a pass that passed F (terminal, not archived) and read B.
    world.service.dreaming._set_state('cursor', [b_row['created_at'], b_row['id']])
    world.service.retry(failed)
    assert world.service.run(failed)['durable_stage'] == 'archived'

    result, read = dream()
    assert result['state'] != 'idle', result
    assert read == [archive_id(world, failed)]

    after, read_after = dream()
    assert after['state'] == 'idle' and read_after is None, after


def test_a_retried_note_a_finding_already_cites_is_not_read_again_after_a_0_3_1_cursor(world, dream):
    # F failed, was retried and archived, and 0.3.1 read it: a finding cites it. Its earlier failure alone does
    # not mean 0.3.1 passed it unread, so the converted cursor does not offer it again.
    dream.drain()
    cited = waiting(world, 'Evidence F: failed once, archived after its retry, then read.', 'f-cited')
    world.service.journal.stage(cited, 'failed', terminal=True, error_code='E_BUSY')
    world.service.retry(cited)
    assert world.service.run(cited)['durable_stage'] == 'archived'
    finding = {'kind': 'stale', 'pages': ['projects/app/feature'], 'reason': 'F changed the export time.',
               'sources': [{'archive_id': archive_id(world, cited), 'version': 1}]}
    with world.service.journal.conn as db:
        db.execute("INSERT INTO dream_findings (fingerprint, kind, finding, baseline, state, first_seen, last_seen) "
                   "VALUES ('f-cited-finding', 'stale', ?, '{}', 'open', '2026-11-15', '2026-11-15')",
                   (json.dumps(finding),))
    b, _ = world.archive('Evidence B: the export runs daily.', key='b-three')
    b_row = world.service.journal.get(b)
    # What 0.3.1 leaves after a pass that read F and B.
    world.service.dreaming._set_state('cursor', [b_row['created_at'], b_row['id']])
    newer, _ = world.archive('Evidence N: the export moved to 21:00.', key='n-three')

    result, read = dream()
    assert result['state'] != 'idle', result
    assert read == [archive_id(world, newer)]


def test_under_a_one_source_ceiling_a_late_archive_waits_only_behind_what_was_archived_before_it(
        world, dream, tmp_path, capsys):
    # The ceiling bounds every pass (docs/DREAMING.md: ceilings, not targets), so a late note is read by the next
    # pass with room for it: in archive order, after a note archived before it, and never skipped.
    from kelmo.cli import call
    dream.drain()
    code = call(['--vault', str(world.root), 'dream', 'configure', '--executor-config', str(tmp_path / 'executor.json'),
                 '--max-sources', '1', '--apply', '--json'])
    capsys.readouterr()
    assert code == 0
    late = waiting(world, 'Late: the export owner changed.', 'late-ceiling')
    newer, _ = world.archive('Newer: exports moved to 20:00.', key='newer-ceiling')
    assert dream()[1] == [archive_id(world, newer)]
    between, _ = world.archive('Archived before the late one: exports run twice.', key='between-ceiling')
    assert world.service.run(late)['durable_stage'] == 'archived'

    assert dream()[1] == [archive_id(world, between)]
    assert dream()[1] == [archive_id(world, late)]
    result, read = dream()
    assert result['state'] == 'idle' and read is None, result
