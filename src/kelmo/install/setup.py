"""User-level installation of client integration and background processing."""
import json
import copy
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
import hashlib
import http.client
import urllib.request
import re

from .instructions import (BEGIN as INSTRUCTION_BEGIN, END as INSTRUCTION_END,
                           install_instructions, normalize_binding)
from ..errors import KelmoError


_UNSET = object()
# A stopping worker finishes the model call in flight; the executor bounds one call at 600 s.
STOP_SECONDS = 660


def write(path, data, expected_prior=_UNSET):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_INPUT', 'Installation paths cannot contain symlinks.')
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = path.read_bytes() if path.exists() else None
    prior_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    if expected_prior is not _UNSET and prior != expected_prior:
        raise KelmoError('E_IO', 'Configuration changed during setup.')
    if prior == data:
        return {'changed': False, 'backup': None, 'path': str(path)}
    backup_path = None
    if prior is not None:
        backup = path.with_name(path.name + '.bak.' + uuid.uuid4().hex)
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(prior)
            output.flush()
            os.fsync(output.fileno())
        if backup.read_bytes() != prior:
            raise KelmoError('E_IO', 'Installation backup verification failed.')
        backup_path = str(backup)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.memory-setup-')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            if prior_mode is not None:
                os.fchmod(output.fileno(), prior_mode)
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != prior:
            raise KelmoError('E_IO', 'Configuration changed during setup.')
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return {'changed': True, 'backup': backup_path, 'path': str(path)}


def read_json(path, *, required=False):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_INPUT', f'Selected configuration path {path} contains a symlink; '
                        'select its real path or use manual integration.')
    try:
        exists = path.exists()
        if exists and not path.is_file():
            raise KelmoError('E_CONFIG', f'Selected configuration path {path} must be a regular file.')
        if required and not exists:
            raise KelmoError('E_CONFIG', 'Executor configuration file is missing or unreadable.')
        value = json.loads(path.read_text()) if exists else {}
    except KelmoError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise KelmoError('E_CONFIG', 'Configuration file is missing, unreadable or invalid JSON.') from error
    if not isinstance(value, dict):
        raise KelmoError('E_CONFIG', 'Client configuration must be an object.')
    return value


def parse_json(path, prior):
    try:
        value = json.loads(prior) if prior is not None else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid JSON client configuration: {error}') from error
    if not isinstance(value, dict):
        raise KelmoError('E_CONFIG', 'Client configuration must be an object.')
    return value


def enable_codex_hooks(text):
    parsed = tomllib.loads(text)
    features = parsed.get('features')
    if features is not None and not isinstance(features, dict):
        raise KelmoError('E_CONFIG', 'Codex features must be a table.')
    expected = copy.deepcopy(parsed)
    expected.setdefault('features', {})['hooks'] = True
    candidates = []
    feature_key = r'(?:features|"features"|\'features\')'
    hook_key = r'(?:hooks|"hooks"|\'hooks\')'

    for header in re.finditer(rf'(?m)^[ \t]*\[\s*{feature_key}\s*\][ \t]*(?:#.*)?$', text):
        after = text[header.end():]
        following = re.search(r'(?m)^\s*\[', after)
        stop = header.end() + (following.start() if following else len(after))
        body = text[header.end():stop]
        assignments = list(re.finditer(
            rf'(?m)^(\s*{hook_key}\s*=\s*)(?:true|false)(\s*(?:#.*)?)$', body))
        if assignments:
            for assignment in assignments:
                updated = (body[:assignment.start()] + assignment.group(1) + 'true'
                           + assignment.group(2) + body[assignment.end():])
                candidates.append(text[:header.end()] + updated + text[stop:])
        else:
            candidates.append(text[:header.end()] + '\nhooks = true' + body + text[stop:])

    for inline in re.finditer(
            rf'(?m)^(\s*{feature_key}\s*=\s*)\{{([^\n}}]*)\}}(\s*(?:#.*)?)$', text):
        body = inline.group(2)
        assignments = list(re.finditer(rf'({hook_key}\s*=\s*)(?:true|false)\b', body))
        if assignments:
            for assignment in assignments:
                updated = (body[:assignment.start()] + assignment.group(1) + 'true'
                           + body[assignment.end():])
                candidates.append(text[:inline.start()] + inline.group(1) + '{' + updated
                                  + '}' + inline.group(3) + text[inline.end():])
        else:
            separator = '' if not body.strip() else ','
            updated = body + separator + ' hooks = true '
            candidates.append(text[:inline.start()] + inline.group(1) + '{' + updated
                              + '}' + inline.group(3) + text[inline.end():])

    for dotted in re.finditer(
            rf'(?m)^(\s*{feature_key}\s*\.\s*{hook_key}\s*=\s*)'
            r'(?:true|false)(\s*(?:#.*)?)$', text):
        candidates.append(text[:dotted.start()] + dotted.group(1) + 'true'
                          + dotted.group(2) + text[dotted.end():])
    if features is not None and 'hooks' not in features:
        dotted = re.search(
                rf'(?m)^\s*{feature_key}\s*\.\s*(?:[A-Za-z0-9_-]+|"[^"]+"|\'[^\']+\')\s*=', text)
        if dotted:
            candidates.append(text[:dotted.start()] + 'features.hooks = true\n' + text[dotted.start():])
        nested = re.search(rf'(?m)^[ \t]*\[\s*{feature_key}\s*\.', text)
        if nested:
            candidates.append('features.hooks = true\n' + text)
    if features is None:
        candidates.append(text + '\n[features]\nhooks = true\n')

    accepted = []
    for candidate in candidates:
        try:
            if tomllib.loads(candidate) == expected and candidate not in accepted:
                accepted.append(candidate)
        except tomllib.TOMLDecodeError:
            continue
    if len(accepted) != 1:
        raise KelmoError('E_CONFIG', 'Unsupported Codex configuration shape.')
    return accepted[0]


