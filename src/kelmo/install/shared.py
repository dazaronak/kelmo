"""Install client integrations for a shared authority without initializing a local writer."""
import json
import os
from pathlib import Path
import shlex

from ..client import profile_text
from ..config import CLIENT_FILE, ClientProfile
from ..errors import KelmoError
from .instructions import render_instructions
from .removal import _read
from .setup import apply_clients, client_paths, prepare_clients, profile_options, write


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
    ClientProfile.parse(root, text.encode())
    plan = prepare_clients(home, args.executable, root, args.client, args.disable_native_memory, mode,
                           **profile_options(args))
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
    result = apply_clients(plan)
    if written['backup']:
        result['backups'].append(written['backup'])
    return dict(result, mode='shared', client_configured=True, complete=False,
                background_started=False, remote_connection_verified=False,
                session_memory_mode=mode,
                next_step='Restart the selected clients, then probe the configured shared connection.')
