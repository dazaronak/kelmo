"""The owner hears of a scheduled Dreaming run that stays idle while notes wait behind the findings pass's cursor,
and of a scheduled run that fails: once when it starts, once a day while it lasts, and once when a run completes.

The scheduler (`Jobs`) is real and `run_dream` is a stand-in that answers what the test names. The alert command
appends each message to a file, as in tests/test_alerts.py.
"""
import sys
from types import SimpleNamespace

import pytest

from kelmo.core.jobs import DREAM_RETRY_SECONDS, Jobs

START = 1_800_000_000
DAY = 86400


def recorder(tmp_path):
    script = tmp_path / 'alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n')
    return [sys.executable, str(script), str(tmp_path / 'messages.txt')]


def messages(tmp_path):
    path = tmp_path / 'messages.txt'
    return path.read_text().splitlines() if path.exists() else []


def scheduler(tmp_path, now):
    state = tmp_path / '.state'
    state.mkdir(exist_ok=True)
    reader = SimpleNamespace(journal=SimpleNamespace(conn=SimpleNamespace(
        execute=lambda _sql, *args: SimpleNamespace(fetchall=lambda: [], fetchone=lambda: (0,)))))
    config = SimpleNamespace(embedding={}, batch_apply=False, batch_ready_sources=8, state=state,
                             alerts={'command': recorder(tmp_path), 'label': 'Server Kelmo'})
    writer = SimpleNamespace(config=config)
    storage = SimpleNamespace(writer=writer, read=lambda fn: fn(reader))
    core = SimpleNamespace(root=tmp_path, storage=storage, profile=None, profile_path=None)
    return Jobs(core, clock=lambda: now[0])


def answering(monkeypatch, now, *outcomes):
    """run_dream starts (on_start) and answers the next outcome, or raises it; the wall clock reads now[0]."""
    from kelmo import alerts, dreaming_cli
    queue = list(outcomes)

    def run(*args, **kwargs):
        kwargs['on_start']()
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(dreaming_cli, 'run_dream', run)
    monkeypatch.setattr(alerts.time, 'time', lambda: now[0])
    monkeypatch.setattr(alerts, '_mono', lambda: now[0])


def scheduled_run(jobs):
    try:
        return jobs._dream({'scheduled': True, 'action': 'run'})
    except RuntimeError as error:
        return error


def test_a_findings_pass_that_stays_idle_with_notes_waiting_alerts_after_the_interval(tmp_path, monkeypatch):
    from kelmo.alerts import DREAM_IDLE_SECONDS
    now = [START]
    jobs = scheduler(tmp_path, now)
    stuck = {'state': 'idle', 'waiting': 2}
    answering(monkeypatch, now, stuck, stuck, stuck, stuck)
    scheduled_run(jobs)
    assert messages(tmp_path) == [], 'a first idle answer is no alert yet'
    now[0] = START + DREAM_IDLE_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert messages(tmp_path)[0].startswith('Server Kelmo: ') and '2' in messages(tmp_path)[0]
    now[0] += DREAM_RETRY_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, 'told once a day, not on every run'
    now[0] = START + DREAM_IDLE_SECONDS + DAY
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_quiet_findings_pass_never_alerts(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, *[{'state': 'idle', 'waiting': 0} for _ in range(4)])
    for day in range(4):
        now[0] = START + day * DAY
        scheduled_run(jobs)
    assert messages(tmp_path) == []
    assert not (tmp_path / '.state' / 'dream-watch.json').exists()


@pytest.mark.parametrize('failure', [{'state': 'failed', 'code': 'E_EXECUTOR', 'reason': 'no findings answer'},
                                     RuntimeError('the executor crashed')],
                         ids=['failed-answer', 'exception'])
def test_a_failed_scheduled_run_is_told_to_the_owner_once_a_day(tmp_path, monkeypatch, failure):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, failure, failure, failure)
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    code = 'E_EXECUTOR' if isinstance(failure, dict) else 'RuntimeError'
    assert code in messages(tmp_path)[0]
    now[0] += DREAM_RETRY_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, 'the retry 15 minutes later tells nothing new'
    now[0] = START + DAY
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


