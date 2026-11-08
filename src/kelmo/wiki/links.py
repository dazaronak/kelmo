#!/usr/bin/env python3
"""Validate wiki links and derive backlinks without rewriting the wiki."""
import argparse
import json
import posixpath
from pathlib import Path, PurePosixPath
import re

from kelmo.store.graph.parser import lookup_keys, parse_text, slugify, target_key


class LinkIndex:
    """Resolve literal paths before source-relative paths, suffixes and aliases.

    The index contains supplied snapshot bytes only; resolving a link never opens
    its target. All ambiguous candidates survive, including colliding aliases.
    """

    def __init__(self, contents):
        self.exact = {}
        self.candidates = {}
        self.metadata_errors = []
        for path, raw in sorted(contents.items()):
            parsed = parse_text(raw)
            identity = target_key(path)
            self.exact.setdefault(identity, set()).add(path)
            if parsed['error']:
                self.metadata_errors.append(path)
                continue
            for key in lookup_keys(identity, parsed['aliases']):
                self.candidates.setdefault(key, set()).add(path)

    def resolve(self, value, source=None):
        if not isinstance(value, str):
            return ()
        raw = value.strip()
        if raw.startswith('[[') and raw.endswith(']]'):
            raw = raw[2:-2]
        raw = raw.split('|', 1)[0].split('#', 1)[0].strip().removeprefix('wiki/')
        name = target_key(raw, allow_parent=True)
        if not name or name.casefold().startswith(('tag:', 'unresolved:')):
            return ()
        relative = raw.startswith(('./', '../')) or '..' in PurePosixPath(raw).parts
        if not relative and name in self.exact:
            return tuple(sorted(self.exact[name]))
        if source is not None:
            contextual = posixpath.normpath(posixpath.join(posixpath.dirname(source), name))
            if contextual == '..' or contextual.startswith(('../', '/')):
                return ()
            matches = self.exact.get(contextual)
            if matches:
                return tuple(sorted(matches))
        if relative:
            return ()
        if self.metadata_errors:
            raise ValueError('Alias metadata is incomplete; use an exact qualified page path.')
        return tuple(sorted(self.candidates.get(slugify(name), ())))


def _within_root(path, root):
    return path.is_relative_to(root) and not any(
        part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)
    )


def audit(vault, overlays=None, wiki_root=None, source_paths=None, *, source_texts=None):
    overlays = overlays or {}
    root = Path(wiki_root).resolve() if wiki_root is not None else vault / 'knowledge/wiki'
    files = {p for p in root.glob('**/*.md') if p.is_file() and _within_root(p, root)} | {
        p for p in overlays if _within_root(p, root)
    }
    relative = {str(p.relative_to(root)): p for p in files}
    contents = {name: overlays[path] if path in overlays else path.read_bytes()
                for name, path in relative.items()}
    index = LinkIndex(contents)
    broken, ambiguous, edges = [], [], []
    unresolved = []
    sources = files if source_paths is None else {
        root / path for path in source_paths if root / path in files
    }
    for path in sorted(sources):
        source = str(path.relative_to(root))
        text = (source_texts or {}).get(path, contents[source])
        parsed = parse_text(text)
        if parsed['error']:
            raise ValueError('Cannot audit malformed Markdown metadata: ' + source)
        for link in parsed['links']:
            raw = link['target']
            reason = None
            try:
                candidates = () if link['invalid'] else index.resolve(raw, source)
            except ValueError as error:
                candidates, reason = (), str(error)
            if not candidates:
                broken.append((source, raw))
            elif len(candidates) > 1:
                ambiguous.append((source, raw))
            else:
                edges.append((source, candidates[0]))
            if len(candidates) != 1:
                concept = (posixpath.normpath(posixpath.join(posixpath.dirname(source), raw))
                           if link['relative'] else raw)
                unresolved.append({'source': source, 'line': link['line'], 'link': raw,
                                   'concept': 'unresolved:' + concept,
                                   'kind': 'unavailable' if reason else 'ambiguous' if candidates else 'missing',
                                   'reason': reason,
                                   'candidates': list(candidates)})
    backlinks = {}
    for source, target in edges:
        backlinks.setdefault(target, set()).add(source)
    return {'broken': sorted(set(broken)), 'ambiguous': sorted(set(ambiguous)),
            'edges': len(edges), 'pages_with_backlinks': len(backlinks),
            'unresolved': unresolved, 'metadata_errors': index.metadata_errors}


def audit_managed(vault, registry, wiki_root=None, overlays=None):
    facts = registry.get('facts', {})
    sources = {fact['target'] for fact in facts.values()}
    sources |= {fact['index_target'] for fact in facts.values() if 'index_target' in fact}
    root = Path(wiki_root).resolve() if wiki_root is not None else vault / 'knowledge/wiki'
    managed = dict(overlays or {})
    source_texts = {}
    for target in sources:
        path = root / target
        if path not in managed and not path.is_file():
            continue
        text = managed[path] if path in managed else path.read_text(encoding='utf-8')
        block = re.search(r'<!-- kb -->(.*?)<!-- /kb -->', text, re.DOTALL)
        source_texts[path] = ('\n' * text[:block.start(1)].count('\n') + block[1]) if block else ''
    return audit(vault, managed, wiki_root, sources, source_texts=source_texts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=Path.home() / '.vault')
    args = parser.parse_args()
    report = audit(args.vault.expanduser().resolve())
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(report['broken'] or report['ambiguous']))
