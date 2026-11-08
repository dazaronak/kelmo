"""Project hub output contracts, using only synthetic wiki data."""
import pytest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from kelmo.errors import KelmoError

from kelmo.wiki import hot_views


def test_generated_set_combines_all_mandatory_pages_and_stamps_generation(tmp_path):
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    registry = {'pages': {
        'projects/app/app.md': {'kind': 'hub', 'layout': 'project-map', 'title': 'App'},
        'person.md': {'kind': 'hub', 'layout': 'person', 'title': 'Person'},
    }, 'facts': {}}
    rendered = {wiki / 'person.md': '# Person\n\nAlways remember.\n',
                wiki / 'projects/app/app.md': '# App\n\nOverview.\n'}
    result = hot_views.render_all(registry, rendered, wiki, tmp_path / 'hot', generation=7)
    mandatory = result[tmp_path / 'hot/mandatory.md']
    hub = result[tmp_path / 'hot/app.md']
    full = result[tmp_path / 'hot/app.full.md']
    assert mandatory.startswith('<!-- kelmo generation 7 -->\n')
    assert 'Always remember.' in mandatory
    assert hub.startswith('<!-- kelmo generation 7 -->\n') and 'Overview.' in hub
    assert 'Always remember.' in full and 'Overview.' in full
    assert full.count('<!-- kelmo generation 7 -->') == 1


def test_generated_set_rejects_colliding_project_names(tmp_path):
    registry = {'pages': {
        'projects/app/app.md': {'kind': 'hub', 'layout': 'project-map', 'title': 'App'},
        'projects/app/another.md': {'kind': 'hub', 'layout': 'project-map', 'title': 'Another'},
    }, 'facts': {}}
    with pytest.raises(ValueError, match='collision'):
        hot_views.render_all(registry, {}, tmp_path / 'wiki', tmp_path / 'hot', generation=1)


def test_resolver_detects_ambiguous_remotes(tmp_path, monkeypatch):
    from kelmo import projects
    config = SimpleNamespace(hot={'projects': {
        'one': {'roots': [], 'remotes': ['remote-one']},
        'two': {'roots': [], 'remotes': ['remote-two']},
    }})
    monkeypatch.setattr(projects, '_git_identity', lambda cwd: ('/common', ['remote-one', 'remote-two']))
    with pytest.raises(KelmoError, match='ambiguous'):
        projects.resolve(config, tmp_path)


def test_resolver_common_directory_precedes_remote_and_roots(tmp_path, monkeypatch):
    from kelmo import projects
    config = SimpleNamespace(hot={'projects': {
        'one': {'roots': [], 'common_dirs': ['/common']},
        'two': {'roots': [str(tmp_path)], 'remotes': ['remote-two']},
    }})
    monkeypatch.setattr(projects, '_git_identity', lambda cwd: ('/common', ['remote-two']))
    assert projects.resolve(config, tmp_path) == 'one'


def test_resolver_reads_all_git_remote_urls(tmp_path):
    from kelmo import projects
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'remote', 'add', 'first', 'https://example.invalid/one'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'remote', 'add', 'second', 'https://example.invalid/two'], check=True)
    config = SimpleNamespace(hot={'projects': {
        'app': {'roots': [], 'remotes': ['https://example.invalid/two']},
    }})
    subprocess.run(['git', '-C', str(tmp_path), 'remote', 'add', 'helper', 'ext::unsupported-helper'], check=True)
    assert projects.resolve(config, tmp_path) == 'app'


def test_link_selects_sibling_worktree(tmp_path):
    from kelmo import config, projects
    from kelmo.initialization import initialize
    root, repo, sibling = tmp_path / 'vault', tmp_path / 'repo', tmp_path / 'sibling'
    initialize(root, 'worktree-hub', project='alpha', git_name='Test', git_email='test@example.invalid')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'initial'], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'other', str(sibling)], check=True)
    projects.link(root, 'alpha', repo)
    loaded = config.Config.load(root)
    assert projects.resolve(loaded, sibling) == 'alpha'
    from kelmo.hot import render
    assert render(loaded, sibling)['project'] == 'alpha'


