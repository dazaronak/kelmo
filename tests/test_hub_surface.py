import threading

import pytest

from kelmo.errors import KelmoError
from kelmo.interface import build_parser
from kelmo.install.hooks import definitions


def test_owned_block_preserves_bytes_and_backs_up(tmp_path):
    from kelmo.hub_embed import write_block, block_present
    path = tmp_path / 'AGENTS.md'
    path.write_bytes(b'header\r\nfooter\n')
    result = write_block(path, 'memory')
    assert open(result['backup'], 'rb').read() == b'header\r\nfooter\n'
    assert path.read_bytes().startswith(b'header\r\nfooter\n')
    assert block_present(path, 'memory')
    assert not write_block(path, 'memory')['changed']
    write_block(path, 'new')
    assert path.read_bytes().startswith(b'header\r\nfooter\n')
    assert block_present(path, 'new')


def test_owned_block_refuses_symlink_and_broken_markers(tmp_path):
    from kelmo.hub_embed import write_block
    target = tmp_path / 'target'
    target.write_text('untouched')
    path = tmp_path / 'AGENTS.md'
    path.symlink_to(target)
    with pytest.raises(KelmoError):
        write_block(path, 'memory')
    assert target.read_text() == 'untouched'
    path.unlink()
    path.write_text('<!-- kelmo:hub begin -->\n')
    with pytest.raises(KelmoError):
        write_block(path, 'memory')


def test_import_layout_and_commands(tmp_path):
    from kelmo.hub_embed import import_lines
    assert import_lines(tmp_path, 'demo') == f'@{tmp_path}/mandatory.md\n@{tmp_path}/demo.md'
    assert import_lines(tmp_path, 'demo', strict=True) == f'@{tmp_path}/demo.full.md'
    assert import_lines(tmp_path, 'demo', scope='global') == f'@{tmp_path}/mandatory.md'
    assert import_lines(tmp_path, 'demo', scope='project') == f'@{tmp_path}/demo.md'
    parser, _ = build_parser('test')
    assert parser.parse_args(['project', 'rename', 'old', 'new']).new == 'new'
    assert parser.parse_args(['link', '--project', 'demo', str(tmp_path)]).project == 'demo'
    assert parser.parse_args(['hub', 'embed', '--codex', '--project', 'demo']).codex
    assert parser.parse_args(['setup', '--project', 'default', '--allow-native-memory'])._project_requested


def test_prompt_hooks_include_provider_and_codex_budget():
    hook = definitions('/bin/kelmo', '/tmp/vault', 'codex', 'off')
    assert '--provider codex' in hook['UserPromptSubmit']['command']
    assert hook['UserPromptSubmit']['additionalContextLimit'] == 0
    assert hook['SessionStart']['additionalContextLimit'] == 0


def test_hub_diagnostics_reports_missing_and_oversized_views(tmp_path):
    from types import SimpleNamespace
    from kelmo.doctor import hub_diagnostics
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    hot = tmp_path / 'hot'
    hot.mkdir()
    (hot / 'demo.md').write_text('x' * 11)
    config = SimpleNamespace(wiki=wiki, state=tmp_path / 'state', hot={'projects': {'demo': {'roots': ['/repo']}},
                                             'views': {'max_bytes': 10}})
    result = hub_diagnostics(config)
    assert result['demo']['links']['roots'] == ['/repo']
    assert result['demo']['view_over_budget_bytes'] == 1
    assert result['demo']['mandatory_present'] is False
    assert 'kelmo project add demo' in result['demo']['repair']


