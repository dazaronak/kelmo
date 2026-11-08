"""gate.json: this folder's own measurement contract, read as data.

Three claims the folder has to keep true whatever the runner does with them:

- the lanes the suite ignores are the lanes the workflow ignores, and each one is run by a section here. A lane
  CI excludes and this gate forgets is a lane nobody runs; a lane this gate runs and CI also collects is run
  twice, in two orders, with two timeouts.
- the `--ignore=` arguments in the suite command are the `exclude` list of the same section. The list is what
  the coverage claim reads; the arguments are what the run obeys. When they drift apart the suite runs a lane
  the gate also runs, and the gate still reports it as measured once.
- every shell file in this folder is inside the reach of both shell sections. The sections name directories and
  a glob, and the runner walks them by shebang, so a launcher added in a directory no part of `files` names is
  parsed and linted by nothing, and a file that joins the parse but not the lint is half-checked.

It reads gate.json, .github/workflows/ci.yml and the files on disk. Standalone cases run the bundled gate's
listing and synthetic probes in temporary source copies. They execute no database, model or native lane.
"""

import fnmatch
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parents[1]
GATE = 'gate.json'
CI = '.github/workflows/ci.yml'
MODEL_FILES = ('config.json', 'model.onnx', 'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json')


def gate():
    return json.loads((FOLDER / GATE).read_text())


def section(name_start):
    matches = [entry for entry in gate()['sections'] if entry['name'].startswith(name_start)]
    assert len(matches) == 1, 'gate.json names %d sections starting %r; this case needs exactly one' % (
        len(matches), name_start)
    return matches[0]


def suite_argv():
    suite = section('the source suite')
    return [arg for runner in suite['runners'] for arg in runner['argv']]


def names_a_shell(path):
    """The bundled runner's rule (tools/gate.py shell_shebang), restated: the first line names a shell.
    The suffix is no use here - the installed launcher is `packaging/kelmo` and the acceptance harness sits
    beside python files that have no suffix either."""
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        first = stream.readline().strip()
    return first.startswith('#!') and any(token.rsplit('/', 1)[-1].endswith('sh') for token in first[2:].split())


def shell_files():
    """Every file under this folder whose first line names a shell, dot-directories left alone."""
    return [str(path.relative_to(FOLDER)) for path in sorted(FOLDER.rglob('*'))
            if path.is_file()
            and not any(part.startswith('.') for part in path.relative_to(FOLDER).parts)
            and not path.is_relative_to(FOLDER / '_docs')
            and names_a_shell(path)]


def named_by(part, relative):
    if any(char in part for char in '*?['):
        if '/' in part:
            return fnmatch.fnmatch(relative, part)
        return '/' not in relative and fnmatch.fnmatch(relative, part)
    return relative == part or relative.startswith(part.rstrip('/') + '/')


class TheLanesTheSuiteIgnores(unittest.TestCase):
    def test_the_gate_ignores_exactly_the_lanes_the_workflow_ignores(self):
        in_workflow = sorted(set(re.findall(r'--ignore=(tests/[^\s]+\.py)', (FOLDER / CI).read_text())))
        self.assertTrue(in_workflow, '%s names no ignored lane, so this case checks nothing' % CI)
        self.assertEqual(in_workflow, sorted(section('the source suite').get('exclude', [])),
                         'gate.json and %s disagree about which lanes the suite excludes' % CI)

    def test_the_ignored_lanes_are_the_lanes_the_sections_run(self):
        ignored = sorted(section('the source suite').get('exclude', []))
        self.assertTrue(ignored, 'the suite section ignores no lane, so this case checks nothing')
        run_by = sorted(test for entry in gate()['sections'] if entry.get('kind') == 'unittest'
                        for test in entry.get('tests', []))
        self.assertEqual(ignored, run_by, 'a lane the suite ignores is run by no section, or a lane is run twice')
        for lane in ignored:
            self.assertTrue((FOLDER / lane).is_file(), '%s named by gate.json is not a file here' % lane)

    def test_the_suite_command_carries_the_lanes_the_exclude_list_names(self):
        ignored = sorted(section('the source suite').get('exclude', []))
        in_argv = sorted(arg[len('--ignore='):] for arg in suite_argv() if arg.startswith('--ignore='))
        self.assertEqual(ignored, in_argv,
                         'the suite command ignores a different set of lanes than the one gate.json excludes')