def test_create_rejects_unknown_project_even_with_selected_page(monkeypatch):
    from kelmo import creation
    registry = {'pages': {'projects/app/app.md': {'layout': 'project-map', 'category': 'projects'}}, 'facts': {}}
    monkeypatch.setattr(creation.engine, 'load_registry', lambda root: registry)
    service = SimpleNamespace(root='/private/tmp/synthetic-kelmo')
    with pytest.raises(KelmoError, match='project add'):
        creation.request(service, 'unknown', ['projects/app/app.md'], False, 1000)


def test_validate_hot_accepts_unlinked_project_and_refuses_duplicate_binding(tmp_path):
    from kelmo.projects import validate_hot
    normalized = validate_hot({'projects': {'app': {'roots': [], 'common_dirs': [], 'remotes': [], 'pages': []}}}, tmp_path)
    assert normalized['projects']['app']['roots'] == []
    with pytest.raises(KelmoError, match='multiple projects'):
        validate_hot({'projects': {'a': {'common_dirs': ['/private/tmp/repo']},
                                   'b': {'common_dirs': ['/private/tmp/repo']}}}, tmp_path)


def test_project_map_transform_refuses_reused_id_and_preserves_registry():
    from kelmo.wiki import engine
    base = engine.initial_registry('synthetic', 'alpha')
    result = engine.apply_project_map(base, 'beta')
    assert result['pages']['projects/alpha/alpha.md'] == base['pages']['projects/alpha/alpha.md']
    assert result['pages']['projects/beta/beta.md']['page_id'] == 'beta'
    assert engine.apply_project_map(result, 'beta') == result
    with pytest.raises(Exception, match='identity'):
        engine.apply_project_map(base, 'alpha', target='projects/other/other.md')


def test_add_and_link_publish_pinned_hubs_and_preserve_existing(tmp_path, monkeypatch):
    from kelmo import config, projects
    from kelmo.initialization import initialize
    from kelmo.wiki.pages import KnowledgeReader
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    root = tmp_path / 'vault'
    initialize(root, 'hub-test', project='alpha', git_name='Test', git_email='test@example.invalid')
    settings = root / 'kelmo.toml'
    settings.write_text(settings.read_text() + '\n[hot.projects.legacy]\nroots=[]\npages=["projects/alpha/alpha.md"]\n')
    projects.add(root, 'beta')
    settings.write_text(settings.read_text() + '\n[hot.projects.late]\nroots=[]\npages=[]\n')
    projects.add(root, 'beta')
    assert (root / 'knowledge/hot/late.full.md').is_file()
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    projects.link(root, 'beta', repo)
    loaded = config.Config.load(root)
    assert projects.resolve(loaded, repo) == 'beta'
    reader = KnowledgeReader(loaded)
    generation = reader.generation()
    assert reader.page('projects/alpha/alpha.md', generation)
    assert reader.page('projects/beta/beta.md', generation)
    assert (root / 'knowledge/hot/beta.md').read_text().startswith(
        f'<!-- kelmo generation {generation.publication_id} -->\n')
    projects.rename(root, 'beta', 'gamma')
    loaded = config.Config.load(root)
    assert projects.resolve(loaded, repo) == 'gamma'
    reader = KnowledgeReader(loaded)
    assert reader.page('projects/gamma/gamma.md', reader.generation())
    projects.unlink(root, repo)
    assert projects.resolve(config.Config.load(root), repo) is None
    from kelmo.wiki import engine
    original = engine.publish_project_map
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, 'publish_project_map', lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted relink')))
        with pytest.raises(RuntimeError, match='interrupted relink'):
            projects.link(root, 'gamma', repo)
    assert (root / '.state/project-add.json').exists()
    assert engine.publish_project_map is original
    projects.link(root, 'gamma', repo)
    loaded = config.Config.load(root)
    assert projects.resolve(loaded, repo) == 'gamma'
    assert not loaded.hot['projects']['gamma']['excluded_common_dirs']
    assert not (root / '.state/project-add.json').exists()


