"""One bounded JSON request over SSH stdio; deployment fixes runtime, vault and the answer shape."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main(runtime, vault):
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) - {'argv', 'input'}:
            raise ValueError
        argv = request.get('argv')
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in argv)):
            raise ValueError
        if argv[0] not in ('add', 'promote', 'status', 'doctor', 'search', 'show',
                           'list', 'trace', 'logs', 'worker', 'queue'):
            raise ValueError
        if any(a == '--vault' or a.startswith('--vault=') or a == '--file'
               or a.startswith('--file=') or a == '--timings' or a.startswith('--timings=')
               or a == '--' for a in argv):
            raise ValueError
        payload = request.get('input', '')
        if not isinstance(payload, str):
            raise ValueError
        if argv[0] in ('add', 'promote'):
            argv.extend(['--file', '-'])
        elif payload:
            raise ValueError
        result = subprocess.run([str(Path(runtime) / 'bin/kelmo'), '--vault', str(vault),
                                 '--json', '--non-interactive', *argv], input=payload,
                                env={key: value for key, value in os.environ.items()
                                     if key != 'KELMO_TIMINGS'},
                                text=True, close_fds=True, check=False, timeout=120)
        return result.returncode
    except (ValueError, OSError, subprocess.TimeoutExpired):
        print(json.dumps({'schema_version': 1, 'ok': False,
                          'error': {'code': 'E_GATE', 'message': 'Gate request could not be processed.'}}))
        return 1


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
