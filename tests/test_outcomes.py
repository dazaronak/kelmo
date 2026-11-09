import json

import pytest

from test_fact_checks import content
from kelmo import editor, outcomes
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo.wiki import engine

pytest_plugins = ['test_fact_checks']


def settled(service, operation_id):
    return service.journal.get(operation_id).get('outcome')


def promote(service, text, key='fixture.fact', check=None):
    operation = service.capture(content(check, text=text, key=key), key + '.md', 'lead', 'promote')
    return service.run(operation['id'])


def test_promote_records_the_fact_it_applied(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    operation = promote(service, 'A durable fact.', check={'result': 'not_found', 'search_id': receipt['search_id']})
    assert operation['state'] == 'applied'
    assert settled(service, operation['id']) == {
        'disposition': 'applied', 'reason': 'fact applied: fixture.fact'}
    assert service.trace(operation['id'])['operation']['outcome']['disposition'] == 'applied'


def test_archive_intent_records_archive_only(service):
    operation = service.capture('An ordinary note with no wiki change.', 'plain.md', 'reviewer', 'archive')
    result = service.run(operation['id'])
    assert result['state'] == 'archived'
    assert settled(service, operation['id']) == {
        'disposition': 'archive-only', 'reason': 'captured as evidence; no fact promotion was requested'}


def test_promote_that_changes_nothing_is_not_reported_as_applied(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    promote(service, 'A durable fact.', check={'result': 'not_found', 'search_id': receipt['search_id']})
    fact = engine.load_registry(service.root)['facts']['fixture.fact']
    check = {'result': 'existing', 'fact_key': 'fixture.fact', 'revision': fact['revision']}
    operation = service.capture(content(check, text='A durable fact.', revision=fact['revision']),
                                'again.md', 'lead', 'promote')
    result = service.run(operation['id'])
    assert result['state'] == 'applied'
    outcome = settled(service, operation['id'])
    assert outcome['disposition'] == 'archive-only'
    assert 'adds no new fact' in outcome['reason']


def test_unresolved_source_records_the_repair_reason(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    check = {'result': 'not_found', 'search_id': receipt['search_id']}
    first = service.capture(content(check), 'first.md', 'lead', 'promote')
    duplicate = service.capture(content(check, key='fixture.duplicate'), 'second.md', 'lead', 'promote')
    assert service.run(first['id'])['state'] == 'applied'
    result = service.run(duplicate['id'])
    assert result['state'] == 'needs_resolution'
    outcome = settled(service, duplicate['id'])
    assert outcome['disposition'] == 'unresolved'
    assert outcome['reason'].startswith('E_FACT_CHECK: ')


def test_status_counts_each_outcome(service):
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    promote(service, 'A durable fact.', check={'result': 'not_found', 'search_id': receipt['search_id']})
    service.run(service.capture('An ordinary note.', 'plain.md', 'reviewer', 'archive')['id'])
    service.capture('Waiting note.', 'waiting.md', 'reviewer', 'archive')
    assert service.status()['outcomes'] == {'applied': 1, 'archive-only': 1, 'unresolved': 0}
    assert service.status()['summary'] == {
        'total': 3, 'pending': 1, 'captured': 0, 'applied': 1,
        'archive_only': 1, 'failed': 0, 'needs_review': 0}


def test_outcome_survives_reopen_and_is_replaced_when_the_source_settles(service):
    from kelmo.resolution import Resolution
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    check = {'result': 'not_found', 'search_id': receipt['search_id']}
    promote(service, 'A durable fact.', check=check)
    fact = engine.load_registry(service.root)['facts']['fixture.fact']
    stale = {'result': 'existing', 'fact_key': 'fixture.fact', 'revision': fact['revision']}
    first = service.capture(content(stale, text='First update.', revision=fact['revision']), 'one.md', 'lead', 'promote')
    second = service.capture(content(stale, text='Second update.', revision=fact['revision']), 'two.md', 'lead', 'promote')
    service.run(first['id'])
    unresolved = service.run(second['id'])
    with Service(service.root) as reopened:
        assert settled(reopened, unresolved['id'])['disposition'] == 'unresolved'
        queue = Resolution(reopened)
        request = queue.claim()
        assert queue.submit(request['id'], request['token'], {'unresolved': True})['state'] == 'needs_review'
        assert settled(reopened, unresolved['id'])['disposition'] == 'unresolved'
        assert any(row['id'] == unresolved['id'] and row['outcome']['disposition'] == 'unresolved'
                   for row in reopened.queue())
        queue.reopen(request['id'])
        assert settled(reopened, unresolved['id']) is None
        changes = json.loads(json.dumps(request['payload']['changes']))
        for change in changes:
            change['expected_revision'] = engine.load_registry(reopened.root)['facts']['fixture.fact']['revision']
        request = queue.claim()
        applied = queue.submit(request['id'], request['token'], {'changes': changes})
        assert applied['state'] == 'applied'
        assert settled(reopened, unresolved['id'])['disposition'] == 'applied'


def test_an_exhaustion_reason_names_the_cause_the_facts_and_the_attempts(service):
    reason = outcomes.exhaustion_reason('E_GIT', 3, {'fixture.fact', 'fixture.second'})
    assert reason == ('E_ATTEMPTS: Git refused the wiki commit for fixture.fact, fixture.second '
                      'after 3 attempt(s)')
    assert outcomes.exhaustion_reason('E_UNKNOWN', 1, set()) == (
        'E_ATTEMPTS: the worker could not complete this operation after 1 attempt(s)')


def test_an_operator_retry_unsettles_an_exhausted_source(service):
    operation = service.capture('Retry evidence.', 'retry.md', 'reviewer', 'archive')
    service.run(operation['id'])
    assert settled(service, operation['id'])['disposition'] == 'archive-only'
    service.journal.stage(operation['id'], 'failed', terminal=True, error_code='E_PROCESSING')
    outcomes.record(service.journal.conn, operation['id'], 'unresolved',
                    outcomes.exhaustion_reason('E_PROCESSING', 3, {'fixture.fact'}))
    assert settled(service, operation['id'])['disposition'] == 'unresolved'
    assert service.retry(operation['id'])['state'] == 'queued'
    assert settled(service, operation['id']) is None
    assert service.status()['outcomes'] == {'applied': 0, 'archive-only': 0, 'unresolved': 0}


def test_an_older_vault_gains_the_outcome_table_on_reopen(service):
    with service.journal.conn:
        service.journal.conn.execute('DROP TABLE source_outcomes')
    with Service(service.root) as reopened:
        assert reopened.status()['outcomes'] == {'applied': 0, 'archive-only': 0, 'unresolved': 0}
        receipt = reopened.reconcile('A durable fact.', 'projects/fixture/fixture.md')
        operation = reopened.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                                     'after.md', 'lead', 'promote')
        assert reopened.run(operation['id'])['state'] == 'applied'
        assert reopened.journal.get(operation['id'])['outcome']['disposition'] == 'applied'


def test_an_outcome_without_a_disposition_or_reason_is_refused(service):
    with pytest.raises(KelmoError) as raised:
        outcomes.record(service.journal.conn, 'op', 'published', 'a fact was applied')
    assert raised.value.code == 'E_OUTCOME'
    with pytest.raises(KelmoError) as raised:
        outcomes.record(service.journal.conn, 'op', 'applied', '   ')
    assert raised.value.code == 'E_OUTCOME'
    with pytest.raises(KelmoError) as raised:
        outcomes.record(service.journal.conn, 'op', 'applied', 'x' * 401)
    assert raised.value.code == 'E_OUTCOME'
    assert service.journal.conn.execute('SELECT COUNT(*) FROM source_outcomes').fetchone()[0] == 0


def test_a_long_reason_names_the_first_facts_and_counts_the_rest(service):
    keys = ['fixture.fact.' + str(index) for index in range(12)]
    reason = outcomes.fact_reason(keys, 'fact applied: ')
    assert len(reason) <= outcomes.REASON_LIMIT
    assert reason.count('fixture.fact.') == 8
    assert reason.endswith('(+4 more)')


LONG_KEYS = ['project.some-page.a-rather-long-fact-key-about-one-thing-' + str(index) for index in range(9)]


def test_long_fact_keys_stay_whole_and_the_reason_fits_its_limit():
    reason = outcomes.fact_reason(LONG_KEYS, 'fact applied: ')
    assert len(reason) <= outcomes.REASON_LIMIT
    named = reason.removeprefix('fact applied: ').split(' (+')[0].split(', ')
    assert set(named) <= set(LONG_KEYS)
    assert reason.endswith('(+' + str(len(LONG_KEYS) - len(named)) + ' more)')
    assert outcomes.validate('applied', reason) == ('applied', reason)


def test_a_published_proposal_with_long_keys_and_other_changes_fits_its_limit():
    delta = {name: [] for name in editor.LIMITS}
    delta['facts'] = [{'fact_key': key} for key in LONG_KEYS]
    other = next(name for name in editor.LIMITS if name != 'facts')
    delta[other] = ['one']
    reason = editor._summary(delta)
    assert len(reason) <= outcomes.REASON_LIMIT
    assert reason.startswith('editor proposal published: facts ')
    assert reason.endswith('more); 1 ' + other)


def test_one_key_longer_than_the_limit_is_counted_and_not_cut():
    key = 'project.page.' + 'x' * outcomes.REASON_LIMIT
    assert outcomes.fact_reason([key], 'fact applied: ') == 'fact applied: (+1 more)'


def failing_note(service, monkeypatch, filename):
    """One note that fails in the archive phase, which is how a real publication failure arrives."""
    from kelmo.store.errors import ArchiveValidationError

    def refused(*args, **kwargs):
        raise ArchiveValidationError('Synthetic archive refusal.')

    monkeypatch.setattr(service.archive_api, 'capture', refused)
    operation = service.capture('Synthetic failing note.', filename, 'lead', 'archive')
    return service.run(operation['id'])


def test_status_names_the_failed_notes_with_their_reason_and_time(service, monkeypatch):
    first = failing_note(service, monkeypatch, 'first-fault.md')
    second = failing_note(service, monkeypatch, 'second-fault.md')
    failed = service.status()['failed_operations']
    assert [item['id'] for item in failed] == [second['id'], first['id']], 'newest first'
    for item, operation in zip(failed, (second, first)):
        assert item['filename'] == operation['filename']
        assert item['error_code'] == 'E_INPUT'
        assert item['reason'].startswith('E_INPUT: Synthetic archive refusal.')
        assert item['created_at'] == operation['created_at']
    assert service.status()['summary']['failed'] == 2


def test_status_reports_only_the_newest_twenty_failed_notes(service, monkeypatch):
    for index in range(25):
        failing_note(service, monkeypatch, 'fault-%02d.md' % index)
    status = service.status()
    failed = status['failed_operations']
    assert len(failed) == 20
    assert status['summary']['failed'] == 25
    reported = {item['id'] for item in failed}
    all_failed = [row['id'] for row in service.journal.conn.execute(
        'SELECT id FROM operations WHERE state=? ORDER BY created_at DESC,id DESC', ('failed',))]
    assert reported == set(all_failed[:20]), 'the cap keeps the newest'
    kept = [item['created_at'] for item in failed]
    assert kept == sorted(kept, reverse=True), 'newest first'
    dropped = [service.journal.get(operation_id)['created_at'] for operation_id in all_failed[20:]]
    assert min(kept) >= max(dropped), 'the oldest are the ones left out'


def test_a_vault_without_failures_reports_none(service):
    service.capture('An ordinary note.', 'plain.md', 'reviewer', 'archive')
    assert service.status()['failed_operations'] == []