def test_rename_recovers_after_config_write(tmp_path, monkeypatch):
    from kelmo import config, projects
    from kelmo.initialization import initialize
    from kelmo.wiki import engine
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    root = tmp_path / 'vault'
    initialize(root, 'hub-recovery', project='alpha', git_name='Test', git_email='test@example.invalid')
    projects.add(root, 'beta')
    original = engine.publish_project_rename
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, 'publish_project_rename', lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted')))
        with pytest.raises(RuntimeError, match='interrupted'):
            projects.rename(root, 'beta', 'gamma')
    assert (root / '.state/project-rename.json').is_file()
    assert engine.publish_project_rename is original
    from kelmo.service import Service
    from kelmo.publication import coordinator_lock, recover_publications
    with Service(root) as service:
        with coordinator_lock(service.config):
            recover_publications(service)
    assert not (root / '.state/project-rename.json').exists()
    projects.rename(root, 'beta', 'gamma')
    assert not (root / '.state/project-rename.json').exists()
    assert 'gamma' in [row['name'] for row in projects.list_projects(root)['projects']]


def test_rename_keeps_open_work_facts_and_redirects():
    import registry_seed
    from kelmo.wiki import engine, hot_views
    map_path = 'projects/alpha/alpha.md'
    task_path = 'projects/alpha/task.md'
    note = 'a' * 64
    spec = {'notes': {note: {'filename': 'source.md', 'ark_id': 'archive-1'}},
            'facts': {'work': registry_seed.fact('work', task_path, 'Working.', note, 'archive-1',
                                                 status='in-progress')}}
    settings = {map_path: {'page_id': 'alpha', 'layout': 'project-map', 'title': 'Alpha'},
                task_path: {'page_id': 'task', 'layout': 'feature', 'title': 'Task', 'parent': map_path,
                            'priority': 'H1', 'summary': 'Work in progress.'}}
    registry = registry_seed.build(spec, authority_id='synthetic', pages_settings=settings)
    narrative = {'narrative_id': 'work-explanation', 'page_id': 'task',
                 'text': 'The current task remains open.', 'evidence_refs': [],
                 'depends_on': {'facts': {'work': registry['facts']['work']['revision']}, 'actions': {}},
                 'state': 'current', 'previous_revision': None, 'compatibility': {}}
    narrative['revision'] = engine.entity_revision(narrative)
    registry['narratives']['work-explanation'] = narrative
    engine.validate_registry(registry)
    renamed, _ = engine.apply_project_rename(registry, 'alpha', 'gamma')
    retained = renamed['narratives']['work-explanation']
    assert retained['text'] == narrative['text'] and retained['page_id'] == 'task'
    assert retained['state'] == 'stale' and retained['previous_revision'] == narrative['revision']
    assert narrative['state'] == 'current'

    new_map, new_task = 'projects/gamma/gamma.md', 'projects/gamma/task.md'
    assert renamed['redirects'][task_path] == 'task'
    assert renamed['facts']['work']['target'] == new_task
    assert renamed['pages'][new_task]['parent'] == new_map
    from kelmo.projects import resolve_bound_work
    assert resolve_bound_work(renamed, 'gamma', 'alpha', 'task') == new_task
    assert resolve_bound_work(renamed, 'gamma', 'other', 'task') is None
    view = hot_views.render(renamed, new_map, '')
    assert '**Task** (H1)' in view and 'Work in progress.' in view


def test_rename_publishes_open_work_with_grouping(tmp_path):
    import registry_seed
    from kelmo import projects
    from kelmo.initialization import initialize
    from kelmo.wiki import engine
    root = tmp_path / 'vault'
    initialize(root, 'rename-work', project='alpha', git_name='Test', git_email='test@example.invalid')
    map_path, topic, task = 'projects/alpha/alpha.md', 'projects/alpha/area.md', 'projects/alpha/task.md'
    note = 'b' * 64
    spec = {'notes': {note: {'filename': 'source.md', 'ark_id': 'archive-2'}},
            'facts': {'work': registry_seed.fact('work', task, 'Working.', note, 'archive-2',
                                                 status='in-progress')}}
    settings = {map_path: {'page_id': 'alpha', 'layout': 'project-map', 'title': 'Alpha'},
                topic: {'page_id': 'area', 'layout': 'topic', 'title': 'Area', 'parent': map_path},
                task: {'page_id': 'task', 'layout': 'feature', 'title': 'Task', 'parent': topic,
                       'priority': 'H1', 'summary': 'Work in progress.'}}
    registry_seed.seed(root, spec, pages_settings=settings)
    projects.add(root, 'alpha')
    projects.rename(root, 'alpha', 'gamma')
    registry = engine.load_registry(root)
    assert registry['facts']['work']['target'] == 'projects/gamma/task.md'
    text = (root / 'knowledge/hot/gamma.md').read_text()
    assert '### Area' in text and '**Task** (H1)' in text and 'Work in progress.' in text


