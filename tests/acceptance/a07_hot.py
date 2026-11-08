#!/usr/bin/env python3
"""A07. HOT delivers whole pages or nothing."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario


def set_max_bytes(vault, value):
    path = vault / 'kelmo.toml'
    path.write_text(re.sub(r'max_bytes = \d+', f'max_bytes = {value}', path.read_text(encoding='utf-8'), count=1),
                    encoding='utf-8')


def body(s):
    v = s.fresh_vault()
    DEMO, SETUP = s.published_hot_pages(v)
    project = s.root / 'P'
    project.mkdir()

    hot = s.result('hot', '--vault', v)
    s.check(hot['configured'] is False and hot['context'] == '' and hot['pages'] == [],
            'Step 1 returns configured = false, context = "", pages = [].')

    s.append_toml(v, f'[hot]\npages = ["projects/demo/demo.md"]\nmax_bytes = 2000\n\n'
                     f'[hot.projects.demo]\nroots = [{json.dumps(str(project))}]\npages = ["projects/demo/setup.md"]\n')
    deeper = project / 'deeper' / 'dir'
    deeper.mkdir(parents=True)
    globals_ = s.result('hot', '--vault', v)
    with_p = s.result('hot', '--vault', v, '--cwd', project)
    with_deeper = s.result('hot', '--vault', v, '--cwd', deeper)
    with_root = s.result('hot', '--vault', v, '--cwd', '/')
    s.check([p['path'] for p in globals_['pages']] == ['projects/demo/demo.md'] and DEMO in globals_['context'],
            'Step 2: without --cwd, pages lists only projects/demo/demo.md and context contains that file\'s complete bytes.')
    s.check(all(r['project'] == 'demo' and [p['path'] for p in r['pages']] == ['projects/demo/demo.md', 'projects/demo/setup.md']
                and DEMO in r['context'] and SETUP in r['context'] for r in (with_p, with_deeper)),
            'Step 2: with --cwd P and with the deeper directory, project = "demo" and pages lists projects/demo/demo.md then projects/demo/setup.md, and context contains both files\' complete bytes.')
    s.check(with_root['project'] is None and [p['path'] for p in with_root['pages']] == ['projects/demo/demo.md'],
            'Step 2: with --cwd /, project = null and only the global page is listed.')
    s.check(all(r['bytes'] == len(r['context'].encode('utf-8')) for r in (globals_, with_p, with_deeper, with_root)),
            'Step 2: bytes is the UTF-8 length of context.')

    hook, _ = s.memory('hook', 'start', '--vault', v, stdin=json.dumps({'cwd': str(project), 'session_id': 's1'}))
    s.check(hook == {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': with_p['context']}},
            'Step 3 returns {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CONTEXT}} where CONTEXT equals the context from step 2 for P.')

    set_max_bytes(v, 200)
    payload, _ = s.memory('hot', '--vault', v, '--cwd', project)
    s.check(payload and payload.get('ok') is False and payload.get('error', {}).get('code') == 'E_HOT'
            and 'context' not in (payload.get('result') or {}),
            'Step 4: hot returns ok = false, error.code = "E_HOT", and no context.')
    hook, _ = s.memory('hook', 'start', '--vault', v, stdin=json.dumps({'cwd': str(project), 'session_id': 's1'}))
    text = (hook or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
    s.check(text.startswith('Kelmo HOT unavailable (E_HOT)') and DEMO not in text and SETUP not in text,
            'Step 4: the hook returns additionalContext that starts with "Kelmo HOT unavailable (E_HOT)" and contains no page text.')

    set_max_bytes(v, 2000)
    path = v / 'kelmo.toml'
    path.write_text(path.read_text(encoding='utf-8').replace('pages = ["projects/demo/demo.md"]',
                                                               'pages = ["projects/demo/demo.md", "projects/demo/absent.md"]', 1),
                    encoding='utf-8')
    payload, _ = s.memory('hot', '--vault', v)
    s.check(payload and payload.get('ok') is False and str(payload.get('error', {}).get('code', '')).startswith('E_')
            and 'context' not in (payload.get('result') or {}),
            'Step 5 returns ok = false with an E_ code and no partial context.')


if __name__ == '__main__':
    sys.exit(Scenario('A07', 'HOT delivers whole pages or nothing').run(body))
