"""Installed server-profile setup must cut over from the 0.1 worker to kelmod safely.

The fixtures are synthetic, and every service-manager call is recorded. No daemon or manager is started.
"""
import hashlib
import json
import subprocess
import sys
import sysconfig
from types import SimpleNamespace

import pytest

from kelmo.config import Config
from kelmo.contracts import CONTRACT_VERSION, EmbeddingSpace
from kelmo.core import daemon_admin, spec as core_spec
from kelmo.errors import KelmoError
from kelmo.install import setup as installer
from kelmo.interface import build_parser
from kelmo.server import PROFILE_SCHEMA, ServerProfile
from kelmo.service import Service
from kelmo.store.memory.embed import InputPolicy, bound_transform, preparation_digest


GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    # Cache the host interpreter's build configuration before tests emulate Linux on macOS.
    sysconfig.get_config_vars()
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    root = tmp_path.resolve()
    machine = SimpleNamespace(home=root / 'home', vault=root / 'vault',
                              executable=root / 'bin' / 'kelmo', kelmod=root / 'bin' / 'kelmod',
                              profile=root / 'server.json')
    machine.home.mkdir()
    for binary in (machine.executable, machine.kelmod):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'')
        binary.chmod(0o755)
    Service.initialize(machine.vault, 'synthetic', project='fixture')
    config = Config.load(machine.vault)
    profile = {
        'schema': PROFILE_SCHEMA,
        'authority_id': config.vault_id,
        'writer': {'writer_id': 'local', 'epoch': 1},
        'fenced': False,
        'archive_process': 'confined',
        'clients': [],
        'repositories': [],
    }
    ServerProfile.from_dict(profile)
    machine.profile.write_text(json.dumps(profile), encoding='utf-8')
    return machine


def configure_broker_embedding(vault):
    """Write a valid offline broker contract; building the daemon spec does not load a model or call a broker."""
    tokenizer = vault / '.cache' / 'synthetic-tokenizer.json'
    tokenizer.parent.mkdir(parents=True, exist_ok=True)
    tokenizer.write_bytes(b'{}')
    identity = 'sha256:' + hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    additions = []
    policy = InputPolicy(identity, 64, 'query: ', 'passage: ', preparation_digest(additions))
    space = EmbeddingSpace(
        CONTRACT_VERSION, 'synthetic-model@sha256:' + '0' * 64, identity,
        bound_transform('broker:query', policy.query_prefix, policy),
        bound_transform('broker:passage', policy.document_prefix, policy),
        'mean', 'none', 'l2', 64, 'float32', 'little')
    config = vault / 'kelmo.toml'
    base = config.read_text(encoding='utf-8').split('[embedding]')[0]
    lines = [base.rstrip(), '[embedding]', 'provider = "broker"',
             'fds_env = "SYNTHETIC_EMBED_FDS"', 'tokenizer = ".cache/synthetic-tokenizer.json"',
             '[embedding.input]', 'max_tokens = 64', 'query_prefix = "query: "',
             'document_prefix = "passage: "', 'special_tokens = []', '[embedding.space]']
    lines.extend(f'{key} = {json.dumps(value)}' for key, value in space.to_dict().items())
    config.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert Config.load(vault).embedding is not None


def old_worker(machine):
    path = machine.home / '.config/systemd/user/kelmo.service'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('legacy worker unit', encoding='utf-8')
    return path


def setup_args(machine, *, no_start=False, executor_config=None):
    parser, _ = build_parser('')
    argv = ['setup', '--vault', str(machine.vault), '--home', str(machine.home),
            '--executable', str(machine.executable), '--client', 'none', '--skip-model',
            '--allow-native-memory', '--non-interactive', '--server-profile', str(machine.profile)]
    if no_start:
        argv.append('--no-start')
    if executor_config is not None:
        argv += ['--executor-config', str(executor_config)]
    return parser.parse_args(argv)


