"""Bounded backfill steps and their scheduler cadence."""
from types import SimpleNamespace

from kelmo.core.jobs import (BACKFILL_FIRST_SECONDS, BACKFILL_LIMIT, BACKFILL_REST_FACTOR,
                             BACKFILL_REST_MAX_SECONDS, BACKFILL_SECONDS, Jobs)


def make_jobs(tmp_path, clock, embedding):
    writer = SimpleNamespace(config=SimpleNamespace(embedding=embedding))
    core = SimpleNamespace(root=tmp_path, storage=SimpleNamespace(writer=writer))
    return Jobs(core, clock=lambda: clock[0])


def test_schedule_waits_for_first_run_and_does_not_overlap(tmp_path, monkeypatch):
    clock = [1000]
    jobs = make_jobs(tmp_path, clock, {'provider': 'broker'})
    started = []
    monkeypatch.setattr(jobs, '_dream_due', lambda: None)
    monkeypatch.setattr(jobs, '_background', lambda kind, options: started.append((kind, options)) or True)
    jobs._tick()
    assert started == []
    clock[0] += BACKFILL_FIRST_SECONDS
    jobs._tick()
    jobs._tick()
    assert len(started) == 1
    monkeypatch.setattr(jobs, 'run', lambda kind, options: None)
    jobs._quietly(*started[0])
    clock[0] += BACKFILL_SECONDS - 1
    jobs._tick()
    assert len(started) == 1
    clock[0] += 1
    jobs._tick()
    assert len(started) == 2


def test_schedule_skips_vaults_without_embedding(tmp_path, monkeypatch):
    clock = [1000]
    jobs = make_jobs(tmp_path, clock, None)
    monkeypatch.setattr(jobs, '_dream_due', lambda: None)
    monkeypatch.setattr(jobs, '_background', lambda *args: (_ for _ in ()).throw(AssertionError()))
    clock[0] += BACKFILL_FIRST_SECONDS
    jobs._tick()


def test_backfill_uses_bounded_steps_until_complete(tmp_path, monkeypatch):
    jobs = make_jobs(tmp_path, [1000], {})
    calls = []

    def backfill(**options):
        calls.append(options)
        return {'written': 2, 'remaining': 1 if len(calls) == 1 else 0, 'stopped': None,
                'cursor': '2' if len(calls) == 1 else None}

    monkeypatch.setattr(jobs, '_admin', lambda: lambda verb: SimpleNamespace(backfill=backfill))
    result = jobs._backfill({})
    assert result['written'] == 4
    assert calls == [{'batch': 16, 'limit': BACKFILL_LIMIT, 'after': None},
                     {'batch': 16, 'limit': BACKFILL_LIMIT, 'after': '2'}]


def test_backfill_stops_when_requested(tmp_path, monkeypatch):
    jobs = make_jobs(tmp_path, [1000], {})

    def backfill(**options):
        jobs._stop.set()
        return {'written': 1, 'remaining': 3, 'stopped': None}

    monkeypatch.setattr(jobs, '_admin', lambda: lambda verb: SimpleNamespace(backfill=backfill))
    assert jobs._backfill({})['written'] == 1


def test_backfill_rests_between_one_batch_steps_and_caps_wait(tmp_path, monkeypatch):
    jobs = make_jobs(tmp_path, [1000], {})
    calls, waits = [], []
    steps = iter([
        {'written': 1, 'cursor': '1', 'seconds': 2, 'stopped': None},
        {'written': 1, 'cursor': '2', 'seconds': 100, 'stopped': None},
        {'written': 1, 'cursor': None, 'seconds': 1, 'stopped': None},
    ])

    def backfill(**options):
        calls.append(options)
        return next(steps)

    monkeypatch.setattr(jobs, '_admin', lambda: lambda verb: SimpleNamespace(backfill=backfill))
    monkeypatch.setattr(jobs._stop, 'wait', lambda seconds: waits.append(seconds) or False)
    assert jobs._backfill({})['written'] == 3
    assert waits == [2 * BACKFILL_REST_FACTOR, BACKFILL_REST_MAX_SECONDS]
    assert [call['limit'] for call in calls] == [BACKFILL_LIMIT] * 3
    assert [call['after'] for call in calls] == [None, '1', '2']


def test_backfill_stops_when_rest_is_interrupted(tmp_path, monkeypatch):
    jobs = make_jobs(tmp_path, [1000], {})
    calls, waits = [], []

    def backfill(**options):
        calls.append(options)
        return {'written': 1, 'cursor': '1', 'seconds': 2, 'stopped': None}

    monkeypatch.setattr(jobs, '_admin', lambda: lambda verb: SimpleNamespace(backfill=backfill))
    monkeypatch.setattr(jobs._stop, 'wait', lambda seconds: waits.append(seconds) or True)
    assert jobs._backfill({})['written'] == 1
    assert waits == [min(BACKFILL_REST_MAX_SECONDS, 2 * BACKFILL_REST_FACTOR)]
    assert calls == [{'batch': 16, 'limit': BACKFILL_LIMIT, 'after': None}]
