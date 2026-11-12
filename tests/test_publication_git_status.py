"""The publication reads the working-copy state of every rendered path with one Git status per repository."""
import subprocess

import pytest

from kelmo.wiki import engine
from test_editor_scoping import DRAFT, build_world


def _git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def test_porcelain_by_path_reports_each_dirty_path_and_leaves_clean_ones_out(tmp_path):
    _git(tmp_path, 'init', '-q', '-b', 'master')
    _git(tmp_path, 'config', 'user.name', 'Synthetic')
    _git(tmp_path, 'config', 'user.email', 'synthetic@example.invalid')
    for name in ('clean.md', 'edited.md', 'old name.md', 'removed.md'):
        (tmp_path / name).write_text(name)
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'baseline')
    (tmp_path / 'edited.md').write_text('changed')
    (tmp_path / 'removed.md').unlink()
    _git(tmp_path, 'mv', 'old name.md', 'new name.md')
    (tmp_path / 'fresh').mkdir()
    (tmp_path / 'fresh' / 'page.md').write_text('untracked')
    asked = ['clean.md', 'edited.md', 'removed.md', 'old name.md', 'new name.md', 'fresh/page.md', 'absent.md']
    statuses = engine._porcelain_by_path(tmp_path, asked)
    assert set(statuses) == {'edited.md', 'removed.md', 'old name.md', 'new name.md', 'fresh/page.md'}
    assert statuses['fresh/page.md'].startswith('?? ')
    assert statuses['old name.md'] == statuses['new name.md']
    # Each path's verdict matches a status of that path alone, the way the publication asked before.
    for path in asked:
        alone = engine.git(tmp_path, 'status', '--porcelain', '--', path)
        assert bool(alone) == (path in statuses), path
        assert alone.startswith('?? ') == statuses.get(path, '').startswith('?? '), path
    assert engine._porcelain_by_path(tmp_path, []) == {}


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_a_publication_runs_a_fixed_few_git_statuses_whatever_the_page_count(world, monkeypatch):
    statuses = []
    real = engine.git

    def counted(repository, *args, **kwargs):
        if args and args[0] == 'status':
            statuses.append(args)
        return real(repository, *args, **kwargs)

    monkeypatch.setattr(engine, 'git', counted)
    rendered = len(world.registry()['pages'])
    assert rendered >= 3
    result = world.service.register([DRAFT], apply=True)
    assert result['applied'] is True
    # The baseline check and the commit's batch; the generation file may add one more. Per page it was one each.
    assert len(statuses) <= 3 < rendered + 2