def install_profile(machine, monkeypatch, *, no_start=False, executor_config=None, manager=None):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *args, **kwargs: {
        'healthy': True, 'checks': {'archive_integrity': True, 'embedding_model': True},
        'runtime_isolation': {'launcher_available': True}})
    if executor_config is not None:
        monkeypatch.setattr('kelmo.daemon.execute', lambda *args, **kwargs: {'ok': True})
    commands = []

    def record(command, **kwargs):
        commands.append((command, kwargs))
        return manager(command, kwargs) if manager else subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(installer.subprocess, 'run', record)
    return installer.setup(setup_args(machine, no_start=no_start, executor_config=executor_config)), commands


def test_build_spec_preserves_server_profile_scheduling_worker_config_and_supervised_cores(
        machine, tmp_path, monkeypatch):
    configure_broker_embedding(machine.vault)
    worker_config = tmp_path / 'worker.json'
    worker_config.write_text(json.dumps({'argv': [sys.executable, '--version'], 'timeout_seconds': 600}),
                             encoding='utf-8')
    runtime = tmp_path / 'runtime'
    options = {
        'role': 'vault', 'port': 47821, 'runtime': str(runtime),
        'worker_config': str(worker_config), 'launcher': str(machine.executable),
        'kelmod': str(machine.kelmod), 'server_profile': str(machine.profile),
    }
    daemon_admin.write_options(machine.vault / '.state', options)
    monkeypatch.setattr(core_spec, 'installed_core_command', lambda launcher: [str(launcher), 'core-run'])

    built = daemon_admin.build_spec(machine.vault)

    state = next(core for core in built['cores'] if core['name'] == 'state')
    archive = next(core for core in built['cores'] if core['name'] == 'archive')
    embed = next(core for core in built['cores'] if core['name'] == 'embed')
    assert built['meta']['server_profile'] == str(machine.profile)
    assert '--server-profile' in state['argv'] and state['argv'][state['argv'].index('--server-profile') + 1] == str(machine.profile)
    assert '--schedule' in state['argv']
    assert '--worker-config' in state['argv']
    assert state['argv'][state['argv'].index('--worker-config') + 1] == str(worker_config)
    assert '--server-profile' in archive['argv']
    assert '--archive-core' in state['argv'] and '--in-process' not in archive['argv']
    assert '--embed-core' in archive['argv']
    assert embed['argv'][:2] == [str(machine.kelmod), 'embed']
    assert 'http' not in built


def test_build_spec_runs_the_archive_core_in_process_when_the_profile_says_so(
        machine, tmp_path, monkeypatch, capsys):
    settings = json.loads(machine.profile.read_text(encoding='utf-8'))
    settings['archive_process'] = 'in-process'
    machine.profile.write_text(json.dumps(settings), encoding='utf-8')
    daemon_admin.write_options(machine.vault / '.state', {
        'role': 'vault', 'port': None, 'runtime': str(tmp_path / 'runtime'), 'worker_config': None,
        'launcher': str(machine.executable), 'kelmod': str(machine.kelmod), 'server_profile': str(machine.profile)})
    monkeypatch.setattr(core_spec, 'installed_core_command', lambda launcher: [str(launcher), 'core-run'])

    archive = next(core for core in daemon_admin.build_spec(machine.vault)['cores'] if core['name'] == 'archive')

    assert '--in-process' in archive['argv']
    assert capsys.readouterr().err == ''  # setup and status build the spec too; only a start speaks

    class Started(Exception):
        pass

    def execv(path, argv):
        raise Started

    monkeypatch.setattr(installer, 'worker_service_file', lambda home, platform=None: None)
    monkeypatch.setattr(installer, 'service_absent', lambda name, platform: True)
    monkeypatch.setattr(daemon_admin.os, 'execv', execv)
    with pytest.raises(Started):
        daemon_admin.run(machine.vault)
    assert 'without confinement' in capsys.readouterr().err  # in the service log at every start


