#!/usr/bin/env python3
"""A04. A fact enters the wiki only through a checked change."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario, git

TEXT = 'The demo project stores its archive in SQLite.'


def body(s):
    v = s.fresh_vault()
    receipt = s.reconcile(v, TEXT)
    s.check(receipt['candidates'] == [] and receipt['proves_absence'] is False and receipt.get('search_id'),
            'Step 1 returns candidates = [], proves_absence = false, and a search_id.')

    commit_count_before = len(git(v, 'log', '--format=%H').splitlines())
    note = s.promotion_note(s.root / 'note.md', 'demo.archive', TEXT, receipt['search_id'])
    payload, _ = s.memory('promote', '--vault', v, '--file', note, '--role', 'lead', '--wait')
    op = payload['result']['operation']
    s.check(op['state'] == 'applied' and op['archive_id'], 'Step 2 returns operation.state = "applied", operation.archive_id non-null.')

    page = (v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8')
    managed = page.split('<!-- kb -->', 1)[1].split('<!-- /kb -->', 1)[0] if '<!-- kb -->' in page else ''
    s.check(any(line.startswith('- ' + TEXT + ' [f:') for line in managed.splitlines()),
            'Step 3: the page contains a line that starts with "- The demo project stores its archive in SQLite. [f:" inside <!-- kb --> and <!-- /kb -->.')
    registry = json.loads((v / 'knowledge/provenance/registry.json').read_text(encoding='utf-8'))
    s.check(registry['facts'].get('demo.archive', {}).get('ark_id') == op['archive_id'],
            'Step 3: the registry has a fact demo.archive whose ark_id equals operation.archive_id.')
    trace = s.result('trace', op['id'], '--vault', v)
    editor_results = [event['details'] for event in trace['events'] if event['stage'] == 'editor_result']
    request_ids = {result['request'] for result in editor_results}
    s.check([result['disposition'] for result in editor_results] == ['prepared', 'applied'] and len(request_ids) == 1,
            'Step 3: one editor request is prepared and applied for this source.')
    editor_id = next(iter(request_ids)) if len(request_ids) == 1 else ''
    log = git(v, 'log', '--format=%s')
    s.check(bool(editor_id) and sum(editor_id[:8] in line for line in log.splitlines()) == 1
            and len(log.splitlines()) == commit_count_before + 1,
            'Step 3: exactly one new Git commit names the editor request linked to this operation.')
    commits_before = len(log.splitlines())

    stages = [event['stage'] for event in trace['events'] if event['stage'] != 'editor_result']
    s.check(stages == ['captured', 'queued', 'archived', 'applied'],
            'Step 4: the stages appear in this order: captured, queued, archived, applied.')

    payload, _ = s.memory('promote', '--vault', v, '--file', note, '--role', 'lead', '--wait')
    s.check(payload and payload.get('ok') and payload['result']['operation']['id'] == op['id'],
            'Step 5 returns the same operation.id as step 2 (idempotent).')
    s.check(len(git(v, 'log', '--format=%s').splitlines()) == commits_before, 'Step 5: git log has no new commit.')

    bad = s.promotion_note(s.root / 'bad.md', 'demo.archive', TEXT, '0000')
    payload, _ = s.memory('promote', '--vault', v, '--file', bad, '--role', 'lead', '--wait')
    s.check(payload and payload.get('ok') is False and payload.get('error', {}).get('code') == 'E_FACT_CHECK',
            'Step 6 returns ok = false, error.code = "E_FACT_CHECK".')
    s.check((v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8') == page
            and json.loads((v / 'knowledge/provenance/registry.json').read_text(encoding='utf-8')) == registry,
            'Step 6: the page and the registry are unchanged.')

    payload, _ = s.memory('add', '--vault', v, '--file', note, '--role', 'collector', '--wait')
    s.check(payload and payload.get('ok') and payload['result']['operation']['state'] == 'captured',
            'Step 7 returns operation.state = "captured".')
    s.check(s.result('status', '--vault', v)['facts'] == 1, 'Step 7: kelmo status reports facts = 1.')
    s.check((v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8') == page,
            'Step 7: the page is unchanged. Collector evidence does not become a fact.')


if __name__ == '__main__':
    sys.exit(Scenario('A04', 'A fact enters the wiki only through a checked change').run(body))
