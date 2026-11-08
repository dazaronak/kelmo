import json
import shutil

import pytest

from test_fact_checks import content
from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']


def pending(service):
    from kelmo.wiki import engine
    receipt = service.reconcile('A durable fact.', 'projects/fixture/fixture.md')
    created = service.capture(content({'result': 'not_found', 'search_id': receipt['search_id']}),
                              'created.md', 'lead', 'promote')
    service.run(created['id'])
    fact = engine.load_registry(service.root)['facts']['fixture.fact']
    check = {'result': 'existing', 'fact_key': 'fixture.fact', 'revision': fact['revision']}
    one = service.capture(content(check, text='First update.', revision=fact['revision']),
                          'one.md', 'lead', 'promote')
    two = service.capture(content(check, text='Second update.', revision=fact['revision']),
                          'two.md', 'lead', 'promote')
    service.run(one['id'])
    return service.run(two['id'])


def publish_concurrent(service, key, text, title):
    """Another writer's publication: a new or changed fact on the fixture page, committed under the queue."""
    import copy

    import registry_seed
    from kelmo.wiki import engine
    registry = engine.load_registry(service.root)
    current = registry['facts']['fixture.fact']
    stored = copy.deepcopy(registry['facts'].get(key, current))
    stored.update(fact_key=key, id=engine.digest(key)[:12], text=text)
    stored['previous_revision'] = registry['facts'][key]['revision'] if key in registry['facts'] else None
    stored['revision'] = engine.entity_revision(stored)
    registry['facts'][key] = stored
    registry_seed.publish(service.root, registry, title)


def resolved_changes(request):
    changes = json.loads(json.dumps(request['payload']['changes']))
    for change in changes:
        prior = request['payload']['facts'].get(change['fact_key'])
        change['expected_revision'] = prior['revision'] if prior else None
    return changes


def test_conflict_persists_request_and_accepts_one_scoped_response(service):
    from kelmo.resolution import Resolution
    from kelmo.service import Service
    operation = pending(service)
    assert operation['state'] == 'needs_resolution'
    assert operation['archive_id']
    with Service(service.root) as reopened:
        queue = Resolution(reopened)
        request = queue.claim()
        assert request['operation_id'] == operation['id']
        assert queue.claim() is None
        changes = resolved_changes(request)
        response = {'changes': changes}
        result = queue.submit(request['id'], request['token'], response)
        assert result['state'] == 'applied'
        assert queue.submit(request['id'], request['token'], response)['state'] == 'applied'
        assert reopened.status()['archive_records'] == 3


def test_unrelated_changes_are_rejected_without_wiki_write(service):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    changes = json.loads(json.dumps(request['payload']['changes']))
    changes[0]['fact_key'] = 'unrelated.fact'
    with pytest.raises(KelmoError):
        queue.submit(request['id'], request['token'], {'changes': changes})
    assert service.status()['facts'] == 1


def test_unresolved_result_remains_visible(service):
    from kelmo.resolution import Resolution
    operation = pending(service)
    queue = Resolution(service)
    request = queue.claim()
    queue.submit(request['id'], request['token'], {'unresolved': True})
    assert service.journal.get(operation['id'])['state'] == 'needs_review'


def test_expired_claim_cannot_submit_after_reclaim(service):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    old = queue.claim()
    with queue.conn:
        queue.conn.execute('UPDATE resolutions SET expires=0 WHERE id=?', (old['id'],))
    current = queue.claim()
    with pytest.raises(KelmoError):
        queue.submit(old['id'], old['token'], {'changes': old['payload']['changes']})
    assert queue.submit(current['id'], current['token'],
                        {'changes': resolved_changes(current)})['state'] == 'applied'


def test_recovery_after_wiki_commit_reuses_saved_response(service, monkeypatch):
    from kelmo.resolution import Resolution
    from kelmo.wiki import engine
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    commit = engine._commit_registry

    def interrupted(*args, **kwargs):
        commit(*args, **kwargs)
        raise SystemExit(99)

    monkeypatch.setattr(engine, '_commit_registry', interrupted)
    with pytest.raises(SystemExit):
        queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})
    monkeypatch.setattr(engine, '_commit_registry', commit)
    with queue.conn:
        queue.conn.execute('UPDATE resolutions SET expires=0 WHERE id=?', (request['id'],))
    resumed = queue.claim()
    assert resumed['response'] is not None
    assert queue.submit(resumed['id'], resumed['token'], resumed['response'])['state'] == 'applied'
    assert service.status()['archive_records'] == 3