def test_embedding_selection_sync_and_repair(tmp_path):
    from types import SimpleNamespace
    from kelmo.hub_embed import select, sync, inspect, import_lines, write_block
    config = SimpleNamespace(state=tmp_path / 'state', wiki=tmp_path / 'wiki')
    views = tmp_path / 'views'
    views.mkdir()
    (views / 'mandatory.md').write_text('<!-- kelmo generation 1 -->\nRules.\n')
    (views / 'demo.md').write_text('<!-- kelmo generation 1 -->\nHub.\n')
    instructions = tmp_path / 'AGENTS.md'
    select(config, 'demo', 'codex', instructions, views)
    assert sync(config, 'demo', 'codex', content='first')['changed']
    assert not sync(config, 'demo', 'codex', content='first')['changed']
    assert sync(config, 'demo', 'codex', content='second')['changed']
    assert 'second' in instructions.read_text()
    assert inspect(config)['demo']['codex']['verified']
    instructions.write_text(instructions.read_text().replace('second', 'tampered'))
    assert not inspect(config)['demo']['codex']['verified']
    assert sync(config, 'demo', 'codex', content='second')['verified']
    claude = tmp_path / 'CLAUDE.md'
    lines = import_lines(views, 'demo', scope='project')
    write_block(claude, lines)
    select(config, 'demo', 'claude', claude, views, scope='project')
    global_file = tmp_path / 'global' / 'CLAUDE.md'
    write_block(global_file, import_lines(views, 'demo', scope='global'))
    select(config, 'demo', 'claude', global_file, views, scope='global')
    assert sync(config, 'demo', 'claude')['verified']
    claude.write_text('removed')
    status = inspect(config)['demo']['claude']
    assert not status['verified']
    assert 'hub import' in status['repair']


def test_hook_acknowledged_only_after_flush(monkeypatch):
    import io
    import sys
    from kelmo import cli
    ack = []
    monkeypatch.setattr(cli, 'dispatch', lambda *_: {'hookSpecificOutput': {}, '_kelmo_ack': lambda: ack.append(True)})
    class Broken(io.StringIO):
        def flush(self):
            raise OSError('broken output')
    output = Broken()
    monkeypatch.setattr(sys, 'stdout', output)
    assert cli.call(['hook', 'start']) != 0
    assert ack == []
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    assert cli.call(['hook', 'start']) == 0
    assert ack == [True]


def test_setup_project_preview_and_apply_use_same_plan(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from kelmo import projects, project_preview
    from kelmo.install.setup import project_plan, apply_project
    calls = []
    monkeypatch.setattr(project_preview, 'preview', lambda root, name, directory, **kwargs: {
        'files': [{'path': str(root / 'kelmo.toml'), 'action': 'update'}]}, raising=False)
    monkeypatch.setattr(projects, 'add', lambda root, name: calls.append(('add', root, name)), raising=False)
    monkeypatch.setattr(projects, 'link', lambda root, name, directory: calls.append(('link', root, name, directory)), raising=False)
    args = SimpleNamespace(project='demo', link=tmp_path / 'repo')
    plan = project_plan(args, tmp_path)
    assert calls == []
    assert plan['files'][0]['path'] == str(tmp_path / 'kelmo.toml')
    apply_project(args, tmp_path)
    assert [call[0] for call in calls] == ['add', 'link']


def test_hook_order_is_checked_for_explicit_profile(tmp_path):
    import json
    from types import SimpleNamespace
    from kelmo.hub_embed import select, inspect
    config = SimpleNamespace(state=tmp_path / 'state')
    hooks = tmp_path / 'hooks.json'
    managed = {'statusMessage': 'kelmo managed hook', 'command': 'kelmo hook start'}
    prompt = {'statusMessage': 'kelmo managed hook', 'command': 'kelmo hook prompt'}
    external = {'command': 'compiler'}
    hooks.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [managed, external]}],
                                         'UserPromptSubmit': [{'hooks': [prompt, external]}]}}))
    target = tmp_path / 'AGENTS.md'
    from kelmo.hub_embed import write_block
    write_block(target, 'memory')
    select(config, 'demo', 'codex', target, tmp_path, hook_file=hooks)
    assert inspect(config)['demo']['codex']['hook_order'] == 'kelmo_first'
    hooks.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [external, managed]}]}}))
    assert inspect(config)['demo']['codex']['hook_order'] == 'repair_needed'


