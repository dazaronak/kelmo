"""Broker commands in vault configuration and the embed core specification."""
import json
import tomllib

import pytest

from embed_core_support import cold_broker_vault
from kelmo.config import validate_embedding
from kelmo.core.spec import build
from kelmo.errors import KelmoError


def test_broker_command_is_validated(tmp_path, monkeypatch):
    root = cold_broker_vault(tmp_path, monkeypatch)
    raw = tomllib.loads((root / 'kelmo.toml').read_text())['embedding']
    assert validate_embedding({**raw, 'command': ['/bin/broker', 'arg']}, root)['command'] == [
        '/bin/broker', 'arg']
    for command in ([], ['relative'], [''], ['/bin/broker', ' '], '/bin/broker', [1]):
        with pytest.raises(KelmoError):
            validate_embedding({**raw, 'command': command}, root)
    with pytest.raises(KelmoError):
        validate_embedding({'provider': 'local', 'command': ['/bin/broker']}, root)


def test_embed_spec_passes_only_provider_environment(tmp_path, monkeypatch):
    root = cold_broker_vault(tmp_path, monkeypatch)
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'private identity')
    monkeypatch.setenv('LANG', 'test-language')
    monkeypatch.setenv('UNRELATED_SECRET', 'not passed')
    config = root / 'kelmo.toml'
    config.write_text(config.read_text().replace('provider = "broker"',
                                                'provider = "broker"\ncommand = ["/bin/broker", "arg"]'))
    spec = build(root, archive_core=True, embed_core=True, kelmod='/bin/kelmod')
    embed = next(core for core in spec['cores'] if core['name'] == 'embed')
    argv = embed['argv']
    assert embed['env'] == {}
    assert json.loads(argv[argv.index('--provider-command') + 1]) == ['/bin/broker', 'arg']
    env = json.loads(argv[argv.index('--provider-env') + 1])
    assert env['LANG'] == 'test-language'
    assert 'UNRELATED_SECRET' not in env
    assert not any(name.startswith('GIT_') for name in env)


def test_embed_spec_without_command_has_no_provider_arguments(tmp_path, monkeypatch):
    root = cold_broker_vault(tmp_path, monkeypatch)
    spec = build(root, archive_core=True, embed_core=True, kelmod='/bin/kelmod')
    embed = next(core for core in spec['cores'] if core['name'] == 'embed')
    assert '--provider-command' not in embed['argv']
    assert '--provider-env' not in embed['argv']
