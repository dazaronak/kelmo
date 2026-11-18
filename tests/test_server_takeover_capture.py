"""A client request taken over after its server process died answers with the note its dead attempt captured,
even when the note would no longer pass the checks a first attempt makes."""

from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

ARGS = {'content': 'A note captured just before its server process died.', 'filename': 'memory.md',
        'project': 'app', 'selected': [APP_PAGE], 'rule_change': False}


def _dies_after_capture(monkeypatch):
    """The first attempt captures the note, then its process dies before the request settles: the ledger row stays
    open and its lock is let go, as the OS does for a dead process."""
    from kelmo.server import ClientSession
    real, captured = ClientSession.write_create, []

    def write_create(self, **args):
        result = real(self, **args)
        if not captured:
            captured.append(result)
            raise RuntimeError('the server process died here')
        return result

    monkeypatch.setattr(ClientSession, 'write_create', write_create)
    return captured


def _no_longer_validates(monkeypatch):
    """The registry changed since the capture: the checks of a first attempt now refuse the note."""
    from kelmo import creation
    from kelmo.errors import KelmoError

    def request(*_args, **_kwargs):
        raise KelmoError('E_INPUT', 'No project map is registered for app.')

    monkeypatch.setattr(creation, 'request', request)


def test_a_taken_over_request_answers_with_the_note_its_dead_attempt_captured(shared, monkeypatch):
    captured = _dies_after_capture(monkeypatch)
    client = shared.client()
    first = client.submit('create', ARGS, 'note-1')
    assert first['queued'] is True and first['accepted'] is False, first
    assert len(captured) == 1, 'the first attempt did not capture the note'
    _no_longer_validates(monkeypatch)
    client.flush()
    entry = client.outbox.get('note-1')
    assert entry['state'] == 'delivered', entry
    operation = captured[0]['operation']['id']
    import json
    assert json.loads(entry['outcome'])['operation']['id'] == operation, entry['outcome']


def test_a_taken_over_request_whose_note_was_not_captured_is_checked_as_a_first_attempt(shared, monkeypatch):
    from kelmo.server import ClientSession

    def dies_before_capture(self, **args):
        raise RuntimeError('the server process died before the capture')

    real = ClientSession.write_create
    monkeypatch.setattr(ClientSession, 'write_create', dies_before_capture)
    client = shared.client()
    client.submit('create', ARGS, 'note-2')
    monkeypatch.setattr(ClientSession, 'write_create', real)
    _no_longer_validates(monkeypatch)
    client.flush()
    entry = client.outbox.get('note-2')
    assert entry['state'] == 'refused', entry
    import json
    assert json.loads(entry['outcome'])['code'] == 'E_INPUT', entry['outcome']
