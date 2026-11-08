"""The terminal questions of `kelmo setup` and the provider account directory they lead to."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kelmo.errors import KelmoError
from kelmo.install import wizard
from kelmo.install.providers import arguments


def answers(*values):
    queue = list(values)
    return lambda prompt: queue.pop(0)


def which_only(*names):
    return lambda name: '/usr/bin/' + name if name in names else None


def blank_args(home):
    return SimpleNamespace(home=home, provider=None, model=None, provider_home=None, executor_config=None,
                           non_interactive=False)


def test_choice_of_a_second_claude_account_sets_its_directory(tmp_path):
    work = tmp_path / '.claude-second'
    work.mkdir()
    (work / '.claude.json').write_text('{}')
    (tmp_path / '.claude-empty').mkdir()  # no account files: not offered
    args = blank_args(tmp_path)
    out = io.StringIO()
    assert wizard.choose(args, ask=answers('1', '', '2'), out=out, which=which_only('claude', 'codex'))
    assert (args.provider, args.model, args.provider_home) == ('claude', 'claude-haiku-4-5', work)
    assert 'billed' in out.getvalue() and '.claude-empty' not in out.getvalue()


def test_single_account_is_not_asked_and_codex_needs_a_typed_model(tmp_path):
    args = blank_args(tmp_path)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return {1: '2', 2: '', 3: 'gpt-synthetic'}[len(asked)]

    assert wizard.choose(args, ask=ask, out=io.StringIO(), which=which_only('claude', 'codex'))
    assert (args.provider, args.model, args.provider_home) == ('codex', 'gpt-synthetic', None)
    assert len(asked) == 3


def test_skip_leaves_the_executor_unset_and_names_missing_clis(tmp_path):
    args = blank_args(tmp_path)
    out = io.StringIO()
    assert wizard.choose(args, ask=answers('9', '1'), out=out, which=which_only()) is False
    assert args.provider is None
    assert 'Neither Claude Code' in out.getvalue() and 'Enter a number from 1 to 1' in out.getvalue()


def test_scripts_and_explicit_choices_are_never_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True, raising=False)
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: True, raising=False)
    assert wizard.wanted(blank_args(tmp_path))
    assert not wizard.wanted(SimpleNamespace(**dict(vars(blank_args(tmp_path)), provider='claude')))
    assert not wizard.wanted(SimpleNamespace(**dict(vars(blank_args(tmp_path)), non_interactive=True)))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False, raising=False)
    assert not wizard.wanted(blank_args(tmp_path))


def test_running_executable_is_the_kelmo_beside_the_interpreter(tmp_path, monkeypatch):
    (tmp_path / 'python').write_text('')
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python'))
    with pytest.raises(KelmoError):
        wizard.running_executable()
    launcher = tmp_path / 'kelmo'
    launcher.write_text('#!/bin/sh\n')
    launcher.chmod(0o755)
    assert wizard.running_executable() == launcher


def test_provider_home_reaches_the_executor_command(tmp_path):
    home = tmp_path / '.claude-second'
    home.mkdir()
    claude = tmp_path / 'claude'
    claude.write_text('#!/bin/sh\n')
    claude.chmod(0o755)
    args = SimpleNamespace(provider='claude', model='claude-haiku-4-5', provider_executable=claude,
                           provider_command_json=None, executor_config=None, provider_home=home, effort=None)
    command = arguments(args, Path('/opt/kelmo/bin/kelmo'), check_provider=True)
    assert command[command.index('--provider-home') + 1] == str(home.resolve())
    args.provider_home = tmp_path / 'missing'
    with pytest.raises(KelmoError):
        arguments(args, Path('/opt/kelmo/bin/kelmo'))
    args.provider, args.provider_home = 'custom', home
    with pytest.raises(KelmoError):
        arguments(args, Path('/opt/kelmo/bin/kelmo'))


def test_executor_runs_the_provider_with_the_account_variable(tmp_path, monkeypatch):
    from kelmo import executor
    from kelmo.cli import dispatch
    from kelmo.interface import build_parser
    seen = {}

    def fake_resolve(*a, **k):
        import os
        seen['dir'] = os.environ.get('CLAUDE_CONFIG_DIR')
        return {'ok': True}

    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setattr(executor, 'resolve', fake_resolve)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'task': 'probe'})))
    parser, commands = build_parser('')
    args = parser.parse_args(['executor', '--provider', 'claude', '--model', 'claude-haiku-4-5',
                              '--provider-home', str(tmp_path)])
    assert dispatch(args, commands) == {'ok': True}
    assert seen['dir'] == str(tmp_path.resolve())