def test_rename_updates_selected_page_paths_in_other_hot_selections():
    from kelmo.projects import _rename_config
    old, new = 'projects/alpha/task.md', 'projects/gamma/task.md'
    before = {'pages': [old], 'projects': {'alpha': {'roots': [], 'pages': [old]},
                                           'other': {'roots': [], 'pages': [old]}}}
    result = _rename_config(before, 'alpha', 'gamma', {old: new})
    assert result['pages'] == [new]
    assert result['projects']['gamma']['pages'] == result['projects']['other']['pages'] == [new]
    assert before['pages'] == [old]


def test_add_existing_project_creates_generated_set(tmp_path, monkeypatch):
    from kelmo import config, projects
    from kelmo.initialization import initialize
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    root = tmp_path / 'vault'
    initialize(root, 'existing-hub', project='alpha', git_name='Test', git_email='test@example.invalid')
    projects.add(root, 'alpha')
    assert (root / 'knowledge/hot/mandatory.md').is_file()
    assert (root / 'knowledge/hot/alpha.full.md').is_file()


def test_repository_enrollment_revision_only_changes_once(tmp_path, monkeypatch):
    import copy
    from contextlib import nullcontext
    from kelmo import projects, server_admin
    value = {'schema': 'kelmo-server/1', 'authority_id': 'synthetic',
             'writer': {'writer_id': 'local', 'epoch': 1}, 'clients': [], 'repositories': []}
    config = SimpleNamespace()
    monkeypatch.setattr(server_admin, 'profile', lambda *args: (config, tmp_path / 'profile', b'prior', copy.deepcopy(value)))
    monkeypatch.setattr(server_admin, 'coordinator_lock', lambda *args: nullcontext())
    monkeypatch.setattr(server_admin.ServerProfile, 'from_dict', lambda data: None)
    monkeypatch.setattr(projects, 'add', lambda *args: None)
    def save(path, raw, **kwargs):
        import json
        value.clear()
        value.update(json.loads(raw))
        return {'backup': 'synthetic'}
    monkeypatch.setattr(server_admin, 'write', save)
    first = server_admin.enroll_repository(tmp_path, tmp_path / 'profile', 'repo', tmp_path, 'https://example.invalid/a',
                                           project='alpha', apply=True)
    second = server_admin.enroll_repository(tmp_path, tmp_path / 'profile', 'repo', tmp_path, 'https://example.invalid/a',
                                            project='alpha', apply=True)
    assert first['enrollment_revision'] == second['enrollment_revision'] == 1
    assert first['changed'] and not second['changed']


def test_server_repository_enrollment_opens_hub(tmp_path):
    import json
    from kelmo import config, server_admin
    from kelmo.initialization import initialize
    from kelmo.server import PROFILE_SCHEMA
    from kelmo.wiki import engine
    root, profile = tmp_path / 'vault', tmp_path / 'profile.json'
    initialize(root, 'server-hub', project='alpha', git_name='Test', git_email='test@example.invalid')
    profile.write_text(json.dumps({'schema': PROFILE_SCHEMA, 'authority_id': config.Config.load(root).vault_id,
                                   'writer': {'writer_id': 'local', 'epoch': 1}, 'clients': [], 'repositories': []}))
    result = server_admin.enroll_repository(root, profile, 'repo', tmp_path / 'repository',
                                            'https://example.invalid/repo', project='beta', apply=True)
    assert result['enrollment_revision'] == 1
    assert 'projects/beta/beta.md' in engine.load_registry(root)['pages']


