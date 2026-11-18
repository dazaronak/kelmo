"""A `SessionEnd` hook in a local vault tries a busy journal again, so the session's last capture is not lost to
one moment of contention; `Stop` and `PreCompact` still make one attempt.

Two callers reach `capture_hook`: the CLI (`kelmo hook session-end`, its own journal) and the state core
(`serve_hook`, which lends its writer journal). Both are driven here. Contention is real: another connection holds
a write transaction or a read on the vault's journal, or another process holds the session intake lock, and a
timer gives it up.
"""

import fcntl
import io
import json
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager

from kelmo.cli import main
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.journal import Journal
from kelmo.session_control import capture_hook, inspect
from kelmo.sessions import Sessions
from test_session_control import configured

# What `kelmod hook` gives a capture hook (`CAPTURE_DEADLINE` in rust/crates/kelmod/src/hook.rs).
HOOK_DEADLINE = 7.0


def prepared(tmp_path):
    root, source = configured(tmp_path)
    Sessions(Config.load(root)).close()  # the session tables exist before the journal is held
    return root, source


def journal_path(root):
    return Config.load(root).internal('.state/journal.sqlite3')


@contextmanager
def held(root, seconds, statement):
    """Another connection runs `statement` on the vault's journal and keeps its lock for `seconds`."""
    connection = sqlite3.connect(journal_path(root), isolation_level=None, check_same_thread=False)
    for part in statement:
        connection.execute(part)
    released = threading.Event()

    def release():
        if not released.is_set():
            released.set()
            connection.execute('ROLLBACK')

    timer = threading.Timer(seconds, release)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        release()
        connection.close()


def journal_held(root, seconds):
    """A write transaction from another connection."""
    return held(root, seconds, ['BEGIN IMMEDIATE'])


def journal_read(root, seconds):
    """A read transaction from another connection: a commit cannot finish while it lasts."""
    return held(root, seconds, ['BEGIN', 'SELECT count(*) FROM sessions'])


@contextmanager
def intake_held(root, seconds):
    """Holds the session intake lock, as a capture in another process does, for `seconds`."""
    stream = open(Config.load(root).internal('.state/sessions.lock'), 'a')
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    timer = threading.Timer(seconds, lambda: fcntl.flock(stream, fcntl.LOCK_UN))
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


@contextmanager
def lent(root):
    """The writer journal the state core lends to `capture_hook`."""
    journal = Journal(journal_path(root))
    try:
        yield journal
    finally:
        journal.close()


def payload(source):
    return {'session_id': 'native', 'transcript_path': str(source)}


def state_core_hook(root, source, action, journal):
    """`capture_hook` as `serve_hook` calls it; returns its result, or the refusal it raised."""
    try:
        return capture_hook(root, 'claude', action, payload(source), internal_job=False, journal=journal)
    except KelmoError as error:
        return error


def answers(journal):
    """Whether the journal's connection still runs a query."""
    try:
        return journal.conn.execute('SELECT 1').fetchone()[0] == 1
    except sqlite3.ProgrammingError:
        return False


def hook(root, source, action, monkeypatch, capsys):
    """Runs `kelmo hook ACTION --json`; returns its exit code, what it printed and how long it took."""
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload(source))))
    capsys.readouterr()
    began = time.monotonic()
    code = main(['hook', action, '--provider', 'claude', '--vault', str(root), '--json'])
    seconds = time.monotonic() - began
    return code, json.loads(capsys.readouterr().out), seconds


def final_jobs(root):
    return [job for job in inspect(root)['jobs'] if job['task'] == 'final']


def test_a_session_end_that_meets_a_busy_journal_once_is_captured(tmp_path, monkeypatch, capsys):
    root, source = prepared(tmp_path)

    with journal_held(root, 2.0):  # longer than one capture attempt's 1.5 s budget
        code, printed, _ = hook(root, source, 'session-end', monkeypatch, capsys)

    assert (code, printed) == (0, {}), printed
    assert len(final_jobs(root)) == 1


