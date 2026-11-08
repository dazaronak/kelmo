import json
import tomllib
from pathlib import Path

import pytest

from platform_support import UNSUPPORTED

pytest_plugins = ['test_fact_checks']


def test_integrations_are_idempotent_and_keep_user_settings(tmp_path):
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    home.mkdir()
    config = home / '.claude.json'
    config.write_text(json.dumps({'other': 42}))
    executable = tmp_path / 'runtime/bin/kelmo'
    executable.parent.mkdir(parents=True)
    executable.write_text('synthetic')
    first = configure_clients(home, executable, tmp_path / 'vault', 'both', True)
    assert first['clients'] == ['claude', 'codex']
    configure_clients(home, executable, tmp_path / 'vault', 'both', False)
    assert json.loads(config.read_text())['other'] == 42
    settings = json.loads((home / '.claude/settings.json').read_text())
    assert len(settings['hooks']['SessionStart']) == 1
    assert 'Do not use' not in (home / '.claude/CLAUDE.md').read_text()
    assert (home / '.codex/config.toml').read_text().count('[mcp_servers.kelmo]') == 1
    assert tomllib.loads((home / '.codex/config.toml').read_text())['features']['hooks'] is True


@pytest.mark.parametrize('features', [
    '[features] # keep this comment\nother = true\nhooks = false\n',
    'features = { hooks = false, other = true }\n',
    'features.other = true\nfeatures.hooks = false\n',
    '["features"]\n"hooks" = false\nother = true\n',
    '"features" = { "hooks" = false, other = true }\n',
    '"features"."hooks" = false\n"features".other = true\n',
    'features = { note = "hooks=false", hooks = false, other = true }\n',
])
def test_codex_features_shapes_are_updated_without_losing_other_settings(tmp_path, features):
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('title = "kept"\n' + features)
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    configure_clients(home, executable, tmp_path / 'vault', 'codex', True)

    text = config.read_text()
    parsed = tomllib.loads(text)
    assert parsed['title'] == 'kept'
    assert parsed['features']['other'] is True
    assert parsed['features']['hooks'] is True
    if '# keep this comment' in features:
        assert '# keep this comment' in text


def test_empty_inline_features_table_is_extended(tmp_path):
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('features = {}\n')
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    configure_clients(home, executable, tmp_path / 'vault', 'codex', True)

    assert tomllib.loads(config.read_text())['features'] == {'hooks': True}


def test_unrelated_table_features_are_preserved(tmp_path):
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[other]\nfeatures = { hooks = false }\n')
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    configure_clients(home, executable, tmp_path / 'vault', 'codex', True)

    parsed = tomllib.loads(config.read_text())
    assert parsed['other']['features']['hooks'] is False
    assert parsed['features']['hooks'] is True


def test_multiline_string_lookalikes_are_rejected_without_mutation(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    text = ('message = """\n[features]\nhooks = false\n'
            '# kelmo:begin\n# kelmo:end\n"""\n')
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(text)
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='Unsupported Codex configuration shape'):
        configure_clients(home, executable, tmp_path / 'vault', 'codex', True)

    assert config.read_text() == text
    assert not (home / '.codex/AGENTS.md').exists()


def test_invalid_codex_mcp_table_shape_returns_config_error(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('mcp_servers = []\n')
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='mcp_servers must be a table'):
        configure_clients(home, executable, tmp_path / 'vault', 'codex', True)

    assert not (home / '.codex/AGENTS.md').exists()


@pytest.mark.parametrize('hooks', [
    {'SessionStart': {}},
    {'SessionStart': [None]},
    {'SessionStart': [{'hooks': {}}]},
    {'SessionStart': [{'hooks': [None]}]},
])
def test_invalid_hook_shapes_return_config_error_before_instructions(tmp_path, hooks):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    hook_path = home / '.claude/settings.json'
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text(json.dumps({'hooks': hooks}))
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='hook'):
        configure_clients(home, executable, tmp_path / 'vault', 'claude', True)

    assert not (home / '.claude/CLAUDE.md').exists()


def test_instruction_markers_for_all_clients_are_preflighted_before_writes(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    home.mkdir()
    claude_config = home / '.claude.json'
    claude_config.write_text(json.dumps({'kept': True}))
    codex_instructions = home / '.codex/AGENTS.md'
    codex_instructions.parent.mkdir(parents=True)
    codex_instructions.write_text('<!-- kelmo:begin -->\nambiguous\n')
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='Ambiguous kelmo instruction markers'):
        configure_clients(home, executable, tmp_path / 'vault', 'both', True)

    assert json.loads(claude_config.read_text()) == {'kept': True}
    assert not (home / '.claude/CLAUDE.md').exists()


