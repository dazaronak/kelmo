"""Local mode and the shared server answer through the same Kelmo operations.

Callers of the compatibility tool names go through the Kelmo compatibility wrapper; no legacy backend is
imported or executed by these paths.
"""

import io
import json
import sqlite3

import pytest

from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: F401
from test_shared_context import publish_push, shared as shared  # noqa: F401


def test_local_mode_needs_no_private_server(world_factory, monkeypatch):
    from kelmo.config import ClientProfile
    world = world_factory()
    monkeypatch.setattr('socket.socket.connect', lambda *args: (_ for _ in ()).throw(AssertionError('network')))
    assert ClientProfile.selected(world.root) is False
    ref = world.archive('Local mode keeps working without any server.', key='local-only')
    assert world.service.show(ref.archive_id)['content'].startswith('Local mode')
    status = world.service.status()
    assert status['writer'] == 'local' and status['remote_configured'] is False
    assert world.service.search('server', with_metadata=True)['search']['mode'] in ('hybrid', 'lexical')


def test_shared_and_local_reads_return_the_same_records(shared, monkeypatch, capsys):
    from contextlib import nullcontext
    from kelmo.cli import call
    from kelmo.client import ClientService
    service = ClientService(shared.client('mac-a'))
    captured = service.add('Parity between local and shared reads.', 'parity.md', role='implementer',
                           idempotency_key='parity')
    shared.hub.session('mac-a').service.run(captured['operation']['id'])
    captured = service.wait(captured)
    archive_id = captured['operation']['archive_id']
    local = shared.world.service
    assert service.show(archive_id) == local.show(archive_id)
    assert service.list(20, 0) == json.loads(json.dumps(local.list(20, 0)))
    assert service.search('parity', with_metadata=True) == json.loads(json.dumps(
        local.search('parity', with_metadata=True)))
    monkeypatch.setattr('kelmo.client.SharedClient', lambda profile: nullcontext(service.client))
    for command, expected in (
        (['pages', '--project', 'app'], local.page_index(['app'])),
        (['page', APP_PAGE], local.page_facts(APP_PAGE)),
        (['context', '--project', 'app', '--budget-tokens', '60000', '--select', APP_PAGE],
         local.editor_context('app', 60000, [APP_PAGE])),
    ):
        assert call(['--vault', str(service.client.profile.root), *command, '--json']) == 0
        assert json.loads(capsys.readouterr().out)['result'] == expected


def test_compat_tools_match_between_local_and_shared(shared):
    from kelmo.client import ClientService
    from kelmo.mcp import compat
    local = shared.world.service
    remote = ClientService(shared.client('mac-a'))
    stored = compat(local, 'memory_store', {'content': 'Old-shaped store through Kelmo.', 'kind': 'decision',
                                            'domain': 'dev', 'project': 'app', 'tags': ['compat']})
    for tool, arguments in [('memory_get', {'id': stored['id'], 'full': True}),
                            ('memory_get', {'id': stored['id'], 'version': 1}),
                            ('memory_history', {'id': stored['id']}),
                            ('memory_list', {'project': 'app', 'tags': ['compat'], 'order_by': 'valid_from'}),
                            ('memory_search', {'query_en': 'Old-shaped', 'query_ru': 'Old-shaped',
                                               'mode': 'hybrid', 'kind': 'decision'}),
                            ('memory_stats', {})]:
        assert remote.compat(tool, arguments) == compat(local, tool, arguments), tool


