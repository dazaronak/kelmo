"""The edge core's delivery pass gives back the outbox's free pages, as the 0.1 `kelmo client flush` did, without
holding a waiter at the outbox longer than one step: a new outbox gives pages back in steps (incremental vacuum),
an old one is rewritten once into that mode."""

import json
import sqlite3
import threading
import time

import pytest

from kelmo import client as client_module
from kelmo.client import SharedClient
from kelmo.core import edge_core
from kelmo.core.edge_core import EdgeCore


def _settled_pieces(outbox, count=60, prefix='piece'):
    """Settled transcript pieces drop their bytes but leave their pages free: about 24 MB here."""
    for index in range(count):
        outbox.enqueue('session_append', {'content': 'x' * 400000, 'provider': 'claude'}, f'{prefix}-{index}')
    for index in range(count):
        outbox.settle(f'{prefix}-{index}', 'delivered', {'ok': True})


def _size(profile):
    return (profile.state / 'outbox.sqlite3').stat().st_size


def _mode(profile):
    with sqlite3.connect(profile.state / 'outbox.sqlite3') as conn:
        return conn.execute('PRAGMA auto_vacuum').fetchone()[0]


def _passes(core, count, seconds=10):
    """Wake the deliverer and wait until it has finished `count` more passes."""
    for _ in range(count):
        before = core._last.get('at')
        core._wake.set()
        stop = time.monotonic() + seconds
        while core._last.get('at') == before and time.monotonic() < stop:
            time.sleep(0.02)
        assert core._last.get('at') != before, 'the deliverer finished no pass'


def _until(check, seconds):
    stop = time.monotonic() + seconds
    while not check() and time.monotonic() < stop:
        time.sleep(0.05)
    return check()


def _legacy(profile):
    """An outbox file made by 0.3.1 or earlier: pages are given back only by rewriting it (auto_vacuum 0)."""
    profile.state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(profile.state / 'outbox.sqlite3') as conn:
        conn.execute('CREATE TABLE made_before_this_release(x)')