def test_codex_refresh_only_touches_selected_repository(tmp_path):
    from types import SimpleNamespace
    from kelmo.hub_embed import select, sync, write_block
    config = SimpleNamespace(state=tmp_path / 'state')
    first, second = tmp_path / 'first' / 'AGENTS.md', tmp_path / 'second' / 'AGENTS.md'
    write_block(first, 'old first')
    write_block(second, 'old second')
    select(config, 'demo', 'codex', first, tmp_path)
    select(config, 'demo', 'codex', second, tmp_path)
    sync(config, 'demo', 'codex', content='new first', cwd=first.parent)
    assert 'new first' in first.read_text()
    assert 'old second' in second.read_text()
    with pytest.raises(KelmoError):
        sync(config, 'demo', 'codex', content='ambiguous')


def test_local_hook_passes_pending_reader_and_closes_it(monkeypatch, tmp_path):
    import io
    import sys
    from types import SimpleNamespace
    from kelmo import cli, integration
    from kelmo.config import Config
    seen = []
    fake_journal = SimpleNamespace(conn=SimpleNamespace(close=lambda: seen.append('closed')))
    monkeypatch.setattr(Config, 'load', lambda root: SimpleNamespace(state=tmp_path))
    monkeypatch.setattr(cli, '_readonly_hook_journal', lambda config: fake_journal, raising=False)
    monkeypatch.setattr(integration, 'hook', lambda service, action, payload, **kwargs:
                        seen.append(service.journal) or {'hookSpecificOutput': {}})
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    parser, commands = build_parser('test')
    cli.dispatch(parser.parse_args(['hook', 'start', '--vault', str(tmp_path)]), commands)
    assert seen == [fake_journal, 'closed']


def test_claude_verification_requires_present_same_generation_imports(tmp_path):
    from types import SimpleNamespace
    from kelmo.hub_embed import select, sync, import_lines, write_block
    config = SimpleNamespace(state=tmp_path / 'state')
    views = tmp_path / 'views'
    views.mkdir()
    target = tmp_path / 'CLAUDE.md'
    write_block(target, import_lines(views, 'demo'))
    select(config, 'demo', 'claude', target, views)
    assert not sync(config, 'demo', 'claude')['verified']
    (views / 'mandatory.md').write_text('<!-- kelmo generation 1 -->\nRules.\n')
    (views / 'demo.md').write_text('<!-- kelmo generation 2 -->\nHub.\n')
    assert not sync(config, 'demo', 'claude')['verified']
    (views / 'demo.md').write_text('<!-- kelmo generation 1 -->\nHub.\n')
    assert sync(config, 'demo', 'claude')['verified']


@pytest.mark.parametrize('failure', ['open', 'query'])
def test_pending_journal_failure_preserves_hot_and_retries(tmp_path, monkeypatch, failure):
    import io
    import json
    import sqlite3
    import sys
    from types import SimpleNamespace
    from tests.test_hot import config
    from tests.test_hub_delivery import snapshot
    from kelmo import cli, hot, creation
    selected = config(tmp_path, '[hot]\nviews=true\n')
    monkeypatch.setattr(hot, 'adopted', lambda *args: True)
    monkeypatch.setattr(hot, 'has_hub_generation', lambda *args: True)
    monkeypatch.setattr(hot, 'hub_context', lambda *args, **kwargs: {
        'hub_set': snapshot(1, 0), 'project': 'alpha', 'hub_work': '', 'hub_work_path': None,
        'freshness': 'current', 'warning': ''})
    callbacks = []
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic journal lock')
    def invoke():
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'test'})))
        parser, commands = build_parser('test')
        args = parser.parse_args(['hook', 'start', '--provider', 'codex', '--vault', str(selected.root)])
        args._kelmo_acknowledge = callbacks.append
        return cli.dispatch(args, commands)['hookSpecificOutput']['additionalContext']
    reader = SimpleNamespace(conn=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, '_readonly_hook_journal', broken if failure == 'open' else lambda config: reader)
    monkeypatch.setattr(creation, 'pending_operations', broken)
    context = invoke()
    assert 'hub alpha' in context and 'Pending updates unavailable' in context
    assert not callbacks
    monkeypatch.setattr(cli, '_readonly_hook_journal', lambda config: reader)
    monkeypatch.setattr(creation, 'pending_operations', lambda *args, **kwargs: [
        ({'id': 'note', 'state': 'queued'}, {'project': 'alpha', 'pending': 'RECOVERED_PENDING'})])
    assert 'RECOVERED_PENDING' in invoke()
    assert len(callbacks) == 1


