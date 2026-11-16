"""A request submitted again under the key of a refusal that was resent is answered as the note it went out as."""

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

ARGS = {'content': 'A note that was refused once.', 'filename': 'memory.md', 'project': 'app', 'selected': [],
        'rule_change': False}


def _answer(call):
    from kelmo.errors import KelmoError
    try:
        return call()
    except KelmoError as error:
        return ('refused', error.code, str(error))


def test_the_old_key_of_a_resent_note_answers_as_the_note_it_went_out_as(shared):
    from kelmo.client import ClientService
    client = shared.client()
    client.outbox.enqueue('create', ARGS, 'old')
    client.outbox.settle('old', 'refused', {'code': 'E_INPUT', 'message': 'Page is not registered: app.md'})
    assert [item['resend_as'] for item in client.outbox.resend(('E_INPUT',), apply=True)] == ['old#resend1']
    service = ClientService(client)
    sent = _answer(lambda: service.create(ARGS['content'], project='app', idempotency_key='old#resend1'))
    again = _answer(lambda: service.create(ARGS['content'], project='app', idempotency_key='old'))
    assert again == sent, f'the old key answered {again!r}, the note it went out as {sent!r}'
    assert client.outbox.pending() == []


def test_a_resend_chain_answers_as_its_last_note(shared):
    client = shared.client()
    client.outbox.enqueue('create', ARGS, 'old')
    client.outbox.settle('old', 'refused', {'code': 'E_INPUT', 'message': 'first'})
    client.outbox.resend(('E_INPUT',), apply=True)
    client.outbox.settle('old#resend1', 'refused', {'code': 'E_INPUT', 'message': 'second'})
    client.outbox.resend(('E_INPUT',), apply=True)
    client.outbox.settle('old#resend2', 'refused', {'code': 'E_FACT_CHECK', 'message': 'third'})
    assert _answer(lambda: client.submit('create', ARGS, 'old')) == ('refused', 'E_FACT_CHECK', 'third')


def test_a_note_of_another_project_with_the_same_text_does_not_answer_for_a_refusal(shared):
    client = shared.client()
    client.outbox.enqueue('create', ARGS, 'refused-in-app')
    client.outbox.settle('refused-in-app', 'refused', {'code': 'E_INPUT', 'message': 'Page is not registered'})
    client.outbox.enqueue('create', dict(ARGS, project='web'), 'delivered-in-web')
    client.outbox.settle('delivered-in-web', 'delivered', {'id': 'op-web'})
    [item] = client.outbox.resend(('E_INPUT',), apply=True)
    assert item['resend_as'] == 'refused-in-app#resend1' and 'duplicate' not in item, item
    assert [row['idempotency_key'] for row in client.outbox.pending()] == ['refused-in-app#resend1']
