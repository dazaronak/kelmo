#!/usr/bin/env python3
"""A08. A conversation becomes a whole note and a checked fact."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario

LINES = [
    '{"type":"user","message":{"role":"user","content":"How do we store the archive?"}}',
    '{"type":"assistant","message":{"role":"assistant","content":"In SQLite."}}',
    '{"type":"user","message":{"role":"user","content":"Fine, keep SQLite."}}',
]
TEXTS = ['How do we store the archive?', 'In SQLite.', 'Fine, keep SQLite.']
CANDIDATE = 'The demo session chose SQLite for storage.'


def body(s):
    v = s.fresh_vault()
    transcripts = s.root / 'T'
    transcripts.mkdir()
    s.append_toml(v, f'[sessions]\nmode = "auto"\n\n[sessions.sources]\nclaude = [{json.dumps(str(transcripts))}]\n')
    executor = s.executor_config()
    s1 = transcripts / 's1.jsonl'
    s1.write_text('\n'.join(LINES) + '\n', encoding='utf-8')

    def hook(session_id, path):
        return s.memory('hook', 'session-end', '--provider', 'claude', '--vault', v,
                        stdin=json.dumps({'session_id': session_id, 'transcript_path': str(path)}))

    def review(session_id, path):
        return s.result('sessions', 'review', '--vault', v, '--provider', 'claude', '--session-id', session_id,
                        '--transcript', path)

    reply, _ = hook('s1', s1)
    jobs = s.result('sessions', 'list', '--vault', v)['jobs']
    s.check(reply == {} and len(jobs) == 1 and jobs[0]['task'] == 'final' and jobs[0]['state'] == 'pending'
            and jobs[0]['end_record'] == 3,
            'Step 1: the hook returns {}; sessions list returns one job with task = "final", state = "pending", end_record = 3.')

    run = s.result('sessions', 'run', '--vault', v, '--executor-config', executor)
    job = run['jobs'][0] if run['jobs'] else {}
    s.check(job.get('state') == 'wiki_applied' and job.get('archive_id') and job.get('attempts') == 1,
            'Step 2 returns that job with state = "wiki_applied", a non-null archive_id and attempts = 1.')

    items = s.result('list', '--vault', v, '--limit', '5')['items']
    note = next((i for i in items if i['id'] == job.get('archive_id')), None)
    s.check(note and all(t in note['content'] for t in TEXTS),
            'Step 3: list.items contains a record whose id equals the job\'s archive_id and whose content contains all three user and assistant texts (the whole note).')
    page = (v / 'knowledge/wiki/projects/demo/demo.md').read_text(encoding='utf-8')
    managed = page.split('<!-- kb -->', 1)[1].split('<!-- /kb -->', 1)[0] if '<!-- kb -->' in page else ''
    s.check(CANDIDATE in managed, 'Step 3: the page contains the stub\'s candidate fact text inside the managed section.')
    rev = review('s1', s1)
    s.check(rev['capture']['status'] == 'captured' and not rev['processing'].get('failures')
            and rev['semantic_completeness_guaranteed'] is False,
            'Step 3: the review returns capture.status = "captured", processing.status with no failures, semantic_completeness_guaranteed = false.')
    records_before = s.result('status', '--vault', v)['archive_records']

    with s1.open('ab') as handle:
        handle.write(b'{"type":"user","message":{"role":"user","content":"partial')
    reply, _ = hook('s1', s1)
    s.check(reply == {}, 'Step 4: the hook returns {}.')
    run = s.result('sessions', 'run', '--vault', v, '--executor-config', executor)
    s.check(run['jobs'] == [], 'Step 4: the hook schedules no new job: the run returns jobs = [].')
    rev = review('s1', s1)
    s.check(rev['capture'].get('partial_tail') is True and rev['processing'].get('incomplete_tail') is True
            and rev['processing'].get('status') == 'pending',
            'Step 4: the review returns capture.partial_tail = true, processing.incomplete_tail = true and processing.status = "pending".')
    s.check(s.result('status', '--vault', v)['archive_records'] == records_before,
            'Step 4: kelmo status shows the archive record count unchanged by this step.')

    path = v / 'kelmo.toml'
    path.write_text(path.read_text(encoding='utf-8').replace('mode = "auto"', 'mode = "off"', 1), encoding='utf-8')
    s2 = transcripts / 's2.jsonl'
    s2.write_text('\n'.join(LINES) + '\n', encoding='utf-8')
    reply, _ = hook('s2', s2)
    jobs = s.result('sessions', 'list', '--vault', v)['jobs']
    before_keys = {j['session_key'] for j in jobs}
    s.check(reply == {} and len(jobs) == 1 and jobs[0]['id'] == job.get('id'),
            'Step 5: the hook returns {} and sessions list still shows exactly the one job of s1 and none for s2.')

    path.write_text(path.read_text(encoding='utf-8').replace('mode = "off"', 'mode = "auto"', 1), encoding='utf-8')
    outside = s.root / 'outside.jsonl'
    outside.write_text('\n'.join(LINES) + '\n', encoding='utf-8')
    snapshots_before = sorted(str(p) for p in (v / '.state').rglob('*') if p.is_file())
    payload, _ = hook('s3', outside)
    snapshots_after = sorted(str(p) for p in (v / '.state').rglob('*') if p.is_file())
    s.check(payload and payload.get('ok') is False and payload.get('error', {}).get('code') == 'E_TRANSCRIPT'
            and {j['session_key'] for j in s.result('sessions', 'list', '--vault', v)['jobs']} == before_keys
            and snapshots_after == snapshots_before,
            'Step 6 returns ok = false, error.code = "E_TRANSCRIPT", and no snapshot is created.')


if __name__ == '__main__':
    sys.exit(Scenario('A08', 'A conversation becomes a whole note and a checked fact').run(body))
