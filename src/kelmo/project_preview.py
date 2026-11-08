"""Read-only write inventory for project setup, including separate knowledge roots."""
from pathlib import Path

from .config import ClientProfile, Config
from .errors import KelmoError
from .projects import _git_identity, _name
from .wiki import engine, hot_views, pages


def preview(root, name, directory=None, *, wiki_root=None):
    root = Path(root).expanduser().resolve()
    name = _name(name)
    if ClientProfile.selected(root):
        raise KelmoError('E_CAPABILITY', 'Shared project enrollment is a server operator action.')
    if directory is not None:
        directory = Path(directory).expanduser().resolve()
        if not directory.is_dir() or _git_identity(directory)[0] is None:
            raise KelmoError('E_INPUT', 'Link requires an existing Git repository directory.')
    targets = {name: f'projects/{name}/{name}.md'}
    if (root / 'kelmo.toml').exists():
        config = Config.load(root)
        wiki, knowledge, state, archive = config.wiki, config.knowledge_git_root, config.state, config.archive
        registry = engine.load_registry(root)
        targets.update({pages.project_of(target): target for target in hot_views.project_maps(registry)})
        projected = engine.apply_project_map(registry, name)
        engine._refuse_claimed_files(wiki, registry, projected)
    else:
        knowledge = Path(wiki_root).expanduser().resolve() if wiki_root is not None else root
        wiki, state, archive = knowledge / 'knowledge/wiki', root / '.state', root / 'archive/archive.db'
    paths = [
        (root / 'kelmo.toml', 'create_or_update'),
        (root / 'kelmo.toml.bak.*', 'backup_if_changed'),
        (state / '**', 'locks_journal_and_recoverable_publication_state'),
        (archive, 'open_existing_or_initialize_archive'),
        (Path(str(archive) + '-*'), 'sqlite_sidecars'),
        (knowledge / '.git/**', 'knowledge_publication'),
        (wiki.parent / 'provenance/**', 'registry_and_generation'),
        (wiki / targets[name], 'create_hub_if_missing'),
        (wiki.parent / 'hot/mandatory.md', 'publish_generated_view'),
        (wiki.parent / 'hot/mandatory-*.md', 'preserve_legacy_import_views'),
    ]
    for project in sorted(targets):
        paths.extend((wiki.parent / 'hot' / (project + suffix), 'publish_generated_view')
                     for suffix in ('.md', '.full.md'))
    return {'project': name, 'directory': str(directory) if directory is not None else None,
            'files': [{'path': str(path), 'action': action} for path, action in paths],
            'dry_run': True, 'writes_performed': False}