def test_old_shaped_versions_history_chunks_and_filters(shared):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.mcp import compat
    service = shared.world.service
    first = compat(service, 'memory_store', {'content': 'Version one of the rule.', 'kind': 'rule',
                                             'domain': 'dev', 'project': 'app', 'valid_from': '2026-01-02'})
    other = compat(service, 'memory_store', {'content': 'An unrelated life event.', 'kind': 'event',
                                             'domain': 'life'})
    updated = compat(service, 'memory_update', {'id': first['id'], 'content': 'Version two of the rule.',
                                                'expected_version': 1})
    assert updated['version'] == 2 and updated['unchanged'] is False
    with pytest.raises(KelmoError) as error:
        compat(service, 'memory_update', {'id': first['id'], 'content': 'Stale writer.', 'expected_version': 1})
    assert error.value.code == 'E_CONFLICT'
    assert compat(service, 'memory_update', {'id': first['id'], 'content': 'Version two of the rule.'})['unchanged']
    exact = compat(service, 'memory_get', {'id': first['id'], 'version': 1})
    assert exact['content'] == 'Version one of the rule.' and exact['valid_from'] == '2026-01-02'
    history = compat(service, 'memory_history', {'id': first['id']})
    assert [item['version'] for item in history['versions']] == [1] and history['next_cursor'] is None

    listed = compat(service, 'memory_list', {'domain': 'life'})
    assert [item['id'] for item in listed['items']] == [other['id']] and listed['total'] == 1
    assert compat(service, 'memory_list', {'since': '2026-01-01', 'until': '2026-01-03'})['total'] == 1

    # Existing stored chunk windows are readable; chunk generation itself is a separate task.
    connection = sqlite3.connect(Config.load(shared.world.root).archive)
    try:
        with connection:
            connection.executemany('INSERT INTO chunks (memory_id, idx, text) VALUES (?,?,?)',
                                   [(other['id'], index, f'chunk {index}') for index in range(4)])
    finally:
        connection.close()
    window = compat(service, 'memory_get', {'id': other['id'], 'chunk_idx': 1, 'neighbors': 1})
    assert [chunk['chunk_idx'] for chunk in window['chunks']] == [0, 1, 2]
    assert [chunk['text'] for chunk in window['chunks']] == ['chunk 0', 'chunk 1', 'chunk 2']
    # The old chunk read keeps record metadata without the full content.
    assert window['id'] == other['id'] and window['kind'] == 'event' and 'content' not in window
    wide = compat(service, 'memory_get', {'id': other['id'], 'chunk_idx': 0, 'neighbors': 9})
    assert [chunk['chunk_idx'] for chunk in wide['chunks']] == [0, 1, 2, 3], 'neighbors are capped at three'
    with pytest.raises(KelmoError) as error:
        compat(service, 'memory_get', {'id': other['id']})
    assert error.value.code == 'E_INPUT', 'as before: a chunk index or full=true is required'
    assert compat(service, 'memory_get', {'id': other['id'], 'full': True})['content'] == 'An unrelated life event.'

    with pytest.raises(KelmoError) as error:
        compat(service, 'memory_delete', {'id': other['id']})
    assert error.value.code == 'E_INPUT', 'there is no delete tool; hiding is the CLI verb kelmo forget'
    assert service.show(other['id'])['id'] == other['id'], 'an unknown tool removes and hides nothing'
    # Both variants reach the archive contract; the record index fuses their rankings too.
    found = compat(service, 'memory_search', {'query_en': 'rule', 'query_ru': 'правило', 'mode': 'hybrid'})
    assert [row['version'] for row in found if row['id'] == first['id']] == [2]
    with pytest.raises(KelmoError) as error:
        compat(service, 'memory_search', {'query': 'rule'})
    assert error.value.code == 'E_INPUT', 'the frozen signature takes query_en and query_ru'
    with pytest.raises(KelmoError) as error:
        compat(service, 'memory_update', {'id': first['id'], 'source': 'other'})
    assert error.value.code == 'E_INPUT', 'the frozen update has no source argument'


