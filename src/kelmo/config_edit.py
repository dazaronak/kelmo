"""Replace one configuration table while retaining unrelated TOML and a verified backup."""
import copy
import fcntl
import json
import os
import re
import tomllib

from .config import Config
from .errors import KelmoError


def table(parts, values):
    result = ['[' + '.'.join(json.dumps(part) for part in parts) + ']']
    for key, value in values.items():
        if not isinstance(value, dict):
            result.append(json.dumps(key) + ' = ' + json.dumps(value, ensure_ascii=False))
    for key, value in values.items():
        if isinstance(value, dict):
            result.extend(['', table([*parts, key], value)])
    return '\n'.join(result) + '\n'


def replace_table(root, name, values, *, dry_run=False, expected=None):
    if dry_run:
        return _replace_table(root, name, values, dry_run=True, expected=expected)
    config = Config.load(root)
    lock_path = config.internal('.state/config.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _replace_table(root, name, values, dry_run=dry_run, expected=expected)


def _replace_table(root, name, values, *, dry_run=False, expected=None):
    from .install.setup import write
    config = Config.load(root)
    path = config.root / 'kelmo.toml'
    prior = path.read_bytes()
    text = prior.decode('utf-8')
    before = tomllib.loads(text)
    if expected is not None and before.get(name, {}) != expected:
        raise KelmoError('E_CONFLICT', 'Configuration table changed during the operation; retry.')
    if before.get(name) == values:
        return {'changed': False, 'applied': False, 'backup': None, 'path': str(path)}
    headers = list(re.finditer(r'(?m)^[ \t]*\[([^\n]+)\][ \t]*(?:#.*)?$', text))
    for index in range(len(headers) - 1, -1, -1):
        match = headers[index]
        # Parse just the header so quoted table names and comments retain TOML semantics.
        try:
            owned = set(tomllib.loads(match.group() + '\n')) == {name}
        except ValueError as error:
            raise KelmoError('E_CONFIG', 'Unsupported configuration table header.') from error
        if owned:
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            text = text[:match.start()] + text[end:]
    text = text.rstrip() + '\n\n' + table([name], values)
    expected = copy.deepcopy(before)
    expected[name] = values
    try:
        if tomllib.loads(text) != expected:
            raise ValueError('Unrelated configuration changed.')
    except (TypeError, ValueError) as error:
        raise KelmoError('E_CONFIG', f'Use a [{name}] table; the current TOML cannot be changed safely.') from error
    if dry_run:
        return {'changed': True, 'applied': False, 'backup': None, 'path': str(path)}
    return dict(write(path, text.encode('utf-8'), expected_prior=prior), applied=True)
