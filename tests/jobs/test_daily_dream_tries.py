"""The scheduler's daily Dreaming run, driven through the scheduler's tick: a core stop gives its try back, a failed
run starts again only after DREAM_RETRY_SECONDS, and no more than DREAM_TRIES runs are started in a day."""
import time
from types import SimpleNamespace

import pytest

from kelmo.core.jobs import DREAM_RETRY_SECONDS, DREAM_TRIES, Jobs
from kelmo.errors import Stopping


def day_at(hour, minute=0):
    return time.mktime((2026, 11, 16, hour, minute, 0, 0, 0, -1))


def ticking(tmp_path, monkeypatch, outcomes, duration=0):
    """Jobs whose tick runs the daily job on the calling thread, through `_quietly`, `run` and `_dream`. The n-th
    run started takes `duration` seconds and answers with `outcomes[n]` (the last one repeats): raised when it
    is an exception."""
    from kelmo import dreaming_cli
    (tmp_path / '.state').mkdir(exist_ok=True)
    now = [day_at(4)]
    reader = SimpleNamespace(journal=SimpleNamespace(conn=SimpleNamespace(
        execute=lambda _sql: SimpleNamespace(fetchall=lambda: []))))
    writer = SimpleNamespace(config=SimpleNamespace(embedding=None, batch_apply=False, batch_ready_sources=8))
    storage = SimpleNamespace(writer=writer, read=lambda fn: fn(reader))
    core = SimpleNamespace(root=tmp_path, storage=storage, profile=None, profile_path=None)
    jobs = Jobs(core, clock=lambda: now[0])
    starts = []

    def run_dream(*args, **kwargs):
        kwargs['on_start']()
        starts.append(now[0])
        now[0] += duration
        outcome = outcomes[min(len(starts), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def background(kind, options):
        assert kind == 'dream' and options == {'action': 'run', 'scheduled': True}, (kind, options)
        jobs._quietly(kind, options)
        return True

    monkeypatch.setattr(dreaming_cli, 'run_dream', run_dream)
    monkeypatch.setattr(jobs, '_dream_hold', lambda: (None, None))
    monkeypatch.setattr(jobs, '_background', background)
    return jobs, now, starts


def tick_until(jobs, now, end, step=60):
    while now[0] < end:
        jobs._tick()
        now[0] += step


def test_the_scheduler_starts_no_more_than_three_daily_tries(tmp_path, monkeypatch):
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('a broken configuration')])
    tick_until(jobs, now, day_at(23, 59))
    assert len(starts) == DREAM_TRIES == 3


def test_a_failed_daily_run_starts_again_only_after_the_delay(tmp_path, monkeypatch):
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('a broken configuration')])
    tick_until(jobs, now, day_at(23, 59))
    assert len(starts) >= 2
    assert starts[0] == day_at(4)
    assert [later - earlier for earlier, later in zip(starts, starts[1:])] == [DREAM_RETRY_SECONDS] * (len(starts) - 1)


def test_a_core_stop_gives_its_daily_try_back(tmp_path, monkeypatch):
    failure = RuntimeError('a broken configuration')
    jobs, now, starts = ticking(tmp_path, monkeypatch, [failure, failure, Stopping(), failure])
    tick_until(jobs, now, day_at(23, 59))
    # Two failed tries, one the stop gave back, then the third counted try: the day still had it.
    assert len(starts) == DREAM_TRIES + 1


@pytest.mark.parametrize('failure', [RuntimeError('a broken configuration'), {'state': 'failed', 'code': 'E_EXECUTOR'}],
                         ids=['raised', 'answered'])
def test_a_long_failed_run_waits_the_delay_from_its_end(tmp_path, monkeypatch, failure):
    jobs, now, starts = ticking(tmp_path, monkeypatch, [failure], duration=1200)
    tick_until(jobs, now, day_at(23, 59))
    assert len(starts) == DREAM_TRIES
    # Each run takes 1200 s and fails at its end; the next starts no sooner than DREAM_RETRY_SECONDS after that.
    assert all(later - earlier >= 1200 + DREAM_RETRY_SECONDS for earlier, later in zip(starts, starts[1:])), starts


def test_a_deferral_whose_reset_has_passed_costs_no_try_but_waits_the_delay(tmp_path, monkeypatch):
    """A usage limit costs no try (docs/DAEMON.md), but one whose reset time is already over holds nothing: the
    scheduler starts the run again only after DREAM_RETRY_SECONDS, never on every tick."""
    jobs, now, starts = ticking(tmp_path, monkeypatch, [{'state': 'deferred', 'reset_at': day_at(4) - 20}])
    tick_until(jobs, now, day_at(23, 59))
    assert len(starts) > DREAM_TRIES, 'a usage limit cost a try'
    assert all(later - earlier >= DREAM_RETRY_SECONDS for earlier, later in zip(starts, starts[1:])), starts


def test_a_usage_limit_leaves_the_day_all_three_tries(tmp_path, monkeypatch):
    """One past-reset usage limit, then a broken configuration: the limit took no try, so three failed tries follow."""
    failure = RuntimeError('a broken configuration')
    jobs, now, starts = ticking(tmp_path, monkeypatch, [{'state': 'deferred', 'reset_at': day_at(4) - 20}, failure])
    tick_until(jobs, now, day_at(23, 59))
    assert len(starts) == 1 + DREAM_TRIES, starts


