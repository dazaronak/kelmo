"""Controller-run StateAPI publication cases beside the business assertions.

They reuse test_action_state's real fixture: explicit v3 adoption, the real archive, journal,
worker lock and the single publisher. Crash cases stop one real step with a BaseException and
resume through a reopened Service; no admission, receipt or publication step is mocked.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from test_action_state import APP_PAGE, WEB_PAGE, TOPIC_PAGE, world_factory as world_factory  # noqa: F401
from test_git_ingress import git_world as git_world, packet, push_receipt  # noqa: F401


class Crash(BaseException):
    """Process death at one boundary; nothing below it runs."""


@pytest.fixture
def world(world_factory):
    return world_factory()


def content(world, path, generation):
    return world.service.knowledge_api.page(path, generation).content


def test_general_actions_show_on_their_actual_subject_page_without_git_fields(world):
    booked = world.archive('I booked the appointment.', key='booked')
    first = world.state.apply(world.update(booked, 'book-one'), world.origins['requester'], 'booked')
    reviewed = world.archive('I completed the design review.', key='reviewed')
    second = world.state.apply(world.update(reviewed, 'review-one'), world.origins['requester'], 'reviewed')

    person = content(world, 'person.md', second.generation)
    topic = content(world, TOPIC_PAGE, second.generation)
    assert b'[a:book-one]: completed' in person and b'[a:review-one]' not in person
    assert b'[a:review-one]: completed' in topic and b'[a:book-one]' not in topic
    assert b'repository:' not in person + topic and b'destination:' not in person + topic
    for path in (APP_PAGE, WEB_PAGE):
        assert b'[a:' not in content(world, path, second.generation)
    assert second.generation.publication_id == first.generation.publication_id + 1


def test_topic_action_without_a_registered_page_is_refused_at_admission(world_factory):
    from kelmo.errors import KelmoError
    from test_action_state import binding
    world = world_factory([binding('push-app'),
                           dict(action_id='review-orphan', subject_kind='topic',
                                subject_id='no-such-topic', kind='review')])
    ref = world.archive('I completed the orphan review.', key='orphan')
    before, generation = world.registry(), world.service.knowledge.generation()
    update = world.update(ref, 'review-orphan')
    for _attempt in range(2):
        with pytest.raises(KelmoError) as error:
            world.state.apply(update, world.origins['requester'], 'orphan-review')
        assert error.value.code == 'E_BINDING'
    assert 'review-orphan' not in world.registry()['actions'] and world.registry() == before
    assert world.service.knowledge.generation() == generation
    assert world.service.journal.prepared_actions() == []
    assert b'[a:review-orphan]' not in content(world, 'person.md', generation)


def test_budget_refusal_is_diagnosed_before_any_intent_and_blocks_nothing_else(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import policy
    requester = world.origins['requester']
    person = (world.service.config.wiki / 'person.md').read_text(encoding='utf-8')
    booking = world.update(world.archive('I booked the appointment.', key='booked'), 'book-one')
    with monkeypatch.context() as tight:
        # The general sentence this booking adds would take person.md over the hub line limit.
        tight.setattr(policy, 'HUB_LINE_LIMIT', len(person.splitlines()))
        with pytest.raises(KelmoError) as refused:
            world.state.apply(booking, requester, 'booked')
        assert refused.value.code == 'E_BUDGET' and 'person.md' in str(refused.value)
        assert 'not retired by hand' in str(refused.value)
        assert world.service.journal.prepared_actions() == []
        assert 'book-one' not in world.registry()['actions']
        pushed = world.state.apply(world.update(world.archive('I pushed the shared feature.', key='pushed')),
                                   requester, 'pushed')
        assert pushed.outcome == 'published'

    # Budget is a repairable condition, not a recorded refusal: the same key is admitted afterwards.
    assert world.state.apply(booking, requester, 'booked').outcome == 'published'
    assert world.action('book-one')['state'] == 'completed'


def test_caller_journal_transaction_is_refused_before_any_side_effect(world):
    from kelmo.errors import KelmoError
    ref = world.archive('I pushed the shared feature.')
    before = world.registry()
    connection = world.service.journal.conn
    connection.execute('BEGIN')
    try:
        with pytest.raises(KelmoError) as error:
            world.state.apply(world.update(ref), world.origins['requester'], 'inside-caller-transaction')
        assert error.value.code == 'E_STATE'
    finally:
        connection.rollback()
    assert world.registry() == before
    assert connection.execute('SELECT COUNT(*) FROM action_requests').fetchone()[0] == 0


def test_crash_before_publication_resumes_the_same_intent_before_new_work(world, monkeypatch):
    from kelmo.wiki import engine
    ref = world.archive('I pushed the shared feature.', key='pushed')
    other = world.archive('I booked the appointment.', key='booked')
    update = world.update(ref)
    original = engine.publish_action

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(engine, 'publish_action', die)
    with pytest.raises(Crash):
        world.state.apply(update, world.origins['requester'], 'crash-push')
    monkeypatch.setattr(engine, 'publish_action', original)
    prepared = world.service.journal.prepared_actions()
    assert len(prepared) == 1
    intent = world.service.journal.action_intent(prepared[0])
    assert 'push-app' not in world.registry()['actions']

    world.reopen()
    world.state.apply(world.update(other, 'book-one'), world.origins['requester'], 'unrelated-booking')
    assert world.action() == intent['record'], 'recovery must publish the saved intent, not a new reduction'
    receipt = world.state.apply(update, world.origins['requester'], 'crash-push')
    assert receipt.operation_id == prepared[0] and receipt.new_revision == intent['record']['revision']
    assert world.state.apply(update, world.origins['requester'], 'crash-push') == receipt
    assert world.service.journal.action_history('push-app') == (intent['record']['accepted_event'],)
    assert world.service.journal.prepared_actions() == []


def test_crash_after_commit_finishes_with_the_committed_generation(world, monkeypatch):
    from kelmo.journal import Journal
    ref = world.archive('I pushed the shared feature.')
    update = world.update(ref)
    original = Journal.action_finish

    def die(self, *args, **kwargs):
        raise Crash

    monkeypatch.setattr(Journal, 'action_finish', die)
    with pytest.raises(Crash):
        world.state.apply(update, world.origins['requester'], 'crash-after-commit')
    monkeypatch.setattr(Journal, 'action_finish', original)
    committed = world.action()
    generation = world.service.knowledge.generation()

    world.reopen()
    receipt = world.state.apply(update, world.origins['requester'], 'crash-after-commit')
    assert receipt.generation == generation and receipt.new_revision == committed['revision']
    assert world.action() == committed
    assert world.service.knowledge.generation() == generation, 'recovery published a second generation'


def test_stale_prepared_intent_is_refused_and_replays_its_refusal(world, monkeypatch):
    import json
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    ref = world.archive('I pushed the shared feature.')
    update = world.update(ref)
    original = engine.publish_action

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(engine, 'publish_action', die)
    with pytest.raises(Crash):
        world.state.apply(update, world.origins['requester'], 'stale-intent')
    monkeypatch.setattr(engine, 'publish_action', original)
    operation_id = world.service.journal.prepared_actions()[0]
    intent = world.service.journal.action_intent(operation_id)
    # Fixture for a baseline that moved while the intent waited: its pinned generation is gone.
    intent['expected_generation']['publication_id'] += 1
    with world.service.journal.conn as connection:
        connection.execute('UPDATE action_intents SET intent=? WHERE operation_id=?',
                           (json.dumps(intent, sort_keys=True), operation_id))
    before = world.registry()

    world.reopen()
    for _ in range(2):
        with pytest.raises(KelmoError) as error:
            world.state.apply(update, world.origins['requester'], 'stale-intent')
        assert error.value.code == 'E_STALE'
    assert world.registry() == before
    assert world.service.journal.prepared_actions() == []
    assert world.service.journal.action_history('push-app') == ()


def test_private_publication_paths_require_the_exact_prepared_journal_intent(world):
    from kelmo.errors import KelmoError
    from kelmo.state import action_revision
    from kelmo.wiki import engine
    ref = world.archive('Push pending.', key='pending')
    pending = world.state.apply(world.update(ref, transition='pending', event_order=1),
                                world.origins['requester'], 'pending')
    before = world.registry()
    forged = copy.deepcopy(before)
    record = forged['actions']['push-app']
    record['state'] = record['accepted_event']['transition'] = 'completed'
    record['previous_revision'] = before['actions']['push-app']['revision']
    record['revision'] = action_revision(record)
    for operation in (None, pending.operation_id, 'invented-operation'):
        with pytest.raises(KelmoError) as error:
            engine._commit_registry(world.service.root, forged, 'chore: forged completion',
                                    wiki_root=world.service.config.wiki, _action_operation=operation)
        assert error.value.code == 'E_OWNERSHIP'
    with pytest.raises(engine.OwnershipConflict):
        engine.publish_action(world.service.root, pending.operation_id)
    assert world.registry() == before


def test_concurrent_duplicate_requests_publish_once_and_share_one_receipt(world):
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    from kelmo.state import ActionState
    ref = world.archive('I completed the push.')
    update = world.update(ref)
    barrier = Barrier(2)

    def attempt(_):
        with Service(world.root, bindings=world.bindings) as service:
            state = ActionState(service, world.contexts)
            barrier.wait(timeout=10)
            try:
                return state.apply(update, world.origins['requester'], 'same-key')
            except KelmoError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))
    assert not any(isinstance(value, KelmoError) for value in outcomes)
    assert outcomes[0] == outcomes[1]
    assert world.service.journal.action_history('push-app') == (world.action()['accepted_event'],)


def test_hidden_evidence_completes_only_through_its_owning_receipt(world):
    from kelmo.errors import KelmoError
    foreign = world.archive('REQUESTER: push completed.', 'editor', key='editor-hidden')
    world.service.forget(foreign.archive_id)
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.state.apply(world.update(foreign), world.origins['requester'], 'foreign-hidden')
    assert error.value.code == 'E_RECEIPT'
    assert world.registry() == before
    owned = world.archive('I completed the enrolled push.', key='requester-hidden')
    world.service.forget(owned.archive_id)
    world.state.apply(world.update(owned), world.origins['requester'], 'owned-hidden')
    assert world.action()['accepted_event']['evidence_ref'] == owned.to_dict()


def test_historical_version_citation_needs_its_own_owning_operation(world):
    from kelmo.errors import KelmoError
    requester = world.origins['requester']
    first = world.archive('Push pending.', key='versioned')
    head = world.service.archive_api.get(first.archive_id)
    second = world.service.archive_api.update(
        first.archive_id, 1, replace(head.source, content=b'I pushed the shared feature.\n'), requester)
    assert second.version == 2 and second.archive_id == first.archive_id
    before = world.registry()
    with pytest.raises(KelmoError):
        world.state.apply(world.update(second), requester, 'unowned-version')
    assert world.registry() == before
    world.state.apply(world.update(first, transition='pending', event_order=1), requester, 'owned-version')
    assert world.action()['accepted_event']['evidence_ref'] == first.to_dict()


def test_semantic_publication_outside_managed_state_refuses_explicitly(world):
    from kelmo.contracts import KnowledgeChange, KnowledgeProposal
    from kelmo.errors import KelmoError
    ref = world.archive('I completed the push.')
    receipt = world.state.apply(world.update(ref), world.origins['requester'], 'complete')
    knowledge = world.service.knowledge_api
    page = knowledge.page(APP_PAGE, receipt.generation)
    edited = page.content + b'\nA hand-written explanation.\n'
    before = world.registry()
    for baseline, code in ((page.content_sha256, 'E_CAPABILITY'), ('f' * 64, 'E_STALE')):
        proposal = KnowledgeProposal('unmanaged-' + code, receipt.generation,
                                     (KnowledgeChange(APP_PAGE, baseline, edited, (ref,)),))
        with pytest.raises(KelmoError) as error:
            knowledge.publish(proposal, receipt.generation)
        assert error.value.code == code
    assert world.registry() == before
    assert knowledge.page(APP_PAGE, receipt.generation) == page
    assert world.service.knowledge.generation() == receipt.generation


def test_action_change_stales_a_dependent_narrative_in_the_same_transform(world):
    from kelmo.state import action_revision
    from kelmo.wiki import engine
    ref = world.archive('Push pending.', key='pending')
    world.state.apply(world.update(ref, transition='pending', event_order=1), world.origins['requester'], 'pending')
    registry = world.registry()
    action = registry['actions']['push-app']
    narrative = {'narrative_id': 'shared-explanation', 'page_id': 'shared-feature',
                 'text': 'Explains why the push waits.', 'evidence_refs': [ref.to_dict()],
                 'depends_on': {'facts': {}, 'actions': {'push-app': action['revision']}},
                 'state': 'current', 'previous_revision': None, 'compatibility': {}}
    narrative['revision'] = engine.entity_revision(narrative)
    registry['narratives']['shared-explanation'] = narrative
    assert engine.validate_registry(registry) == registry
    record = copy.deepcopy(action)
    record['state'] = record['accepted_event']['transition'] = 'completed'
    record['previous_revision'] = action['revision']
    record['revision'] = action_revision(record)

    result = engine.apply_action_intent(registry, {'action_id': 'push-app', 'record': record,
                                                   'expected_revision': action['revision']})

    stale = result['narratives']['shared-explanation']
    assert stale['state'] == 'stale' and stale['previous_revision'] == narrative['revision']
    assert stale['text'] == narrative['text'] and stale['depends_on'] == narrative['depends_on']
    assert engine.validate_registry(result) == result
    current = copy.deepcopy(result)
    current['narratives']['shared-explanation'] = narrative
    with pytest.raises(ValueError, match='cannot stay current'):
        engine.validate_registry(current)


def test_completed_publication_leaves_no_pending_locator(world):
    ref = world.archive('I pushed the shared feature.')
    world.state.apply(world.update(ref), world.origins['requester'], 'complete')
    assert not (world.service.root / '.git/kelmo-pending.json').exists()


def test_failed_producer_receipt_is_archived_and_replays_its_recorded_refusal(git_world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.git_ingress import GitIngress
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    payload['tool_response']['exit_code'] = 1
    source = packet(payload, 'failed-push')
    calls = []
    original = GitIngress._collect

    def counted(self, *args):
        calls.append(args)
        return original(self, *args)

    monkeypatch.setattr(GitIngress, '_collect', counted)
    before = world.registry()
    for _ in range(2):
        with pytest.raises(KelmoError) as error:
            world.ingress.ingest(source, world.origins['producer'], 'failed-push')
        assert error.value.code == 'E_EVIDENCE'
    assert len(calls) == 1, 'the collector must run only for a new intake'
    intake = world.service.journal.conn.execute(
        "SELECT source_operation_id FROM action_requests WHERE state='refused'").fetchone()[0]
    archived = world.service.journal.evidence_record(intake)
    assert archived is not None and archived['source_id'] == 'failed-push'
    assert world.service.trace(intake)['operation']['outcome']['disposition'] == 'unresolved'
    assert world.registry() == before


def outcome_of(world, operation_id):
    from kelmo import outcomes
    return outcomes.get(world.service.journal.conn, operation_id)


def test_settlement_and_source_outcome_commit_or_roll_back_together(git_world, monkeypatch):
    from kelmo import outcomes
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    producer = world.origins['producer']
    original = outcomes.write

    def die(connection, operation_id, disposition, reason):
        original(connection, operation_id, disposition, reason)
        if disposition in ('unresolved', 'applied'):
            raise Crash

    # Archival finishes first; death during refusal rolls back the row and its new outcome.
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    failed = copy.deepcopy(payload)
    failed['tool_response']['exit_code'] = 1
    with monkeypatch.context() as interrupted:
        interrupted.setattr(outcomes, 'write', die)
        with pytest.raises(Crash):
            world.ingress.ingest(packet(failed, 'failed'), producer, 'failed')
    assert world.service.journal.conn.execute('SELECT COUNT(*) FROM action_requests').fetchone()[0] == 0
    with pytest.raises(KelmoError):
        world.ingress.ingest(packet(failed, 'failed'), producer, 'failed')
    refused = world.service.journal.conn.execute(
        "SELECT operation_id FROM action_requests WHERE state='refused'").fetchone()[0]
    assert outcome_of(world, refused)['disposition'] == 'unresolved'

    # Publication: death between the receipt and its outcome leaves the request prepared, no history.
    source = packet(payload, 'pushed')
    with monkeypatch.context() as interrupted:
        interrupted.setattr(outcomes, 'write', die)
        with pytest.raises(Crash):
            world.ingress.ingest(source, producer, 'pushed')
    pending = world.service.journal.prepared_actions()
    assert len(pending) == 1
    assert outcome_of(world, pending[0])['disposition'] == 'archive-only'
    assert world.service.journal.action_history('push-feature') == ()
    assert outcomes.write is original
    receipt = world.ingress.ingest(source, producer, 'pushed')
    assert receipt.operation_id == pending[0] and outcome_of(world, pending[0])['disposition'] == 'applied'
    assert world.service.journal.action_history('push-feature') == (world.action('push-feature')['accepted_event'],)

    # Unchanged (the same output under a new key) and ignored (up to date) settle with their outcome too.
    again = world.ingress.ingest(packet(payload, 'again'), producer, 'again')
    assert again.outcome == 'unchanged' and outcome_of(world, again.operation_id)['disposition'] == 'archive-only'
    current = world.ingress.ingest(packet(push_receipt(fixture.repo, 'origin', 'feature/shared'), 'current'),
                                   producer, 'current')
    assert current.outcome == 'ignored' and outcome_of(world, current.operation_id)['disposition'] == 'archive-only'


def test_terminal_failed_git_intake_names_its_operation_and_the_retry_repair(git_world, monkeypatch):
    import shlex
    from kelmo.errors import KelmoError
    from kelmo.interface import build_parser
    fixture, world = git_world, git_world.world
    producer = world.origins['producer']
    source = packet(push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), 'failed-intake')
    archive = type(world.service.archive_api)

    def refused(*args, **kwargs):
        raise KelmoError('E_WRITER', 'Synthetic writer fence during intake archival.')

    with monkeypatch.context() as fenced:
        fenced.setattr(archive, 'capture', refused)
        with pytest.raises(KelmoError) as first:
            world.ingress.ingest(source, producer, 'failed-intake')
    failed = world.service.journal.conn.execute(
        "SELECT id FROM operations WHERE state='failed' AND terminal=1").fetchone()[0]
    assert first.value.code == 'E_WRITER'
    assert failed in str(first.value)
    repair = str(first.value).split('`')[1]
    args = build_parser('kelmo')[0].parse_args(shlex.split(repair)[1:])
    assert (args.command, args.action, args.operation_id) == ('queue', 'retry', failed)

    # The cause is gone, but a terminal operation is not retried by resubmitting the same key alone.
    with pytest.raises(KelmoError) as again:
        world.ingress.ingest(source, producer, 'failed-intake')
    assert again.value.code == 'E_WRITER' and failed in str(again.value)
    assert 'push-feature' not in world.registry()['actions']

    world.service.retry(failed)
    receipt = world.ingress.ingest(source, producer, 'failed-intake')
    assert receipt.outcome == 'published' and receipt.operation_id == failed
    assert world.action('push-feature')['state'] == 'completed'


def test_captured_git_maps_a_missing_spool_to_retryable_io(git_world):
    from kelmo.errors import KelmoError
    from kelmo.publication import StateCoordinator
    fixture, world = git_world, git_world.world
    producer = world.origins['producer']
    captured = world.ingress.capture(push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), producer,
                                     'spooled', source_id='spooled', observed_at='2026-10-15T03:00:00Z')
    intake = world.service.journal.action_request(
        StateCoordinator._request_key('git', producer, 'spooled'))['source_operation_id']
    spool = world.service.source_path(world.service.journal.get(intake))
    held = spool.read_bytes()
    spool.unlink()
    try:
        for attempt in (lambda: world.ingress.ingest(captured, producer, 'spooled'),
                        lambda: world.service.action_coordinator.captured_git(producer, 'spooled')):
            with pytest.raises(KelmoError) as error:
                attempt()
            assert error.value.code == 'E_IO'
        assert 'push-feature' not in world.registry()['actions']
    finally:
        spool.write_bytes(held)

    receipt = world.ingress.ingest(captured, producer, 'spooled')
    assert receipt.outcome == 'published' and world.action('push-feature')['state'] == 'completed'


CHILD = r"""
import json, os, socket, sys
from pathlib import Path
from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.memory import store, search
from kelmo.config import Config
from kelmo.context_binding import ActionBinding, ContextBindings
from kelmo.contracts import SourcePacket
from kelmo.git_ingress import GitIngress
from kelmo.server import Enrollment, OriginBindings, RepositoryBinding, WriterIdentity
from kelmo.service import Service
from kelmo.state import ActionState
from kelmo import executor

