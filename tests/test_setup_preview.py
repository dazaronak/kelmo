import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from kelmo.errors import KelmoError
from kelmo.interface import build_parser
from kelmo.install.setup import configure_clients, service_file, setup


def args(tmp_path, *extra):
    return build_parser('')[0].parse_args([
        'setup', '--dry-run', '--vault', str(tmp_path/'vault'), '--home', str(tmp_path/'home'),
        '--executable', str(tmp_path/'runtime/bin/kelmo'), '--disable-native-memory',
        '--no-start', *extra])


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_empty_profile_preview_has_no_writes_or_provider_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('subprocess invoked'))
    before = tree(tmp_path)
    result = setup(args(tmp_path, '--provider', 'codex', '--model', 'synthetic',
                        '--session-memory', 'auto'))
    assert result['dry_run'] and not result['complete']
    assert result['session_memory_mode'] == 'auto'
    assert len(result['files']) == 9
    assert all(row['action'] == 'create' for row in result['files'])
    assert tree(tmp_path) == before
    assert list(tmp_path.iterdir()) == []


def test_preview_asks_for_a_project_only_when_none_was_requested(tmp_path):
    repo = tmp_path/'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    plain = setup(args(tmp_path/'plain'))
    linked = setup(args(tmp_path/'linked', '--project', 'demo', '--link', str(repo)))
    project_only = setup(args(tmp_path/'project-only', '--project', 'demo'))
    assert any('No repository is linked' in warning for warning in project_only['warnings'])
    assert any('No repository is linked' in warning for warning in plain['warnings'])
    assert any('kelmo project add' in step for step in plain['next_steps'])
    assert not any('project' in warning.lower() for warning in linked['warnings'])
    assert not any('kelmo project add' in step for step in linked['next_steps'])


def test_existing_client_preview_is_idempotent_and_does_not_expose_configuration(tmp_path):
    options = args(tmp_path)
    home = tmp_path/'home'
    home.mkdir()
    (home/'.claude.json').write_text(json.dumps({'retained_secret': 'synthetic-private-marker'}))
    configure_clients(home, options.executable, options.vault, 'both', True, 'off')
    service_file(home, options.executable, options.vault, sys.platform, None)
    before = tree(tmp_path)
    result = setup(options)
    assert all(row['action'] == 'unchanged' for row in result['files'])
    assert tree(tmp_path) == before
    assert 'synthetic-private-marker' not in json.dumps(result)


def test_preview_native_policy_change_matches_apply(tmp_path):
    options = args(tmp_path)
    configure_clients(options.home, options.executable, options.vault, 'both', False, 'off')
    result = setup(options)
    instructions = [row for row in result['files'] if row['path'].endswith(('CLAUDE.md', 'AGENTS.md'))]
    assert all(row['action'] == 'update' and row['backup_on_apply'] for row in instructions)
    configure_clients(options.home, options.executable, options.vault, 'both', True, 'off')
    result = setup(options)
    assert all(row['action'] == 'unchanged' for row in result['files']
               if row['path'].endswith(('CLAUDE.md', 'AGENTS.md')))


@pytest.mark.parametrize('kind', ['directory', 'fifo', 'symlink'])
def test_preview_rejects_nonregular_instruction_paths_without_writes(tmp_path, kind):
    path = tmp_path/'home/.claude/CLAUDE.md'
    path.parent.mkdir(parents=True)
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.symlink_to(tmp_path/'outside')
    with pytest.raises(KelmoError):
        setup(args(tmp_path, '--client', 'claude'))
    assert not (tmp_path/'vault').exists()
    assert not (tmp_path/'home/.claude.json').exists()


def test_preview_existing_wiki_is_refused_and_retains_bytes(tmp_path):
    wiki = tmp_path/'wiki'
    wiki.mkdir()
    (wiki/'page.md').write_bytes(b'Human maintained page.\n')
    before = tree(tmp_path)
    with pytest.raises(KelmoError, match='empty wiki directory'):
        setup(args(tmp_path, '--wiki', str(wiki)))
    assert tree(tmp_path) == before


def test_preview_client_conflict_does_not_initialize_vault(tmp_path):
    config = tmp_path/'home/.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.kelmo]\ncommand="unmanaged"\n')
    before = tree(tmp_path)
    with pytest.raises(KelmoError, match='unmanaged'):
        setup(args(tmp_path))
    assert tree(tmp_path) == before
    assert not (tmp_path/'vault').exists()


def test_checkout_dry_run_works_without_dependencies_and_creates_no_runtime(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-I', '-S', str(repo/'install.py'),
        '--dry-run', '--disable-native-memory', '--no-start', '--home', str(tmp_path/'home'),
        '--vault', str(tmp_path/'vault'), '--prefix', str(tmp_path/'runtime')],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)['result']
    assert plan['runtime']['action'] == 'create'
    assert not (tmp_path/'runtime').exists()
    assert not (tmp_path/'home').exists()
    assert not (tmp_path/'vault').exists()


def test_installed_setup_preview_uses_same_envelope(tmp_path, capsys):
    from kelmo.cli import main
    code = main(['setup', '--dry-run', '--disable-native-memory', '--json', '--no-start',
                 '--home', str(tmp_path/'home'), '--vault', str(tmp_path/'vault'),
                 '--executable', str(tmp_path / 'kelmo')])
    assert code == 0
    assert json.loads(capsys.readouterr().out)['result']['dry_run']
    assert list(tmp_path.iterdir()) == []
