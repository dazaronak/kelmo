"""Only verified current imports may suppress repeated hook context."""
from types import SimpleNamespace

from kelmo.hub_delivery import install_set
from kelmo.hub_embed import import_lines, select, sync, write_block
from tests.test_hub_delivery import snapshot


def selected_import(tmp_path, current):
    config = SimpleNamespace(state=tmp_path / 'state')
    target = tmp_path / 'CLAUDE.md'
    write_block(target, import_lines(current, 'alpha'))
    select(config, 'alpha', 'claude', target, current)
    return config


def test_managed_current_symlink_is_a_valid_import_directory(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(1, 0))
    config = selected_import(tmp_path, root / 'current')
    assert sync(config, 'alpha', 'claude')['verified']
    install_set(root, snapshot(2, 0))
    assert sync(config, 'alpha', 'claude')['verified']


def test_stale_or_altered_imports_cannot_suppress_current_memory(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(1, 0))
    config = selected_import(tmp_path, root / 'current')
    current = snapshot(2, 0)
    assert not sync(config, 'alpha', 'claude', snapshot=current)['verified']
    install_set(root, current)
    page = root / 'current' / 'alpha.md'
    page.write_text(current['files']['alpha.md']['text'] + '\nChanged without publication.')
    assert not sync(config, 'alpha', 'claude', snapshot=current)['verified']
    page.write_text(current['files']['alpha.md']['text'])
    assert sync(config, 'alpha', 'claude', snapshot=current)['verified']


def test_project_rename_refreshes_both_opted_in_instruction_formats(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(1, 0))
    config = selected_import(tmp_path, root / 'current')
    agents = tmp_path / 'AGENTS.md'
    write_block(agents, 'Old project memory.')
    select(config, 'alpha', 'codex', agents, root / 'current')
    current = snapshot(2, 0, projects=('beta',))
    install_set(root, current)
    aliases = {'alpha': 'beta'}
    result = sync(config, 'beta', 'claude', cwd=tmp_path, snapshot=current, aliases=aliases)
    assert result['verified'] and result['changed']
    assert 'beta.md' in (tmp_path / 'CLAUDE.md').read_text()
    assert 'alpha.md' not in (tmp_path / 'CLAUDE.md').read_text()
    result = sync(config, 'beta', 'codex', cwd=tmp_path, content='Renamed memory.', aliases=aliases)
    assert result['verified'] and result['changed']
    assert 'Renamed memory.' in agents.read_text()
