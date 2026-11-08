"""The macOS kernel boundary itself, run with a stdlib-only controller outside the child OS sandbox: only macOS has
the seatbelt the runtime builds, so the gate's macOS section runs this file on a Mac and every other host reports
that section absent."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kelmo.runtime import RuntimePaths, command, environment


class MacRuntimeTests(unittest.TestCase):
    # No case here asks a macOS sandbox to hide another process's arguments: the seatbelt policy of
    # macos_policy() does not deny the same-UID KERN_PROCARGS2 read, and docs/RUNTIME-SECURITY.md keeps that
    # gap open. The claim belongs to the Linux adapter and is measured in tests/test_linux_runtime.py.
    @unittest.skipUnless(sys.platform == 'darwin', 'Requires macOS kernel boundary')
    def test_os_denies_network_files_and_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch, code = root / 'scratch', root / 'code'
            scratch.mkdir()
            code.mkdir()
            forbidden, protected = root / 'private-canary.txt', code / 'dependency.py'
            forbidden.write_text('Synthetic private canary')
            protected.write_text('synthetic read-only dependency')
            executable = Path(sys.executable).resolve()
            app_executable = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
            executables = (executable, app_executable.resolve()) if app_executable.is_file() else (executable,)
            paths = RuntimePaths(tuple(p.resolve() for p in (
                Path(sys.base_prefix), code, Path('/System/Library'), Path('/usr/lib'),
                Path('/opt/homebrew/Cellar/openssl@3'), Path('/opt/homebrew/Cellar/sqlite'),
                Path('/opt/homebrew/Cellar/mpdecimal'), Path('/opt/homebrew/Cellar/xz'),
            ) if p.exists()), (scratch,), executables)
            probe = '''import json,socket,subprocess,os
from pathlib import Path
results={}
def denied(name,action):
    try:action()
    except PermissionError:results[name]=True
    except OSError:results[name]=False
    else:results[name]=False
denied('tcp',lambda:socket.socket().connect(('127.0.0.1',9)))
denied('udp',lambda:socket.socket(type=socket.SOCK_DGRAM).sendto(b'synthetic',('127.0.0.1',9)))
denied('private_file',lambda:Path(FORBIDDEN).read_text())
denied('private_metadata',lambda:Path(FORBIDDEN).stat())
denied('code_write',lambda:Path(PROTECTED).write_text('modified'))
denied('child_exec',lambda:subprocess.run(['/bin/sh','-c','exit 0'],check=True))
denied('hardlink',lambda:os.link(PROTECTED,LINK))
Path(OUTPUT).write_text('allowed')
print(json.dumps(results))
'''.replace('FORBIDDEN', repr(str(forbidden))).replace('PROTECTED', repr(str(protected))).replace('OUTPUT', repr(str(scratch / 'result'))).replace('LINK', repr(str(scratch / 'alias')))
            result = subprocess.run(command(paths, [str(executable), '-I', '-S', '-c', probe]),
                                    env=environment(scratch, [executable.parent]),
                                    capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all(json.loads(result.stdout).values()), result.stdout)
            self.assertEqual((scratch / 'result').read_text(), 'allowed')
            self.assertEqual(protected.read_text(), 'synthetic read-only dependency')


if __name__ == '__main__':
    unittest.main()