def test_cli_doctor_asks_a_running_daemon_for_the_embedding_provider(machine, monkeypatch):
    from kelmo import cli
    from kelmo.core import daemon_client

    monkeypatch.setattr(daemon_admin, 'lock_free', lambda root: True)
    assert cli.daemon_embedding(machine.vault) is None  # no daemon: doctor checks in its own process

    monkeypatch.setattr(daemon_admin, 'lock_free', lambda root: False)
    asked = []
    answers = [{'ok': True, 'reason': None}, {'ok': False, 'reason': 'embedding_unavailable'}]

    def call(root, operation, body, **options):
        asked.append((operation, body))
        return answers.pop(0)

    monkeypatch.setattr(daemon_client, 'call', call)
    handle = cli.daemon_embedding(machine.vault)
    assert handle.embedding_health() == {'ok': True, 'reason': None}
    assert handle.embedding_health() == {'ok': False, 'reason': 'embedding_unavailable'}
    assert asked == [('state.embedding_health', {})] * 2


def test_server_setup_installs_kelmod_service_and_retires_worker_after_successful_stop(
        machine, monkeypatch, tmp_path):
    configure_broker_embedding(machine.vault)
    worker_config = tmp_path / 'worker.json'
    worker_config.write_text(json.dumps({'argv': [sys.executable, '--version'], 'timeout_seconds': 600}),
                             encoding='utf-8')
    legacy = old_worker(machine)
    observed_worker_during_stop = []

    def manager(command, _kwargs):
        if command[:4] == ['systemctl', '--user', 'disable', '--now']:
            observed_worker_during_stop.append(legacy.exists())
        return subprocess.CompletedProcess(command, 0, b'', b'')

    result, commands = install_profile(machine, monkeypatch, executor_config=worker_config, manager=manager)

    service = machine.home / '.config/systemd/user/kelmod.service'
    assert result['background_started'] is True
    assert result['service_file'] == str(service)
    assert not legacy.exists() and observed_worker_during_stop == [True]
    assert any(command == ['systemctl', '--user', 'disable', '--now', 'kelmo.service']
               for command, _ in commands)
    assert any(command == ['systemctl', '--user', 'restart', 'kelmod.service']
               for command, _ in commands)
    spec_path = machine.vault / '.state/kelmod.spec.json'
    saved_spec = json.loads(spec_path.read_text(encoding='utf-8'))
    assert 'http' not in saved_spec
    state = next(core for core in saved_spec['cores'] if core['name'] == 'state')
    archive = next(core for core in saved_spec['cores'] if core['name'] == 'archive')
    embed = next(core for core in saved_spec['cores'] if core['name'] == 'embed')
    assert '--schedule' in state['argv']
    assert state['argv'][state['argv'].index('--server-profile') + 1] == str(machine.profile)
    assert state['argv'][state['argv'].index('--worker-config') + 1] == str(worker_config.resolve())
    assert '--archive-core' in state['argv'] and '--in-process' not in archive['argv']
    assert '--embed-core' in archive['argv'] and embed['argv'][:2] == [str(machine.kelmod), 'embed']
    options = json.loads((machine.vault / '.state/kelmod.options.json').read_text(encoding='utf-8'))
    assert options['server_profile'] == str(machine.profile)
    assert not (machine.vault / '.state/http-token').exists()
    assert 'ExecStart=' in service.read_text(encoding='utf-8')
    assert 'kelmo" "daemon" "run"' in service.read_text(encoding='utf-8')
    assert saved_spec['meta']['server_profile'] == str(machine.profile)


