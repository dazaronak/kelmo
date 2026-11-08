"""Exercise the public launcher without an OS sandbox installation."""
import json
import hashlib
import os
import pty
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UserLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        prefix = self.base / 'installation with spaces'
        (prefix / 'bin').mkdir(parents=True)
        (prefix / 'bin/python').symlink_to(sys.executable)
        self.launcher = prefix / 'bin/kelmo'
        shutil.copy2(ROOT / 'packaging/kelmo', self.launcher)
        self.launcher.chmod(0o755)
        share = prefix / 'share/kelmo'
        share.mkdir(parents=True)
        shutil.copy2(ROOT / 'src/kelmo/entry.py', share / 'entry.py')
        version = f'python{sys.version_info.major}.{sys.version_info.minor}'
        packages = prefix / 'lib' / version / 'site-packages'
        packages.mkdir(parents=True)
        for source in (ROOT / '.venv/lib' / version / 'site-packages').iterdir():
            if source.name not in ('kelmo', 'kelmo.store') and source.suffix != '.pth':
                (packages / source.name).symlink_to(source)
        for name in ('kelmo', 'kelmo.store'):
            (packages / name).symlink_to(ROOT / 'src' / name)
        self.vault = self.base / 'vault'
        self.env = dict(os.environ, HOME=str(self.base), GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_AUTHOR_NAME='Synthetic',
                        GIT_AUTHOR_EMAIL='synthetic@example.invalid',
                        GIT_COMMITTER_NAME='Synthetic',
                        GIT_COMMITTER_EMAIL='synthetic@example.invalid')

    def run_cli(self, *args, content=None):
        return subprocess.run([str(self.launcher), '--vault', str(self.vault),
                               '--json', *args], input=content, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_help_without_bootstrap_or_sandbox(self):
        result = self.run_cli('help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('promote', json.loads(result.stdout)['result']['commands'])
        self.assertFalse(self.vault.exists())

    def test_utf8_protocol_with_ascii_locale_and_python_utf8_mode_disabled(self):
        self.vault = self.base / 'pamięć'
        initialized = self.run_cli('init', '--label', 'synthetic')
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        prefix = self.launcher.parent.parent
        command = [str(prefix / 'bin/python'), '-I', '-S', '-X', 'utf8=0',
                   str(prefix / 'share/kelmo/entry.py'), '--vault', str(self.vault), '--json']
        environment = dict(self.env, LC_ALL='C', LANG='C')
        source = 'Zażółć. Память. 日本語.'
        captured = subprocess.run([*command, 'add', '--file', '-', '--role', 'collector'],
                                  input=source.encode('utf-8'), env=environment,
                                  capture_output=True, timeout=30)
        self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
        operation = json.loads(captured.stdout)['operation']
        self.assertEqual(operation['source_hash'], hashlib.sha256(source.encode('utf-8')).hexdigest())
        shown = subprocess.run([*command, 'config', 'show'], env=environment,
                               capture_output=True, timeout=30)
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['result']['root'], str(self.vault))

    def test_initialize_capture_and_trace_without_sandbox(self):
        result = self.run_cli('init', '--label', 'synthetic', '--non-interactive')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_cli('add', '--file', '-', '--role', 'collector', content='Synthetic evidence.')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        operation = json.loads(result.stdout)['operation']
        self.assertEqual(operation['state'], 'captured')
        trace = self.run_cli('trace', operation['id'])
        self.assertEqual(trace.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(trace.stdout)['result']['events'][0]['stage'], 'captured')

    def test_init_prompt_offers_the_vault_the_environment_named(self):
        env = dict(self.env, KELMO_VAULT=str(self.vault))
        master, slave = pty.openpty()
        child = subprocess.Popen([str(self.launcher), 'init', '--label', 'synthetic'],
                                 stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env, text=True)
        os.close(slave)
        os.write(master, b'\n')
        out, err = child.communicate(timeout=60)
        os.close(master)
        self.assertEqual(child.returncode, 0, out + err)
        self.assertIn(f'Vault directory [{self.vault}]:', err)
        self.assertTrue((self.vault / 'kelmo.toml').is_file())
        self.assertFalse((self.base / '.local/share/kelmo/default').exists())


    def test_model_import_reports_missing_source_without_isolation_error(self):
        initialized = self.run_cli('init', '--label', 'synthetic')
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        result = self.run_cli('models', 'import', '--from', str(self.base / 'missing-model'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_MODEL_IMPORT')
        self.assertNotIn('Traceback', result.stderr)

    def test_recovery_command_uses_the_installed_launcher_without_path_entry(self):
        initialized = self.run_cli('init', '--label', 'synthetic')
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        archived = self.run_cli('add', '--role', 'lead', '--file', '-', '--wait',
                                content='Synthetic evidence to correct.')
        self.assertEqual(archived.returncode, 0, archived.stdout + archived.stderr)
        archive_id = json.loads(archived.stdout)['operation']['archive_id']
        # Corrections require vectors; plain captures may keep them pending.
        failed = self.run_cli('add', '--role', 'lead', '--file', '-', '--wait', '--supersedes', archive_id,
                              content='Synthetic evidence without provisioned embeddings.')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stdout)['error']['code'], 'E_EMBEDDING')
        retry = shlex.split(json.loads(failed.stdout)['error']['retry'][0]['command'])
        self.assertEqual(retry[0], str(self.launcher))
        retried = subprocess.run([*retry, '--json'], env=self.env, capture_output=True,
                                 text=True, timeout=30)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)

    def test_symlink_uses_installation_instead_of_current_directory(self):
        link = self.base / 'kelmo-link'
        link.symlink_to(self.launcher)
        result = subprocess.run([str(link), 'version', '--json'], cwd=self.base,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_symlink_launcher_supports_the_alternate_system_readlink_location(self):
        alternate = self.base / 'system-tools/readlink'
        alternate.parent.mkdir()
        alternate.symlink_to(shutil.which('readlink'))
        source = self.launcher.read_text().replace('/usr/bin/readlink', '/missing-system-readlink')
        source = source.replace('/bin/readlink', str(alternate))
        self.launcher.write_text(source)
        self.test_symlink_uses_installation_instead_of_current_directory()

    def test_incomplete_install_returns_structured_error(self):
        (self.launcher.parent / 'python').unlink()
        result = self.run_cli('help')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_RUNTIME')

    def test_missing_application_returns_runtime_envelope_without_traceback(self):
        prefix = self.launcher.parent.parent
        version = f'python{sys.version_info.major}.{sys.version_info.minor}'
        (prefix / 'lib' / version / 'site-packages/kelmo').unlink()
        result = self.run_cli('version')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_RUNTIME')
        self.assertNotIn('Traceback', result.stderr)

    def test_installed_cli_writes_guidance_and_accepts_verified_note(self):
        instructions = self.base / 'AGENTS.md'
        installed = self.run_cli('install', '--instructions', str(instructions), '--disable-native-memory')
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue(json.loads(installed.stdout)['result']['native_memory_prohibited'])
        self.assertFalse(json.loads(installed.stdout)['result']['native_memory_disabled'])
        initialized = self.run_cli('init', '--label', 'synthetic', '--non-interactive')
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        searched = self.run_cli('reconcile', '--text', 'A synthetic fact.', '--target', 'projects/test.md')
        self.assertEqual(searched.returncode, 0, searched.stdout + searched.stderr)
        receipt = json.loads(searched.stdout)['result']['search_id']
        changes = [dict(fact_key='test.fact', target='projects/test.md', text='A synthetic fact.',
                        status='active', expected_revision=None, replaces=[])]
        checks = [dict(fact_key='test.fact', verified_match=dict(result='not_found', search_id=receipt))]
        source = ('```wiki-changes\n' + json.dumps(changes) + '\n```\n'
                  + '```fact-checks\n' + json.dumps(checks) + '\n```\n')
        promoted = self.run_cli('promote', '--role', 'lead', '--file', '-', content=source)
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        self.assertEqual(json.loads(promoted.stdout)['operation']['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
