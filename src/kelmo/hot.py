"""Deterministic, bounded selection of complete wiki pages."""
import hashlib
import json
import os
from pathlib import Path
import stat

if __package__:
    from .errors import KelmoError
else:
    from errors import KelmoError


def validate(raw, root):
    from .projects import validate_hot
    return validate_hot(raw, root)


def _page(wiki, name, limit):
    path = wiki / name
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise KelmoError('E_HOT', f'HOT page uses a symlink: {name}')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise KelmoError('E_HOT', f'HOT page must be a regular file: {name}')
        with os.fdopen(descriptor, 'rb') as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise KelmoError('E_HOT', 'HOT budget exceeded; reduce selected pages or raise max_bytes.')
        return raw.decode('utf-8'), hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError) as error:
        raise KelmoError('E_HOT', f'HOT page is missing, unreadable or not UTF-8: {name}') from error


def adopted(config, reader=None):
    """A committed registry cannot be downgraded by unpublished registry changes. `reader` is the caller's
    reader of the same knowledge repository, whose answer for a commit it has already read is reused."""
    from .wiki.pages import KnowledgeReader, committed_paths
    git_root = Path(getattr(config, 'knowledge_git_root', config.root))
    if (git_root / '.git').exists():
        if reader is None or (reader.root, reader.registry_path, reader.manifest_path) != committed_paths(config):
            reader = KnowledgeReader(config)
        if reader.adopted():
            return True
    registry = config.checked_path(config.registry)
    try:
        raw = registry.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise KelmoError('E_GENERATION', 'The bound registry is unreadable; publication state is unknown.') from error
    try:
        return json.loads(raw).get('version') == 3
    except (ValueError, AttributeError):
        # A damaged working registry beside a manifest is refused by the generation reader.
        return registry.with_name('generation.json').is_file()


def _published(reader, generation, name):
    try:
        page = reader.page(name, generation)
    except KelmoError as error:
        if error.code not in ('E_NOT_FOUND', 'E_AMBIGUOUS'):
            raise
        page = None
    if page is None or page.path != name:
        raise KelmoError('E_HOT', f'HOT page is not in the published knowledge generation: {name}')
    try:
        return page.content.decode('utf-8')
    except UnicodeError as error:
        raise KelmoError('E_GENERATION', f'A published HOT page is not UTF-8: {name}') from error


def _render_generation(config, project, paths, budget, reader, *, generation=None):
    """Mandatory pages and selected maps from one committed generation, or a refusal."""
    from .wiki.pages import KnowledgeReader, generation_paths
    if reader is None:
        reader = KnowledgeReader(config)
    elif (reader.config.vault_id != config.vault_id
          or (reader.root, reader.registry_path, reader.wiki_path) != generation_paths(config)[:3]):
        raise KelmoError('E_BINDING', 'The supplied knowledge reader is bound to another repository.')
    generation = generation or reader.generation()
    # The canonical mandatory set (pages.mandatory_paths): person pages, whose facts need no flag,
    # and every page holding a mandatory or protected record. Each is delivered whole, once.
    mandatory = [page.path for page in reader.mandatory(generation)]
    selected = [name for name in dict.fromkeys(paths) if name not in mandatory]
    blocks, pages = [], []
    for name in mandatory + selected:
        text = _published(reader, generation, name)
        blocks.append(f'## {name}\n\n{text}')
        pages.append({'path': name, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                      'bytes': len(text.encode('utf-8'))})
    freshness = 'current' if reader.generation() == generation else 'stale'
    complete = freshness == 'current'
    header = (f'Kelmo knowledge generation {generation.publication_id} '
              f'(manifest {generation.manifest_sha256[:12]}): {freshness}, '
              + ('complete' if complete else 'incomplete, a newer generation was published during this read')
              + '; mandatory pages: ' + (', '.join(mandatory) or 'none') + '.')

    def size(parts):
        return len('\n\n'.join(parts).encode('utf-8'))

    required = size([header, *blocks[:len(mandatory)]])
    if required > budget:
        raise KelmoError('E_BUDGET', f'Mandatory content needs {required} bytes but HOT max_bytes is '
                                    f'{budget}; delivery would be incomplete, so nothing was delivered. '
                                    'Raise max_bytes.')
    context = '\n\n'.join([header, *blocks])
    if len(context.encode('utf-8')) > budget:
        raise KelmoError('E_HOT', 'HOT budget exceeded; reduce selected pages or raise max_bytes.')
    identity = json.dumps({'project': project, 'pages': pages, 'generation': generation.to_dict()},
                          sort_keys=True).encode()
    return {'configured': config.hot is not None, 'project': project, 'pages': pages,
            'bytes': len(context.encode('utf-8')), 'max_bytes': budget, 'context': context,
            'fingerprint': hashlib.sha256(identity).hexdigest(),
            'generation': generation.to_dict(), 'freshness': freshness, 'complete': complete,
            'mandatory': mandatory}