def test_a_run_that_fails_before_it_starts_waits_the_delay(tmp_path, monkeypatch):
    """A scheduled run that fails before it really starts (reading Dreaming's settings, say): the scheduler waits
    DREAM_RETRY_SECONDS before it calls it again instead of calling it on every tick."""
    from kelmo import dreaming_cli
    jobs, now, _ = ticking(tmp_path, monkeypatch, [RuntimeError('unused')])
    calls = []

    def failing_before_the_start(*args, **kwargs):
        calls.append(now[0])
        raise RuntimeError('the settings could not be read')

    monkeypatch.setattr(dreaming_cli, 'run_dream', failing_before_the_start)
    tick_until(jobs, now, day_at(6))
    assert len(calls) >= 2, calls
    assert all(later - earlier >= DREAM_RETRY_SECONDS for earlier, later in zip(calls, calls[1:])), calls


def test_a_run_that_fails_after_midnight_waits_the_delay_on_the_new_day(tmp_path, monkeypatch):
    """A run started one day and failing the next, after the daily hour: the new day's first try still waits
    DREAM_RETRY_SECONDS from that failure instead of starting on the next tick."""
    failure = RuntimeError('a broken configuration')
    jobs, now, starts = ticking(tmp_path, monkeypatch, [failure], duration=86400)
    tick_until(jobs, now, day_at(23, 59) + 86400)
    assert len(starts) >= 2, starts
    assert starts[1] - (starts[0] + 86400) >= DREAM_RETRY_SECONDS, starts


def test_a_run_that_fails_before_it_starts_counts_its_try(tmp_path, monkeypatch):
    """A failed run is a try whether it failed before or after its start: three a day, as docs/DAEMON.md says."""
    from kelmo import dreaming_cli
    jobs, now, _ = ticking(tmp_path, monkeypatch, [RuntimeError('unused')])
    calls = []

    def failing_before_the_start(*args, **kwargs):
        calls.append(now[0])
        raise RuntimeError('the settings could not be read')

    monkeypatch.setattr(dreaming_cli, 'run_dream', failing_before_the_start)
    tick_until(jobs, now, day_at(23, 59))
    assert len(calls) == DREAM_TRIES, calls


def test_a_run_whose_lock_cannot_be_opened_waits_the_delay_and_counts_its_try(tmp_path, monkeypatch):
    """The Dreaming lock file cannot be opened (here a directory stands in its place): the run fails before
    run_dream is reached, and the scheduler still waits the delay and stops after three tries."""
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('unused')])
    (tmp_path / '.state' / 'dreaming.lock').mkdir()
    attempts = []
    opening = jobs._dreaming_lock

    def counted_lock():
        attempts.append(now[0])
        return opening()

    monkeypatch.setattr(jobs, '_dreaming_lock', counted_lock)
    tick_until(jobs, now, day_at(23, 59))
    assert starts == []
    assert len(attempts) == DREAM_TRIES, attempts
    assert all(later - earlier >= DREAM_RETRY_SECONDS for earlier, later in zip(attempts, attempts[1:])), attempts


def test_a_scheduled_run_that_reaches_the_job_inside_the_delay_starts_nothing(tmp_path, monkeypatch):
    # The tick read the gate open, then a failing run ended before this job started (or a local client sent
    # `scheduled` itself): the job reads the gate again and starts nothing.
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('a broken configuration')])
    jobs._tick()
    assert len(starts) == 1
    try:
        answer = jobs._dream({'action': 'run', 'scheduled': True})
    except RuntimeError as error:
        answer = error
    assert len(starts) == 1, answer
    assert answer == {'state': 'not_due'}


def test_a_stop_that_fails_a_call_before_the_start_charges_no_try(tmp_path, monkeypatch):
    # The core is stopping while the run waits on a call to another core (its archive writer check): that call
    # fails with a KelmoError, not Stopping. The stop caused it, so it charges no try and starts no delay.
    from kelmo import dreaming_cli
    from kelmo.errors import KelmoError
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('unused')])

    def stopped_before_start(*args, **kwargs):
        jobs._stop.set()
        raise KelmoError('E_TRANSPORT', 'The archive core can no longer answer.')

    monkeypatch.setattr(dreaming_cli, 'run_dream', stopped_before_start)
    try:
        jobs._dream({'action': 'run', 'scheduled': True})
    except KelmoError:
        pass
    tries = jobs._read_state().get('dream_tries') or {}
    assert tries.get('count', 0) == 0, tries
    assert tries.get('delay') is not True, tries


def test_a_stop_that_fails_a_call_after_the_start_charges_no_try(tmp_path, monkeypatch):
    # The run started, then the core began to stop and the run's next call to another core failed as a
    # KelmoError: the stop caused it, so the try comes back as for Stopping, with no delay.
    from kelmo import dreaming_cli
    from kelmo.errors import KelmoError
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('unused')])

    def stopped_after_start(*args, **kwargs):
        kwargs['on_start']()
        jobs._stop.set()
        raise KelmoError('E_UNAVAILABLE', 'The core is stopping.')

    monkeypatch.setattr(dreaming_cli, 'run_dream', stopped_after_start)
    try:
        jobs._dream({'action': 'run', 'scheduled': True})
    except KelmoError:
        pass
    tries = jobs._read_state().get('dream_tries') or {}
    assert tries.get('count', 0) == 0, tries
    assert tries.get('delay') is not True, tries


def test_a_stopped_try_gives_back_the_wait_it_found(tmp_path, monkeypatch):
    # A failed try ends at 04:05, so the next may start at 04:20. That one is stopped: the state is as before
    # it, and the following start is allowed from 04:20, not 15 minutes after the stop.
    jobs, now, starts = ticking(tmp_path, monkeypatch, [RuntimeError('a broken executor'), Stopping()], duration=300)
    jobs._tick()
    before = dict(jobs._read_state()['dream_tries'])
    now[0] = day_at(4, 20)
    jobs._tick()
    assert len(starts) == 2
    assert jobs._read_state()['dream_tries'] == before
    now[0] = day_at(4, 26)
    assert jobs._dream_try_allowed()
