"""Remove exact-owned client integrations with recoverable per-file writes."""
from contextlib import ExitStack
import copy
import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import tomllib

from .instructions import BASE, LEGACY_BASE, BEGIN, END, PROHIBITION, normalize_binding
from .setup import client_config, client_paths, mcp_entry, profile_options, validate_hook_shapes, write
from .hooks import EVENTS, variants
from ..errors import KelmoError


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_CONFIG', 'Removal paths cannot contain symlinks.')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise KelmoError('E_CONFIG', 'Removal requires regular client files.')
    with os.fdopen(descriptor, 'rb') as source:
        raw = source.read(2097153)
    if len(raw) > 2097152:
        raise KelmoError('E_CONFIG', 'Client file exceeds the two MiB removal limit.')
    return raw


def _region(raw, begin, end):
    if begin not in raw and end not in raw:
        return None
    if raw.count(begin) != 1 or raw.count(end) != 1 or raw.index(begin) > raw.index(end):
        raise KelmoError('E_CONFIG', 'Ambiguous managed markers; removal stopped.')
    return raw.index(begin), raw.index(end) + len(end)


def prepare(args):
    if args.vault is None:
        raise KelmoError('E_INPUT', 'Removal requires an explicit --vault binding.')
    home = Path(args.home).expanduser().absolute()
    executable, vault = normalize_binding(args.executable), normalize_binding(args.vault)
    clients = ['claude', 'codex'] if args.client == 'both' else [args.client]
    files, observed, roots = [], {}, []

    def observe(path):
        raw = _read(path)
        observed[path] = raw
        return raw

    def change(path, before, after):
        if before is not None and before != after:
            files.append({'path': path, 'before': before, 'after': after})

    entries = [mcp_entry(executable, vault, reconnect=value) for value in (True, False)]
    for client in clients:
        root, path = client_paths(home, client, **profile_options(args))
        roots.append(root)
        prior = observe(path)
        if client == 'claude':
            _, data = client_config(path, 'json')
            servers = data.get('mcpServers', {})
            if not isinstance(servers, dict):
                raise KelmoError('E_CONFIG', 'Invalid MCP entries; removal stopped.')
            receipt = root / 'kelmo/ownership.json'
            observe(receipt)
            _, owner = client_config(receipt, 'json')
            if 'kelmo' in servers:
                if (servers['kelmo'] not in entries
                        or owner.get('current_owned_entry', owner.get('claude_mcp_entry')) != servers['kelmo']):
                    raise KelmoError('E_CONFIG', 'Claude MCP ownership differs; removal stopped.')
                del servers['kelmo']
                change(path, prior, (json.dumps(data, indent=2) + '\n').encode())
        else:
            try:
                text = prior.decode('utf-8') if prior is not None else ''
                data = tomllib.loads(text)
                servers = data.get('mcp_servers', {})
                if not isinstance(servers, dict):
                    raise KelmoError('E_CONFIG', 'Invalid MCP entries; removal stopped.')
                region = _region(text, '# kelmo:begin', '# kelmo:end')
                if region is not None:
                    if servers.get('kelmo') not in entries:
                        raise KelmoError('E_CONFIG', 'Codex MCP binding changed; removal stopped.')
                    start, end = region
                    after = text[:start] + text[end:]
                    expected = copy.deepcopy(data)
                    del expected['mcp_servers']['kelmo']
                    if not expected['mcp_servers']:
                        del expected['mcp_servers']
                    parsed = tomllib.loads(after)
                    if parsed.get('mcp_servers') == {}:
                        parsed.pop('mcp_servers')
                    if parsed != expected:
                        raise KelmoError('E_CONFIG', 'Codex removal changes unrelated settings.')
                    change(path, prior, after.encode())
                elif 'kelmo' in servers:
                    raise KelmoError('E_CONFIG', 'Unmarked Codex MCP entry; removal stopped.')
            except (UnicodeError, ValueError) as error:
                raise KelmoError('E_CONFIG', 'Invalid Codex configuration; removal stopped.') from error

        path = root / ('settings.json' if client == 'claude' else 'hooks.json')
        prior = observe(path)
        _, settings = client_config(path, 'json')
        hooks = settings.get('hooks', {})
        validate_hook_shapes(hooks)
        changed = False
        for event in EVENTS:
            expected = variants(executable,vault,client,event)
            groups = []
            for group in hooks.get(event, []):
                remaining = []
                removed = False
                for member in group.get('hooks', []):
                    if member.get('statusMessage') == 'kelmo managed hook':
                        if member not in expected:
                            raise KelmoError('E_CONFIG', 'Managed hook changed; removal stopped.')
                        changed = removed = True
                    else:
                        remaining.append(member)
                if removed:
                    if remaining or set(group) != {'hooks'}:
                        groups.append(dict(group, hooks=remaining))
                else:
                    groups.append(group)
            if event in hooks:
                hooks[event] = groups
        if changed:
            change(path, prior, (json.dumps(settings, indent=2) + '\n').encode())

        path = root / ('CLAUDE.md' if client == 'claude' else 'AGENTS.md')
        prior = observe(path)
        region = _region(prior or b'', BEGIN, END)
        if region is not None:
            start, end = region
            binding = shlex.join([executable, '--vault', vault])
            allowed = [BEGIN + b'\n' + (body.replace('@command@', binding) + policy).encode() + END
                       for body in (BASE, LEGACY_BASE) for policy in ('', PROHIBITION)]
            if prior[start:end] not in allowed:
                raise KelmoError('E_CONFIG', 'Managed instructions changed; removal stopped.')
            change(path, prior, prior[:start] + prior[end:])
    return {'files': files, 'observed': observed, 'roots': roots, 'clients': clients}


def apply(plan):
    if not plan['files']:
        return []
    backups = []
    try:
        with ExitStack() as stack:
            for root in sorted(set(plan['roots'])):
                if not root.exists():
                    continue
                lock = root / 'kelmo/install.lock'
                if any(p.is_symlink() for p in (lock, *lock.parents)):
                    raise KelmoError('E_CONFIG', 'Removal lock cannot use symlinks.')
                lock.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                stream = stack.enter_context(os.fdopen(fd, 'a'))
                fcntl.flock(stream, fcntl.LOCK_EX)
            for path, prior in plan['observed'].items():
                if _read(path) != prior:
                    raise KelmoError('E_IO', 'Client configuration changed; removal stopped.')
            for item in plan['files']:
                result = write(item['path'], item['after'], expected_prior=item['before'])
                if result['backup']:
                    backups.append(result['backup'])
    except (KelmoError, OSError) as error:
        raise KelmoError('E_IO', 'Removal did not complete; earlier writes and backups were retained. '
                        'Resolve the conflict and rerun removal.') from error
    return backups


def uninstall(args):
    plan = prepare(args)
    backups = apply(plan) if args.apply else []
    return {'applied': args.apply, 'clients': plan['clients'],
            'files': [str(item['path']) for item in plan['files']], 'backups': backups,
            'background_retained': True, 'data_retained': True,
            'client_restart_required': bool(args.apply and plan['files'])}
