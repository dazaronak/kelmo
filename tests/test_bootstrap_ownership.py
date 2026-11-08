import json
from pathlib import Path
import sys

import pytest

from kelmo.errors import KelmoError
from test_installer_failures import load_bootstrap


@pytest.mark.parametrize('kind', ['foreign', 'hardlink'])
def test_bootstrap_rejects_invalid_runtime_marker_before_writes(tmp_path, monkeypatch, kind):
    module = load_bootstrap()
    prefix = tmp_path / 'runtime'
    prefix.mkdir()
    marker = prefix / '.kelmo-runtime'
    unrelated = tmp_path / 'unrelated.txt'
    original = b'kelmo\n' if kind == 'hardlink' else b'Unrelated bytes.\n'
    unrelated.write_bytes(original)
    if kind == 'hardlink':
        marker.hardlink_to(unrelated)
    else:
        marker.write_bytes(original)
    kept = prefix / 'keep.txt'
    kept.write_bytes(b'Keep this file.\n')
    monkeypatch.setattr(module.venv.EnvBuilder, 'create',
                        lambda *a, **k: pytest.fail('venv created'))
    monkeypatch.setattr(module.subprocess, 'run',
                        lambda *a, **k: pytest.fail('subprocess invoked'))
    monkeypatch.setattr(sys, 'argv', [
        'install.py', '--prefix', str(prefix), '--home', str(tmp_path / 'home'),
        '--vault', str(tmp_path / 'vault'), '--disable-native-memory', '--no-start',
    ])

    with pytest.raises(SystemExit) as raised:
        module.main()

    assert raised.value.code == 2
    assert marker.read_bytes() == original
    assert unrelated.read_bytes() == original
    assert kept.read_bytes() == b'Keep this file.\n'
    assert not (tmp_path / 'home').exists()
    assert not (tmp_path / 'vault').exists()


@pytest.mark.parametrize('failed_name', ['config.toml', 'hooks.json'])
def test_codex_same_upgrade_recovers_after_interrupted_write(tmp_path, monkeypatch, failed_name):
    from kelmo.install import setup as installer

    home = tmp_path / 'home'
    installer.configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'vault', 'codex', True, 'auto')
    config = home / '.codex/config.toml'
    with config.open('a') as output:
        output.write('\n[user_settings]\npreserve = true\n')
    real_write = installer.write
    failed = []

    def interrupt(path, data, expected_prior=installer._UNSET):
        if Path(path).name == failed_name and not failed:
            failed.append(True)
            raise KelmoError('E_IO', 'synthetic interruption')
        return real_write(path, data, expected_prior)

    monkeypatch.setattr(installer, 'write', interrupt)
    with pytest.raises(KelmoError, match='synthetic interruption'):
        installer.configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'vault', 'codex', True, 'auto')
    monkeypatch.setattr(installer, 'write', real_write)

    installer.configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'vault', 'codex', True, 'auto')
    assert '[user_settings]\npreserve = true' in config.read_text()
    hooks = json.loads((home / '.codex/hooks.json').read_bytes())['hooks']
    for groups in hooks.values():
        for group in groups:
            for member in group['hooks']:
                assert str(tmp_path / 'new-kelmo') in member['command']
                assert str(tmp_path / 'old-kelmo') not in member['command']


def test_legacy_marked_codex_upgrade_creates_receipt_without_losing_binding(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'old-kelmo', tmp_path / 'vault', 'codex', True, 'auto')
    receipt = home / '.codex/kelmo/ownership.json'
    receipt.unlink()

    configure_clients(home, tmp_path / 'new-kelmo', tmp_path / 'vault', 'codex', True, 'auto')

    owner = json.loads(receipt.read_bytes())
    assert owner['previous_owned_entry']['command'] == str(tmp_path / 'old-kelmo')
    assert owner['current_owned_entry']['command'] == str(tmp_path / 'new-kelmo')


def test_receipted_codex_owned_mcp_edit_is_not_overwritten(tmp_path):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True, 'auto')
    config = home / '.codex/config.toml'
    before = config.read_text().replace(str(tmp_path / 'kelmo'), '/foreign/runtime')
    config.write_text(before)

    with pytest.raises(KelmoError, match='owned MCP entry changed'):
        configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', 'codex', True, 'auto')

    assert config.read_text() == before


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_disabling_sessions_preserves_user_group_metadata(tmp_path, client):
    from kelmo.install.setup import configure_clients

    home = tmp_path / 'home'
    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', client, True, 'auto')
    settings_path = home / ('.claude/settings.json' if client == 'claude' else '.codex/hooks.json')
    settings = json.loads(settings_path.read_bytes())
    settings['hooks']['Stop'][0]['matcher'] = 'user matcher'
    settings_path.write_text(json.dumps(settings))

    configure_clients(home, tmp_path / 'kelmo', tmp_path / 'vault', client, True, 'off')

    assert json.loads(settings_path.read_bytes())['hooks']['Stop'] == [
        {'matcher': 'user matcher', 'hooks': []},
    ]
