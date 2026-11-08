from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from kelmo.doctor import diagnose
from kelmo.service import Service
from platform_support import UNSUPPORTED
from test_fact_checks import content

pytest_plugins = ['test_fact_checks']


def healthy_vault(tmp_path):
    root = tmp_path / 'vault'
    Service.initialize(root, 'synthetic')
    (root / '.cache/embeddings/model').mkdir(parents=True)
    return root


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_healthy_doctor_preserves_public_shape(tmp_path):
    root = healthy_vault(tmp_path)
    with patch('kelmo.store.memory.embed.embed', return_value=bytes(1536)), \
            patch('kelmo.linux_runtime.launcher', return_value=Path(sys.executable)):
        result = diagnose(root)
    assert result['healthy'] is True
    assert all(result['checks'].values())
    assert result['orphan_sources'] == 0
    assert result['warnings'] == []
    assert result['deployment_parity_verified'] is False


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_missing_sandbox_launcher_is_reported_without_failing_a_local_vault(tmp_path):
    root = healthy_vault(tmp_path)
    with patch('kelmo.store.memory.embed.embed', return_value=bytes(1536)), \
            patch('kelmo.doctor.sys.platform', 'unsupported'):
        result = diagnose(root)
    assert result['healthy'] is True
    assert result['runtime_isolation'] == {'launcher_available': False}
    assert any('sandbox launcher is unavailable' in warning for warning in result['warnings'])


def test_invalid_configuration_keeps_doctor_result_keys(tmp_path):
    root = healthy_vault(tmp_path)
    with patch('kelmo.store.memory.embed.embed', return_value=bytes(1536)):
        valid = diagnose(root)
    invalid = diagnose(tmp_path / 'missing')
    assert invalid.keys() == valid.keys()
    assert invalid['budget_policy'] == {}


def test_doctor_reports_missing_data_repository_root(tmp_path):
    import shutil
    from kelmo.wiki import engine
    root = healthy_vault(tmp_path)
    shutil.rmtree(root / '.git')
    engine.git(tmp_path, 'init', '-q')
    result = diagnose(root)
    assert result['checks']['data_repository'] is False
    assert any('own Git repository' in warning for warning in result['warnings'])


def test_doctor_explains_budget_failure_without_echoing_page_contents(tmp_path):
    root = healthy_vault(tmp_path)
    page = root / 'knowledge/wiki/person.md'
    page.write_text('PRIVATE_CONTENT_SENTINEL\n' * 201)
    result = diagnose(root)
    assert result['checks']['budget'] is False
    assert result['issues']['budget'] == ['hub size exceeded: person.md']
    assert 'budget' in ' '.join(result['warnings']).lower()
    assert 'PRIVATE_CONTENT_SENTINEL' not in str(result)


@pytest.mark.parametrize('relative,check', [
    ('archive/archive.db', 'archive_integrity'),
    ('.state/journal.sqlite3', 'journal_integrity'),
])
def test_missing_database_is_reported_without_recreation(tmp_path, relative, check):
    root = healthy_vault(tmp_path)
    path = root / relative
    path.unlink()
    result = diagnose(root)
    assert result['healthy'] is False
    assert result['checks'][check] is False
    assert not path.exists()


@pytest.mark.parametrize('relative,check', [
    ('archive/archive.db', 'archive_integrity'),
    ('.state/journal.sqlite3', 'journal_integrity'),
])
def test_corrupt_database_is_reported_without_rewrite(tmp_path, relative, check):
    root = healthy_vault(tmp_path)
    path = root / relative
    path.write_bytes(b'not a sqlite database')
    before = path.read_bytes()
    result = diagnose(root)
    assert result['healthy'] is False
    assert result['checks'][check] is False
    assert path.read_bytes() == before


def test_unavailable_wiki_is_reported_without_recreation(tmp_path):
    root = healthy_vault(tmp_path)
    wiki = root / 'knowledge/wiki'
    moved = tmp_path / 'moved-wiki'
    wiki.rename(moved)
    result = diagnose(root)
    assert result['healthy'] is False
    assert result['checks']['wiki_available'] is False
    assert not wiki.exists()


def test_malformed_managed_markers_are_metadata_only_failure(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'fact.md', 'lead', 'promote')
    service.run(operation['id'])
    page = service.config.wiki / 'projects/fixture/fixture.md'
    page.write_text(page.read_text().replace('<!-- /kb -->', '<!-- kb -->'))
    result = diagnose(service.root)
    assert result['checks']['wiki_consistency'] is False
    assert result['issues']['wiki_consistency'] == ['projects/fixture/fixture.md']
    assert '<!-- kb -->' not in str(result)


def test_dirty_managed_page_warning_names_restore_and_the_note_path(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'fact.md', 'lead', 'promote')
    service.run(operation['id'])
    page = service.config.wiki / 'projects/fixture/fixture.md'
    page.write_text('Uncommitted prose.\n' + page.read_text())
    result = diagnose(service.root)
    assert result['checks']['wiki_consistency'] is True
    assert result['checks']['wiki_committed'] is False
    assert result['issues']['wiki_uncommitted'] == ['projects/fixture/fixture.md']
    warning = ' '.join(result['warnings']).lower()
    assert 'restore' in warning and 'kelmo create' in warning and 'invalidates' in warning


