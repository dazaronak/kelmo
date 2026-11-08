"""Explicit, backed-up instruction blocks for project hub delivery."""
import re
import fcntl
import json
import hashlib
import shlex
from pathlib import Path

from .errors import KelmoError
from .install.setup import write

BEGIN = '<!-- kelmo:hub begin -->'
END = '<!-- kelmo:hub end -->'


def import_lines(current, project, strict=False, scope='both'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', project) or project == 'mandatory':
        raise KelmoError('E_INPUT', 'Invalid project name.')
    current = Path(current).expanduser().absolute()
    if strict:
        if scope != 'both':
            raise KelmoError('E_INPUT', 'Strict import cannot be split across files.')
        return f'@{current / (project + ".full.md")}'
    if scope not in ('global', 'project', 'both'):
        raise KelmoError('E_INPUT', 'Invalid import scope.')
    lines = []
    if scope in ('global', 'both'):
        lines.append(f'@{current / "mandatory.md"}')
    if scope in ('project', 'both'):
        lines.append(f'@{current / (project + ".md")}')
    return '\n'.join(lines)


def _render(prior, content):
    begin, end = BEGIN.encode(), END.encode()
    if BEGIN in content or END in content:
        raise KelmoError('E_CONFIG', 'Hub content contains reserved block markers.')
    if prior.count(begin) != prior.count(end) or prior.count(begin) > 1:
        raise KelmoError('E_CONFIG', 'Ambiguous Kelmo hub markers; repair them before retrying.')
    block = begin + b'\n' + content.encode('utf-8') + b'\n' + end
    if begin in prior:
        start, stop = prior.index(begin), prior.index(end)
        if stop < start:
            raise KelmoError('E_CONFIG', 'Reversed Kelmo hub markers.')
        return prior[:start] + block + prior[stop + len(end):]
    if prior and not prior.endswith(b'\n'):
        prior += b'\n'
    if prior and not prior.endswith(b'\n\n') and prior != b'\n':
        prior += b'\n'  # one blank line keeps the block apart from the user's last line
    return prior + block + b'\n'


def write_block(path, content):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_INPUT', 'Instruction paths cannot contain symlinks.')
    prior = path.read_bytes() if path.exists() else None
    return write(path, _render(prior or b'', content), expected_prior=prior)


def block_present(path, content):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        return False
    try:
        prior = path.read_bytes()
        return BEGIN.encode() in prior and _render(prior, content) == prior
    except (OSError, KelmoError):
        return False


def _state_path(config):
    return Path(config.state) / 'hub-embed.json'


def _block_digest(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    try:
        raw = path.read_bytes()
        begin, end = BEGIN.encode(), END.encode()
        if raw.count(begin) != 1 or raw.count(end) != 1 or raw.index(begin) > raw.index(end):
            return None
        return hashlib.sha256(raw[raw.index(begin):raw.index(end) + len(end)]).hexdigest()
    except OSError:
        return None


def _read_selection(config):
    path = _state_path(config)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError('E_CONFIG', 'Hub selection path contains a symlink.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise KelmoError('E_CONFIG', 'Hub selection is unreadable or invalid.') from error
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('projects'), dict):
        raise KelmoError('E_CONFIG', 'Invalid hub selection.')
    return value['projects']


def select(config, project, provider, path, current, *, scope='both', strict=False, hook_file=None):
    """Remember explicit instruction targets; preserve independently selected Claude scopes."""
    import_lines(current, project, strict=strict, scope=scope)
    if provider not in ('claude', 'codex'):
        raise KelmoError('E_INPUT', 'Invalid hub provider.')
    target = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise KelmoError('E_INPUT', 'Instruction paths cannot contain symlinks.')
    state = _state_path(config)
    if any(p.is_symlink() for p in (state, *state.parents)):
        raise KelmoError('E_CONFIG', 'Hub selection path contains a symlink.')
    state.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state.with_suffix('.lock')
    if lock_path.is_symlink():
        raise KelmoError('E_CONFIG', 'Hub selection lock cannot be a symlink.')
    with lock_path.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        projects = _read_selection(config)
        entry = {'path': str(target), 'current': str(Path(current).expanduser().absolute()),
                 'scope': scope, 'strict': strict, 'block_sha256': _block_digest(target),
                 'hook_file': str(Path(hook_file).expanduser().absolute()) if hook_file else None}
        selected = projects.setdefault(project, {})
        items = selected.setdefault(provider, [])
        items[:] = [item for item in items if item['path'] != str(target)]
        items.append(entry)
        write(state, (json.dumps({'version': 1, 'projects': projects}, indent=2) + '\n').encode())


def _hook_order(path):
    if not path:
        return 'not_configured'
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        return 'repair_needed'
    try:
        settings = json.loads(path.read_text(encoding='utf-8'))
        hooks = settings['hooks']
        for event in ('SessionStart', 'UserPromptSubmit'):
            groups = hooks[event]
            entries = [entry for group in groups for entry in group['hooks']]
            if not entries or entries[0].get('statusMessage') != 'kelmo managed hook':
                return 'repair_needed'
            argv = shlex.split(entries[0]['command'])
            expected = 'start' if event == 'SessionStart' else 'prompt'
            if len(argv) < 3 or argv[1:3] != ['hook', expected]:
                return 'repair_needed'
        return 'kelmo_first'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 'repair_needed'


def _status(items, project, provider, content=None, update=False, *, snapshot=None):
    if not items:
        return {'mode': 'hook', 'verified': False, 'changed': False, 'repair': ''}
    if not isinstance(items, list):
        raise KelmoError('E_CONFIG', 'Invalid hub selection entries.')
    verified, changed, repairs, scopes, orders, targets, hook_repairs = True, False, [], set(), [], [], []
    generations = set()
    for item in items:
        try:
            path, current = Path(item['path']), Path(item['current'])
            scope, strict = item['scope'], item['strict']
            lines = import_lines(current, project, strict=strict, scope=scope)
            scopes.add('both' if strict else scope)
        except (KeyError, TypeError, ValueError) as error:
            raise KelmoError('E_CONFIG', 'Invalid hub selection entry.') from error
        if provider == 'codex':
            if content is not None and update:
                changed |= write_block(path, content)['changed']
            if content is None:
                valid = bool(item.get('block_sha256') and _block_digest(path) == item['block_sha256'])
            else:
                valid = block_present(path, content)
            command = ['kelmo', 'hub', 'embed', '--codex', '--project', project, '--current', str(current), '--file', str(path)]
        else:
            previous = item.get('_renamed_from')
            if update and previous and block_present(path, import_lines(current, previous, strict=strict, scope=scope)):
                changed |= write_block(path, lines)['changed']
            valid = block_present(path, lines)
            for line in lines.splitlines():
                source = Path(line[1:])
                try:
                    if source.is_symlink() or not source.is_file():
                        raise ValueError
                    raw = source.read_bytes()
                    marker = re.fullmatch(rb'<!-- kelmo generation ([0-9]+) -->', raw.split(b'\n', 1)[0])
                    if marker is None:
                        raise ValueError
                    generation = int(marker.group(1))
                    generations.add(generation)
                    if snapshot is not None:
                        expected = snapshot['files'].get(source.name)
                        if (generation != snapshot['generation'] or expected is None
                                or hashlib.sha256(raw).hexdigest() != expected['sha256']):
                            valid = False
                            repairs.append('Generated import differs from the current verified publication: ' + str(source))
                except (OSError, UnicodeError, ValueError):
                    valid = False
                    repairs.append('Missing or invalid generated import: ' + str(source))
            command = ['kelmo', 'hub', 'import', '--project', project, '--current', str(current), '--scope', scope,
                       '--file', str(path)] + (['--strict'] if strict else [])
        verified &= valid
        order = _hook_order(item.get('hook_file'))
        orders.append(order)
        if order == 'repair_needed':
            hook_repairs.append('Place Kelmo SessionStart and UserPromptSubmit first in ' + str(item['hook_file']) + '.')
        elif order == 'not_configured':
            hook_repairs.append('Set --hook-file on the hub command to check hook order.')
        targets.append({'path': str(path), 'verified': valid, 'hook_file': item.get('hook_file'),
                        'hook_order': order, 'repair': '' if valid else shlex.join(command)})
        if not valid:
            repairs.append(shlex.join(command))
    if provider == 'claude' and len(generations) > 1:
        verified = False
        repairs.append('Imported files name different Kelmo generations; retry after publication completes.')
    if provider == 'claude' and not ('both' in scopes or {'global', 'project'} <= scopes):
        verified = False
        repairs.append('Configure both global and project imports, or use --strict.')
    order = ('repair_needed' if 'repair_needed' in orders else
             'not_configured' if 'not_configured' in orders else 'kelmo_first')
    return {'mode': 'embed', 'verified': verified, 'changed': changed, 'repair': '; '.join(repairs),
            'targets': targets, 'hook_order': order,
            'hook_repair': '; '.join(hook_repairs),
            'native_trust': 'Approve and verify hook trust in the client.'}


def sync(config, project, provider, *, content=None, cwd=None, snapshot=None, aliases=None):
    if provider not in ('claude', 'codex'):
        raise KelmoError('E_INPUT', 'Invalid hub provider.')
    selections = _read_selection(config)
    names = [old for old, new in (aliases or {}).items() if new == project] + [project]
    by_path = {}
    for name in names:
        selected = selections.get(name, {}).get(provider, [])
        if not isinstance(selected, list):
            raise KelmoError('E_CONFIG', 'Invalid hub selection entries.')
        for item in selected:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise KelmoError('E_CONFIG', 'Invalid hub instruction target.')
            by_path[item['path']] = dict(item, **({'_renamed_from': name} if name != project else {}))
    items = list(by_path.values())
    if provider == 'codex' and items:
        if cwd is not None:
            selected_cwd = Path(cwd).expanduser().absolute()
            items = [item for item in items if selected_cwd.is_relative_to(Path(item['path']).parent)]
            if len(items) > 1:
                depth = max(len(Path(item['path']).parent.parts) for item in items)
                items = [item for item in items if len(Path(item['path']).parent.parts) == depth]
        if len(items) != 1:
            raise KelmoError('E_CONFIG', 'Select one Codex instruction target for this repository.')
    elif provider == 'claude' and cwd is not None:
        selected_cwd = Path(cwd).expanduser().absolute()
        items = [item for item in items if item['scope'] == 'global'
                 or selected_cwd.is_relative_to(Path(item['path']).parent)]
    result = _status(items, project, provider, content, True, snapshot=snapshot)
    if result['changed']:
        for item in items:
            select(config, project, provider, item['path'], item['current'],
                   scope=item['scope'], strict=item['strict'], hook_file=item.get('hook_file'))
    return result


def inspect(config):
    projects = _read_selection(config)
    return {project: {provider: _status(items, project, provider)
                      for provider, items in selected.items()}
            for project, selected in projects.items()}
