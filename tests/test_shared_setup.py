import json
import uuid

from kelmo.config import ClientProfile
from kelmo.interface import build_parser
from kelmo.install.preview import preview
from kelmo.install.setup import setup


def arguments(tmp_path):
    executable = tmp_path / 'kelmo'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    return build_parser('test')[0].parse_args([
        'setup', '--vault', str(tmp_path / 'client'), '--home', str(tmp_path / 'home'),
        '--executable', str(executable), '--client', 'claude', '--disable-native-memory',
        '--shared-authority', str(uuid.uuid4()), '--shared-command-json', json.dumps(['/bin/false']),
        '--session-memory', 'archive-only'])


def test_shared_preview_is_read_only_even_before_a_real_install(tmp_path):
    args = arguments(tmp_path)
    assert not args.dry_run
    result = preview(args)
    assert result['mode'] == 'shared' and result['provider_calls'] == 'none'
    assert not args.vault.exists() and not args.home.exists()


def test_shared_setup_creates_only_client_integrations_and_retains_unrelated_mcp(tmp_path):
    args = arguments(tmp_path)
    args.home.mkdir()
    config = args.home / '.claude.json'
    original = {'mcpServers': {'unrelated': {'command': '/bin/false'}}}
    config.write_text(json.dumps(original))
    result = setup(args)
    assert result['client_configured'] and not result['background_started']
    profile = ClientProfile.load(args.vault)
    assert profile.authority_id == args.shared_authority and profile.sessions['claude']
    assert not (args.vault / 'kelmo.toml').exists() and not (args.vault / 'archive').exists()
    assert json.loads(config.read_text())['mcpServers']['unrelated'] == original['mcpServers']['unrelated']
    assert result['backups']
    assert not (args.home / 'Library/LaunchAgents/org.kelmo.worker.plist').exists()
