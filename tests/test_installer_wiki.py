"""Wiki-root forwarding through the checkout installer and setup command."""
import importlib.util
from pathlib import Path
import sys

import pytest

pytest_plugins = ['test_fact_checks']


def load_installer():
    path = Path(__file__).parents[1] / 'install.py'
    spec = importlib.util.spec_from_file_location('kelmo_checkout_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('wiki_args, expected', [
    ([], []),
    (['--wiki', '/synthetic/wiki'], ['--wiki', '/synthetic/wiki']),
])
def test_checkout_installer_forwards_only_explicit_wiki_options(
        tmp_path, monkeypatch, wiki_args, expected):
    installer = load_installer()
    commands = []

    class FakeBuilder:
        def __init__(self, **kwargs):
            pass

        def create(self, prefix):
            (Path(prefix) / 'bin').mkdir(parents=True)

    class Result:
        returncode = 0

    def run(command, **kwargs):
        commands.append(command)
        return Result()

    prefix = tmp_path / 'runtime'
    monkeypatch.setattr(installer.venv, 'EnvBuilder', FakeBuilder)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setattr(sys, 'argv', [
        'install.py', '--prefix', str(prefix), '--vault', str(tmp_path / 'vault'),
        '--home', str(tmp_path / 'home'), '--disable-native-memory', '--no-start', *wiki_args,
    ])

    assert installer.main() == 0
    setup_command = commands[-1]
    forwarded = [value for value in setup_command if value in ('--wiki', '/synthetic/wiki')]
    assert forwarded == expected


def test_setup_rejects_explicit_wiki_switch_on_reinstall(service, tmp_path, monkeypatch):
    import hashlib
    import sys
    from kelmo.store.memory import embed
    from kelmo import model_store
    from kelmo.errors import KelmoError
    from kelmo.install.setup import setup
    from kelmo.interface import build_parser
    source = tmp_path / 'model'
    source.mkdir()
    data = b'Synthetic model bytes'
    (source / 'model.onnx').write_bytes(data)
    monkeypatch.setattr(model_store, 'FILES', {
        'model.onnx': (len(data), hashlib.sha256(data).hexdigest())})
    monkeypatch.setattr(embed, 'embed', lambda *args, **kwargs: bytes(1536))
    args = build_parser('')[0].parse_args([
        'setup', '--vault', str(service.root), '--wiki', str(tmp_path / 'different-wiki'),
        '--home', str(tmp_path / 'home'), '--executable', sys.executable,
        '--model-source', str(source), '--disable-native-memory', '--no-start',
    ])

    with pytest.raises(KelmoError, match='wiki'):
        setup(args)

    assert not (tmp_path / 'home/.claude.json').exists()
