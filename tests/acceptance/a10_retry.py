#!/usr/bin/env python3
"""A10. Failure after archive is retried without duplicates."""
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario, git

FIRST = 'The demo project stores its archive in SQLite.'
TEXT = 'The demo CLI prints JSON envelopes.'


def body(s):
    v = s.fresh_vault()
    payload, _ = s.promote_new_fact(v, 'demo.archive', FIRST, 'first.md')
    if payload['result']['operation']['state'] != 'applied':
        raise RuntimeError('precondition fact not applied')

    a = s.result('status', '--vault', v)['archive_records']
    c = len(git(v, 'log', '--format=%H').splitlines())
    folder = v / 'knowledge/wiki/projects/demo'
    os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    try:
        payload, _ = s.promote_new_fact(v, 'demo.cli', TEXT, 'note.md')
    finally:
        pass
    op = (payload or {}).get('result', {}).get('operation', {})
    s.check(payload and payload.get('ok') is False and op.get('state') == 'failed' and op.get('archive_id')
            and op.get('durable_stage') == 'archived',
            'Step 3 returns ok = false, result.operation.state = "failed", result.operation.archive_id non-null, result.operation.durable_stage = "archived".')
    archive_id = op.get('archive_id')

    trace = s.result('trace', op['id'], '--vault', v)
    last = trace['events'][-1]
    s.check(last['stage'] == 'failed' and last.get('details', {}).get('phase') == 'editor',
            'Step 4: the trace ends with a failed stage whose details name phase = "editor".')
    s.check(s.result('status', '--vault', v)['archive_records'] == a + 1, 'Step 4: status reports archive_records = A + 1.')
    payload, _ = s.memory('doctor', '--vault', v)
    s.check(payload and payload.get('ok') is True, 'Step 4: doctor returns ok = true (a failed operation is not corruption).')
    porcelain = git(v, 'status', '--porcelain')
    s.check(not any(line.startswith((' M', 'M ', 'MM')) and 'knowledge/wiki/' in line for line in porcelain.splitlines()),
            'Step 4: the Git working tree has no modified tracked wiki file.')

    os.chmod(folder, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    retried = s.result('queue', 'retry', op['id'], '--vault', v)['operation']
    s.check(retried['state'] == 'queued' and retried['archive_id'] == archive_id,
            'Step 5: retry returns the operation with state = "queued" and the same archive_id.')
    run = s.result('worker', 'run', '--vault', v)
    ops = {o['id']: o for o in run['operations']}
    s.check(ops.get(op['id'], {}).get('state') == 'applied', 'Step 5: worker run returns it with state = "applied".')

    trace = s.result('trace', op['id'], '--vault', v)
    stages = [e['stage'] for e in trace['events']]
    carried = {e['details'].get('archive_id') for e in trace['events'] if e.get('details', {}).get('archive_id')}
    s.check(stages.index('archived') < stages.index('failed') and carried == {archive_id},
            'Step 6: the trace contains an archived stage before failed, and every stage that carries an archive_id carries the same one.')
    s.check(s.result('status', '--vault', v)['archive_records'] == a + 1, 'Step 6: archive_records = A + 1 (no second record).')
    s.check(len(git(v, 'log', '--format=%H').splitlines()) == c + 1, 'Step 6: the Git log has exactly C + 1 commits.')
    page = (v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8')
    s.check(page.count('- ' + TEXT + ' [f:') == 1, 'Step 6: the page contains the new fact line once.')


if __name__ == '__main__':
    sys.exit(Scenario('A10', 'Failure after archive is retried without duplicates').run(body))
