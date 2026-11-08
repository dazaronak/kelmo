"""kelmo forget: hidden from every reader, kept in storage, visible in the trace."""
import json
from unittest.mock import patch

import pytest

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.errors import KelmoError


@pytest.fixture
def service(tmp_path):
    from kelmo.service import Service

    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node', project='fixture')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)), Service(root) as value:
        yield value


def archived(service, text='The espresso machine in the office kitchen needs descaling every two months.'):
    operation = service.capture(text + '\n', 'espresso.md', 'implementer')
    return service.run(operation['id'])


def test_search_and_list_stop_reporting_a_forgotten_record(service):
    record_id = archived(service)['archive_id']
    assert service.forget(record_id)['forgotten'] is True

    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        assert service.search('descaling', with_metadata=True)['items'] == []
    assert [row['id'] for row in service.list()['items']] == []


def test_search_excludes_a_forgotten_record_in_hybrid_mode(service):
    record_id = archived(service)['archive_id']
    assert service.forget(record_id)['forgotten'] is True

    with patch('kelmo.store.memory.search.embed', return_value=bytes(384 * 4)):
        result = service.search('descaling', with_metadata=True)
    assert result['search'] == {'mode': 'hybrid', 'degraded': False, 'reason': None}
    assert result['items'] == []


def test_show_refuses_a_forgotten_record_and_an_unknown_id(service):
    from kelmo.errors import KelmoError

    record_id = archived(service)['archive_id']
    service.forget(record_id)

    with pytest.raises(KelmoError) as refused:
        service.show(record_id)
    assert refused.value.code == 'E_FORGOTTEN'
    with pytest.raises(KelmoError) as absent:
        service.show('00000000-0000-0000-0000-000000000000')
    assert absent.value.code == 'E_NOT_FOUND'


def test_status_counts_stored_records_and_names_the_hidden_ones(service):
    hidden_id = archived(service)['archive_id']
    kept_id = archived(service, 'The demo gateway retries its uploads three times.')['archive_id']
    service.forget(hidden_id)

    status = service.status()
    assert status['archive_records'] == 2
    assert status['forgotten_records'] == 1
    assert [row['id'] for row in service.list()['items']] == [kept_id]


def test_forget_records_one_forgotten_stage_on_the_operation_that_archived_it(service):
    operation = archived(service)
    first = service.forget(operation['archive_id'])
    second = service.forget(operation['archive_id'])

    assert first == {'id': operation['archive_id'], 'forgotten': True,
                     'forgotten_at': first['forgotten_at'], 'operations': [operation['id']]}
    assert second['forgotten'] is False and second['forgotten_at'] == first['forgotten_at']
    events = service.trace(operation['id'])['events']
    stages = [event['stage'] for event in events]
    assert stages.count('forgotten') == 1
    assert stages.index('forgotten') > stages.index('archived')
    forgotten = next(event for event in events if event['stage'] == 'forgotten')
    assert forgotten['details'] == {'record_id': operation['archive_id'], 'forgotten_at': first['forgotten_at']}


def test_a_forgotten_record_still_answers_for_the_operation_that_owns_its_receipt(service, monkeypatch):
    from kelmo.wiki import engine

    commit = engine._commit_registry
    monkeypatch.setattr(engine, '_commit_registry',
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('page directory is unwritable')))
    receipt = service.reconcile('The demo CLI prints JSON envelopes.', 'projects/fixture/fixture.md')
    change = {'fact_key': 'fixture.cli', 'target': 'projects/fixture/fixture.md',
              'text': 'The demo CLI prints JSON envelopes.', 'status': 'active',
              'expected_revision': None, 'replaces': []}
    check = {'fact_key': 'fixture.cli', 'verified_match': {'result': 'not_found', 'search_id': receipt['search_id']}}
    note = ('---\nproject: fixture\ndomain: dev\nsource: manual\n---\n\n```wiki-changes\n'
            + json.dumps([change]) + '\n```\n```fact-checks\n' + json.dumps([check]) + '\n```\n')
    operation = service.run(service.capture(note, 'cli.md', 'lead', 'promote')['id'])
    assert operation['state'] == 'failed' and operation['durable_stage'] == 'archived'
    monkeypatch.setattr(engine, '_commit_registry', commit)

    assert service.forget(operation['archive_id'])['forgotten'] is True
    retried = service.run(service.retry(operation['id'])['id'])

    assert retried['state'] == 'applied' and retried['archive_id'] == operation['archive_id']
    assert service.status()['archive_records'] == 1
    events = service.trace(operation['id'])['events']
    stages = [event['stage'] for event in events]
    assert stages.index('archived') < stages.index('failed') < stages.index('applied')
    receipts = {event['details']['archive_id'] for event in events if 'archive_id' in event['details']}
    assert receipts == {operation['archive_id']}


def test_a_vault_created_before_the_marker_table_still_answers_forget(service):
    from kelmo.service import Service

    record_id = archived(service)['archive_id']
    with service.archive:
        service.archive.execute('DROP TABLE record_visibility')

    with Service(service.root) as reopened:
        assert reopened.forget(record_id)['forgotten'] is True
        assert [row['id'] for row in reopened.list()['items']] == []
        with pytest.raises(KelmoError) as refused:
            reopened.show(record_id)
        assert refused.value.code == 'E_FORGOTTEN'


def test_forget_show_and_unknown_id_through_the_cli(service, capsys):
    from kelmo.cli import main

    record_id = archived(service)['archive_id']
    assert main(['forget', record_id, '--vault', str(service.root), '--json']) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope['ok'] is True
    assert envelope['result']['forgotten'] is True and envelope['result']['id'] == record_id

    assert main(['show', record_id, '--vault', str(service.root), '--json']) == 1
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_FORGOTTEN'
    assert main(['forget', '00000000-0000-0000-0000-000000000000', '--vault', str(service.root), '--json']) == 1
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_NOT_FOUND'


def test_forget_needs_a_record_id_and_is_listed_in_the_command_contract(tmp_path, capsys):
    from kelmo.cli import main
    from kelmo.interface import build_parser

    _, commands = build_parser('test')
    assert 'forget' in commands.choices
    assert main(['forget', '--vault', str(tmp_path / 'absent'), '--json']) == 2
    assert 'Traceback' not in capsys.readouterr().err
