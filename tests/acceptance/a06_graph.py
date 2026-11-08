#!/usr/bin/env python3
"""A06. The graph follows the wiki."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario

DEMO = '---\ntitle: Demo\ntags: [knowledge]\n---\n\n# Demo\n'
SETUP = '---\ntitle: Setup\nparent: projects/demo/demo.md\n---\n\n# Setup\n\nSee [[projects/demo/demo]].\n'
OTHER = '---\ntitle: Setup\n---\n\n# Setup\n\nOther setup.\n'


def seed(s, v):
    wiki = v / 'knowledge/wiki'
    s.write(wiki / 'projects/demo/demo.md', DEMO)
    s.write(wiki / 'projects/demo/setup.md', SETUP)
    s.write(wiki / 'projects/other/setup.md', OTHER)
    return wiki


def body(s):
    v = s.fresh_vault()
    wiki = seed(s, v)

    stats = s.result('graph', 'stats', '--vault', v)
    s.check(stats['graph']['status'] == 'absent', 'Step 1: graph.status = "absent" (no index yet); ok = true.')
    digest_1 = stats['graph'].get('source_digest')

    found = s.result('graph', 'query', 'search', '--query', 'setup', '--vault', v)
    ambiguous = found['result'] if isinstance(found['result'], dict) else {}
    s.check(ambiguous.get('error') == 'ambiguous_entity'
            and {'projects/demo/setup', 'projects/other/setup'} <= set(ambiguous.get('candidates', []))
            and found['graph']['status'] == 'current',
            'Step 2: result.error = "ambiguous_entity" and result.candidates contains both projects/demo/setup and projects/other/setup; graph.status = "current".')

    payload, _ = s.memory('graph', 'read', 'setup', '--vault', v)
    s.check(payload and payload.get('ok') is False and payload.get('error', {}).get('code') == 'E_AMBIGUOUS',
            'Step 3: the short name returns ok = false, error.code = "E_AMBIGUOUS".')
    read = s.result('graph', 'read', 'projects/demo/setup', '--vault', v)
    s.check(read['content'] == SETUP and read['file_path'] == 'projects/demo/setup.md',
            'Step 3: the full ID returns content equal to the file\'s bytes and file_path = "projects/demo/setup.md".')

    neighbors = s.result('graph', 'query', 'neighbors', '--id', 'projects/demo/demo', '--vault', v)['result']
    impact = s.result('graph', 'query', 'impact', '--id', 'projects/demo/demo', '--vault', v)['result']
    details = s.result('graph', 'query', 'details', '--id', 'projects/demo/setup', '--vault', v)['result']
    path = s.result('graph', 'query', 'path', '--id', 'projects/other/setup', '--target', 'projects/demo/demo', '--vault', v)['result']
    link = [e for e in details if e.get('relation') == 'links_to' and e.get('source_id') == 'projects/demo/setup'
            and e.get('target_id') == 'projects/demo/demo']
    s.check('projects/demo/setup' in [n['id'] for n in neighbors], 'Step 4: neighbors contains projects/demo/setup.')
    s.check('projects/demo/setup' in [n['id'] for n in impact], 'Step 4: impact contains projects/demo/setup.')
    s.check(link and link[0].get('source_file') == 'projects/demo/setup.md' and (link[0].get('source_line') or 0) > 0
            and any(e.get('relation') == 'child_of' for e in details),
            'Step 4: details lists an edge with relation = "links_to" from projects/demo/setup to projects/demo/demo carrying source_file = "projects/demo/setup.md" and a positive source_line, and an edge child_of.')
    s.check(path == [], 'Step 4: path returns an empty path (the two pages are not connected).')

    with (wiki / 'projects/demo/setup.md').open('a', encoding='utf-8') as handle:
        handle.write('Also see [[projects/demo/missing]].\n')
    stale = s.result('graph', 'stats', '--vault', v)
    s.check(stale['graph']['status'] == 'stale', 'Step 5: the first stats reports graph.status = "stale".')
    after = s.result('graph', 'query', 'neighbors', '--id', 'projects/demo/setup', '--vault', v)
    s.check('unresolved:projects/demo/missing' in [n['id'] for n in after['result']] and after['graph']['status'] == 'current',
            'Step 5: neighbors returns ok = true with an entry whose id = "unresolved:projects/demo/missing" and graph.status = "current".')
    current = s.result('graph', 'stats', '--vault', v)
    s.check(current['graph']['status'] == 'current' and current['graph']['source_digest'] != digest_1,
            'Step 5: the second stats reports current with a source_digest different from step 1.')

    (wiki / 'projects/other/setup.md').unlink()
    payload, _ = s.memory('graph', 'read', 'setup', '--vault', v)
    s.check(payload and payload.get('ok') is True and payload['result']['id'] == 'projects/demo/setup',
            'Step 6: read setup now returns ok = true for projects/demo/setup (the name became unique).')


if __name__ == '__main__':
    sys.exit(Scenario('A06', 'The graph follows the wiki').run(body))
