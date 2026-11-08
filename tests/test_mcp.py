import pytest

pytest_plugins = ['test_fact_checks']


@pytest.mark.asyncio
async def test_mcp_keeps_capture_show_reconcile_and_rebuild_as_cli_verbs(service):
    from fastmcp import Client
    from kelmo.mcp import server
    async with Client(server(service.root)) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert not names & {'memory_capture', 'memory_show', 'memory_reconcile', 'graph_rebuild'}
        with pytest.raises(Exception):
            await client.call_tool('memory_capture', {'content': 'Synthetic.', 'filename': 'note.md'})
        assert service.capture('Synthetic.', 'note.md')['state'] == 'captured'


@pytest.mark.asyncio
async def test_mcp_search_works_with_telemetry_and_exposes_no_log_or_admin_tool(service):
    from fastmcp import Client
    from kelmo.mcp import server
    path = service.root / 'kelmo.toml'
    path.write_text(path.read_text() + '\n[telemetry]\nsearch_log = "redacted"\n')
    service.run(service.capture('Synthetic telemetry basalt.', 'basalt.md', 'implementer')['id'])
    async with Client(server(service.root)) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == {'memory_create', 'memory_store', 'memory_update', 'memory_apply_action', 'memory_search', 'memory_get', 'memory_history', 'memory_list', 'memory_stats', 'memory_status', 'memory_trace', 'memory_doctor', 'graph_query', 'graph_read', 'graph_stats'}
        result = await client.call_tool('memory_search', {'query_en': 'basalt', 'query_ru': 'базальт'})
        assert set(result.data) == {'items', 'search'}
    from kelmo.store.memory.search_log import read_recent
    logged = read_recent(service.config.archive)
    assert len(logged) == 1 and logged[0]['privacy'] == 'redacted' and logged[0]['query_en'] == ''

