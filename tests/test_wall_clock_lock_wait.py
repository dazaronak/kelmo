"""A wait for another connection's lock that must end by a deadline is timed by the clock, not by SQLite.

SQLite's busy timeout adds up the sleeps it asks for. Where the system wakes sleepers late (macOS coalesces timers
for a background process; a loaded VM does the same), a 1 s timeout waited more than 2 s. `retry_busy` reads the
clock after every pause instead, and the outbox commits a write on its own inside a wait, so a commit that meets a
reader is tried again without doing the write twice.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kelmo.client import Outbox
from kelmo.errors import BUSY_PAUSES, retry_busy, sqlite_busy


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'held.sqlite3'
    setup = sqlite3.connect(path)
    setup.execute('CREATE TABLE t (x)')
    setup.commit()
    setup.close()
    return path


def held(path, *statements, release_after=None):
    """Another connection that ran `statements` and keeps their lock, given up after `release_after` seconds."""
    other = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
    for statement in statements:
        other.execute(statement)
    if release_after is not None:
        threading.Timer(release_after, other.rollback).start()
    return other


def test_a_wait_whose_pauses_run_twenty_times_long_still_ends_with_the_first_look_past_its_deadline(database):
    other = held(database, 'BEGIN EXCLUSIVE')
    conn = sqlite3.connect(database, timeout=0, isolation_level=None)
    events = []
    until = time.monotonic() + 0.3

    def late_sleep(seconds):  # a sleeper woken twenty times later than it asked
        events.append(('pause', seconds, until - time.monotonic()))
        time.sleep(seconds * 20)

    def step():
        events.append(('try', None, until - time.monotonic()))
        return conn.execute('BEGIN IMMEDIATE')

    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            retry_busy(step, until, sleep=late_sleep)
    finally:
        other.rollback()
        conn.close()
        other.close()

    assert sqlite_busy(raised.value)
    pauses = [event for event in events if event[0] == 'pause']
    assert pauses, 'the step was never tried again'
    # No pause is asked longer than SQLite's own, nor longer than the time left when it is asked.
    assert all(seconds <= max(BUSY_PAUSES) and seconds <= left for _, seconds, left in pauses), pauses
    # A try follows every pause, and the wait ends with the try after the last one: no pause is taken once the
    # clock is past the deadline, however long the pauses ran.
    assert [kind for kind, _, _ in events] == ['try', 'pause'] * len(pauses) + ['try'], events


def test_a_lock_given_up_inside_the_wait_lets_the_step_through(database):
    other = held(database, 'BEGIN EXCLUSIVE', release_after=0.1)
    conn = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        started = time.monotonic()
        retry_busy(lambda: conn.execute('BEGIN IMMEDIATE'), started + 5.0)
        took = time.monotonic() - started
        conn.execute('ROLLBACK')
    finally:
        conn.close()
        other.close()
    assert 0.05 < took < 2.0, took


def test_a_failure_that_is_no_lock_is_raised_at_once(database):
    conn = sqlite3.connect(database, timeout=0, isolation_level=None)
    tries = []

    def step():
        tries.append(True)
        return conn.execute('SELECT * FROM no_such_table')

    try:
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            retry_busy(step, time.monotonic() + 5.0)
    finally:
        conn.close()
    assert len(tries) == 1


@pytest.mark.parametrize('code, message', [
    (sqlite3.SQLITE_LOCKED, 'database table is locked'),
    (sqlite3.SQLITE_BUSY, 'cannot commit transaction - SQL statements in progress'),
])
def test_a_lock_that_no_other_connection_holds_is_raised_at_once(code, message):
    """A conflict inside this connection does not end when another one lets go: waiting would only spend the
    deadline."""
    tries = []

    def step():
        tries.append(True)
        error = sqlite3.OperationalError(message)
        error.sqlite_errorcode = code
        raise error

    with pytest.raises(sqlite3.OperationalError, match=message):
        retry_busy(step, time.monotonic() + 5.0)
    assert len(tries) == 1


def outbox_with_entry(tmp_path):
    outbox = Outbox(tmp_path / 'outbox')
    outbox.enqueue('capture', {'content': 'A synthetic note.'}, 'k1')
    return outbox, tmp_path / 'outbox' / 'outbox.sqlite3'


def a_later_reader(path):
    """What a reader that comes now meets: 'let in', or the error that refused it."""
    conn = sqlite3.connect(path, timeout=0)
    try:
        conn.execute('SELECT count(*) FROM entries').fetchone()
        return 'let in'
    except sqlite3.OperationalError as error:
        return str(error)
    finally:
        conn.close()


def test_a_commit_that_meets_a_reader_inside_a_wait_is_tried_again_with_one_effect(tmp_path, monkeypatch):
    """The commit of a cut meets a reader. At the first pause another reader tries the file, then the first one
    leaves: the later reader is refused, since the writer kept its claim on the file while it waited, and the cut's
    statement ran once."""
    import kelmo.client as client_module
    outbox, path = outbox_with_entry(tmp_path)
    reader = held(path, 'BEGIN', 'SELECT count(*) FROM entries')
    late, writes = [], []
    real = client_module.retry_busy

    def first_pause_lets_another_reader_try(step, until, *, sleep=time.sleep):
        def pause(seconds):
            if not late:
                late.append(a_later_reader(path))
                reader.rollback()
            sleep(seconds)
        return real(step, until, sleep=pause)

    monkeypatch.setattr(client_module, 'retry_busy', first_pause_lets_another_reader_try)
    outbox.conn.set_trace_callback(lambda sql: writes.append(sql) if sql.startswith('UPDATE entries SET cuts') else None)
    try:
        with outbox._serial, outbox.waiting(5.0):
            cuts = outbox.cut('k1')
    finally:
        outbox.conn.set_trace_callback(None)
        reader.close()
    assert late == ['database is locked'], late
    assert len(writes) == 1, writes
    assert cuts == 1
    assert outbox.conn.execute("SELECT cuts FROM entries WHERE idempotency_key='k1'").fetchone()[0] == 1
    outbox.close()


def test_a_commit_that_never_meets_a_free_file_ends_by_the_wait_and_leaves_nothing(tmp_path):
    outbox, path = outbox_with_entry(tmp_path)
    reader = held(path, 'BEGIN', 'SELECT count(*) FROM entries')
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as raised, outbox._serial, outbox.waiting(0.3):
            outbox.cut('k1')
        took = time.monotonic() - started
    finally:
        reader.rollback()
        reader.close()
    assert sqlite_busy(raised.value)
    assert took < 0.3 + 0.6, took  # the 0.3 s wait, not the reader's: it never lets go
    assert not outbox.conn.in_transaction
    assert outbox.conn.execute("SELECT cuts FROM entries WHERE idempotency_key='k1'").fetchone()[0] == 0
    outbox.close()


def test_outside_a_wait_sqlite_waits_as_before(tmp_path):
    outbox, _path = outbox_with_entry(tmp_path)
    timeout = outbox.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    with outbox._serial, outbox.waiting(1.0):
        inside = outbox.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    after = outbox.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    assert (timeout, inside, after) == (60000, 0, 60000)
    outbox.close()


def test_an_inner_wait_never_outlasts_the_one_around_it(tmp_path):
    outbox, _path = outbox_with_entry(tmp_path)
    with outbox._serial, outbox.waiting(0.5):
        outer = outbox._wait_until
        with outbox.waiting(5.0):
            assert outbox._wait_until == outer
        with outbox.waiting(0.1):
            assert outbox._wait_until < outer
        assert outbox._wait_until == outer
    assert outbox._wait_until is None
    outbox.close()


def test_a_bounded_summary_reads_while_sqlite_does_not_wait(tmp_path, monkeypatch):
    outbox, _path = outbox_with_entry(tmp_path)
    seen = []
    real = Outbox.summary

    def summary(self):
        seen.append(self.conn.execute('PRAGMA busy_timeout').fetchone()[0])
        return real(self)

    monkeypatch.setattr(Outbox, 'summary', summary)
    assert outbox.summary_within(1.0)['pending'] == 1
    assert seen == [0]
    assert outbox.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 60000
    outbox.close()


def test_a_capture_transaction_under_a_deadline_waits_by_the_clock_and_one_without_as_before(tmp_path):
    from kelmo.config import Config
    from kelmo.sessions import Sessions
    from test_session_end_busy import prepared

    root, _source = prepared(tmp_path)
    sessions = Sessions(Config.load(root))
    try:
        db = sessions.journal.conn
        before = db.execute('PRAGMA busy_timeout').fetchone()[0]
        with sessions._capture_transaction(time.monotonic() + 5.0):
            under_a_deadline = db.execute('PRAGMA busy_timeout').fetchone()[0]
        with sessions._capture_transaction(None):
            without = db.execute('PRAGMA busy_timeout').fetchone()[0]
        after = db.execute('PRAGMA busy_timeout').fetchone()[0]
    finally:
        sessions.close()
    assert before > 0
    assert under_a_deadline == 0
    assert without == before == after


BACKGROUND_SUMMARY = '''
import json, sqlite3, sys, time
from pathlib import Path
import kelmo.client as client
real, pauses = client.retry_busy, []

def observed(step, until, *, sleep=time.sleep):
    def pause(seconds):
        pauses.append({'left': until - time.monotonic(), 'until': until})
        sleep(seconds)
    return real(step, until, sleep=pause)

client.retry_busy = observed
outbox = client.Outbox(Path(sys.argv[1]))
other = sqlite3.connect(Path(sys.argv[1]) / 'outbox.sqlite3', timeout=0, isolation_level=None)
other.execute('BEGIN EXCLUSIVE')
started = time.monotonic()
summary = outbox.summary_within(1.0)
print(json.dumps({'busy': summary == {'busy': True}, 'took': time.monotonic() - started, 'started': started,
                  'pauses': pauses}))
'''


@pytest.mark.skipif(sys.platform != 'darwin' or shutil.which('taskpolicy') is None,
                    reason='macOS taskpolicy runs a process at background priority, whose timers macOS coalesces')
def test_a_wait_bounded_at_one_second_holds_in_a_process_whose_timers_are_coalesced(tmp_path):
    """The case of the macos-26 CI runner: at background priority SQLite's own 1 s busy timeout waited 2.4 to 4 s on
    a 2026 Mac. Here the outbox waits in `retry_busy` by the clock: every pause starts before the 1 s bound and no
    wait is given a later one. Wall time is only checked loosely, since the child also gets the least CPU."""
    import json

    import kelmo
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(Path(kelmo.__file__).resolve().parents[1]), os.environ.get('PYTHONPATH', '')]))
    done = subprocess.run(['taskpolicy', '-b', sys.executable, '-c', BACKGROUND_SUMMARY, str(tmp_path / 'outbox')],
                          capture_output=True, text=True, timeout=60, env=env, check=False)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen['busy'] is True, seen
    assert seen['pauses'], 'the outbox did not wait by the clock'
    assert all(pause['left'] > 0 for pause in seen['pauses']), seen['pauses']
    # The bound is read just after `started`; the margin covers a child put off the CPU between the two.
    assert max(pause['until'] for pause in seen['pauses']) - seen['started'] < 1.5, seen['pauses']
    assert seen['took'] < 4.0, seen['took']
