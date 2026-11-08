"""The vault's optional [budget] table: which instruction files the budget check measures."""
import tomllib
from pathlib import Path
import re

if __package__:
    from .errors import KelmoError
else:
    from errors import KelmoError

DEFAULT_TOKEN_LIMIT = 60000
TOKEN_LIMIT_RANGE = (1, 1000000)
MAX_CHAINS = 16
CHAIN_FIELDS = {'name', 'root', 'global', 'projects'}
NAME = re.compile(r'[A-Za-z0-9_.-]{1,60}')


def _directory(value, vault, label):
    if not isinstance(value, str) or not value.strip() or '\\' in value or '..' in value:
        raise KelmoError('E_CONFIG', label + ' must be a directory path without a parent step.')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(vault) / path
    return path


def _patterns(value, label):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
        raise KelmoError('E_CONFIG', label + ' must be a path or a list of paths.')
    for item in values:
        path = Path(item)
        if path.is_absolute() or '..' in path.parts or '**' in path.parts or '\\' in item:
            raise KelmoError('E_CONFIG', label + ' must stay relative to the chain root, one segment per level.')
    return list(dict.fromkeys(item.strip() for item in values))


def validate(raw, vault):
    """Return the measured shape, or None when the vault configures no budget table."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'token_limit', 'anchors', 'chain'}:
        raise KelmoError('E_CONFIG', 'Budget configuration has unsupported fields.')
    limit = raw.get('token_limit', DEFAULT_TOKEN_LIMIT)
    if type(limit) is not int or not TOKEN_LIMIT_RANGE[0] <= limit <= TOKEN_LIMIT_RANGE[1]:
        raise KelmoError('E_CONFIG', 'Budget token_limit must be a positive integer at most '
                                    + str(TOKEN_LIMIT_RANGE[1]) + '.')
    chains = []
    entries = raw.get('chain', [])
    if not isinstance(entries, list):
        raise KelmoError('E_CONFIG', 'Each budget chain must be its own [[budget.chain]] table.')
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) - CHAIN_FIELDS:
            raise KelmoError('E_CONFIG', 'Budget chain ' + str(index) + ' has unsupported fields.')
        name = entry.get('name', 'chain-' + str(index))
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise KelmoError('E_CONFIG', 'Budget chain ' + str(index) + ' name must use letters, digits, dot, '
                                        'underscore or hyphen.')
        if any(existing['name'] == name for existing in chains):
            raise KelmoError('E_CONFIG', 'Budget chain names must be unique: ' + name + '.')
        chains.append({'name': name, 'root': _directory(entry.get('root'), vault, 'Budget chain ' + name + ' root'),
                       'global': _patterns(entry.get('global', []), 'Budget chain ' + name + ' global'),
                       'projects': _patterns(entry.get('projects', []), 'Budget chain ' + name + ' projects')})
    if len(chains) > MAX_CHAINS:
        raise KelmoError('E_CONFIG', 'Budget configuration accepts at most ' + str(MAX_CHAINS) + ' chains.')
    anchors = []
    raw_anchors = raw.get('anchors', {})
    if not isinstance(raw_anchors, dict):
        raise KelmoError('E_CONFIG', 'Budget anchors must be a [budget.anchors] table of prefix and directory.')
    for prefix, value in raw_anchors.items():
        if not isinstance(prefix, str) or not prefix.startswith('~/') or not prefix.endswith('/') or '..' in prefix:
            raise KelmoError('E_CONFIG', 'Each budget anchor prefix must start and end with /.')
        anchors.append((prefix, _directory(value, vault, 'Budget anchor ' + prefix)))
    anchors.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    return {'token_limit': limit, 'chains': chains, 'anchors': anchors}


def public(budget):
    """The validated table in the shape a reader sees: every path as a string, None when the vault configures none."""
    if budget is None:
        return None
    return {'token_limit': budget['token_limit'],
            'chains': [{'name': chain['name'], 'root': str(chain['root']), 'global': chain['global'],
                        'projects': chain['projects']} for chain in budget['chains']],
            'anchors': [[prefix, str(path)] for prefix, path in budget['anchors']]}


def load(vault):
    """Read the selected vault's own [budget] table; None when it configures none."""
    path = Path(vault) / 'kelmo.toml'
    try:
        with path.open('rb') as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            return None
        data = tomllib.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return validate(data.get('budget'), vault)
