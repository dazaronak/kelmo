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

    def test_a_shard_killed_before_its_cases_ran_counts_every_one_of_them_as_never_run(self):
        """Round robin over two shards deals test_a and test_c to the first; it dies before either runs."""
        (self.root / 'conftest.py').write_text(
            'import os\n'
            'def pytest_collection_modifyitems(config, items):\n'
            '    if not config.option.collectonly and any(item.name == "test_a" for item in items):\n'
            '        os._exit(9)\n')
        done = self.shards(width='2')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('report 2 outcome(s) for the 4 case(s) they were dealt: 2 never ran', done.stdout)

    def test_a_shard_that_died_after_a_case_ran_does_not_say_its_cases_never_ran(self):
        """The first of two shards runs test_a, then dies before it reports."""
        (self.root / 'conftest.py').write_text(
            'import os\n'
            'def pytest_runtest_logreport(report):\n'
            '    if report.when == "teardown" and report.nodeid.endswith("::test_a"):\n'
            '        os._exit(9)\n')
        done = self.shards(width='2')
        self.assertEqual(done.returncode, 1, done.stdout)
        # test_a finished before the shard died; test_c, dealt to the same shard, never ran.
        self.assertIn('dealt: 1 never ran or ran unreported', done.stdout)

    def test_the_summary_stays_the_last_line_when_the_step_summary_cannot_be_written(self):
        env = dict(os.environ, GATE_SHARD_WIDTH='2', PYTHONUNBUFFERED='1',
                   GITHUB_STEP_SUMMARY=str(self.root / 'missing' / 'summary.md'))
        env.pop('PYTEST_ADDOPTS', None)
        done = subprocess.run([sys.executable, str(SHARDS), '-q'], cwd=self.root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn('step summary not written', done.stdout)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('4 passed in '), done.stdout)

    def test_a_shard_whose_output_is_not_utf8_still_gives_a_summary(self):
        (self.root / 'test_bytes.py').write_text('import os\n\ndef test_e():\n    os.write(1, b"\\xff\\xfe\\n")\n')
        done = self.shards('-s', width='2')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('5 passed in '), done.stdout + done.stderr)

    def test_coloured_pytest_output_still_gives_a_summary(self):
        done = self.shards(width='2', PY_COLORS='1')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith('4 passed in '), done.stdout)

    def test_a_selection_that_matches_nothing_is_not_called_a_collection_failure(self):
        summary = self.root / 'summary.md'
        done = self.shards('-k', 'nothing_matches_this', GITHUB_STEP_SUMMARY=str(summary))
        self.assertEqual(done.returncode, 5, done.stdout)
        self.assertNotIn('collection failed', summary.read_text())
        self.assertIn('no tests collected', summary.read_text())

    def test_a_coloured_collection_error_still_names_the_module(self):
        (self.root / 'test_broken.py').write_text('def test_e(:\n')
        summary = self.root / 'summary.md'
        self.shards(PY_COLORS='1', GITHUB_STEP_SUMMARY=str(summary))
        self.assertIn('test_broken.py', summary.read_text())

    def test_the_summary_block_starts_on_a_line_of_its_own(self):
        summary = self.root / 'summary.md'
        summary.write_text('earlier step text without a line end')
        self.shards(GITHUB_STEP_SUMMARY=str(summary))
        self.assertIn('\n### pytest\n', summary.read_text())

    def test_a_shard_whose_cases_were_all_dropped_says_so(self):
        (self.root / 'conftest.py').write_text(
            'def pytest_collection_modifyitems(config, items):\n'
            '    if not config.option.collectonly:\n'
            '        items[:] = [item for item in items if item.name not in ("test_a", "test_c")]\n')
        done = self.shards(width='2')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertNotIn('without a pytest summary', done.stdout)
        self.assertIn('2 never ran', done.stdout)

    def test_an_unwritable_step_summary_costs_the_run_nothing(self):
        done = self.shards(GITHUB_STEP_SUMMARY=str(self.root / 'missing' / 'summary.md'))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('step summary not written', done.stderr)

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

    def test_progress_counts_the_cases_a_shard_ran_not_the_ones_it_was_dealt(self):
        (self.root / 'conftest.py').write_text(
            'def pytest_collection_modifyitems(config, items):\n'
            '    if not config.option.collectonly:\n'
            '        items[:] = [item for item in items if item.name != "test_b"]\n')
        done = self.shards(width='1')
        self.assertEqual(done.returncode, 1, done.stdout)
        progress = [line for line in done.stdout.splitlines() if line.startswith('progress: ')]
        self.assertEqual(progress, ['progress: shard 1 of 1 ended with exit 0, 3 of 4 cases done'], done.stdout)

    def test_a_case_with_two_outcomes_does_not_hide_a_case_that_never_ran(self):
        """test_a passes and errors in its teardown (two outcomes); --maxfail=1 then stops the shard before test_b.
        Two outcomes for two cases must not read as every case having run."""
        (self.root / 'test_four.py').write_text(
            'import pytest\n\n'
            '@pytest.fixture\n'
            'def broken():\n'
            '    yield\n'
            '    raise RuntimeError("synthetic teardown error")\n\n'
            'def test_a(broken):\n'
            '    pass\n\n'
            'def test_b():\n'
            '    pass\n')
        done = self.shards('--maxfail=1', width='1')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('for the 2 case(s) they were dealt: 1 never ran', done.stdout)
        progress = [line for line in done.stdout.splitlines() if line.startswith('progress: ')]
        self.assertEqual(progress, ['progress: shard 1 of 1 ended with exit 1, 1 of 2 cases done'], done.stdout)

    def test_a_module_that_does_not_import_still_reaches_the_step_summary(self):
        (self.root / 'test_broken.py').write_text('def test_e(:\n')
        summary = self.root / 'step-summary.md'
        done = self.shards(GITHUB_STEP_SUMMARY=str(summary))
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertTrue(summary.is_file(), 'the collection red wrote no step summary')
        written = summary.read_text()
        self.assertIn('collection failed', written)
        self.assertIn('test_broken.py', written)


if __name__ == '__main__':
    unittest.main()
