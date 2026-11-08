"""One MCP call for a cycle an agent otherwise repeats: create then trace, search then get, find then read."""
import asyncio

import pytest
from fastmcp import Client

from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']


class Held:
    """A service double for the tools that only pass through to it."""

    def __init__(self, settle_after=2, refuse_trace=False):
        self.traces, self.settle_after, self.refuse_trace = 0, settle_after, refuse_trace

    def create(self, content, filename, **options):
        return {'operation': {'id': 'op-1', 'state': 'queued', 'terminal': False}}

    def trace(self, operation_id):
        if self.refuse_trace:
            raise KelmoError('E_TRANSPORT', 'The channel closed before replying.')
        self.traces += 1
        done = self.traces >= self.settle_after
        stage = 'applied' if done else 'queued'
        return {'operation': {'id': operation_id, 'state': stage, 'terminal': done},
                'events': [{'stage': 'queued'}] + ([{'stage': 'applied', 'details': {'facts': 2}}] if done else [])}

    def graph_query(self, **options):
        return {'result': [{'id': 'projects/app/app'}, {'id': 'projects/app/other'}]}

    def graph_read(self, entity_id):
        return {'id': entity_id, 'content': '# App\n'}


def tool(service, name):
    from kelmo.mcp import server
    return asyncio.run(server('/unused', service).get_tool(name)).fn


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr('kelmo.mcp.WAIT_POLL_SECONDS', 0.01)


def test_create_with_wait_returns_the_final_stage_and_its_last_event():
    held = Held()
    result = tool(held, 'memory_create')(content='A fact.', project='app', wait=30)
    assert result['operation']['state'] == 'applied' and result['operation']['terminal']
    assert result['last_event'] == {'stage': 'applied', 'details': {'facts': 2}}
    plain = tool(Held(), 'memory_create')(content='A fact.', project='app')
    assert plain['operation']['state'] == 'queued' and 'last_event' not in plain


def test_create_wait_ends_at_its_deadline_or_a_lost_channel_without_undoing_the_write():
    late = tool(Held(settle_after=10 ** 6), 'memory_create')(content='A fact.', project='app', wait=0.05)
    assert late['wait_timed_out'] and 'memory_trace op-1' in late['next_step']
    lost = tool(Held(refuse_trace=True), 'memory_create')(content='A fact.', project='app', wait=30)
    assert lost['wait_error']['code'] == 'E_TRANSPORT' and lost['operation']['id'] == 'op-1'


def test_graph_search_with_read_carries_the_first_page():
    result = tool(Held(), 'graph_query')(action='search', query='app', read=True)
    assert result['page'] == {'id': 'projects/app/app', 'content': '# App\n'}
    assert 'page' not in tool(Held(), 'graph_query')(action='search', query='app')
    assert 'page' not in tool(Held(), 'graph_query')(action='neighbors', entity_id='projects/app/app', read=True)


@pytest.mark.asyncio
async def test_search_with_expand_carries_each_top_record_around_its_hit(service):
    from kelmo.mcp import server
    for index, text in enumerate(('Synthetic basalt quarry ledger.', 'Second basalt quarry report.')):
        service.run(service.capture(text, f'basalt-{index}.md', 'implementer')['id'])
    async with Client(server(service.root)) as client:
        found = (await client.call_tool('memory_search', {'query_en': 'basalt quarry', 'query_ru': 'базальт',
                                                          'expand': 1})).data
        plain = (await client.call_tool('memory_search', {'query_en': 'basalt quarry',
                                                          'query_ru': 'базальт'})).data
    assert 'expanded' not in plain
    assert len(found['expanded']) == 1
    top = found['expanded'][0]
    assert top['id'] == found['items'][0]['id'] and 'basalt' in str(top['context'])
