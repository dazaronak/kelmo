"""The HTTP MCP endpoint kelmod serves over the real state core: the 15 tools, the bearer, Host and Origin
refusals, sessions that come and go, 50 concurrent sessions, the stdio bridge and the file modes.
Every call goes through the product's own tool handlers on a synthetic vault."""
import asyncio
import stat
from urllib.parse import urlsplit

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError

from daemon_harness import KELMOD

TOOLS = {
    'memory_create', 'memory_store', 'memory_update', 'memory_apply_action', 'memory_search',
    'memory_get', 'memory_history', 'memory_list', 'memory_stats', 'memory_status', 'memory_trace',
    'memory_doctor', 'graph_query', 'graph_read', 'graph_stats',
}
TEXT = 'Synthetic gateway probe ledger for the HTTP endpoint test.'
QUERY_EN = 'gateway probe ledger'
QUERY_RU = 'шлюз зонд журнал'
INITIALIZE = {
    'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
               'clientInfo': {'name': 'gateway-test', 'version': '0'}},
}
HEADERS = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}


def transport(url, token):
    return StreamableHttpTransport(url, headers={'Authorization': f'Bearer {token}'})


def post(url, headers):
    return httpx.post(url, json=INITIALIZE, headers={**HEADERS, **headers}, timeout=10)


@pytest.mark.asyncio
async def test_tools_store_search_and_update_through_the_endpoint(gateway):
    url, token, _ = gateway
    async with Client(transport(url, token)) as client:
        tools = await client.list_tools()
        assert len(tools) == 15 and {tool.name for tool in tools} == TOOLS

        stored = (await client.call_tool('memory_store', {
            'content': TEXT, 'kind': 'event', 'domain': 'dev', 'project': 'gateway-probe',
            'tags': ['gateway-test']})).data
        found = (await client.call_tool(
            'memory_search', {'query_en': QUERY_EN, 'query_ru': QUERY_RU})).data
        hits = [item for item in found['items'] if item['id'] == stored['id']]
        assert hits and all(TEXT in item['content'] for item in hits)

        with pytest.raises(ToolError, match=r'[Ee]xpected version'):
            await client.call_tool('memory_update', {
                'id': stored['id'], 'content': TEXT + ' Changed.', 'expected_version': 2})
        updated = (await client.call_tool('memory_update', {
            'id': stored['id'], 'content': TEXT + ' Changed.', 'expected_version': 1})).data
        assert updated['version'] == 2


def test_missing_or_wrong_token_host_and_origin_are_refused(gateway):
    url, token, _ = gateway
    port = urlsplit(url).port
    good = {'Authorization': f'Bearer {token}'}
    assert post(url, good).status_code == 200, 'control: a good request answers'
    assert post(url, {}).status_code == 401
    wrong = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert post(url, {'Authorization': f'Bearer {wrong}'}).status_code == 401
    refused_host = post(url, {'Authorization': f'Bearer {token}', 'Host': f'evil.example:{port}'})
    assert refused_host.status_code >= 400
    refused_origin = post(url, {'Authorization': f'Bearer {token}', 'Origin': 'http://evil.example'})
    assert refused_origin.status_code >= 400


@pytest.mark.asyncio
async def test_a_dropped_session_initializes_again(gateway):
    url, token, _ = gateway
    async with Client(transport(url, token)) as client:
        assert isinstance((await client.call_tool('memory_status')).data, dict)
    async with Client(transport(url, token)) as client:
        assert isinstance((await client.call_tool('memory_status')).data, dict)


@pytest.mark.asyncio
async def test_fifty_concurrent_sessions_all_answer_memory_status(gateway):
    url, token, _ = gateway

    async def one_session():
        async with Client(transport(url, token)) as client:
            return (await client.call_tool('memory_status')).data

    results = await asyncio.gather(*(one_session() for _ in range(50)), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    assert not failures, failures[:3]
    assert len(results) == 50 and all(isinstance(result, dict) for result in results)


@pytest.mark.asyncio
async def test_mcp_stdio_lists_tools_and_answers_memory_status(gateway):
    _, _, runtime = gateway
    bridge = StdioTransport(command=str(KELMOD), args=['mcp-stdio', '--runtime-dir', str(runtime)])
    async with Client(bridge) as client:
        tools = await client.list_tools()
        assert len(tools) == 15 and {tool.name for tool in tools} == TOOLS
        assert isinstance((await client.call_tool('memory_status')).data, dict)


def test_endpoint_and_token_files_are_owner_only(gateway, vault_root):
    _, _, runtime = gateway
    for path in (runtime / 'endpoint.json', vault_root / '.state' / 'http-token'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
