"""`kelmo client dismiss`: refusals the owner has read stop being listed, and are never sent again."""

import json

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401


def _refuse(outbox, key, code, op='create', content=None):
    outbox.enqueue(op, {'content': content or f'Text of {key}.', 'project': 'app'}, key)
    outbox.settle(key, 'refused', {'code': code, 'message': f'{code} for {key}'})


def _cli(client, *argv):
    from kelmo.cli import run_borrowed_client
    root = str(client.profile.root)
    return run_borrowed_client(client, ['client', 'dismiss', '--vault', root, *argv, '--json'], root=root)


def test_dismiss_lists_the_matching_refusals_and_changes_nothing_without_apply(shared):
    client = shared.client()
    _refuse(client.outbox, 'old-input', 'E_INPUT')
    _refuse(client.outbox, 'old-check', 'E_FACT_CHECK')
    _refuse(client.outbox, 'old-capture', 'E_INPUT', op='capture')
    listed = _cli(client, '--code', 'E_INPUT')
    assert listed['applied'] is False
    assert [item['idempotency_key'] for item in listed['dismissed']] == ['old-input', 'old-capture']
    assert [item['code'] for item in listed['dismissed']] == ['E_INPUT', 'E_INPUT']
    assert [item['idempotency_key'] for item in _cli(client, '--key', 'old-check')['dismissed']] == ['old-check']
    assert [item['idempotency_key'] for item in _cli(client)['dismissed']] == ['old-input', 'old-check', 'old-capture']
    assert {item['idempotency_key'] for item in client.outbox.summary()['refused']} == {
        'old-input', 'old-check', 'old-capture'}, 'a listing changes nothing'


def test_applied_dismissal_leaves_the_status_and_keeps_the_refusal(shared):
    client = shared.client()
    _refuse(client.outbox, 'old-input', 'E_INPUT')
    _refuse(client.outbox, 'old-check', 'E_FACT_CHECK')
    applied = _cli(client, '--code', 'E_INPUT', '--apply')
    assert applied['applied'] is True and [item['idempotency_key'] for item in applied['dismissed']] == ['old-input']
    summary = client.outbox.summary()
    assert [item['idempotency_key'] for item in summary['refused']] == ['old-check']
    assert summary['dismissed'] == 1
    assert 'refused_total' not in summary and 'refused_by_code' not in summary
    entry = client.outbox.get('old-input')
    assert entry['state'] == 'dismissed'
    outcome = json.loads(entry['outcome'])
    assert outcome['code'] == 'E_INPUT' and outcome['message'] == 'E_INPUT for old-input'
    assert outcome['dismissed_at']
    assert _cli(client, '--code', 'E_INPUT', '--apply')['dismissed'] == [], 'a second run finds nothing'


def test_a_dismissed_refusal_is_never_sent_again(shared):
    import pytest
    from kelmo.errors import KelmoError
    client = shared.client()
    _refuse(client.outbox, 'old-input', 'E_INPUT')
    _cli(client, '--apply')
    assert client.outbox.resend(('E_INPUT',), apply=True) == []
    client.outbox.enqueue('create', {'content': 'Text of old-input.', 'project': 'app'}, 'old-input')
    assert client.outbox.get('old-input')['state'] == 'dismissed'
    assert client.outbox.pending() == []
    # The same request under its key is answered with its refusal, not as queued for delivery.
    with pytest.raises(KelmoError) as error:
        client.submit('create', {'content': 'Text of old-input.', 'project': 'app'}, 'old-input')
    assert error.value.code == 'E_INPUT' and str(error.value) == 'E_INPUT for old-input'
