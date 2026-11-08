import pytest
import io
import json
import sys


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_native_cli_adapter_decodes_actual_subprocess_output(tmp_path, monkeypatch, provider):
    from kelmo.executor import resolve
    executable = tmp_path / provider
    executable.write_text('''#!/usr/bin/env python3
import json,sys
p=json.load(sys.stdin)
assert p['input']['task']=='probe'
if '--output-last-message' in sys.argv:
    assert 'model_reasoning_effort="high"' in sys.argv
    from pathlib import Path
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{"ok":true}')
else:
    assert sys.argv[sys.argv.index('--effort')+1]=='high'
    print(json.dumps({'result':'{"ok":true}'}))
''')
    executable.chmod(0o755)
    monkeypatch.setattr('kelmo.executor.shutil.which', lambda _: str(executable))
    assert resolve(provider, 'synthetic-model', {'task': 'probe'}, effort='high') == {'ok': True}


def test_custom_adapter_receives_contract_and_returns_reconciliation():
    from kelmo.executor import resolve
    script = ('import json,sys; r=json.load(sys.stdin); '
              'assert r["protocol"]=="kelmo-executor/1" and r["model"] is None; '
              'assert r["task"]=="edit_pages" and r["output_schema"] and r["effort"]=="high"; '
              'print(json.dumps({"kind":"reconcile","result":{"facts":'
              '[{"text":"Uses SQLite.","target":"projects/app/app.md"}]}}))')
    assert resolve('custom', None, {'task': 'edit_pages'},
                   command=[sys.executable, '-c', script], effort='high') == {
                       'reconcile': [{'text': 'Uses SQLite.', 'target': 'projects/app/app.md'}]}


def test_custom_quota_survives_the_cli_adapter(monkeypatch, capsys):
    import time
    from kelmo.cli import main
    reset = time.time() + 120
    script = 'print(' + repr(json.dumps({'kind': 'deferred', 'reset_at': reset})) + ')'
    monkeypatch.setattr('sys.stdin', io.StringIO('{"task":"probe"}'))
    assert main(['executor', '--provider', 'custom', '--command-json',
                 json.dumps([sys.executable, '-c', script])]) == 0
    assert json.loads(capsys.readouterr().out) == {'outcome': 'quota_wait', 'reset_at': reset}
    from kelmo.errors import KelmoError
    from kelmo.executor import decode_custom_outcome
    with pytest.raises(KelmoError, match='Model output cannot declare'):
        decode_custom_outcome({'kind': 'proposal', 'result': {'outcome': 'quota_wait', 'reset_at': reset}})


# What the Claude CLI prints, with exit 1, when the account reaches its usage limit.
USAGE_LIMIT = {'type': 'result', 'subtype': 'success', 'is_error': True, 'terminal_reason': 'api_error',
               'api_error_status': 429, 'api_error': 'usage_limit_reached', 'num_turns': 1,
               'result': "You've hit your session limit · resets 5pm (Europe/Berlin)"}


def failing_claude(tmp_path, reply):
    executable = tmp_path / 'claude-fixture'
    executable.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('
                          + repr(json.dumps(reply)) + ')\nsys.exit(1)\n')
    executable.chmod(0o755)
    return str(executable)


