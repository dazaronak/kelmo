"""Fact promotion: the worker hands a note's declared facts to the scoped editor.

The world is the editor fixture's synthetic v3 store. No model runs: the declared wiki-changes answer
the editor as a prepared proposal, so these cases prove the wiring, the typed refusals and the retry
key, never semantic quality.
"""

import json

import pytest

from test_editor_scoping import FEATURE, NOTES, SEED, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def note(world, changes, *, project='app'):
    """A promoted note: frontmatter, the declared facts and the real reconciliation receipts they need."""
    facts = [dict(change, replaces=change.get('replaces', [])) for change in changes]
    checks = world.checks([{k: fact[k] for k in ('fact_key', 'target', 'text', 'expected_revision')}
                           for fact in facts])
    return ('---\nproject: ' + project + '\n---\n\n# Promoted note\n\n```wiki-changes\n' + json.dumps(facts)
            + '\n```\n\n```fact-checks\n' + json.dumps(checks) + '\n```\n')


def promote(world, content, *, principal='requester', key='promoted'):
    from kelmo.service import Service
    with Service(world.root, bindings=world.bindings, origin=world.origins[principal]) as service:
        operation = service.capture(content, key + '.md', 'lead', 'promote', idempotency_key=key)
        return service.run(operation['id'])


def test_declared_facts_publish_through_the_editor(world):
    before = world.fact('app.export')
    content = note(world, [
        dict(fact_key='app.export-window', target=FEATURE, text='Exports run inside a two-hour window.',
             status='active', expected_revision=None),
        world.change('app.export', 'Exports run nightly at 02:00.')])
    operation = promote(world, content)
    assert operation['state'] == 'applied' and operation['terminal']
    assert world.outcome(operation['id'])['disposition'] == 'applied'
    registry = world.registry()
    assert registry['facts']['app.export-window']['text'] == 'Exports run inside a two-hour window.'
    assert registry['facts']['app.export']['text'] == 'Exports run nightly at 02:00.'
    assert registry['facts']['app.export']['revision'] != before['revision']
    assert 'Exports run inside a two-hour window.' in world.page(FEATURE)
    requests = world.service.journal.semantic_requests(operation['id'])
    assert len(requests) == 1 and requests[0]['state'] not in ('open', 'prepared')


def test_replaces_retires_the_replaced_fact(world):
    content = note(world, [
        dict(fact_key='app.notes-scope-v2', target=NOTES, text='Notes cover the export and import pipelines.',
             status='active', expected_revision=None, replaces=['app.notes-scope'])])
    operation = promote(world, content)
    assert operation['state'] == 'applied'
    registry = world.registry()
    assert registry['facts']['app.notes-scope']['status'] == 'retired'
    assert registry['facts']['app.notes-scope-v2']['status'] == 'active'


def test_restated_facts_apply_without_a_change(world):
    before = world.registry()
    content = note(world, [world.change('app.export', SEED['app.export'][1])])
    operation = promote(world, content)
    assert operation['state'] == 'applied'
    assert world.outcome(operation['id'])['disposition'] == 'archive-only'
    assert world.registry()['facts'] == before['facts']


def test_unregistered_target_fails_before_the_editor(world):
    content = note(world, [
        dict(fact_key='app.fresh-page', target='projects/app/fresh.md', text='A page the registry lacks.',
             status='active', expected_revision=None)])
    operation = promote(world, content)
    assert operation['state'] == 'failed' and operation['error_code'] == 'E_INPUT'
    assert any(event['details'].get('phase') == 'editor'
               for event in world.service.trace(operation['id'])['events'])
    assert world.service.journal.semantic_requests(operation['id']) == []


def test_person_facts_need_a_requester_origin(world):
    content = note(world, [world.change('person.desk-plant', 'Waters the desk plant on Tuesdays.')])
    refused = promote(world, content, principal='editor', key='person-editor')
    assert refused['state'] == 'failed' and refused['error_code'] == 'E_ORIGIN'
    assert world.outcome(refused['id'])['disposition'] == 'unresolved'
    content = note(world, [world.change('person.desk-plant', 'Waters the desk plant on Tuesdays.')])
    applied = promote(world, content, key='person-requester')
    assert applied['state'] == 'applied'
    assert world.fact('person.desk-plant')['text'] == 'Waters the desk plant on Tuesdays.'


