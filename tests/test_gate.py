"""Malformed requests must never launch the runtime, and never widen what the runtime answers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GateTests(unittest.TestCase):
    def test_rejects_scope_override_and_non_runtime_commands(self):
        gate = Path(__file__).resolve().parents[1] / 'packaging/gate.py'
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'bin').mkdir()
            launcher = runtime / 'bin/kelmo'
            launcher.write_text('#!/bin/sh\n'
                                'if [ -n "${KELMO_TIMINGS-}" ]; then '
                                'printf \'%s\\n\' \'{"ok":false,"leaked":true}\'\n'
                                'else printf \'%s\\n\' \'{"ok":true}\'\nfi\n')
            launcher.chmod(0o700)

            def invoke(argv, env=None):
                return subprocess.run([sys.executable, '-I', '-S', str(gate),
                                       str(runtime), '/synthetic-vault'],
                                      input=json.dumps({'argv': argv}), text=True,
                                      capture_output=True, env=dict(os.environ, **(env or {})))

            self.assertTrue(json.loads(invoke(['status']).stdout)['ok'])
            self.assertTrue(json.loads(invoke(['status'], {'KELMO_TIMINGS': '1'}).stdout)['ok'])
            for argv in (['status', '--vault', '/other'], ['search', '--vault=/other'],
                         ['add', '--file', '/other'], ['models', 'import'], ['init'],
                         ['status', '--'], ['status', 1], ['status', '--timings'],
                         ['search', 'basalt', '--timings=1']):
                with self.subTest(argv=argv):
                    result = invoke(argv)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_GATE')


if __name__ == '__main__':
    unittest.main()
