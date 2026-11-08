#!/usr/bin/env python3
"""Deterministic stand-in for a model executor: reads one JSON task on stdin, answers on stdout."""
import json
import sys

payload = json.load(sys.stdin)
task = payload.get('task')
if task == 'probe':
    reply = {'ok': True}
elif task in ('extract_checkpoint', 'extract_final'):
    texts = [m.get('text') or m.get('content') or '' for m in payload.get('messages', [])]
    reply = {'note_markdown': 'Session note. The user asked: ' + ' | '.join(str(t) for t in texts),
             'candidate_facts': [{'text': 'The demo session chose SQLite for storage.',
                                  'target': 'projects/demo/demo.md'}],
             'skip': False}
elif task == 'reconcile_session':
    reply = {'decisions': [{'index': i, 'action': 'new', 'fact_key': 'demo.session.' + str(i)}
                           for i, _ in enumerate(payload.get('candidates', []))]}
elif task == 'resolve_facts':
    reply = {'unresolved': True}
else:
    reply = {'unresolved': True}
json.dump(reply, sys.stdout)
