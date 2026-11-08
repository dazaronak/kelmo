import json
import importlib.util
import http.client
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from kelmo.errors import KelmoError
from kelmo.interface import build_parser


def setup_args(tmp_path, *extra):
    return build_parser('')[0].parse_args([
        'setup', '--vault', str(tmp_path / 'vault'), '--home', str(tmp_path / 'home'),
        '--executable', sys.executable, '--disable-native-memory', '--no-start', *extra,
    ])


def test_atomic_write_preserves_existing_file_mode(tmp_path):
    from kelmo.install.setup import write

    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    path.chmod(0o640)

    write(path, b'new')

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_pinned_sandbox_install_previews_and_backs_up(tmp_path, monkeypatch):
    import hashlib
    from kelmo.install.sandbox import provision
    monkeypatch.setattr(sys, 'platform', 'linux')
    source, prefix = tmp_path / 'bwrap', tmp_path / 'runtime'
    source.write_bytes(b'synthetic sandbox binary')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = provision(prefix, source, digest)
    assert not prefix.exists()
    target = Path(plan['path'])
    target.parent.mkdir(parents=True)
    target.write_bytes(b'previous')
    result = provision(prefix, source, digest, apply=True)
    assert target.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert Path(result['backup']).read_bytes() == b'previous'
    with pytest.raises(KelmoError, match='SHA-256'):
        provision(prefix, source, '0' * 64, apply=True)


def test_runtime_backup_preserves_files_modes_and_links_without_following(tmp_path):
    from kelmo.install.runtime_backup import backup_runtime
    prefix = tmp_path / 'runtime'
    prefix.mkdir()
    (prefix / '.kelmo-runtime').write_bytes(b'kelmo\n')
    (prefix / 'binary').write_bytes(b'original executable')
    (prefix / 'binary').chmod(0o755)
    (prefix / 'python').symlink_to('/synthetic/unavailable/python')
    saved = backup_runtime(prefix)
    copy = Path(saved['runtime'])
    assert (copy / 'binary').read_bytes() == b'original executable'
    assert stat.S_IMODE((copy / 'binary').stat().st_mode) == 0o755
    assert os.readlink(copy / 'python') == '/synthetic/unavailable/python'
    assert stat.S_IMODE(Path(saved['path']).stat().st_mode) == 0o700
    assert json.loads((Path(saved['path']) / 'manifest.json').read_bytes())['original'] == str(prefix)


def test_atomic_write_fsyncs_parent_directory_after_replace(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    directories = []
    real_fsync = os.fsync

    def record(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(setup_module.os, 'fsync', record)
    setup_module.write(tmp_path / 'settings.json', b'new')

    assert directories


@pytest.mark.parametrize('extra', [
    ('--model', 'synthetic'),
    ('--provider', 'codex'),
    ('--provider', 'codex', '--model', 'synthetic', '--executor-config', 'executor.json'),
])
def test_invalid_executor_option_combinations_fail_before_provisioning(tmp_path, monkeypatch, extra):
    from kelmo.install import setup as setup_module

    initialized = []
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: initialized.append(True))

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path, *extra))

    assert raised.value.code == 'E_CONFIG'
    assert initialized == []


def test_doctor_failure_names_only_failed_check_keys(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {
        'healthy': False,
        'checks': {'archive_integrity': False, 'links': True, 'private': 'do-not-print'},
    })

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path))

    assert raised.value.code == 'E_SETUP'
    assert str(raised.value) == 'Memory health checks failed: archive_integrity.'
    assert 'do-not-print' not in str(raised.value)


def test_custom_executor_config_is_validated_before_probe(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': ['relative'], 'timeout_seconds': 'slow'}))
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True, 'checks': {}})
    invoked = []
    monkeypatch.setattr('kelmo.daemon.execute', lambda *a, **k: invoked.append(True))

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path, '--executor-config', str(config)))

    assert raised.value.code == 'E_CONFIG'
    assert invoked == []


def test_probe_failure_names_a_missing_provider_login(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': [sys.executable], 'timeout_seconds': 30}))
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True, 'checks': {}})
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda *a, **k: {'outcome': 'quota_wait', 'reset_at': 1.0, 'reason': 'login'})

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path, '--executor-config', str(config)))

    assert raised.value.code == 'E_SETUP'
    assert 'not logged in' in str(raised.value)


def test_spaced_features_header_is_supported(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('  [ features ] # retained\nhooks = false\n')

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True)

    assert '  [ features ] # retained' in config.read_text()
    assert 'hooks = true' in config.read_text()


