import json
import shlex
import io
import pytest
from unittest.mock import patch

from kelmo.cli import main
from kelmo.errors import KelmoError
from kelmo.service import Service


def test_failure_retry_command_selects_the_original_operation_and_vault(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'vault with spaces'
    Service.initialize(root, 'fixture', project='fixture')
    with Service(root) as service:
        operation = service.capture('Synthetic retry evidence.', 'retry.md', 'lead')

    def unavailable(*args):
        raise KelmoError('E_EMBEDDING', 'Synthetic model unavailable.')

    monkeypatch.setattr(Service, '_archive', unavailable)
    assert main(['worker', 'run', operation['id'], '--vault', str(root), '--json']) != 0
    failure = json.loads(capsys.readouterr().out)
    retry = failure['error']['retry'][0]
    assert retry['operation_id'] == operation['id']
    command = shlex.split(retry['command'])
    assert command[0] == 'kelmo'
    assert main([*command[1:], '--json']) == 0
    assert json.loads(capsys.readouterr().out)['operation']['state'] == 'queued'


def test_daemon_once_reports_counts_without_iterating_an_integer(tmp_path, capsys):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    assert main(['worker', 'daemon', '--vault', str(root), '--once', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is True
    assert result['result'] == {'operations': 0, 'resolutions': 0}


def test_daemon_once_marks_failed_attempts_without_losing_their_ids(monkeypatch, capsys):
    attempt = {'resolution_id': 'resolution-fixture', 'operation_id': 'operation-fixture',
               'error_code': 'E_EXECUTOR', 'exhausted': False}
    monkeypatch.setattr('kelmo.cli.dispatch', lambda *args:
                        {'operations': 0, 'resolutions': 1, 'failed_attempts': [attempt]})
    assert main(['worker', 'daemon', '--once', '--json']) != 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False
    assert result['error']['code'] == 'E_EXECUTOR'
    assert result['result']['failed_attempts'] == [attempt]


@pytest.mark.parametrize('code', ['E_BUSY', 'E_CLAIM'])
def test_wait_preserves_queued_operation_when_background_writer_is_busy(tmp_path, monkeypatch, capsys, code):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')

    def busy(*args):
        raise KelmoError(code, 'Synthetic writer busy.')

    monkeypatch.setattr(Service, 'run', busy)
    monkeypatch.setattr('sys.stdin', io.StringIO('Synthetic queued evidence.'))
    assert main(['add', '--vault', str(root), '--file', '-', '--role', 'lead', '--wait', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['operation']['state'] == 'queued'
    assert result['result']['processing_deferred'] is True
    with Service(root) as service:
        assert service.trace(result['operation']['id'])['operation']['state'] == 'queued'


def test_wait_explains_that_collector_evidence_is_not_scheduled(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    monkeypatch.setattr('sys.stdin', io.StringIO('Synthetic collector evidence.'))
    assert main(['add', '--vault', str(root), '--file', '-', '--wait', '--json']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['operation']['state'] == 'captured'
    assert response['result']['processing_scheduled'] is False
    assert 'processing_deferred' not in response['result']


def test_missing_source_has_a_specific_safe_error(tmp_path, capsys):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    assert main(['add', '--vault', str(root), '--file', str(tmp_path / 'missing.md'), '--json']) != 0
    error = json.loads(capsys.readouterr().out)['error']
    assert error['code'] == 'E_INPUT'
    assert 'does not exist' in error['message']


def test_invalid_fact_target_reports_the_rule_without_echoing_source(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    change = dict(fact_key='fixture.fact', target='PRIVATE_TARGET_SENTINEL', text='Synthetic fact.',
                  status='active', expected_revision=None, replaces=[])
    monkeypatch.setattr('sys.stdin', io.StringIO('```wiki-changes\n' + json.dumps([change]) + '\n```\n'))
    assert main(['promote', '--vault', str(root), '--file', '-', '--role', 'lead', '--json']) != 0
    error = json.loads(capsys.readouterr().out)['error']
    assert error['code'] == 'E_INPUT'
    assert 'target' in error['message']
    assert 'PRIVATE_TARGET_SENTINEL' not in error['message']


def test_missing_git_identity_is_actionable_before_publishing_vault(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for name in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / 'vault'
    assert main(['init', '--vault', str(root), '--label', 'fixture', '--json']) != 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_GIT_IDENTITY'
    assert not root.exists()


def test_review_queue_exposes_resolution_identity_for_recovery(tmp_path):
    from kelmo.resolution import Resolution
    from test_resolution import pending
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)), Service(root) as service:
        operation = pending(service)
        resolver = Resolution(service)
        request = resolver.claim()
        resolver.submit(request['id'], request['token'], {'unresolved': True})
        row = next(item for item in service.queue() if item['id'] == operation['id'])
        assert row['resolution_id'] == request['id']
        assert row['resolution_state'] == 'review'


def test_resolution_submit_reports_unresolved_proposal_as_review_needed(tmp_path, monkeypatch, capsys):
    from kelmo.resolution import Resolution
    from test_resolution import pending
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)), Service(root) as service:
        operation = pending(service)
        request = Resolution(service).claim()
    monkeypatch.setattr('sys.stdin', io.StringIO('{"unresolved":true}'))
    assert main(['resolution', 'submit', request['id'], '--token', request['token'],
                 '--vault', str(root), '--json'], executable='/opt/private-runtime/bin/kelmo') != 0
    response = json.loads(capsys.readouterr().out)
    assert response['error']['code'] == 'E_UNRESOLVED'
    assert response['operation']['id'] == operation['id']
    assert response['operation']['state'] == 'needs_review'
    assert response['error']['resolution_id'] == request['id']
    assert 'resolution reopen' in response['error']['reopen']
    assert shlex.split(response['error']['reopen'])[0] == '/opt/private-runtime/bin/kelmo'
    assert 'retry' not in response['error']


def test_interactive_empty_label_has_mode_appropriate_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda *args: '')
    assert main(['init', '--vault', str(tmp_path / 'vault')]) != 0
    error = capsys.readouterr().err
    assert 'label' in error
    assert 'non-interactive' not in error


@pytest.mark.parametrize('code', ['E_BUSY', 'E_CLAIM'])
def test_worker_batch_retains_completed_results_when_another_writer_takes_lock(tmp_path, monkeypatch, capsys, code):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    with Service(root) as service:
        first = service.capture('First synthetic evidence.', 'first.md', 'lead')
        second = service.capture('Second synthetic evidence.', 'second.md', 'lead')
    run = Service.run

    def busy_on_second(service, operation_id):
        if operation_id == second['id']:
            raise KelmoError(code, 'Synthetic writer took the lock.')
        return run(service, operation_id)

    monkeypatch.setattr(Service, 'run', busy_on_second)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        assert main(['worker', 'run', '--vault', str(root), '--json']) == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert [(item['id'], item['state']) for item in result['operations']] == [
        (first['id'], 'archived'), (second['id'], 'queued')]
    assert result['deferred_operations'] == [second['id']]


@pytest.mark.parametrize('payload', ['[]', 'null', '5', '"text"'])
def test_hook_requires_an_object_payload(tmp_path, monkeypatch, capsys, payload):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    monkeypatch.setattr('sys.stdin', io.StringIO(payload))
    assert main(['hook', 'capture', '--vault', str(root), '--json']) != 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_INPUT'


def test_search_model_failure_returns_safe_lexical_result(tmp_path, monkeypatch, capsys):
    from kelmo.store.errors import ArchiveEmbeddingError
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')

    def unavailable(*args, **kwargs):
        raise ArchiveEmbeddingError('PRIVATE_DEBUG_SENTINEL')

    monkeypatch.setattr('kelmo.store.memory.search.embed', unavailable)
    assert main(['search', 'synthetic', '--vault', str(root), '--json']) == 0
    response = capsys.readouterr().out
    result = json.loads(response)['result']
    assert result['items'] == []
    assert result['search'] == {
        'mode': 'lexical', 'degraded': True, 'reason': 'embedding_unavailable',
    }
    assert 'PRIVATE_DEBUG_SENTINEL' not in response


@pytest.mark.parametrize('query', ['', '   '])
def test_empty_search_is_an_input_error(tmp_path, capsys, query):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    assert main(['search', query, '--vault', str(root), '--json']) != 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_INPUT'


def test_unhealthy_doctor_points_at_checks_instead_of_operation_retry(tmp_path, capsys):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    assert main(['doctor', '--vault', str(root), '--json']) != 0
    result = json.loads(capsys.readouterr().out)
    assert result['error']['code'] == 'E_HEALTH'
    assert 'retry' not in result['error']
    assert 'checks' in result['error']['message']
    assert result['result']['checks']['embedding_model'] is False


@pytest.mark.parametrize('fault,expected', [('busy', 'E_BUSY'), ('corrupt', 'E_STATE')])
def test_database_faults_have_safe_recovery_guidance(tmp_path, monkeypatch, capsys, fault, expected):
    import sqlite3
    path = tmp_path / 'synthetic.db'
    if fault == 'busy':
        first = sqlite3.connect(path)
        first.execute('CREATE TABLE fixture(value)')
        first.execute('BEGIN EXCLUSIVE')
        second = sqlite3.connect(path, timeout=0)
    else:
        path.write_bytes(b'Synthetic damaged database')
        first = None
        second = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.Error) as caught:
            second.execute('SELECT * FROM sqlite_master').fetchall()
        error = caught.value
    finally:
        second.close()
        if first:
            first.close()

    def unavailable(*args):
        raise error

    monkeypatch.setattr(Service, '__init__', unavailable)
    assert main(['status', '--vault', str(tmp_path / 'vault'), '--json']) != 0
    response = json.loads(capsys.readouterr().out)['error']
    assert response['code'] == expected
    assert ('retry' if fault == 'busy' else 'doctor') in response['message']
    assert str(path) not in response['message']
