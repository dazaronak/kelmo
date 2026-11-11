"""memory_create refuses a bad wait before writing; awaited pauses the client, never sleeps."""
import pytest

from kelmo.errors import KelmoError
from kelmo.mcp import awaited, server


class RecordingVault:
    """Stands in for the vault: each create call is a write that was made."""

    def __init__(self):
        self.creates = []

    def create(self, *args, **kwargs):
        self.creates.append((args, kwargs))
        return {'operation': {'id': 'op-1', 'state': 'applied', 'terminal': True}}


class FollowedWrite:
    """A shared client's service: pause lends the connection out; trace reports the stages."""

    def __init__(self):
        self.pauses = []
        self.traces = 0

    def pause(self, seconds):
        self.pauses.append(seconds)

    def trace(self, operation_id):
        self.traces += 1
        if self.traces == 1:
            return {'operation': {'id': operation_id, 'state': 'queued', 'terminal': False}, 'events': []}
        return {'operation': {'id': operation_id, 'state': 'applied', 'terminal': True},
                'events': [{'stage': 'applied'}]}


@pytest.mark.parametrize('wait', ['30', -1, True])
def test_memory_create_refuses_a_bad_wait_before_writing(tmp_path, wait):
    vault = RecordingVault()
    tools = server(tmp_path / 'vault', service=vault, registry={})
    with pytest.raises(KelmoError) as refused:
        tools['memory_create'](content='Synthetic note.', project='app', wait=wait)
    assert refused.value.code == 'E_INPUT'
    assert vault.creates == []


def test_awaited_pauses_the_client_between_polls_and_never_sleeps(monkeypatch):
    import kelmo.mcp as mcp

    def no_sleep(seconds):
        raise AssertionError('awaited slept instead of pausing the client')

    monkeypatch.setattr(mcp.time, 'sleep', no_sleep)
    selected = FollowedWrite()
    result = {'operation': {'id': 'op-1', 'state': 'queued', 'terminal': False}}
    final = awaited(selected, result, 30)
    assert selected.traces == 2
    assert len(selected.pauses) == 2
    assert final['operation']['state'] == 'applied'
    assert final['last_event'] == {'stage': 'applied'}