def test_raw_note_path_survives_verbatim_and_is_never_opened(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.mcp import compat
    original = 'knowledge/raw/notes/2026-01-02 draft (v2).md'
    import io
    import os
    opened = []
    real = {'builtins.open': open, 'io.open': io.open, 'os.open': os.open, 'os.stat': os.stat}

    def watch(name):
        def watched(path, *args, **kwargs):
            opened.append(str(path))
            return real[name](path, *args, **kwargs)
        return watched
    remote = ClientService(shared.client('mac-a'))
    for name in real:
        monkeypatch.setattr(name, watch(name))
    stored = remote.compat('memory_store', {'content': 'Raw path survives.', 'kind': 'event', 'domain': 'dev',
                                            'raw_note_path': original})
    for name, function in real.items():
        monkeypatch.setattr(name, function)
    assert stored['raw_note_path'] == original and stored['filename'] == 'memory-store.md'
    assert not any('draft (v2)' in path for path in opened), 'the opaque path is never resolved or opened'
    service = shared.world.service
    full = compat(service, 'memory_get', {'id': stored['id'], 'full': True})
    assert full['raw_note_path'] == original and full['archive_raw_note_path'] != original
    assert compat(service, 'memory_get', {'id': stored['id'], 'version': 1})['raw_note_path'] == original
    listed = compat(service, 'memory_list', {'kind': 'event'})
    assert [item['raw_note_path'] for item in listed['items'] if item['id'] == stored['id']] == [original]
    found = compat(service, 'memory_search', {'query_en': 'survives', 'query_ru': 'survives'})
    assert [item['raw_note_path'] for item in found if item['id'] == stored['id']] == [original]
    assert remote.compat('memory_get', {'id': stored['id'], 'full': True}) == full
    # A path with a safe basename keeps that basename as the internal source name.
    named = compat(service, 'memory_store', {'content': 'Named source.', 'kind': 'event', 'domain': 'dev',
                                             'raw_note_path': 'notes/2026-01-03_named.md'})
    assert named['filename'] == '2026-01-03_named.md' and named['raw_note_path'] == 'notes/2026-01-03_named.md'
    [operation_id] = [row['id'] for row in service.journal.conn.execute(
        'SELECT id FROM operations WHERE archive_id=?', (named['id'],))]
    assert service.journal.source_metadata(operation_id)['metadata']['compat'] == {
        'raw_note_path': 'notes/2026-01-03_named.md'}


def test_bilingual_variants_are_forwarded_to_the_archive_contract():
    from types import SimpleNamespace
    from kelmo.contracts import EvidenceRef, SearchHit, SearchResult
    from kelmo.mcp import compat
    calls = []

    class Archive:
        def search(self, queries, filters, limit, mode='hybrid'):
            calls.append((queries, filters.backend(), limit, mode))
            ref = EvidenceRef('record-1', 2, 'a' * 64, 'source-1')
            return SearchResult((SearchHit(ref, 'fused chunk'),), 'hybrid', False, None, 'fixture-index/1')

    rows = compat(SimpleNamespace(archive_api=Archive()), 'memory_search',
                  {'query_en': 'deploy rule', 'query_ru': 'правило деплоя', 'kind': 'rule', 'limit': 3})
    assert calls == [(('deploy rule', 'правило деплоя'), {'kind': 'rule'}, 3, 'hybrid')]
    # No locator and no full-record read in this backend: both boundaries are named, not guessed.
    assert rows == [{'id': 'record-1', 'version': 2, 'content': 'fused chunk', 'content_sha256': 'a' * 64,
                     'search_mode': 'hybrid', 'degraded': False, 'degraded_reason': None, 'semantic': True,
                     'unavailable_fields': ['chunk_idx', 'chunk_total']}]


def test_search_returns_chunk_rows_with_locators_and_the_diversity_cap():
    from types import SimpleNamespace
    from kelmo.contracts import ChunkLocator, ChunkWindow, EvidenceRef, SearchHit, SearchResult
    from kelmo.mcp import compat
    ref = EvidenceRef('record-1', 3, 'b' * 64, 'source-1')
    locators = tuple(ChunkLocator('record-1', 3, 'b' * 64, 'chunk:profile', index) for index in (4, 1, 7))

    class Archive:
        def search(self, queries, filters, limit, mode='hybrid'):
            return SearchResult((SearchHit(ref, 'best chunk', locators),), 'hybrid', False, None, 'chunk:profile')

        def full_record(self, archive_id):
            return {'id': archive_id, 'version': 3, 'content': 'whole text', 'kind': 'rule', 'chunk_total': 9,
                    'versions': [{'version': 1}, {'version': 2}]}

        def expand(self, locator, neighbors):
            assert neighbors == 0
            return ChunkWindow(locator, locator.chunk_index, ('chunk %d' % locator.chunk_index,))

    rows = compat(SimpleNamespace(archive_api=Archive()), 'memory_search', {'query_en': 'rule', 'query_ru': ''})
    assert [(row['chunk_idx'], row['content']) for row in rows] == [(4, 'best chunk'), (1, 'chunk 1')]
    assert all(row['chunk_total'] == 9 and row['kind'] == 'rule' for row in rows)
    assert all('versions' not in row and row['locator']['index_generation'] == 'chunk:profile' for row in rows)


def test_full_get_keeps_chunk_total_and_versions(shared):
    from kelmo.client import ClientService
    from kelmo.mcp import compat
    service = shared.world.service
    stored = compat(service, 'memory_store', {'content': 'Full read one.', 'kind': 'rule', 'domain': 'dev'})
    compat(service, 'memory_update', {'id': stored['id'], 'content': 'Full read two.', 'expected_version': 1})
    full = compat(service, 'memory_get', {'id': stored['id'], 'full': True})
    assert full['content'] == 'Full read two.' and 'chunk_total' in full
    assert [item['version'] for item in full['versions']] == [1]
    assert ClientService(shared.client('mac-a')).compat('memory_get', {'id': stored['id'], 'full': True}) == full


def test_stats_report_the_old_fields_from_the_archive_alike_local_and_shared(shared):
    from kelmo.client import ClientService
    from kelmo.mcp import STATS_FIELDS, STATS_UNAVAILABLE, compat
    service = shared.world.service
    compat(service, 'memory_store', {'content': 'A counted rule.', 'kind': 'rule', 'domain': 'dev', 'project': 'app'})
    hidden = compat(service, 'memory_store', {'content': 'A forgotten event.', 'kind': 'event', 'domain': 'life'})
    service.forget(hidden['id'])
    stats = compat(service, 'memory_stats', {})
    assert stats['unavailable_fields'] == []
    assert all(key in stats for key in (*STATS_FIELDS, *STATS_UNAVAILABLE))
    assert stats['by_kind'].get('rule', 0) >= 1 and stats['by_project'].get('app', 0) >= 1
    assert 'life' not in stats['by_domain'], 'a forgotten record is not counted as visible'
    assert stats['forgotten_records'] == 1 and stats['total'] == stats['archive_records'] - 1
    assert stats['db_size_bytes'] > 0 and 'vectors_pending' in stats
    remote = ClientService(shared.client('mac-a')).compat('memory_stats', {})
    assert remote['by_kind'] == stats['by_kind'] and remote['total'] == stats['total']


def test_graph_wrappers_keep_old_shapes_and_answer_analytics(shared):
    from kelmo.mcp import compat
    service = shared.world.service
    ambiguous = compat(service, 'graph_query', {'action': 'search', 'query': 'Shared feature'})
    assert ambiguous['error'] == 'ambiguous_entity'
    found = compat(service, 'graph_query', {'action': 'search', 'query': 'projects/app/shared-feature'})
    assert isinstance(found, list) and found
    stats = compat(service, 'graph_stats', {})
    assert 'graph' in stats and isinstance(stats['graph'], dict)
    for arguments in ({'action': 'communities'}, {'action': 'god_nodes'}, {'action': 'surprises'},
                      {'action': 'search', 'query': 'shared', 'exclude_god_nodes': True},
                      {'action': 'communities', 'community_id': 0}):
        assert isinstance(compat(service, 'graph_query', arguments), list), arguments
    groups = compat(service, 'graph_query', {'action': 'communities'})
    assert groups and all('members' in group for group in groups)


def test_shared_reconciliation_issues_the_same_receipt_as_local(shared):
    from kelmo.client import ClientService
    remote = ClientService(shared.client('mac-a'))
    args = ('Exports use an isolated staging directory.', 'projects/app/shared-feature.md')
    receipt = remote.reconcile(*args)
    assert receipt == shared.world.service.reconcile(*args)
    assert receipt['search_id'] and receipt['method'] == 'lexical'
    assert remote.client.outbox.pending() == []
    local = shared.world.service
    index, page = local.page_index(), local.page_facts(args[1])
    context = local.editor_context('app', 60000, [args[1]])
    local.config.registry.write_text('{unpublished registry edit}')
    assert remote.reconcile(*args) == receipt
    assert local.page_index() == index
    assert local.page_facts(args[1]) == page
    assert local.editor_context('app', 60000, [args[1]]) == context


def test_shared_create_records_original_text_and_scoped_request(shared):
    from kelmo.client import ClientService
    client = shared.client('mac-a')
    result = ClientService(client).create('Keep the exact source.', project='app',
                                          selected=['projects/app/shared-feature.md'])
    operation = result['operation']
    assert operation['intent'] == 'create' and operation['state'] == 'queued'
    assert shared.world.service.source_packet(operation['id']).content == b'Keep the exact source.'
    assert shared.world.service.journal.origin_record(operation['id'])['client_id'] == 'mac-a'
    assert client.outbox.pending() == []


def test_native_mcp_client_metadata_does_not_break_shared_search(shared):
    from kelmo.client import ClientService
    remote = ClientService(shared.client('mac-a'))
    result = remote.search('No matching record.', with_metadata=True, client='mcp-desktop')
    assert isinstance(result['items'], list)
    assert 'client' not in shared.hub.requests[-1][1]['args']


def test_capture_timing_matches_local_add(shared):
    """Capture is queued in both modes; shared wait observes the worker without competing with it."""
    import fcntl
    from kelmo.client import ClientService
    from kelmo.config import Config
    remote = ClientService(shared.client('mac-a'))
    local = shared.world.service.capture('Local queued capture.', 'local-queued.md', 'implementer',
                                         idempotency_key='local-queued')
    # Service.capture's shape: the operation's own fields, as the local MCP tool returns them.
    shared_only = remote.capture('Shared queued capture.', 'shared-queued.md', role='implementer',
                                 idempotency_key='shared-queued')
    assert set(shared_only) == set(json.loads(json.dumps(local)))
    assert shared_only['state'] == local['state'] == 'queued'
    assert shared_only['durable_stage'] == local['durable_stage']

    with Config.load(shared.world.root).internal('.state/worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = remote.add('Shared busy capture.', 'shared-busy.md', role='implementer',
                          idempotency_key='shared-busy', wait=True, wait_timeout=0)
    assert busy['processing_deferred'] is True and busy['operation']['state'] == 'queued'

    done = remote.add('Shared waited capture.', 'shared-waited.md', role='implementer',
                      idempotency_key='shared-waited')
    shared.hub.session('mac-a').service.run(done['operation']['id'])
    done = remote.wait(done)
    assert done['operation']['durable_stage'] == 'archived' and done['evidence_ref']['version'] == 1
    collected = remote.add('Collected only.', 'collected.md', role='collector', idempotency_key='collected',
                           wait=True)
    assert collected['processing_scheduled'] is False and collected['operation']['state'] == 'captured'


def test_tool_cli_entry_point(shared, monkeypatch, capsys):
    from kelmo import cli
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'content': 'Stored by the old CLI shape.',
                                                             'kind': 'event', 'domain': 'dev'})))
    assert cli.call(['tool', 'memory_store', '--vault', str(shared.world.root), '--json']) == 0
    stored = json.loads(capsys.readouterr().out)['result']['result']
    assert shared.world.service.show(stored['id'])['content'] == 'Stored by the old CLI shape.'