def test_claude_owned_mcp_entry_can_move_to_new_runtime_and_vault(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'old-vault', 'claude', True)
    configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)

    entry = json.loads((home / '.claude.json').read_text())['mcpServers']['kelmo']
    assert entry == {'command': str(tmp_path / 'new-kelmo'),
                     'args': ['mcp', '--vault', str(tmp_path / 'new-vault'), '--reconnect']}


def test_claude_owned_mcp_entry_refuses_foreign_edit_after_install(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'old-vault', 'claude', True)
    config = json.loads((home / '.claude.json').read_text())
    config['mcpServers']['kelmo']['command'] = '/foreign/runtime'
    (home / '.claude.json').write_text(json.dumps(config))

    with pytest.raises(KelmoError, match='owned MCP entry changed'):
        configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)

    assert json.loads((home / '.claude.json').read_text()) == config


def test_user_empty_hook_groups_are_preserved(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    settings = home / '.claude/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'hooks': {'SessionStart': [
        {'matcher': 'keep-missing'}, {'matcher': 'keep-empty', 'hooks': []},
    ]}}))

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)

    groups = json.loads(settings.read_text())['hooks']['SessionStart']
    assert {'matcher': 'keep-missing'} in groups
    assert {'matcher': 'keep-empty', 'hooks': []} in groups


def test_client_preflight_happens_before_vault_provisioning(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    config = tmp_path / 'home/.claude.json'
    config.parent.mkdir(parents=True)
    config.write_text('{broken')
    initialized = []
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: initialized.append(True))

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path, '--client', 'claude'))

    assert raised.value.code == 'E_CONFIG'
    assert initialized == []


def test_model_download_failure_has_offline_recovery_hint(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    monkeypatch.setattr(setup_module.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('private provider detail')))
    with pytest.raises(KelmoError) as raised:
        setup_module.fetch_model(tmp_path)

    assert raised.value.code == 'E_SETUP'
    assert '--model-source' in str(raised.value)
    assert 'private provider detail' not in str(raised.value)


def test_native_executor_config_pins_provider_executable(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    vault = tmp_path / 'vault'
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    provider = tmp_path / 'stable-provider'
    provider.write_text('stub')
    provider.chmod(0o700)
    monkeypatch.setattr('kelmo.executor.provider_path', lambda name: str(provider))
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True, 'checks': {}})
    monkeypatch.setattr('kelmo.daemon.execute', lambda *a, **k: {'ok': True})
    monkeypatch.setattr('kelmo.config.Config.load',
                        lambda root: SimpleNamespace(state=vault / '.state'))

    setup_module.setup(setup_args(tmp_path, '--provider', 'codex', '--model', 'synthetic'))

    config = json.loads((vault / '.state/executor.json').read_text())
    assert config['argv'][-2:] == ['--provider-executable', str(provider)]
    timeout_index = config['argv'].index('--timeout')
    assert float(config['argv'][timeout_index + 1]) == 560
    assert config['timeout_seconds'] == 600