@pytest.fixture
def real_edge(profile):
    client = SharedClient(profile, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    _passes(core, 1)
    yield core, client
    core.close()


@pytest.fixture
def legacy_edge(profile):
    """A factory: the edge core over an old outbox with free pages, made once the test has set its bounds (its
    first pass runs at once)."""
    _legacy(profile)
    client = SharedClient(profile, multiplex=True)
    assert client.outbox.compaction_state()[3] == 0
    _settled_pieces(client.outbox)
    made = []

    def make():
        core = EdgeCore(profile.root, client=client)
        made.append(core)
        _passes(core, 1)
        return core, client

    yield make
    for core in made:
        core.close()


def test_a_new_outbox_is_made_in_incremental_mode(profile):
    client = SharedClient(profile, multiplex=True)
    try:
        assert client.outbox.compaction_state()[3] == 2
    finally:
        client.close()


def test_a_delivery_pass_gives_back_the_outbox_free_space_in_steps(real_edge, profile, monkeypatch):
    core, client = real_edge
    _settled_pieces(client.outbox)
    before = _size(profile)
    assert before > 20 * 1048576
    steps = []
    real = client.outbox.vacuum_step

    def step(pages):
        steps.append(pages)
        return real(pages)

    monkeypatch.setattr(client.outbox, 'vacuum_step', step)
    _passes(core, 1)
    after = _size(profile)
    assert after < before // 4, f'the outbox kept its free pages: {before} bytes before the pass, {after} after'
    assert len(steps) > 1 and set(steps) == {client_module.COMPACT_CHUNK_PAGES}, steps
    result = core._last['result']['compaction']
    assert result['state'] == 'done' and result['freed'] >= before - after - 65536, result
    assert core.status()['compaction']['state'] == 'done'


def test_an_old_outbox_is_rewritten_once_into_incremental_mode_when_the_rewrite_fits(legacy_edge, profile):
    before = _size(profile)
    legacy_edge()
    assert _size(profile) < before // 4, 'the old outbox kept its free pages'
    assert _mode(profile) == 2, 'the old outbox was not rewritten into incremental mode'


def test_an_old_outbox_is_converted_early_before_compaction_is_due(profile):
    """A file whose rewrite fits is converted on the first pass with nothing pending, due or not: waiting for
    "due" lets its live data outgrow the bound."""
    _legacy(profile)
    client = SharedClient(profile, multiplex=True)
    client.outbox.enqueue('session_append', {'content': 'x' * 1000, 'provider': 'claude'}, 'one')
    client.outbox.settle('one', 'delivered', {'ok': True})
    core = EdgeCore(profile.root, client=client)
    try:
        _passes(core, 1)
        assert _mode(profile) == 2
    finally:
        core.close()


def test_an_old_outbox_too_large_for_the_bound_waits_for_idle_and_is_then_converted(legacy_edge, profile,
                                                                                    monkeypatch):
    before = _size(profile)
    monkeypatch.setattr(edge_core, 'CONVERSION_SECONDS_PER_MB', 1000.0)
    monkeypatch.setattr(edge_core, 'COMPACT_RETRY_SECONDS', 0.0)
    core, client = legacy_edge()
    assert core._compaction['state'] == 'too_large', core._compaction
    assert _size(profile) == before and _mode(profile) == 0, 'converted past the bound while calls were served'
    monkeypatch.setattr(edge_core, 'IDLE_SECONDS', 0.0)
    _passes(core, 1)
    assert _mode(profile) == 2 and _size(profile) < before // 4, 'the idle pass did not convert the old outbox'


def test_a_rewrite_slower_than_predicted_is_stopped_and_the_outbox_stays_whole(legacy_edge, profile, monkeypatch):
    before = _size(profile)
    monkeypatch.setattr(edge_core, 'COMPACT_COPY_SECONDS', 0.0)
    monkeypatch.setattr(client_module, 'COMPACT_PROGRESS_STEPS', 1)
    core, client = legacy_edge()
    assert core._compaction['state'] == 'too_large', core._compaction
    assert _size(profile) == before and _mode(profile) == 0, 'a rewrite past its bound still ran to the end'
    assert client.outbox.conn.execute('PRAGMA busy_timeout').fetchone()[0] == \
        client_module.OUTBOX_WAIT_SECONDS * 1000, 'the outbox kept the short wait after the stopped rewrite'
    client.outbox.enqueue('session_append', {'content': 'y', 'provider': 'claude'}, 'after-the-stop')
    assert [entry['idempotency_key'] for entry in client.outbox.pending()] == ['after-the-stop']


def test_a_held_outbox_leaves_compaction_to_a_pass_a_second_later(real_edge, profile):
    core, client = real_edge
    _settled_pieces(client.outbox)
    before = _size(profile)
    with core.owned(1.0):
        started = time.monotonic()
        _passes(core, 1)
        assert time.monotonic() - started < 3, 'the pass waited for the held outbox'
        assert _size(profile) == before, 'compacted while another caller held the outbox'
    assert core._deliverer.is_alive()
    assert _until(lambda: _size(profile) < before // 4, 6), 'no pass compacted soon after the outbox was let go'


def test_a_pass_does_not_compact_while_a_hook_waits_for_the_outbox(real_edge, profile, monkeypatch):
    """Each step is taken only while nobody holds or waits for the outbox: a waiter waits one step at most."""
    core, client = real_edge
    _settled_pieces(client.outbox)
    monkeypatch.setattr(client_module, 'COMPACT_CHUNK_PAGES', 16)
    waited = []
    real = client.outbox.vacuum_step

    def step(pages):
        if not waited:  # after the first step, a hook asks for the outbox and gets it before the next one
            hook = threading.Thread(target=lambda: waited.append(core._owner.acquire(timeout=5, urgent=True))
                                    or core._owner.release())
            hook.start()
            _until(lambda: core._owner._waiting[True], 2)
            waited.append('asked')
        return real(pages)

    monkeypatch.setattr(client.outbox, 'vacuum_step', step)
    _passes(core, 1)
    assert core._last['result']['compaction']['state'] == 'busy', core._last['result']
    assert _until(lambda: True in waited, 3), waited


def test_a_hook_queued_during_the_drain_ends_it_for_delivery_first(real_edge, profile, monkeypatch):
    core, client = real_edge
    _settled_pieces(client.outbox)
    monkeypatch.setattr(client_module, 'COMPACT_CHUNK_PAGES', 16)
    real, woken = client.outbox.vacuum_step, []

    def step(pages):
        if not woken:
            woken.append(True)
            core._wake.set()  # what a hook does when it queues something
        return real(pages)

    monkeypatch.setattr(client.outbox, 'vacuum_step', step)
    _passes(core, 1)
    assert _until(lambda: woken and core._compaction.get('state') == 'paused', 3), core._compaction
    assert core._compaction['freed'] > 0, core._compaction


def test_a_pass_ends_within_its_deadline_while_a_flush_command_holds_the_outbox(real_edge, profile):
    core, client = real_edge
    _settled_pieces(client.outbox)
    before = _size(profile)
    with client.outbox._serial:
        started = time.monotonic()
        _passes(core, 1, seconds=edge_core.DELIVER_SECONDS + 4)
        assert time.monotonic() - started < 3, 'the pass waited for the outbox another caller held'
        assert _size(profile) == before, 'compacted while another caller held the outbox'
        assert core._last['result'].get('stopped') == 'E_BUSY', core._last['result']
    assert core._deliverer.is_alive()


def test_a_pass_does_not_wait_on_another_process_holding_the_outbox_file(real_edge, profile):
    """`kelmo mcp` on a shared client keeps its own connection to the outbox, outside the edge core's gate."""
    core, client = real_edge
    _settled_pieces(client.outbox)
    before = _size(profile)
    other = sqlite3.connect(profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    other.execute('BEGIN IMMEDIATE')
    release = threading.Timer(6.0, other.rollback)
    release.start()
    try:
        started = time.monotonic()
        _passes(core, 1)
        assert time.monotonic() - started < 3, 'the pass waited for another process holding the outbox file'
        assert _size(profile) == before, 'compacted while another process held the outbox file'
        assert core._last['result']['compaction']['state'] == 'busy', core._last['result']
    finally:
        release.cancel()
        other.rollback()
        other.close()
    assert _until(lambda: _size(profile) < before // 4, 6), 'no pass compacted soon after the file was let go'


def test_a_paused_compaction_survives_a_restart_of_the_edge_core(legacy_edge, profile, monkeypatch):
    before = _size(profile)
    monkeypatch.setattr(edge_core, 'CONVERSION_SECONDS_PER_MB', 1000.0)
    core, client = legacy_edge()
    record = json.loads((profile.state / 'compaction.json').read_text())
    assert record['state'] == 'too_large' and record['retry_at'] > time.time() + 600, record
    core.close()
    monkeypatch.setattr(edge_core, 'CONVERSION_SECONDS_PER_MB', 0.0)
    again = EdgeCore(profile.root, client=SharedClient(profile, multiplex=True))
    try:
        _passes(again, 1)
        assert _size(profile) == before and _mode(profile) == 0, 'a restarted edge core retried at once'
    finally:
        again.close()


def test_a_pause_in_the_past_does_not_make_the_deliverer_spin(real_edge):
    core, _client = real_edge
    core._compaction = {'state': 'done', 'retry_at': time.time() - 60}
    passes = []
    real = core.ingest_spool

    def counted(*args, **kwargs):
        passes.append(1)
        return real(*args, **kwargs)

    core.ingest_spool = counted
    time.sleep(1.0)
    assert len(passes) <= 1, f'{len(passes)} passes in a second with nothing to wake the deliverer'


def test_a_flush_command_under_the_edge_core_leaves_the_compaction_to_the_deliverer(legacy_edge, profile,
                                                                                    monkeypatch):
    import argparse
    monkeypatch.setattr(edge_core, 'CONVERSION_SECONDS_PER_MB', 1000.0)  # the deliverer leaves it alone
    core, client = legacy_edge()
    woken = []
    monkeypatch.setattr(core, 'wake', lambda: woken.append(True))
    before = _size(profile)
    with core.owned(1.0):
        answer = client_module.dispatch(profile.root, argparse.Namespace(command='client', action='flush'),
                                        lambda *_: None, client=client)
    assert answer['compacted'] == 0 and woken == [True], answer
    assert _size(profile) == before and _mode(profile) == 0, 'the command rewrote the outbox under its hold'


def test_a_client_on_its_own_converts_and_drains_when_compaction_is_due(profile):
    _legacy(profile)
    client = SharedClient(profile)
    try:
        _settled_pieces(client.outbox)
        before = _size(profile)
        assert client.outbox.compact() >= before // 2
        assert _mode(profile) == 2
        _settled_pieces(client.outbox, prefix='again')
        middle = _size(profile)
        assert client.outbox.compact() >= middle // 2
        assert client.outbox.compaction_state()[1] == 0
    finally:
        client.close()


def test_the_edge_core_stops_cleanly_while_another_caller_holds_the_outbox(real_edge, capsys):
    core, _client = real_edge
    taken, release = threading.Event(), threading.Event()

    def hold():
        core._owner.acquire()
        taken.set()
        release.wait(10)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(5)
    try:
        started = time.monotonic()
        core.close()  # the fixture's close afterwards is a second, harmless one
        assert time.monotonic() - started < edge_core.DELIVER_SECONDS + 4
        assert 'did not close cleanly' in capsys.readouterr().err
    finally:
        release.set()
        holder.join(10)


def test_status_reads_the_outbox_within_the_deadline_while_another_caller_holds_it(profile):
    client = SharedClient(profile, multiplex=True)
    taken, release = threading.Event(), threading.Event()

    def hold():
        with client.outbox._serial:
            taken.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(5)
    try:
        started = time.monotonic()
        with client.deadline(1.0):
            status = client.local_status()
        assert time.monotonic() - started < 2.0, 'status waited for the outbox past its deadline'
        assert status['outbox'] == {'busy': True}, status
    finally:
        release.set()
        holder.join(10)
        client.close()


def test_compaction_is_due_only_past_half_free():
    from kelmo.client import _compaction_due
    mib = 1048576
    assert not _compaction_due(pages=100, free=50, size=mib, min_free_bytes=16 * mib)
    assert _compaction_due(pages=100, free=51, size=mib, min_free_bytes=16 * mib)
    assert not _compaction_due(pages=100, free=99, size=1024, min_free_bytes=16 * mib)


def test_a_disk_error_reading_the_pending_count_is_not_reported_as_a_held_outbox(profile, monkeypatch):
    client = SharedClient(profile, multiplex=True)

    def broken():
        raise sqlite3.OperationalError('disk I/O error')

    monkeypatch.setattr(client.outbox, 'pending_count', broken)
    try:
        answer = client._pending_within()
    except sqlite3.OperationalError as error:
        answer = error
    finally:
        monkeypatch.undo()
        client.close()
    assert isinstance(answer, sqlite3.OperationalError), answer
