"""Public commands must connect project lifecycle to actual hook delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from kelmo.config import Config
from kelmo.initialization import initialize
from kelmo.wiki.pages import KnowledgeReader


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / 'vault'
    initialize(root, 'synthetic-hub-workflow', project='alpha',
               git_name='Hub Test', git_email='hub@example.invalid')
    return root


def cli(vault, *args, payload=None):
    env = {'PATH': os.defpath, 'TMPDIR': '/private/tmp', 'GIT_CONFIG_NOSYSTEM': '1',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    result = subprocess.run([sys.executable, '-m', 'kelmo.cli', '--vault', str(vault),
                             *map(str, args), '--json'], env=env, cwd=vault.parent,
                            input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value.get('ok', True), value
    return value.get('result', value)


def test_public_lifecycle_reaches_hooks_without_losing_existing_hub(vault, tmp_path, request):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    cli(vault, 'project', 'add', 'beta')
    cli(vault, 'link', '--project', 'beta', repo)
    from kelmo.client import SharedClient, prepare_profile
    from kelmo.config import ClientProfile
    from kelmo.server import ServerProfile
    from tests.test_shared_context import Hub
    remote = 'https://example.invalid/hub-test.git'
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', remote], check=True)
    authority = Config.load(vault).vault_id
    profile = ServerProfile.from_dict({
        'schema': 'kelmo-server/1', 'authority_id': authority,
        'writer': {'writer_id': 'local', 'epoch': 1}, 'archive_process': 'in-process',
        'clients': [{'client_id': 'test-client', 'machine_tag': 'synthetic', 'repository_ids': ['repo']}],
        'repositories': [{'repository_id': 'repo', 'root': str(repo), 'remote': remote, 'project': 'beta'}]})
    hub = Hub(vault, profile)
    request.addfinalizer(hub.close)
    client_root = tmp_path / 'shared-client'
    prepare_profile(client_root, authority, ['/bin/false'])
    client = SharedClient(ClientProfile.load(client_root), channel_factory=hub.factory('test-client'))
    request.addfinalizer(client.close)
    assert client.context(str(repo))['project'] == 'beta'
    waiting = 'A captured beta note is awaiting publication.'
    cli(vault, 'create', '--project', 'beta', '--text', 'Synthetic open work.', '--pending', waiting)
    assert any(row['pending'] == waiting for row in client.context(str(repo))['pending'])
    context = cli(vault, 'hook', 'start', '--provider', 'codex',
                  payload={'cwd': str(repo), 'session_id': 'public-workflow'})
    text = context['hookSpecificOutput']['additionalContext']
    assert 'beta' in text.lower()
    assert 'unavailable' not in text.lower()
    assert waiting in text
    config = Config.load(vault)
    reader = KnowledgeReader(config)
    generation = reader.generation()
    assert reader.page('projects/alpha/alpha.md', generation)
    assert reader.page('projects/beta/beta.md', generation)

    cli(vault, 'hub', 'embed', '--codex', '--project', 'beta', '--file', repo / 'AGENTS.md')
    cli(vault, 'project', 'rename', 'beta', 'gamma')
    from kelmo.projects import resolve
    assert resolve(Config.load(vault), repo) == 'gamma'
    assert client.context(str(repo))['project'] == 'gamma'
    changed = cli(vault, 'hook', 'prompt', '--provider', 'codex',
                  payload={'cwd': str(repo), 'session_id': 'public-workflow'})
    assert 'gamma' in changed['hookSpecificOutput']['additionalContext'].lower()
    assert waiting in changed['hookSpecificOutput']['additionalContext']
    assert 'projects/gamma/gamma.md' in (repo / 'AGENTS.md').read_text()
    cli(vault, 'unlink', repo)
    assert resolve(Config.load(vault), repo) is None
    listing = cli(vault, 'projects')
    assert 'alpha' in json.dumps(listing) and 'gamma' in json.dumps(listing)
    cli(vault, 'link', '--project', 'delta', repo)
    assert resolve(Config.load(vault), repo) == 'delta'


def test_project_preview_lists_external_views_without_creating_storage(tmp_path):
    from kelmo.project_preview import preview
    root, external = tmp_path / 'new-vault', tmp_path / 'knowledge-repo'
    plan = preview(root, 'alpha', wiki_root=external)
    paths = {item['path'] for item in plan['files']}
    assert str(root / 'kelmo.toml') in paths
    assert str(external / 'knowledge/hot/mandatory.md') in paths
    assert str(external / 'knowledge/hot/alpha.full.md') in paths
    assert not root.exists() and not external.exists()