def load_bootstrap():
    path = Path(__file__).parents[1] / 'install.py'
    spec = importlib.util.spec_from_file_location('kelmo_bootstrap_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_constraint_table_has_actionable_error():
    module = load_bootstrap()

    with pytest.raises(ValueError, match='constraint-dependencies'):
        module.constraint_dependencies({})


def outer_home_fixture(tmp_path, shape):
    """The HOME a run inherits: 'client-symlinks' is a home where
    .claude/settings.json and .codex/config.toml are symlinks into the configuration repository
    and the default vault already holds data; 'plain' is an empty one."""
    outer = tmp_path / 'outer'
    home = outer / 'home'
    home.mkdir(parents=True)
    if shape == 'client-symlinks':
        repo = outer / 'demo/global'
        repo.mkdir(parents=True)
        (repo / 'settings.json').write_text('{"hooks": {"SessionStart": [{"matcher": "live"}]}}\n')
        (repo / 'config.toml').write_text('model = "live"\n')
        (home / '.claude').mkdir()
        (home / '.codex').mkdir()
        (home / '.claude/settings.json').symlink_to(repo / 'settings.json')
        (home / '.codex/config.toml').symlink_to(repo / 'config.toml')
        (home / '.claude.json').write_text('{"mcpServers": {"other_server": {"command": "live"}}}\n')
        vault = home / '.local/share/kelmo/default'
        vault.mkdir(parents=True)
        (vault / 'kelmo.toml').write_text('kept = true\n')
        (outer / 'canary.txt').write_text('canary\n')
    return outer


def tree_signature(root):
    entries = []
    for path in (root, *root.rglob('*')):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            entries.append((str(path.relative_to(root)), 'link', os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            entries.append((str(path.relative_to(root)), 'dir', None))
        else:
            entries.append((str(path.relative_to(root)), 'file', path.read_bytes()))
    return sorted(entries)


def bootstrap_argv(tmp_path, *extra):
    """install.py with every default-derived path named: --prefix, --home and --vault all come
    from Path.home() when they are left out, and the preview reads them before pip is reached."""
    return ['install.py', '--prefix', str(tmp_path / 'runtime'),
            '--home', str(tmp_path / 'owned-home'), '--vault', str(tmp_path / 'owned-vault'),
            '--disable-native-memory', '--no-start', *extra]


@pytest.mark.parametrize('shape', ['plain', 'client-symlinks'])
def test_successful_install_removes_pip_log(tmp_path, monkeypatch, shape):
    module = load_bootstrap()
    outer = outer_home_fixture(tmp_path, shape)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', str(outer / 'home'))
    monkeypatch.setattr(module.tempfile, 'tempdir', str(scratch))
    before = tree_signature(outer)
    prefix = tmp_path / 'runtime'
    log = tmp_path / 'pip.log'
    fd = os.open(log, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(module.tempfile, 'mkstemp', lambda **kwargs: (fd, str(log)))
    monkeypatch.setattr(module.venv.EnvBuilder, 'create', lambda self, path: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, 'argv', bootstrap_argv(tmp_path))

    assert module.main() == 0
    assert not log.exists()
    assert (prefix / '.kelmo-runtime').read_bytes() == b'kelmo\n'
    assert tree_signature(outer) == before


def test_claude_upgrade_recovers_after_hook_write_failure(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    home = tmp_path / 'home'
    setup_module.configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'old-vault', 'claude', True)
    real_write = setup_module.write
    failed = []

    def fail_hook_once(path, data, expected_prior=setup_module._UNSET):
        if Path(path).name == 'settings.json' and not failed:
            failed.append(True)
            raise KelmoError('E_IO', 'synthetic hook failure')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(setup_module, 'write', fail_hook_once)
    with pytest.raises(KelmoError, match='synthetic hook failure'):
        setup_module.configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)
    monkeypatch.setattr(setup_module, 'write', real_write)

    setup_module.configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)
    entry = json.loads((home / '.claude.json').read_text())['mcpServers']['kelmo']
    assert entry['command'] == str(tmp_path / 'new-kelmo')


def test_initial_claude_install_recovers_after_config_write_failure(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    home = tmp_path / 'home'
    real_write = setup_module.write
    failed = []

    def fail_config_once(path, data, expected_prior=setup_module._UNSET):
        if Path(path) == home / '.claude.json' and not failed:
            failed.append(True)
            raise KelmoError('E_IO', 'synthetic config failure')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(setup_module, 'write', fail_config_once)
    with pytest.raises(KelmoError, match='synthetic config failure'):
        setup_module.configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)
    monkeypatch.setattr(setup_module, 'write', real_write)

    setup_module.configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)
    assert 'kelmo' in json.loads((home / '.claude.json').read_text())['mcpServers']


def test_stale_client_plan_cannot_overwrite_completed_install(tmp_path):
    from kelmo.install.setup import apply_clients, prepare_clients

    home = tmp_path / 'home'
    first = prepare_clients(home, tmp_path / 'first-memory', tmp_path / 'first-vault', 'claude', True)
    stale = prepare_clients(home, tmp_path / 'stale-memory', tmp_path / 'stale-vault', 'claude', True)
    apply_clients(first)

    with pytest.raises(KelmoError, match='changed during setup'):
        apply_clients(stale)

    entry = json.loads((home / '.claude.json').read_text())['mcpServers']['kelmo']
    assert entry['command'] == str(tmp_path / 'first-memory')


def test_deleted_owned_claude_entry_is_repaired_after_upgrade(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'old-vault', 'claude', True)
    configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)
    config = json.loads((home / '.claude.json').read_text())
    del config['mcpServers']['kelmo']
    (home / '.claude.json').write_text(json.dumps(config))

    configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'new-vault', 'claude', True)

    repaired = json.loads((home / '.claude.json').read_text())
    assert repaired['mcpServers']['kelmo']['command'] == str(tmp_path / 'new-kelmo')


