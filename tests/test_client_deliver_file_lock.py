"""A delivery pass waits for another process's lock on the outbox file no longer than its deadline before it
sends; once the server has answered, the answer is recorded whatever the wait."""

import json
import sqlite3
import threading
import time

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

ARGS = {'content': 'A note held behind a lock.', 'filename': 'held.md', 'role': 'implementer', 'promote': False,
        'supersedes': None}


def _holder(client, mode):
    other = sqlite3.connect(client.profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    other.execute(f'BEGIN {mode}')
    return other


def _flush_within(client, seconds):
    started = time.monotonic()
    try:
        with client.deadline(seconds):
            result = client.flush()
    except sqlite3.OperationalError as error:  # the pass outwaited its deadline and then gave up on the file
        result = {'stopped': repr(error), 'delivered': 0}
    return result, time.monotonic() - started


def test_a_pass_ends_within_its_deadline_while_another_process_holds_the_outbox_file(shared):
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'held-exclusive')
    other = _holder(client, 'EXCLUSIVE')
    try:
        result, took = _flush_within(client, 1.0)
        assert took < 2.0, f'the pass waited {took:.2f} s for the outbox file past its 1 s deadline'
        assert result['stopped'] == 'E_BUSY' and result['delivered'] == 0, result
    finally:
        other.rollback()
        other.close()
    entry = client.outbox.get('held-exclusive')
    assert (entry['state'], entry['attempts']) == ('pending', 0), entry


def test_a_pass_whose_attempt_cannot_be_recorded_in_time_sends_nothing(shared):
    """Another process holds the file's write lock: the pass reads its entry but cannot record the attempt; it
    ends within its deadline without sending, and the entry counts no attempt."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'held-immediate')
    sent = []
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        sent.append(op)
        return real(channel, op, args, key, *rest, **kwargs)

    client._send = send
    other = _holder(client, 'IMMEDIATE')
    try:
        result, took = _flush_within(client, 1.0)
        assert took < 2.0, f'the pass waited {took:.2f} s to record its attempt past its 1 s deadline'
        assert result['stopped'] == 'E_BUSY', result
        assert 'capture' not in sent, sent
    finally:
        other.rollback()
        other.close()
    entry = client.outbox.get('held-immediate')
    assert (entry['state'], entry['attempts']) == ('pending', 0), entry


def test_an_answer_the_file_cannot_take_in_time_is_answered_again_under_its_key_with_no_second_effect(
        shared, monkeypatch):
    """The server answered; another process then holds the file past the pass's deadline: the pass ends within its
    deadline with the entry pending, and the next pass gets the server's stored answer under the same key, so the
    entry is delivered with the first answer and the server's effect happened once."""
    from kelmo.server import ClientSession
    effects, write_capture = [], ClientSession.write_capture

    def counted(self, **kwargs):
        effects.append(kwargs.get('key'))
        return write_capture(self, **kwargs)

    monkeypatch.setattr(ClientSession, 'write_capture', counted)
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'answered-late')
    holders, answers = [], []
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        answer = real(channel, op, args, key, *rest, **kwargs)
        if op == 'capture':
            answers.append(answer)
            if len(answers) == 1:
                other = _holder(client, 'IMMEDIATE')
                holders.append(other)
                threading.Timer(1.5, other.rollback).start()
        return answer

    client._send = send
    try:
        result, took = _flush_within(client, 1.0)
        assert took < 2.0, f'the pass waited {took:.2f} s to record an answer past its 1 s deadline'
        assert result['stopped'] == 'E_BUSY' and result['reached'] is not True, result
        assert client.outbox.get('answered-late')['state'] == 'pending'
        time.sleep(max(0.0, 1.7 - took))  # the holder has let go
        with client.deadline(5.0):
            client.flush()
    finally:
        for other in holders:
            other.close()
    entry = client.outbox.get('answered-late')
    assert entry['state'] == 'delivered', (entry, result)
    assert len(answers) == 2 and answers[0] == answers[1], answers  # the stored answer, not a second effect
    assert effects == ['answered-late'], effects  # the server ran the capture once
    assert json.loads(entry['outcome']) == answers[0][1], entry


def test_an_answer_is_recorded_though_the_file_stays_held_past_the_outbox_wait(shared):
    """The file stays held for a second, longer than the outbox's own wait (made short here) and within the pass's
    deadline: the answer is recorded once the file is free, never left pending to be sent again."""
    client = shared.client()
    # The outbox's own wait made short: an answer recorded outside the pass's bounded wait would fail at 0.2 s.
    client.outbox.conn.execute('PRAGMA busy_timeout = 200')
    client.outbox.enqueue('capture', ARGS, 'answered-long-held')
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        answer = real(channel, op, args, key, *rest, **kwargs)
        if op == 'capture':
            other = _holder(client, 'IMMEDIATE')
            threading.Timer(1.0, lambda: (other.rollback(), other.close())).start()
        return answer

    client._send = send
    try:
        with client.deadline(5.0):
            client.flush()
    except sqlite3.OperationalError as error:
        raise AssertionError(f'the answer was dropped when the outbox wait ran out: {error}') from error
    entry = client.outbox.get('answered-long-held')
    assert entry['state'] == 'delivered', entry


