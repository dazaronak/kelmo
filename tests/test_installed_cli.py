"""Exercise the installed shell launcher in a disposable runtime."""
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFIX = Path(os.environ.get('KELMO_TEST_PREFIX', str(REPO / '.venv')))
LAUNCHER = PREFIX / 'bin/kelmo'


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires macOS or Linux')
class InstalledCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kelmo-cli-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / '.gitconfig').write_text('[user]\nname = Test User\nemail = test@example.invalid\n')
        self.env = {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(self.root),
                    'GIT_CONFIG_NOSYSTEM': '1'}

    def run_cli(self, *args, content=None, pass_fds=()):
        return subprocess.run([str(LAUNCHER), *map(str, args)], input=content,
                              cwd=self.root, env=self.env, capture_output=True, text=True,
                              timeout=20, pass_fds=pass_fds, check=False)

    def test_help_without_storage(self):
        result = self.run_cli('--vault', 'missing', 'help', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertFalse((self.root / 'missing').exists())
        for flag in ('--help', '--version'):
            result = self.run_cli(flag, '--json')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])

    def test_setup_dry_run_has_no_profile_or_vault_writes(self):
        result = self.run_cli('setup', '--dry-run', '--json', '--disable-native-memory',
                              '--vault', self.root/'new-vault', '--home', self.root/'profile',
                              '--executable', LAUNCHER, '--no-start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(result.stdout)['result']
        self.assertTrue(plan['dry_run'])
        self.assertFalse(plan['complete'])
        self.assertFalse((self.root/'new-vault').exists())
        self.assertFalse((self.root/'profile').exists())

    def test_search_without_embedding_model_reports_lexical_fallback(self):
        vault = self.root/'vault'
        self.assertEqual(self.run_cli('init', '--vault', vault, '--label', 'fixture', '--json').returncode, 0)
        result = self.run_cli('search', 'absent', '--vault', vault, '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        search = json.loads(result.stdout)['result']
        self.assertEqual(search['items'], [])
        self.assertTrue(search['search']['degraded'])
        self.assertEqual(search['search']['mode'], 'lexical')

    def test_installed_forget_hides_the_record_from_every_reader(self):
        """Forget through the installed launcher: hidden from search and list, refused by show, still stored."""
        model = os.environ.get('KELMO_MODEL_DIR') or os.environ.get('KELMO_GATE_MODEL_DIR')
        if not model:
            self.skipTest('KELMO_MODEL_DIR (or KELMO_GATE_MODEL_DIR) must name the directory holding the five '
                          'pinned model files; this host names none, so the installed hybrid search that the hiding '
                          'path has to hide a record from cannot run here')
        vault = self.root / 'vault'
        self.assertEqual(self.run_cli('init', '--vault', vault, '--label', 'fixture', '--json').returncode, 0)
        imported = self.run_cli('models', 'import', '--vault', vault, '--from', model, '--json')
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        added = self.run_cli('add', '--vault', vault, '--file', '-', '--role', 'implementer', '--wait', '--json',
                             content='The espresso machine in the office kitchen needs descaling every two months.\n')
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        operation = json.loads(added.stdout)['result']['operation']
        record, operation_id = operation['archive_id'], operation['id']
        self.assertEqual(operation['durable_stage'], 'archived')

        search = json.loads(self.run_cli('search', 'descaling', '--vault', vault, '--json').stdout)['result']
        self.assertEqual(search['search'], {'mode': 'hybrid', 'degraded': False, 'reason': None})
        self.assertEqual([item['id'] for item in search['items']], [record])

        first = json.loads(self.run_cli('forget', record, '--vault', vault, '--json').stdout)['result']
        self.assertEqual(first, {'id': record, 'forgotten': True,
                                 'forgotten_at': first['forgotten_at'], 'operations': [operation_id]})

        hidden = json.loads(self.run_cli('search', 'descaling', '--vault', vault, '--json').stdout)['result']
        self.assertEqual(hidden['search'], {'mode': 'hybrid', 'degraded': False, 'reason': None})
        self.assertEqual(hidden['items'], [])
        self.assertEqual(json.loads(self.run_cli('list', '--vault', vault, '--json').stdout)['result']['items'], [])
        refused = json.loads(self.run_cli('show', record, '--vault', vault, '--json').stdout)
        self.assertEqual((refused['ok'], refused['error']['code']), (False, 'E_FORGOTTEN'))

        repeated = json.loads(self.run_cli('forget', record, '--vault', vault, '--json').stdout)['result']
        self.assertFalse(repeated['forgotten'])
        self.assertEqual(repeated['forgotten_at'], first['forgotten_at'])
        unknown = json.loads(self.run_cli('forget', '00000000-0000-0000-0000-000000000000',
                                          '--vault', vault, '--json').stdout)
        self.assertEqual((unknown['ok'], unknown['error']['code']), (False, 'E_NOT_FOUND'))

        events = json.loads(self.run_cli('trace', operation_id, '--vault', vault, '--json').stdout)['result']['events']
        stages = [event['stage'] for event in events]
        self.assertEqual(stages.count('forgotten'), 1)
        self.assertLess(stages.index('archived'), stages.index('forgotten'))
        status = json.loads(self.run_cli('status', '--vault', vault, '--json').stdout)['result']
        self.assertEqual((status['archive_records'], status['forgotten_records']), (1, 1))
        self.assertTrue((vault / operation['source_path']).is_file())
        self.assertTrue(json.loads(self.run_cli('doctor', '--vault', vault, '--json').stdout)['result']['healthy'])

    def test_session_review_reports_disabled_without_model(self):
        vault = self.root/'vault'
        self.assertEqual(self.run_cli('init', '--vault', vault, '--label', 'fixture', '--json').returncode, 0)
        result = self.run_cli('sessions', 'review', '--vault', vault, '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        review = json.loads(result.stdout)['result']
        self.assertEqual(review['status'], 'disabled')
        self.assertFalse(review['semantic_completeness_guaranteed'])

    def test_installed_graph_cli_and_stdio_use_current_selected_wiki(self):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        vault = self.root / 'vault'
        initialized = self.run_cli('init', '--vault', vault, '--label', 'fixture', '--json')
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        wiki = vault / 'knowledge/wiki'
        (wiki / 'guide.md').write_text('# Guide\nRead [[detail]].\n')
        (wiki / 'detail.md').write_text('# Detail\nOriginal condition.\n')
        found = self.run_cli('graph', 'query', 'neighbors', '--id', 'guide', '--vault', vault, '--json')
        self.assertEqual(found.returncode, 0, found.stdout + found.stderr)
        self.assertEqual([node['id'] for node in json.loads(found.stdout)['result']['result']], ['detail'])

        async def exercise():
            transport = StdioTransport(command=str(LAUNCHER), args=['mcp', '--vault', str(vault)],
                                       env=self.env, cwd=str(self.root), keep_alive=False)
            async with Client(transport) as client:
                names = {tool.name for tool in await client.list_tools()}
                self.assertEqual(names, {'memory_create', 'memory_store', 'memory_update',
                                         'memory_apply_action', 'memory_search', 'memory_get', 'memory_history',
                                         'memory_list', 'memory_stats', 'memory_status', 'memory_trace',
                                         'memory_doctor', 'graph_query', 'graph_read', 'graph_stats'})
                (wiki / 'detail.md').write_text('# Detail\nChanged condition.\n')
                page = await client.call_tool('graph_read', {'entity_id': 'detail'})
                self.assertEqual(page.data['content'], '# Detail\nChanged condition.\n')
                self.assertEqual(page.data['graph']['status'], 'current')
                status = await client.call_tool('memory_status', {})
                self.assertEqual(status.data['archive_records'], 0)

        asyncio.run(asyncio.wait_for(exercise(), timeout=30))

    def test_hot_preview_and_hook_read_configured_pages_without_model(self):
        """HOT and the start hook deliver the committed page bytes; a file written beside them is not served."""
        vault = self.root / 'vault'
        self.assertEqual(self.run_cli('init', '--vault', vault, '--label', 'test', '--json').returncode, 0)
        with (vault / 'kelmo.toml').open('a') as output:
            output.write('\n[hot]\npages = ["projects/default/default.md"]\n')
        (vault / 'knowledge/wiki/selected.md').write_text('Keep the exception: only after approval.')
        result = self.run_cli('hot', '--vault', vault, '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads(result.stdout)['result']['context']
        self.assertIn('## projects/default/default.md', context)
        self.assertIn('title: "Default"', context)
        self.assertNotIn('only after approval', context)
        started = self.run_cli('hook', 'start', '--vault', vault, content='{}')
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(json.loads(started.stdout)['hookSpecificOutput']['additionalContext'], context)
        with (vault / 'kelmo.toml').open('a') as output:
            output.write('\n[hot.projects.extra]\nroots = ["/nonexistent-root"]\npages = ["selected.md"]\n')
        refused = self.run_cli('hot', '--vault', vault, '--cwd', '/nonexistent-root', '--json')
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(json.loads(refused.stdout)['error']['code'], 'E_HOT')

    def test_client_removal_preview_apply_and_repeat(self):
        from kelmo.install.setup import configure_clients
        vault = self.root / 'absent-vault'
        configure_clients(self.root, LAUNCHER, vault, 'both', True)
        command = ['uninstall', '--home', self.root, '--executable', LAUNCHER, '--vault', vault, '--json']
        preview = self.run_cli(*command)
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertFalse(json.loads(preview.stdout)['result']['applied'])
        removed = self.run_cli(*command, '--apply')
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertTrue(json.loads(removed.stdout)['result']['background_retained'])
        repeated = self.run_cli(*command, '--apply')
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)['result']['files'], [])
        self.assertFalse(vault.exists())

    def test_session_hook_capture_inspection_pause_and_skip_worker(self):
        vault=self.root/'vault'
        self.assertEqual(self.run_cli('init','--vault',vault,'--label','test','--json').returncode,0)
        native=self.root/'native'
        native.mkdir()
        source=native/'chat.jsonl'
        source.write_text(json.dumps({'type':'user','message':{'content':'Synthetic useful request.'}})+'\n')
        with (vault/'kelmo.toml').open('a') as file:
            file.write('\n[sessions]\nmode="auto"\n[sessions.sources]\nclaude='+json.dumps([str(native)])+'\n')
        captured=self.run_cli('hook','session-end','--provider','claude','--vault',vault,
            content=json.dumps({'session_id':'native','transcript_path':str(source)}))
        self.assertEqual(captured.returncode,0,captured.stdout+captured.stderr)
        listed=self.run_cli('sessions','list','--vault',vault,'--json')
        self.assertEqual(listed.returncode,0,listed.stdout+listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)['result']['jobs']),1)
        source.unlink()
        paused=self.run_cli('sessions','pause','--vault',vault,'--json')
        self.assertEqual(paused.returncode,0,paused.stdout+paused.stderr)
        self.assertTrue(json.loads(paused.stdout)['result']['paused'])
        self.assertEqual(self.run_cli('sessions','resume','--vault',vault,'--json').returncode,0)
        profile=self.root/'executor.json'
        profile.write_text(json.dumps({'argv':[str(PREFIX/'bin/python'),'-c',
            'print(\'{"skip":true,"note_markdown":"","candidate_facts":[]}\')']}))
        processed=self.run_cli('sessions','run','--vault',vault,'--executor-config',profile,'--json')
        self.assertEqual(processed.returncode,0,processed.stdout+processed.stderr)
        self.assertEqual(json.loads(processed.stdout)['result']['jobs'][0]['state'],'skipped')

    def test_installed_removal_handles_new_session_hooks(self):
        from kelmo.install.setup import configure_clients
        vault=self.root/'absent'
        configure_clients(self.root,LAUNCHER,vault,'both',True,session_mode='auto')
        result=self.run_cli('uninstall','--home',self.root,'--executable',LAUNCHER,'--vault',vault,'--apply','--json')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for relative in ('.claude/settings.json','.codex/hooks.json'):
            hooks=json.loads((self.root/relative).read_text())['hooks']
            self.assertTrue(all(not hooks.get(event) for event in ('Stop','PreCompact','SessionEnd')))

    def test_page_model_verbs_answer_through_the_installed_launcher(self):
        """The discovery verbs reach the installed runtime and describe a fresh vault's current registry.

        The launcher is what a person and an editor call, so the three verbs are measured through it and not
        only through the module: `pages` lists the project map initialization registered, `page` answers an
        unregistered target with no entry and no facts, and `context` for a project without pages selects
        nothing instead of failing.
        """
        vault = self.root / 'vault'
        self.assertEqual(self.run_cli('init', '--vault', vault, '--label', 'fixture', '--json').returncode, 0)
        pages = json.loads(self.run_cli('pages', '--vault', vault, '--json').stdout)
        self.assertTrue(pages['ok'], pages)
        self.assertEqual(pages['result'], {'version': 3, 'projects': {'default': [
            {'target': 'projects/default/default.md', 'kind': 'hub', 'title': 'Default', 'status': 'shipped'}]}})
        read = json.loads(self.run_cli('page', 'projects/fixture/fixture.md', '--vault', vault, '--json').stdout)
        self.assertTrue(read['ok'], read)
        self.assertEqual(read['result']['version'], 3)
        self.assertEqual(read['result']['page']['target'], 'projects/fixture/fixture.md')
        self.assertIsNone(read['result']['page']['entry'])
        self.assertEqual(read['result']['page']['facts'], [])
        answered = self.run_cli('context', '--project', 'fixture', '--budget-tokens', '4000',
                                '--vault', vault, '--json')
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        result = json.loads(answered.stdout)['result']
        self.assertEqual((result['project'], result['selected'], result['facts']), ('fixture', [], {}))

    def test_symlink_launcher_uses_its_own_runtime(self):
        link = self.root / 'kelmo'
        link.symlink_to(LAUNCHER)
        result = subprocess.run([str(link), 'version', '--json'], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=20,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_invalid_prefix_arguments_cannot_select_initialization(self):
        result = self.run_cli('--idempotency-key', 'init', 'add', '--vault', 'absent',
                              '--file', '-', '--json', content='Synthetic input')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_USAGE')
        self.assertFalse((self.root / 'absent').exists())

    def test_nonempty_initialization_keeps_its_domain_error(self):
        result = self.run_cli('init', '--vault', '.', '--label', 'test', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_ROOT_NOT_EMPTY')

    def test_missing_git_identity_is_actionable(self):
        (self.root / '.gitconfig').unlink()
        result = self.run_cli('init', '--vault', 'missing-identity', '--label', 'test', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_GIT_IDENTITY')
        self.assertFalse((self.root / 'missing-identity').exists())

    def test_relative_paths_capture_and_failed_trace(self):
        created = self.run_cli('init', '--vault', 'vault', '--label', 'test', '--json')
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        (self.root / 'input.md').write_text('Synthetic local evidence.')
        result = self.run_cli('add', '--vault=vault', '--file', 'input.md',
                              '--role', 'lead', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        op = json.loads(result.stdout)['operation']
        (self.root / 'vault' / op['source_path']).write_text('Synthetic changed input')
        failed = self.run_cli('worker', 'run', op['id'], '--vault', 'vault', '--json')
        self.assertEqual(failed.returncode, 1)
        trace = self.run_cli('trace', op['id'], '--vault', 'vault', '--json')
        self.assertEqual(trace.returncode, 0, trace.stdout + trace.stderr)
        record = json.loads(trace.stdout)
        self.assertTrue(record['ok'])
        self.assertEqual(record['result']['events'][-1]['details']['phase'], 'source')
        human = self.run_cli('trace', op['id'], '--vault', 'vault')
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('"events"', human.stdout)
        queued = self.run_cli('queue', 'list', '--vault', 'vault', '--json')
        self.assertEqual(queued.returncode, 0, queued.stdout + queued.stderr)
        self.assertEqual(json.loads(queued.stdout)['result']['operations'][0]['id'], op['id'])
        retry = shlex.split(json.loads(failed.stdout)['error']['retry'][0]['command'])
        self.assertEqual(retry[0], str(LAUNCHER))
        retried = subprocess.run([*retry, '--json'], cwd=self.root, env=self.env,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)['operation']['id'], op['id'])

    def test_config_validation_does_not_require_archive_health(self):
        created = self.run_cli('init', '--vault', 'vault', '--label', 'test', '--json')
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        (self.root / 'vault/archive/archive.db').unlink()
        result = self.run_cli('config', 'validate', '--vault', 'vault', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['result']['valid'])
        self.assertNotEqual(self.run_cli('doctor', '--vault', 'vault', '--json').returncode, 0)

    def test_startup_injection_is_not_loaded(self):
        site = next((PREFIX / 'lib').glob('python*/site-packages'))
        injection = site / 'kelmo_test_injection.pth'
        self.assertFalse(injection.exists())
        marker = self.root / 'unexpected-startup'
        try:
            injection.write_text('import pathlib; pathlib.Path(' + repr(str(marker)) + ').write_text("executed")\n')
            result = self.run_cli('version', '--json')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
        finally:
            injection.unlink(missing_ok=True)

    def test_missing_entry_is_rejected_before_interpreter_runs(self):
        prefix = self.root / 'fake-install'
        (prefix / 'bin').mkdir(parents=True)
        (prefix / 'share/kelmo').mkdir(parents=True)
        launcher = prefix / 'bin/kelmo'
        shutil.copyfile(LAUNCHER, launcher)
        launcher.chmod(0o755)
        marker = self.root / 'unexpected-interpreter'
        python = prefix / 'bin/python'
        python.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        python.chmod(0o755)
        # Deliberately omit entry.py: this is an incomplete-install guard,
        # not authentication of the interpreter in a complete runtime.
        result = subprocess.run([str(launcher), 'version', '--json'], env=self.env,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'E_RUNTIME')


if __name__ == '__main__':
    unittest.main()
