"""One model conversation across editor rounds: Kelmo names it, a provider may continue it."""
import io
import json
import sys

import pytest

from kelmo import conversation


def test_an_update_carries_only_what_changed():
    before = {'project': 'app', 'pages': [{'target': 'a'}], 'reconciliations': [], 'expansions_left': 2,
              'refusal': {'code': 'E_PROPOSAL'}}
    after = {'project': 'app', 'pages': [{'target': 'a'}, {'target': 'b'}], 'reconciliations': [{'id': 1}],
             'expansions_left': 1, 'new_work': {'target': 'w'}}
    message = conversation.update(before, after)
    assert message['appended'] == {'pages': [{'target': 'b'}], 'reconciliations': [{'id': 1}]}
    assert message['changed'] == {'expansions_left': 1, 'new_work': {'target': 'w'}}
    assert message['removed'] == ['refusal']
    assert 'project' not in json.dumps({key: message[key] for key in ('changed', 'appended')})
    # A list that did not only grow is replaced whole.
    assert conversation.update({'pages': [1, 2]}, {'pages': [2, 3]})['changed'] == {'pages': [2, 3]}
    assert conversation.update({'pages': [1, 2]}, {'pages': [1]})['changed'] == {'pages': [1]}


def test_the_first_turn_starts_and_later_turns_update():
    key = conversation.key('edit_pages', 'op-1', 'secret-token')
    assert 'secret-token' not in key and len(key) == 32
    assert conversation.turn(key, 0, None, {'a': 1}) == {'key': key, 'turn': 0, 'message': None}
    later = conversation.turn(key, 2, {'a': 1}, {'a': 2})
    assert later['turn'] == 2 and later['message']['changed'] == {'a': 2}


def test_a_job_refuses_a_malformed_conversation():
    from kelmo.contracts import ContextBundle
    from kelmo.executor import ModelJob
    bundle = ContextBundle.__new__(ContextBundle)
    for bad in ({'key': 'k', 'turn': 1, 'message': None}, {'key': 'k', 'turn': 0, 'message': {}},
                {'key': '', 'turn': 0, 'message': None}, {'key': 'k', 'turn': True, 'message': None},
                {'key': 'k', 'turn': 0}):
        with pytest.raises(ValueError):
            ModelJob('edit_pages', bundle, {}, conversation=bad)
    assert ModelJob('edit_pages', bundle, {}).conversation is None


def test_the_conversation_reaches_a_custom_provider_and_never_the_model_input(monkeypatch, capsys, tmp_path):
    from kelmo.cli import main
    seen = tmp_path / 'request.json'
    script = ('import json,sys; r=json.load(sys.stdin); open(' + repr(str(seen)) + ',"w").write(json.dumps(r)); '
              'print(json.dumps({"kind":"proposal","result":{"ok":True}}))')
    turn = {'key': 'k' * 32, 'turn': 1, 'message': {'update': 'x', 'changed': {'a': 2}, 'appended': {}, 'removed': []}}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'task': 'probe', 'a': 2, 'conversation': turn})))
    assert main(['executor', '--provider', 'custom', '--command-json', json.dumps([sys.executable, '-c', script])]) == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True}
    request = json.loads(seen.read_text())
    assert request['conversation'] == turn
    assert 'conversation' not in request['input'] and request['input']['a'] == 2


def test_a_request_without_a_conversation_keeps_the_old_shape():
    from kelmo.executor import resolve
    script = ('import json,sys; r=json.load(sys.stdin); assert "conversation" not in r; '
              'print(json.dumps({"kind":"proposal","result":{"ok":True}}))')
    assert resolve('custom', None, {'task': 'probe'}, command=[sys.executable, '-c', script]) == {'ok': True}


def test_the_worker_sends_the_conversation_beside_the_payload(monkeypatch):
    from kelmo import creation
    from kelmo.contracts import ContextBundle
    from kelmo.executor import ModelJob
    sent = {}

    def execute(argv, payload, timeout):
        sent.update(payload)
        return {'ok': True}
    monkeypatch.setattr(creation, 'execute', execute)
    turn = {'key': 'k' * 32, 'turn': 0, 'message': None}
    creation.CommandExecutor(['/bin/true']).execute(
        ModelJob('edit_pages', ContextBundle.__new__(ContextBundle), {'a': 1}, conversation=turn))
    assert sent == {'a': 1, 'task': 'edit_pages', 'conversation': turn}
