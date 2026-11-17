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