def client_config(path, kind):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_INPUT', f'Selected configuration path {path} contains a symlink; '
                        'select its real path or use manual integration.')
    try:
        exists = path.exists()
        if exists and not path.is_file():
            raise KelmoError('E_CONFIG', f'Selected configuration path {path} must be a regular file.')
        prior = path.read_bytes() if exists else None
    except KelmoError:
        raise
    except OSError as error:
        raise KelmoError('E_CONFIG', f'Selected configuration path {path} could not be read.') from error
    if kind == 'json':
        return prior, parse_json(path, prior)
    try:
        text = prior.decode() if prior is not None else ''
        return prior, text, tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid TOML client configuration: {error}') from error


def validate_hook_shapes(hooks):
    if not isinstance(hooks, dict):
        raise KelmoError('E_CONFIG', 'Client hooks configuration must be an object.')
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise KelmoError('E_CONFIG', 'Each hook event must contain a list of groups.')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks', []), list):
                raise KelmoError('E_CONFIG', 'Each hook group must contain a hooks list.')
            if not all(isinstance(member, dict) for member in group.get('hooks', [])):
                raise KelmoError('E_CONFIG', 'Each hook member must be an object.')


def validate_instruction_markers(path):
    if path.name not in ('CLAUDE.md', 'AGENTS.md') or any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_INPUT', 'Instruction path is invalid or contains a symlink.')
    if not path.exists():
        return
    if not path.is_file():
        raise KelmoError('E_INPUT', 'Instructions must be a regular file.')
    original = path.read_bytes()
    if INSTRUCTION_BEGIN in original or INSTRUCTION_END in original:
        if (original.count(INSTRUCTION_BEGIN) != 1 or original.count(INSTRUCTION_END) != 1
                or original.index(INSTRUCTION_BEGIN) > original.index(INSTRUCTION_END)):
            raise KelmoError('E_INPUT', 'Ambiguous kelmo instruction markers.')


def profile_options(args):
    return {key: getattr(args, key, None) for key in
            ('claude_profile', 'codex_profile', 'claude_mcp_config')}


def client_paths(home, client, *, claude_profile=None, codex_profile=None, claude_mcp_config=None):
    home = Path(home).expanduser().absolute()
    selected = claude_profile if client == 'claude' else codex_profile
    root = Path(selected).expanduser().absolute() if selected else home / ('.claude' if client == 'claude' else '.codex')
    if client == 'codex':
        return root, root / 'config.toml'
    path = Path(claude_mcp_config).expanduser().absolute() if claude_mcp_config else (
        root / '.claude.json' if claude_profile else home / '.claude.json')
    return root, path


def mcp_entry(executable, vault, *, reconnect=False):
    return {'command': str(executable), 'args': ['mcp', '--vault', str(vault)]
            + (['--reconnect'] if reconnect else [])}


def headers_helper(endpoint):
    """The command a client runs for the endpoint's `Authorization` header: `kelmod http-headers` reads the
    token from the state directory and gives it out only to a live daemon on that port, so no client file holds
    the token."""
    return shlex.join([str(endpoint['kelmod']), 'http-headers', '--state', str(endpoint['state']),
                       '--port', str(endpoint['port']), '--runtime-dir', str(endpoint['runtime'])])


