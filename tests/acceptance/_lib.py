"""Shared helpers for the frozen acceptance scenarios in docs/TARGET-STATE.md Part 2.

Every scenario script imports this module, builds its own fresh vault, runs the
exact commands of its scenario and records one line per "Pass when" sentence.
The scripts are a fixed contract: code changes are judged by them and never edit
them (see TARGET-STATE, Part 2, opening paragraph).

Environment:
  KELMO_MODEL_DIR   directory with the five pinned model files (required)
  KELMO_ACCEPTANCE_ROOT  where vaults are created (default: a temp dir)
  KELMO_PYTHON      interpreter to run the CLI (default: this interpreter)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / 'src'
STUB = Path(__file__).resolve().parent / 'stub_executor.py'
PYTHON = os.environ.get('KELMO_PYTHON') or sys.executable


class Failure(Exception):
    pass


class Scenario:
    """Collects checks; prints PASS/FAIL in the agreed format."""

    def __init__(self, code, title):
        self.code = code
        self.title = title
        self.failures = []
        root = os.environ.get('KELMO_ACCEPTANCE_ROOT')
        if root:
            self.root = Path(root).resolve() / code.lower()
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True)
        else:
            self.root = Path(tempfile.mkdtemp(prefix='kelmo-' + code.lower() + '-')).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ)
        self.env.update({
            'PYTHONPATH': str(SRC),
            'HOME': str(self.home),
            'GIT_AUTHOR_NAME': 'acceptance', 'GIT_AUTHOR_EMAIL': 'acceptance@example.invalid',
            'GIT_COMMITTER_NAME': 'acceptance', 'GIT_COMMITTER_EMAIL': 'acceptance@example.invalid',
            'GIT_CONFIG_GLOBAL': str(self.home / '.gitconfig'),
        })
        self.env.pop('KELMO_INTERNAL_JOB', None)

    # --- running the CLI -------------------------------------------------
    def memory(self, *args, stdin=None, timeout=300):
        """Run `kelmo ... --json` from source and return (parsed JSON, returncode)."""
        argv = [PYTHON, '-m', 'kelmo.cli', *map(str, args)]
        if '--json' not in argv:
            argv.append('--json')
        completed = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                                   env=self.env, timeout=timeout, check=False)
        try:
            payload = json.loads(completed.stdout) if completed.stdout.strip() else None
        except ValueError:
            payload = None
        if payload is None:
            self.note(f'non-JSON stdout for {" ".join(map(str, args))}: {completed.stdout[:200]!r} stderr={completed.stderr[:200]!r}')
        return payload, completed.returncode

    def result(self, *args, stdin=None):
        """Run a command that must succeed and return its `result`."""
        payload, code = self.memory(*args, stdin=stdin)
        if not payload or payload.get('ok') is not True:
            raise Failure(f'{" ".join(map(str, args))} did not succeed: {json.dumps(payload)[:300]}')
        return payload['result']

    # --- vaults ----------------------------------------------------------
    def fresh_vault(self, name='v'):
        vault = self.root / name
        self.result('init', '--vault', vault, '--label', 'acceptance',
                    '--project', 'demo', '--non-interactive')
        model = os.environ.get('KELMO_MODEL_DIR')
        if not model:
            raise Failure('KELMO_MODEL_DIR is not set; the five pinned model files are required')
        self.result('models', 'import', '--vault', vault, '--from', model)
        shutil.copyfile(self.executor_config(), vault / '.state' / 'executor.json')
        return vault

    def append_toml(self, vault, text):
        with (Path(vault) / 'kelmo.toml').open('a', encoding='utf-8') as handle:
            handle.write('\n' + text.strip('\n') + '\n')

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    # --- notes -----------------------------------------------------------
    def reconcile(self, vault, text, target='projects/demo/demo.md'):
        return self.result('reconcile', '--vault', vault, '--text', text, '--target', target)

    def promotion_note(self, path, key, text, search_id, *, target='projects/demo/demo.md',
                       expected_revision=None, existing=None):
        """Build a note exactly as docs/NOTE-CONTRACT.md describes.

        `existing` = (fact_key, revision) switches the check to result=existing.
        """
        change = {'fact_key': key, 'target': target, 'text': text, 'status': 'active',
                  'expected_revision': expected_revision, 'replaces': []}
        if existing:
            check = {'fact_key': key, 'verified_match': {'result': 'existing', 'fact_key': existing[0],
                                                         'revision': existing[1]}}
        else:
            check = {'fact_key': key, 'verified_match': {'result': 'not_found', 'search_id': search_id}}
        body = ('---\nproject: demo\ndomain: dev\nsource: manual\n---\n\n' + text + '\n\n'
                '```wiki-changes\n' + json.dumps([change], indent=2) + '\n```\n'
                '```fact-checks\n' + json.dumps([check], indent=2) + '\n```\n')
        return self.write(path, body)

    def promote_new_fact(self, vault, key, text, name, wait=True):
        receipt = self.reconcile(vault, text)
        note = self.promotion_note(self.root / name, key, text, receipt['search_id'])
        args = ['promote', '--vault', vault, '--file', note, '--role', 'lead']
        if wait:
            args.append('--wait')
        return self.memory(*args)

    def executor_config(self):
        path = self.root / 'executor.json'
        path.write_text(json.dumps({'argv': [PYTHON, str(STUB)], 'timeout_seconds': 60,
                                    'max_attempts': 3, 'poll_seconds': 1}))
        return path

    def published_hot_pages(self, vault):
        """Two demo pages in a committed publication: configured HOT reads the generation, not working files."""
        program = """
