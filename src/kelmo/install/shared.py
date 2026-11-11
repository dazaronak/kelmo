"""Install client integrations for a shared authority without initializing a local writer."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from ..client import profile_text
from ..config import CLIENT_FILE, ClientProfile
from ..errors import KelmoError
from .instructions import render_instructions
from .removal import _read
from .setup import apply_clients, client_paths, prepare_clients, profile_options, write

# The machine daemon's HTTP MCP port when setup is not given one. Fixed, because the clients register the URL.
DEFAULT_HTTP_PORT = 47821
SERVICE_LABEL = 'org.kelmo.daemon'
SERVICE_UNIT = 'kelmod.service'


def edge_plan(args, root, profile):
    """The machine daemon of an installed launcher (kelmod beside it): its spec, service file and endpoint.
    None for a development checkout, which keeps `kelmo mcp` and `kelmo hook`."""
    executable = Path(args.executable).expanduser().resolve()
    kelmod = executable.parent / 'kelmod'
    if not kelmod.is_file():
        return None
    from ..core.daemon_client import runtime_dir
    port = getattr(args, 'http_port', None) or DEFAULT_HTTP_PORT
    return {'kelmod': kelmod, 'state': profile.state, 'port': int(port), 'runtime': runtime_dir(root),
            'url': f'http://127.0.0.1:{int(port)}/mcp', 'spec': profile.state / 'kelmod.spec.json'}


def memory_limits(spec):
    """(MemoryHigh, MemoryMax) in MiB for the whole daemon tree: the cores' soft and hard budgets summed, plus
    64 MiB for kelmod itself."""
    soft = sum(core.get('budget', {}).get('soft_mb', 0) for core in spec.get('cores', []))
    hard = sum(core.get('budget', {}).get('hard_mb', 0) for core in spec.get('cores', []))
    return soft + 64, hard + 64


def service_definition(home, launcher, root, platform, limits=None):
    """One user service that keeps the machine daemon running. On Linux it is a `Type=notify` unit with a
    watchdog (kelmod sends READY and WATCHDOG) and, given `limits`, the memory bounds of the tree."""
    import plistlib
    # The launcher rebuilds the spec at every start, then execs kelmod: systemd's main pid stays kelmod.
    argv = [str(launcher), 'daemon', 'run', '--vault', str(root)]
    if platform == 'darwin':
        path = Path(home) / f'Library/LaunchAgents/{SERVICE_LABEL}.plist'
        data = plistlib.dumps({'Label': SERVICE_LABEL, 'ProgramArguments': argv, 'RunAtLoad': True,
                               'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 5,
                               'ExitTimeOut': 40})
    elif platform == 'linux':
        path = Path(home) / f'.config/systemd/user/{SERVICE_UNIT}'
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        memory = '' if limits is None else f'\nMemoryHigh={limits[0]}M\nMemoryMax={limits[1]}M'
        data = ('[Unit]\nDescription=Kelmo daemon\n[Service]\nType=notify\nNotifyAccess=main\nExecStart='
                + ' '.join(quote(a) for a in argv)
                + '\nRestart=on-failure\nRestartSec=2\nRestartPreventExitStatus=78\nTimeoutStopSec=40'
                + '\nWatchdogSec=30' + memory + '\n[Install]\nWantedBy=default.target\n').encode()
    else:
        raise KelmoError('E_CONFIG', 'The machine daemon runs as a launchd or systemd user service.')
    return path, data


def activate(path, platform):
    uid = os.getuid()
    commands = ([['launchctl', 'bootout', f'gui/{uid}/{SERVICE_LABEL}'],
                 ['launchctl', 'bootstrap', f'gui/{uid}', str(path)]] if platform == 'darwin'
                else [['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', SERVICE_UNIT],
                      ['systemctl', '--user', 'restart', SERVICE_UNIT]])
    for index, command in enumerate(commands):
        try:
            result = subprocess.run(command, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KelmoError('E_SETUP', f'The kelmod service could not be started ({command[0]}: {error}); '
                                        f'the configuration was written; start {path} by hand.') from error
        if result.returncode and platform == 'darwin' and index == 1:
            # bootstrap right after bootout can fail while launchd still tears the old job down.
            time.sleep(1.0)
            result = subprocess.run(command, capture_output=True, timeout=30)
        # bootout of a service that is not loaded fails, which is expected on a first install.
        if result.returncode and not (platform == 'darwin' and index == 0):
            raise KelmoError('E_SETUP', f'The kelmod service could not be started; the configuration was '
                                        f'written; start {path} by hand.')


def selected(args):
    return (bool(getattr(args, 'shared_authority', None) or getattr(args, 'shared_command_json', None))
            or ClientProfile.selected(args.vault))


def prepare(args):
    root, home = Path(args.vault).expanduser().resolve(), Path(args.home).expanduser().resolve()
    if (root / 'kelmo.toml').exists():
        raise KelmoError('E_CONFIG', 'A shared client needs its own directory, separate from a local vault.')
    if any(getattr(args, key, None) for key in ('provider', 'model', 'executor_config', 'model_source',
                                               'embedding_profile', 'skip_model', 'effort', 'wiki', 'server_profile')):
        raise KelmoError('E_CONFIG', 'Configure the worker, wiki and models on the server, not in a shared client.')
    authority, command = getattr(args, 'shared_authority', None), getattr(args, 'shared_command_json', None)
    if bool(authority) != bool(command):
        raise KelmoError('E_CONFIG', 'Select --shared-authority and --shared-command-json together.')
    prior = _read(root / CLIENT_FILE)
    if prior is None and root.exists() and any(root.iterdir()):
        raise KelmoError('E_ROOT_NOT_EMPTY', 'Select an empty client directory or an existing shared profile.')
    if prior is None:
        if authority is None:
            raise KelmoError('E_CONFIG', 'A new shared client requires its authority and channel command.')
        try:
            argv = json.loads(command)
        except ValueError as error:
            raise KelmoError('E_CONFIG', 'The shared command must be a JSON argument vector.') from error
        if not isinstance(argv, list):
            raise KelmoError('E_CONFIG', 'The shared command must be a JSON argument vector.')
        profile = ClientProfile.parse(root, profile_text(authority, argv).encode())
    else:
        profile = ClientProfile.parse(root, prior)
        if authority and (authority != profile.authority_id or json.loads(command) != list(profile.command)):
            raise KelmoError('E_CONFIG', 'Setup does not switch an existing client to another authority or channel.')
    mode = getattr(args, 'session_memory', None) or 'off'
    sessions = {key: list(value) for key, value in profile.sessions.items()}
    if mode != 'off':
        for provider in (() if args.client == 'none' else ('claude', 'codex') if args.client == 'both' else (args.client,)):
            selected, _ = client_paths(home, provider, **profile_options(args))
            sessions[provider] = sessions.get(provider) or [str(selected / (
                'projects' if provider == 'claude' else 'sessions'))]
    text = profile_text(profile.authority_id, profile.command, sessions=sessions,
                        timeout_seconds=profile.timeout_seconds, max_snapshot_bytes=profile.max_snapshot_bytes)
    parsed = ClientProfile.parse(root, text.encode())
    edge = edge_plan(args, root, parsed)
    plan = prepare_clients(home, args.executable, root, args.client, args.disable_native_memory, mode,
                           edge=edge, **profile_options(args))
    plan['edge'] = edge
    return plan, prior, text.encode(), mode


def setup(args):
    plan, prior, desired, mode = prepare(args)
    root = Path(args.vault).expanduser().resolve()
    profile_path = root / CLIENT_FILE
    if getattr(args, 'dry_run', False):
        from .preview import _change
        changes = [_change(profile_path, prior, desired)]
        binding = shlex.join([str(args.executable), '--vault', str(root)])
        for name, item in plan['prepared'].items():
            instructions = item['root'] / ('CLAUDE.md' if name == 'claude' else 'AGENTS.md')
            original = _read(instructions)
            text = render_instructions(original or b'', disable_native=args.disable_native_memory, command=binding)
            changes.append(_change(instructions, original, text))
            for path, old, data in ((item['path'], item['prior'], item['config_data']),
                                    (item['hook_path'], item['hook_prior'], item['hook_data']),
                                    (item['ownership_path'], item['ownership_prior'], item['ownership_data'])):
                if path is not None:
                    changes.append(_change(path, old, data))
        return {'dry_run': True, 'mode': 'shared', 'complete': False, 'files': changes,
                'clients': plan['clients'], 'model': {'action': 'server-managed'},
                'service': {'action': 'server-managed'}, 'provider_calls': 'none',
                'session_memory_mode': mode, 'remote_connection_verified': False}
    executable = Path(args.executable).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise KelmoError('E_SETUP', 'Installed memory executable is missing.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = write(profile_path, desired, expected_prior=prior)
    edge = plan['edge']
    service = None
    if edge is not None:
        import json
        from ..core.spec import http_token_at
        from ..core.daemon_admin import build_spec, write_options, write_private
        http_token_at(edge['state'])
        write_options(edge['state'], {'role': 'edge', 'port': edge['port'], 'runtime': str(edge['runtime']),
                                      'launcher': str(executable), 'kelmod': str(edge['kelmod'])})
        spec = build_spec(root)
        write_private(edge['spec'], (json.dumps(spec, indent=2) + '\n').encode())
        service, data = service_definition(Path(args.home).expanduser().resolve(), executable, root,
                                           sys.platform, memory_limits(spec))
        write(service, data)
    result = apply_clients(plan)
    if written['backup']:
        result['backups'].append(written['backup'])
    started = False
    if service is not None and not getattr(args, 'no_start', False):
        activate(service, sys.platform)
        started = True
    return dict(result, mode='shared', client_configured=True, complete=False,
                background_started=started, remote_connection_verified=False,
                session_memory_mode=mode, service_file=str(service) if service else None,
                mcp_url=edge['url'] if edge else None,
                next_step='Restart the selected clients, then probe the configured shared connection.')