def offline(*args, **kwargs):
    raise ArchiveEmbeddingError('Process-death fixture has no embedding service')
def forbidden(*args, **kwargs):
    raise AssertionError('The process-death fixture cannot call a model or network')
store.embed = search.embed = offline
executor.provider_execute = forbidden
socket.socket.connect = socket.create_connection = forbidden
spec = json.loads(Path(sys.argv[1]).read_text())
root, repo = Path(spec['root']), Path(spec['repo'])
authority = Config.load(root).vault_id
enrollments = (
    Enrollment('local', 'fixture-node', repository_ids=('repo-app',)),
    Enrollment('requester', 'fixture-node', True, ('repo-app',)),
    Enrollment('producer', 'fixture-node', repository_ids=('repo-app',)),
    Enrollment('producer-other', 'fixture-node'),
    Enrollment('editor', 'fixture-node', repository_ids=('repo-app',)),
    Enrollment('outsider', 'fixture-node'),
)
trusted = OriginBindings(authority, WriterIdentity(authority, 'local', 1),
                         enrollments, (RepositoryBinding('repo-app', str(repo)),))
contexts = ContextBindings(
    trusted, actions=tuple(ActionBinding(**item) for item in spec['actions']),
    repository_remotes={'repo-app': spec['remote']},
    worktrees={str(repo): 'repo-app', spec['other']: 'repo-app'},
    feature_pages={'shared-feature': tuple(spec['pages'])},
    producer_client_ids=('producer', 'producer-other'),
)
source = SourcePacket.from_dict(spec['source'])
with Service(root, bindings=trusted) as service:
    def die(*args, **kwargs):
        Path(spec['marker']).write_text(json.dumps({'phase': spec['phase'], 'pid': os.getpid()}))
        os._exit(spec['expected_exit'])
    if spec['phase'] == 'before-publication':
        from kelmo.wiki import engine
        engine.publish_action = die
    else:
        from kelmo.journal import Journal
        Journal.action_finish = die
    state = ActionState(service, contexts)
    GitIngress(service, contexts, state).ingest(source, trusted.origin('producer'), spec['key'])