@pytest.mark.parametrize('trouble', ['stuck', 'failed'])
def test_a_run_that_completes_after_an_alert_sends_one_recovery_message(tmp_path, monkeypatch, trouble):
    from kelmo.alerts import DREAM_IDLE_SECONDS
    now = [START]
    jobs = scheduler(tmp_path, now)
    if trouble == 'stuck':
        bad = [{'state': 'idle', 'waiting': 3}, {'state': 'idle', 'waiting': 3}]
    else:
        bad = [{'state': 'failed', 'code': 'E_EXECUTOR', 'reason': 'timeout'}]
    answering(monkeypatch, now, *bad, {'state': 'finished'}, {'state': 'finished'})
    for outcome in bad:
        scheduled_run(jobs)
        now[0] += DREAM_IDLE_SECONDS
    told = len(messages(tmp_path))
    assert told == 1, messages(tmp_path)
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == told + 1, 'one recovery message'
    assert not (tmp_path / '.state' / 'dream-watch.json').exists()
    now[0] += DAY
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == told + 1, 'no second recovery message'


def test_a_run_that_completes_before_anything_was_told_sends_nothing(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, {'state': 'idle', 'waiting': 1}, {'state': 'finished'})
    scheduled_run(jobs)
    now[0] += DAY // 2
    scheduled_run(jobs)
    assert messages(tmp_path) == []
    assert not (tmp_path / '.state' / 'dream-watch.json').exists()


def test_a_scheduled_run_whose_lock_cannot_be_opened_is_told_to_the_owner(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now)
    (tmp_path / '.state' / 'dreaming.lock').mkdir()  # the lock file cannot be opened for writing
    with pytest.raises(OSError):
        jobs._dream({'scheduled': True, 'action': 'run'})
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert 'IsADirectoryError' in messages(tmp_path)[0]


def test_an_idle_alert_names_when_the_oldest_waiting_note_reached_the_archive(tmp_path, monkeypatch):
    # The cursor was held long before the first scheduled run that saw it: the alert goes at once and names
    # that time, not the time of the run.
    from datetime import UTC, datetime

    from kelmo.alerts import _when
    now = [START]
    jobs = scheduler(tmp_path, now)
    since = START - 30 * DAY
    stuck = {'state': 'idle', 'waiting': 2, 'waiting_since': datetime.fromtimestamp(since, UTC).isoformat()}
    answering(monkeypatch, now, stuck)
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert _when(since) in messages(tmp_path)[0], messages(tmp_path)
    assert _when(START) not in messages(tmp_path)[0], messages(tmp_path)


def test_passes_started_by_the_users_timer_are_watched_too(tmp_path, monkeypatch):
    # `kelmo dream schedule` writes a timer that runs `kelmo dream run`: its passes reach _dream with no
    # `scheduled` flag, and the owner hears of them as of the daemon's own.
    from kelmo.alerts import DREAM_IDLE_SECONDS
    now = [START]
    jobs = scheduler(tmp_path, now)
    stuck = {'state': 'idle', 'waiting': 2}
    failure = {'state': 'failed', 'code': 'E_EXECUTOR', 'reason': 'no findings answer'}
    answering(monkeypatch, now, stuck, stuck, failure)
    jobs._dream({'action': 'run'})
    now[0] = START + DREAM_IDLE_SECONDS
    jobs._dream({'action': 'run'})
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    now[0] += DAY
    jobs._dream({'action': 'run'})
    assert len(messages(tmp_path)) == 2, messages(tmp_path)
    assert 'E_EXECUTOR' in messages(tmp_path)[1]


def direct(tmp_path, monkeypatch, now, *outcomes):
    """`dreaming_cli.dispatch` for `kelmo dream run`, as a `--server-profile` timer runs it outside the daemon,
    over a stand-in Service whose Dreaming is configured; run_dream answers the outcomes in turn."""
    from kelmo import dreaming_cli
    from kelmo.interface import build_parser
    state = tmp_path / '.state'
    state.mkdir(exist_ok=True)
    config = SimpleNamespace(state=state, alerts={'command': recorder(tmp_path), 'label': 'Server Kelmo'},
                             internal=lambda relative: tmp_path / relative)

    class Configured:
        dreaming = SimpleNamespace(_state=lambda name, default=None: {'executor_config': 'x'}
                                   if name == 'settings' else default)

        def __init__(self, root):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dreaming_cli, 'Service', Configured)
    monkeypatch.setattr(dreaming_cli.Config, 'load', staticmethod(lambda root: config))
    from kelmo import alerts
    queue = list(outcomes)

    def answer(*args, **kwargs):
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(dreaming_cli, 'run_dream', answer)
    monkeypatch.setattr(alerts.time, 'time', lambda: now[0])
    parser, _commands = build_parser('0')
    args = parser.parse_args(['dream', 'run', '--vault', str(tmp_path)])

    def run():
        try:
            return dreaming_cli.dispatch(tmp_path, args)
        except (RuntimeError, OSError) as error:
            return error
    return run