def http_entry(client, endpoint):
    """The registration of the machine daemon's HTTP MCP endpoint in each client's own keys: Claude Code
    `type`/`url`/`headersHelper`, Codex `url`/`http_headers_helper`."""
    if client == 'claude':
        return {'type': 'http', 'url': endpoint['url'], 'headersHelper': headers_helper(endpoint)}
    return {'url': endpoint['url'], 'http_headers_helper': headers_helper(endpoint)}


def codex_table(entry):
    """The `[mcp_servers.kelmo]` table with bare keys, as setup has always written it; a header map
    (`http_headers`) follows as its own subtable."""
    lines = ['[mcp_servers.kelmo]']
    lines += [f'{key} = {json.dumps(value)}' for key, value in entry.items() if not isinstance(value, dict)]
    for key, value in entry.items():
        if isinstance(value, dict):
            lines += [f'[mcp_servers.kelmo.{key}]'] + [f'{json.dumps(k)} = {json.dumps(v)}' for k, v in value.items()]
    return '\n'.join(lines) + '\n'


def is_http_entry(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('url'), str)
            and entry['url'].startswith('http://127.0.0.1:') and 'command' not in entry)


def render_codex(text, names, entry):
    """Codex TOML with the [mcp_servers.NAME] tables of `names` replaced by one marked kelmo table;
    refuses a layout where that edit would change any unrelated value."""
    from ..config_edit import table
    before = tomllib.loads(text)
    headers = list(re.finditer(r"(?m)^[ \t]*\[([^\n]+)\][ \t]*(?:#.*)?$", text))
    for index in range(len(headers) - 1, -1, -1):
        match = headers[index]
        parsed = tomllib.loads(match.group() + "\n")
        servers = parsed.get("mcp_servers", {})
        if isinstance(servers, dict) and set(servers).intersection(names):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            text = text[:match.start()] + text[end:]
    text = re.sub(r"(?m)^# kelmo:(?:begin|end)[ \t]*\n?", "", text)
    text = (text.rstrip() + "\n\n# kelmo:begin\n" +
            table(["mcp_servers", "kelmo"], entry) + "# kelmo:end\n")
    expected = copy.deepcopy(before)
    for name in names:
        expected["mcp_servers"].pop(name, None)
    expected["mcp_servers"]["kelmo"] = entry
    try:
        if tomllib.loads(text) != expected:
            raise ValueError("Unrelated configuration changed")
    except ValueError as error:
        raise KelmoError("E_CONFIG", "Use explicit [mcp_servers.NAME] tables; "
                        "this TOML layout cannot be changed safely.") from error
    return text.encode()


