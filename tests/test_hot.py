"""Explicit, whole-page HOT selection without archive/model startup."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kelmo.config import Config
from kelmo.errors import KelmoError


def config(tmp_path, hot=''):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'knowledge/wiki').mkdir(parents=True)
    (root / 'kelmo.toml').write_text(
        'schema_version = 1\nwriter = "local"\n'
        'vault_id = "00000000-0000-0000-0000-000000000001"\nnode_label = "test"\n' + hot)
    return Config.load(root)


def test_unconfigured_hot_does_not_dump_registry(tmp_path):
    from kelmo.hot import render
    result = render(config(tmp_path))
    assert result['configured'] is False
    assert result['context'] == ''


def test_project_worktrees_order_deduplication_and_refresh(tmp_path):
    from kelmo.hot import render
    selected = config(tmp_path, '''
[hot]
pages = ["global.md", "shared.md"]
[hot.projects.app]
roots = ["../app", "../worktrees/app"]
pages = ["shared.md", "app.md"]
[hot.projects.nested]
roots = ["../app/nested"]
pages = ["nested.md"]
''')
    for page in ('global', 'shared', 'app', 'nested'):
        (selected.wiki / (page + '.md')).write_text(page + ' evidence\n')
    first = render(selected, tmp_path / 'app/src')
    worktree = render(selected, tmp_path / 'worktrees/app/src')
    assert first == worktree
    assert first['project'] == 'app'
    assert [p['path'] for p in first['pages']] == ['global.md', 'shared.md', 'app.md']
    assert render(selected, tmp_path / 'application')['project'] is None
    assert render(selected, tmp_path / 'app/nested/src')['project'] == 'nested'
    assert render(selected)['project'] is None
    (selected.wiki / 'app.md').write_text('changed condition\n')
    assert render(selected, tmp_path / 'app')['fingerprint'] != first['fingerprint']


def test_budget_counts_complete_utf8_rendering_without_truncation(tmp_path):
    from kelmo.hot import render
    selected = config(tmp_path, '[hot]\npages = ["a.md"]\nmax_bytes = 4096\n')
    (selected.wiki / 'a.md').write_text('条件\n')
    result = render(selected)
    size = len(result['context'].encode())
    assert result['bytes'] == size
    path = selected.root / 'kelmo.toml'
    path.write_text(path.read_text().replace('4096', str(size)))
    assert render(Config.load(selected.root))['context'] == result['context']
    path.write_text(path.read_text().replace(f'max_bytes = {size}', f'max_bytes = {size - 1}'))
    with pytest.raises(KelmoError, match='budget'):
        render(Config.load(selected.root))


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'directory', 'invalid_utf8'])
def test_bad_page_rejects_entire_selection(tmp_path, kind):
    from kelmo.hot import render
    selected = config(tmp_path, '[hot]\npages = ["good.md", "bad.md"]\n')
    (selected.wiki / 'good.md').write_text('VALID_PRIVATE_SENTINEL')
    bad = selected.wiki / 'bad.md'
    if kind == 'symlink':
        bad.symlink_to(selected.wiki / 'good.md')
    elif kind == 'directory':
        bad.mkdir()
    elif kind == 'invalid_utf8':
        bad.write_bytes(b'\xff')
    with pytest.raises(KelmoError) as error:
        render(selected)
    assert 'VALID_PRIVATE_SENTINEL' not in str(error.value)


@pytest.mark.parametrize('hot', [
    '[hot]\nmax_bytes = true', '[hot]\nmax_bytes = 0',
    '[hot]\npages = ["../out.md"]', '[hot]\npages = ["/out.md"]',
    '[hot]\npages = ["secret.txt"]', '[hot]\npages = "a.md"',
    '[hot]\nunknown = true',
    '[hot.projects.a]\nroots=["../app"]\n[hot.projects.b]\nroots=["../app"]',
])
def test_invalid_contract_rejected(tmp_path, hot):
    with pytest.raises(KelmoError):
        config(tmp_path, hot)


def test_hook_uses_payload_cwd_and_reports_missing_page(tmp_path):
    from kelmo.integration import hook
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    service = SimpleNamespace(root=selected.root)
    failed = hook(service, 'start', {})['hookSpecificOutput']['additionalContext']
    assert 'unavailable' in failed.lower()
    (selected.wiki / 'global.md').write_text('A complete condition, including its exception.')
    assert 'including its exception' in hook(service, 'start', {})['hookSpecificOutput']['additionalContext']


def test_preview_cli_needs_no_archive_and_check_all_excludes_content(tmp_path, capsys):
    from kelmo.cli import main
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    (selected.wiki / 'global.md').write_text('PRIVATE_SENTINEL')
    assert main(['hot', '--vault', str(selected.root), '--json']) == 0
    assert 'PRIVATE_SENTINEL' in capsys.readouterr().out
    assert main(['hot', '--vault', str(selected.root), '--check-all', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is True
    assert 'PRIVATE_SENTINEL' not in str(result)
    assert not (selected.root / 'archive').exists()


def test_unadopted_vault_keeps_working_tree_pages_without_generation_metadata(tmp_path):
    from kelmo.hot import render
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    (selected.wiki / 'global.md').write_text('Working condition.')
    result = render(selected)
    assert result['context'] == '## global.md\n\nWorking condition.'
    assert not {'generation', 'freshness', 'complete', 'mandatory'} & result.keys()


def test_relative_cwd_is_not_inferred_from_runner(tmp_path):
    from kelmo.hot import render
    with pytest.raises(KelmoError):
        render(config(tmp_path), Path('relative'))


def test_doctor_checks_unselected_projects_without_echoing_pages(tmp_path):
    from kelmo.doctor import diagnose
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n'
                      '[hot.projects.app]\nroots=["../app"]\npages=["missing.md"]\n')
    (selected.wiki / 'global.md').write_text('PRIVATE_SENTINEL')
    result = diagnose(selected.root)
    assert result['checks']['hot'] is False
    assert 'PRIVATE_SENTINEL' not in str(result)
    (selected.wiki / 'missing.md').write_text('Project condition.')
    result = diagnose(selected.root)
    assert result['checks']['hot'] is True
    assert len(result['hot_policy']['selections']) == 2


def test_hook_start_cli_works_without_archive_or_model(tmp_path, capsys, monkeypatch):
    import io
    from kelmo.cli import main
    selected = config(tmp_path, '[hot]\npages=["global.md"]\n')
    (selected.wiki / 'global.md').write_text('Current condition.')
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    assert main(['hook', 'start', '--vault', str(selected.root)]) == 0
    assert 'Current condition.' in capsys.readouterr().out
    assert not (selected.root / 'archive').exists()
