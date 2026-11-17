"""One request key is executed by one attempt at a time, whichever server process serves the client: the state
core (keys in flight) and `kelmo serve-client` (lock files) refuse each other's running key with E_BUSY."""

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
