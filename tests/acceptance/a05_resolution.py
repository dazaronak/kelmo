#!/usr/bin/env python3
"""A05. A concurrent change becomes a resolution job that an agent settles."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario

TWO = 'The demo worker polls every two seconds.'
FIVE = 'The demo worker polls every five seconds.'
CHANGED = 'The demo worker polls every two seconds (changed from five).'


def page(v):
    return (v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8')


def body(s):
    v = s.fresh_vault()
    payload, _ = s.promote_new_fact(v, 'demo.poll', TWO, 'n1.md', wait=False)
    op1 = payload['result']['operation']
    s.check(op1['state'] == 'queued', 'Step 1 returns operation.state = "queued".')
    payload, _ = s.promote_new_fact(v, 'demo.poll', FIVE, 'n2.md')
    s.check(payload['result']['operation']['state'] == 'applied', 'Step 2 returns operation.state = "applied".')

    run = s.result('worker', 'run', '--vault', v)
    ops = {op['id']: op for op in run['operations']}
    first = ops.get(op1['id'], {})
    s.check(first.get('state') == 'needs_resolution' and first.get('archive_id') and first.get('error_code') == 'E_FACT_CHECK',
            'Step 3 returns N1\'s operation with state = "needs_resolution", a non-null archive_id, and error_code = "E_FACT_CHECK".')
    s.check(FIVE in page(v), 'Step 3: The page still shows the five-second text.')

    jobs = s.result('resolution', 'list', '--vault', v)['jobs']
    s.check(len(jobs) == 1 and jobs[0]['state'] == 'pending' and jobs[0]['operation_id'] == op1['id'],
            'Step 4: list returns one job with state = "pending" and operation_id = OP1.')
    claim = s.result('resolution', 'claim', '--vault', v)['job']
    payload_ = claim.get('payload', {})
    fact = payload_.get('facts', {}).get('demo.poll', {})
    s.check(claim.get('token') and payload_.get('task') == 'resolve_facts'
            and payload_.get('changes', [{}])[0].get('text') == TWO
            and fact.get('text') == FIVE and fact.get('revision')
            and payload_.get('archive_id') == first.get('archive_id'),
            'Step 4: claim returns the job with a token and a payload whose task = "resolve_facts", whose changes are N1\'s changes, whose facts["demo.poll"].text is the five-second text with its revision, and whose archive_id equals N1\'s archive_id.')

    proposal = {'changes': [{'fact_key': 'demo.poll', 'target': 'projects/demo/demo.md', 'text': CHANGED,
                             'status': 'active', 'expected_revision': fact.get('revision'), 'replaces': []}]}
    payload, _ = s.memory('resolution', 'submit', claim['id'], '--token', claim['token'], '--vault', v,
                          stdin=json.dumps(proposal))
    s.check(payload and payload.get('ok') and payload['result']['operation']['state'] == 'applied',
            'Step 5 returns operation.state = "applied".')

    trace = s.result('trace', op1['id'], '--vault', v)
    stages = [event['stage'] for event in trace['events']]
    s.check(stages.count('archived') == 1 and stages.index('archived') < stages.index('needs_resolution') < stages.index('applied'),
            'Step 6: the trace contains archived before needs_resolution before applied, and exactly one archived stage.')
    s.check(CHANGED in page(v) and ('- ' + FIVE) not in page(v),
            'Step 6: the page contains the "(changed from five)" text and no longer the plain five-second text.')

    payload, _ = s.memory('resolution', 'claim', '--vault', v)
    s.check(payload and ((payload.get('ok') is False and str(payload.get('error', {}).get('code', '')).startswith('E_'))
                         or (payload.get('ok') is True and not payload.get('result', {}).get('job'))),
            'Step 7 returns no claimable job (ok = false with an E_ code, or ok = true with a null job).')


if __name__ == '__main__':
    sys.exit(Scenario('A05', 'A concurrent change becomes a resolution job that an agent settles').run(body))
