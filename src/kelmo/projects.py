"""Project selection for repository and worktree contexts."""
from pathlib import Path
import re
import subprocess
import copy
import json
import shlex
import tomllib
from contextlib import contextmanager

from .errors import KelmoError


def _remote(value):
    from .context_binding import canonical_remote
    try:
        if '://' not in value and '/' in value and '@' not in value:
            normalized = canonical_remote('https://' + value)
            if normalized == value:
                return value
        return canonical_remote(value)
    except ValueError:
        if re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value):
            return value
        raise KelmoError('E_CONFIG', 'A repository remote is not a supported canonical identity.')


def validate_hot(raw, root):
    """Validate HOT configuration, including optional repository bindings."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'pages', 'max_bytes', 'projects', 'views', 'views_max_bytes'}:
        raise KelmoError('E_CONFIG', 'HOT configuration has unsupported fields.')
    budget, views, view_budget = raw.get('max_bytes', 48000), raw.get('views', False), raw.get('views_max_bytes', 48000)
    if type(budget) is not int or not 1 <= budget <= 1048576:
        raise KelmoError('E_CONFIG', 'HOT max_bytes must be a positive integer at most 1048576.')
    if type(views) is not bool or type(view_budget) is not int or not 4096 <= view_budget <= 1048576:
        raise KelmoError('E_CONFIG', 'HOT views or views_max_bytes is invalid.')

    def page_list(values):
        if not isinstance(values, list):
            raise KelmoError('E_CONFIG', 'HOT pages must be a list.')
        for value in values:
            if (not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value)
                    or Path(value).is_absolute() or '..' in Path(value).parts or Path(value).suffix.lower() != '.md'):
                raise KelmoError('E_CONFIG', 'HOT pages must be wiki-relative Markdown paths.')
        return list(dict.fromkeys(Path(value).as_posix() for value in values))

    entries = raw.get('projects', {})
    if not isinstance(entries, dict):
        raise KelmoError('E_CONFIG', 'HOT projects must be a table.')
    result, owners = {}, {}
    for name, entry in entries.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)
                or not isinstance(entry, dict) or set(entry) - {'roots', 'pages', 'common_dirs', 'remotes', 'excluded_common_dirs'}):
            raise KelmoError('E_CONFIG', 'HOT project is invalid.')
        row = {'pages': page_list(entry.get('pages', []))}
        for field in ('roots', 'common_dirs', 'remotes', 'excluded_common_dirs'):
            values = entry.get(field, [])
            if not isinstance(values, list):
                raise KelmoError('E_CONFIG', 'HOT project ' + field + ' must be a list.')
            normalized = []
            for value in values:
                if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                    raise KelmoError('E_CONFIG', 'HOT project binding is invalid.')
                if field != 'remotes':
                    path = Path(value).expanduser()
                    value = str((path if path.is_absolute() else Path(root) / path).resolve())
                else:
                    value = _remote(value)
                key = (field, value)
                if field != 'excluded_common_dirs' and key in owners and owners[key] != name:
                    raise KelmoError('E_CONFIG', 'A HOT binding cannot belong to multiple projects.')
                owners[key] = name
                normalized.append(value)
            row[field] = list(dict.fromkeys(normalized))
        if set(row['common_dirs']) & set(row['excluded_common_dirs']):
            raise KelmoError('E_CONFIG', 'A common directory cannot be both linked and excluded from a project.')
        result[name] = row
    value = {'pages': page_list(raw.get('pages', [])), 'max_bytes': budget, 'projects': result}
    if views:
        value['views'] = {'max_bytes': view_budget}
    return value


def _git_identity(cwd):
    def git(*args):
        return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                              check=False)
    common = git('rev-parse', '--path-format=absolute', '--git-common-dir')
    if common.returncode:
        return None, []
    remotes = git('config', '--get-regexp', r'^remote\..*\.url$')
    urls = []
    for line in remotes.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            value = parts[1]
            if value.startswith(('./', '../')):
                value = str((Path(cwd) / value).resolve())
            try:
                normalized = _remote(value)
                if _remote(normalized) == normalized:
                    urls.append(normalized)
            except KelmoError:
                continue
    return str(Path(common.stdout.strip()).resolve()), urls


def resolve(config, cwd):
    """Select a project by common directory, remote, then most-specific configured root."""
    path = Path(cwd)
    if not path.is_absolute():
        raise KelmoError('E_INPUT', 'Project cwd must be an absolute path.')
    path = path.resolve()
    projects = (config.hot or {}).get('projects', {})
    common, remotes = _git_identity(path)
    eligible = {name: row for name, row in projects.items() if common not in row.get('excluded_common_dirs', [])}
    for field, values in (('common_dirs', [common] if common else []), ('remotes', remotes)):
        values = {_remote(value) for value in values} if field == 'remotes' else set(values)
        candidates = projects if field == 'common_dirs' else eligible
        matched = {name for name, entry in candidates.items()
                   if ({_remote(value) for value in entry.get(field, [])} if field == 'remotes'
                       else set(entry.get(field, []))) & values}
        if len(matched) > 1:
            raise KelmoError('E_CONFLICT', 'Project selection is ambiguous: ' + ', '.join(sorted(matched)))
        if matched:
            return next(iter(matched))
    matches = [(len(Path(root).parts), name) for name, entry in eligible.items()
               for root in entry.get('roots', []) if path.is_relative_to(Path(root))]
    if not matches:
        return None
    depth = max(item[0] for item in matches)
    owners = {name for size, name in matches if size == depth}
    if len(owners) > 1:
        raise KelmoError('E_CONFLICT', 'Project root selection is ambiguous: ' + ', '.join(sorted(owners)))
    return next(iter(owners))


def project_aliases(registry):
    """Project names retained by redirects to the same registered map identity."""
    from .wiki import pages
    maps = {entry['page_id']: pages.project_of(target) for target, entry in registry['pages'].items()
            if entry.get('layout') == 'project-map' and entry.get('page_id')}
    result = {}
    for target, identity in registry.get('redirects', {}).items():
        previous = pages.project_of(target)
        current = maps.get(identity)
        if previous and current and previous != current:
            if previous in result and result[previous] != current:
                raise KelmoError('E_CONFLICT', 'A project redirect names different map identities.')
            result[previous] = current
    return result


def canonical_project(registry, project):
    """Resolve a stored project name through stable map identities after a rename."""
    from .wiki import pages
    if not isinstance(project, str):
        return None
    if pages.project_entry(registry, project):
        return project
    maps = {entry['page_id']: pages.project_of(target) for target, entry in registry['pages'].items()
            if entry.get('layout') == 'project-map' and entry.get('page_id')}
    matches = {maps[identity] for target, identity in registry.get('redirects', {}).items()
               if pages.project_of(target) == project and identity in maps}
    return next(iter(matches)) if len(matches) == 1 else None


def resolve_bound_work(registry, project, bound_project, work):
    """Return the registered work path, following a project-rename redirect if present."""
    from .wiki import pages
    if (not isinstance(project, str) or not pages.PROJECT.fullmatch(project)
            or (bound_project is not None and (not isinstance(bound_project, str)
                                               or not pages.PROJECT.fullmatch(bound_project)))
            or not isinstance(work, str)
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*', work)):
        return None
    if bound_project not in (None, project):
        old = pages.project_directory(bound_project) + '/' + work + '.md'
        page_id = registry.get('redirects', {}).get(old)
        matches = [target for target, entry in registry['pages'].items()
                   if entry['page_id'] == page_id and pages.project_of(target) == project]
        return matches[0] if page_id and len(matches) == 1 else None
    target = pages.project_directory(project) + '/' + work + '.md'
    return target if target in registry['pages'] else None


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,59}', name) or name == 'mandatory':
        raise KelmoError('E_INPUT', 'Project names use lowercase letters, digits, underscores or hyphens; mandatory is reserved.')
    return name


def _raw(config):
    return copy.deepcopy(tomllib.loads((config.root / 'kelmo.toml').read_text()).get('hot', {}))


def _enroll_existing(hot, registry):
    projects = hot.setdefault('projects', {})
    from .wiki import hot_views, pages
    for target in hot_views.project_maps(registry):
        name = pages.project_of(target)
        projects.setdefault(name, {'roots': [], 'pages': []})
    hot['views'] = True
    return projects


@contextmanager
def _locked(root):
    from .config import Config, ClientProfile
    from .publication import coordinator_lock, recover_publications
    from .service import Service
    if ClientProfile.selected(root):
        raise KelmoError('E_CAPABILITY', 'Shared clients cannot change projects; ask the server operator to enroll the repository.')
    config = Config.load(root)
    with coordinator_lock(config):
        service = Service(config.root)
        try:
            recover_publications(service)
            yield service
        finally:
            service.close()


def _save(config, hot, expected):
    from .config import validate_hot as active_validator
    from .config_edit import replace_table
    validate_hot(hot, config.root)
    try:
        active = active_validator(hot, config.root)
    except KelmoError as error:
        raise KelmoError('E_CAPABILITY', 'The installed HOT validator cannot read project bindings; '
                         'configuration was not changed.') from error
    if any(active['projects'][name].get(field, []) != values
           for name, row in validate_hot(hot, config.root)['projects'].items()
           for field, values in row.items()):
        raise KelmoError('E_CAPABILITY', 'The installed HOT validator would discard project bindings; '
                         'configuration was not changed.')
    return replace_table(config.root, 'hot', hot, expected=expected)


def _resume_add(service):
    from .wiki import engine, pages
    marker = service.config.internal('.state/project-add.json')
    if not marker.exists():
        return None
    try:
        intent = json.loads(marker.read_text(encoding='utf-8'))
        if set(intent) != {'project', 'before', 'after', 'target', 'page_id'}:
            raise ValueError
        project = _name(intent['project'])
        before, after = intent['before'], intent['after']
        validate_hot(before, service.root)
        validate_hot(after, service.root)
        if project not in after.get('projects', {}):
            raise ValueError
        if not isinstance(intent['target'], str) or not isinstance(intent['page_id'], str):
            raise ValueError
        before_other = {key: value for key, value in before.items() if key not in ('projects', 'views')}
        after_other = {key: value for key, value in after.items() if key not in ('projects', 'views')}
        if before_other != after_other or after.get('views') is not True:
            raise ValueError
        for name, row in before.get('projects', {}).items():
            if name != project and after['projects'].get(name) != row:
                raise ValueError
    except (ValueError, TypeError, KeyError, KelmoError) as error:
        raise KelmoError('E_CONFLICT', 'Project recovery marker is invalid; it was preserved.') from error
    registry = engine.load_registry(service.root)
    projected = engine.apply_project_map(registry, project)
    target = pages.project_entry(projected, project)
    if target != intent['target'] or projected['pages'][target]['page_id'] != intent['page_id']:
        raise KelmoError('E_CONFLICT', 'Project identity changed during recovery; intent was preserved.')
    current = _raw(service.config)
    if current == before:
        saved = _save(service.config, after, before)
    elif current == after:
        saved = {'changed': False, 'applied': False, 'backup': None}
    else:
        raise KelmoError('E_CONFLICT', 'HOT configuration changed during project recovery; it was preserved.')
    published = engine.publish_project_map(service.root, project)
    marker.unlink()
    engine.sync_directory(marker.parent)
    return saved, published


def _prepare_add(service, project, before, after):
    from .wiki import engine, pages
    # Check that the currently installed validator accepts the future config before leaving an intent.
    from .config import validate_hot as active_validator
    try:
        active_validator(after, service.root)
    except KelmoError as error:
        raise KelmoError('E_CAPABILITY', 'The installed HOT validator cannot read project bindings; '
                         'configuration was not changed.') from error
    marker = service.config.internal('.state/project-add.json')
    projected = engine.apply_project_map(engine.load_registry(service.root), project)
    engine.validate_project_outputs(service.config, projected, after.get('projects', {}))
    target = pages.project_entry(projected, project)
    engine.atomic_write(marker, json.dumps({'project': project, 'before': before, 'after': after,
                                            'target': target, 'page_id': projected['pages'][target]['page_id']},
                                           sort_keys=True) + '\n')
    return _resume_add(service)


def add(root, name):
    from .wiki import engine
    name = _name(name)
    with _locked(root) as service:
        registry = engine.load_registry(service.root)
        # Check the projection and the file before config is changed.
        projected = engine.apply_project_map(registry, name)
        engine._refuse_claimed_files(service.config.wiki, registry, projected)
        hot = _raw(service.config)
        before = copy.deepcopy(hot)
        entries = _enroll_existing(hot, registry)
        entries.setdefault(name, {'roots': [], 'pages': []})
        saved, published = _prepare_add(service, name, before, hot)
        return {'project': name, 'changed': saved['changed'] or published,
                'config': saved, 'published': published}


def _rename_config(before, old, new, moves):
    after = copy.deepcopy(before)
    projects = after.setdefault('projects', {})
    if new in projects:
        raise KelmoError('E_CONFLICT', 'Project rename configuration is no longer available.')
    projects[new] = projects.pop(old, {'roots': [], 'pages': []})
    after['views'] = True
    if 'pages' in after:
        after['pages'] = [moves.get(path, path) for path in after['pages']]
    for row in projects.values():
        if 'pages' in row:
            row['pages'] = [moves.get(path, path) for path in row['pages']]
    return after


def _resume_rename(service):
    from .wiki import engine, pages
    marker = service.config.internal('.state/project-rename.json')
    if not marker.exists():
        return
    try:
        intent = json.loads(marker.read_text(encoding='utf-8'))
        if set(intent) != {'old', 'new', 'before', 'after', 'page_id', 'old_target', 'moves'}:
            raise ValueError
        old, new = _name(intent['old']), _name(intent['new'])
        moves = intent['moves']
        if (not isinstance(moves, dict) or not moves or pages.project_of(intent['old_target']) != old
                or any(not isinstance(source, str) or not isinstance(dest, str)
                       or pages.project_of(source) != old or pages.project_of(dest) != new
                       for source, dest in moves.items())):
            raise ValueError
        if _rename_config(intent['before'], old, new, moves) != intent['after']:
            raise ValueError
    except (ValueError, TypeError, KeyError, KelmoError) as error:
        raise KelmoError('E_CONFLICT', 'Project rename recovery marker is invalid; it was preserved.') from error
    registry = engine.load_registry(service.root)
    old_target = pages.project_entry(registry, old)
    new_target = pages.project_entry(registry, new)
    target = old_target or new_target
    if not target or registry['pages'][target]['page_id'] != intent['page_id'] or (old_target and old_target != intent['old_target']):
        raise KelmoError('E_CONFLICT', 'Project identity changed during rename recovery; intent was preserved.')
    if old_target and engine.apply_project_rename(registry, old, new)[1] != moves:
        raise KelmoError('E_CONFLICT', 'Project pages changed during rename recovery; intent was preserved.')
    current = _raw(service.config)
    if current == intent['before']:
        _save(service.config, intent['after'], current)
    elif current != intent['after']:
        raise KelmoError('E_CONFLICT', 'HOT configuration changed during project rename; recovery stopped.')
    if old_target:
        engine.publish_project_rename(service.root, old, new)
    elif registry['redirects'].get(intent['old_target']) != intent['page_id']:
        raise KelmoError('E_CONFLICT', 'Project redirect is missing during rename recovery.')
    marker.unlink()
    engine.sync_directory(marker.parent)


def rename(root, old, new):
    from .wiki import engine, pages
    old, new = _name(old), _name(new)
    with _locked(root) as service:
        registry = engine.load_registry(service.root)
        new_target = pages.project_entry(registry, new)
        if (not pages.project_entry(registry, old) and new_target
                and any(pages.project_of(target) == old and identity == registry['pages'][new_target]['page_id']
                        for target, identity in registry['redirects'].items())):
            return {'old': old, 'project': new, 'changed': False}
        projected, moves = engine.apply_project_rename(registry, old, new)
        engine._refuse_claimed_files(service.config.wiki, registry, projected)
        engine.require_clean_baseline(service.root, projected, previous=registry)
        before = _raw(service.config)
        after = _rename_config(before, old, new, moves)
        validate_hot(after, service.root)
        engine.validate_project_outputs(service.config, projected, after.get('projects', {}))
        marker = service.config.internal('.state/project-rename.json')
        target = pages.project_entry(registry, old)
        engine.atomic_write(marker, json.dumps({'old': old, 'new': new, 'before': before, 'after': after,
                                               'page_id': registry['pages'][target]['page_id'], 'old_target': target,
                                               'moves': moves},
                                               sort_keys=True) + '\n')
        _resume_rename(service)
        return {'old': old, 'project': new, 'changed': True}


def link(root, project, directory):
    from .config import ClientProfile
    from .wiki import engine
    project = _name(project)
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise KelmoError('E_INPUT', 'Repository directory does not exist.')
    common, remotes = _git_identity(directory)
    if common is None:
        raise KelmoError('E_INPUT', 'Directory is not in a Git repository.')
    if ClientProfile.selected(root):
        if not remotes:
            raise KelmoError('E_INPUT', 'This repository has no remote; the server operator needs one for enrollment.')
        commands = []
        for remote in remotes:
            url = remote if remote.startswith('file://') else 'https://' + remote
            commands.append('kelmo server repository --profile <SERVER_PROFILE> --repository-id <REPOSITORY_ID> '
                            '--root <SERVER_REPOSITORY_ROOT> --remote ' + shlex.quote(url)
                            + ' --project ' + shlex.quote(project) + ' --apply')
        return {'project': project, 'directory': str(directory), 'changed': False,
                'server_commands': commands, 'message': 'Ask the server operator to enroll the repository.'}
    with _locked(root) as service:
        registry = engine.load_registry(service.root)
        hot = _raw(service.config)
        before = copy.deepcopy(hot)
        entries = _enroll_existing(hot, registry)
        entries.setdefault(project, {'roots': [], 'pages': []})
        projected = engine.apply_project_map(registry, project)
        engine._refuse_claimed_files(service.config.wiki, registry, projected)
        row = entries[project]
        normalized = validate_hot(hot, service.root)['projects'][project]
        row['excluded_common_dirs'] = [value for value in normalized['excluded_common_dirs'] if value != common]
        for field, additions in (('roots', [str(directory)]), ('common_dirs', [common]), ('remotes', remotes)):
            row[field] = list(dict.fromkeys(row.get(field, []) + additions))
        saved, published = _prepare_add(service, project, before, hot)
        return {'project': project, 'directory': str(directory), 'common_dir': common,
                'changed': saved['changed'] or published, 'config': saved, 'published': published}


def unlink(root, directory, project=None):
    directory = Path(directory).expanduser().resolve()
    common, remotes = _git_identity(directory)
    if common is None:
        raise KelmoError('E_INPUT', 'Directory is not in a Git repository.')
    with _locked(root) as service:
        hot = _raw(service.config)
        before = copy.deepcopy(hot)
        entries = hot.get('projects', {})
        normalized = validate_hot(hot, service.config.root)['projects']
        owners = [name for name, row in normalized.items() if common in row.get('common_dirs', [])]
        if not owners:
            selected = resolve(service.config, directory)
            owners = [selected] if selected is not None else []
        if project is not None:
            _name(project)
            owners = [name for name in owners if name == project]
        if len(owners) > 1:
            raise KelmoError('E_CONFLICT', 'Repository is linked to multiple projects.')
        if not owners:
            return {'changed': False, 'directory': str(directory), 'project': project}
        name = owners[0]
        row = entries[name]
        binding = normalized[name]
        row['common_dirs'] = [value for value in binding['common_dirs'] if value != common]
        row['excluded_common_dirs'] = list(dict.fromkeys(binding['excluded_common_dirs'] + [common]))
        roots = []
        for value in binding['roots']:
            linked_common, _ = _git_identity(Path(value))
            if linked_common != common:
                roots.append(value)
        row['roots'] = roots
        saved = _save(service.config, hot, before)
        return {'changed': saved['changed'], 'directory': str(directory), 'project': name, 'config': saved}


def list_projects(root):
    from .config import Config
    from .wiki import engine, hot_views, pages
    with _locked(root) as service:
        config = Config.load(service.root)
        registry = engine.load_registry(config.root)
        configured = (config.hot or {}).get('projects', {})
        names = set(configured) | {pages.project_of(path) for path in hot_views.project_maps(registry)}
        rows = []
        for name in sorted(names):
            entries = configured.get(name, {})
            work = hot_views._open_pages(registry, name)
            rows.append({'name': name, 'roots': entries.get('roots', []),
                         'common_dirs': entries.get('common_dirs', []), 'remotes': entries.get('remotes', []),
                         'excluded_common_dirs': entries.get('excluded_common_dirs', []),
                         'wip': sum(item[0] == 'in-progress' for item in work),
                         'backlog': sum(item[0] == 'backlog' for item in work)})
        return {'projects': rows}
