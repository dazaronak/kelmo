"""Renaming a project must preserve its existing branch and server bindings."""
from contextlib import nullcontext
from types import SimpleNamespace

from tests.test_hot import config


def test_local_delivery_follows_bound_work_after_project_rename(tmp_path, monkeypatch):
    from kelmo import client, hot, hub_delivery, projects
    from kelmo.wiki.pages import generation_paths
    selected = config(tmp_path, '[hot]\nviews=true\n[hot.projects.new]\nroots=[]\n')
    target = 'projects/new/task.md'
    registry = {'pages': {target: {'page_id': 'work-id', 'kind': 'work'}},
                'redirects': {'projects/old/task.md': 'work-id'}}
    root, registry_path, wiki_path, *_ = generation_paths(selected)
    reader = SimpleNamespace(config=selected, root=root, registry_path=registry_path, wiki_path=wiki_path,
                             generation=lambda: 2, _pinned=lambda _: {'paths': {target}, 'registry': registry},
                             page=lambda name, generation: SimpleNamespace(content=b'Current task context.'))
    monkeypatch.setattr(projects, 'resolve', lambda config, cwd: 'new')
    monkeypatch.setattr(client, 'local_binding', lambda cwd: {
        'binding_status': 'bound', 'work': 'task', 'work_project': 'old'})
    monkeypatch.setattr(hub_delivery, 'published_set', lambda *args: {})
    result = hot.hub_context(selected, tmp_path, reader=reader)
    assert result['hub_work'] == 'Current task context.'
    assert result['hub_work_path'] == target
    assert not result['warning']


def test_server_project_identity_follows_map_redirect_without_changing_enrollment():
    from kelmo.projects import canonical_project
    registry = {'pages': {'projects/new/new.md': {'page_id': 'map-id', 'layout': 'project-map'}},
                'redirects': {'projects/old/old.md': 'map-id'}}
    assert canonical_project(registry, 'old') == 'new'
    assert canonical_project(registry, 'new') == 'new'
    assert canonical_project(registry, 'unrelated') is None


def test_queued_request_follows_rename_without_rewriting_archived_metadata(monkeypatch):
    from kelmo import creation
    registry = {'pages': {'projects/new/new.md': {'page_id': 'map-id', 'layout': 'project-map'}},
                'redirects': {'projects/old/old.md': 'map-id'}}
    saved = {'project': 'old', 'selected': ['projects/old/old.md'], 'pending': 'Waiting for publication.',
             'new_work': {'target': 'projects/old/task.md', 'parent': 'projects/old/old.md',
                          'page_id': 'stable-work-id', 'title': 'Task', 'summary': 'Work started.'}}
    reader = SimpleNamespace(conn=SimpleNamespace(execute=lambda sql, *params: SimpleNamespace(fetchall=lambda: [{'id': 'op'}])),
                             get=lambda identifier: {'id': identifier},
                             source_metadata=lambda identifier: {'metadata': {'editor_request': saved}})
    monkeypatch.setattr(creation.engine, 'load_registry', lambda root: registry)
    pending = creation.pending_operations(SimpleNamespace(root='/unused', journal=reader), 'new')
    assert len(pending) == 1
    current = pending[0][1]
    assert current['project'] == 'new' and current['selected'] == ['projects/new/new.md']
    assert current['new_work']['target'] == 'projects/new/task.md'
    assert current['new_work']['parent'] == 'projects/new/new.md'
    assert current['new_work']['page_id'] == 'stable-work-id'
    assert saved['project'] == 'old' and saved['new_work']['target'] == 'projects/old/task.md'
    pinned = {'pages': {'projects/old/old.md': {'page_id': 'map-id', 'layout': 'project-map'}}, 'redirects': {}}
    pending = creation.pending_operations(SimpleNamespace(root='/unused', journal=reader), 'old', registry=pinned)
    assert pending[0][1]['project'] == 'old'


