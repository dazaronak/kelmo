#!/usr/bin/env python3
"""A09. The MCP contract."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import McpServer, Scenario
from a06_graph import OTHER, SETUP

TOOLS = [
    'memory_create', 'memory_store', 'memory_update',
    'memory_apply_action', 'memory_search', 'memory_get', 'memory_history',
    'memory_list', 'memory_stats', 'memory_status',
    'memory_trace', 'memory_doctor',
    'graph_query', 'graph_read', 'graph_stats',
]
TEXT = 'The demo project stores its archive in SQLite.'


def body(s):
    v = s.fresh_vault()
    payload, _ = s.promote_new_fact(v, 'demo.archive', TEXT, 'note.md')
    op = payload['result']['operation']
    if op['state'] != 'applied':
        raise RuntimeError('precondition fact not applied')
    wiki = v / 'knowledge/wiki'  # the two A06 setup pages; demo.md is the page the promotion rendered
    s.write(wiki / 'projects/demo/setup.md', SETUP)
    s.write(wiki / 'projects/other/setup.md', OTHER)

    server = McpServer(s, v)
    try:
        names = server.list_tools()
        s.check(sorted(names) == sorted(TOOLS) and len(names) == len(TOOLS),
                'Step 1 returns exactly the native tool names in TOOLS and no others.')

        ok, hit = server.call('memory_search', query_en='archive', query_ru='архив', limit=5)
        s.check(ok and 'items' in hit and all(k in hit.get('search', {}) for k in ('mode', 'degraded', 'reason')),
                'Step 2: the first call returns an object with items and search (mode, degraded, reason).')
        s.check(ok and any(item.get('id') == op['archive_id'] for item in hit.get('items', [])),
                'Step 2: the first call finds the seeded record among its items.')
        ok, _ = server.call('memory_search', query_en='archive', query_ru='архив', limit=0)
        s.check(not ok, 'Step 2: the second call is a tool error (limit must be between 1 and 100).')

        ok, stored = server.call('memory_store', content='Via MCP.', kind='event', domain='dev', project='demo')
        s.check(ok and stored.get('version') == 1 and stored.get('id'), 'Step 3: the first call returns the record id and version 1.')
        ok, error = server.call('memory_capture', content='Via MCP.', filename='mcp.md')
        _, status = server.call('memory_status')
        s.check(not ok and status.get('facts') == 1,
                'Step 3: the second call is a tool error (memory_capture is not a tool of this server), and memory_status afterwards shows facts unchanged.')

        payload, _ = s.memory('reconcile', '--vault', v, '--target', 'projects/demo/demo.md', '--text', TEXT)
        receipt = (payload or {}).get('result', {}) if payload and payload.get('ok') else {}
        candidates = receipt.get('candidates', [])
        s.check(bool(payload) and payload.get('ok') is True and len(candidates) == 1
                and candidates[0].get('fact_key') == 'demo.archive' and candidates[0].get('revision') and receipt.get('search_id'),
                'Step 4 (CLI kelmo reconcile) returns one candidate with fact_key = "demo.archive" and a revision, plus a search_id.')

        ok, found = server.call('graph_query', action='search', query='setup')
        ambiguous = found.get('result', {}) if ok and isinstance(found, dict) else {}
        s.check(ok and isinstance(ambiguous, dict) and ambiguous.get('error') == 'ambiguous_entity'
                and {'projects/demo/setup', 'projects/other/setup'} <= set(ambiguous.get('candidates', [])),
                'Step 5: search reports ambiguous_entity with both setup pages as candidates.')
        ok, error = server.call('graph_read', entity_id='setup')
        s.check(not ok and 'ambig' in str(error).lower(), 'Step 5: the short-name read is a tool error naming ambiguity.')
        ok, read = server.call('graph_read', entity_id='projects/demo/setup')
        s.check(ok and read.get('content') == SETUP, 'Step 5: the full-ID read returns the file bytes.')

        ok, shown = server.call('memory_get', id=op['archive_id'], full=True)
        s.check(ok and TEXT in shown.get('content', ''), 'Step 6: the first call returns the record with its full content.')
        ok, error = server.call('memory_get', id='missing', full=True)
        s.check(not ok and 'not found' in str(error).lower(), 'Step 6: the second call is a tool error with "not found" semantics.')

        ok, trace = server.call('memory_trace', operation_id=op['id'])
        stages = [event['stage'] for event in trace.get('events', []) if event['stage'] != 'editor_result'] if ok else []
        editor_results = [event['details'] for event in trace.get('events', []) if event['stage'] == 'editor_result'] if ok else []
        s.check([result['disposition'] for result in editor_results] == ['prepared', 'applied'],
                'Step 7: the trace carries one prepared and one applied editor result for this source.')
        s.check(stages == ['captured', 'queued', 'archived', 'applied'],
                'Step 7 returns stages in the order captured, queued, archived, applied.')
    finally:
        server.stop()


if __name__ == '__main__':
    sys.exit(Scenario('A09', 'The MCP contract').run(body))