def test_a_retryable_answer_while_the_file_is_held_ends_the_pass_within_its_deadline(shared):
    """The server answers retryable; noting the code needs the file, which another process holds: the note is
    skipped rather than waited for, and the pass ends within its deadline."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'retryable-held')
    holders = []
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        if op != 'capture':
            return real(channel, op, args, key, *rest, **kwargs)
        other = _holder(client, 'IMMEDIATE')
        holders.append(other)
        threading.Timer(3.0, other.rollback).start()
        return False, {'code': 'E_BUSY', 'message': 'busy', 'retryable': True, 'cut': False}

    client._send = send
    try:
        result, took = _flush_within(client, 1.0)
        assert took < 2.0, f'the pass waited {took:.2f} s to note a retryable answer past its 1 s deadline'
        assert result['stopped'] == 'E_BUSY', result
    finally:
        time.sleep(3.2)
        for other in holders:
            other.close()


def test_a_pass_waits_for_another_thread_in_the_outbox_no_longer_than_its_deadline(shared):
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'thread-held')
    held, release = threading.Event(), threading.Event()

    def hold():
        with client.outbox._serial:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert held.wait(5)
        threading.Timer(3.0, release.set).start()
        result, took = _flush_within(client, 1.0)
        assert took < 2.0, f'the pass waited {took:.2f} s for another thread in the outbox'
        assert result['stopped'] == 'E_BUSY', result
    finally:
        release.set()
        holder.join(10)


def _reader(client):
    """Another process reading the outbox: its read transaction lets a write begin but keeps it from committing."""
    other = sqlite3.connect(client.profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    other.execute('BEGIN')
    other.execute('SELECT COUNT(*) FROM entries').fetchone()
    return other


def test_an_attempt_whose_commit_a_reader_kept_back_is_never_counted_later(shared):
    """The attempt's update ran but its commit timed out behind a reader: the pass ends without sending, and the
    attempt does not reach the file later with the next write."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'reader-held')
    other = _reader(client)
    try:
        result, _took = _flush_within(client, 0.5)
        assert result['stopped'] == 'E_BUSY', result
    finally:
        other.rollback()
        other.close()
    try:
        client.outbox.enqueue('capture', dict(ARGS, filename='later.md'), 'a-later-write')
        later = None
    except sqlite3.OperationalError as error:  # a transaction left open by the refused commit
        later = error
    assert later is None, f'the next write failed: {later!r}'
    entry = client.outbox.get('reader-held')
    assert (entry['state'], entry['attempts']) == ('pending', 0), entry


def test_a_cut_recorded_behind_a_reader_counts_once(shared):
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'cut-behind-reader')
    # The outbox's own wait made short: a cut recorded outside `_record`'s bounded wait would fail at 0.2 s.
    client.outbox.conn.execute('PRAGMA busy_timeout = 200')
    other = _reader(client)
    threading.Timer(1.0, lambda: (other.rollback(), other.close())).start()
    assert client._record(client.outbox.cut, 'cut-behind-reader') == 1
    assert client.outbox.conn.execute("SELECT cuts FROM entries WHERE idempotency_key='cut-behind-reader'"
                                      ).fetchone()[0] == 1


def test_a_pass_that_finds_the_file_busy_keeps_the_entrys_earlier_refusal(shared):
    """The outbox file is busy when the pass records its attempt, and free again at once: nothing about the entry
    failed, so it keeps the refusal the server gave it before, not a local E_BUSY."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'busy-once')
    client.outbox.note('busy-once', 'E_EARLIER')
    real, calls = client.outbox.attempt, []

    def attempt(key, *rest):
        calls.append(key)
        if len(calls) == 1:
            raise sqlite3.OperationalError('database is locked')  # busy once, free right after
        return real(key, *rest)

    client.outbox.attempt = attempt
    with client.deadline(5.0):
        result = client.flush()
    entry = client.outbox.get('busy-once')
    assert result['stopped'] == 'E_BUSY', result
    assert (entry['state'], entry['last_error']) == ('pending', 'E_EARLIER'), entry


def test_an_attempt_that_waits_for_a_writer_and_then_a_reader_ends_by_its_deadline(shared):
    """Another process holds the write lock for most of the deadline, and a third reads the file meanwhile: the
    attempt's update waits for the writer and its commit for the reader, and both waits together stay within one
    deadline."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'writer-then-reader')
    writer = _holder(client, 'IMMEDIATE')
    reader = _reader(client)
    threading.Timer(0.6, lambda: (writer.rollback(), writer.close())).start()
    try:
        result, took = _flush_within(client, 1.0)
    finally:
        time.sleep(0.7)  # the writer is let go by its timer
        reader.rollback()
        reader.close()
    assert took < 1.3, f'the pass took {took:.2f} s against its 1 s deadline'
    assert result['stopped'] == 'E_BUSY', result
    entry = client.outbox.get('writer-then-reader')
    assert (entry['state'], entry['attempts']) == ('pending', 0), entry