raise SystemExit('The selected crash boundary was not reached')
"""


@pytest.mark.parametrize('phase,code', [('before-publication', 81), ('after-commit', 82)])
def test_real_push_survives_process_death_without_second_publication(git_world, tmp_path, phase, code):
    """A real child process dies at the boundary; a fresh Service recovers the captured push once."""
    import json
    import os
    import subprocess
    import sys
    from kelmo.git_ingress import GitIngress
    fixture, world = git_world, git_world.world
    key = 'process-crash'
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    assert payload['tool_response']['exit_code'] == 0
    source = world.ingress.capture(payload, world.origins['producer'], key, source_id=key,
                                   observed_at='2026-10-15T03:00:00Z')
    intake_id = world.service.journal.conn.execute(
        'SELECT id FROM operations WHERE source_hash=?', (source.content_sha256,)).fetchone()[0]
    archived_outcome = outcome_of(world, intake_id)
    assert archived_outcome['disposition'] == 'archive-only'
    baseline = world.service.knowledge.generation()
    marker = tmp_path / 'process-death.json'
    spec = {'root': str(world.root), 'repo': str(fixture.repo.resolve()),
            'remote': str(fixture.remote.resolve()), 'other': str(fixture.other.resolve()),
            'actions': list(world.identities.values()), 'pages': [APP_PAGE, WEB_PAGE],
            'source': source.to_dict(), 'key': key, 'phase': phase,
            'expected_exit': code, 'marker': str(marker)}
    path = tmp_path / 'crash-spec.json'
    path.write_text(json.dumps(spec))
    world.service.close()
    crashed = subprocess.run([sys.executable, '-c', CHILD, str(path)], env=os.environ.copy(),
                             text=True, capture_output=True, timeout=40)
    assert crashed.returncode == code, (crashed.returncode, crashed.stdout, crashed.stderr)
    mark = json.loads(marker.read_text())
    assert mark['phase'] == phase and mark['pid'] != os.getpid()

    world.reopen()
    world.ingress = GitIngress(world.service, world.contexts, world.state)
    pending = world.service.journal.prepared_actions()
    assert len(pending) == 1
    intent = world.service.journal.action_intent(pending[0])
    before_recovery = world.service.knowledge.generation()
    records = world.service.status()['archive_records']
    if phase == 'before-publication':
        assert before_recovery == baseline
        assert 'push-feature' not in world.registry()['actions']
    else:
        assert before_recovery.publication_id == baseline.publication_id + 1
        assert world.action('push-feature') == intent['record']
    assert pending[0] == intake_id
    assert outcome_of(world, pending[0]) == archived_outcome

    receipt = world.ingress.ingest(source, world.origins['producer'], key)

    assert receipt.operation_id == pending[0]
    assert receipt.new_revision == intent['record']['revision']
    assert receipt.generation.publication_id == baseline.publication_id + 1
    if phase == 'after-commit':
        assert receipt.generation == before_recovery
    assert world.action('push-feature') == intent['record']
    assert world.service.journal.action_history('push-feature') == (intent['record']['accepted_event'],)
    assert world.service.journal.prepared_actions() == []
    assert outcome_of(world, pending[0])['disposition'] == 'applied'
    assert world.service.status()['archive_records'] == records
    page = world.service.knowledge.page(APP_PAGE, receipt.generation).content
    assert fixture.revision.encode() in page and b'completed' in page
    assert 'deploy-feature' not in world.registry()['actions']
    assert world.ingress.ingest(source, world.origins['producer'], key) == receipt
    assert world.service.knowledge.generation() == receipt.generation