def test_legacy_start_budget_includes_pending_and_preserves_full_readable_context(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from tests.test_hot import config
    from kelmo import creation, integration
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    original = 'LEGACY_CONTEXT_' + 'x' * 9500
    (selected.wiki/'global.md').write_text(original)
    monkeypatch.setattr(creation, 'pending_operations', lambda *args, **kwargs: [
        ({'id': str(i), 'state': 'queued'}, {'project': 'app', 'pending': 'P' * 240}) for i in range(3)])
    output = integration.hook(SimpleNamespace(root=selected.root, journal=object()), 'start',
                              {'kelmo_provider': 'claude'})['hookSpecificOutput']['additionalContext']
    assert len(output) <= 10000
    copies = [path for path in selected.state.rglob('*.json')
              if original in json.loads(path.read_text()).get('context', '')]
    assert len(copies) == 1 and str(copies[0]) in output


def test_local_hook_omission_pointer_uses_verified_copy(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from tests.test_hot import config
    from tests.test_hub_delivery import snapshot
    from kelmo import hot, integration
    selected = config(tmp_path, '[hot]\nviews=true\n')
    value = snapshot(1, 0)
    text = '<!-- kelmo generation 1 -->\nVERIFIED_HUB_' + 'x' * 12000
    value['files']['alpha.md'] = {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    old = selected.wiki.parent/'hot'
    old.mkdir()
    (old/'alpha.md').write_text('UNPUBLISHED_TAMPER')
    monkeypatch.setattr(hot, 'adopted', lambda *args: True)
    monkeypatch.setattr(hot, 'has_hub_generation', lambda *args: True)
    monkeypatch.setattr(hot, 'hub_context', lambda *args, **kwargs: {
        'hub_set': value, 'project': 'alpha', 'hub_work': '', 'hub_work_path': None,
        'freshness': 'current', 'warning': ''})
    output = integration.hook(SimpleNamespace(root=selected.root), 'start', {'kelmo_provider': 'claude'})['hookSpecificOutput']['additionalContext']
    copies = [path for path in selected.state.rglob('alpha.md') if path.read_text() == text]
    assert copies and any(str(path) in output for path in copies)
    assert 'UNPUBLISHED_TAMPER' not in output and str(old/'alpha.md') not in output


@pytest.mark.parametrize('backend', ['local', 'shared'])
@pytest.mark.parametrize('action,size', [('start', 11000), ('prompt', 9950), ('prompt', 11000)])
@pytest.mark.parametrize('storage_failure', [False, True])
def test_legacy_hook_preserves_final_context_or_reports_storage_failure(
        tmp_path, monkeypatch, backend, action, size, storage_failure):
    import json
    import time
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tests.test_hot import config
    from kelmo import hot, creation, integration, client, hub_delivery
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    pending = [{'pending': 'PENDING_SENTINEL', 'project': 'app'}]
    original = 'FULL_SENTINEL_' + 'x' * (size - len(hot.pending_block(pending)) - 15)
    delivered = {'context': original, 'project': 'app', 'pending': pending}
    monkeypatch.setattr(hot, 'render', lambda *args, **kwargs: delivered)
    monkeypatch.setattr(creation, 'pending_operations', lambda *args, **kwargs: [
        ({'id': 'one', 'state': 'queued'}, pending[0])])
    if storage_failure:
        def unavailable(*args, **kwargs):
            raise OSError('synthetic unavailable context store')
        monkeypatch.setattr(hub_delivery, 'context_reference', unavailable)
    class Client:
        profile = selected
        _deadline = None
        _channel = object()
        files = threading.RLock()
        @contextmanager
        def deadline(self, seconds):
            self._deadline = time.monotonic() + seconds
            yield
        def flush(self):
            return {'reached': True, 'stopped': None}
        def context(self, *args, **kwargs):
            return delivered
    callbacks = []
    result = (integration.hook(SimpleNamespace(root=selected.root, journal=object()), action,
                              {'kelmo_provider': 'claude'}, acknowledge=callbacks.append)
              if backend == 'local' else client.hook(Client(), action, {}, 'claude', acknowledge=callbacks.append))
    output = result['hookSpecificOutput']['additionalContext']
    assert len(output) <= 10000 and not callbacks
    if storage_failure:
        assert 'unavailable' in output and 'No selected pages were delivered' in output
    else:
        copies = list((selected.state / 'hub-context').glob('*.json'))
        assert len(copies) == 1 and str(copies[0]) in output
        saved = json.loads(copies[0].read_text())
        assert original in saved['context'] and 'PENDING_SENTINEL' in saved['context']
        if action == 'prompt':
            assert 'restart' in output.lower() and 'restart' in saved['context'].lower()


def test_local_fitting_hook_does_not_need_a_view_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.test_hot import config
    from tests.test_hub_delivery import snapshot
    from kelmo import hot, integration, hub_delivery
    selected = config(tmp_path, '[hot]\nviews=true\n')
    monkeypatch.setattr(hot, 'adopted', lambda *args: True)
    monkeypatch.setattr(hot, 'has_hub_generation', lambda *args: True)
    monkeypatch.setattr(hot, 'hub_context', lambda *args, **kwargs: {
        'hub_set': snapshot(1, 0), 'project': 'alpha', 'hub_work': '', 'hub_work_path': None,
        'freshness': 'current', 'warning': ''})
    def unavailable(*args, **kwargs):
        raise OSError('synthetic unavailable cache')
    monkeypatch.setattr(hub_delivery, 'install_set', unavailable)
    output = integration.hook(SimpleNamespace(root=selected.root), 'start', {})['hookSpecificOutput']['additionalContext']
    assert 'mandatory' in output and 'hub alpha' in output


def test_owned_block_is_separated_from_user_text_by_one_blank_line(tmp_path):
    from kelmo.hub_embed import write_block, block_present
    path = tmp_path / 'AGENTS.md'
    path.write_text('# Notes\n\nUser line.\n')
    write_block(path, 'memory')
    assert path.read_text() == '# Notes\n\nUser line.\n\n<!-- kelmo:hub begin -->\nmemory\n<!-- kelmo:hub end -->\n'
    assert block_present(path, 'memory')
    # Already separated text gains no second blank line; an empty file gets the block alone.
    path.write_text('User line.\n\n')
    write_block(path, 'memory')
    assert path.read_text().startswith('User line.\n\n<!-- kelmo:hub begin -->')
    empty = tmp_path / 'EMPTY.md'
    write_block(empty, 'memory')
    assert empty.read_text().startswith('<!-- kelmo:hub begin -->')


def test_hook_output_stands_when_the_delivery_record_cannot_be_written(monkeypatch, capsys):
    import json
    from kelmo import cli

    def failing():
        raise OSError('read-only hub-sessions')
    monkeypatch.setattr(cli, 'dispatch', lambda *_: {'hookSpecificOutput': {'x': 1}, '_kelmo_ack': failing})
    assert cli.call(['hook', 'start']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip().splitlines()[-1]) == {'hookSpecificOutput': {'x': 1}}
    assert 'delivery record not written' in captured.err
