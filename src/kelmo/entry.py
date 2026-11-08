"""Load the installed application without ambient Python startup hooks.

Python -I -S controls imports; it does not restrict OS permissions.
"""
import sys
import json
import os
from pathlib import Path

def main():
    if not sys.flags.utf8_mode and sys.getfilesystemencoding().lower() != 'utf-8':
        # Filesystem decoding is fixed at startup; stream reconfiguration cannot repair argv.
        os.execv(sys.executable, [sys.executable, '-I', '-S', '-X', 'utf8=1', __file__, *sys.argv[1:]])
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    prefix = Path(__file__).resolve().parents[2]
    packages = prefix / 'lib' / ('python' + str(sys.version_info.major) + '.' + str(sys.version_info.minor)) / 'site-packages'
    sys.path.insert(0, str(packages))
    try:
        from kelmo.cli import main as cli_main
    except ImportError:
        message = 'Installed application is unavailable; reinstall the runtime.'
        if '--json' in sys.argv:
            print(json.dumps({'schema_version': 1, 'ok': False,
                              'error': {'code': 'E_RUNTIME', 'message': message}}))
        else:
            print('E_RUNTIME: ' + message, file=sys.stderr)
        return 1
    return cli_main(executable=str(prefix / 'bin/kelmo'))


if __name__ == '__main__':
    raise SystemExit(main())