def test_unrelated_fact_change_does_not_stale_claimed_resolution(service):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    publish_concurrent(service, 'unrelated.fact', 'Unrelated.', 'chore: Add unrelated fact')
    assert queue.submit(request['id'], request['token'],
                        {'changes': resolved_changes(request)})['state'] == 'applied'


def test_relevant_fact_change_moves_claimed_resolution_to_review(service):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    publish_concurrent(service, 'fixture.fact', 'Concurrent relevant update.', 'chore: Update relevant fact')
    result = queue.submit(request['id'], request['token'],
                          {'changes': resolved_changes(request)})
    assert result['state'] == 'needs_review'
    assert result['error_code'] == 'E_STALE'


def test_invalid_revision_response_is_not_saved_and_executor_can_correct_it(service):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    first = queue.claim()
    invalid = json.loads(json.dumps(first['payload']['changes']))
    invalid[0]['expected_revision'] = '0' * 64
    with pytest.raises(KelmoError) as raised:
        queue.submit(first['id'], first['token'], {'changes': invalid})
    assert raised.value.code == 'E_PROPOSAL'
    row = queue.conn.execute('SELECT response FROM resolutions WHERE id=?', (first['id'],)).fetchone()
    assert row['response'] is None


def test_reopen_rebases_review_and_preserves_operation_evidence(service):
    from kelmo.resolution import Resolution
    operation = pending(service)
    queue = Resolution(service)
    request = queue.claim()
    queue.submit(request['id'], request['token'], {'unresolved': True})
    before = service.journal.get(operation['id'])
    reopened = queue.reopen(request['id'])
    after = service.journal.get(operation['id'])
    assert reopened['state'] == 'pending'
    assert after['state'] == 'needs_resolution'
    assert after['source_hash'] == before['source_hash']
    assert after['archive_id'] == before['archive_id']
    assert queue.claim()['response'] is None
    assert service.trace(operation['id'])['events'][-1]['stage'] == 'resolution_reopened'


def test_public_cli_dispatch_reopens_review(service):
    from kelmo.cli import dispatch
    from kelmo.interface import build_parser
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    queue.submit(request['id'], request['token'], {'unresolved': True})
    parser, commands = build_parser('test')
    args = parser.parse_args(['resolution', 'reopen', request['id'],
                              '--vault', str(service.root), '--json'])
    assert dispatch(args, commands)['job']['state'] == 'pending'


def test_reopen_preserves_validated_response_and_rejects_replacement(service, monkeypatch):
    from kelmo.daemon import tick
    from kelmo.resolution import Resolution
    from kelmo.wiki import engine
    operation = pending(service)
    commit = engine._commit_registry
    monkeypatch.setattr(engine, '_commit_registry',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('dirty wiki')))
    monkeypatch.setattr('kelmo.daemon.execute',
                        lambda argv, payload, **kwargs: {'changes': resolved_changes({'payload': payload})})
    tick(service, ['/synthetic/executor'], max_attempts=1)
    queue = Resolution(service)
    job = queue.list()[0]
    saved = queue.conn.execute('SELECT response FROM resolutions WHERE id=?', (job['id'],)).fetchone()['response']
    assert saved is not None
    queue.reopen(job['id'])
    request = queue.claim()
    changed = json.loads(json.dumps(request['response']))
    changed['changes'][0]['text'] = 'Mutated replay.'
    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], changed)
    assert raised.value.code == 'E_CLAIM'
    monkeypatch.setattr(engine, '_commit_registry', commit)
    assert queue.submit(request['id'], request['token'], request['response'])['state'] == 'applied'
    assert service.journal.get(operation['id'])['archive_id']


def test_resolution_submit_rejects_symlinked_worker_lock(service, tmp_path):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    lock = service.config.state / 'worker.lock'
    lock.unlink(missing_ok=True)
    lock.symlink_to(tmp_path / 'outside.lock')
    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})
    assert raised.value.code == 'E_STATE'


def test_resolution_reopen_rejects_symlinked_worker_lock(service, tmp_path):
    from kelmo.resolution import Resolution
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    queue.submit(request['id'], request['token'], {'unresolved': True})
    lock = service.config.state / 'worker.lock'
    lock.unlink(missing_ok=True)
    lock.symlink_to(tmp_path / 'outside.lock')
    with pytest.raises(KelmoError) as raised:
        queue.reopen(request['id'])
    assert raised.value.code == 'E_STATE'