def render(config, cwd=None, *, project=None, reader=None):
    if cwd is not None:
        if not isinstance(cwd, (str, Path)) or not str(cwd) or not Path(cwd).is_absolute():
            raise KelmoError('E_INPUT', 'HOT cwd must be an absolute path.')
        cwd = Path(cwd).resolve()
    hot = config.hot
    if project is None and hot and cwd is not None:
        from .projects import resolve
        project = resolve(config, cwd)
    paths = list(hot['pages']) if hot else []
    if project is not None:
        if not hot or project not in hot['projects']:
            raise KelmoError('E_INPUT', 'Unknown HOT project.')
        paths += hot['projects'][project]['pages']
    budget = hot['max_bytes'] if hot else 48000
    # Unconfigured HOT stays an explicit empty selection, with or without a committed registry.
    if hot is not None and adopted(config, reader):
        return _render_generation(config, project, paths, budget, reader)
    blocks, pages = [], []
    size = 0
    for name in dict.fromkeys(paths):
        text, checksum = _page(config.wiki, name, budget - size)
        block = f'## {name}\n\n{text}'
        size += len(block.encode('utf-8')) + (2 if blocks else 0)
        if size > budget:
            raise KelmoError('E_HOT', 'HOT budget exceeded; reduce selected pages or raise max_bytes.')
        blocks.append(block)
        pages.append({'path': name, 'sha256': checksum, 'bytes': len(text.encode('utf-8'))})
    context = '\n\n'.join(blocks)
    identity = json.dumps({'project': project, 'pages': pages}, sort_keys=True).encode()
    return {'configured': hot is not None, 'project': project, 'pages': pages,
            'bytes': size, 'max_bytes': budget, 'context': context,
            'fingerprint': hashlib.sha256(identity).hexdigest()}


def has_hub_generation(config, reader=None, generation=None):
    from .wiki.pages import KnowledgeReader
    reader = reader or KnowledgeReader(config)
    generation = generation or reader.generation()
    name = str(Path(reader.wiki_path).parent / 'hot/mandatory.md')
    return name in reader._pinned(generation)['entries']


