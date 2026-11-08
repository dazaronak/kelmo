#!/usr/bin/env python3
"""A02. Restart and relocation keep everything."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import McpServer, Scenario

ESPRESSO = 'The espresso machine in the office kitchen needs descaling every two months.'


def seed(s, v):
    """At least 3 archived records (one of them the espresso note) and 1 applied fact."""
    for text in (ESPRESSO, 'Roof tiles are inspected after every storm.', 'The ferry runs hourly in winter.'):
        payload, _ = s.memory('add', '--vault', v, '--file', '-', '--role', 'implementer', '--wait', stdin=text + '\n')
        if not payload or not payload.get('ok'):
            raise RuntimeError('seed capture failed')
    payload, _ = s.promote_new_fact(v, 'demo.archive', 'The demo project stores its archive in SQLite.', 'seed.md')
    if not payload or payload['result']['operation']['state'] != 'applied':
        raise RuntimeError('seed promotion failed')


def body(s):
    v = s.fresh_vault()
    seed(s, v)
    before = s.result('status', '--vault', v)

    server = McpServer(s, v)
    ok, stored = server.call('memory_store', content='Restart probe.', kind='event', domain='dev', project='demo')
    if not ok:
        raise RuntimeError('memory_store failed: ' + str(stored))
    op_id, record_id = stored['operation_id'], stored['id']
    server.kill()

    server = McpServer(s, v)
    ok, trace = server.call('memory_trace', operation_id=op_id)
    stages = [event['stage'] for event in trace.get('events', [])] if ok else []
    s.check(ok and 'captured' in stages and 'archived' in stages,
            'Step 2: memory_trace returns the operation with stages captured and archived present.')
    ok, status = server.call('memory_status')
    s.check(ok and status['archive_records'] == before['archive_records'] + 1 and status['facts'] == before['facts']
            and sum(status['operations'].values()) == sum(before['operations'].values()) + 1,
            'Step 2: memory_status returns archive_records one higher than before the kill, the same facts, and one more operation.')
    ok, shown = server.call('memory_get', id=record_id, full=True)
    s.check(ok and shown.get('content', '').strip() == 'Restart probe.', 'Step 2: memory_get returns the stored probe in full.')
    server.stop()

    v2 = s.root / 'v2'
    shutil.copytree(v, v2, symlinks=True)
    doctor = s.result('doctor', '--vault', v2)
    s.check(doctor['healthy'] is True, 'Step 3: doctor returns healthy = true.')
    status_v = s.result('status', '--vault', v)
    status_v2 = s.result('status', '--vault', v2)
    s.check(status_v2 == status_v, 'Step 3: status on V2 equals status on V field by field.')
    hit = s.result('search', '--vault', v2, '--limit', '3', 'descaling')
    s.check(any(ESPRESSO in item['content'] for item in hit['items']), 'Step 3: the search finds the espresso record.')
    payload, _ = s.memory('graph', 'stats', '--vault', v2)
    s.check(payload and payload.get('ok') is True, 'Step 3: graph stats returns ok = true.')

    payload, _ = s.memory('worker', 'run', '--vault', v2)
    s.check(payload and payload.get('ok') is True, 'Step 4: the worker run returns ok = true.')
    trace_v2 = s.result('trace', op_id, '--vault', v2)
    s.check(trace_v2['operation']['state'] == 'archived', 'Step 4: the restart probe stays archived in V2.')
    s.check(s.result('status', '--vault', v) == status_v, 'Step 4: kelmo status --vault V is unchanged (the two vaults are independent).')


if __name__ == '__main__':
    sys.exit(Scenario('A02', 'Restart and relocation keep everything').run(body))