def test_missing_custom_executor_config_fails_before_provisioning(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    initialized = []
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: initialized.append(True))

    with pytest.raises(KelmoError) as raised:
        setup_module.setup(setup_args(tmp_path, '--executor-config', str(tmp_path / 'missing.json')))

    assert raised.value.code == 'E_CONFIG'
    assert initialized == []


def test_daemon_only_executor_config_skips_probe_and_is_not_executor_ready(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    config = tmp_path / 'daemon.json'
    config.write_text(json.dumps({'poll_seconds': 4, 'max_attempts': 2}))
    vault = tmp_path / 'vault'
    (vault / '.cache/embeddings/model').mkdir(parents=True)
    monkeypatch.setattr('kelmo.initialization.initialize', lambda *a, **k: {})
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {'healthy': True, 'checks': {}})
    invoked = []
    monkeypatch.setattr('kelmo.daemon.execute', lambda *a, **k: invoked.append(True))

    result = setup_module.setup(setup_args(tmp_path, '--executor-config', str(config)))

    assert invoked == []
    assert result['executor_ready'] is False
    assert result['complete'] is False
    assert str(config) in Path(result['service_file']).read_text()


def test_client_setup_returns_all_backup_paths(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude.json').write_text(json.dumps({'kept': True}))
    (home / '.claude/settings.json').write_text(json.dumps({'kept': True}))
    (home / '.claude/CLAUDE.md').write_text('kept instructions\n')

    result = configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)

    assert len(result['backups']) == 3
    assert all(Path(path).is_file() for path in result['backups'])


def test_partial_multi_client_failure_names_progress_and_preserves_code(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    home = tmp_path / 'home'
    real_write = setup_module.write

    def fail_codex(path, data, expected_prior=setup_module._UNSET):
        if Path(path) == home / '.codex/config.toml':
            raise KelmoError('E_IO', 'configuration changed during setup')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(setup_module, 'write', fail_codex)
    with pytest.raises(KelmoError) as raised:
        setup_module.configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'both', True)

    assert raised.value.code == 'E_IO'
    assert 'completed: claude' in str(raised.value)
    assert 'current: codex' in str(raised.value)
    assert 'rollback' not in str(raised.value).lower()


def test_multiple_dotted_features_keys_get_one_deterministic_hooks_insertion(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('features.alpha = true\nfeatures.beta = "kept"\n')

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True)

    text = config.read_text()
    assert text.count('features.hooks = true') == 1
    assert tomllib.loads(text)['features'] == {'alpha': True, 'beta': 'kept', 'hooks': True}


