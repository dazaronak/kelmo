"""The server answers the old key of a resent note as the note it went out as, also for a client whose outbox no
longer holds the old entry."""

from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

from contextlib import contextmanager

ARGS = {'content': 'A note refused once and resent.', 'filename': 'memory.md', 'project': 'app', 'selected': [APP_PAGE],
        'rule_change': False}


def _answer(call):
    from kelmo.errors import KelmoError
    try:
        return call()
    except KelmoError as error:
        return ('refused', error.code, str(error))


def _refuse_first(monkeypatch, code='E_INPUT', times=1):
    """The server refuses the first `times` creates with a final refusal, then runs them as usual."""
    from kelmo.errors import KelmoError
    from kelmo.server import ClientSession
    real, calls = ClientSession.write_create, []

    def write_create(self, **args):
        calls.append(args['key'])
        if len(calls) <= times:
            raise KelmoError(code, f'synthetic refusal {len(calls)}')
        return real(self, **args)

    monkeypatch.setattr(ClientSession, 'write_create', write_create)
    return calls


@contextmanager
def _running(session, key, *, live=True):
    """An attempt of `key` claimed on the server; held by a live attempt (its lock file locked) or abandoned."""
    import fcntl
    import os
    from kelmo.server import _request_digest
    request_key = _request_digest(['mac-a', key])
    session.service.journal.client_request_claim(request_key, 'mac-a', 'create',
                                                 _request_digest({'op': 'create', 'args': ARGS}))
    if not live:
        yield
        return
    directory = session.service.config.internal('.state/client-requests')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / (request_key + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def test_a_client_without_the_old_entry_gets_the_answer_of_the_note_it_went_out_as(shared, monkeypatch, tmp_path):
    _refuse_first(monkeypatch)
    first = shared.client()
    assert _answer(lambda: first.submit('create', ARGS, 'old'))[:2] == ('refused', 'E_INPUT')
    assert [item['resend_as'] for item in first.outbox.resend(('E_INPUT',), apply=True)] == ['old#resend1']
    flushed = first.flush()
    assert flushed['delivered'] == 1, (flushed, first.outbox.pending())
    sent = _answer(lambda: first.submit('create', ARGS, 'old#resend1'))
    assert isinstance(sent, dict), sent
    # The same client id from a profile made again: its outbox holds neither key.
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert again.outbox.get('old') is None
    answer = _answer(lambda: again.submit('create', ARGS, 'old'))
    assert answer == sent, f'the old key answered {answer!r}, the note it went out as {sent!r}'


def test_the_server_answers_the_old_key_as_the_last_note_of_a_resend_chain(shared, monkeypatch, tmp_path):
    _refuse_first(monkeypatch, times=2)
    first = shared.client()
    _answer(lambda: first.submit('create', ARGS, 'old'))
    first.outbox.resend(('E_INPUT',), apply=True)
    first.flush()
    assert first.outbox.get('old#resend1')['state'] == 'refused'
    first.outbox.resend(('E_INPUT',), apply=True)
    flushed = first.flush()
    assert flushed['delivered'] == 1, (flushed, first.outbox.pending())
    sent = _answer(lambda: first.submit('create', ARGS, 'old#resend2'))
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == sent
    assert _answer(lambda: again.submit('create', ARGS, 'old#resend1')) == sent


def test_a_refusal_with_no_resend_is_still_replayed_as_itself(shared, monkeypatch, tmp_path):
    _refuse_first(monkeypatch)
    first = shared.client()
    refused = _answer(lambda: first.submit('create', ARGS, 'old'))
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == refused


def test_another_clients_resend_does_not_answer_for_this_clients_key(shared, monkeypatch, tmp_path):
    """Keys are scoped per client: mac-b's `old#resend1` says nothing about mac-a's `old`."""
    _refuse_first(monkeypatch)
    first = shared.client()
    refused = _answer(lambda: first.submit('create', ARGS, 'old'))
    other = shared.client('mac-b')
    other.outbox.enqueue('create', ARGS, 'old#resend1')
    other.flush()
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == refused


def test_the_old_key_waits_while_the_note_it_was_resent_as_is_still_running(shared, monkeypatch, tmp_path):
    """A resend whose attempt is still open on the server: the old key is answered busy and stays queued, never with
    the old refusal."""
    _refuse_first(monkeypatch)
    first = shared.client()
    _answer(lambda: first.submit('create', ARGS, 'old'))
    session = shared.hub.session('mac-a')
    with _running(session, 'old#resend1'):
        again = shared.client(root=tmp_path / 'client-mac-a-again')
        answer = again.submit('create', ARGS, 'old')
    assert answer['queued'] is True and answer['accepted'] is False, answer
    assert answer['pending_reason'] == 'E_BUSY', answer


def test_an_abandoned_resend_attempt_does_not_hold_the_old_key(shared, monkeypatch, tmp_path):
    """The resend's attempt was claimed and its process is gone (no live lock): the old key is not held busy for
    good; it answers with the last answer the chain has, the old refusal."""
    _refuse_first(monkeypatch)
    first = shared.client()
    refused = _answer(lambda: first.submit('create', ARGS, 'old'))
    with _running(shared.hub.session('mac-a'), 'old#resend1', live=False):
        again = shared.client(root=tmp_path / 'client-mac-a-again')
        assert _answer(lambda: again.submit('create', ARGS, 'old')) == refused


def test_a_suffix_key_recorded_before_the_refusal_is_not_its_resend(shared, monkeypatch, tmp_path):
    """A request the client sent under `old#resend1` before `old` was refused is not a resend of it."""
    first = shared.client()
    other = dict(ARGS, content='Another note under a key that looks like a resend.')
    sent_first = _answer(lambda: first.submit('create', other, 'old#resend1'))
    assert isinstance(sent_first, dict), sent_first
    _refuse_first(monkeypatch)
    refused = _answer(lambda: first.submit('create', ARGS, 'old'))
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == refused


def test_a_suffix_key_of_another_operation_is_not_its_resend(shared, monkeypatch, tmp_path):
    _refuse_first(monkeypatch)
    first = shared.client()
    refused = _answer(lambda: first.submit('create', ARGS, 'old'))
    first.submit('capture', {'content': 'A capture, not the note.', 'filename': 'c.md', 'role': 'unknown',
                             'promote': False, 'supersedes': None}, 'old#resend1')
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == refused


def test_a_key_with_resend_followed_by_text_finds_the_chain_the_client_made(shared, monkeypatch, tmp_path):
    """`client resend` cuts a key at its first `#resend`; the server follows the same chain."""
    _refuse_first(monkeypatch)
    first = shared.client()
    _answer(lambda: first.submit('create', ARGS, 'parent#resend-note'))
    assert [item['resend_as'] for item in first.outbox.resend(('E_INPUT',), apply=True)] == ['parent#resend1']
    assert first.flush()['delivered'] == 1
    sent = _answer(lambda: first.submit('create', ARGS, 'parent#resend1'))
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'parent#resend-note')) == sent


def test_a_chain_whose_last_note_was_refused_answers_with_that_refusal(shared, monkeypatch, tmp_path):
    _refuse_first(monkeypatch, times=2)
    first = shared.client()
    _answer(lambda: first.submit('create', ARGS, 'old'))
    first.outbox.resend(('E_INPUT',), apply=True)
    first.flush()
    last = _answer(lambda: first.submit('create', ARGS, 'old#resend1'))
    assert last[:2] == ('refused', 'E_INPUT') and 'refusal 2' in last[2], last
    again = shared.client(root=tmp_path / 'client-mac-a-again')
    assert _answer(lambda: again.submit('create', ARGS, 'old')) == last