class TheShellFilesHere(unittest.TestCase):
    def test_every_shell_file_here_is_in_reach_of_both_shell_sections(self):
        on_disk = shell_files()
        self.assertTrue(on_disk, 'the shebang walk found no shell file, so it checks nothing')
        for name in ('syntax', 'lint (shellcheck'):
            entry = section(name)
            self.assertTrue(entry.get('shebang'), 'section %r names no shebang walk' % name)
            for relative in on_disk:
                self.assertTrue(any(named_by(part, relative) for part in entry['files']),
                                '%s is in no part of the files list of section %r' % (relative, name))

    def test_the_two_shell_sections_name_the_same_reach(self):
        parse, lint = section('syntax'), section('lint (shellcheck')
        self.assertEqual(parse['files'], lint['files'],
                         'a shell file can join one of the two shell checks and not the other')

    def test_the_model_directory_the_acceptance_section_holds_is_the_five_pinned_files(self):
        held = gate()['needs']['modeldir']['holding']
        self.assertEqual(list(MODEL_FILES), held,
                         'the acceptance prerequisite holds a different set than the five pinned model files')


class TheStandaloneGate(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='kelmo-gate-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / 'source copy with spaces'
        self.root.mkdir()
        paths = ['run-tests.sh', 'gate.json', 'tools/gate.py', 'tools/pytest_shards.py',
                 'tests/acceptance/run.sh', 'packaging/kelmo', 'packaging/kelmo-isolated',
                 'src/kelmo/__init__.py']
        paths += section('the source suite')['exclude']
        for relative in paths:
            source = FOLDER / relative
            if source.is_file():
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        self.env = dict(os.environ, GATE_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE='1')
        for name in ('KELMO_GATE_MODEL_DIR', 'KELMO_MODEL_DIR', 'KELMO_TEST_PREFIX',
                     'GATE_WITHOUT', 'GATE_PIN', 'GATE_BASH32'):
            self.env.pop(name, None)

    def run_gate(self, *args):
        return subprocess.run(['/bin/bash', str(self.root / 'run-tests.sh'), *args],
                              cwd=self.scratch.name, env=self.env, capture_output=True,
                              text=True, timeout=20)

    def test_a_source_copy_lists_every_section_without_a_parent_checkout(self):
        result = self.run_gate('--list')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({'1', '2', '3', '4', '5', '6', '7', '8'},
                         {line.split()[0] for line in result.stdout.splitlines()})
        self.assertIn('1 packaging/kelmo\n', result.stdout)
        self.assertIn('5 tests/acceptance/run.sh\n', result.stdout)
        self.assertFalse((self.root / '.git').exists())

    def test_an_extracted_source_archive_runs_after_the_original_copy_is_removed(self):
        archive_path = Path(self.scratch.name) / 'source.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as archive:
            archive.add(self.root, arcname='kelmo-source')
        shutil.rmtree(self.root)
        destination = Path(self.scratch.name) / 'extracted'
        with tarfile.open(archive_path) as archive:
            archive.extractall(destination, filter='data')
        self.root = destination / 'kelmo-source'
        result = self.run_gate('--list')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('7 tests/test_installed_cli.py\n', result.stdout)

    def test_missing_local_runner_is_an_actionable_error(self):
        runner = self.root / 'tools/gate.py'
        if runner.exists():
            runner.unlink()
        result = self.run_gate('--list')
        self.assertEqual(2, result.returncode)
        self.assertIn(str(runner), result.stderr)
        self.assertIn('runner is missing', result.stderr)

    def test_invalid_section_selection_cannot_report_a_successful_empty_run(self):
        for selection in ('9', '1,9', '', 'x', '0'):
            with self.subTest(selection=selection):
                result = self.run_gate('--only', selection)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn('--only', result.stderr)
                self.assertNotIn('PASS=', result.stdout)

    def test_an_unrunnable_explicit_interpreter_does_not_fall_back(self):
        self.env['GATE_PYTHON'] = str(self.root / 'missing-python')
        result = self.run_gate('--list')
        self.assertEqual(2, result.returncode)
        self.assertIn('is not runnable', result.stderr)

    def test_path_python_is_used_when_no_interpreter_or_venv_is_named(self):
        self.env.pop('GATE_PYTHON')
        bin_dir = Path(self.scratch.name) / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').symlink_to(sys.executable)
        for name in ('dirname', 'bash'):
            (bin_dir / name).symlink_to(shutil.which(name))
        self.env['PATH'] = str(bin_dir)
        result = self.run_gate('--list')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('PATH python3 runs the gate', result.stderr)

    def write_import_probe(self):
        stale = Path(self.scratch.name) / 'old site-packages' / 'kelmo'
        stale.mkdir(parents=True)
        (stale / '__init__.py').write_text("raise RuntimeError('stale installed wheel')\n")
        self.env['PYTHONPATH'] = str(stale.parent)
        probe = (
            'import kelmo, pathlib, sys; '
            'assert pathlib.Path(kelmo.__file__).resolve() == '
            "pathlib.Path('src/kelmo/__init__.py').resolve(); "
            'assert sys.executable == sys.argv[1]'
        )
        (self.root / 'import_probe.py').write_text(probe + '\n')
        config = {'title': 'copy import probe', 'needs': {'interpreter': {'capability': 'python'}},
                  'sections': [
                      {'name': 'shell reach', 'kind': 'syntax',
                       'files': ['run-tests.sh'], 'shebang': True},
                      {'name': 'checkout import', 'kind': 'command', 'needs': ['interpreter'],
                       'argv': ['$interpreter', 'import_probe.py', self.env['GATE_PYTHON']]},
                  ]}
        # Only synthetic inputs run here; the real source/acceptance/native lanes never execute.
        shutil.rmtree(self.root / 'tests')
        shutil.rmtree(self.root / 'packaging')
        (self.root / 'gate.json').write_text(json.dumps(config))

    def test_explicit_interpreter_imports_the_copy_before_an_old_wheel(self):
        self.write_import_probe()
        venv_python = self.root / '.venv/bin/python'
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text('#!/bin/sh\nexit 88\n')
        venv_python.chmod(0o755)
        result = self.run_gate('--only', '2')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('PASS=1 FAIL=0', result.stdout)

    def test_local_venv_is_discovered_from_the_copy_not_the_callers_directory(self):
        venv_python = self.root / '.venv/bin/python'
        venv_python.parent.mkdir(parents=True)
        marker = self.root / '.venv/selected'
        venv_python.write_text('#!/bin/sh\n: > %s\nexec %s "$@"\n' %
                               (shlex.quote(str(marker)), shlex.quote(sys.executable)))
        venv_python.chmod(0o755)
        self.write_import_probe()
        self.env.pop('GATE_PYTHON')
        result = self.run_gate('--only', '2')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(marker.is_file(), 'the gate never started the local venv interpreter')
        self.assertNotIn('PATH python3 runs the gate', result.stderr)
        self.assertIn('PASS=1 FAIL=0', result.stdout)


class TheBundledRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('measurement_gate', FOLDER / 'tools/gate.py')
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def test_the_real_checkout_lists_the_same_targets_as_the_runner(self):
        result = subprocess.run(['/bin/bash', str(FOLDER / 'run-tests.sh'), '--list'],
                                env=dict(os.environ, GATE_PYTHON=sys.executable),
                                capture_output=True, text=True, timeout=20)
        expected = io.StringIO()
        with redirect_stdout(expected):
            self.runner.listed(self.runner.load_gate(FOLDER), FOLDER)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected.getvalue(), result.stdout)
        self.assertEqual([], self.runner.coverage(gate(), FOLDER))
        self.assertEqual([], self.runner.shell_coverage(gate(), FOLDER))

    def test_model_inputs_are_explicit_ordered_and_require_all_five_files(self):
        config = {'needs': {'modeldir': gate()['needs']['modeldir']}}
        with tempfile.TemporaryDirectory() as scratch, patch.dict(os.environ):
            for name in ('KELMO_GATE_MODEL_DIR', 'KELMO_MODEL_DIR'):
                os.environ.pop(name, None)
            with patch.object(self.runner, 'WITHOUT', ''):
                absent = self.runner.resolve_needs(config, Path(scratch))['modeldir']
                self.assertFalse(absent.present)
                self.assertIn('none of KELMO_GATE_MODEL_DIR, KELMO_MODEL_DIR is set', absent.detail)
                first, second = Path(scratch) / 'first', Path(scratch) / 'second'
                first.mkdir()
                second.mkdir()
                for name in MODEL_FILES:
                    (second / name).touch()
                os.environ['KELMO_MODEL_DIR'] = str(second)
                selected = self.runner.resolve_needs(config, Path(scratch))['modeldir']
                self.assertTrue(selected.present)
                self.assertEqual(str(second), selected.value)
                os.environ['KELMO_GATE_MODEL_DIR'] = str(first)
                incomplete = self.runner.resolve_needs(config, Path(scratch))['modeldir']
                self.assertFalse(incomplete.present)
                self.assertIn('model.onnx', incomplete.detail)
                for name in MODEL_FILES:
                    (first / name).mkdir()
                directories = self.runner.resolve_needs(config, Path(scratch))['modeldir']
                self.assertFalse(directories.present, 'model file names must refer to files')
                for name in MODEL_FILES:
                    (first / name).rmdir()
                for name in MODEL_FILES:
                    (first / name).touch()
                selected = self.runner.resolve_needs(config, Path(scratch))['modeldir']
                self.assertTrue(selected.present)
                self.assertEqual(str(first), selected.value)

    def test_prefix_need_requires_a_launcher_that_answers(self):
        """A bin/kelmo that only reports E_RUNTIME (a uv virtualenv's console script) is not an installed prefix."""
        config = {'needs': {'prefix': gate()['needs']['prefix']}}
        with tempfile.TemporaryDirectory() as scratch, patch.dict(os.environ), patch.object(self.runner, 'WITHOUT', ''):
            prefix = Path(scratch) / 'prefix'
            (prefix / 'bin').mkdir(parents=True)
            os.environ['KELMO_TEST_PREFIX'] = str(prefix)
            empty = self.runner.resolve_needs(config, Path(scratch))['prefix']
            self.assertFalse(empty.present)
            self.assertIn('carries no bin/kelmo', empty.detail)
            launcher = prefix / 'bin/kelmo'
            launcher.write_text('#!/bin/sh\nprintf \'{"ok": false, "error": {"code": "E_RUNTIME"}}\\n\'\nexit 1\n')
            launcher.chmod(0o755)
            unavailable = self.runner.resolve_needs(config, Path(scratch))['prefix']
            self.assertFalse(unavailable.present)
            self.assertIn('exits 1', unavailable.detail)
            self.assertIn('E_RUNTIME', unavailable.detail)
            launcher.write_text('#!/bin/sh\nprintf \'{"ok": true, "result": {"version": "0"}}\\n\'\n')
            answering = self.runner.resolve_needs(config, Path(scratch))['prefix']
            self.assertTrue(answering.present, answering.detail)
            self.assertEqual(str(prefix), answering.value)

    def test_wrapped_counts_keep_failure_skip_and_empty_suite_semantics(self):
        cases = [
            ('2 passed, 0 failed', 0, (1, 0, 0)),
            ('2 passed, 1 failed', 0, (0, 1, 0)),
            ('2 passed, 0 failed', 1, (0, 1, 0)),
            ('0 passed, 0 failed', 0, (0, 1, 0)),
            ('2 passed, 0 failed, 1 skipped\nSKIPPED [1] synthetic prerequisite absent', 0, (0, 0, 1)),
        ]
        for output, code, counts in cases:
            with self.subTest(output=output, code=code):
                entry = {'name': 'synthetic counts', 'kind': 'runner',
                         'runners': [{'argv': [sys.executable, '-c',
                                              'import sys; print(%r); sys.exit(%d)' % (output, code)]}],
                         'counts': r'^(?P<passed>\d+) passed, (?P<failed>\d+) failed(?:, (?P<skipped>\d+) skipped)?$',
                         'skip_line': r'^SKIPPED ', 'leaves': 'synthetic cases'}
                report = self.runner.Report()
                with redirect_stdout(io.StringIO()):
                    self.runner.run_runner(entry, FOLDER, {}, report)
                self.assertEqual(counts, (report.n_pass, report.n_fail, report.n_skip))
                if report.n_skip:
                    self.assertIn('synthetic prerequisite absent', report.skipped_lines)
                    self.assertEqual(['synthetic cases'], report.unmeasured)


if __name__ == '__main__':
    unittest.main()