def hub_context(config, cwd, *, reader=None):
    """Read generated local views and bound work from one committed generation."""
    from .client import local_binding
    from .hub_delivery import published_set
    from .projects import resolve, project_aliases
    from .wiki.pages import KnowledgeReader, generation_paths
    reader = reader or KnowledgeReader(config)
    if (reader.config.vault_id != config.vault_id
            or (reader.root, reader.registry_path, reader.wiki_path) != generation_paths(config)[:3]):
        raise KelmoError('E_BINDING', 'The supplied knowledge reader is bound to another repository.')
    generation = reader.generation()
    project = resolve(config, cwd) if cwd else None
    snapshot = published_set(reader, generation, list(config.hot['projects']), 0)
    binding = local_binding(cwd)
    work, warning, work_path = '', '', None
    if binding.get('binding_status') in ('pending', 'invalid-binding', 'stale-branch'):
        warning = 'Work binding is ' + binding['binding_status'] + '; no work page was delivered.'
    if binding['work']:
        from .projects import resolve_bound_work
        name = resolve_bound_work(reader._pinned(generation)['registry'], project,
                                  binding['work_project'], binding['work'])
        if name is not None and name in reader._pinned(generation)['paths']:
            work = reader.page(name, generation).content.decode('utf-8')
            work_path = name
        else:
            warning = 'Bound work page is absent from the selected project generation.'
    return {'hub_set': snapshot, 'project': project, '_registry': reader._pinned(generation)['registry'],
            'project_aliases': project_aliases(reader._pinned(generation)['registry']), 'hub_work': work, 'hub_work_path': work_path, 'warning': warning,
            'freshness': 'current' if reader.generation() == generation else 'stale'}


HOOK_CONTEXT_LIMIT = 10000


def startup_context(context):
    """What a startup hook may hand the agent: the whole context while it fits the hook limit, else its
    first line and a pointer.

    Claude Code caps each hook context string at 10,000 characters and swaps a longer one for a file
    path plus a 2,000-character preview the agent is never asked to read, so a cut page would arrive
    silently incomplete. Content of that size travels in the instruction files, which carry only what they
    import, and graph_read returns any page on demand.
    """
    if len(context) <= HOOK_CONTEXT_LIMIT:
        return context
    header = context.split('\n\n', 1)[0]
    if len(header) > HOOK_CONTEXT_LIMIT // 2:
        header = header[:HOOK_CONTEXT_LIMIT // 2] + '…'
    return (header + f'\n\nThe selected pages ({len(context.encode("utf-8"))} bytes) exceed the '
            f'{HOOK_CONTEXT_LIMIT}-character startup-hook limit, so they are not repeated here. The instruction '
            'files carry only the pages they import (the generated project views under knowledge/hot among '
            'them); read any page named above that they do not import with graph_read.')


def views_over_budget(config):
    """{view name: bytes over} for every generated project view that the renderer marked over budget.

    A publication writes such a view instead of failing, so the marker is the only trace; the doctor and
    `kelmo hot --check-all` turn it into a warning.
    """
    from .wiki import hot_views
    if not config.hot or not config.hot.get('views'):
        return {}
    hot_root = config.wiki.parent / 'hot'
    result = {}
    for path in sorted(hot_root.glob('*.md')) if hot_root.is_dir() else ():
        try:
            over = hot_views.over_budget(path.read_bytes().decode('utf-8'))
        except (OSError, UnicodeError):
            continue
        if over is not None:
            result[path.name] = over
    return result


def pending_block(items):
    """The startup lines for notes still waiting for the editor, or '' when none carry a pending line.

    Each note is one line, so the block stays well inside the hook limit and changes only when the
    waiting set changes; it goes after the startup context, never inside it.
    """
    if not items:
        return ''
    lines = ['Pending wiki updates (archived, applied by the next Dreaming pass; not yet on any page):']
    for item in items:
        state = ' (retrying)' if item.get('state') == 'parked' else ''
        lines.append(f'- {item["pending"]} [{item.get("project") or "?"}{state}]')
    return '\n'.join(lines)


def check_all(config):
    # One reader for every selection: it loads each committed generation once instead of once per project.
    reader = None
    if config.hot is not None and (Path(getattr(config, 'knowledge_git_root', config.root)) / '.git').exists():
        from .wiki.pages import KnowledgeReader
        reader = KnowledgeReader(config)
    selections = []
    for project in [None, *sorted(config.hot['projects'] if config.hot else {})]:
        result = render(config, project=project, reader=reader)
        result.pop('context')
        selections.append(result)
    return {'configured': config.hot is not None, 'selections': selections,
            'views_over_budget': views_over_budget(config)}