def test_invalid_client_config_is_rejected_before_installing_instructions(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[broken\n')
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='Invalid TOML client configuration'):
        configure_clients(home, executable, tmp_path / 'vault', 'both', True)

    assert not (home / '.claude/CLAUDE.md').exists()
    assert not (home / '.codex/AGENTS.md').exists()
    assert config.read_text() == '[broken\n'


def test_claude_unmanaged_kelmo_entry_is_not_overwritten(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.install.setup import configure_clients
    home = tmp_path / 'home'
    home.mkdir()
    config = home / '.claude.json'
    existing = {'mcpServers': {'kelmo': {'command': '/custom/memory'}}}
    config.write_text(json.dumps(existing))
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    with pytest.raises(KelmoError, match='unmanaged kelmo'):
        configure_clients(home, executable, tmp_path / 'vault', 'claude', True)

    assert json.loads(config.read_text()) == existing
    assert not (home / '.claude/CLAUDE.md').exists()


def test_concurrent_client_config_update_is_preserved(tmp_path, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.install import setup as setup_module
    home = tmp_path / 'home'
    home.mkdir()
    config = home / '.claude.json'
    config.write_text(json.dumps({'before': True}))
    executable = tmp_path / 'kelmo'
    executable.write_text('synthetic')

    def concurrent_update(*args, **kwargs):
        config.write_text(json.dumps({'before': True, 'concurrent': True}))

    monkeypatch.setattr(setup_module, 'install_instructions', concurrent_update)
    with pytest.raises(KelmoError, match='changed during setup'):
        setup_module.configure_clients(home, executable, tmp_path / 'vault', 'claude', True)

    assert json.loads(config.read_text()) == {'before': True, 'concurrent': True}


def test_context_hook_and_capture_are_real_service_operations(service):
    from kelmo.integration import hook
    result = hook(service, 'start', {})
    assert result['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    result = hook(service, 'capture', {'session_id': 'synthetic', 'last_assistant_message': 'Synthetic evidence.'})
    assert result == {}
    assert service.queue()[0]['state'] == 'captured'
    hook(service, 'capture', {'session_id': 'synthetic', 'last_assistant_message': 'Synthetic evidence.'})
    assert len(service.queue()) == 1


def test_background_service_files_quote_paths(tmp_path):
    from kelmo.install.setup import service_file
    path = service_file(tmp_path, Path('/a path/bin/kelmo'), Path('/vault path'), 'linux', None)
    text = path.read_text()
    assert '"/a path/bin/kelmo"' in text
    assert '"/vault path"' in text


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_setup_provisions_model_and_profiles_without_claiming_started(service, tmp_path, monkeypatch):
    import hashlib
    import sys
    from kelmo import model_store
    from kelmo.install.setup import setup
    from kelmo.interface import build_parser
    from kelmo.store.memory import embed
    source = tmp_path / 'model'
    source.mkdir()
    data = b'Synthetic model bytes'
    (source / 'model.onnx').write_bytes(data)
    monkeypatch.setattr(model_store, 'FILES', {'model.onnx': (len(data), hashlib.sha256(data).hexdigest())})
    monkeypatch.setattr(embed, 'embed', lambda *a, **k: bytes(1536))
    executor = tmp_path / 'executor.py'
    executor.write_text('print(\'{"ok":true}\')')
    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': [sys.executable, str(executor)]}))
    args = build_parser('')[0].parse_args(['setup', '--vault', str(service.root), '--home', str(tmp_path / 'home'),
        '--executable', sys.executable, '--model-source', str(source), '--executor-config', str(config),
        '--disable-native-memory', '--no-start'])
    result = setup(args)
    assert result['memory_ready'] is True
    assert result['executor_ready'] is True
    assert result['background_started'] is False
    assert result['complete'] is False
    assert (service.root / '.cache/embeddings/model/model.onnx').read_bytes() == data
    assert setup(args)['memory_ready'] is True


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_setup_survives_a_provider_probe_that_rewrites_the_client_config(service, tmp_path, monkeypatch):
    import sys
    from kelmo.install.setup import setup
    from kelmo.interface import build_parser
    from kelmo.store.memory import embed
    monkeypatch.setattr(embed, 'embed', lambda *a, **k: bytes(1536))
    home = tmp_path / 'home'
    home.mkdir()
    client_config = home / '.claude.json'
    client_config.write_text('{"numStartups": 1}')
    executor = tmp_path / 'executor.py'
    executor.write_text('import json, pathlib\n'
                        f'path = pathlib.Path({str(client_config)!r})\n'
                        'path.write_text(json.dumps({"numStartups": 2}))\n'
                        'print(\'{"ok":true}\')\n')
    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': [sys.executable, str(executor)]}))
    args = build_parser('')[0].parse_args(['setup', '--vault', str(service.root), '--home', str(home),
        '--executable', sys.executable, '--skip-model', '--executor-config', str(config),
        '--client', 'claude', '--disable-native-memory', '--no-start'])
    result = setup(args)
    assert result['executor_ready'] is True
    written = json.loads(client_config.read_text())
    assert written['numStartups'] == 2 and 'kelmo' in written['mcpServers']


@pytest.mark.parametrize('already_running', [False, True])
def test_linux_setup_activates_new_service_definition(tmp_path, monkeypatch, already_running):
    import sys
    from types import SimpleNamespace
    from kelmo.install import setup as installer
    from kelmo.interface import build_parser

    vault = tmp_path / 'vault'
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    home = tmp_path / 'home'
    unit = home / '.config/systemd/user/kelmo.service'
    state = {'running': 'old definition' if already_running else None, 'loaded': None,
             'enabled': False}
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True})
    monkeypatch.setattr(installer.sys, 'platform', 'linux')

    def systemctl(command, **kwargs):
        assert command[:2] == ['systemctl', '--user']
        action = command[2]
        if action == 'daemon-reload':
            state['loaded'] = unit.read_text()
        elif action == 'enable':
            state['enabled'] = True
            if '--now' in command and state['running'] is None:
                state['running'] = state['loaded']
        elif action == 'restart':
            state['running'] = state['loaded']
        else:
            pytest.fail(f'Unexpected service operation: {action}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.subprocess, 'run', systemctl)
    args = build_parser('')[0].parse_args([
        'setup', '--vault', str(vault), '--home', str(home),
        '--executable', sys.executable, '--disable-native-memory',
    ])

    result = installer.setup(args)

    assert result['background_started'] is True
    assert state['enabled'] is True
    assert state['running'] == unit.read_text()


@pytest.mark.parametrize('failure', ['daemon-reload', 'enable', 'restart', None])
def test_linux_activation_failure_retains_configuration_and_stops(tmp_path, monkeypatch, failure):
    import sys
    from types import SimpleNamespace
    from kelmo.errors import KelmoError
    from kelmo.install import setup as installer
    from kelmo.interface import build_parser

    vault = tmp_path / 'vault'
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    home = tmp_path / 'home'
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True})
    monkeypatch.setattr(installer.sys, 'platform', 'linux')
    actions = []

    def systemctl(command, **kwargs):
        assert command[:2] == ['systemctl', '--user']
        actions.append(command[2])
        return SimpleNamespace(returncode=1 if command[2] == failure else 0)

    monkeypatch.setattr(installer.subprocess, 'run', systemctl)
    args = build_parser('')[0].parse_args([
        'setup', '--vault', str(vault), '--home', str(home),
        '--executable', sys.executable, '--disable-native-memory',
    ] + (['--no-start'] if failure is None else []))

    if failure is None:
        result = installer.setup(args)
        assert result['background_started'] is False
        assert result['complete'] is False
        assert actions == []
    else:
        with pytest.raises(KelmoError) as raised:
            installer.setup(args)
        assert raised.value.code == 'E_SETUP'
        assert 'configuration was retained' in str(raised.value)
        order = ['daemon-reload', 'enable', 'restart']
        assert actions == order[:order.index(failure) + 1]
    assert (home / '.config/systemd/user/kelmo.service').is_file()
    assert (home / '.claude.json').is_file()
    assert (home / '.codex/config.toml').is_file()


def test_upgrade_backups_restore_previous_client_generation_with_user_edits(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    old_executable = tmp_path / 'runtime-old/bin/kelmo'
    new_executable = tmp_path / 'runtime-new/bin/kelmo'
    vault = tmp_path / 'vault'
    configure_clients(home, old_executable, vault, 'both', True)
    claude_config = home / '.claude.json'
    data = json.loads(claude_config.read_bytes())
    data['user_added_setting'] = {'preserve': True}
    claude_config.write_text(json.dumps(data, indent=4) + '\n')
    codex_config = home / '.codex/config.toml'
    with codex_config.open('a') as output:
        output.write('\n[user_settings]\npreserve = true\n')
    for relative in ('.claude/CLAUDE.md', '.codex/AGENTS.md'):
        with (home / relative).open('ab') as output:
            output.write(b'\nUser instructions after installation.\r\n')
    managed = [home / relative for relative in (
        '.claude.json', '.claude/settings.json', '.claude/CLAUDE.md',
        '.claude/kelmo/ownership.json', '.codex/config.toml',
        '.codex/hooks.json', '.codex/AGENTS.md',
        '.codex/kelmo/ownership.json',
    )]
    previous = {path: path.read_bytes() for path in managed}

    result = configure_clients(home, new_executable, vault, 'both', False)

    backups = {Path(value.split('.bak.')[0]): Path(value) for value in result['backups']}
    assert backups.keys() == previous.keys()
    for path, content in previous.items():
        assert backups[path].read_bytes() == content
        path.write_bytes(backups[path].read_bytes())
    assert {path: path.read_bytes() for path in managed} == previous
    repaired = configure_clients(home, old_executable, vault, 'both', True)
    assert len(repaired['backups']) == 1
    assert Path(repaired['backups'][0]).read_bytes() == previous[claude_config]
    assert configure_clients(home, old_executable, vault, 'both', True)['backups'] == []
    upgraded = configure_clients(home, new_executable, vault, 'both', False)
    assert upgraded['clients'] == ['claude', 'codex']
    assert json.loads(claude_config.read_bytes())['user_added_setting'] == {'preserve': True}
    assert tomllib.loads(codex_config.read_text())['user_settings']['preserve'] is True