def test_server_setup_keeps_worker_when_stop_fails(machine, monkeypatch):
    legacy = old_worker(machine)
    activated = []

    def manager(command, _kwargs):
        if command == ['systemctl', '--user', 'disable', '--now', 'kelmo.service']:
            return subprocess.CompletedProcess(command, 1, b'', b'not found')
        activated.append(command)
        return subprocess.CompletedProcess(command, 0, b'loaded\n', b'')

    with pytest.raises(KelmoError):
        install_profile(machine, monkeypatch, manager=manager)

    assert legacy.exists()
    assert not any(command == ['systemctl', '--user', 'restart', 'kelmod.service'] for command in activated)


def test_fenced_server_no_start_preserves_worker_and_never_calls_service_manager(machine, monkeypatch):
    profile = json.loads(machine.profile.read_text(encoding='utf-8'))
    profile['fenced'] = True
    ServerProfile.from_dict(profile)
    machine.profile.write_text(json.dumps(profile), encoding='utf-8')
    legacy = old_worker(machine)

    result, commands = install_profile(machine, monkeypatch, no_start=True)

    assert result['background_started'] is False
    assert legacy.exists()
    assert commands == []
    assert (machine.home / '.config/systemd/user/kelmod.service').is_file()
    assert json.loads(machine.profile.read_text(encoding='utf-8'))['fenced'] is True


def test_server_setup_dry_run_plans_kelmod_without_changing_the_old_install(machine, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    legacy = old_worker(machine)
    args = setup_args(machine, no_start=True)
    args.dry_run = True
    before = {path.relative_to(machine.home.parent):
              path.read_bytes() if path.is_file() else None
              for path in machine.home.parent.rglob('*')}

    result = installer.setup(args)

    daemon_unit = machine.home / '.config/systemd/user/kelmod.service'
    assert result['service']['path'] == str(daemon_unit)
    assert any(item['path'] == str(daemon_unit) for item in result['files'])
    after = {path.relative_to(machine.home.parent):
             path.read_bytes() if path.is_file() else None
             for path in machine.home.parent.rglob('*')}
    assert after == before
    assert legacy.read_text(encoding='utf-8') == 'legacy worker unit'


def test_fresh_server_install_accepts_manager_confirmed_missing_worker(machine, monkeypatch):
    def manager(command, _kwargs):
        if command == ['systemctl', '--user', 'disable', '--now', 'kelmo.service']:
            return subprocess.CompletedProcess(command, 1, b'', b'unit not loaded')
        if command == ['systemctl', '--user', 'show', '--property=LoadState', '--value', 'kelmo.service']:
            return subprocess.CompletedProcess(command, 0, b'not-found\n', b'')
        return subprocess.CompletedProcess(command, 0, b'', b'')

    result, commands = install_profile(machine, monkeypatch, manager=manager)

    assert result['background_started'] is True
    assert result['service_file'] == str(machine.home / '.config/systemd/user/kelmod.service')
    assert ['systemctl', '--user', 'show', '--property=LoadState', '--value', 'kelmo.service'] in [
        command for command, _ in commands]


def test_plain_local_vault_setup_keeps_http_daemon_regression(machine, monkeypatch):
    # The ordinary local vault still gets the installed HTTP endpoint and local client URL.
    parser, _ = build_parser('')
    args = parser.parse_args(['setup', '--vault', str(machine.vault), '--home', str(machine.home),
                              '--executable', str(machine.executable), '--client', 'none', '--skip-model',
                              '--allow-native-memory', '--non-interactive'])
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *a, **k: {
        'healthy': True, 'checks': {'archive_integrity': True}, 'runtime_isolation': {'launcher_available': True}})
    monkeypatch.setattr('kelmo.install.shared.activate', lambda *a: None)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, b'', b''))

    result = installer.setup(args)

    saved_spec = json.loads((machine.vault / '.state/kelmod.spec.json').read_text(encoding='utf-8'))
    assert result['mcp_url'] == 'http://127.0.0.1:47821/mcp'
    assert saved_spec['http']['port'] == 47821
    assert (machine.vault / '.state/http-token').is_file()

