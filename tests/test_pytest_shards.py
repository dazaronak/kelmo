"""tools/pytest_shards.py is green only when every collected case ran: a module that does not import, a case a
shard never ran, and an option whose value would swallow an id are reds."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SHARDS = Path(__file__).resolve().parents[1] / 'tools/pytest_shards.py'
FOUR = ''.join('def test_%s():\n    pass\n\n' % name for name in 'abcd')


class PytestShardsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / 'test_four.py').write_text(FOUR)

    def shards(self, *args, width='2', cwd=None, **extra):
        env = dict(os.environ, GATE_SHARD_WIDTH=width, **extra)
        env.pop('PYTEST_ADDOPTS', None)
        if 'GITHUB_STEP_SUMMARY' not in extra:
            env.pop('GITHUB_STEP_SUMMARY', None)
        return subprocess.run([sys.executable, str(SHARDS), '-q', *args], cwd=cwd or self.root, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_a_run_started_outside_the_project_finds_the_ids_it_collected(self):
        """Collected ids are relative to pytest's rootdir, not to where the runner was started."""
        project = self.root / 'project'
        (project / 'tests').mkdir(parents=True)
        (project / 'pytest.ini').write_text('[pytest]\n')
        (project / 'tests' / 'test_four.py').write_text(FOUR)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        done = self.shards(str(project / 'tests'), cwd=elsewhere)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('4 passed in '), done.stdout)

    def test_a_module_that_does_not_import_is_a_red_though_the_others_list_their_ids(self):
        (self.root / 'test_broken.py').write_text('def test_e(:\n')
        for extra in ((), ('--continue-on-collection-errors',)):
            with self.subTest(extra=extra):
                done = self.shards(*extra)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn('test_broken.py', done.stdout)
                self.assertNotIn('test_four.py::test_a\n', done.stdout)

    def test_a_case_dealt_to_a_shard_that_never_ran_it_is_a_red(self):
        (self.root / 'conftest.py').write_text(
            'def pytest_collection_modifyitems(config, items):\n'
            '    if not config.option.collectonly:\n'
            '        items[:] = [item for item in items if item.name != "test_b"]\n')
        done = self.shards(width='1')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('report 3 outcome(s) for the 4 case(s) they were dealt: 1 never ran', done.stdout)

    def test_an_option_whose_value_is_the_next_argument_keeps_it(self):
        done = self.shards('--deselect', 'test_four.py::test_a')
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('3 passed in '), done.stdout)

    def test_an_option_the_runner_does_not_know_takes_no_id_for_its_value(self):
        done = self.shards('--log-format', '%(message)s')
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn('--log-format', done.stdout)
        # No shard printed a summary: the runner still ends on its own summary line, not a traceback.
        self.assertNotIn('Traceback', done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('0 passed, 2 errors in '), done.stdout)

    def test_the_run_says_what_it_collected_and_each_shard_as_it_ends(self):
        done = self.shards()
        self.assertEqual(done.returncode, 0, done.stdout)
        lines = done.stdout.splitlines()
        self.assertEqual(lines[0], 'collected 4 cases in 2 shards', done.stdout)
        progress = [line for line in lines if line.startswith('progress: ')]
        self.assertEqual(len(progress), 2, done.stdout)
        self.assertTrue(progress[-1].endswith(', 4 of 4 cases done'), done.stdout)
        self.assertEqual(lines[-2], '4 of 4 collected cases passed', done.stdout)
        self.assertTrue(lines[-1].startswith('4 passed in '), done.stdout)

    def test_a_github_step_summary_file_gets_the_result(self):
        summary = self.root / 'step-summary.md'
        done = self.shards(GITHUB_STEP_SUMMARY=str(summary))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertTrue(summary.is_file(), 'the run wrote no step summary')
        written = summary.read_text()
        self.assertIn('4 of 4 collected cases passed', written)
        self.assertIn(done.stdout.splitlines()[-1], written)


if __name__ == '__main__':
    unittest.main()
