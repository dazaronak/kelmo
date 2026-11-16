"""The scheduler's daily Dreaming run, driven through the scheduler's tick: a core stop gives its try back, a failed
run starts again only after DREAM_RETRY_SECONDS, and no more than DREAM_TRIES runs are started in a day."""
import time
from types import SimpleNamespace

from kelmo.core.jobs import DREAM_RETRY_SECONDS, DREAM_TRIES, Jobs
from kelmo.errors import Stopping


def day_at(hour, minute=0):
    return time.mktime((2026, 11, 16, hour, minute, 0, 0, 0, -1))


def ticking(tmp_path, monkeypatch, outcomes):
    """Jobs whose tick runs the daily job on the calling thread, through `_quietly`, `run` and `_dream`. The n-th
    run started answers with `outcomes[n]` (the last one repeats): raised when it is an exception."""
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
