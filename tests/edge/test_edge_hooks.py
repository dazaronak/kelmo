"""The edge core replays spooled hooks, and `kelmod hook` spools or answers when no daemon listens.

The in-process tests drive EdgeCore with the fake client from conftest.py. The `kelmod hook` tests run the real
binary against a runtime directory with no socket in it, which is what a session sees when the daemon is down.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from kelmo.core.edge_core import HOOK_SECONDS

PAYLOAD = {'session_id': 'synthetic-session', 'transcript_path': '/synthetic/transcript.jsonl'}


def frame(action, payload=PAYLOAD):
    return json.dumps({'action': action, 'payload': payload,
                       'context': {'provider': 'claude', 'internal_job': False}})


def write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding='utf-8')


class Context:
    """The CoreLink context of one call: how much of its deadline is left."""

    def __init__(self, remaining):
        self._remaining = remaining

    def remaining(self):
        return self._remaining

    def check(self):
        """Never cancelled, never past its deadline."""


# --- the spool, replayed by the edge core ------------------------------------------------------------------------


def test_spool_replays_captures_in_name_order_and_quarantines_a_malformed_frame(edge):
    core, client, wakes = edge
    spool = core.spool_directory()
    # Written out of order: the file names, not the write order, decide which capture runs first.
    write(spool, '00000000000000000002-1.json', frame('session-end'))
    write(spool, '00000000000000000001-1.json', frame('session-stop'))
    write(spool, '00000000000000000003-1.json', '{"action": "session-stop", ')

    assert core.ingest_spool() == {'ingested': 2, 'pending': 0}

    assert [(provider, action) for provider, action, *_ in client.captured] == [
        ('claude', 'session-stop'), ('claude', 'session-end')]
    assert [payload for _, _, payload, *_ in client.captured] == [PAYLOAD, PAYLOAD]
    assert len(wakes) == 2, 'each queued capture wakes the deliverer'
    assert [path.name for path in spool.iterdir()] == ['00000000000000000003-1.bad']


@pytest.mark.parametrize('action', ['start', 'prompt'])
def test_spool_never_replays_a_context_hook(edge, monkeypatch, action):
    core, client, _ = edge
    ran = []
    monkeypatch.setattr('kelmo.client.hook', lambda *args, **kwargs: ran.append(args))
    write(core.spool_directory(), '00000000000000000001-1.json', frame(action, payload={}))

    assert core.ingest_spool() == {'ingested': 0, 'pending': 0}

    assert ran == [] and client.captured == []
    assert [path.name for path in core.spool_directory().iterdir()] == ['00000000000000000001-1.bad']


# --- handle_hook: the deadline each action gets ------------------------------------------------------------------


def test_start_hook_runs_under_the_context_remaining_time(edge, monkeypatch, tmp_path):
    core, client, _ = edge
    calls = []

    def fake_hook(shared, action, payload, provider=None, **kwargs):
        calls.append((action, provider))
        return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 'synthetic'}}

    monkeypatch.setattr('kelmo.client.hook', fake_hook)
    body = {'action': 'start', 'payload': {}, 'context': {'provider': 'claude', 'cwd': str(tmp_path)}}

    reply = core.handle_hook(Context(12.5), body)

    assert calls == [('start', 'claude')]
    assert client.deadlines == [12.5], 'the context remaining time, not the capture bound'
    assert reply['hookSpecificOutput']['hookEventName'] == 'SessionStart'


def test_capture_hook_keeps_the_short_capture_bound(edge):
    core, client, _ = edge
    body = {'action': 'session-stop', 'payload': dict(PAYLOAD), 'context': {'provider': 'claude'}}

    assert core.handle_hook(Context(12.5), body) == {}

    assert client.deadlines == [HOOK_SECONDS]
    assert [action for _, action, *_ in client.captured] == ['session-stop']


# --- kelmod hook with no daemon listening ------------------------------------------------------------------------


def clean_env(**extra):
    environment = {key: value for key, value in os.environ.items() if key != 'KELMO_INTERNAL_JOB'}
    return {**environment, **extra}


@pytest.fixture
def idle_runtime():
    """A private runtime directory with no socket in it: no daemon is listening at `d.sock`.

    It sits under /tmp so that `d.sock` stays under the Unix socket path limit: the only reason `kelmod hook`
    finds no daemon is that none listens."""
    runtime = Path(tempfile.mkdtemp(prefix='kelmo-hook-', dir='/tmp'))
    yield runtime
    shutil.rmtree(runtime, ignore_errors=True)


def run_hook(kelmod, cwd, args, payload, env):
    started = time.monotonic()
    result = subprocess.run([str(kelmod), 'hook', *args], input=json.dumps(payload), capture_output=True,
                            text=True, cwd=cwd, env=env, timeout=30)
    return result, time.monotonic() - started


def spooled(state):
    directory = state / 'hook-spool'
    return list(directory.iterdir()) if directory.exists() else []


@pytest.mark.parametrize('action', ['stop', 'session-stop'])
def test_capture_without_a_daemon_is_spooled_silently(kelmod, tmp_path, idle_runtime, action):
    # 'stop' is the spec's example; 'session-stop' is the name the installed Stop hook sends.
    state = tmp_path / 'state'
    args = [action, '--provider', 'claude', '--runtime-dir', str(idle_runtime), '--state', str(state)]

    result, seconds = run_hook(kelmod, tmp_path, args, PAYLOAD, clean_env())

    assert result.returncode == 0
    assert (result.stdout, result.stderr) == ('', '')
    assert seconds < 2.0
    [path] = spooled(state)
    assert path.suffix == '.json'
    frame_body = json.loads(path.read_text(encoding='utf-8'))
    assert frame_body['action'] == action
    assert frame_body['payload'] == PAYLOAD
    assert frame_body['context']['provider'] == 'claude'
    assert frame_body['context']['internal_job'] is False
    assert Path(frame_body['context']['cwd']).resolve() == tmp_path.resolve()


def test_internal_job_guard_is_carried_into_the_spooled_frame(kelmod, tmp_path, idle_runtime):
    state = tmp_path / 'state'
    args = ['stop', '--provider', 'claude', '--runtime-dir', str(idle_runtime), '--state', str(state)]

    result, _ = run_hook(kelmod, tmp_path, args, PAYLOAD, clean_env(KELMO_INTERNAL_JOB='1'))

    assert result.returncode == 0
    [path] = spooled(state)
    assert json.loads(path.read_text(encoding='utf-8'))['context']['internal_job'] is True


@pytest.mark.parametrize(('action', 'event'), [('start', 'SessionStart'), ('prompt', 'UserPromptSubmit')])
def test_context_hook_without_a_daemon_prints_the_notice_and_spools_nothing(kelmod, tmp_path, idle_runtime,
                                                                           action, event):
    state = tmp_path / 'state'
    args = [action, '--provider', 'claude', '--runtime-dir', str(idle_runtime), '--state', str(state)]

    result, _ = run_hook(kelmod, tmp_path, args, {}, clean_env())

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['hookSpecificOutput']['hookEventName'] == event
    assert 'E_TRANSPORT' in output['hookSpecificOutput']['additionalContext']
    assert not (state / 'hook-spool').exists()
