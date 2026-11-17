"""A delivery pass waits for another process's lock on the outbox file no longer than its deadline before it
sends; once the server has answered, the answer is recorded whatever the wait."""

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


def test_an_answer_the_server_gave_is_recorded_even_after_the_deadline(shared):
    """The server answered; another process then holds the file past the pass's deadline: the settle waits for it,
    so the entry is delivered and is not sent again."""
    client = shared.client()
    client.outbox.enqueue('capture', ARGS, 'answered-late')
    holders = []
    real = client._send

    def send(channel, op, args, key, *rest, **kwargs):
        answer = real(channel, op, args, key, *rest, **kwargs)
        if op == 'capture':
            other = _holder(client, 'IMMEDIATE')
            holders.append(other)
            threading.Timer(1.5, other.rollback).start()
        return answer

    client._send = send
    try:
        result, _took = _flush_within(client, 1.0)
    finally:
        for other in holders:
            other.close()
    entry = client.outbox.get('answered-late')
    assert entry['state'] == 'delivered', (entry, result)