def test_a_session_end_in_the_state_core_that_meets_a_busy_journal_once_is_captured(tmp_path):
    root, source = prepared(tmp_path)

    with lent(root) as journal, journal_held(root, 2.0):
        result = state_core_hook(root, source, 'session-end', journal)

    assert result == {}, result
    assert len(final_jobs(root)) == 1


def test_a_session_end_tried_again_is_captured_once(tmp_path, monkeypatch, capsys):
    root, source = prepared(tmp_path)

    with intake_held(root, 0.3):
        code, printed, _ = hook(root, source, 'session-end', monkeypatch, capsys)
    assert (code, printed) == (0, {}), printed
    again, _, _ = hook(root, source, 'session-end', monkeypatch, capsys)

    assert again == 0
    jobs = final_jobs(root)
    assert len(jobs) == 1
    assert len({(job['session_key'], job['end_record']) for job in jobs}) == 1


def test_a_session_end_whose_commit_meets_a_reader_is_captured_once(tmp_path):
    root, source = prepared(tmp_path)
    with lent(root) as journal:
        mode = journal.conn.execute('PRAGMA journal_mode').fetchone()[0]
        assert mode != 'wal', 'a reader blocks a commit only outside WAL mode'

        with journal_read(root, 2.0):  # the first attempt's commit waits out its budget
            result = state_core_hook(root, source, 'session-end', journal)

        assert result == {}, result
        assert not journal.conn.in_transaction
    assert len(final_jobs(root)) == 1


def test_a_busy_intake_leaves_the_state_cores_journal_open(tmp_path):
    root, source = prepared(tmp_path)
    with lent(root) as journal, intake_held(root, 30.0):
        refused = state_core_hook(root, source, 'session-stop', journal)

        assert isinstance(refused, KelmoError) and refused.code == 'E_BUSY', refused
        assert answers(journal)


def test_a_session_end_under_a_journal_that_stays_busy_ends_inside_its_deadline(tmp_path, monkeypatch, capsys):
    root, source = prepared(tmp_path)

    with journal_held(root, HOOK_DEADLINE + 3):
        code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)

    assert code != 0
    assert printed['ok'] is False and printed['error']['code'] == 'E_BUSY', printed
    assert seconds < HOOK_DEADLINE, seconds
    assert final_jobs(root) == []


def test_a_session_end_under_an_exclusive_lock_ends_inside_its_deadline(tmp_path, monkeypatch, capsys):
    """An exclusive lock blocks reads too, so the hook waits already while it opens the journal and reads the
    session schema, before any capture budget runs."""
    root, source = prepared(tmp_path)
    with lent(root) as journal:
        assert journal.conn.execute('PRAGMA journal_mode').fetchone()[0] != 'wal'

    with held(root, HOOK_DEADLINE + 5, ['BEGIN EXCLUSIVE']):
        code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)

    assert code != 0, printed
    assert printed['error']['code'] == 'E_BUSY', printed
    assert seconds < HOOK_DEADLINE, seconds


def test_stop_and_precompact_still_make_one_attempt(tmp_path, monkeypatch, capsys):
    root, source = prepared(tmp_path)

    for action in ('session-stop', 'session-checkpoint'):
        with journal_held(root, 2.5):
            code, printed, seconds = hook(root, source, action, monkeypatch, capsys)
        assert code != 0, action
        assert printed['error']['code'] == 'E_BUSY', (action, printed)
        assert seconds < 2.5, (action, seconds)  # gave up before the journal was free: no second attempt
    assert inspect(root)['jobs'] == []


def test_a_session_end_whose_journal_is_locked_while_it_reads_is_tried_again(tmp_path, monkeypatch, capsys):
    """The journal is locked for 2 s right after the transcript is parsed, while the capture reads the session's
    rows before its write transaction: the attempt runs out of its time and the next one captures."""
    import kelmo.sessions as sessions_module

    root, source = prepared(tmp_path)
    original = sessions_module.parse
    holders = []

    def locking_parse(*args, **kwargs):
        result = original(*args, **kwargs)
        if not holders:
            holder = held(root, 2.0, ['BEGIN EXCLUSIVE'])
            holder.__enter__()
            holders.append(holder)
        return result

    monkeypatch.setattr(sessions_module, 'parse', locking_parse)
    try:
        code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)
    finally:
        for holder in holders:
            holder.__exit__(None, None, None)

    assert holders, 'the journal was never locked'
    assert (code, printed) == (0, {}), printed
    assert len(final_jobs(root)) == 1
    assert seconds < HOOK_DEADLINE, seconds