def test_a_pass_the_cli_runs_outside_the_daemon_is_watched_too(tmp_path, monkeypatch):
    from kelmo.alerts import DREAM_IDLE_SECONDS
    now = [START]
    stuck = {'state': 'idle', 'waiting': 2}
    run = direct(tmp_path, monkeypatch, now, stuck, stuck, RuntimeError('the executor crashed'),
                 {'state': 'finished'})
    run()
    now[0] = START + DREAM_IDLE_SECONDS
    run()
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    now[0] += DAY
    assert isinstance(run(), RuntimeError)
    assert len(messages(tmp_path)) == 2 and 'RuntimeError' in messages(tmp_path)[1], messages(tmp_path)
    run()
    assert len(messages(tmp_path)) == 3, 'one recovery message'
    assert not (tmp_path / '.state' / 'dream-watch.json').exists()


def test_a_pass_the_cli_cannot_even_start_is_told_to_the_owner(tmp_path, monkeypatch):
    # The timer's run fails before Dreaming starts (here its lock file cannot be opened): the owner hears of it,
    # as of a failure in the daemon's dream job.
    now = [START]
    run = direct(tmp_path, monkeypatch, now, {'state': 'finished'})
    (tmp_path / '.state' / 'dreaming.lock').mkdir()
    assert isinstance(run(), OSError)
    assert len(messages(tmp_path)) == 1 and 'IsADirectoryError' in messages(tmp_path)[0], messages(tmp_path)


def test_a_clock_set_back_does_not_silence_the_daily_reminder(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    failure = {'state': 'failed', 'code': 'E_EXECUTOR', 'reason': 'no findings answer'}
    answering(monkeypatch, now, failure, failure)
    scheduled_run(jobs)
    now[0] = START - 3 * DAY  # the clock was set back three days; the run fails again
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_cli_result_is_told_before_its_lock_is_released(tmp_path, monkeypatch):
    # A daemon pass may take the lock the moment the CLI lets it go, complete and announce the recovery. The CLI's
    # own answer must already be told by then, so an idle answer it got before cannot reopen the alert afterwards.
    from datetime import UTC, datetime

    from kelmo import dreaming_cli
    from kelmo.alerts import DreamWatch
    now = [START]
    since = datetime.fromtimestamp(START - 3 * DAY, UTC).isoformat()
    stuck = {'state': 'idle', 'waiting': 2, 'waiting_since': since}
    run = direct(tmp_path, monkeypatch, now, stuck, stuck)
    run()
    assert len(messages(tmp_path)) == 1, messages(tmp_path)  # the hold is three days old: told at once
    inner = dreaming_cli._dispatch

    def then_the_daemon_recovers(root, args):
        result = inner(root, args)
        DreamWatch.for_config(dreaming_cli.Config.load(root)).ran(now[0])
        return result

    monkeypatch.setattr(dreaming_cli, '_dispatch', then_the_daemon_recovers)
    now[0] += DAY
    run()
    assert not (tmp_path / '.state' / 'dream-watch.json').exists(), messages(tmp_path)
    assert messages(tmp_path)[-1].endswith('reads new evidence.'), messages(tmp_path)


def test_a_cli_failure_inside_its_lock_is_told_before_the_lock_is_released(tmp_path, monkeypatch):
    # The same race for a run that raises: a daemon pass that takes the lock next and completes must come after the
    # CLI's failure, so the recovery it announces is the last word.
    from kelmo import dreaming_cli
    from kelmo.alerts import DreamWatch
    now = [START]
    run = direct(tmp_path, monkeypatch, now, RuntimeError('executor ended'))
    inner = dreaming_cli._dispatch

    def then_the_daemon_recovers(root, args):
        try:
            return inner(root, args)
        finally:
            DreamWatch.for_config(dreaming_cli.Config.load(root)).ran(now[0])

    monkeypatch.setattr(dreaming_cli, '_dispatch', then_the_daemon_recovers)
    assert isinstance(run(), RuntimeError)
    assert not (tmp_path / '.state' / 'dream-watch.json').exists(), messages(tmp_path)
    assert messages(tmp_path)[-1].endswith('reads new evidence.'), messages(tmp_path)


def test_a_daemon_failure_is_told_before_its_lock_is_released(tmp_path, monkeypatch):
    # The daemon's run raises; a CLI run may take the lock the moment the daemon lets it go and complete. The
    # daemon's failure must be told by then, so the recovery that run announces is the last word.
    from contextlib import contextmanager

    from kelmo.alerts import DreamWatch
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, RuntimeError('the executor crashed'))
    held = jobs._dreaming_lock

    @contextmanager
    def then_a_cli_run_completes():
        try:
            with held() as free:
                yield free
        finally:
            DreamWatch.for_config(jobs.storage.writer.config).ran(now[0])
    monkeypatch.setattr(jobs, '_dreaming_lock', then_a_cli_run_completes)
    assert isinstance(scheduled_run(jobs), RuntimeError)
    assert not (tmp_path / '.state' / 'dream-watch.json').exists(), messages(tmp_path)
    assert messages(tmp_path)[-1].endswith('reads new evidence.'), messages(tmp_path)


