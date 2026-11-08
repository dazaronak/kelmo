"""The optional bridge uses synthetic crew replies; no model or account is contacted."""
import json
import sys
import time

import pytest

from kelmo import crew_executor


def test_usage_selects_allowed_route_and_completed_result_is_reused(tmp_path, monkeypatch):
    starts = []

    def crew(_binary, command, *args, **kwargs):
        if command == 'recommend':
            return {'advice': 'route-a', 'rows': [
                {'account': 'route-a', 'excluded': [], 'windows': {'seven_day': {'used': 0}}},
                {'account': 'route-b', 'excluded': [], 'windows': {'seven_day': {'used': 50}}}]}
        if command == 'start':
            from pathlib import Path
            starts.append(json.loads(Path(args[-1]).read_text()))
            return {'session_id': 'run-1', 'agents': [{'id': 'executor', 'invocation_id': 'turn-1'}]}
        return {'agents': [{'id': 'executor', 'invocations': [
            {'id': 'turn-1', 'state': 'completed', 'exit_code': 0,
             'structured_output': {'ok': True}}]}]}

    monkeypatch.setattr(crew_executor, 'call', crew)
    request = {'protocol': 'kelmo-executor/1', 'task': 'probe', 'input': {'task': 'probe'},
               'instructions': 'Return JSON.', 'output_schema': {'type': 'object'}}
    config = {'crew_executable': sys.executable, 'state_dir': str(tmp_path),
              'routes': [{'account': 'route-a', 'provider': 'agy', 'model': 'synthetic-model'}]}
    assert crew_executor.run(request, config) == {'kind': 'proposal', 'result': {'ok': True}}
    assert crew_executor.run(request, config) == {'kind': 'proposal', 'result': {'ok': True}}
    assert len(starts) == 1
    agent = starts[0]['crew'][0]
    assert agent['account'] == 'route-a' and agent['policy'] == 'isolated' and agent['tools'] == []
    from kelmo.errors import KelmoError
    def lost_receipt(binary, command, *args, **kwargs):
        if command == 'start':
            starts.append('lost-receipt')
            raise KelmoError('E_EXECUTOR', 'allocation response lost')
        return crew(binary, command, *args, **kwargs)
    monkeypatch.setattr(crew_executor, 'call', lost_receipt)
    request = dict(request, input={'task': 'another probe'})
    with pytest.raises(KelmoError, match='response lost'):
        crew_executor.run(request, config)
    with pytest.raises(KelmoError) as uncertain:
        crew_executor.run(request, config)
    assert uncertain.value.code == 'E_LAUNCH_UNCERTAIN' and len(starts) == 2


def test_exhausted_allowed_account_defers_without_using_an_unlisted_account():
    from kelmo.quota import QuotaWait
    reset = time.time() + 120
    advice = {'advice': 'unlisted', 'rows': [
        {'account': 'route-a', 'excluded': ['reserve:5h'],
         'windows': {'five_hour': {'label': '5h', 'used': 100, 'resets_at': reset}}},
        {'account': 'unlisted', 'excluded': [], 'windows': {'seven_day': {'used': 0}}}]}
    with pytest.raises(QuotaWait) as failure:
        crew_executor.select_route(advice, [{'account': 'route-a', 'provider': 'agy', 'model': 'synthetic-model'}])
    assert failure.value.reset_at == reset
