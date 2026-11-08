import json

import pytest

pytest_plugins = ['test_fact_checks']


def archive(service):
    operation = service.capture('Synthetic basalt evidence.', 'evidence.md', 'implementer')
    service.run(operation['id'])


def test_cli_reports_degraded_search_with_existing_match(service, capsys):
    from kelmo.cli import main
    archive(service)
    code = main(['search', 'basalt', '--vault', str(service.root), '--json'])
    assert code == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert len(result['items']) == 1
    assert result['search'] == {'mode': 'lexical', 'degraded': True, 'reason': 'embedding_unavailable'}
    assert 'distance' not in result['items'][0]


@pytest.mark.asyncio
async def test_mcp_exposes_degradation_even_without_results(service):
    from fastmcp import Client
    from kelmo.mcp import server
    async with Client(server(service.root)) as client:
        result = await client.call_tool('memory_search', {'query_en': 'absent', 'query_ru': 'отсутствует'})
    assert result.data['items'] == []
    assert result.data['search']['degraded'] is True
    assert result.data['search']['mode'] == 'lexical'


def test_sessions_review_cli_does_not_initialize_missing_session_state(service, capsys):
    from kelmo.cli import main
    code = main(['sessions', 'review', '--vault', str(service.root), '--json'])
    assert code == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert result['status'] == 'disabled'
    assert result['preservation_ready'] is False
    assert result['semantic_completeness_guaranteed'] is False
