"""Graph analytics through the public CLI and MCP surfaces, and the boundaries they keep."""
import json
import subprocess
import sys

import pytest

pytest_plugins = ['test_fact_checks']

PAGES = {'a/one': '[[a/two]] [[b/four]]', 'a/two': '[[a/one]]', 'b/four': '[[b/five]]', 'b/five': '[[b/four]]'}


def write(service, pages):
    for page, body in pages.items():
        path = service.config.wiki / (page + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'# {page}\n\n{body}\n', encoding='utf-8')


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', *map(str, args)],
                          capture_output=True, text=True, timeout=60, check=False)


def test_cli_analytics_carry_snapshot_identity_and_refuse_a_stale_name(service):
    write(service, PAGES)
    first = cli('graph', 'query', 'communities', '--vault', service.root, '--json')
    assert first.returncode == 0, first.stderr
    envelope = json.loads(first.stdout)
    digest = envelope['result']['graph']['source_digest']
    assert envelope['result']['analysis'] == dict(envelope['result']['analysis'], verified=False, snapshot=digest)
    hubs = cli('graph', 'query', 'god_nodes', '--vault', service.root, '--snapshot', digest, '--json')
    assert json.loads(hubs.stdout)['ok'] is True
    write(service, {'a/two': '[[a/one]] [[b/five]]'})
    stale = cli('graph', 'query', 'surprises', '--vault', service.root, '--snapshot', digest, '--json')
    assert stale.returncode != 0
    assert json.loads(stale.stdout)['error']['code'] == 'E_GRAPH_STALE'


@pytest.mark.asyncio
async def test_mcp_offers_analytics_and_no_administrator_tool(service):
    from fastmcp import Client
    from kelmo.mcp import server
    write(service, PAGES)
    async with Client(server(service.root)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert not [name for name in tools if any(word in name for word in ('admin', 'purge', 'log', 'delete'))]
        actions = json.dumps(tools['graph_query'].inputSchema)
        assert all(action in actions for action in ('communities', 'god_nodes', 'surprises'))
        result = await client.call_tool('graph_query', {'action': 'surprises'})
        assert result.data['analysis']['verified'] is False
        assert result.data['analysis']['snapshot'] == result.data['graph']['source_digest']


def test_doctor_inspects_graph_state_without_publishing_it(service):
    from kelmo.doctor import diagnose
    write(service, PAGES)
    assert diagnose(service.root)['graph']['status'] == 'absent'
    assert service.archive.execute("SELECT 1 FROM archive_meta WHERE key='graph.snapshot'").fetchone() is None


def test_graph_operations_leave_archive_evidence_untouched(service):
    operation = service.capture('Graph adoption evidence.', 'evidence.md', 'implementer')
    archived = service.run(operation['id'])
    write(service, PAGES)
    before = service.archive.execute('SELECT id, content, version FROM memories ORDER BY id').fetchall()
    service.graph_rebuild()
    service.graph_query(action='communities')
    after = service.archive.execute('SELECT id, content, version FROM memories ORDER BY id').fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert service.show(archived['archive_id'])['content'] == 'Graph adoption evidence.'