def test_a_schedule_probe_that_fails_while_the_daily_run_is_owed_is_told_to_the_owner(tmp_path, monkeypatch):
    # A broken kelmo.toml or journal makes the probe raise on every tick: no daily pass starts, and the owner hears
    # of it as of a failed run, once a day.
    import time

    from kelmo.core.jobs import DREAM_HOUR
    now = [time.mktime((2027, 1, 15, DREAM_HOUR, 5, 0, 0, 0, -1))]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now)

    def broken():
        raise RuntimeError('kelmo.toml cannot be read')
    monkeypatch.setattr(jobs, '_dream_hold', broken)
    monkeypatch.setattr(jobs, '_background', lambda kind, options: False)
    jobs._tick()
    now[0] += 5
    jobs._tick()
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert 'RuntimeError' in messages(tmp_path)[0], messages(tmp_path)


def test_a_clock_set_back_does_not_silence_the_stuck_reminder(tmp_path, monkeypatch):
    # The stuck cursor was told; then the clock is set back to an hour after the hold began. The reminder follows
    # the same rule as a failure's: a told time ahead of the clock makes it due, whatever the hold's age reads now.
    from datetime import UTC, datetime
    now = [START]
    since = datetime.fromtimestamp(START - DAY - 10, UTC).isoformat()
    stuck = {'state': 'idle', 'waiting': 2, 'waiting_since': since}
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, stuck, stuck)
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    now[0] = START - DAY + 3600
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


class _Died(BaseException):
    """Stands in for a process killed under the run: nothing after it runs, nothing is told."""


def test_a_run_whose_process_died_is_told_when_the_next_run_starts(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, _Died(), {'state': 'finished'})
    with pytest.raises(_Died):
        jobs._dream({'scheduled': True, 'action': 'run'})
    assert messages(tmp_path) == []
    now[0] += DREAM_RETRY_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) >= 1, messages(tmp_path)
    assert 'without an answer' in messages(tmp_path)[0], messages(tmp_path)


def test_a_run_the_core_stop_ended_is_not_told_as_died(tmp_path, monkeypatch):
    from kelmo.errors import Stopping
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, Stopping(), {'state': 'finished'})
    with pytest.raises(Stopping):
        jobs._dream({'scheduled': True, 'action': 'run'})
    now[0] += DREAM_RETRY_SECONDS
    scheduled_run(jobs)
    assert messages(tmp_path) == [], messages(tmp_path)