def prepare_clients(home, executable, vault, client, disable_native, session_mode=None, edge=None, **profiles):
    """Plans the client configuration. `edge` ({'kelmod', 'state', 'url', 'token'}) registers the machine
    daemon's HTTP endpoint and `kelmod hook` instead of a per-session `kelmo mcp` and `kelmo hook`."""
    from .hooks import EVENTS, definitions, variants
    if session_mode not in (None,'off','auto','archive-only'):
        raise KelmoError('E_CONFIG','Invalid session-memory mode.')
    executable = normalize_binding(executable)
    vault = normalize_binding(vault)
    home = Path(home).expanduser().resolve()
    clients = [] if client == 'none' else ['claude', 'codex'] if client == 'both' else [client]
    prepared = {}
    for name in clients:
        prior_bindings = []
        root, path = client_paths(home, name, **profiles)
        entry = http_entry(name, edge) if edge else mcp_entry(executable, vault)
        if name == 'claude':
            prior, data = client_config(path, 'json')
            servers = data.setdefault('mcpServers', {})
            if not isinstance(servers, dict):
                raise KelmoError('E_CONFIG', 'Claude mcpServers must be an object.')
            ownership_path = root / 'kelmo/ownership.json'
            ownership_prior, ownership = client_config(ownership_path, 'json')
            legacy_owned = ownership.get('claude_mcp_entry')
            current_owned = ownership.get('current_owned_entry', legacy_owned)
            has_previous = 'previous_owned_entry' in ownership
            previous_owned = ownership.get('previous_owned_entry')
            live = servers.get('kelmo')
            prior_bindings = [item for item in (live,current_owned,previous_owned) if isinstance(item,dict)]
            allowed = [current_owned]
            if has_previous:
                allowed.append(previous_owned)
            if current_owned is not None and live is not None and live not in allowed:
                raise KelmoError('E_CONFIG', 'The owned MCP entry changed outside kelmo.')
            if current_owned is None and live is not None and live != entry:
                raise KelmoError('E_CONFIG', 'An unmanaged kelmo MCP entry already exists.')
            if edge is None and is_http_entry(live):
                raise KelmoError('E_CONFIG', 'The kelmo MCP entry points at a machine daemon; remove it first.')
            servers['kelmo'] = entry
            config_data = (json.dumps(data, indent=2) + '\n').encode()
            ownership.pop('claude_mcp_entry', None)
            if current_owned != entry:
                ownership['previous_owned_entry'] = live
            ownership['current_owned_entry'] = entry
            ownership_data = (json.dumps(ownership, indent=2) + '\n').encode()
        else:
            prior, text, parsed = client_config(path, 'toml')
            old_entry = parsed.get('mcp_servers',{}).get('kelmo') if isinstance(parsed.get('mcp_servers',{}),dict) else None
            ownership_path = root / 'kelmo/ownership.json'
            ownership_prior, ownership = client_config(ownership_path, 'json')
            current_owned = ownership.get('current_owned_entry')
            has_previous = 'previous_owned_entry' in ownership
            previous_owned = ownership.get('previous_owned_entry')
            prior_bindings = [item for item in (old_entry, current_owned, previous_owned)
                              if isinstance(item, dict)]
            allowed = [current_owned]
            if has_previous:
                allowed.append(previous_owned)
            if (current_owned is not None and old_entry is not None
                    and old_entry not in allowed):
                raise KelmoError('E_CONFIG', 'The owned MCP entry changed outside kelmo.')
            if not isinstance(parsed.get('mcp_servers', {}), dict):
                raise KelmoError('E_CONFIG', 'Codex mcp_servers must be a table.')
            begin, end = '# kelmo:begin', '# kelmo:end'
            block = begin + '\n' + codex_table(entry) + end
            if begin in text or end in text:
                if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) > text.index(end):
                    raise KelmoError('E_CONFIG', 'Ambiguous Codex integration markers.')
                text = text[:text.index(begin)] + block + text[text.index(end) + len(end):]
            else:
                if 'kelmo' in parsed.get('mcp_servers', {}):
                    if old_entry != entry:
                        raise KelmoError('E_CONFIG', 'An unmanaged kelmo MCP entry already exists.')
                    text = render_codex(text, {'kelmo'}, entry).decode()
                else:
                    text += '\n' + block + '\n'
            text = enable_codex_hooks(text)
            try:
                final = tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                raise KelmoError('E_CONFIG', 'Unsupported Codex configuration shape.') from error
            expected = copy.deepcopy(parsed)
            expected.setdefault('mcp_servers', {})['kelmo'] = entry
            expected.setdefault('features', {})['hooks'] = True
            if final != expected:
                raise KelmoError('E_CONFIG', 'Unsupported Codex configuration shape.')
            config_data = text.encode()
            entry = expected['mcp_servers']['kelmo']
            if current_owned != entry:
                ownership['previous_owned_entry'] = old_entry
            ownership['current_owned_entry'] = entry
            ownership_data = (json.dumps(ownership, indent=2) + '\n').encode()
        hook_path = root / ('settings.json' if name == 'claude' else 'hooks.json')
        hook_prior, settings = client_config(hook_path, 'json')
        hooks = settings.setdefault('hooks', {})
        validate_hook_shapes(hooks)
        desired = definitions(executable,vault,name,session_mode,edge)
        for event in EVENTS:
            groups = hooks.get(event, [])
            allowed = variants(executable,vault,name,event,edge)
            for binding in prior_bindings:
                args = binding.get('args',[])
                if (isinstance(binding.get('command'),str) and isinstance(args,list)
                        and len(args) in (3,4) and args[:2]==['mcp','--vault'] and isinstance(args[2],str)
                        and args[3:] in ([], ['--reconnect'])):
                    allowed += variants(binding['command'],args[2],name,event)
            retained = []
            for group in groups:
                original_hooks = group.get('hooks', [])
                if session_mode is not None and any(h.get('statusMessage')=='kelmo managed hook'
                                                    and h not in allowed for h in original_hooks):
                    raise KelmoError('E_CONFIG','Managed hook changed outside setup; resolve before upgrading.')
                remaining = [h for h in original_hooks
                             if h.get('statusMessage') != 'kelmo managed hook']
                if remaining != original_hooks:
                    group['hooks'] = remaining
                    if not remaining and set(group) == {'hooks'}:
                        continue
                retained.append(group)
            groups[:] = retained
            if event in desired:
                groups.append({'hooks':[desired[event]]})
            if groups or event in hooks:
                hooks[event]=groups
        prepared[name] = dict(root=root, path=path, prior=prior, config_data=config_data,
                              hook_path=hook_path, hook_prior=hook_prior,
                              hook_data=(json.dumps(settings, indent=2) + '\n').encode(),
                              ownership_path=ownership_path, ownership_prior=ownership_prior,
                              ownership_data=ownership_data)
    for name in clients:
        root = prepared[name]['root']
        validate_instruction_markers(root / ('CLAUDE.md' if name == 'claude' else 'AGENTS.md'))
    return {'clients': clients, 'prepared': prepared, 'disable_native': disable_native,
            'executable': executable, 'vault': vault}


