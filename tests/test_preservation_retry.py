import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kelmo.resolution import Resolution
from kelmo.service import Service
from kelmo.wiki import engine
from test_fact_checks import content
from test_resolution import pending


def legacy_external_vault(root, label, wiki):
    """The pre-schema-2 layout an installed vault may still run: the registry in the vault's own Git
    repository, the pages in a separate repository named by `wiki_root`. `init --wiki` now binds one
    knowledge repository instead, so the legacy layout is built here directly; its publication and
    recovery paths stay supported for existing vaults."""
    from kelmo.config import Config
    Service.initialize(root, label)
    root, wiki = Path(root).resolve(), Path(wiki).resolve()
    wiki.mkdir(parents=True, exist_ok=True)
    if not (wiki / '.git').exists():
        engine.git(wiki, 'init', '-q', '-b', 'master')
        engine.git(wiki, 'commit', '--allow-empty', '-q', '-m', 'chore: Initialize wiki storage')
    settings = root / 'kelmo.toml'
    settings.write_text(settings.read_text(encoding='utf-8') + 'wiki_root = ' + json.dumps(str(wiki)) + '\n',
                        encoding='utf-8')
    engine.git(root, 'add', '--', 'kelmo.toml')
    engine.git(root, 'commit', '-q', '-m', 'chore: Select the external wiki repository')
    engine.activate_wiki(wiki, Config.load(root).vault_id)
    return root

pytest_plugins = ['test_fact_checks']


def captured_promotion(service, filename='receipt.md'):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    return service.capture(
        content({'result': 'not_found', 'search_id': receipt['search_id']}),
        filename, 'lead', 'promote')


def failed_after_archive(service):
    operation = captured_promotion(service)
    with patch.object(engine, '_commit_registry', side_effect=RuntimeError('synthetic wiki failure')):
        failed = service.run(operation['id'])
    assert failed['state'] == 'failed'
    assert failed['durable_stage'] == 'archived'
    return failed


def test_saved_archive_receipt_retry_does_not_store_again(service):
    failed = failed_after_archive(service)
    source = service.source_path(failed).read_text(encoding='utf-8')
    assert service.show(failed['archive_id'])['content'] == source
    service.retry(failed['id'])
    with patch.object(service, '_archive', side_effect=AssertionError('receipt must be reused')) as archive:
        result = service.run(failed['id'])
    archive.assert_not_called()
    assert result['state'] == 'applied'
    assert result['archive_id'] == failed['archive_id']
    assert service.status()['archive_records'] == 1


def test_mismatched_saved_archive_receipt_blocks_wiki_without_replacing_it(service):
    failed = failed_after_archive(service)
    receipt = service.show(failed['archive_id'])
    receipt['content'] = 'Different synthetic evidence.'
    service.retry(failed['id'])
    with patch.object(service, 'archive_record', return_value=receipt), \
            patch.object(service, '_archive') as archive, \
            patch.object(engine, '_commit_registry') as commit:
        result = service.run(failed['id'])
    assert result['state'] == 'failed'
    assert result['error_code'] == 'E_SOURCE'
    assert result['durable_stage'] == 'archived'
    assert result['archive_id'] == failed['archive_id']
    archive.assert_not_called()
    commit.assert_not_called()
    assert service.status()['facts'] == 0
    assert service.status()['archive_records'] == 1


def test_service_budget_failure_is_archived_failed_without_resolution_or_auto_retry(service):
    from kelmo.daemon import tick
    operation = captured_promotion(service)
    head = engine.git(service.root, 'rev-parse', 'HEAD')
    with patch.object(engine.policy, 'check', return_value={'ok': False, 'violations': ['synthetic hub overflow']}):
        result = service.run(operation['id'])
    assert result['state'] == 'failed'
    assert result['terminal'] is True
    assert result['error_code'] == 'E_BUDGET'
    assert result['durable_stage'] == 'archived'
    assert service.show(result['archive_id'])['content'] == service.source_path(result).read_text(encoding='utf-8')
    assert Resolution(service).list() == []
    assert tick(service)['operations'] == 0
    assert service.journal.get(operation['id'])['state'] == 'failed'
    assert engine.git(service.root, 'rev-parse', 'HEAD') == head
    assert not engine.transaction_path(service.root).exists()
    assert service.status()['facts'] == 0


@pytest.mark.parametrize('action', ['fail', 'release_busy'])
def test_expired_resolution_callback_cannot_mutate_job_or_operation(service, monkeypatch, action):
    operation = pending(service)
    queue = Resolution(service)
    clock = [1000.0]
    monkeypatch.setattr('kelmo.resolution.time.time', lambda: clock[0])
    request = queue.claim(lease_seconds=1)
    before = dict(queue.conn.execute('SELECT * FROM resolutions WHERE id=?', (request['id'],)).fetchone())
    trace = service.trace(operation['id'])
    clock[0] = 1002.0
    if action == 'fail':
        assert queue.fail(request['id'], request['token'], 'E_EXECUTOR', 1) is None
    else:
        assert queue.release_busy(request['id'], request['token']) is False
    after = dict(queue.conn.execute('SELECT * FROM resolutions WHERE id=?', (request['id'],)).fetchone())
    assert after == before
    assert service.trace(operation['id']) == trace
    current = queue.claim()
    assert current['token'] != request['token']