def test_a_failure_whose_telling_died_is_told_when_the_next_run_starts(tmp_path, monkeypatch):
    # The pass raised and its process was killed while the failure was being recorded: the start mark is still
    # there, so the next run tells it.
    from kelmo import alerts
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, RuntimeError('the pass broke'), {'state': 'finished'})
    original = alerts.DreamWatch.failed
    calls = []

    def dies_once(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise _Died()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(alerts.DreamWatch, 'failed', dies_once)
    with pytest.raises(_Died):
        jobs._dream({'scheduled': True, 'action': 'run'})
    assert messages(tmp_path) == []
    now[0] += DREAM_RETRY_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) >= 1, messages(tmp_path)
    assert 'without an answer' in messages(tmp_path)[0], messages(tmp_path)


def test_a_schedule_probe_that_fails_while_a_run_holds_the_dreaming_lock_tells_nothing(tmp_path, monkeypatch):
    # A pass is under way (the daemon's own job or a timer's run in its own process) when a later tick's probe
    # fails: that pass is not a failed run, and the owner hears nothing of the probe.
    import fcntl
    import time

    from kelmo.core.jobs import DREAM_HOUR
    now = [time.mktime((2027, 1, 15, DREAM_HOUR, 5, 0, 0, 0, -1))]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now)

    def broken():
        raise RuntimeError('the journal is busy')
    monkeypatch.setattr(jobs, '_dream_hold', broken)
    monkeypatch.setattr(jobs, '_background', lambda kind, options: False)
    with jobs._state_path.with_name('dreaming.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs._tick()
        now[0] += 5
        jobs._tick()
    assert messages(tmp_path) == [], messages(tmp_path)


def test_a_watch_file_that_cannot_be_written_tells_a_failure_once_a_day(tmp_path, monkeypatch):
    # The state directory takes no write (here the watch file is a directory): the told time cannot be kept on
    # disk, and a failing run tried on the next tick is still told once a day.
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, RuntimeError('the pass broke'), RuntimeError('the pass broke'),
              RuntimeError('the pass broke'))
    (tmp_path / '.state' / 'dream-watch.json').mkdir()
    scheduled_run(jobs)
    now[0] += 5
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    now[0] += DAY
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_failing_alert_command_is_tried_again_after_the_retry_delay_not_on_every_tick(tmp_path, monkeypatch):
    # The owner's alert command fails: nothing reached the owner, so the alert is tried again, but not on every
    # five-second tick of the scheduler.
    from kelmo.alerts import SEND_RETRY_SECONDS
    now = [START]
    jobs = scheduler(tmp_path, now)
    script = tmp_path / 'failing_alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n'
                      'sys.exit(1)\n')
    jobs.storage.writer.config.alerts = {'command': [sys.executable, str(script), str(tmp_path / 'messages.txt')],
                                         'label': 'Server Kelmo'}
    answering(monkeypatch, now, *[RuntimeError('the pass broke')] * 3)
    scheduled_run(jobs)
    now[0] += 5
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    now[0] += SEND_RETRY_SECONDS
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_cli_failure_before_the_lock_leaves_the_start_mark_of_a_pass_that_holds_it(tmp_path, monkeypatch):
    # A daemon pass is under way (its start mark is set) when a timer's run fails before it takes the lock: the
    # failure is told, and the mark stays for the pass that set it.
    now = [START]
    run = direct(tmp_path, monkeypatch, now, {'state': 'finished'})
    running = tmp_path / '.state' / 'dream-running.json'
    running.write_text('{"since": %d}' % START)
    (tmp_path / '.state' / 'dreaming.lock').mkdir()
    assert isinstance(run(), OSError)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert running.exists()


def test_a_daemon_failure_before_the_lock_leaves_the_start_mark_of_a_pass_that_holds_it(tmp_path, monkeypatch):
    now = [START]
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now)
    running = tmp_path / '.state' / 'dream-running.json'
    running.write_text('{"since": %d}' % START)
    (tmp_path / '.state' / 'dreaming.lock').mkdir()
    with pytest.raises(OSError):
        jobs._dream({'scheduled': True, 'action': 'run'})
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    assert running.exists()