def test_local_agent_instance_is_isolated(shared, tmp_path):
    """A second local authority is neither searchable from nor reachable by the shared one."""
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    agent_root = tmp_path / 'agent'
    Service.initialize(agent_root, 'agent-node')
    with Service(agent_root) as agent:
        operation = agent.capture('Private local instance fact.', 'agent.md', 'implementer', idempotency_key='i')
        agent.run(operation['id'])
        assert agent.config.vault_id != shared.world.bindings.authority_id
    remote = ClientService(shared.client('mac-a'))
    assert remote.search('Private local instance', with_metadata=True)['items'] == []
    with Service(agent_root) as agent:
        assert agent.search('Private local instance', with_metadata=True)['items']
    from kelmo.server import ClientSession
    with pytest.raises(KelmoError) as error:
        ClientSession(agent_root, shared.hub.profile, 'mac-a')
    assert error.value.code == 'E_ORIGIN'


def test_snapshot_graph_is_bound_to_the_committed_generation(shared):
    from kelmo.client import ClientService
    service = ClientService(shared.client('mac-a'))
    stats = service.graph_stats()
    assert stats['graph']['status'] in ('absent', 'stale')
    rebuilt = service.graph_rebuild()
    generation = shared.world.service.knowledge.generation().to_dict()
    assert rebuilt['graph']['status'] == 'current' and rebuilt['graph']['generation'] == generation
    found = service.graph_query(action='search', query='Shared feature')
    assert found['graph']['generation'] == generation
    read = service.graph_read(APP_PAGE[:-3])
    assert read['file_path'] == APP_PAGE
    assert read['content'] == shared.world.service.knowledge.page(
        APP_PAGE, shared.world.service.knowledge.generation()).content.decode('utf-8')

    receipt = publish_push(shared.world, 'graph-generation')
    assert service.graph_stats()['graph']['status'] == 'stale'
    refreshed = service.graph_query(action='search', query='Shared feature')
    assert refreshed['graph']['status'] == 'current'
    assert refreshed['graph']['generation'] == receipt.generation.to_dict()


