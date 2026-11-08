"""Removal preserves user-owned configuration and never removes durable data."""
import json
import tomllib
from types import SimpleNamespace

import pytest

from kelmo.errors import KelmoError
from kelmo.install.setup import configure_clients


def installed(tmp_path):
    home = tmp_path / 'home'
    executable, vault = tmp_path / 'runtime/bin/kelmo', tmp_path / 'vault'
    home.mkdir()
    (home / '.claude.json').write_text('{"user_setting":42}\n')
    (home / '.codex').mkdir()
    (home / '.codex/config.toml').write_text('title = "keep"\n')
    (home / '.codex/AGENTS.md').write_text('User instructions.\n')
    configure_clients(home, executable, vault, 'both', True)
    return SimpleNamespace(home=home, executable=executable, vault=vault, client='both', apply=False)


def snapshot(home):
    return {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}


def test_preview_then_apply_removes_only_integrations_and_is_idempotent(tmp_path):
    from kelmo.install.removal import uninstall
    args = installed(tmp_path)
    args.vault.mkdir()
    (args.vault / 'evidence.md').write_text('Retained evidence.')
    unit = args.home / '.config/systemd/user/kelmo.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('Retained service.')
    before = snapshot(args.home)
    preview = uninstall(args)
    assert preview['applied'] is False
    assert len(preview['files']) == 6
    assert snapshot(args.home) == before
    args.apply = True
    result = uninstall(args)
    assert result['applied'] is True
    assert result['background_retained'] is True
    assert len(result['backups']) == 6
    assert json.loads((args.home / '.claude.json').read_text()) == {'user_setting': 42, 'mcpServers': {}}
    codex = tomllib.loads((args.home / '.codex/config.toml').read_text())
    assert codex['title'] == 'keep'
    assert codex['features']['hooks'] is True
    assert 'kelmo' not in codex.get('mcp_servers', {})
    assert (args.home / '.codex/AGENTS.md').read_text().startswith('User instructions.\n')
    assert unit.read_text() == 'Retained service.'
    assert (args.vault / 'evidence.md').read_text() == 'Retained evidence.'
    assert uninstall(args)['files'] == []
    configure_clients(args.home, args.executable, args.vault, 'both', True)
    assert len(uninstall(SimpleNamespace(**(vars(args) | {'apply': False})))['files']) == 6


@pytest.mark.parametrize('changed', ['claude_mcp', 'codex_mcp', 'instructions', 'hook'])
def test_foreign_edits_conflict_before_any_write(tmp_path, changed):
    from kelmo.install.removal import uninstall
    args = installed(tmp_path)
    if changed == 'claude_mcp':
        path = args.home / '.claude.json'
        data = json.loads(path.read_text())
        data['mcpServers']['kelmo']['args'].append('foreign')
        path.write_text(json.dumps(data))
    elif changed == 'codex_mcp':
        path = args.home / '.codex/config.toml'
        path.write_text(path.read_text().replace('command = ', 'foreign = true\ncommand = '))
    elif changed == 'instructions':
        path = args.home / '.codex/AGENTS.md'
        path.write_text(path.read_text().replace('## Memory', '## Changed memory'))
    else:
        path = args.home / '.claude/settings.json'
        data = json.loads(path.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 999
        path.write_text(json.dumps(data))
    before = snapshot(args.home)
    args.apply = True
    with pytest.raises(KelmoError):
        uninstall(args)
    assert snapshot(args.home) == before


def test_user_hooks_and_instruction_suffix_survive(tmp_path):
    from kelmo.install.removal import uninstall
    args = installed(tmp_path)
    user_hook = {'type': 'command', 'command': 'user-owned'}
    path = args.home / '.claude/settings.json'
    data = json.loads(path.read_text())
    data['hooks']['Stop'][0]['hooks'].append(user_hook)
    path.write_text(json.dumps(data))
    instructions = args.home / '.codex/AGENTS.md'
    with instructions.open('ab') as output:
        output.write(b'User suffix\r\n')
    args.apply = True
    uninstall(args)
    assert json.loads(path.read_text())['hooks']['Stop'][0]['hooks'] == [user_hook]
    assert instructions.read_bytes().endswith(b'User suffix\r\n')


def test_absent_profiles_do_not_create_files(tmp_path):
    from kelmo.install.removal import uninstall
    args = SimpleNamespace(home=tmp_path / 'absent', executable=tmp_path / 'kelmo',
                           vault=tmp_path / 'vault', client='both', apply=True)
    assert uninstall(args)['files'] == []
    assert not args.home.exists()


def test_partial_failure_can_resume(tmp_path, monkeypatch):
    from kelmo.install import removal
    args = installed(tmp_path)
    args.apply = True
    real = removal.write
    calls = []

    def fail_second(*a, **k):
        calls.append(a[0])
        if len(calls) == 2:
            raise KelmoError('E_IO', 'Synthetic interruption.')
        return real(*a, **k)

    monkeypatch.setattr(removal, 'write', fail_second)
    with pytest.raises(KelmoError):
        removal.uninstall(args)
    monkeypatch.setattr(removal, 'write', real)
    assert len(removal.uninstall(args)['files']) == 5
    assert removal.uninstall(args)['files'] == []


def test_stale_removal_plan_preserves_concurrent_changes(tmp_path):
    from kelmo.install.removal import prepare, apply
    args = installed(tmp_path)
    plan = prepare(args)
    path = args.home / '.codex/AGENTS.md'
    with path.open('ab') as output:
        output.write(b'Concurrent user edit.')
    before = snapshot(args.home)
    with pytest.raises(KelmoError):
        apply(plan)
    assert snapshot(args.home) == before


def test_uninstall_cli_preview_requires_no_vault_runtime(tmp_path, capsys):
    from kelmo.cli import main
    args = installed(tmp_path)
    assert not args.vault.exists()
    assert main(['uninstall', '--home', str(args.home), '--executable', str(args.executable),
                 '--vault', str(args.vault), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['result']['applied'] is False
    assert not args.vault.exists()


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo'])
def test_nonregular_instruction_path_is_rejected_without_mutation(tmp_path, kind):
    import os
    from kelmo.install.removal import uninstall
    args = installed(tmp_path)
    path = args.home / '.codex/AGENTS.md'
    path.unlink()
    if kind == 'symlink':
        path.symlink_to(args.home / '.claude/CLAUDE.md')
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    original = (args.home / '.claude.json').read_bytes()
    args.apply = True
    with pytest.raises(KelmoError):
        uninstall(args)
    assert (args.home / '.claude.json').read_bytes() == original


def test_removing_one_client_keeps_the_other_profile(tmp_path):
    from kelmo.install.removal import uninstall
    args = installed(tmp_path)
    before = snapshot(args.home / '.codex')
    args.client, args.apply = 'claude', True
    assert len(uninstall(args)['files']) == 3
    assert snapshot(args.home / '.codex') == before