def test_a_pass_stopped_by_its_own_held_outbox_does_not_report_the_server_reached(shared):
    """The channel is open but the outbox is held when the attempt is recorded: our queued writes did not go, so
    the pass does not report a reached server (the session hook then serves the cache, not a context that may
    predate them)."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'held-after-connect')

    def attempt(key, *rest):
        raise sqlite3.OperationalError('database is locked')

    client.outbox.attempt = attempt
    with client.deadline(5.0):
        result = client.flush()
    assert result['stopped'] == 'E_BUSY', result
    assert result['reached'] is not True, result


def test_a_pass_whose_next_entry_is_held_back_by_its_own_outbox_does_not_report_the_server_reached(shared):
    """The first of two entries goes out; the outbox is then held when the pass looks for the next one: a write of
    ours is still queued, so the pass does not report a reached server and the session hook serves the cache."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'first-goes')
    client.outbox.enqueue('capture', dict(ARGS, filename='second.md'), 'second-held')
    real, calls = client.outbox.next_pending, []

    def next_pending(*args):
        calls.append(args)
        if len(calls) == 2:
            raise sqlite3.OperationalError('database is locked')
        return real(*args)

    client.outbox.next_pending = next_pending
    with client.deadline(5.0):
        result = client.flush()
    assert result['delivered'] == 1 and result['stopped'] == 'E_BUSY', result
    assert result['reached'] is not True, result


def test_an_answer_that_cannot_be_recorded_within_the_deadline_ends_the_pass_and_stays_pending(shared):
    """Another process keeps the file past the pass's deadline for recording an answer: the pass gives up, ends with
    E_BUSY and leaves the entry pending, so the server answers its key again on a later pass, instead of waiting for
    as long as that process holds the file."""
    client = shared.client()
    # The outbox's own wait made short: only the pass's deadline decides how long the answer waits.
    client.outbox.conn.execute('PRAGMA busy_timeout = 100')
    client.outbox.enqueue('capture', ARGS, 'answered-never-recorded')
    real, holders = client._send, []

    def send(channel, op, args, key, *rest, **kwargs):
        answer = real(channel, op, args, key, *rest, **kwargs)
        if op == 'capture':
            other = _holder(client, 'IMMEDIATE')
            holders.append(other)
            threading.Timer(3.0, lambda: (other.rollback(), other.close())).start()  # lets go well past the deadline
        return answer

    client._send = send
    started = time.monotonic()
    try:
        with client.deadline(0.5):
            result = client.flush()
    finally:
        took = time.monotonic() - started
    time.sleep(max(0.0, 3.2 - took))  # the holder's timer has let go
    assert took < 2.0, f'the pass waited {took:.2f} s to record an answer past its 0.5 s deadline'
    assert result['stopped'] == 'E_BUSY' and result['delivered'] == 0, result
    assert client.outbox.get('answered-never-recorded')['state'] == 'pending'


def test_an_answer_held_by_a_writer_then_a_reader_still_ends_the_pass_within_its_deadline(shared):
    """After the server answered, another process holds the file's write lock for most of the pass's time left,
    and a reader then keeps the commit back: the settle's commit waits only for what is left of the deadline, so the
    pass ends with E_BUSY on time and the entry stays pending for the server's stored answer."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'writer-then-reader')
    others = []
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        answer = real(channel, op, args, key, *rest, **kwargs)
        if op == 'capture' and not others:
            writer = _holder(client, 'IMMEDIATE')
            reader = _reader(client)
            others.extend([writer, reader])
            threading.Timer(0.7, writer.rollback).start()
            threading.Timer(3.0, reader.rollback).start()
        return answer

    client._send = send
    try:
        result, took = _flush_within(client, 1.0)
        assert took < 1.5, f'the pass waited {took:.2f} s to record an answer past its 1 s deadline'
        assert result['stopped'] == 'E_BUSY' and result['reached'] is not True, result
    finally:
        time.sleep(max(0.0, 3.2 - took))  # both holders have let go
        for other in others:
            other.close()
    assert client.outbox.get('writer-then-reader')['state'] == 'pending'
    with client.deadline(5.0):  # the refused commit left no transaction open: the next pass records the answer
        client.flush()
    assert client.outbox.get('writer-then-reader')['state'] == 'delivered'