def test_add_recovers_publication_after_interruption(tmp_path, monkeypatch):
    from kelmo import config, projects
    from kelmo.initialization import initialize
    from kelmo.wiki import engine
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    root = tmp_path / 'vault'
    initialize(root, 'add-recovery', project='alpha', git_name='Test', git_email='test@example.invalid')
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, 'publish_project_map', lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted')))
        with pytest.raises(RuntimeError, match='interrupted'):
            projects.add(root, 'beta')
    assert (root / '.state/project-add.json').is_file()
    projects.add(root, 'beta')
    assert not (root / '.state/project-add.json').exists()
    assert 'projects/beta/beta.md' in engine.load_registry(root)['pages']


@pytest.mark.parametrize('boundary', ['before-commit', 'after-commit'])
def test_rename_recovers_deleted_paths_at_commit_boundary(tmp_path, monkeypatch, boundary):
    from kelmo import projects
    from kelmo.initialization import initialize
    from kelmo.wiki import engine
    root = tmp_path / 'vault'
    initialize(root, 'rename-boundary', project='alpha', git_name='Test', git_email='test@example.invalid')
    projects.add(root, 'beta')
    original = engine.git
    triggered = []
    def failing(repository, *args, **kwargs):
        if not triggered and boundary == 'before-commit' and args[0] == 'add':
            triggered.append(True)
            raise RuntimeError('interrupted')
        result = original(repository, *args, **kwargs)
        if not triggered and boundary == 'after-commit' and args[0] == 'commit':
            triggered.append(True)
            raise RuntimeError('interrupted')
        return result
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, 'git', failing)
        with pytest.raises(RuntimeError, match='interrupted'):
            projects.rename(root, 'beta', 'gamma')
    projects.rename(root, 'beta', 'gamma')
    assert not (root / 'knowledge/wiki/projects/beta/beta.md').exists()
    assert (root / 'knowledge/wiki/projects/gamma/gamma.md').is_file()
    assert not (root / 'knowledge/hot/beta.md').exists()
    assert not (root / '.state/wiki-transaction.json').exists()


def test_configured_selection_without_map_gets_its_own_view(tmp_path):
    wiki, out = tmp_path / 'wiki', tmp_path / 'hot'
    selected = wiki / 'topic.md'
    rendered = hot_views.render_all({'pages': {}, 'facts': {}}, {selected: 'Legacy selected page.'}, wiki, out,
                                   generation=1, selection={'projects': {'legacy': {'pages': ['topic.md']}}})
    assert 'Legacy selected page.' in rendered[out / 'legacy.md']
    assert 'Legacy selected page.' in rendered[out / 'legacy.full.md']


def test_generated_views_preserve_legacy_mandatory_import_names(tmp_path):
    wiki, out = tmp_path / 'wiki', tmp_path / 'hot'
    registry = {'pages': {'person.md': {'kind': 'hub', 'layout': 'person', 'title': 'Person'}}, 'facts': {}}
    rendered = hot_views.render_all(registry, {wiki / 'person.md': '# Person\nCurrent rules.'}, wiki, out, generation=2)
    assert 'Current rules.' in rendered[out / 'mandatory-person.md']


def test_first_rename_conflict_does_not_enable_unpublished_views(tmp_path):
    from kelmo.initialization import initialize
    from kelmo import projects
    root = tmp_path / 'vault'
    initialize(root, 'rename-conflict', project='alpha', git_name='Test', git_email='test@example.invalid')
    path = root / 'kelmo.toml'
    path.write_text(path.read_text() + '\n[hot.projects.beta]\nroots=[]\npages=[]\n')
    before = path.read_bytes()
    with pytest.raises(KelmoError):
        projects.rename(root, 'alpha', 'beta')
    assert path.read_bytes() == before


