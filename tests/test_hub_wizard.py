"""The interactive setup link offer does not mutate storage or slow noninteractive setup."""
import io
from types import SimpleNamespace

from kelmo.install import wizard


def test_link_offer_uses_current_repository_and_records_explicit_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, 'repository_root', lambda cwd: tmp_path / 'sample-app')
    args = SimpleNamespace(project='default', link=None)
    replies = iter(['y', ''])
    assert wizard.offer_project(args, cwd=tmp_path, ask=lambda _: next(replies), out=io.StringIO())
    assert args.project == 'sample-app'
    assert args.link == tmp_path / 'sample-app'
    assert args._project_requested
    assert list(tmp_path.iterdir()) == []


def test_link_offer_decline_and_nonrepository_leave_arguments_unchanged(tmp_path, monkeypatch):
    args = SimpleNamespace(project='default', link=None)
    monkeypatch.setattr(wizard, 'repository_root', lambda cwd: tmp_path / 'sample-app')
    assert not wizard.offer_project(args, ask=lambda _: '', out=io.StringIO())
    assert vars(args) == {'project': 'default', 'link': None}
    monkeypatch.setattr(wizard, 'repository_root', lambda cwd: None)
    assert not wizard.offer_project(args, ask=lambda _: (_ for _ in ()).throw(AssertionError('unexpected prompt')))


def test_explicit_link_skips_repository_probe(monkeypatch):
    monkeypatch.setattr(wizard, 'repository_root', lambda cwd: (_ for _ in ()).throw(AssertionError('unexpected probe')))
    args = SimpleNamespace(project='app', link='/some/repo')
    assert not wizard.offer_project(args)
