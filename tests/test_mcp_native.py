"""The native MCP surface carries every archive capability, locally and through a
shared client whose graph and archive answer from the server's restricted archive."""
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_shared_context import shared as shared, world_factory as world_factory  # noqa: F401

pytest_plugins = ['test_fact_checks']


async def call(client, tool, **arguments):
    return (await client.call_tool(tool, arguments)).data


@pytest.mark.asyncio
async def test_search_requires_both_languages_and_answers_chunks(service):
    from kelmo.mcp import server
    service.run(service.capture('Synthetic basalt quarry ledger.', 'basalt.md', 'implementer')['id'])
    async with Client(server(service.root)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools['memory_search'].inputSchema['required']) == {'query_en', 'query_ru'}
        with pytest.raises(ToolError):
            await client.call_tool('memory_search', {'query': 'basalt'})
        found = await call(client, 'memory_search', query_en='basalt quarry', query_ru='базальтовый карьер')
    assert found['search']['mode'] in ('hybrid', 'lexical') and 'semantic' in found['search']
    assert [item['content'] for item in found['items']] == ['Synthetic basalt quarry ledger.']
    assert 'search_mode' not in found['items'][0], 'how the search ranked is reported once, not per item'


@pytest.mark.asyncio
async def test_versions_lists_and_stats_through_native_tools(service):
    from kelmo.mcp import server
    async with Client(server(service.root)) as client:
        stored = await call(client, 'memory_store', content='Kettle size is figure one.', kind='rule',
                            domain='life', project='kitchen', tags=['archive-only'], valid_from='2026-09-17')
        updated = await call(client, 'memory_update', id=stored['id'], content='Kettle size is figure two.',
                             expected_version=1)
        assert updated['version'] == 2 and updated['unchanged'] is False
        with pytest.raises(ToolError, match='[Ee]xpected version'):
            await client.call_tool('memory_update', {'id': stored['id'], 'content': 'Stale.', 'expected_version': 1})
        history = await call(client, 'memory_history', id=stored['id'])
        assert [item['content'] for item in history['versions']] == ['Kettle size is figure one.']
        old = await call(client, 'memory_get', id=stored['id'], version=1)
        assert old['content'] == 'Kettle size is figure one.' and old['valid_from'] == '2026-09-17'
        full = await call(client, 'memory_get', id=stored['id'], full=True)
        assert full['content'] == 'Kettle size is figure two.' and [item['version'] for item in full['versions']] == [1]

        tagged = await call(client, 'memory_list', tags=['archive-only'])
        assert [item['id'] for item in tagged['items']] == [stored['id']]
        found = await call(client, 'memory_search', query_en='kettle size', query_ru='размер чайника',
                           tags=['archive-only'], domain='life')
        assert {item['id'] for item in found['items']} == {stored['id']}
        assert all(item['version'] == 2 for item in found['items']), 'search sees only the current version'

        stats = await call(client, 'memory_stats')
        assert stats['by_kind'] == {'rule': 1} and stats['by_project'] == {'kitchen': 1}
        assert stats['unavailable_fields'] == []

        assert 'memory_forget' not in {tool.name for tool in await client.list_tools()}, 'forgetting is CLI-only'
        assert service.forget(stored['id'], reason='asked to forget')['forgotten'] is True
        assert (await call(client, 'memory_list', tags=['archive-only']))['items'] == []
        again = await call(client, 'memory_search', query_en='kettle size', query_ru='размер чайника')
        assert all(item['id'] != stored['id'] for item in again['items'])
        assert (await call(client, 'memory_stats'))['forgotten_records'] == 1


def native(root, service):
    """The native tools' own functions, called in this thread with their declared defaults. An in-process
    hub's sessions belong to the thread that opened them, so the server's worker threads stay out of it."""
    import asyncio
    from kelmo.mcp import server
    mcp = server(root, service)

    def invoke(name, **arguments):
        return asyncio.run(mcp.get_tool(name)).fn(**arguments)
    return invoke


def test_shared_client_answers_graph_actions_with_every_default_option(shared):
    """The native tool sends every graph option; the restricted archive path validates them as the local
    graph does and answers navigation and topology analytics alike."""
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    tool = native(shared.world.root, ClientService(shared.client('mac-a')))
    found = tool('graph_query', action='search', query='projects/app/shared-feature')
    assert found['result'] and found['graph']['status'] == 'current'
    groups = tool('graph_query', action='communities')
    assert groups['result'] and groups['analysis']['verified'] is False
    assert groups['analysis']['snapshot'] == groups['graph']['source_digest']
    assert groups['analysis']['assignment_sha256']
    pinned = tool('graph_query', action='god_nodes', assignment=groups['analysis']['assignment_sha256'])
    assert pinned['analysis']['assignment_sha256'] == groups['analysis']['assignment_sha256']
    with pytest.raises(KelmoError, match='no longer current'):
        tool('graph_query', action='communities', snapshot='0' * 64)
    stats = tool('graph_stats')
    assert stats['graph']['status'] == 'current' and stats['analysis']['communities'] >= 1
    assert tool('graph_read', entity_id='projects/app/shared-feature')['content']


def test_shared_client_reaches_every_archive_tool(shared):
    from kelmo.client import ClientService
    remote = ClientService(shared.client('mac-a'))
    tool = native(shared.world.root, remote)
    stored = tool('memory_store', content='Shared rule, first wording.', kind='rule', domain='dev', project='app')
    assert tool('memory_update', id=stored['id'], content='Shared rule, second wording.',
                expected_version=1)['version'] == 2
    history = tool('memory_history', id=stored['id'])
    assert [item['content'] for item in history['versions']] == ['Shared rule, first wording.']
    found = tool('memory_search', query_en='shared rule wording', query_ru='общее правило')
    assert stored['id'] in {item['id'] for item in found['items']}
    assert stored['id'] in {item['id'] for item in tool('memory_list', project='app', kind='rule')['items']}
    stats = tool('memory_stats')
    assert stats['by_kind'].get('rule', 0) >= 1 and stats['unavailable_fields'] == []
    assert set(tool('memory_doctor')) >= {'healthy', 'checks', 'warnings', 'outbox'}
    assert remote.forget(stored['id'])['forgotten'] is True
    assert stored['id'] not in {item['id'] for item in tool('memory_list', project='app')['items']}
    assert remote.client.outbox.pending() == [], 'every write was delivered'
