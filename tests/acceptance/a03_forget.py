#!/usr/bin/env python3
"""A03. Forgotten means not found."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import Scenario

ESPRESSO = 'The espresso machine in the office kitchen needs descaling every two months.'


def body(s):
    v = s.fresh_vault()
    payload, _ = s.memory('add', '--vault', v, '--file', '-', '--role', 'implementer', '--wait', stdin=ESPRESSO + '\n')
    op = payload['result']['operation']
    record, op_id = op['archive_id'], op['id']
    source_file = v / op['source_path']

    payload, _ = s.memory('forget', record, '--vault', v)
    result = (payload or {}).get('result', {})
    s.check(payload and payload.get('ok') is True and result.get('forgotten') is True and result.get('id') == record,
            'Step 1 returns ok = true and result.forgotten = true with result.id = R.')

    def observe(vault):
        hit = s.result('search', '--vault', vault, '--limit', '10', 'descaling')
        listing = s.result('list', '--vault', vault, '--limit', '100')
        shown, _ = s.memory('show', record, '--vault', vault)
        return ([i['id'] for i in hit['items']], [i['id'] for i in listing['items']], shown)

    found, listed, shown = observe(v)
    s.check(record not in found, 'Step 2: search.items does not contain R.')
    s.check(record not in listed, 'Step 2: list.items does not contain R.')
    s.check(shown and shown.get('ok') is False and shown.get('error', {}).get('code') == 'E_FORGOTTEN',
            'Step 2: show returns ok = false, error.code = "E_FORGOTTEN".')

    payload, _ = s.memory('forget', record, '--vault', v)
    s.check(payload and payload.get('ok') is True and payload.get('result', {}).get('forgotten') is False,
            'Step 3: the repeated forget returns ok = true with result.forgotten = false (already forgotten) and no error.')
    payload, _ = s.memory('forget', '00000000-0000-0000-0000-000000000000', '--vault', v)
    s.check(payload and payload.get('ok') is False and payload.get('error', {}).get('code') == 'E_NOT_FOUND',
            'Step 3: the unknown id returns ok = false, error.code = "E_NOT_FOUND".')

    trace = s.result('trace', op_id, '--vault', v)
    stages = [event['stage'] for event in trace['events']]
    forgotten = [event for event in trace['events'] if event['stage'] == 'forgotten']
    s.check('captured' in stages and 'archived' in stages and forgotten and forgotten[0].get('created_at'),
            'Step 4: the trace still lists the original captured, archived stages and a new stage forgotten with a timestamp.')
    s.check(source_file.is_file(), 'Step 4: The immutable source file under .state/sources/ still exists.')
    import sqlite3
    with sqlite3.connect(v / 'archive' / 'archive.db') as db:
        rows = db.execute('SELECT count(*) FROM memories WHERE id = ?', (record,)).fetchone()[0]
    s.check(rows == 1, 'Step 4: archive/archive.db still holds the record\'s bytes.')

    v3 = s.root / 'v3'
    shutil.copytree(v, v3, symlinks=True)
    found, listed, shown = observe(v3)
    s.check(record not in found and record not in listed
            and shown and shown.get('ok') is False and shown.get('error', {}).get('code') == 'E_FORGOTTEN',
            'Step 5: the same observations hold on the copy.')
    doctor = s.result('doctor', '--vault', v)
    s.check(doctor['healthy'] is True, 'kelmo doctor after the forget returns healthy = true.')


if __name__ == '__main__':
    sys.exit(Scenario('A03', 'Forgotten means not found').run(body))