def test_a_session_end_whose_journal_is_locked_again_between_statements_ends_inside_its_deadline(
        tmp_path, monkeypatch, capsys):
    """The journal is locked when the hook starts and freed after 4.5 s; as soon as the intake opens it is locked
    again for 5 s. No single wait of the attempt may run on past the hook's deadline."""
    root, source = prepared(tmp_path)
    original = Sessions.__init__
    again = []

    def relocked(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if not again:
            holder = held(root, 5.0, ['BEGIN EXCLUSIVE'])
            holder.__enter__()
            again.append(holder)

    monkeypatch.setattr(Sessions, '__init__', relocked)
    try:
        with held(root, 4.5, ['BEGIN EXCLUSIVE']):
            code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)
    finally:
        for holder in again:
            holder.__exit__(None, None, None)

    assert again, 'the journal was never locked again'
    assert seconds < HOOK_DEADLINE, (seconds, printed)


def test_a_session_end_whose_intake_opened_late_still_ends_inside_its_deadline(tmp_path, monkeypatch):
    """The state core's call: the attempt's intake opens 0.5 s before the hook's deadline and the journal stays
    busy, so the capture has 0.5 s left, not a fresh 1.5 s."""
    root, source = prepared(tmp_path)
    began = time.monotonic()
    deadline = began + HOOK_DEADLINE - 0.5
    original = Sessions.__init__

    def late(self, *args, **kwargs):
        original(self, *args, **kwargs)
        time.sleep(max(0.0, deadline - 0.5 - time.monotonic()))

    monkeypatch.setattr(Sessions, '__init__', late)
    with lent(root) as journal, journal_held(root, 30.0):
        try:
            outcome = capture_hook(root, 'claude', 'session-end', payload(source), internal_job=False,
                                   journal=journal, deadline=deadline)
        except KelmoError as error:
            outcome = error
        seconds = time.monotonic() - began

    assert isinstance(outcome, KelmoError) and outcome.code == 'E_BUSY', outcome
    assert seconds < deadline - began + 0.2, seconds


def test_a_lent_journal_whose_commit_never_meets_a_free_reader_is_left_without_a_transaction(tmp_path,
                                                                                            monkeypatch):
    """A reader starts after the first attempt has parsed its transcript and outlasts every attempt: that
    attempt's commit is refused, and the state core's writer journal is handed back with no transaction open, so
    the baton gives it back cleanly and the hook answers E_BUSY (retryable) rather than a processing failure."""
    import kelmo.sessions as sessions_module

    root, source = prepared(tmp_path)
    original = sessions_module.parse
    readers = []

    def reading_parse(*args, **kwargs):
        result = original(*args, **kwargs)
        if not readers:
            reader = journal_read(root, 6.0)
            reader.__enter__()
            readers.append(reader)
        return result

    monkeypatch.setattr(sessions_module, 'parse', reading_parse)
    with lent(root) as journal:
        try:
            outcome = state_core_hook(root, source, 'session-end', journal)
        finally:
            for reader in readers:
                reader.__exit__(None, None, None)
        assert readers, 'no reader was started'
        assert isinstance(outcome, KelmoError) and outcome.code == 'E_BUSY', outcome
        assert not journal.conn.in_transaction
        assert answers(journal)
    assert final_jobs(root) == []