def test_resolution_reclaims_stale_local_worker_claim_before_apply(service):
    from kelmo.resolution import Resolution
    operation = pending(service)
    with service.journal.conn:
        service.journal.conn.execute('''INSERT INTO claims
            (operation_id,token,expires_at,local_worker) VALUES (?,?,?,1)''',
            (operation['id'], 'stale-local', 9999999999))
    queue = Resolution(service)
    request = queue.claim()
    result = queue.submit(request['id'], request['token'],
                          {'changes': resolved_changes(request)})
    assert result['state'] == 'applied'
    assert service.journal.conn.execute('SELECT 1 FROM claims WHERE operation_id=?',
                                        (operation['id'],)).fetchone() is None


def test_resolution_never_reclaims_external_operation_claim(service):
    from kelmo.resolution import Resolution
    operation = pending(service)
    with service.journal.conn:
        service.journal.conn.execute('''INSERT INTO claims
            (operation_id,token,expires_at,local_worker) VALUES (?,?,?,0)''',
            (operation['id'], 'external-owner', 9999999999))
    queue = Resolution(service)
    request = queue.claim()
    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})
    assert raised.value.code == 'E_CLAIM'
    claim = service.journal.conn.execute('SELECT token,local_worker FROM claims WHERE operation_id=?',
                                         (operation['id'],)).fetchone()
    assert dict(claim) == {'token': 'external-owner', 'local_worker': 0}


def test_resolution_source_invalid_utf8_is_source_error(service):
    from kelmo.resolution import Resolution
    receipt = service.reconcile('Unreadable source.', 'projects/fixture/fixture.md')
    body = content({'result': 'not_found', 'search_id': receipt['search_id']},
                   text='Unreadable source.')
    operation = service.capture(body, 'invalid-utf8.md', 'lead', 'promote')
    service.source_path(operation).write_bytes(b'\xff')
    with pytest.raises(KelmoError) as raised:
        Resolution(service).create(operation, body)
    assert raised.value.code == 'E_SOURCE'


def test_resolution_source_permission_error_is_source_error(service, monkeypatch):
    from pathlib import Path
    from kelmo.resolution import Resolution
    receipt = service.reconcile('Permission source.', 'projects/fixture/fixture.md')
    body = content({'result': 'not_found', 'search_id': receipt['search_id']},
                   text='Permission source.')
    operation = service.capture(body, 'permission.md', 'lead', 'promote')
    source = service.source_path(operation)
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', denied)
    with pytest.raises(KelmoError) as raised:
        Resolution(service).create(operation, body)
    assert raised.value.code == 'E_SOURCE'


def test_resolution_submit_refuses_parent_repository_and_keeps_the_claim(service, tmp_path):
    from kelmo.resolution import Resolution
    from kelmo.wiki import engine
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    shutil.rmtree(service.root / '.git')
    engine.git(tmp_path, 'init', '-q', '-b', 'master')
    engine.git(tmp_path, 'config', 'user.name', 'Parent User')
    engine.git(tmp_path, 'config', 'user.email', 'parent@example.invalid')
    engine.git(tmp_path, 'add', '--', service.root.name)
    engine.git(tmp_path, 'commit', '-q', '-m', 'parent baseline')
    head = engine.git(tmp_path, 'rev-parse', 'HEAD')
    status = engine.git(tmp_path, 'status', '--porcelain')

    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})

    assert raised.value.code == 'E_CONFIG'
    assert 'bound repository' in str(raised.value)
    assert engine.git(tmp_path, 'rev-parse', 'HEAD') == head
    assert engine.git(tmp_path, 'status', '--porcelain') == status
    saved = queue.conn.execute('SELECT response,state FROM resolutions WHERE id=?',
                               (request['id'],)).fetchone()
    assert saved['response'] is not None
    assert saved['state'] == 'claimed'


def test_resolution_recovery_conflict_is_safe_and_response_remains_unaccepted(service):
    from kelmo.resolution import Resolution
    from kelmo.wiki import engine
    pending(service)
    queue = Resolution(service)
    request = queue.claim()
    marker = engine.transaction_path(service.root)
    marker.write_text('{}')

    with pytest.raises(KelmoError) as raised:
        queue.submit(request['id'], request['token'], {'changes': resolved_changes(request)})

    assert raised.value.code == 'E_CONFLICT'
    assert 'kelmo doctor' in str(raised.value)
    assert marker.read_text() == '{}'
    row = queue.conn.execute('SELECT state,token,response FROM resolutions WHERE id=?',
                             (request['id'],)).fetchone()
    assert row['state'] == 'claimed'
    assert row['token'] == request['token']
    assert row['response'] is None
