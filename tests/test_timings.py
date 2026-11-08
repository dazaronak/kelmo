"""The opt-in stage timings: the flag, the environment switch, and what the one stderr line names."""
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'src'


def environment(extra=None):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.update(extra or {})
    return {key: value for key, value in env.items() if value is not None}


def run_cli(*argv, env=None):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', *map(str, argv)],
                          input='', capture_output=True, text=True, timeout=90, check=False,
                          env=environment(env))


def timings_of(result):
    """The one timings line a call wrote, or None when it wrote none."""
    lines = [line for line in result.stderr.splitlines() if '"timings"' in line]
    assert len(lines) <= 1, result.stderr
    return json.loads(lines[0]) if lines else None


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / 'vault'
    created = run_cli('init', '--vault', root, '--label', 'fixture', '--json', '--non-interactive')
    assert created.returncode == 0, created.stdout + created.stderr
    return root


def test_timings_go_to_stderr_and_leave_the_answer_untouched(vault):
    result = run_cli('search', 'basalt', '--vault', vault, '--limit', '3', '--json', '--timings')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['ok'] is True
    report = timings_of(result)
    assert report is not None
    assert report['schema_version'] == 1
    stages = report['timings']
    assert stages['command'] == 'search'
    assert stages['parse_ms'] >= 0 and stages['engine_import_ms'] >= 0 and stages['operation_ms'] >= 0
    assert stages['process_ms'] >= stages['operation_ms']


def test_no_line_is_written_when_nobody_asked(vault):
    assert timings_of(run_cli('search', 'basalt', '--vault', vault, '--json')) is None
    assert timings_of(run_cli('status', '--vault', vault, '--json')) is None


def test_the_environment_switch_measures_every_call_of_a_session(vault):
    for value in ('1', 'true', 'YES', 'on'):
        report = timings_of(run_cli('search', 'basalt', '--vault', vault, '--json',
                                    env={'KELMO_TIMINGS': value}))
        assert report is not None, value
    for value in ('0', 'false', '', '  '):
        quiet = run_cli('status', '--vault', vault, '--json', env={'KELMO_TIMINGS': value})
        assert timings_of(quiet) is None, value


def test_the_flag_works_before_and_after_the_command_name(vault):
    for argv in (('--timings', 'status', '--vault', vault, '--json'),
                 ('status', '--timings', '--vault', vault, '--json')):
        report = timings_of(run_cli(*argv))
        assert report is not None, argv
        assert report['timings']['command'] == 'status'


def test_a_call_that_loaded_no_model_names_no_model_load(vault):
    for argv in (('status',), ('search', 'basalt')):
        stages = timings_of(run_cli(*argv, '--vault', vault, '--json', '--timings'))['timings']
        assert 'model_load_ms' not in stages, argv


def test_a_failing_call_still_reports_its_stages(vault):
    result = run_cli('show', 'not-a-record', '--vault', vault, '--json', '--timings')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'E_NOT_FOUND'
    assert timings_of(result)['timings']['command'] == 'show'


def test_the_helper_writes_one_line_and_then_stops():
    from kelmo import timings

    call = timings.start('search', reporting=True)
    with call.stage('parse'):
        pass
    stream = io.StringIO()
    written = timings.report(stream=stream)
    assert written['command'] == 'search'
    assert written['parse_ms'] >= 0
    assert list(written) == ['command', 'parse_ms', 'process_ms']
    assert stream.getvalue().count('\n') == 1
    assert json.loads(stream.getvalue())['timings'] == written
    assert timings.report(stream=io.StringIO()) is None

    timings.start('status', reporting=True)
    timings.disarm()
    assert timings.report(stream=io.StringIO()) is None


def fake_embedder(monkeypatch, load_ms):
    """Stand in for the embedding module, so a test can say what the process had already loaded."""
    state = {'load_ms': load_ms}
    monkeypatch.setitem(sys.modules, 'kelmo.store.memory.embed',
                        types.SimpleNamespace(model_load_ms=lambda: state['load_ms']))
    return state


def test_the_call_that_pays_for_the_model_load_names_it(monkeypatch):
    from kelmo import timings

    state = fake_embedder(monkeypatch, None)
    timings.start('search', reporting=True)
    state['load_ms'] = 452.0  # the load happened inside this call.
    assert timings.report(stream=io.StringIO())['model_load_ms'] == 452.0


def test_a_call_in_a_process_that_was_already_warm_names_no_load(monkeypatch):
    """The number belongs to whoever paid for it: a warm process reports no load per call, and a test that
    warmed the model earlier in the same process does not lend its load to the next call."""
    from kelmo import timings

    fake_embedder(monkeypatch, 452.0)
    timings.start('search', reporting=True)
    assert 'model_load_ms' not in timings.report(stream=io.StringIO())
    assert timings.model_load_ms() == 452.0


def test_the_vault_named_by_environment_is_used_and_a_flag_overrides_it(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    for root in (first, second):
        started = run_cli('init', '--vault', root, '--label', root.name, '--json', '--non-interactive')
        assert started.returncode == 0, started.stdout + started.stderr
    from_environment = run_cli('status', '--json', env={'KELMO_VAULT': str(first)})
    with_flag = run_cli('status', '--vault', str(second), '--json', env={'KELMO_VAULT': str(first)})
    assert from_environment.returncode == 0, from_environment.stdout + from_environment.stderr
    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
    assert json.loads(from_environment.stdout)['result']['vault_id'] != \
        json.loads(with_flag.stdout)['result']['vault_id']
