"""Stdlib controller tests; kernel tests run on Linux outside the child boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kelmo.runtime import IsolationError, RuntimePaths, environment


class LinuxRuntimeTests(unittest.TestCase):
    def test_filter_refuses_unknown_architecture(self):
        from kelmo.linux_runtime import seccomp_filter
        with self.assertRaises(IsolationError):
            seccomp_filter('unknown')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux kernel required')
    def test_kernel_boundary(self):
        from kelmo.linux_runtime import execute, seccomp_filter
        try:
            seccomp_filter()
        except IsolationError as error:
            # The policy builder refuses this architecture (an arm64 container on an arm Mac): there is no
            # boundary here to measure, and every host the runtime is deployed on builds it.
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / 'scratch'
            scratch.mkdir()
            secret = root / 'synthetic-secret'
            secret.write_text('synthetic')
            code = root / 'code'
            code.mkdir()
            protected = code / 'module.py'
            protected.write_text('synthetic')
            python = Path(sys.executable).resolve()
            paths = RuntimePaths(tuple(p.resolve() for p in
                                       (Path('/usr/lib'), Path('/usr/lib64'), code) if p.exists()),
                                 (scratch,), (python,))
            target = subprocess.Popen([str(python), '-I', '-S', '-c',
                                       'import time; time.sleep(30)'],
                                      env={'SYNTHETIC_ONLY': 'test-value'},
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            try:
                probe = '''import json,os,socket
from pathlib import Path
checks={}
for name, action in [('tcp', lambda: socket.socket()),
                     ('udp', lambda: socket.socket(type=socket.SOCK_DGRAM)),
                     ('unix', lambda: socket.socket(socket.AF_UNIX)),
                     ('private', lambda: Path(SECRET).read_text()),
                     ('write_code', lambda: Path(PROTECTED).write_text('bad'))]:
    try: action()
    except OSError: checks[name]=True
    else: checks[name]=False
checks['host_process_hidden']=not Path('/proc/TARGET/environ').exists()
checks['host_process_args_hidden']=not Path('/proc/TARGET/cmdline').exists()
checks['clean_environment']='SYNTHETIC_HOST_TOKEN' not in os.environ
checks['synthetic_identity']=Path('/etc/machine-id').read_text().strip()=='0'*32
status=Path('/proc/self/status').read_text()
checks['no_new_privs']='NoNewPrivs:\\t1' in status
checks['no_capabilities']='CapEff:\\t0000000000000000' in status
checks['no_routes']=len(Path('/proc/net/route').read_text().splitlines())==1
Path(OUTPUT).write_text('allowed')
print(json.dumps(checks))
'''.replace('SECRET', repr(str(secret))).replace('PROTECTED', repr(str(protected))).replace('TARGET', str(target.pid)).replace('OUTPUT', repr(str(scratch / 'result')))
                env = environment(scratch, [python.parent])
                result = execute(paths, [str(python), '-I', '-S', '-c', probe],
                                 env=env, cwd=scratch, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all(json.loads(result.stdout).values()), result.stdout)
                self.assertEqual((scratch / 'result').read_text(), 'allowed')
                self.assertEqual(protected.read_text(), 'synthetic')
            finally:
                target.terminate()
                target.wait(timeout=5)
