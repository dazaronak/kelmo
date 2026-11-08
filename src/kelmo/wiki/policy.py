#!/usr/bin/env python3
"""Read-only budget checks: wiki hub sizes and the instruction chains the vault configures."""
import argparse
import json
import math
import os
from pathlib import Path
import re

if __package__:
    from ..budget import DEFAULT_TOKEN_LIMIT, load as load_budget
else:  # run as a script: python3 src/kelmo/wiki/policy.py --vault /path/to/vault
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from budget import DEFAULT_TOKEN_LIMIT, load as load_budget

HUB_BYTE_LIMIT = 24000
HUB_LINE_LIMIT = 200
TOKEN_ESTIMATE = 2.5


def anchor_pairs(vault, budget):
    """The prefixes the vault configures, longest first; no prefix is built in."""
    return sorted((budget or {}).get('anchors', ()), key=lambda pair: (-len(pair[0]), pair[0]))


def expand(source, vault, overlays, stack=(), anchors=None, unresolved=None):
    source = source.resolve()
    if source in stack:
        raise ValueError('anchor cycle')
    anchors = anchors if anchors is not None else anchor_pairs(vault, None)
    text = overlays.get(source)
    if text is None:
        text = source.read_text(encoding='utf-8')
    output = []
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r'\s*@(.+?)\s*', line)
        if not match:
            output.append(line)
            continue
        name = match.group(1).strip('"\'')
        for prefix, root in anchors:
            if name.startswith(prefix):
                target = root / name[len(prefix):]
                break
        else:
            target = Path(name).expanduser()
            if not target.is_absolute():
                target = source.parent / target
        try:
            output.append(expand(target, vault, overlays, (*stack, source), anchors, unresolved))
        except FileNotFoundError:
            if unresolved is None:
                raise
            unresolved.append(name)
            output.append(line)
    return ''.join(output)


def instruction_chains(budget, vault, overlays, anchors):
    """One entry per instruction chain and project file it loads; a chain with no file is uncovered."""
    chains, uncovered = [], []
    for chain in (budget or {}).get('chains', ()):
        root = Path(chain['root'])
        shared = [path for path in (root / name for name in chain['global']) if path.is_file()]
        if not shared:
            uncovered.append(chain['name'])
            continue
        projects = sorted({path for pattern in chain['projects'] for path in root.glob(pattern)})
        for project in [None, *projects]:
            label = chain['name'] if project is None else chain['name'] + '/' + project.parent.name
            missing = []
            try:
                text = ''.join(expand(path, vault, overlays, anchors=anchors, unresolved=missing) for path in shared)
                if project is not None:
                    text += expand(project, vault, overlays, anchors=anchors, unresolved=missing)
            except (OSError, ValueError) as error:
                chains.append({'profile': label, 'error': type(error).__name__})
                continue
            entry = {'profile': label, 'estimated_tokens': math.ceil(len(text.encode('utf-8')) / TOKEN_ESTIMATE)}
            if missing:
                entry['unresolved_imports'] = sorted(set(missing))
            chains.append(entry)
    return chains, uncovered


def check(vault, overlays=None, wiki_root=None, budget=None):
    overlays = overlays or {}
    budget = load_budget(vault) if budget is None else budget
    limit = (budget or {}).get('token_limit', DEFAULT_TOKEN_LIMIT)
    anchors = anchor_pairs(vault, budget)
    violations = []
    chains, uncovered = instruction_chains(budget, vault, overlays, anchors)
    unresolved = []
    for chain in chains:
        if 'error' in chain:
            violations.append('anchor resolution failed: ' + chain['profile'] + ' (' + chain['error'] + ')')
        elif chain['estimated_tokens'] > limit:
            violations.append('HOT budget exceeded: ' + chain['profile'])
        unresolved.extend(chain['profile'] + ' -> ' + item for item in chain.get('unresolved_imports', ()))
    wiki = Path(wiki_root).resolve() if wiki_root is not None else Path(vault) / 'knowledge/wiki'
    embedded = wiki == Path(vault) / 'knowledge/wiki'
    files = (set(wiki.glob('**/*.md')) | {p for p in overlays if p.is_relative_to(wiki)}) if embedded else set()
    for path in files:
        hub = (path.name in {'person.md', 'index.md', 'life.md'} or path.stem == path.parent.name
               or path.stem.endswith(('-wip', '-backlog')))
        if not hub:
            continue
        text = overlays.get(path)
        if text is None:
            text = path.read_text(encoding='utf-8')
        if len(text.encode()) > HUB_BYTE_LIMIT or len(text.splitlines()) > HUB_LINE_LIMIT:
            violations.append('hub size exceeded: ' + str(path.relative_to(wiki)))
    return {'ok': not violations, 'token_limit': limit,
            'scope': {'hub_limits_enforced': embedded, 'hot_chains_checked': len(chains),
                      'hot_chains_configured': len((budget or {}).get('chains', ())),
                      'hot_chains_uncovered': uncovered, 'hot_imports_unresolved': unresolved},
            'metric': 'estimated tokens: UTF-8 bytes / ' + str(TOKEN_ESTIMATE) + '; comments included',
            'chains': chains, 'violations': violations}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=os.environ.get('VAULT_ROOT'),
                        required='VAULT_ROOT' not in os.environ)
    args = parser.parse_args()
    result = check(args.vault.expanduser().resolve())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