def test_two_failed_upgrades_retain_observed_owned_live_entry(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    home = tmp_path / 'home'
    setup_module.configure_clients(home, tmp_path / 'memory-a', tmp_path / 'vault-a', 'claude', True)
    real_write = setup_module.write
    failures = []

    def fail_config(path, data, expected_prior=setup_module._UNSET):
        if Path(path) == home / '.claude.json' and len(failures) < 2:
            failures.append(True)
            raise KelmoError('E_IO', 'synthetic config failure')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(setup_module, 'write', fail_config)
    for suffix in ('b', 'c'):
        with pytest.raises(KelmoError, match='synthetic config failure'):
            setup_module.configure_clients(home, tmp_path / f'memory-{suffix}',
                                           tmp_path / f'vault-{suffix}', 'claude', True)
    monkeypatch.setattr(setup_module, 'write', real_write)

    setup_module.configure_clients(home, tmp_path / 'memory-c', tmp_path / 'vault-c', 'claude', True)
    entry = json.loads((home / '.claude.json').read_text())['mcpServers']['kelmo']
    assert entry['command'] == str(tmp_path / 'memory-c')


def test_nested_features_subtable_preserved_when_enabling_hooks(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[features.experimental]\nvalue = 1\n')

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True)

    features = tomllib.loads(config.read_text())['features']
    assert features == {'hooks': True, 'experimental': {'value': 1}}


def test_incomplete_model_download_maps_to_setup_recovery_hint(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            raise http.client.IncompleteRead(b'partial')

    monkeypatch.setattr(setup_module.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(KelmoError) as raised:
        setup_module.fetch_model(tmp_path)

    assert raised.value.code == 'E_SETUP'
    assert '--model-source' in str(raised.value)


@pytest.mark.parametrize('shape', ['plain', 'client-symlinks'])
def test_dependency_failure_uses_versioned_error_envelope(tmp_path, monkeypatch, capsys, shape):
    module = load_bootstrap()
    outer = outer_home_fixture(tmp_path, shape)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setenv('HOME', str(outer / 'home'))
    monkeypatch.setattr(module.tempfile, 'tempdir', str(scratch))
    before = tree_signature(outer)
    prefix = tmp_path / 'runtime'
    monkeypatch.setattr(module.venv.EnvBuilder, 'create', lambda self, path: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(sys, 'argv', bootstrap_argv(tmp_path))

    assert module.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope['schema_version'] == 1
    assert envelope['ok'] is False
    assert envelope['error']['code'] == 'E_DEPENDENCIES'
    assert envelope['error']['stage'] == 'dependencies'
    assert (prefix / '.kelmo-runtime').is_file()
    assert Path(envelope['error']['log']).is_file()
    assert Path(envelope['error']['log']).is_relative_to(tmp_path)
    assert tree_signature(outer) == before


def test_linked_client_config_error_names_selected_path_and_remedies(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    target = tmp_path / 'elsewhere/config.json'
    target.parent.mkdir(parents=True)
    target.write_text('{}')
    selected = home / '.claude.json'
    home.mkdir()
    selected.symlink_to(target)

    with pytest.raises(KelmoError) as raised:
        configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)

    message = str(raised.value)
    assert str(selected) in message
    assert 'real path' in message
    assert 'manual integration' in message


def test_client_failure_does_not_claim_partial_application_when_none_completed(tmp_path, monkeypatch):
    from kelmo.install import setup as setup_module

    real_write = setup_module.write

    def fail_receipt(path, data, expected_prior=setup_module._UNSET):
        if Path(path).name == 'ownership.json':
            raise KelmoError('E_IO', 'synthetic receipt failure')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(setup_module, 'write', fail_receipt)
    with pytest.raises(KelmoError) as raised:
        setup_module.configure_clients(tmp_path / 'home', tmp_path / 'kelmo',
                                       tmp_path / 'vault', 'claude', True)

    assert raised.value.code == 'E_IO'
    assert 'did not complete' in str(raised.value)
    assert 'completed: none' in str(raised.value)
    assert 'Earlier writes/backups may remain' in str(raised.value)
    assert 'partially applied' not in str(raised.value)


def test_nested_features_after_unrelated_table_enables_top_level_hooks(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    config = home / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.foo]\ncommand = "x"\n\n'
                      '[features.web_search]\nenabled = true\n')

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True)

    parsed = tomllib.loads(config.read_text())
    assert parsed['features'] == {'hooks': True, 'web_search': {'enabled': True}}
    assert 'features' not in parsed['mcp_servers']['foo']


def test_setup_instructions_use_executable_without_path_lookup(tmp_path):
    from kelmo.install.setup import configure_clients

    executable = tmp_path / 'private runtime/bin/kelmo'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    home = tmp_path / 'home'

    result = configure_clients(home, executable, tmp_path / 'vault', 'claude', True)

    instructions = (home / '.claude/CLAUDE.md').read_text()
    command = re.search(r'Use `(.+? help --json)`', instructions)[1]
    completed = subprocess.run(shlex.split(command), env={'PATH': '/usr/bin:/bin'}, check=False)
    assert completed.returncode == 0
    assert result['executable'] == str(executable.absolute())
    assert str(executable.parent) in result['path_hint']


def test_setup_instructions_contain_no_bare_memory_commands(tmp_path):
    from kelmo.install.setup import configure_clients

    executable = tmp_path / 'runtime/bin/kelmo'
    executable.parent.mkdir(parents=True)
    executable.write_text('synthetic')
    home = tmp_path / 'home'

    configure_clients(home, executable, tmp_path / 'vault', 'claude', True)

    instructions = (home / '.claude/CLAUDE.md').read_text()
    commands = re.findall(r'`([^`]+)`', instructions)
    assert not [command for command in commands if command.startswith('memory ')]
    assert any(' create --project ' in command for command in commands)


def test_setup_instruction_commands_bind_custom_vault_when_default_is_absent(tmp_path):
    from kelmo.install.setup import configure_clients
    from kelmo.service import Service

    vault = tmp_path / 'custom vault'
    Service.initialize(vault, 'fixture')
    executable = tmp_path / 'private runtime/bin/kelmo'
    executable.parent.mkdir(parents=True)
    source_root = Path(__file__).parents[1] / 'src'
    executable.write_text(f'#!/bin/sh\nPYTHONPATH={shlex.quote(str(source_root))} '
                          f'exec {shlex.quote(sys.executable)} -m kelmo.cli "$@"\n')
    executable.chmod(0o700)
    home = tmp_path / 'empty-home'

    configure_clients(home, executable, vault, 'claude', True)

    instructions = (home / '.claude/CLAUDE.md').read_text()
    commands = [value for value in re.findall(r'`([^`]+)`', instructions)
                if str(executable) in value]
    assert commands
    assert all(shlex.split(command)[:3] == [str(executable), '--vault', str(vault)]
               for command in commands)
    prefix = shlex.split(commands[0])[:3]
    env = {'HOME': str(home), 'PATH': '/usr/bin:/bin'}
    assert subprocess.run([*prefix, 'help', '--json'], env=env, check=False).returncode == 0
    assert subprocess.run([*prefix, 'config', 'show', '--json'], env=env, check=False).returncode == 0
    assert not (home / '.local/share/kelmo/default').exists()


def test_prepare_clients_normalizes_executable_and_vault_once(tmp_path, monkeypatch):
    from kelmo.install.setup import prepare_clients

    monkeypatch.chdir(tmp_path)
    plan = prepare_clients(tmp_path / 'home', Path('runtime/../bin/kelmo'),
                           Path('data/../vault'), 'claude', True)
    item = plan['prepared']['claude']
    config = json.loads(item['config_data'])['mcpServers']['kelmo']
    hook = json.loads(item['hook_data'])['hooks']['SessionStart'][-1]['hooks'][0]['command']

    assert config['command'] == plan['executable'] == str(tmp_path / 'bin/kelmo')
    assert config['args'][2] == plan['vault'] == str(tmp_path / 'vault')
    assert shlex.split(hook)[:2] == [plan['executable'], 'hook']
    assert shlex.split(hook)[-1] == plan['vault']


def test_standalone_instruction_update_preserves_owned_setup_binding(tmp_path):
    from kelmo.install.instructions import install_instructions
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    path = home / '.claude/CLAUDE.md'
    executable = tmp_path / 'runtime/bin/kelmo'
    vault = tmp_path / 'custom vault'
    configure_clients(home, executable, vault, 'claude', True)
    before = path.read_bytes()
    backups = set(path.parent.glob('CLAUDE.md.bak.*'))

    result = install_instructions(path, disable_native=True)

    assert result['changed'] is False
    assert path.read_bytes() == before
    assert set(path.parent.glob('CLAUDE.md.bak.*')) == backups


@pytest.mark.parametrize('field,value', [
    ('executable', '/tmp/a`b/memory'),
    ('executable', '/tmp/a\nend/memory'),
    ('executable', '/tmp/<!-- kelmo:end -->/memory'),
    ('vault', '/tmp/a`b/vault'),
])
def test_instruction_binding_rejects_marker_breaking_paths_before_write(tmp_path, field, value):
    from kelmo.install.instructions import install_instructions

    path = tmp_path / 'CLAUDE.md'
    kwargs = {'executable': '/safe/memory', 'vault': '/safe/vault'}
    kwargs[field] = value
    with pytest.raises(KelmoError, match='command binding'):
        install_instructions(path, disable_native=True, **kwargs)
    assert not path.exists()


def test_client_prepare_rejects_marker_breaking_binding_without_writes(tmp_path):
    from kelmo.install.setup import prepare_clients

    home = tmp_path / 'home'
    with pytest.raises(KelmoError, match='command binding'):
        prepare_clients(home, '/tmp/bad`memory', tmp_path / 'vault', 'both', True)
    assert not home.exists()


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_client_preflight_rejects_non_regular_config_without_read_or_writes(tmp_path, kind):
    from kelmo.install.setup import prepare_clients

    home = tmp_path / 'home'
    selected = home / '.claude.json'
    home.mkdir()
    if kind == 'directory':
        selected.mkdir()
    else:
        os.mkfifo(selected)

    with pytest.raises(KelmoError, match='regular file'):
        prepare_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'claude', True)

    assert not (home / '.claude').exists()


def test_instruction_binding_rejects_nul_before_write(tmp_path):
    from kelmo.install.instructions import install_instructions

    path = tmp_path / 'CLAUDE.md'
    with pytest.raises(KelmoError, match='command binding'):
        install_instructions(path, disable_native=True,
                             executable='/tmp/bad\0memory', vault='/tmp/vault')
    assert not path.exists()