import json
import sys
from kelmo.context_binding import ContextBindings
from kelmo.promotion import DeclaredChanges
from kelmo.service import Service

paths = ['projects/demo/demo.md', 'projects/demo/setup.md']
with Service(sys.argv[1]) as service:
    contexts = ContextBindings(service.bindings, actions=(), repository_remotes={}, worktrees={},
                               feature_pages={}, producer_client_ids=())
    source = service.capture('The demo project has a setup reference page.', 'hot-fixture.md', role='implementer')
    archived = service.run(source['id'])
    if archived['state'] != 'archived':
        raise RuntimeError('HOT fixture source was not archived')
    receipt = service.editor.edit(
        source['id'], origin=service._operation_origin(source['id']), idempotency_key='acceptance-hot-pages',
        project='demo', selected=[paths[0]], budget_tokens=24000, contexts=contexts,
        executor=DeclaredChanges({'pages': [{'target': paths[1], 'expected_revision': None, 'page_id': 'demo-setup',
                                             'layout': 'topic', 'title': 'Setup', 'summary': 'Demo setup reference.',
                                             'parent': paths[0]}]}))
    if receipt.outcome != 'published':
        raise RuntimeError('HOT fixture pages were not published')
    generation = service.knowledge.generation()
    print(json.dumps([service.knowledge.page(path, generation).content.decode('utf-8') for path in paths]))
"""
        completed = subprocess.run([PYTHON, '-c', program, str(vault)], capture_output=True, text=True,
                                   env=self.env, check=False)
        if completed.returncode:
            raise Failure('HOT fixture publication failed: ' + completed.stderr)
        return tuple(json.loads(completed.stdout))

    # --- checks ----------------------------------------------------------
    def check(self, condition, sentence):
        """Record a "Pass when" sentence; the first failing sentence is quoted in the FAIL line."""
        if not condition:
            self.failures.append(sentence)
        return bool(condition)

    def note(self, text):
        print(f'  note: {text}', file=sys.stderr)

    def finish(self):
        if self.failures:
            print(f'FAIL {self.code}: {self.failures[0]}')
            for extra in self.failures[1:]:
                print(f'  also: {extra}')
            return 1
        print(f'PASS {self.code}')
        return 0

    def run(self, body):
        try:
            body(self)
        except Failure as error:
            self.failures.append(f'scenario could not run its steps: {error}')
        except Exception as error:  # a crash is a failure, not a pass
            import traceback
            traceback.print_exc()
            self.failures.append(f'scenario crashed: {type(error).__name__}: {error}')
        code = self.finish()
        if not os.environ.get('KELMO_ACCEPTANCE_KEEP'):
            shutil.rmtree(self.root, ignore_errors=True)
        return code


# --- MCP over stdio, newline-delimited JSON-RPC -----------------------------
class McpServer:
    """A child `kelmo mcp --vault V` process driven with raw JSON-RPC over stdio."""

    def __init__(self, scenario, vault):
        self.scenario = scenario
        self.process = subprocess.Popen(
            [PYTHON, '-m', 'kelmo.cli', 'mcp', '--vault', str(vault)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=scenario.env, bufsize=1)
        self.next_id = 1
        self._send({'jsonrpc': '2.0', 'id': self._id(), 'method': 'initialize',
                    'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': 'acceptance', 'version': '1'}}})
        self._receive()
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def _id(self):
        value = self.next_id
        self.next_id += 1
        return value

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def _receive(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise Failure('MCP server closed its stdout')
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if 'id' in message:
                return message
        raise Failure('MCP server did not answer in time')

    def list_tools(self):
        self._send({'jsonrpc': '2.0', 'id': self._id(), 'method': 'tools/list', 'params': {}})
        return [tool['name'] for tool in self._receive()['result']['tools']]

    def call(self, name, **arguments):
        """Return (ok, data): ok=False for a tool error, data is the parsed result or the error text."""
        self._send({'jsonrpc': '2.0', 'id': self._id(), 'method': 'tools/call',
                    'params': {'name': name, 'arguments': arguments}})
        message = self._receive()
        if 'error' in message:
            return False, message['error']
        result = message['result']
        if result.get('isError'):
            return False, ' '.join(item.get('text', '') for item in result.get('content', []))
        if 'structuredContent' in result:
            data = result['structuredContent']
            if isinstance(data, dict) and set(data) == {'result'}:
                data = data['result']
            return True, data
        text = ' '.join(item.get('text', '') for item in result.get('content', []))
        try:
            return True, json.loads(text)
        except ValueError:
            return True, text

    def kill(self):
        self.process.kill()
        self.process.wait(timeout=30)

    def stop(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def git(vault, *args):
    completed = subprocess.run(['git', '-C', str(vault), *args], capture_output=True, text=True, check=False)
    return completed.stdout