def test_external_wiki_reports_budget_scope_without_claiming_hot_chain_validation(tmp_path):
    root = tmp_path / 'vault'
    wiki = tmp_path / 'wiki'
    Service.initialize(root, 'synthetic', wiki_root=wiki)
    (wiki / 'index.md').write_text('Ordinary existing content\n' * 201)
    result = diagnose(root)
    assert result['checks']['budget'] is True
    assert result['budget_policy'] == {'hub_limits_enforced': False, 'hot_chains_checked': 0,
                                       'hot_chains_configured': 0, 'hot_chains_uncovered': [],
                                       'hot_imports_unresolved': []}


def test_git_status_failure_does_not_invalidate_other_doctor_checks(tmp_path, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    root = healthy_vault(tmp_path)
    original_git = engine.git

    def unavailable_status(repo, *args, **kwargs):
        if args[0] == 'status':
            raise KelmoError('E_GIT', 'Synthetic status failure')
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(engine, 'wiki_committed', lambda *args, **kwargs: False)
    monkeypatch.setattr(engine, 'git', unavailable_status)
    result = diagnose(root)
    assert result['checks']['wiki_consistency'] is True
    assert result['issues']['wiki_consistency'] == []
    assert result['checks']['budget'] is True
    assert result['checks']['links'] is True
    assert any('Git status' in warning for warning in result['warnings'])


def test_doctor_lists_uncommitted_registry_with_one_status_call_per_repo(tmp_path, monkeypatch):
    from kelmo.wiki import engine
    root = healthy_vault(tmp_path)
    registry = root / 'knowledge/provenance/registry.json'
    registry.write_text(registry.read_text() + '\n')
    original_git = engine.git
    calls = []

    def counted_git(repo, *args, **kwargs):
        if args[0] == 'status':
            calls.append(repo)
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(engine, 'git', counted_git)
    result = diagnose(root)
    assert result['checks']['wiki_committed'] is False
    assert result['issues']['wiki_uncommitted'] == ['knowledge/provenance/registry.json']
    assert len(calls) == 1


def test_symlinked_wiki_target_is_reported_without_reading_target(service, tmp_path):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                'fact.md', 'lead', 'promote')
    service.run(operation['id'])
    page = service.config.wiki / 'projects/fixture/fixture.md'
    outside = tmp_path / 'outside.md'
    outside.write_text('private sentinel')
    page.unlink()
    page.symlink_to(outside)
    result = diagnose(service.root)
    assert result['healthy'] is False
    assert result['checks']['wiki_consistency'] is False
    assert 'private sentinel' not in str(result)


@pytest.mark.parametrize('parent,failed_check', [
    ('archive', 'archive_integrity'),
    ('.state', 'journal_integrity'),
    ('knowledge/provenance', 'registry_integrity'),
])
def test_symlinked_internal_parent_is_not_followed(tmp_path, parent, failed_check):
    root = healthy_vault(tmp_path)
    internal = root / parent
    outside = tmp_path / ('outside-' + parent.replace('/', '-').replace('.', 'state'))
    internal.rename(outside)
    sentinel = outside / 'sentinel.txt'
    sentinel.write_text('private sentinel')
    internal.symlink_to(outside, target_is_directory=True)
    result = diagnose(root)
    assert result['healthy'] is False
    assert result['checks'][failed_check] is False
    assert 'private sentinel' not in str(result)
    assert sentinel.read_text() == 'private sentinel'


def test_a_note_waiting_over_an_hour_for_a_lock_is_named(tmp_path):
    from kelmo.config import Config
    from kelmo.doctor import _long_waits
    root = healthy_vault(tmp_path)
    with Service(root) as service:
        operation = service.capture('Waiting evidence.', 'waiting.md', 'lead')
        service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_BUSY')
        fresh = service.capture('Fresh evidence.', 'fresh.md', 'lead')
        service.journal.stage(fresh['id'], 'failed', terminal=True, error_code='E_BUSY')
        journal = Config.load(root).internal('.state/journal.sqlite3')
    assert _long_waits(journal, []) == [], 'a wait under an hour is the queue working'
    import time
    named = {item['operation_id'] for item in _long_waits(journal, [], clock=lambda: time.time() + 7200)}
    assert named == {operation['id'], fresh['id']}
    jobs = [{'id': 'job-1', 'state': 'pending', 'error_code': 'E_BUSY', 'created_at': '2000-01-01T00:00:00+00:00'},
            {'id': 'job-2', 'state': 'pending', 'error_code': 'E_EXTRACTION', 'created_at': '2000-01-01T00:00:00+00:00'}]
    jobs.append({'id': 'job-3', 'state': 'pending', 'error_code': None, 'created_at': '2000-01-01T00:00:00+00:00'})
    assert [item['session_job_id'] for item in _long_waits(journal, jobs)] == ['job-1', 'job-3']
    with patch('kelmo.store.memory.embed.embed', return_value=bytes(1536)), \
            patch('kelmo.doctor._long_waits', return_value=[{'operation_id': operation['id']}]):
        result = diagnose(root)
    assert result['issues']['long_waits'] == [{'operation_id': operation['id']}]
    assert any('waited over an hour' in warning and operation['id'] in warning for warning in result['warnings'])