def test_unlink_one_clone_does_not_reselect_it_by_shared_remote(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from kelmo import projects
    hot = {'projects': {'alpha': {'roots': [str(tmp_path/'a'), str(tmp_path/'b')],
                                 'common_dirs': ['/a.git', '/b.git'], 'remotes': ['example.invalid/repo']}}}
    @contextmanager
    def locked(root):
        yield SimpleNamespace(config=SimpleNamespace(root=tmp_path, hot=hot))
    monkeypatch.setattr(projects, '_locked', locked)
    monkeypatch.setattr(projects, '_raw', lambda config: hot)
    monkeypatch.setattr(projects, '_save', lambda config, after, before: {'changed': after != before})
    monkeypatch.setattr(projects, '_git_identity', lambda path: ('/'+Path(path).name+'.git', ['example.invalid/repo']))
    projects.unlink(tmp_path, tmp_path/'a')
    config = SimpleNamespace(hot=hot)
    assert projects.resolve(config, tmp_path/'a') is None
    assert projects.resolve(config, tmp_path/'b') == 'alpha'
    assert projects.resolve(config, tmp_path/'new-clone') == 'alpha'
    hot['projects']['alpha']['roots'].append(str(tmp_path))
    assert projects.resolve(config, tmp_path/'a') is None
    assert not projects.unlink(tmp_path, tmp_path/'a')['changed']


def test_upgrade_keeps_legacy_hook_delivery_before_new_publication(tmp_path):
    from kelmo.initialization import initialize
    from kelmo.integration import hook
    root = tmp_path / 'vault'
    initialize(root, 'legacy-delivery', project='alpha', git_name='Test', git_email='test@example.invalid')
    path = root / 'kelmo.toml'
    path.write_text(path.read_text() + '\n[hot]\nviews=true\npages=["projects/alpha/alpha.md"]\n')
    output = hook(SimpleNamespace(root=root), 'start', {'kelmo_provider': 'codex'})['hookSpecificOutput']['additionalContext']
    assert 'projects/alpha/alpha.md' in output and 'unavailable' not in output


def test_legacy_mandatory_alias_combines_duplicate_basenames_and_refuses_project_collision(tmp_path):
    wiki, out = tmp_path / 'wiki', tmp_path / 'hot'
    registry = {'pages': {name: {'kind': 'hub', 'layout': 'person', 'title': name}
                          for name in ('first/person.md', 'second/person.md')}, 'facts': {}}
    source = {wiki / 'first/person.md': 'FIRST_RULE', wiki / 'second/person.md': 'SECOND_RULE'}
    views = hot_views.render_all(registry, source, wiki, out, generation=1)
    assert 'FIRST_RULE' in views[out/'mandatory-person.md'] and 'SECOND_RULE' in views[out/'mandatory-person.md']
    registry['pages']['projects/mandatory-person/mandatory-person.md'] = {
        'kind': 'hub', 'layout': 'project-map', 'title': 'Collision'}
    with pytest.raises(ValueError, match='collision'):
        hot_views.render_all(registry, source, wiki, out, generation=2)


@pytest.mark.parametrize('remote', ['https://example.invalid:8443/team/repo.git', 'ssh://git@example.invalid:8443/team/repo.git'])
def test_remote_selection_preserves_nondefault_ports(tmp_path, monkeypatch, remote):
    from kelmo import projects
    identity = projects._remote(remote)
    monkeypatch.setattr(projects, '_git_identity', lambda path: ('/unlinked-clone.git', [identity]))
    config = SimpleNamespace(hot={'projects': {'alpha': {'remotes': ['example.invalid:8443/team/repo']}}})
    assert projects.resolve(config, tmp_path) == 'alpha'
    assert projects._remote(identity) == 'example.invalid:8443/team/repo'


def test_project_exclusions_preserve_other_explicit_bindings(tmp_path, monkeypatch):
    from kelmo import projects
    hot = projects.validate_hot({'projects': {
        'alpha': {'remotes': ['example.invalid/repo'], 'roots': [str(tmp_path)],
                  'excluded_common_dirs': ['./detached.git']},
        'beta': {'roots': [str(tmp_path/'other')]}}}, tmp_path)
    monkeypatch.setattr(projects, '_git_identity', lambda cwd: (str(tmp_path/'detached.git'), ['example.invalid/repo']))
    assert projects.resolve(SimpleNamespace(hot=hot), tmp_path) is None
    assert projects.resolve(SimpleNamespace(hot=hot), tmp_path/'other') == 'beta'
    assert projects.resolve(SimpleNamespace(hot=hot), tmp_path/'sibling') is None
    hot['projects']['beta']['common_dirs'] = [str(tmp_path/'detached.git')]
    assert projects.resolve(SimpleNamespace(hot=hot), tmp_path/'sibling') == 'beta'
    with pytest.raises(KelmoError, match='excluded'):
        projects.validate_hot({'projects': {'alpha': {'common_dirs': ['./repo.git'],
                                                      'excluded_common_dirs': ['./repo.git']}}}, tmp_path)


def test_unlink_remote_only_binding(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from kelmo import projects
    hot = {'projects': {'alpha': {'roots': [], 'remotes': ['example.invalid/repo']}}}
    @contextmanager
    def locked(root):
        yield SimpleNamespace(config=SimpleNamespace(root=tmp_path, hot=hot))
    monkeypatch.setattr(projects, '_locked', locked)
    monkeypatch.setattr(projects, '_raw', lambda config: hot)
    monkeypatch.setattr(projects, '_save', lambda config, after, before: {'changed': after != before})
    monkeypatch.setattr(projects, '_git_identity', lambda cwd: (str(tmp_path/'repo.git'), ['example.invalid/repo']))
    assert not projects.unlink(tmp_path, tmp_path, project='other')['changed']
    assert projects.unlink(tmp_path, tmp_path)['changed']
    assert projects.resolve(SimpleNamespace(hot=hot), tmp_path) is None


def test_unregistered_published_person_survives_hubs_and_reserves_its_import(tmp_path):
    from kelmo.initialization import initialize
    from kelmo import projects
    from kelmo.wiki import engine
    root = tmp_path / 'vault'
    initialize(root, 'unregistered-person', project='alpha', git_name='Test', git_email='test@example.invalid')
    person = root / 'knowledge/wiki/person.md'
    person.write_text('# Person\nUNREGISTERED_RULE\n')
    engine.git(root, 'add', 'knowledge/wiki/person.md')
    engine.git(root, 'commit', '-qm', 'Add personal instructions')
    from registry_seed import _restamp
    from kelmo.config import Config
    _restamp(Config.load(root))
    for name in ('alpha', 'beta'):
        projects.add(root, name)
        for view in ('mandatory.md', 'mandatory-person.md', name + '.full.md'):
            assert 'UNREGISTERED_RULE' in (root / 'knowledge/hot' / view).read_text()
    before = (root / 'kelmo.toml').read_bytes()
    with pytest.raises(ValueError, match='collision'):
        projects.add(root, 'mandatory-person')
    assert (root / 'kelmo.toml').read_bytes() == before
    assert not (root / '.state/project-add.json').exists()


def test_selected_pages_are_preserved_and_count_toward_view_warning(tmp_path):
    wiki, out = tmp_path/'wiki', tmp_path/'hot'
    registry = {'pages': {'projects/app/app.md': {'kind': 'hub', 'layout': 'project-map', 'title': 'App'}}, 'facts': {}}
    selected = 'SELECTED_' * 700
    views = hot_views.render_all(registry, {wiki/'projects/app/app.md': '# App\nSmall.', wiki/'extra.md': selected},
                                wiki, out, max_bytes=4096, generation=1,
                                selection={'projects': {'app': {'pages': ['extra.md']}}})
    assert selected in views[out/'app.md']
    assert hot_views.over_budget(views[out/'app.md']) > 0


def test_rename_retitles_a_map_named_after_the_project_and_keeps_a_hand_set_title():
    import registry_seed
    from kelmo.wiki import engine, hot_views
    for title, expected in (('Demo A', 'Demo Alpha'), ('Harbor demo', 'Harbor demo')):
        map_path = 'projects/demo-a/demo-a.md'
        settings = {map_path: {'page_id': 'demo-a', 'layout': 'project-map', 'title': title}}
        registry = registry_seed.build({'notes': {}, 'facts': {}}, authority_id='synthetic',
                                       pages_settings=settings)
        before = registry['pages'][map_path]['revision']
        renamed, _ = engine.apply_project_rename(registry, 'demo-a', 'demo-alpha')
        entry = renamed['pages']['projects/demo-alpha/demo-alpha.md']
        assert entry['title'] == expected
        assert (entry['previous_revision'] == before) == (title != expected)
        assert '# ' + expected in hot_views.render(renamed, 'projects/demo-alpha/demo-alpha.md', '').splitlines()[:2]
