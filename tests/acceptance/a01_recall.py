#!/usr/bin/env python3
"""A01. Written once, found by meaning."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario

ESPRESSO = 'The espresso machine in the office kitchen needs descaling every two months.'
TOPICS = ['tide tables', 'violin strings', 'tax forms', 'bicycle chains', 'sourdough starters',
          'glacier ice', 'chess openings', 'roof tiles', 'bee hives', 'submarine cables',
          'opera tickets', 'wool sweaters', 'railway signals', 'tomato seedlings', 'lighthouse lamps',
          'copper pipes', 'desert tortoises', 'ballet shoes', 'parking meters', 'library cards',
          'cathedral bells', 'ski bindings', 'ink cartridges', 'harbor cranes', 'birthday candles',
          'postage stamps', 'fishing nets', 'piano tuning', 'wind turbines', 'museum guards',
          'subway maps', 'garden hoses', 'camera lenses', 'wedding rings', 'soccer goals',
          'textbook margins', 'ferry schedules', 'orchard ladders', 'puppet strings', 'salt mines',
          'hockey pucks', 'telescope mirrors', 'picnic blankets', 'carousel horses', 'bridge tolls',
          'kite tails', 'sand dunes', 'theater curtains', 'pottery kilns', 'canal locks']


def filler(index):
    topic = TOPICS[index]
    return (f'Note {index + 1} is about {topic}. Nobody has checked the {topic} since last spring, '
            f'and the plan is to review them before the next quarterly meeting.\n')


def body(s):
    v = s.fresh_vault()
    espresso_id = None
    archived = 0
    for index in range(50):
        text = ESPRESSO + '\n' if index == 7 else filler(index)
        payload, _ = s.memory('add', '--vault', v, '--file', '-', '--role', 'implementer', '--wait', stdin=text)
        op = (payload or {}).get('result', {}).get('operation', {})
        if payload and payload.get('ok') and op.get('state') == 'archived' and op.get('archive_id'):
            archived += 1
            if index == 7:
                espresso_id = op['archive_id']
    s.check(archived == 50, 'Step 1: each call returns ok = true, operation.state = "archived" and a non-null operation.archive_id.')
    status = s.result('status', '--vault', v)
    s.check(status['archive_records'] == 50, 'Step 1: kelmo status returns archive_records = 50.')

    hit = s.result('search', '--vault', v, '--limit', '3', 'barista equipment cleaning routine')
    ids = [item['id'] for item in hit['items']]
    s.check(hit['search']['mode'] == 'hybrid' and hit['search']['degraded'] is False,
            'Step 2: search.mode = "hybrid", search.degraded = false.')
    s.check(espresso_id in ids, 'Step 2: items contains the espresso record (same id as its archive_id).')

    hit = s.result('search', '--vault', v, '--limit', '3', 'descaling')
    s.check(espresso_id in [item['id'] for item in hit['items']], 'Step 3: items contains the espresso record.')

    cache = v / '.cache' / 'embeddings'
    os.rename(cache, v / '.cache' / 'embeddings.off')
    try:
        payload, _ = s.memory('search', '--vault', v, '--limit', '3', 'descaling')
        search = (payload or {}).get('result', {}).get('search', {})
        items = (payload or {}).get('result', {}).get('items', [])
        s.check(payload and payload.get('ok') is True and search.get('mode') == 'lexical'
                and search.get('degraded') is True and search.get('reason'),
                'Step 4: the search returns ok = true, search.mode = "lexical", search.degraded = true, a non-null search.reason.')
        s.check(espresso_id in [item['id'] for item in items], 'Step 4: items still contains the espresso record.')
        payload, _ = s.memory('add', '--vault', v, '--file', '-', '--role', 'implementer', '--wait',
                              stdin='A note written while the model directory is absent.\n')
        # durable-text/1: model availability no longer gates ordinary archive intake.
        op = (payload or {}).get('result', {}).get('operation', {})
        s.check(payload and payload.get('ok') is True and op.get('state') == 'archived' and op.get('archive_id'),
                'Step 4: the add returns ok = true, archived state and a durable archive_id without the model.')
        status = s.result('status', '--vault', v)
        s.check(status['archive_records'] == 51, 'Step 4: kelmo status returns archive_records = 51.')
    finally:
        # A degraded search creates an empty .cache/embeddings/tmp; clear it before restoring the model files.
        import shutil
        shutil.rmtree(cache, ignore_errors=True)
        os.rename(v / '.cache' / 'embeddings.off', cache)


if __name__ == '__main__':
    sys.exit(Scenario('A01', 'Written once, found by meaning').run(body))
