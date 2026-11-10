"""Helpers for the job tests: a stub executor that stays in its model call for a while, and the claim
count a test reads back from the journal."""
import sys
from pathlib import Path

STUB = """import json, os, sys, time
from pathlib import Path

json.load(sys.stdin)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(float(sys.argv[2]))
print(json.dumps({"done": True}))
"""


def slow_executor(directory, seconds):
    """argv of a stub executor: it reads its JSON payload, writes its pid to `directory/executor.pid`, sleeps
    for `seconds`, then prints a JSON object."""
    script = Path(directory) / 'slow_executor.py'
    script.write_text(STUB)
    return [sys.executable, str(script), str(Path(directory) / 'executor.pid'), str(seconds)]


def claim_rows(core, token):
    """How many claim rows carry this token, read through a reader."""
    return core.storage.read(lambda reader: reader.journal.conn.execute(
        'SELECT COUNT(*) FROM claims WHERE token=?', (token,)).fetchone()[0])