def test_a_hold_is_not_told_before_the_interval_even_on_the_next_days_daily_run(tmp_path, monkeypatch):
    # The hold was first seen by a run late on day one (a catch-up, say); day two's daily run comes a few hours or
    # a few minutes short of a day later. The interval is the spec's day: day two tells nothing, the first run
    # after a full day does.
    now = [START]
    stuck = {'state': 'idle', 'waiting': 2}
    jobs = scheduler(tmp_path, now)
    answering(monkeypatch, now, stuck, stuck, stuck)
    scheduled_run(jobs)
    now[0] += DAY - 210
    scheduled_run(jobs)
    assert messages(tmp_path) == [], messages(tmp_path)
    now[0] += DAY
    scheduled_run(jobs)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)


def test_the_watch_file_is_changed_by_one_teller_at_a_time(tmp_path):
    # A failure told outside the Dreaming lock (before it was taken) and a pass's own outcome change the same
    # watch file: each read, send and write is done under the watch's own lock, one teller at a time.
    import fcntl
    import threading
    import time

    from kelmo.alerts import DreamWatch
    config = SimpleNamespace(state=tmp_path / '.state', alerts={'command': recorder(tmp_path), 'label': 'K'})
    config.state.mkdir()
    watch = DreamWatch.for_config(config)
    with (config.state / 'dream-watch.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        teller = threading.Thread(target=watch.failed, args=('E_X', 'broke', START))
        teller.start()
        time.sleep(0.5)
        assert messages(tmp_path) == [], messages(tmp_path)
        fcntl.flock(held, fcntl.LOCK_UN)
    teller.join(10)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)


def _watch(tmp_path, command=None):
    from kelmo.alerts import DreamWatch
    state = tmp_path / '.state'
    state.mkdir(exist_ok=True)
    config = SimpleNamespace(state=state, alerts={'command': command or recorder(tmp_path), 'label': 'K'})
    return DreamWatch.for_config(config)


def test_the_defined_interval_is_the_specs_day():
    from kelmo.alerts import DREAM_IDLE_SECONDS
    assert DREAM_IDLE_SECONDS == 86400


def test_a_daily_reminder_goes_on_the_next_day_whatever_minute_the_run_starts(tmp_path):
    # Told at one minute of day one; the next day's run fails again a couple of minutes earlier in the day.
    watch = _watch(tmp_path)
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    watch.failed('E_EXECUTOR', 'no findings answer', START + DAY - 110)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_hold_seen_by_runs_that_are_not_daily_waits_the_full_interval(tmp_path):
    # A timer or a run by hand sees the hold twice 23 hours apart: not the defined interval yet.
    watch = _watch(tmp_path)
    watch.idle(2, now=START)
    watch.idle(2, now=START + 23 * 3600)
    assert messages(tmp_path) == []


def test_a_failure_after_another_process_cleared_the_watch_file_is_told(tmp_path):
    # This process told a failure and kept it in the watch file; another process's run then completed and took
    # the file away. A new failure later the same day is a new failure and is told.
    watch = _watch(tmp_path)
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    watch.marker.unlink()
    watch.failed('E_EXECUTOR', 'no findings answer', START + 900)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_quiet_idle_after_an_alert_does_not_claim_new_evidence_was_read(tmp_path):
    watch = _watch(tmp_path)
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    watch.idle(0, now=START + 10)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)
    assert 'reads new evidence' not in messages(tmp_path)[-1], messages(tmp_path)


def test_a_clock_set_back_does_not_let_a_failed_delivery_retry_at_once(tmp_path):
    script = tmp_path / 'failing_alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n'
                      'sys.exit(1)\n')
    watch = _watch(tmp_path, [sys.executable, str(script), str(tmp_path / 'messages.txt')])
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    watch.failed('E_EXECUTOR', 'no findings answer', START - 100)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)


def test_a_told_alert_whose_record_never_reached_the_file_still_gets_its_recovery(tmp_path):
    # The state directory took no write when the failure was told; it does again when a run completes.
    watch = _watch(tmp_path)
    watch.marker.mkdir()
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)
    watch.marker.rmdir()
    watch.ran(START + 10)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)
    assert 'completes again' in messages(tmp_path)[-1], messages(tmp_path)


def test_an_idle_answer_after_a_failure_alert_tells_the_recovery_though_the_file_takes_no_write(tmp_path):
    from datetime import UTC, datetime
    watch = _watch(tmp_path)
    watch.marker.mkdir()
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    since = datetime.fromtimestamp(START - 2 * DAY, UTC).isoformat()
    watch.idle(2, now=START + 10, since=since)
    assert any(line.endswith('the Dreaming run completes again.') for line in messages(tmp_path)), messages(tmp_path)


