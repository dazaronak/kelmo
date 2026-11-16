"""`kelmo worker daemon` keeps its loop going when the executor check refuses a resolution request, and its line
about operations held by another attempt names only those.

The loop is the real `daemon.run` (not `--once`) with an executor command that gives another build's instructions
for `resolve`. Each pass is the real `tick`; the test only counts the passes and, as the second one starts, sends
the process the SIGTERM `run` stops on.
"""

import json
import os
import signal

from kelmo import daemon
from kelmo.executor import identity
from test_executor_identity import saying
from test_fact_checks import content
from test_resolution import pending

pytest_plugins = ['test_fact_checks']  # the `service` fixture


def other_build_for_resolve():
    theirs = identity()
    theirs['version'] = '0.3.2'
    theirs['instructions'].update(resolve='0' * 64)
    return {'kelmo': theirs}


def two_passes(service, tmp_path, monkeypatch):
    """Runs the worker loop until its second pass has run; returns what each pass returned and what `run`
    returned or raised."""
    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': saying(other_build_for_resolve()), 'poll_seconds': 0.1}),
                      encoding='utf-8')
    real, passes = daemon.tick, []

    def counted(*args, **kwargs):
        if len(passes) == 1:
            os.kill(os.getpid(), signal.SIGTERM)  # `run` stops after the pass that starts now
        passes.append(real(*args, **kwargs))
        return passes[-1]

    monkeypatch.setattr(daemon, 'tick', counted)
    try:
        ended = daemon.run(service, config)
    except Exception as error:  # noqa: BLE001 - the test asserts on how the loop ended
        ended = error
    return passes, ended


def request(service):
    return dict(service.journal.conn.execute('SELECT id, operation_id, state, attempts FROM resolutions').fetchone())


def test_the_loop_takes_its_next_pass_after_a_refused_resolution_request(service, tmp_path, monkeypatch):
    pending(service)

    passes, ended = two_passes(service, tmp_path, monkeypatch)

    assert ended == {'stopped': True}, ended
    assert len(passes) == 2
    assert [entry['error_code'] for entry in passes[0]['skipped'] if 'resolution_id' in entry] == ['E_EXECUTOR']


def test_the_refused_request_stays_pending_with_its_attempts(service, tmp_path, monkeypatch):
    pending(service)
    before = request(service)

    passes, ended = two_passes(service, tmp_path, monkeypatch)

    assert ended == {'stopped': True}, ended
    after = request(service)
    assert (after['state'], after['attempts']) == ('pending', before['attempts'])


def test_the_held_line_names_a_dead_claim_and_never_the_refused_request(service, tmp_path, monkeypatch, capsys):
    pending(service)
    receipt = service.reconcile('Another durable fact.', 'projects/fixture/fixture.md')
    held = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']},
                                   text='Another durable fact.', key='fixture.other'), 'held.md', 'lead', 'promote')
    with service.journal.conn:  # the unexpired claim of a worker this machine cannot see
        service.journal.conn.execute('INSERT INTO claims (operation_id, token, expires_at, local_worker) '
                                     'VALUES (?, ?, ?, 0)', (held['id'], 'gone-worker', 9999999999))
    refused = request(service)
    capsys.readouterr()

    passes, ended = two_passes(service, tmp_path, monkeypatch)

    assert ended == {'stopped': True}, ended
    lines = [line for line in capsys.readouterr().err.splitlines()
             if 'skipped operations held by another attempt' in line]
    assert len(lines) == 1, lines
    assert held['id'] in lines[0]
    assert refused['id'] not in lines[0] and refused['operation_id'] not in lines[0]


def test_the_held_line_never_names_an_operation_skipped_for_a_busy_journal(service, tmp_path, monkeypatch, capsys):
    """A queued note skipped only because the journal was busy while its retry was charged is held by no other
    attempt, so the line about held operations does not name it."""
    import sqlite3
    receipt = service.reconcile('A busy fact.', 'projects/fixture/fixture.md')
    queued = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']},
                                     text='A busy fact.', key='fixture.busy'), 'busy.md', 'lead', 'promote')
    real = daemon.charge_retry

    def busy(service, operation, max_attempts=3):
        if operation['id'] == queued['id']:
            raise sqlite3.OperationalError('database is locked')
        return real(service, operation, max_attempts)

    monkeypatch.setattr(daemon, 'charge_retry', busy)
    capsys.readouterr()

    passes, ended = two_passes(service, tmp_path, monkeypatch)

    assert ended == {'stopped': True}, ended
    assert {'operation_id': queued['id'], 'error_code': 'E_BUSY'}.items() <= passes[0]['skipped'][0].items(), passes
    lines = [line for line in capsys.readouterr().err.splitlines()
             if 'skipped operations held by another attempt' in line]
    assert not any(queued['id'] in line for line in lines), lines


def test_the_held_line_never_names_an_operation_whose_run_met_a_busy_journal(service, tmp_path, monkeypatch, capsys):
    import sqlite3
    receipt = service.reconcile('A locked fact.', 'projects/fixture/fixture.md')
    queued = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']},
                                     text='A locked fact.', key='fixture.locked'), 'locked.md', 'lead', 'promote')
    real = service.run

    def locked(operation_id, *args, **kwargs):
        if operation_id == queued['id']:
            raise sqlite3.OperationalError('database is locked')
        return real(operation_id, *args, **kwargs)

    monkeypatch.setattr(service, 'run', locked)
    capsys.readouterr()

    passes, ended = two_passes(service, tmp_path, monkeypatch)

    assert ended == {'stopped': True}, ended
    assert any(entry.get('operation_id') == queued['id'] and entry['error_code'] == 'E_BUSY'
               for entry in passes[0]['skipped']), passes
    lines = [line for line in capsys.readouterr().err.splitlines()
             if 'skipped operations held by another attempt' in line]
    assert not any(queued['id'] in line for line in lines), lines