def apply_clients(plan):
    clients = plan['clients']
    prepared = plan['prepared']
    backups = []
    completed = []
    current = 'preflight'
    try:
        with ExitStack() as stack:
            for root in sorted({prepared[name]['root'] for name in clients}, key=str):
                root.mkdir(parents=True, exist_ok=True)
                lock_path = root / 'kelmo/install.lock'
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                lock = stack.enter_context(os.fdopen(descriptor, 'a'))
                fcntl.flock(lock, fcntl.LOCK_EX)
            # Ownership is a write-ahead receipt: recovery accepts only its bounded
            # current/previous entries if a later client write or hook write fails.
            for name in clients:
                current = name
                item = prepared[name]
                if item['ownership_path'] is not None:
                    result = write(item['ownership_path'], item['ownership_data'],
                                   expected_prior=item['ownership_prior'])
                    if result['backup']:
                        backups.append(result['backup'])
            for name in clients:
                current = name
                item = prepared[name]
                root = item['root']
                instruction = install_instructions(
                    root / ('CLAUDE.md' if name == 'claude' else 'AGENTS.md'),
                    disable_native=plan['disable_native'], executable=plan['executable'],
                    vault=plan['vault'])
                if instruction and instruction['backup']:
                    backups.append(instruction['backup'])
                results = (write(item['path'], item['config_data'], expected_prior=item['prior']),
                           write(item['hook_path'], item['hook_data'], expected_prior=item['hook_prior']))
                for result in results:
                    if result['backup']:
                        backups.append(result['backup'])
                completed.append(name)
    except KelmoError as error:
        done = ','.join(completed) if completed else 'none'
        raise KelmoError(error.code,
                        f'Client setup did not complete; completed: {done}; current: {current}. '
                        f'Earlier writes/backups may remain. {error}') from error
    executable = plan['executable']
    return {'clients': clients, 'backups': backups, 'client_restart_required': bool(clients),
            'hook_trust_may_require_approval': bool(clients), 'executable': executable,
            'path_hint': f'Add {Path(executable).parent} to PATH to run kelmo without its full path.'}


def configure_clients(home, executable, vault, client, disable_native, session_mode=None, **profiles):
    return apply_clients(prepare_clients(home, executable, vault, client, disable_native, session_mode, **profiles))


