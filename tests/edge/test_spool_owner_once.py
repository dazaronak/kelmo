"""A delivery pass replays a spooled hook only while nobody holds or waits for the owner of the outbox: with the
owner held, the pass leaves the file for a later pass at once instead of waiting for it."""

import json
import threading
import time

from kelmo.client import SharedClient
from kelmo.core.edge_core import EdgeCore

NAME = '00000000000000000001-1.json'
FRAME = {'action': 'session-stop',
         'payload': {'session_id': 'synthetic-session', 'transcript_path': '/synthetic/transcript.jsonl'},
         'context': {'provider': 'claude', 'internal_job': False}}


def _core(profile):
    client = SharedClient(profile, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    stop = time.monotonic() + 10
    while core._last.get('at') is None and time.monotonic() < stop:
        time.sleep(0.02)
    ran = []

    def run_hook(body):  # a capture takes the owner of the outbox (client._lock) to queue its piece
        with core.client._lock:
            ran.append(body['action'])
        return {}, []

    core._run_hook = run_hook
    return core, ran


def _spool(core):
    spool = core.spool_directory()
    spool.mkdir(parents=True, exist_ok=True)
    (spool / NAME).write_text(json.dumps(FRAME), encoding='utf-8')
    return spool / NAME


def test_a_spooled_hook_is_left_for_a_later_pass_while_the_owner_is_held(profile):
    core, ran = _core(profile)
    taken, release = threading.Event(), threading.Event()

    def hold():
        core._owner.acquire()
        taken.set()
        release.wait(30)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert taken.wait(5)
        path = _spool(core)
        started = time.monotonic()
        result = core.ingest_spool()
        took = time.monotonic() - started
        assert took < 1.0, f'the replay waited {took:.2f} s for the held owner'
        assert result == {'ingested': 0, 'pending': 1, 'stopped': 'E_BUSY'}, result
        assert path.exists() and ran == [], 'the spooled hook was not left for a later pass'
        release.set()
        holder.join(10)
        assert core.ingest_spool() == {'ingested': 1, 'pending': 0}
        assert ran == ['session-stop'] and not path.exists()
    finally:
        release.set()
        holder.join(10)
        core.close()


def test_a_spooled_hook_is_replayed_while_the_owner_is_free(profile):
    core, ran = _core(profile)
    try:
        path = _spool(core)
        assert core.ingest_spool() == {'ingested': 1, 'pending': 0}
        assert ran == ['session-stop'] and not path.exists()
    finally:
        core.close()


def test_a_spooled_hook_left_behind_a_held_owner_is_replayed_a_second_later_unwoken(profile):
    """The outbox is empty, so the flush of the same pass is not busy: the spool's own E_BUSY brings the deliverer
    back a second later, not after its full interval."""
    from kelmo.core import edge_core
    core, ran = _core(profile)
    taken, release = threading.Event(), threading.Event()

    def hold():
        core._owner.acquire()
        taken.set()
        release.wait(30)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert taken.wait(5)
        path = _spool(core)
        core._compact_after = time.monotonic() + 3600  # no compaction pause brings the deliverer back early
        before = core._last.get('at')
        core._wake.set()
        stop = time.monotonic() + 5
        while core._last.get('at') == before and time.monotonic() < stop:
            time.sleep(0.02)
        assert (core._last.get('spool') or {}).get('stopped') == 'E_BUSY', core._last
        release.set()
        holder.join(10)
        stop = time.monotonic() + edge_core.BUSY_RETRY_SECONDS + 1.5
        while path.exists() and time.monotonic() < stop:
            time.sleep(0.05)
        assert not path.exists(), 'the deliverer did not look at the spool again a second later'
        assert ran == ['session-stop']
    finally:
        release.set()
        holder.join(10)
        core.close()


def test_a_replayed_hook_holds_the_owner_only_for_its_outbox_steps(profile):
    """While a replayed hook reads and parses its transcript (no outbox step yet), a live hook gets the owner at
    once; the replay then queues its piece and removes the file."""
    core, ran = _core(profile)
    parsing, go = threading.Event(), threading.Event()

    def run_hook(body):
        parsing.set()
        go.wait(10)  # reading and parsing the transcript
        with core.client._lock:
            ran.append(body['action'])
        return {}, []

    core._run_hook = run_hook
    try:
        path = _spool(core)
        core._wake.set()
        assert parsing.wait(5), 'the spooled hook was never replayed'
        got = core._owner.acquire(timeout=0.5, urgent=True)
        if got:
            core._owner.release()
        go.set()
        assert got, 'a live hook waited for a replayed hook that was still reading its transcript'
        stop = time.monotonic() + 5
        while path.exists() and time.monotonic() < stop:
            time.sleep(0.05)
        assert not path.exists() and ran == ['session-stop'], ran
    finally:
        go.set()
        core.close()


def test_a_real_replayed_capture_that_finds_the_owner_taken_while_it_read_is_left_for_a_later_pass(
        profile, tmp_path, monkeypatch):
    """The real hook: the transcript is read with the owner free, another caller takes the owner before the piece
    is queued, and the replay gives the file back at once instead of waiting; once the owner is free it queues."""
    from kelmo import client as client_module
    root = tmp_path / 'provider'
    root.mkdir()
    transcript = root / 'session.jsonl'
    transcript.write_bytes(json.dumps({'type': 'user', 'message': {'content': 'A synthetic source.'}}).encode()
                           + b'\n')
    profile.sessions['claude'] = (str(root),)
    client = SharedClient(profile, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    core._compact_after = time.monotonic() + 3600
    stop = time.monotonic() + 10
    while core._last.get('at') is None and time.monotonic() < stop:  # the deliverer's first pass is over
        time.sleep(0.02)
    taken, release = threading.Event(), threading.Event()

    def hold():
        core._owner.acquire()
        taken.set()
        release.wait(30)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    read = client_module._stable_read
    first = []

    def stable_read(source):
        result = read(source)
        if not first:  # the first read only: another caller takes the owner before the piece is queued
            first.append(True)
            holder.start()
            assert taken.wait(5)
        return result

    monkeypatch.setattr(client_module, '_stable_read', stable_read)
    frame = dict(FRAME, payload={'session_id': 'synthetic-session', 'transcript_path': str(transcript)})
    spool = core.spool_directory()
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / NAME
    path.write_text(json.dumps(frame), encoding='utf-8')
    try:
        started = time.monotonic()
        result = core.ingest_spool()
        took = time.monotonic() - started
        assert took < 1.0, f'the replayed capture waited {took:.2f} s for the owner'
        assert result == {'ingested': 0, 'pending': 1, 'stopped': 'E_BUSY'}, result
        assert path.exists(), 'the spooled hook was not left for a later pass'
        queued = client.outbox.conn.execute("SELECT count(*) FROM entries WHERE op='session_append'").fetchone()[0]
        assert queued == 0, 'the refused replay queued a piece'
        release.set()
        holder.join(10)
        core._wake.set()  # the deliverer, or the next pass, replays it now that the owner is free
        stop = time.monotonic() + 5
        while path.exists() and time.monotonic() < stop:
            time.sleep(0.05)
        assert not path.exists(), 'the spooled hook was not replayed once the owner was free'
        queued = client.outbox.conn.execute("SELECT count(*) FROM entries WHERE op='session_append'").fetchone()[0]
        assert queued == 1, queued
    finally:
        release.set()
        if holder.is_alive():
            holder.join(10)
        core.close()


def test_a_busy_spool_is_looked_at_again_after_a_long_compaction(profile):
    """The spool was busy (a tool held the owner), the tool let go before the flush, so the same pass compacted, for
    longer than a second: the deliverer looks at the spool again once the compaction ends, not after its full
    interval."""
    core, ran = _core(profile)
    taken, let_go = threading.Event(), threading.Event()

    def hold():
        core._owner.acquire()
        taken.set()
        let_go.wait(30)
        core._owner.release()

    holder = threading.Thread(target=hold, daemon=True)
    real_ingest = core.ingest_spool

    def ingest_then_let_go():
        result = real_ingest()
        if result.get('stopped') == 'E_BUSY' and not let_go.is_set():
            let_go.set()  # the tool's write ends before the pass's flush
            holder.join(5)
        return result

    def slow_compact():
        time.sleep(1.5)
        return {'state': 'done', 'freed': 0}

    try:
        holder.start()
        assert taken.wait(5)
        path = _spool(core)
        core.ingest_spool = ingest_then_let_go
        core._compact = slow_compact
        core._compact_after = 0.0
        core._wake.set()
        stop = time.monotonic() + 4.5
        while path.exists() and time.monotonic() < stop:
            time.sleep(0.05)
        assert let_go.is_set(), 'the spool was never found busy'
        assert not path.exists(), 'the busy spool waited past the compaction for the full interval'
        assert ran == ['session-stop'], ran
    finally:
        let_go.set()
        holder.join(10)
        core.close()