def test_hook_pending_includes_captured_notes_without_making_them_editor_work(monkeypatch):
    import sqlite3
    from kelmo import creation
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE operations (id, intent, terminal, state, created_at)')
    states = ['captured', 'queued', 'archived', 'parked', 'applied', 'failed']
    connection.executemany('INSERT INTO operations VALUES (?, ?, ?, ?, ?)',
                           [(state, 'create', state in ('applied', 'failed'), state, index)
                            for index, state in enumerate(states)])
    reader = SimpleNamespace(conn=connection, get=lambda state: {'id': state, 'state': state},
                             source_metadata=lambda state: {'metadata': {'editor_request': {
                                 'project': 'app', 'selected': [], 'pending': state}}})
    service = SimpleNamespace(root='/unused', journal=reader)
    monkeypatch.setattr(creation.engine, 'load_registry', lambda root: {'pages': {}, 'redirects': {}})
    try:
        assert [item['pending'] for item in creation.pending_summaries(service, 'app')] == states[:4]
        assert [operation['state'] for operation, _ in creation.pending_operations(service, 'app')] == states[2:4]
    finally:
        connection.close()


def test_new_work_retry_keeps_its_original_page_id_after_project_rename(monkeypatch, tmp_path):
    from kelmo import creation
    registry = {'pages': {'projects/new/new.md': {'page_id': 'map-id', 'layout': 'project-map'},
                          'projects/new/task.md': {'page_id': 'projects-old-task', 'kind': 'work'}},
                'redirects': {'projects/old/old.md': 'map-id'}}
    previous = {'project': 'old', 'selected': ['projects/old/old.md'], 'rule_change': False,
                'budget_tokens': 60000, 'new_work': {'target': 'projects/old/task.md',
                'page_id': 'projects-old-task', 'parent': 'projects/old/old.md',
                'title': 'Task', 'summary': 'Task'}}
    service = SimpleNamespace(root=tmp_path, config=SimpleNamespace(wiki=tmp_path))
    monkeypatch.setattr(creation.engine, 'load_registry', lambda root: registry)
    result = creation.request(service, 'old', [], False, 60000,
                              new_work={'slug': 'task', 'title': 'Task'}, previous=previous)
    assert result['new_work']['page_id'] == 'projects-old-task'
    assert result['new_work']['target'] == 'projects/new/task.md'
    assert previous['new_work']['target'] == 'projects/old/task.md'


def test_shared_global_context_only_includes_pending_for_authorized_projects(tmp_path, monkeypatch):
    from kelmo import creation, hot, hub_delivery
    from kelmo.config import Config
    from kelmo.server import ServerProfile, shared_context
    profile = ServerProfile.from_dict({
        'schema': 'kelmo-server/1', 'authority_id': '00000000-0000-0000-0000-000000000001',
        'writer': {'writer_id': 'local', 'epoch': 1},
        'clients': [{'client_id': 'a', 'machine_tag': 'test', 'repository_ids': ['repo-a']}],
        'repositories': [{'repository_id': 'repo-a', 'root': '/private/tmp/a',
                          'remote': 'https://example.invalid/a', 'project': 'alpha'},
                         {'repository_id': 'repo-b', 'root': '/private/tmp/b',
                          'remote': 'https://example.invalid/b', 'project': 'beta'}]})
    registry = {'pages': {}, 'redirects': {}}
    reader = SimpleNamespace(generation=lambda: 1, _pinned=lambda _: {'registry': registry},
                             holding_head=nullcontext)
    service = SimpleNamespace(root=tmp_path, knowledge=reader)
    monkeypatch.setattr(Config, 'load', lambda root: SimpleNamespace(hot={'pages': [], 'projects': {},
                                                                         'views': {'max_bytes': 48000}, 'max_bytes': 48000}))
    monkeypatch.setattr(hot, 'adopted', lambda *args: True)
    monkeypatch.setattr(hot, 'has_hub_generation', lambda *args: True)
    monkeypatch.setattr(hot, '_render_generation', lambda *args, **kwargs: {'context': 'mandatory'})
    monkeypatch.setattr(hub_delivery, 'published_set', lambda *args: {})
    def operations(service, project, **kwargs):
        return [({'id': name, 'state': 'queued'}, {'project': name, 'pending': name+' pending'})
                for name in ('alpha', 'beta') if project is None or project == name]
    monkeypatch.setattr(creation, 'pending_operations', operations)
    result = shared_context(service, profile, profile.bindings.origin('a'))
    assert [item['project'] for item in result['pending']] == ['alpha']
    import pytest
    from kelmo.errors import KelmoError
    generations = iter([1, 2])
    reader.generation = lambda: next(generations)
    with pytest.raises(KelmoError, match='changed'):
        shared_context(service, profile, profile.bindings.origin('a'))