def test_a_session_end_with_little_time_left_still_makes_one_attempt(tmp_path, monkeypatch):
    """A session end that reaches the journal with less time left than one pause (in the state core, after a long
    wait for the baton) still tries once; a free journal captures it."""
    from kelmo import session_control
    root, source = prepared(tmp_path)
    monkeypatch.setattr(session_control, 'SESSION_END_PAUSE', 1.0)

    with lent(root) as journal:
        try:
            result = capture_hook(root, 'claude', 'session-end', payload(source), internal_job=False,
                                  journal=journal, deadline=time.monotonic() + 0.6)
        except KelmoError as error:
            result = error

    assert result == {}, result
    assert len(final_jobs(root)) == 1


def test_a_commit_that_meets_a_reader_late_in_an_attempt_ends_inside_its_deadline(tmp_path):
    """The capture transaction opens late (a writer holds the journal) and its commit then meets a reader: the
    commit waits only for what is left of the attempt, so the hook still answers by its deadline."""
    root, source = prepared(tmp_path)
    deadline = 1.2

    with lent(root) as journal, journal_read(root, 5.0), journal_held(root, 0.6):
        began = time.monotonic()
        try:
            outcome = capture_hook(root, 'claude', 'session-end', payload(source), internal_job=False,
                                   journal=journal, deadline=began + deadline)
        except KelmoError as error:
            outcome = error
        seconds = time.monotonic() - began
        assert not journal.conn.in_transaction

    assert isinstance(outcome, KelmoError) and outcome.code == 'E_BUSY', outcome
    assert seconds < deadline + 0.25, seconds
    assert final_jobs(root) == []


def test_a_session_end_that_must_migrate_the_schema_under_an_exclusive_lock_ends_inside_its_deadline(
        tmp_path, monkeypatch, capsys):
    """A vault on the version 1 session schema migrates (and first backs the journal up) when the intake opens; an
    exclusive lock taken right before that backup still ends the hook with E_BUSY inside its deadline, and the
    unfinished backup file is not left behind."""
    import uuid as real_uuid

    from kelmo import sessions as module
    root, source = prepared(tmp_path)
    connection = sqlite3.connect(journal_path(root), isolation_level=None)
    connection.execute('DROP TABLE session_processing')
    connection.execute('UPDATE session_schema SET version=1')
    connection.close()
    backups = Config.load(root).internal('.state/backups')
    before = set(backups.glob('sessions-v2-*')) if backups.exists() else set()
    holder = sqlite3.connect(journal_path(root), isolation_level=None, check_same_thread=False)
    taken = threading.Event()
    timer = threading.Timer(HOOK_DEADLINE + 5, lambda: holder.execute('ROLLBACK'))

    class LockingUuid:
        """`uuid.uuid4()` names the backup file right before the backup starts: the lock is taken there."""
        @staticmethod
        def uuid4():
            if not taken.is_set():
                holder.execute('BEGIN EXCLUSIVE')
                taken.set()
                timer.start()
            return real_uuid.uuid4()

    monkeypatch.setattr(module, 'uuid', LockingUuid)
    try:
        code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)
    finally:
        timer.cancel()
        if holder.in_transaction:
            holder.execute('ROLLBACK')
        holder.close()

    assert taken.is_set(), 'the hook never reached the backup'
    assert code != 0, printed
    assert printed['error']['code'] == 'E_BUSY', printed
    assert seconds < HOOK_DEADLINE, seconds
    assert set(backups.glob('sessions-v2-*')) == before


def test_a_session_end_whose_attempt_overran_its_budget_is_not_tried_past_the_deadline(tmp_path, monkeypatch, capsys):
    """An attempt that runs past its own budget on work, not on a lock (a transcript that takes long to read), is not
    started again when the time left is shorter than that attempt took: the hook still answers by its deadline."""
    from kelmo import sessions as module
    root, source = prepared(tmp_path)
    original = module.parse

    def slow_parse(*args, **kwargs):
        time.sleep(3.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'parse', slow_parse)
    code, printed, seconds = hook(root, source, 'session-end', monkeypatch, capsys)

    assert code != 0, printed
    assert printed['error']['code'] in ('E_BUSY', 'E_CAPTURE_RETRY'), printed
    assert seconds < HOOK_DEADLINE, seconds