def test_a_process_that_dies_between_the_idle_recovery_and_its_record_sends_no_second_recovery(tmp_path):
    from datetime import UTC, datetime

    class Killed(BaseException):
        pass

    watch = _watch(tmp_path)
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    since = datetime.fromtimestamp(START - 3600, UTC).isoformat()

    def killed(records):
        raise Killed
    watch._keep = killed
    with pytest.raises(Killed):
        watch.idle(2, now=START + 10, since=since)
    del watch._keep
    watch.idle(2, now=START + 20, since=since)
    recoveries = [line for line in messages(tmp_path) if line.endswith('the Dreaming run completes again.')]
    assert len(recoveries) == 1, messages(tmp_path)


def test_a_recovery_another_process_owns_is_not_sent_again(tmp_path):
    # A told failure is in the watch file; this process tells a second code but the file takes no write. Another
    # process's run then completes: it sees the told failure, takes the file away and sends the recovery.
    watch = _watch(tmp_path)
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    keep = watch._keep

    def full(records):
        raise OSError('no space left')
    watch._keep = full
    watch.failed('E_PROTOCOL', 'bad answer', START + 60)
    watch._keep = keep
    assert len(messages(tmp_path)) == 2, messages(tmp_path)
    watch.marker.unlink()
    watch.ran(START + 120)
    assert len(messages(tmp_path)) == 2, messages(tmp_path)


def test_a_clock_set_back_before_the_first_stuck_alert_delays_it_by_one_interval_at_most(tmp_path):
    from datetime import UTC, datetime
    watch = _watch(tmp_path)
    since = datetime.fromtimestamp(START - 3600, UTC).isoformat()
    watch.idle(2, now=START, since=since)
    back = START - 3 * DAY + 10
    watch.idle(2, now=back, since=since)
    watch.idle(2, now=back + DAY, since=since)
    assert len(messages(tmp_path)) == 1, messages(tmp_path)


def _refusing_command(tmp_path):
    script = tmp_path / 'refusing_alert.py'
    script.write_text('import sys\nsys.exit(1)\n')
    return [sys.executable, str(script)]


@pytest.mark.parametrize('completion', ['finished', 'idle with notes waiting', 'idle with nothing waiting'])
def test_a_failure_the_command_never_took_is_told_when_the_run_completes(tmp_path, completion):
    from datetime import UTC, datetime
    watch = _watch(tmp_path, _refusing_command(tmp_path))
    watch.failed('E_EXECUTOR', 'no findings answer', START)
    watch.argv = recorder(tmp_path)
    if completion == 'finished':
        watch.ran(START + 900)
    elif completion == 'idle with notes waiting':
        watch.idle(2, now=START + 900, since=datetime.fromtimestamp(START, UTC).isoformat())
    else:
        watch.idle(0, now=START + 900)
    told = messages(tmp_path)
    assert len(told) == 1, told
    assert 'E_EXECUTOR' in told[0] and 'did not go out' in told[0] and 'completes again' in told[0], told


def test_a_held_cursor_alert_the_command_never_took_is_told_when_the_run_completes(tmp_path):
    from datetime import UTC, datetime
    watch = _watch(tmp_path, _refusing_command(tmp_path))
    watch.idle(2, now=START, since=datetime.fromtimestamp(START - 2 * DAY, UTC).isoformat())
    watch.argv = recorder(tmp_path)
    watch.ran(START + 900)
    told = messages(tmp_path)
    assert len(told) == 1, told
    assert 'read nothing' in told[0] and 'did not go out' in told[0] and 'completes again' in told[0], told


def test_a_hold_that_ends_before_its_alert_was_due_tells_nothing(tmp_path):
    # Seen 23 hours into its hold, over by the next run two hours later: no alert was ever due, so none is missed.
    from datetime import UTC, datetime
    watch = _watch(tmp_path)
    watch.idle(2, now=START, since=datetime.fromtimestamp(START - 23 * 3600, UTC).isoformat())
    watch.ran(START + 2 * 3600)
    assert messages(tmp_path) == []