def test_unrepresentable_legacy_update_refuses_without_rewriting_history(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    from kelmo.mcp import compat
    adapter = shared.world.service.archive_api
    with adapter.connection:
        adapter.connection.execute('''INSERT INTO memories
            (id,content,kind,domain,tags_json,source,machine_tag,created_at,updated_at,valid_from)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            ('old-untyped', 'Original legacy bytes.\n', 'event', 'dev', '["kept"]', 'raw_note',
             'old-node', '2026-04-17T09:00:00+00:00', '2026-04-17T09:00:00+00:00', '2026-02-30'))
    before = compat(shared.world.service, 'memory_get', {'id': 'old-untyped', 'full': True})
    arguments = {'id': 'old-untyped', 'content': 'New typed content.', 'valid_from': '2026-04-17',
                 'expected_version': 1}
    for update in (lambda: compat(shared.world.service, 'memory_update', arguments),
                   lambda: ClientService(shared.client()).compat('memory_update', arguments)):
        with pytest.raises(KelmoError) as refused:
            update()
        assert refused.value.code == 'E_CAPABILITY'
        assert 'old-untyped' in str(refused.value) and 'version 1' in str(refused.value)
    assert compat(shared.world.service, 'memory_get', {'id': 'old-untyped', 'full': True}) == before
    assert adapter.connection.execute(
        "SELECT COUNT(*) FROM memory_versions WHERE memory_id='old-untyped'").fetchone()[0] == 0
    assert adapter.connection.execute(
        "SELECT COUNT(*) FROM archive_version_sources WHERE archive_id='old-untyped'").fetchone()[0] == 0
