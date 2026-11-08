"""Exercise selected-wiki navigation through public CLI and MCP transports."""
import json
import subprocess
import sys

import pytest

from kelmo.service import Service


@pytest.fixture
def graph_vault(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.gitconfig').write_text('[user]\nname = Fixture\nemail = fixture@example.invalid\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(home / '.gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture')
    wiki = root / 'knowledge/wiki'
    (wiki / 'guide.md').write_text('# Guide\nRead [[detail]].\n')
    (wiki / 'detail.md').write_text('# Detail\nKeep this exception.\n')
    return root


def cli(root, *args):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', '--vault', str(root),
                           '--json', 'graph', *args], capture_output=True, text=True,
                          timeout=20, check=False)


def test_cli_navigates_and_reads_the_current_selected_page(graph_vault):
    query = cli(graph_vault, 'query', 'neighbors', '--id', 'guide')
    assert query.returncode == 0, query.stdout + query.stderr
    found = json.loads(query.stdout)['result']
    assert found['graph']['status'] == 'current'
    assert [node['id'] for node in found['result']] == ['detail']
    page = cli(graph_vault, 'read', 'detail')
    assert page.returncode == 0, page.stdout + page.stderr
    assert json.loads(page.stdout)['result']['content'] == '# Detail\nKeep this exception.\n'
    (graph_vault / 'knowledge/wiki/detail.md').unlink()
    rebuilt = cli(graph_vault, 'rebuild')
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    missing = cli(graph_vault, 'read', 'detail')
    assert missing.returncode != 0
    assert json.loads(missing.stdout)['ok'] is False


def test_cli_stats_does_not_build_and_bad_arguments_do_not_build(graph_vault):
    stats = cli(graph_vault, 'stats')
    assert stats.returncode == 0, stats.stdout + stats.stderr
    assert json.loads(stats.stdout)['result']['graph']['status'] == 'absent'
    invalid = cli(graph_vault, 'query', 'neighbors', '--id', 'guide', '--radius', '3')
    assert invalid.returncode != 0
    assert json.loads(invalid.stdout)['error']['code'] == 'E_INPUT'
    assert json.loads(cli(graph_vault, 'stats').stdout)['result']['graph']['status'] == 'absent'


@pytest.mark.asyncio
async def test_mcp_discovers_graph_and_reads_fresh_content(graph_vault):
    from fastmcp import Client
    from kelmo.mcp import server

    async with Client(server(graph_vault)) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {'graph_query', 'graph_stats', 'graph_read'} <= names
        assert not names & {'graph_rebuild', 'memory_capture'}
        found = await client.call_tool('graph_query', {'action': 'search', 'query': 'Guide'})
        assert [node['id'] for node in found.data['result']] == ['guide']
        (graph_vault / 'knowledge/wiki/detail.md').write_text('# Detail\nChanged condition.\n')
        page = await client.call_tool('graph_read', {'entity_id': 'detail'})
        assert page.data['content'] == '# Detail\nChanged condition.\n'
        assert page.data['graph']['status'] == 'current'


def test_doctor_reports_graph_state_without_building(graph_vault):
    from kelmo.doctor import diagnose

    assert diagnose(graph_vault)['graph']['status'] == 'absent'
    with Service(graph_vault) as service:
        service.graph_rebuild()
    assert diagnose(graph_vault)['graph']['status'] == 'current'
    (graph_vault / 'knowledge/wiki/guide.md').write_text('# Changed\n')
    assert diagnose(graph_vault)['graph']['status'] == 'stale'
