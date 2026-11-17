"""One request key is executed by one attempt at a time, whichever server process serves the client: the state
core (keys in flight) and `kelmo serve-client` (lock files) refuse each other's running key with E_BUSY."""

import pathlib
import threading

from kelmo.errors import KelmoError
from kelmo.server import ClientSession, KeysInFlight, _request_digest

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401


def _sessions(shared):
    in_core = ClientSession(shared.world.root, shared.hub.profile, 'mac-a', keys_in_flight=KeysInFlight())
    alone = ClientSession(shared.world.root, shared.hub.profile, 'mac-a')
    return in_core, alone


def _refused(session, key):
    """The code of the refusal, or None when the attempt was let run."""
    try:
        with session._request_lock(key):
            return None
    except KelmoError as error:
        return error.code


def test_serve_client_refuses_a_key_the_state_core_is_running(shared):
    in_core, alone = _sessions(shared)
    key = _request_digest(['mac-a', 'shared-key'])
    try:
        with in_core._request_lock(key):
            assert _refused(alone, key) == 'E_BUSY', 'serve-client ran a key the state core was running'
    finally:
        in_core.close()
        alone.close()


def test_the_state_core_refuses_a_key_serve_client_is_running(shared):
    in_core, alone = _sessions(shared)
    key = _request_digest(['mac-a', 'shared-key-2'])
    try:
        with alone._request_lock(key):
            assert _refused(in_core, key) == 'E_BUSY', 'the state core ran a key serve-client was running'
    finally:
        in_core.close()
        alone.close()


def test_a_key_is_free_again_once_its_attempt_ends(shared):
    in_core, alone = _sessions(shared)
    key = _request_digest(['mac-a', 'shared-key-3'])
    try:
        with in_core._request_lock(key) as release:
            release()
        with alone._request_lock(key):
            pass
        with in_core._request_lock(key):
            pass
    finally:
        in_core.close()
        alone.close()


def _lock_path(session, key):
    digest = _request_digest([session.client_id, key])
    return session.service.config.internal('.state/client-requests/' + digest + '.lock')


def _effect(session, release):
    """A write the test can hold open: it waits for `release`, then answers."""
    def write_slow(key):
        session.entered.set()
        release.wait(10)
        return {'done': key}
    session.entered = threading.Event()
    session.write_slow = write_slow


def _code(call):
    try:
        call()
    except KelmoError as error:
        return error.code
    return None


def test_serve_client_refuses_a_request_the_state_core_runs_through_mutate(shared):
    """The state core runs a write under a key; `serve-client` gets the same key meanwhile and refuses it with
    E_BUSY instead of running it; once the first settles, the second replays its answer."""
    release, started, results, sessions = threading.Event(), threading.Event(), [], []

    def run_in_core():
        in_core = ClientSession(shared.world.root, shared.hub.profile, 'mac-a', keys_in_flight=KeysInFlight())
        try:
            sessions.append(in_core)
            _effect(in_core, release)
            started.set()
            results.append(in_core._mutate('slow', {}, 'mutate-key'))
        finally:
            in_core.close()  # sqlite objects belong to the thread that made them

    alone = ClientSession(shared.world.root, shared.hub.profile, 'mac-a')
    alone_release = threading.Event()
    alone_release.set()
    _effect(alone, alone_release)
    runner = threading.Thread(target=run_in_core, daemon=True)
    try:
        runner.start()
        assert started.wait(10) and sessions[0].entered.wait(10), 'the state core never ran the write'
        assert _code(lambda: alone._mutate('slow', {}, 'mutate-key')) == 'E_BUSY', 'serve-client ran it as well'
        release.set()
        runner.join(10)
        assert results and results[0]['ok'], results
        replay = alone._mutate('slow', {}, 'mutate-key')
        assert replay.get('replayed') is True and not alone.entered.is_set(), replay
    finally:
        release.set()
        runner.join(10)
        alone.close()


def test_the_state_core_removes_the_lock_file_once_a_write_settles(shared):
    in_core = ClientSession(shared.world.root, shared.hub.profile, 'mac-a', keys_in_flight=KeysInFlight())
    try:
        release = threading.Event()
        release.set()
        _effect(in_core, release)
        assert in_core._mutate('slow', {}, 'settled-key')['ok']
        assert not _lock_path(in_core, 'settled-key').exists(), 'the lock file stayed after the write settled'
    finally:
        in_core.close()


def test_a_replay_found_after_taking_the_lock_leaves_no_lock_file(shared):
    """The row was open when first read and finished by the time the lock was taken: the answer is replayed and the
    lock file is removed, as after a settlement."""
    alone = ClientSession(shared.world.root, shared.hub.profile, 'mac-a')
    try:
        release = threading.Event()
        release.set()
        _effect(alone, release)
        assert alone._mutate('slow', {}, 'raced-key')['ok']
        journal = alone.service.journal
        real = journal.client_request

        def stale(request_key):
            row = real(request_key)
            journal.client_request = real  # only the first read is stale
            return dict(row, state='open')

        journal.client_request = stale
        replay = alone._mutate('slow', {}, 'raced-key')
        assert replay.get('replayed') is True, replay
        assert not _lock_path(alone, 'raced-key').exists(), 'a replay after the lock left its lock file'
    finally:
        alone.close()


def test_a_lock_file_that_cannot_be_removed_does_not_refuse_a_finished_write(shared, monkeypatch):
    in_core = ClientSession(shared.world.root, shared.hub.profile, 'mac-a', keys_in_flight=KeysInFlight())
    real = pathlib.Path.unlink

    def unlink(self, *args, **kwargs):
        if self.suffix == '.lock':
            raise PermissionError('not removable')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'unlink', unlink)
    try:
        release = threading.Event()
        release.set()
        _effect(in_core, release)
        outcome = None
        try:
            outcome = in_core._mutate('slow', {}, 'kept-file-key')
        except KelmoError as error:
            outcome = error.code
        assert isinstance(outcome, dict) and outcome['ok'], outcome
    finally:
        monkeypatch.undo()
        in_core.close()
