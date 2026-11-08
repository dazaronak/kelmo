"""kelmo add --supersedes: a correction replaces the record it corrects instead of ranking beside it."""
import json
from unittest.mock import patch

import pytest

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.errors import KelmoError

STALE = 'The parser directory holds 24 rules and 33 test fixtures.\n'
FRESH = 'The parser directory holds 26 rules and 35 test fixtures.\n'


@pytest.fixture
def service(tmp_path):
    from kelmo.service import Service

    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)), Service(root) as value:
        yield value


def store(service, text, supersedes=None, filename='rule-counts.md'):
    return service.run(service.capture(text, filename, 'implementer', supersedes=supersedes)['id'])


def lexical(service, query, **filters):
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        return service.search(query, with_metadata=True, **filters)['items']


def test_the_replacement_is_found_and_the_record_it_corrects_is_not(service):
    stale = store(service, STALE)['archive_id']
    fresh = store(service, FRESH, supersedes=stale)

    assert fresh['state'] == 'archived'
    assert [item['id'] for item in lexical(service, 'parser rules test fixtures')] == [fresh['archive_id']]
    status = service.status()
    assert (status['archive_records'], status['forgotten_records'], status['superseded_records']) == (2, 1, 1)


def test_show_names_the_chain_in_both_directions(service):
    stale = store(service, STALE)['archive_id']
    fresh = store(service, FRESH, supersedes=stale)['archive_id']

    shown = service.show(fresh)
    assert shown['supersedes'] == stale and shown['superseded_by'] == []
    with pytest.raises(KelmoError) as refused:
        service.show(stale)
    assert refused.value.code == 'E_FORGOTTEN'
    assert fresh in str(refused.value)


def test_the_trace_records_the_replacement_once(service):
    stale = store(service, STALE)['archive_id']
    operation = store(service, FRESH, supersedes=stale)

    stages = [event['stage'] for event in service.trace(operation['id'])['events']]
    assert stages.count('superseded') == 1
    assert stages.index('superseded') < stages.index('archived')
    captured = next(event for event in service.trace(operation['id'])['events'] if event['stage'] == 'captured')
    assert captured['details']['supersedes'] == stale


def test_an_unknown_superseded_id_is_refused_before_anything_is_captured(service):
    with pytest.raises(KelmoError) as refused:
        service.capture(FRESH, 'rule-counts.md', 'implementer', supersedes='00000000-0000-0000-0000-000000000000')
    assert refused.value.code == 'E_NOT_FOUND'
    assert service.status()['operations'] == {}


def test_the_same_bytes_as_a_plain_add_and_as_a_replacement_are_two_requests(service):
    stale = store(service, STALE)['archive_id']
    plain = service.capture(FRESH, 'rule-counts.md', 'implementer')
    replacing = service.capture(FRESH, 'rule-counts.md', 'implementer', supersedes=stale)

    assert plain['id'] != replacing['id']
    assert service.capture(FRESH, 'rule-counts.md', 'implementer', supersedes=stale)['id'] == replacing['id']


def test_a_record_cannot_supersede_itself(service):
    stale = store(service, STALE)['archive_id']
    with pytest.raises(KelmoError) as refused:
        service.capture(STALE, 'rule-counts.md', 'implementer', supersedes=stale)

    assert refused.value.code == 'E_INPUT'
    assert service.show(stale)['id'] == stale


def test_the_bytes_of_a_forgotten_record_cannot_replace_its_replacement(service):
    """Re-adding the old text under its old name returns the hidden record; it must not hide the visible one."""
    stale = store(service, STALE)['archive_id']
    fresh = store(service, FRESH, supersedes=stale)['archive_id']
    back = store(service, STALE, supersedes=fresh)

    assert back['state'] == 'failed' and back['error_code'] == 'E_CONFLICT'
    assert service.show(fresh)['id'] == fresh
    assert [item['id'] for item in lexical(service, 'parser rules test fixtures')] == [fresh]


def test_promotion_cannot_carry_supersedes(service):
    with pytest.raises(KelmoError) as refused:
        service.capture(FRESH, 'rule-counts.md', 'lead', 'promote', supersedes='any-id')
    assert refused.value.code == 'E_INPUT'


def test_a_failed_replacement_forgets_nothing_until_it_is_archived(service):
    stale = store(service, STALE)['archive_id']
    with patch('kelmo.store.memory.store.embed', side_effect=ArchiveEmbeddingError('missing model')):
        failed = store(service, FRESH, supersedes=stale)
    assert failed['state'] == 'failed' and failed['error_code'] == 'E_EMBEDDING'
    assert service.show(stale)['id'] == stale

    retried = service.run(service.retry(failed['id'])['id'])
    assert retried['state'] == 'archived'
    assert [item['id'] for item in lexical(service, 'parser rules test fixtures')] == [retried['archive_id']]


def test_a_journal_created_before_the_column_still_opens_and_supersedes(tmp_path):
    import sqlite3

    from kelmo.service import Service

    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node')
    journal = root / '.state/journal.sqlite3'
    with sqlite3.connect(journal) as connection:
        connection.execute('ALTER TABLE operations DROP COLUMN supersedes')
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)), Service(root) as service:
        stale = store(service, STALE)['archive_id']
        assert store(service, FRESH, supersedes=stale)['state'] == 'archived'


def test_supersedes_through_the_cli(service, tmp_path, capsys):
    from kelmo.cli import main

    stale = store(service, STALE)['archive_id']
    source = tmp_path / 'rule-counts.md'
    source.write_text(FRESH)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(384 * 4)):
        code = main(['add', '--file', str(source), '--role', 'implementer', '--supersedes', stale, '--wait',
                     '--vault', str(service.root), '--json'])
    assert code == 0
    operation = json.loads(capsys.readouterr().out)['result']['operation']
    assert operation['state'] == 'archived' and operation['supersedes'] == stale


def test_a_correction_of_an_already_corrected_record_is_refused_with_the_current_head(service):
    stale = store(service, STALE)['archive_id']
    fresh = store(service, FRESH, supersedes=stale)['archive_id']

    with pytest.raises(KelmoError) as refused:
        service.capture('The parser directory holds 27 rules.\n', 'rule-counts.md', 'implementer',
                        supersedes=stale)
    assert refused.value.code == 'E_CONFLICT'
    assert fresh in str(refused.value)


def test_two_queued_corrections_of_one_record_do_not_fork_the_chain(service):
    stale = store(service, STALE)['archive_id']
    first = service.capture(FRESH, 'rule-counts.md', 'implementer', supersedes=stale)['id']
    second = service.capture('The parser directory holds 27 rules.\n', 'rule-counts.md', 'implementer',
                             supersedes=stale)['id']
    winner = service.run(first)['archive_id']

    lost = service.run(second)
    assert lost['state'] == 'failed' and lost['error_code'] == 'E_CONFLICT'
    assert not lost['archive_id'], 'the refused correction never became a visible record'
    assert service.show(winner)['superseded_by'] == []
    assert [item['id'] for item in lexical(service, 'parser')] == [winner]