def test_promotion_crash_reuses_the_published_editor_receipt(world, monkeypatch):
    from kelmo.service import Service
    with Service(world.root, bindings=world.bindings, origin=world.origins['requester']) as service:
        content = note(world, [world.change('app.export', 'Exports run at 04:00.')])
        operation = service.capture(content, 'crash.md', 'lead', 'promote')
        original = service.journal.stage

        def interrupted(operation_id, stage, **kwargs):
            if stage == 'applied':
                raise RuntimeError('Synthetic journal interruption.')
            return original(operation_id, stage, **kwargs)

        monkeypatch.setattr(service.journal, 'stage', interrupted)
        with pytest.raises(RuntimeError, match='Synthetic'):
            service.run(operation['id'])
        monkeypatch.setattr(service.journal, 'stage', original)
        assert service.run(operation['id'])['state'] == 'applied'
        assert len(service.journal.semantic_requests(operation['id'])) == 1
        assert world.fact('app.export')['text'] == 'Exports run at 04:00.'


def test_resolution_uses_current_editor_and_replays_after_publication(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.promotion import DeclaredChanges
    from kelmo.resolution import Resolution
    from kelmo.service import Service
    first = note(world, [world.change('app.export', 'Exports run at 01:00.')])
    second = note(world, [world.change('app.export', 'Exports run at 02:00.')])
    with Service(world.root, bindings=world.bindings, origin=world.origins['requester']) as service:
        one = service.capture(first, 'first.md', 'lead', 'promote')
        two = service.capture(second, 'second.md', 'lead', 'promote')
        assert service.run(one['id'])['state'] == 'applied'
        assert service.run(two['id'])['state'] == 'needs_resolution'
        queue = Resolution(service)
        request = queue.claim()
        changes = request['payload']['changes']
        for change in changes:
            change['expected_revision'] = request['payload']['facts'][change['fact_key']]['revision']
        with pytest.raises(KelmoError, match='owned by its promotion'):
            service.editor.edit(two['id'], origin=world.origins['requester'], idempotency_key='bypass',
                                project='app', selected=[FEATURE], executor=DeclaredChanges({}),
                                budget_tokens=24000)
        original = service.journal.stage

        def fail_after_publish(operation_id, stage, **kwargs):
            if operation_id == two['id'] and stage == 'applied':
                raise RuntimeError('Synthetic crash after resolution publication.')
            return original(operation_id, stage, **kwargs)

        monkeypatch.setattr(service.journal, 'stage', fail_after_publish)
        response = {'changes': changes}
        with pytest.raises(RuntimeError, match='Synthetic crash'):
            queue.submit(request['id'], request['token'], response)
        monkeypatch.setattr(service.journal, 'stage', original)
        assert queue.submit(request['id'], request['token'], response)['state'] == 'applied'
        assert len(service.journal.semantic_requests(two['id'])) == 1
        assert service.source_packet(two['id']).content == second.encode()
        registry = world.registry()
        assert registry['version'] == 3 and registry['facts']['app.export']['text'] == 'Exports run at 02:00.'
        assert registry['facts']['app.export']['evidence_refs']


def new_fact(text, key='app.retry'):
    return dict(fact_key=key, target=FEATURE, text=text, status='active', expected_revision=None)


def consultations(monkeypatch):
    """Every time the declared facts answer the editor; a retry must not consult again."""
    from kelmo import promotion
    calls = []
    original = promotion.DeclaredChanges.execute

    def counted(self, job):
        calls.append(job)
        return original(self, job)

    monkeypatch.setattr(promotion.DeclaredChanges, 'execute', counted)
    return calls


def commits(world):
    from kelmo.wiki import engine
    return len(engine.git(world.root, 'log', '--format=%H').splitlines())


def fail_publication(monkeypatch):
    """The A10 shape: the prepared intent cannot be written, an environment failure, not a refusal."""
    from kelmo.wiki import engine
    original = engine.publish_semantic

    def unwritable(*args, **kwargs):
        raise PermissionError(13, 'Permission denied', 'projects/app')

    monkeypatch.setattr(engine, 'publish_semantic', unwritable)
    return lambda: monkeypatch.setattr(engine, 'publish_semantic', original)


def test_queue_retry_resumes_open_editor_request(world, monkeypatch):
    from kelmo.service import Service
    calls = consultations(monkeypatch)
    with Service(world.root, bindings=world.bindings, origin=world.origins['requester']) as service:
        operation = service.capture(note(world, [new_fact('Retries resume the open request.')]),
                                    'open.md', 'lead', 'promote')
        before = commits(world)
        original = service.editor._settle
        monkeypatch.setattr(service.editor, '_settle',
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Synthetic crash before settlement.')))
        failed = service.run(operation['id'])
        assert failed['state'] == 'failed' and failed['durable_stage'] == 'archived'
        assert [row['state'] for row in service.journal.semantic_requests(operation['id'])] == ['open']
        monkeypatch.setattr(service.editor, '_settle', original)
        assert service.retry(operation['id'])['state'] == 'queued'
        assert service.run(operation['id'])['state'] == 'applied'
        assert [row['state'] for row in service.journal.semantic_requests(operation['id'])] == ['finished']
        assert len(calls) == 1, 'the recorded answer is settled, not consulted again'
        assert commits(world) == before + 1
        assert world.fact('app.retry')['text'] == 'Retries resume the open request.'
        assert world.outcome(operation['id'])['disposition'] == 'applied'


def test_queue_retry_resumes_prepared_editor_request(world, monkeypatch):
    from kelmo.service import Service
    calls = consultations(monkeypatch)
    with Service(world.root, bindings=world.bindings, origin=world.origins['requester']) as service:
        operation = service.capture(note(world, [new_fact('Retries resume the prepared intent.')]),
                                    'prepared.md', 'lead', 'promote')
        before = commits(world)
        restore = fail_publication(monkeypatch)
        failed = service.run(operation['id'])
        assert failed['state'] == 'failed' and failed['durable_stage'] == 'archived' and failed['archive_id']
        last = service.trace(operation['id'])['events'][-1]
        assert last['stage'] == 'failed' and last['details']['phase'] == 'editor'
        assert [row['state'] for row in service.journal.semantic_requests(operation['id'])] == ['prepared']
        restore()
        assert service.retry(operation['id'])['archive_id'] == failed['archive_id']
        assert service.run(operation['id'])['state'] == 'applied'
        assert [row['state'] for row in service.journal.semantic_requests(operation['id'])] == ['finished']
        assert len(calls) == 1, 'the prepared intent is published, not proposed again'
        assert commits(world) == before + 1
        assert world.page(FEATURE).count('Retries resume the prepared intent.') == 1
        assert world.outcome(operation['id'])['disposition'] == 'applied'


def test_queue_retry_replays_finished_editor_request(world, monkeypatch):
    """Another publication's recovery finished the prepared intent first; the retry replays its receipt."""
    from kelmo.service import Service
    calls = consultations(monkeypatch)
    with Service(world.root, bindings=world.bindings, origin=world.origins['requester']) as service:
        first = service.capture(note(world, [new_fact('Retries replay the finished receipt.')]),
                                'first.md', 'lead', 'promote')
        before = commits(world)
        restore = fail_publication(monkeypatch)
        assert service.run(first['id'])['state'] == 'failed'
        restore()
        other = service.capture(note(world, [world.change('app.export', 'Exports run at 05:00.')]),
                                'other.md', 'lead', 'promote')
        assert service.run(other['id'])['state'] == 'applied'
        assert [row['state'] for row in service.journal.semantic_requests(first['id'])] == ['finished']
        assert service.journal.get(first['id'])['state'] == 'failed'
        assert service.retry(first['id'])['state'] == 'queued'
        assert service.run(first['id'])['state'] == 'applied'
        assert len(service.journal.semantic_requests(first['id'])) == 1
        assert len(calls) == 2, 'one consultation per note'
        assert commits(world) == before + 2
        assert world.page(FEATURE).count('Retries replay the finished receipt.') == 1
        assert world.fact('app.export')['text'] == 'Exports run at 05:00.'
        assert world.outcome(first['id'])['disposition'] == 'applied'