def service_definition(home, executable, vault, platform, executor_config, server_profile=None):
    argv = [str(executable), 'worker', 'daemon', '--vault', str(vault)]
    if executor_config:
        argv += ['--executor-config', str(executor_config)]
    if server_profile:
        argv += ['--server-profile', str(server_profile)]
    if platform == 'darwin':
        path = Path(home) / 'Library/LaunchAgents/org.kelmo.worker.plist'
        data = plistlib.dumps({'Label': 'org.kelmo.worker', 'ProgramArguments': argv,
                              'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                              'ExitTimeOut': STOP_SECONDS})
    elif platform == 'linux':
        path = Path(home) / '.config/systemd/user/kelmo.service'
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        text = ('[Unit]\nDescription=Kelmo worker\n[Service]\nExecStart='
                + ' '.join(quote(a) for a in argv)
                + '\nRestart=on-failure\nRestartSec=5'
                # Only the worker gets SIGTERM; its executor call is not killed before the worker lets go.
                + f'\nKillMode=mixed\nTimeoutStopSec={STOP_SECONDS}'
                + '\n[Install]\nWantedBy=default.target\n')
        data = text.encode()
    else:
        raise KelmoError('E_CONFIG', 'Background installation supports macOS and Linux.')
    return path, data


def service_file(home, executable, vault, platform, executor_config, server_profile=None):
    path, data = service_definition(home, executable, vault, platform, executor_config, server_profile)
    write(path, data)
    return path


def local_daemon(args, executable, vault, server_profile):
    """The machine daemon of a local vault under an installed launcher (kelmod beside it): sessions reach the
    state core over the HTTP endpoint and `kelmod hook`. None for a development checkout or a server (a server
    profile serves remote clients over ssh, not the endpoint)."""
    kelmod = Path(executable).parent / 'kelmod'
    if server_profile or not kelmod.is_file():
        return None
    from .shared import DEFAULT_HTTP_PORT
    from ..core.daemon_client import runtime_dir
    port = int(getattr(args, 'http_port', None) or DEFAULT_HTTP_PORT)
    # Nothing is written here: the vault must still be empty when `initialize` runs.
    state = Path(vault) / '.state'
    return {'kelmod': kelmod, 'state': state, 'port': port, 'url': f'http://127.0.0.1:{port}/mcp',
            'runtime': runtime_dir(Path(vault)), 'spec': state / 'kelmod.spec.json'}


def finish_local_daemon(args, home, executable, vault, daemon, executor_config, server_profile, checked, clients,
                        session_ready, project_result, executor_ready, model_action):
    """Writes the vault's daemon spec and its one user service, stops the 0.1 worker service (two writers on one
    journal otherwise), and starts the daemon."""
    from ..core.daemon_admin import build_spec, write_options, write_private
    from ..core.spec import http_token_at
    from .shared import activate, memory_limits, service_definition
    http_token_at(daemon['state'])
    write_options(daemon['state'], {'role': 'vault', 'port': daemon['port'], 'runtime': str(daemon['runtime']),
                                    'worker_config': str(executor_config) if executor_config else None,
                                    'launcher': str(executable), 'kelmod': str(daemon['kelmod'])})
    spec = build_spec(vault)
    write_private(daemon['spec'], (json.dumps(spec, indent=2) + '\n').encode())
    path, data = service_definition(home, executable, vault, sys.platform, memory_limits(spec))
    write(path, data)
    retired = retire_worker_service(home, sys.platform, stop=not args.no_start)
    active = False
    if not args.no_start:
        activate(path, sys.platform)
        active = True
    embedding_ready = checked.get('checks', {}).get('embedding_model', False)
    return dict(clients, **session_ready, project_hub=project_result, memory_ready=True,
                embedding_ready=embedding_ready, search_degraded=not embedding_ready, executor_ready=executor_ready,
                background_started=active, service_file=str(path), retired_service=retired,
                mcp_url=daemon['url'],
                complete=active and executor_ready and (session_ready['session_memory_mode'] == 'off'
                                                        or session_ready['native_hooks_verified']),
                native_memory_disabled=False, native_memory_prohibited=bool(args.disable_native_memory))


def retire_worker_service(home, platform, *, stop=True):
    """Unloads and removes the 0.1 `kelmo worker daemon` service; returns its path when one was there."""
    if platform == 'darwin':
        path = Path(home) / 'Library/LaunchAgents/org.kelmo.worker.plist'
        command = ['launchctl', 'bootout', f'gui/{os.getuid()}/org.kelmo.worker']
    else:
        path = Path(home) / '.config/systemd/user/kelmo.service'
        command = ['systemctl', '--user', 'disable', '--now', 'kelmo.service']
    # Without `stop` (setup --no-start) nothing is unloaded and the file stays: `kelmo daemon run` refuses to
    # start while it is there, so the worker and the daemon never write one journal together.
    if not stop:
        return None
    # Unloaded whether or not the file is still there: a loaded job outlives its file.
    try:
        subprocess.run(command, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not path.exists():
        return None
    path.unlink()
    return str(path)


def worker_service_file(home, platform=None):
    """The 0.1 worker unit file, when one is installed."""
    platform = platform or sys.platform
    path = (Path(home) / 'Library/LaunchAgents/org.kelmo.worker.plist' if platform == 'darwin'
            else Path(home) / '.config/systemd/user/kelmo.service')
    return path if path.exists() else None


def model_plan(args, vault):
    from ..config import Config
    source = getattr(args, 'model_source', None)
    profile = getattr(args, 'embedding_profile', None)
    skip = getattr(args, 'skip_model', False)
    if sum(bool(item) for item in (source, profile, skip)) > 1:
        raise KelmoError('E_CONFIG', 'Choose model-source, embedding-profile or skip-model.')
    if getattr(args, 'effort', None) and not args.provider:
        raise KelmoError('E_CONFIG', 'Setup effort applies to a selected provider.')
    if args.provider == 'codex' and getattr(args, 'effort', None) == 'max':
        raise KelmoError('E_CONFIG', 'Codex effort supports low, medium, high and xhigh.')
    if source and not Path(source).expanduser().is_dir():
        raise KelmoError('E_INPUT', 'Model source must be an existing directory.')
    if profile:
        from ..migration_cli import read_spec
        if read_spec(profile).get('provider') not in ('local', 'broker'):
            raise KelmoError('E_CONFIG', 'Embedding profile needs a local or broker provider.')
        return 'profile'
    if skip:
        return 'skip'
    config = Config.load(vault) if (vault / 'kelmo.toml').is_file() else None
    cache = config.cache if config else vault / '.cache/embeddings'
    return 'import' if source else 'reuse' if (
        config and config.embedding is not None or (cache / 'model').is_dir()) else 'download'


def project_plan(args, vault):
    from ..project_preview import preview
    return preview(vault, args.project, getattr(args, 'link', None), wiki_root=getattr(args, 'wiki', None))


def apply_project(args, vault):
    from .. import projects
    result = {'project': projects.add(vault, args.project)}
    if getattr(args, 'link', None) is not None:
        result['link'] = projects.link(vault, args.project, args.link)
    return result


def setup(args):
    from .shared import selected as shared_selected, setup as shared_setup
    from . import wizard
    if getattr(args, 'executable', None) is None:
        args.executable = wizard.running_executable()
    if not shared_selected(args) and wizard.wanted(args):
        wizard.choose(args)
        wizard.offer_project(args)
    wants_project = (getattr(args, '_project_requested', False) or getattr(args, 'link', None) is not None
                     or getattr(args, 'project', 'default') != 'default')
    if shared_selected(args):
        if wants_project:
            raise KelmoError('E_CAPABILITY', 'Shared project enrollment must be performed by the server operator.')
        return shared_setup(args)
    project_preview = project_plan(args, args.vault) if wants_project else None
    if getattr(args, 'dry_run', False):
        from .preview import preview
        result = preview(args, linked=getattr(args, 'link', None) is not None)
        if project_preview is not None:
            result['project'] = project_preview
            result['files'].extend(project_preview['files'])
        return result
    from ..initialization import initialize
    from ..model_store import import_model
    from ..cli import dispatch
    from ..interface import build_parser
    from .providers import arguments as provider_arguments
    provider_argv = provider_arguments(args, args.executable, check_provider=True)
    executor_config = args.executor_config.expanduser().resolve() if args.executor_config else None
    executor_settings = None
    if executor_config:
        from ..daemon import validate_config
        executor_settings = validate_config(read_json(executor_config, required=True))
    home = args.home.expanduser().resolve()
    executable = args.executable.expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise KelmoError('E_SETUP', 'Installed memory executable is missing.')
    vault = args.vault.expanduser().resolve()
    model_action = model_plan(args, vault)
    server_profile = getattr(args, 'server_profile', None)
    if server_profile:
        from ..server import ServerProfile
        from ..config import Config
        profile = ServerProfile.load(server_profile)
        if profile.bindings.authority_id != Config.load(vault).vault_id:
            raise KelmoError('E_AUTHORITY', 'The server profile belongs to another vault.')
        if profile.fenced and not args.no_start:
            raise KelmoError('E_FENCED', 'The server is fenced; prepare with --no-start before cutover.')
    from .session_setup import prepare as prepare_sessions, finish as finish_sessions
    session_plan=prepare_sessions(args,home,vault)
    daemon = local_daemon(args, executable, vault, server_profile)
    client_plan = prepare_clients(home, executable, vault, args.client,
                                  args.disable_native_memory,session_plan['settings']['mode'], edge=daemon,
                                  **profile_options(args))
    initialize(vault, args.label, wiki_root=args.wiki,
               project=getattr(args, 'project', 'default'),
               git_name=getattr(args, 'git_name', None), git_email=getattr(args, 'git_email', None))
    project_result = apply_project(args, vault) if wants_project else None
    if model_action == 'profile':
        from ..migration_cli import read_spec
        from ..model_selection import use
        use(vault, read_spec(args.embedding_profile))
    elif model_action == 'import':
        import_model(vault, args.model_source)
    elif model_action == 'download':
        with tempfile.TemporaryDirectory(prefix='kelmo-model-fetch-') as temporary:
            source = Path(temporary)
            fetch_model(source)
            import_model(vault, source)
    parser, commands = build_parser('')
    checked = dispatch(parser.parse_args(['doctor', '--vault', str(vault)]), commands)
    if not checked['healthy']:
        ignored = {'sessions'} | ({'embedding_model'} if model_action == 'skip' else set())
        failed = sorted(key for key, value in checked.get('checks', {}).items() if value is False and key not in ignored)
        detail = ', '.join(failed) if failed else 'unknown'
        if failed or not checked.get('checks'):
            raise KelmoError('E_SETUP', f'Memory health checks failed: {detail}.')
    if (server_profile and profile.archive_process == 'confined'
            and not checked.get('runtime_isolation', {}).get('launcher_available')):
        raise KelmoError('E_SETUP', 'This server runs its archive confined, and the sandbox launcher is '
                                    'unavailable (bubblewrap on x86_64 Linux, sandbox-exec on macOS).')
    if args.provider:
        from ..config import Config
        executor_config = Config.load(vault).state / 'executor.json'
        provider_argv[0] = str(executable)
        executor_settings = {'argv': provider_argv, 'timeout_seconds': 600}
        write(executor_config, json.dumps(executor_settings).encode())
    executor_ready = bool(executor_settings and executor_settings['argv'])
    if executor_config and executor_ready:
        from ..daemon import execute, validate_config
        executor_settings = validate_config(executor_settings or read_json(executor_config, required=True))
        probe = execute(executor_settings['argv'], {'task': 'probe'},
                        timeout=executor_settings['timeout_seconds'])
        if probe != {'ok': True}:
            reason = probe.get('reason') if isinstance(probe, dict) else None
            detail = ('the provider CLI is not logged in; log in with it once, then rerun setup'
                      if reason == 'login' else 'executor answered ' + json.dumps(probe)[:300])
            raise KelmoError('E_SETUP', f'Executor readiness probe failed: {detail}.')
        # A provider CLI rewrites its own client configuration on every call (Claude Code updates
        # ~/.claude.json), so the plan read before the probe is stale: read the clients again.
        client_plan = prepare_clients(home, executable, vault, args.client, args.disable_native_memory,
                                      session_plan['settings']['mode'], edge=daemon, **profile_options(args))
    session_ready=finish_sessions(args,session_plan,vault,executable,executor_config)
    clients = apply_clients(client_plan)
    if daemon is not None:
        return finish_local_daemon(args, home, executable, vault, daemon, executor_config, server_profile,
                                   checked, clients, session_ready, project_result, executor_ready, model_action)
    path = service_file(home, executable, vault, sys.platform, executor_config, server_profile)
    active = False
    if not args.no_start:
        commands = ([['launchctl', 'bootout', f'gui/{os.getuid()}/org.kelmo.worker'],
                     ['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)]] if sys.platform == 'darwin'
                    else [['systemctl', '--user', 'daemon-reload'],
                          ['systemctl', '--user', 'enable', 'kelmo.service'],
                          ['systemctl', '--user', 'restart', 'kelmo.service']])
        for index, command in enumerate(commands):
            result = subprocess.run(command, capture_output=True, timeout=30)
            if result.returncode and not (sys.platform == 'darwin' and index == 0):
                raise KelmoError('E_SETUP', 'Background service activation failed; configuration was retained.')
        active = True
    embedding_ready = checked.get('checks', {}).get('embedding_model', False)
    return dict(clients, **session_ready, project_hub=project_result, memory_ready=True, embedding_ready=embedding_ready,
                search_degraded=not embedding_ready, executor_ready=executor_ready,
                background_started=active, service_file=str(path),
                complete=active and executor_ready and (session_ready['session_memory_mode']=='off'
                                                        or session_ready['native_hooks_verified']), native_memory_disabled=False,
                native_memory_prohibited=bool(args.disable_native_memory))


def fetch_model(destination):
    from ..model_store import FILES, REVISION, SOURCE_REPO
    hint = ' Retry or provide verified files with --model-source.'
    for name, (size, checksum) in FILES.items():
        url = f'https://huggingface.co/{SOURCE_REPO}/resolve/{REVISION}/{name}'
        digest = hashlib.sha256()
        received = 0
        try:
            with urllib.request.urlopen(url, timeout=60) as response, (destination / name).open('xb') as output:
                while chunk := response.read(65536):
                    received += len(chunk)
                    if received > size:
                        raise KelmoError('E_SETUP', 'Model download exceeded pinned size.' + hint)
                    digest.update(chunk)
                    output.write(chunk)
        except KelmoError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise KelmoError('E_SETUP',
                            'Model download failed; retry or provide verified files with --model-source.') from error
        if received != size or digest.hexdigest() != checksum:
            raise KelmoError('E_SETUP', 'Downloaded model failed checksum verification.' + hint)