def test_a_usage_limit_defers_to_the_reset_its_message_names(tmp_path, monkeypatch, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from kelmo.cli import main
    from kelmo.executor import provider_wait
    from kelmo.quota import QuotaWait
    from kelmo.executor import resolve
    berlin = ZoneInfo('Europe/Berlin')
    before = datetime(2026, 10, 28, 15, 10, tzinfo=berlin).timestamp()
    assert provider_wait(USAGE_LIMIT, now=before) == (datetime(2026, 10, 28, 17, 1, tzinfo=berlin).timestamp(), 'limit')
    after = datetime(2026, 10, 28, 18, 0, tzinfo=berlin).timestamp()
    assert provider_wait(USAGE_LIMIT, now=after)[0] == datetime(2026, 10, 29, 17, 1, tzinfo=berlin).timestamp()
    at_reset = datetime(2026, 10, 28, 17, 0, 30, tzinfo=berlin).timestamp()
    assert provider_wait(USAGE_LIMIT, now=at_reset)[0] == at_reset + 86400 - 60, 'the worker accepts at most a day'
    executable = failing_claude(tmp_path, USAGE_LIMIT)
    with pytest.raises(QuotaWait) as waited:
        resolve('claude', 'synthetic', {'task': 'probe'}, executable=executable)
    assert waited.value.reason == 'limit'
    # The worker reads this answer and waits without spending an attempt.
    monkeypatch.setattr('sys.stdin', io.StringIO('{"task":"probe"}'))
    assert main(['executor', '--provider', 'claude', '--model', 'synthetic',
                 '--provider-executable', executable]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer['outcome'] == 'quota_wait' and answer['reason'] == 'limit' and answer['reset_at'] > before


def test_a_lost_login_waits_and_other_errors_still_fail(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.executor import provider_wait, resolve
    now = 1_800_000_000
    login = dict(USAGE_LIMIT, api_error_status=401, api_error='authentication_failed',
                 result='Failed to authenticate: OAuth session expired and could not be refreshed')
    assert provider_wait(login, now=now) == (now + 900, 'login')
    unreadable = dict(USAGE_LIMIT, result='Usage limit reached')
    assert provider_wait(unreadable, now=now) == (now + 900, 'limit'), 'a limit without a readable reset is polled'
    # The model's own words never defer: no API error from the CLI, no wait.
    spoken = {'type': 'result', 'is_error': True, 'result': 'You hit your session limit, resets 5pm'}
    assert provider_wait(spoken, now=now) is None
    other = dict(USAGE_LIMIT, api_error_status=500, api_error='overloaded', result='Internal server error')
    assert provider_wait(other, now=now) is None
    with pytest.raises(KelmoError, match='reported an error'):
        resolve('claude', 'synthetic', {'task': 'probe'}, executable=failing_claude(tmp_path, other))


def test_claude_readiness_uses_validated_structured_output(tmp_path):
    from kelmo.executor import resolve
    executable = tmp_path / 'claude-fixture'
    executable.write_text('''#!/usr/bin/env python3
import json,sys
request=json.load(sys.stdin)
assert request['input']['task']=='probe'
schema=json.loads(sys.argv[sys.argv.index('--json-schema')+1])
assert schema['required']==['ok']
assert schema['properties']['ok']['type']=='boolean'
assert schema['additionalProperties'] is False
print(json.dumps({'result':'', 'structured_output':{'ok':True}}))
''')
    executable.chmod(0o755)
    assert resolve('claude', 'synthetic', {'task': 'probe'}, executable=str(executable)) == {'ok': True}


def test_executor_preserves_a_full_intake_source_after_json_escaping(monkeypatch, capsys):
    from kelmo.cli import main
    source = '\x00' * 1048000
    payload = {'task': 'resolve_facts', 'source': source, 'changes': [], 'facts': {}}
    received = []

    def resolve(provider, model, value, *, timeout, **kwargs):
        received.append((value, timeout))
        return {'unresolved': True}

    monkeypatch.setattr('kelmo.executor.resolve', resolve)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert main(['executor', '--provider', 'claude', '--model', 'synthetic',
                 '--timeout', '170', '--json']) == 0
    assert received == [(payload, 170)]
    assert json.loads(capsys.readouterr().out) == {'unresolved': True}


def test_executor_rejects_requests_above_transport_limit_before_model_call(monkeypatch, capsys):
    from kelmo.cli import main
    called = []
    monkeypatch.setattr('kelmo.executor.resolve', lambda *args: called.append(args))
    monkeypatch.setattr('sys.stdin', io.StringIO(' ' * (8 * 1048576 + 1)))
    assert main(['executor', '--provider', 'claude', '--model', 'synthetic', '--json']) != 0
    assert called == []
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_INPUT'


def test_pinned_provider_executable_does_not_depend_on_background_path(tmp_path, monkeypatch):
    from kelmo.executor import resolve
    executable = tmp_path / 'provider'
    executable.write_text('#!/usr/bin/env python3\nimport json\n'
                          'print(json.dumps({"result": "{\\"ok\\":true}"}))\n')
    executable.chmod(0o755)
    monkeypatch.setattr('kelmo.executor.shutil.which', lambda _: None)
    assert resolve('claude', 'synthetic-model', {'task': 'probe'},
                   executable=str(executable)) == {'ok': True}


@pytest.mark.parametrize('reply', [{}, {'result': None}, {'result': []}, {'result': 'not-json'}])
def test_malformed_provider_envelope_is_an_executor_error(monkeypatch, reply):
    from kelmo.errors import KelmoError
    from kelmo.executor import resolve
    monkeypatch.setattr('kelmo.executor.execute', lambda *args, **kwargs: reply)
    with pytest.raises(KelmoError) as caught:
        resolve('claude', 'synthetic-model', {}, executable=sys.executable)
    assert caught.value.code == 'E_EXECUTOR'


@pytest.mark.parametrize('timeout', [0, 601, float('nan'), float('inf'), True])
def test_resolve_rejects_invalid_timeout_before_provider_launch(monkeypatch, timeout):
    from kelmo.errors import KelmoError
    from kelmo.executor import resolve
    called = []
    monkeypatch.setattr('kelmo.executor.execute', lambda *args, **kwargs: called.append(True))
    with pytest.raises(KelmoError) as caught:
        resolve('claude', 'synthetic-model', {}, executable=sys.executable, timeout=timeout)
    assert caught.value.code == 'E_EXECUTOR'
    assert called == []


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_resolve_propagates_timeout_to_provider_execute(monkeypatch, provider):
    from kelmo.executor import resolve
    calls = []

    def execute(*args, **kwargs):
        calls.append(kwargs['timeout'])
        if kwargs.get('raw_output'):
            from pathlib import Path
            argv = args[0]
            Path(argv[argv.index('--output-last-message') + 1]).write_text('{"ok":true}')
            return ''
        return {'result': '{"ok":true}'}

    monkeypatch.setattr('kelmo.executor.execute', execute)
    assert resolve(provider, 'synthetic', {}, executable=sys.executable, timeout=170) == {'ok': True}
    assert calls == [170]


def test_nested_provider_group_is_cleaned_before_outer_timeout(tmp_path):
    import time
    from kelmo.errors import KelmoError
    from kelmo.executor import resolve
    marker = tmp_path / 'nested-child-survived'
    child = f"import pathlib,time;time.sleep(0.8);pathlib.Path({str(marker)!r}).write_text('alive')"
    provider = tmp_path / 'provider'
    provider.write_text("#!/usr/bin/env python3\nimport subprocess,sys,time\n"
                        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                        "time.sleep(30)\n")
    provider.chmod(0o755)
    with pytest.raises(KelmoError):
        resolve('claude', 'synthetic', {}, executable=str(provider), timeout=0.2)
    time.sleep(1)
    assert not marker.exists()


def test_native_executor_sigterm_cleans_nested_provider_group(tmp_path):
    import subprocess
    import time
    marker = tmp_path / 'signal-child-survived'
    started = tmp_path / 'provider-started'
    child = f"import pathlib,time;time.sleep(0.8);pathlib.Path({str(marker)!r}).write_text('alive')"
    provider = tmp_path / 'provider'
    provider.write_text("#!/usr/bin/env python3\nimport pathlib,subprocess,sys,time\n"
                        f"pathlib.Path({str(started)!r}).write_text('ready')\n"
                        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                        "time.sleep(30)\n")
    provider.chmod(0o755)
    process = subprocess.Popen(
        [sys.executable, '-m', 'kelmo.cli', 'executor', '--provider', 'claude',
         '--model', 'synthetic', '--provider-executable', str(provider), '--timeout', '30', '--json'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write('{"task":"probe"}')
        process.stdin.close()
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        process.terminate()
        assert process.wait(timeout=2) != 0
        time.sleep(1)
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_outer_timeout_allows_native_wrapper_to_clean_nested_provider(tmp_path):
    import time
    from pathlib import Path
    from kelmo.daemon import execute
    from kelmo.errors import KelmoError
    marker = tmp_path / 'outer-timeout-child-survived'
    started = tmp_path / 'outer-provider-started'
    child = ("import pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
             f"pathlib.Path({str(started)!r}).write_text('ready');"
             f"time.sleep(3);pathlib.Path({str(marker)!r}).write_text('alive')")
    provider = tmp_path / 'provider'
    provider.write_text("#!/usr/bin/env python3\nimport pathlib,signal,subprocess,sys,time\n"
                        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                        "time.sleep(30)\n")
    provider.chmod(0o755)
    source = Path(__file__).resolve().parents[1] / 'src'
    wrapper = (f"import sys;sys.path.insert(0,{str(source)!r})\n"
               "from kelmo.executor import resolve\n"
               f"resolve('claude','synthetic',{{'task':'probe'}},executable={str(provider)!r},timeout=30)\n")
    wrapper_path = tmp_path / 'native-wrapper.py'
    wrapper_path.write_text(wrapper)
    with pytest.raises(KelmoError):
        execute([sys.executable, str(wrapper_path)], {'task': 'probe'}, timeout=2)
    assert started.exists()
    time.sleep(3.2)
    assert not marker.exists()


def test_executor_exit_reason_keeps_the_executors_own_words():
    from kelmo.daemon import _exit_reason
    envelope = b'{"schema_version":1,"ok":false,"error":{"code":"E_EXECUTOR","message":"Executor timed out."}}'
    assert _exit_reason(1, bytearray(envelope), bytearray()) == 'Executor exited 1: E_EXECUTOR: Executor timed out.'
    assert (_exit_reason(1, bytearray(), bytearray(b'warning\nE_EXECUTOR: The configured CLI reported an error.\n'))
            == 'Executor exited 1: E_EXECUTOR: The configured CLI reported an error.')
    assert _exit_reason(137, bytearray(b'not json'), bytearray()) == 'Executor exited 137: no diagnostic output'
