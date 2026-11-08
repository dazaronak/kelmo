"""Boundary cases for wiki and action-state updates.

Git cases use real local repositories, a disposable bare remote and Git's actual command output.
Adoption cases archive real sources in an isolated vault before asserting exactness; invented
references appear only as the inputs that must be refused. Nothing here mocks Git, the archive,
the journal or the publisher.
"""

from dataclasses import replace

import pytest

from test_action_state import APP_PAGE, PAGES, binding, world_factory as world_factory  # noqa: F401
from test_git_ingress import git, git_world as git_world, packet, push_receipt  # noqa: F401


def ingest(world, payload, key, source_id=None):
    return world.ingress.ingest(packet(payload, source_id or key), world.origins['producer'], key)


def refused_intake(world):
    return world.service.journal.conn.execute(
        "SELECT source_operation_id FROM action_requests WHERE state='refused'").fetchone()[0]


@pytest.fixture
def two_revisions(tmp_path, world_factory):
    """feature/shared exists remotely at base; R1 and R2 are both enrolled, R2 not yet pushed."""
    remote, repo = tmp_path / 'remote.git', tmp_path / 'repo'
    git(tmp_path, 'init', '--bare', remote)
    git(tmp_path, 'init', '-b', 'main', repo)
    git(repo, 'config', 'user.name', 'Synthetic Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'remote', 'add', 'origin', remote)
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', 'base.txt')
    git(repo, 'commit', '-m', 'chore: base')
    git(repo, 'push', '-u', 'origin', 'main')
    git(repo, 'push', 'origin', 'main:refs/heads/feature/shared')
    git(repo, 'switch', '-c', 'feature/shared')
    revisions = []
    for name in ('first', 'second'):
        (repo / (name + '.txt')).write_text(name + '\n')
        git(repo, 'add', name + '.txt')
        git(repo, 'commit', '-m', 'feat: add the ' + name + ' revision')
        revisions.append(git(repo, 'rev-parse', 'HEAD').stdout.strip())
    git(repo, 'reset', '--hard', revisions[0])
    world = world_factory([binding('push-r1', work_revision=revisions[0]),
                           binding('push-r2', work_revision=revisions[1])],
                          repositories={'repo-app': repo}, remotes={'repo-app': str(remote.resolve())},
                          worktrees={str(repo.resolve()): 'repo-app'})
    return world, repo, remote, revisions


def test_old_success_receipt_cannot_complete_a_later_revision(two_revisions):
    world, repo, remote, (r1, r2) = two_revisions
    original = push_receipt(repo, '-u', 'origin', 'feature/shared')
    first = ingest(world, original, 'r1-push')
    accepted = world.action('push-r1')
    assert accepted['state'] == 'completed' and accepted['work_revision'] == r1

    # R2 reaches the remote with no receipt; the remote-tracking ref and its reflog now say R1 -> R2.
    git(repo, 'reset', '--hard', r2)
    git(repo, 'push', 'origin', 'feature/shared')
    assert git(remote, 'rev-parse', 'refs/heads/feature/shared').stdout.strip() == r2

    # The same archived output, delivered again by a retried hook under a new operation key.
    replay = ingest(world, original, 'retried-hook-event')

    assert replay.outcome == 'unchanged' and replay.new_revision == first.new_revision
    assert world.action('push-r1') == accepted
    assert 'push-r2' not in world.registry()['actions']


def test_first_push_of_a_new_branch_names_no_revision_and_stays_unresolved(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    git(fixture.repo, 'push', 'origin', '--delete', 'feature/shared')
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    assert payload['tool_response']['exit_code'] == 0
    assert '[new branch]' in payload['tool_response']['stderr']
    before = world.registry()

    with pytest.raises(KelmoError) as error:
        ingest(world, payload, 'new-branch')

    assert error.value.code == 'E_EVIDENCE'
    assert world.registry() == before
    intake = refused_intake(world)
    assert world.service.journal.evidence_record(intake)['source_id'] == 'new-branch'
    assert world.service.trace(intake)['operation']['outcome']['disposition'] == 'unresolved'


def test_merge_commit_output_names_no_merged_revision(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    git(fixture.repo, 'switch', 'main')
    (fixture.repo / 'main.txt').write_text('diverged\n')
    git(fixture.repo, 'add', 'main.txt')
    git(fixture.repo, 'commit', '-m', 'chore: diverge main')
    result = git(fixture.repo, 'merge', '--no-ff', '-m', 'merge shared feature', 'feature/shared')
    assert 'Fast-forward' not in result.stdout
    payload = dict(cwd=str(fixture.repo),
                   tool_input={'command': 'git merge --no-ff -m "merge shared feature" feature/shared'},
                   tool_response={'exit_code': result.returncode, 'stdout': result.stdout,
                                  'stderr': result.stderr})
    before = world.registry()

    with pytest.raises(KelmoError) as error:
        ingest(world, payload, 'merge-commit')

    assert error.value.code == 'E_EVIDENCE'
    assert world.registry() == before


def test_retimed_copy_of_old_producer_output_cannot_complete_a_retracted_action(git_world):
    from kelmo.errors import KelmoError
    from kelmo.state import source_order
    fixture, world = git_world, git_world.world
    source = packet(push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), 'original-push')
    first = world.ingress.ingest(source, world.origins['producer'], 'original-push')
    accepted = world.action('push-feature')
    correction = world.archive('Correction: this push completion is withdrawn.', key='withdraw')
    command_source = world.service.archive_api.get(correction.archive_id, correction.version).source
    world.state.apply(world.update(correction, 'push-feature', expected_revision=first.new_revision,
                                   event_order=source_order(command_source), transition='retract',
                                   correction_of=accepted['accepted_event']['event_id']),
                      world.origins['requester'], 'withdraw')
    withdrawn = world.action('push-feature')
    assert withdrawn['state'] == 'unresolved'
    retimed = replace(source, source_id='retimed-copy', filename='retimed-copy.md',
                      observed_at='2030-01-01T00:00:00Z')

    with pytest.raises(KelmoError) as error:
        world.ingress.ingest(retimed, world.origins['producer'], 'retimed-copy')

    assert error.value.code == 'E_STALE'
    assert world.action('push-feature') == withdrawn


@pytest.mark.parametrize('route', ['reserialized', 'captured-envelope', 'hook-envelope', 'blank-output-line'])
def test_same_tool_receipt_in_another_packet_cannot_recomplete_a_retracted_action(git_world, route):
    import json
    from kelmo.errors import KelmoError
    from kelmo.state import source_order
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    source = packet(payload, 'original-push')
    first = world.ingress.ingest(source, world.origins['producer'], 'original-push')
    accepted = world.action('push-feature')
    correction = world.archive('Correction: this push completion is withdrawn.', key='withdraw')
    command_source = world.service.archive_api.get(correction.archive_id, correction.version).source
    world.state.apply(world.update(correction, 'push-feature', expected_revision=first.new_revision,
                                   event_order=source_order(command_source), transition='retract',
                                   correction_of=accepted['accepted_event']['event_id']),
                      world.origins['requester'], 'withdraw')
    withdrawn = world.action('push-feature')
    if route != 'captured-envelope':
        if route == 'hook-envelope':
            # Delivery identifiers the producer parser never reads.
            payload = dict(payload, session_id='another-session', tool_use_id='another-delivery')
        elif route == 'blank-output-line':
            payload = dict(payload, tool_response=dict(payload['tool_response'],
                                                       stderr=payload['tool_response']['stderr'] + '\n'))
        copied = replace(source, content=json.dumps(payload, sort_keys=True).encode(), source_id='retimed-copy',
                         filename='retimed-copy.md', observed_at='2030-01-01T00:00:00Z')
    else:
        copied = world.ingress.capture(payload, world.origins['producer'], 'retimed-copy',
                                       source_id='retimed-copy', observed_at='2030-01-01T00:00:00Z')
    assert copied.content_sha256 != source.content_sha256

    with pytest.raises(KelmoError) as error:
        world.ingress.ingest(copied, world.origins['producer'], 'retimed-copy')

    assert error.value.code == 'E_STALE'
    assert world.action('push-feature') == withdrawn
    # The original bytes stay archived under the original owning receipt.
    ref = first.evidence_refs[0]
    assert world.service.archive_api.get(ref.archive_id, ref.version).source.content == source.content


def test_absent_tracking_reflog_cannot_attest_an_old_first_push(two_revisions):
    from kelmo.errors import KelmoError
    world, repo, _, (_r1, r2) = two_revisions
    git(repo, 'config', 'core.logAllRefUpdates', 'false')
    git(repo, 'push', 'origin', '--delete', 'feature/shared')
    payload = push_receipt(repo, '-u', 'origin', 'feature/shared')
    assert '[new branch]' in payload['tool_response']['stderr']
    move_to(repo, r2)
    assert git(repo, 'reflog', 'exists', 'refs/remotes/origin/feature/shared', check=False).returncode != 0

    with pytest.raises(KelmoError) as error:
        world.ingress.capture(payload, world.origins['producer'], 'late-first', source_id='late-first',
                              observed_at='2030-01-01T00:00:00Z')

    assert error.value.code == 'E_EVIDENCE'
    assert 'push-r2' not in world.registry()['actions']


def test_refused_snapshot_does_not_preempt_a_lawful_capture_under_its_key(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    origin = world.origins['producer']
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    genuine = capture(world, payload, 'genuine')
    # Exactly the packet a capture under 'lawful' will archive, submitted before that capture runs.
    early = replace(genuine, source_id='lawful', filename='lawful.md')

    with pytest.raises(KelmoError) as error:
        world.ingress.ingest(early, origin, 'lawful')

    assert error.value.code == 'E_EVIDENCE'
    assert world.service.action_coordinator.captured_git(origin, 'lawful') == (None, None)
    lawful = capture(world, payload, 'lawful')
    assert lawful == early
    receipt = world.ingress.ingest(lawful, origin, 'lawful')
    assert receipt.outcome == 'published' and world.action('push-feature')['state'] == 'completed'





def test_dirty_managed_page_is_a_typed_conflict_and_recovers_once_resolved(world):
    from kelmo.errors import KelmoError
    page = world.service.config.wiki / APP_PAGE
    committed = page.read_bytes()
    edited = committed + b'\nHand note written while a publication was pending.\n'
    page.write_bytes(edited)
    push = world.update(world.archive('I pushed the shared feature.', key='pushed'))
    booking = world.update(world.archive('I booked the appointment.', key='booked'), 'book-one')

    for update, key in ((push, 'push'), (booking, 'booked')):
        with pytest.raises(KelmoError) as error:
            world.state.apply(update, world.origins['requester'], key)
        # Detected before reduction: an environment condition, never a recorded refusal.
        assert error.value.code == 'E_CONFLICT' and isinstance(error.value, RuntimeError)
        assert 'knowledge/wiki/' + APP_PAGE in error.value.paths
    assert world.service.journal.prepared_actions() == []
    assert world.service.journal.conn.execute(
        "SELECT COUNT(*) FROM action_requests WHERE state='refused'").fetchone()[0] == 0
    assert world.registry()['actions'] == {}
    assert page.read_bytes() == edited, 'the hand edit was discarded'

    page.write_bytes(committed)  # the person resolves the conflict; nothing here auto-commits it
    pushed = world.state.apply(push, world.origins['requester'], 'push')
    booked = world.state.apply(booking, world.origins['requester'], 'booked')

    assert world.action('push-app')['state'] == 'completed'
    assert world.action('book-one')['state'] == 'completed'
    assert world.state.apply(push, world.origins['requester'], 'push') == pushed
    assert booked.generation.publication_id == pushed.generation.publication_id + 1


@pytest.mark.parametrize('edit', ['intent-record', 'other-record'])
def test_dirty_registry_during_prepared_recovery_is_named_with_its_paths(world, monkeypatch, edit):
    import json
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    requester = world.origins['requester']
    push = world.update(world.archive('I pushed the shared feature.', key='pushed'))

    def die(*args, **kwargs):
        raise Crash

    with monkeypatch.context() as interrupted:
        interrupted.setattr(engine, 'publish_action', die)
        with pytest.raises(Crash):
            world.state.apply(push, requester, 'push')
    intent = world.service.journal.action_intent(world.service.journal.prepared_actions()[0])
    path = world.service.config.registry
    committed = path.read_bytes()
    working = json.loads(committed)
    if edit == 'intent-record':
        # Formerly the publisher's working-file shortcut skipped the commit (E_PUBLICATION).
        working['actions']['push-app'] = intent['record']
    else:
        # Formerly the publisher's working-file reduction refused it as E_OWNERSHIP.
        working['actions']['push-app'] = dict(intent['record'], revision='0' * 64)
    path.write_text(engine.encoded(working))
    booking = world.update(world.archive('I booked the appointment.', key='booked'), 'book-one')
    try:
        with pytest.raises(KelmoError) as error:
            world.state.apply(booking, requester, 'booked')
        assert isinstance(error.value, engine.DirtyKnowledge) and error.value.code == 'E_CONFLICT'
        assert any(item.endswith('registry.json') for item in error.value.paths)
        assert len(world.service.journal.prepared_actions()) == 1
    finally:
        path.write_bytes(committed)

    world.state.apply(booking, requester, 'booked')
    assert world.action('push-app') == intent['record'] and world.action('book-one')['state'] == 'completed'


def test_committed_publication_settles_despite_later_working_registry_edits(world, monkeypatch):
    from kelmo.journal import Journal
    requester = world.origins['requester']
    push = world.update(world.archive('I pushed the shared feature.', key='pushed'))
    original = Journal.action_finish

    def die(self, *args, **kwargs):
        raise Crash

    monkeypatch.setattr(Journal, 'action_finish', die)
    with pytest.raises(Crash):
        world.state.apply(push, requester, 'push')
    monkeypatch.setattr(Journal, 'action_finish', original)
    committed_record, generation = world.action(), world.service.knowledge.generation()
    path = world.service.config.registry
    committed = path.read_bytes()
    path.write_bytes(committed.replace(b'"actions": {', b'"actions": {"local-edit": {}, ', 1))
    try:
        receipt = world.state.apply(push, requester, 'push')
    finally:
        path.write_bytes(committed)

    assert receipt.outcome == 'published' and receipt.generation == generation
    assert receipt.new_revision == committed_record['revision']
    assert world.service.journal.prepared_actions() == []
    assert world.service.journal.action_history('push-app') == (committed_record['accepted_event'],)


def test_dirty_registry_does_not_permanently_refuse_a_valid_correction(world):
    from kelmo.errors import KelmoError
    path = world.service.config.registry
    old_bytes = path.read_bytes()
    first = world.state.apply(world.update(world.archive('The push is complete.', key='first')),
                              world.origins['requester'], 'first')
    accepted = world.action()
    command = world.update(world.archive('Withdraw that completion.', key='correction'),
                           expected_revision=first.new_revision, event_order=20, transition='retract',
                           correction_of=accepted['accepted_event']['event_id'])
    committed_bytes = path.read_bytes()
    try:
        path.write_bytes(old_bytes)  # an uncommitted working registry without the accepted action
        with pytest.raises(KelmoError) as error:
            world.state.apply(command, world.origins['requester'], 'correction')
        assert error.value.code == 'E_CONFLICT' and 'registry.json' in ' '.join(error.value.paths)
    finally:
        path.write_bytes(committed_bytes)

    result = world.state.apply(command, world.origins['requester'], 'correction')

    assert result.outcome == 'published' and world.action()['state'] == 'unresolved'


@pytest.fixture
def world(world_factory):
    return world_factory()


def capture(world, payload, key, observed_at='2026-10-14T10:00:00Z'):
    """The trusted producer path at event occurrence: archive the receipt with its immutable event."""
    return world.ingress.capture(payload, world.origins['producer'], key, source_id=key,
                                 observed_at=observed_at)


def captured_event(packet_):
    import json
    envelope = json.loads(packet_.content)
    return envelope, json.loads(envelope['observation']['event_json'])


def test_captured_first_push_names_its_revision_after_refs_move(git_world):
    fixture, world = git_world, git_world.world
    git(fixture.repo, 'push', 'origin', '--delete', 'feature/shared')
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    assert '[new branch]' in payload['tool_response']['stderr']

    captured = capture(world, payload, 'first-push')

    assert 'push-feature' not in world.registry()['actions'], 'capture alone must not publish'
    envelope, event = captured_event(captured)
    assert envelope['tool_receipt'] == payload
    assert (event['old_sha'], event['new_sha']) == (fixture.base, fixture.revision)
    assert (event['source_branch'], event['target_branch']) == ('feature/shared', 'feature/shared')
    # The branch moves on before the deferred admission runs.
    (fixture.repo / 'later.txt').write_text('later\n')
    git(fixture.repo, 'add', 'later.txt')
    git(fixture.repo, 'commit', '-m', 'feat: later work')
    git(fixture.repo, 'push', 'origin', 'feature/shared')
    later = git(fixture.repo, 'rev-parse', 'HEAD').stdout.strip()
    assert git(fixture.remote, 'rev-parse', 'refs/heads/feature/shared').stdout.strip() == later

    receipt = world.ingress.ingest(captured, world.origins['producer'], 'first-push')

    action = world.action('push-feature')
    assert receipt.outcome == 'published' and action['state'] == 'completed'
    assert action['work_revision'] == fixture.revision != later
    assert action['accepted_event']['evidence_ref'] == receipt.evidence_refs[0].to_dict()
    assert world.ingress.ingest(captured, world.origins['producer'], 'first-push') == receipt


def test_captured_merge_commit_completes_only_the_local_merge(git_world):
    fixture, world = git_world, git_world.world
    ingest(world, push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), 'push')
    push = world.action('push-feature')
    git(fixture.repo, 'switch', 'main')
    (fixture.repo / 'main.txt').write_text('diverged\n')
    git(fixture.repo, 'add', 'main.txt')
    git(fixture.repo, 'commit', '-m', 'chore: diverge main')
    result = git(fixture.repo, 'merge', '--no-ff', '-m', 'merge shared feature', 'feature/shared')
    payload = dict(cwd=str(fixture.repo),
                   tool_input={'command': 'git merge --no-ff -m "merge shared feature" feature/shared'},
                   tool_response={'exit_code': result.returncode, 'stdout': result.stdout,
                                  'stderr': result.stderr})

    captured = capture(world, payload, 'merge-commit')
    receipt = world.ingress.ingest(captured, world.origins['producer'], 'merge-commit')

    _, event = captured_event(captured)
    assert event['merged_sha'] == fixture.revision and event['new_sha'] != fixture.revision
    merge = world.action('merge-feature')
    assert receipt.outcome == 'published' and merge['state'] == 'completed'
    assert merge['work_revision'] == fixture.revision and merge['remote_ref'] == 'refs/heads/main'
    assert world.action('push-feature') == push
    assert 'deploy-feature' not in world.registry()['actions']


def test_captured_failed_and_rollback_pushes_complete_nothing(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    failed = push_receipt(fixture.repo, str(fixture.repo.parent / 'missing.git'), 'feature/shared', check=False)
    before = world.registry()
    for _ in range(2):  # the retry replays the recorded refusal; it does not observe again
        with pytest.raises(KelmoError) as error:
            capture(world, failed, 'failed-push')
        assert error.value.code == 'E_EVIDENCE'
    assert world.registry() == before
    ingest(world, push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), 'push')
    accepted = world.action('push-feature')
    rollback = push_receipt(fixture.repo, '--force-with-lease', 'origin',
                            fixture.base + ':refs/heads/feature/shared')
    captured = capture(world, rollback, 'rollback')
    assert world.ingress.ingest(captured, world.origins['producer'], 'rollback').outcome == 'ignored'
    assert world.action('push-feature') == accepted


class Crash(BaseException):
    """Process death at one boundary; nothing below it runs."""


OBSERVED = '2026-10-14T10:00:00Z'


def move_to(repo, revision):
    git(repo, 'reset', '--hard', revision)
    git(repo, 'push', 'origin', 'feature/shared')


@pytest.mark.parametrize('admitted', [False, True], ids=['before-admission', 'after-admission'])
def test_capture_retry_after_refs_move_returns_the_original_packet(two_revisions, admitted):
    world, repo, remote, (r1, r2) = two_revisions
    origin = world.origins['producer']
    payload = push_receipt(repo, '-u', 'origin', 'feature/shared')
    first = world.ingress.capture(payload, origin, 'capture-r1', source_id='capture-r1', observed_at=OBSERVED)
    if admitted:
        world.ingress.ingest(first, origin, 'capture-r1')
    move_to(repo, r2)
    assert git(remote, 'rev-parse', 'refs/heads/feature/shared').stdout.strip() == r2

    repeated = world.ingress.capture(payload, origin, 'capture-r1', source_id='capture-r1', observed_at=OBSERVED)

    assert repeated == first
    assert captured_event(repeated)[1]['new_sha'] == r1
    receipt = world.ingress.ingest(repeated, origin, 'capture-r1')
    assert receipt.outcome == 'published'
    assert world.action('push-r1')['state'] == 'completed'
    assert 'push-r2' not in world.registry()['actions']


@pytest.mark.parametrize('change', ['payload', 'source_id', 'observed_at', 'origin'])
def test_capture_key_reuse_with_different_input_refuses(two_revisions, change):
    import copy
    from kelmo.errors import KelmoError
    world, repo, _, (r1, _r2) = two_revisions
    origin = world.origins['producer']
    payload = push_receipt(repo, '-u', 'origin', 'feature/shared')
    first = world.ingress.capture(payload, origin, 'reused', source_id='reused', observed_at=OBSERVED)
    other = dict(payload=payload, origin=origin, source_id='reused', observed_at=OBSERVED)
    if change == 'payload':
        other['payload'] = copy.deepcopy(payload)
        other['payload']['tool_response']['stderr'] += '\nedited receipt'
    elif change == 'source_id':
        other['source_id'] = 'renamed'
    elif change == 'observed_at':
        other['observed_at'] = '2026-10-14T11:00:00Z'
    else:
        # Keys are origin-scoped: another producer is a new request, refused by its own enrollment.
        other['origin'] = world.origins['producer-other']

    with pytest.raises(KelmoError) as error:
        world.ingress.capture(other['payload'], other['origin'], 'reused', source_id=other['source_id'],
                              observed_at=other['observed_at'])

    assert error.value.code == ('E_BINDING' if change == 'origin' else 'E_IDEMPOTENCY')
    again = world.ingress.capture(payload, origin, 'reused', source_id='reused', observed_at=OBSERVED)
    assert again == first and captured_event(again)[1]['new_sha'] == r1


@pytest.mark.parametrize('resume', ['capture', 'ingest'])
def test_crash_after_archived_capture_before_its_row_resumes_from_that_packet(two_revisions, monkeypatch,
                                                                            resume):
    from kelmo.publication import StateCoordinator
    world, repo, _, (r1, r2) = two_revisions
    origin = world.origins['producer']
    payload = push_receipt(repo, '-u', 'origin', 'feature/shared')
    original = StateCoordinator._observe

    def die(*args, **kwargs):
        raise Crash

    # The request row is written in _observe, after the packet is durably archived.
    monkeypatch.setattr(StateCoordinator, '_observe', die)
    with pytest.raises(Crash):
        world.ingress.capture(payload, origin, 'crashed', source_id='crashed', observed_at=OBSERVED)
    monkeypatch.setattr(StateCoordinator, '_observe', original)
    archived, row = world.service.action_coordinator.captured_git(origin, 'crashed')
    assert row is None and archived is not None
    assert captured_event(archived)[1]['new_sha'] == r1
    move_to(repo, r2)

    if resume == 'capture':
        packet_ = world.ingress.capture(payload, origin, 'crashed', source_id='crashed', observed_at=OBSERVED)
        assert packet_ == archived, 'a lawful retry was recaptured from moved refs'
    else:
        packet_ = archived
    receipt = world.ingress.ingest(packet_, origin, 'crashed')

    assert receipt.outcome == 'published'
    assert world.action('push-r1')['state'] == 'completed' and world.action('push-r1')['work_revision'] == r1
    assert 'push-r2' not in world.registry()['actions']
    assert world.service.action_coordinator.captured_git(origin, 'crashed')[1] is not None


def test_submitted_capture_snapshot_cannot_self_promote(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    captured = capture(world, push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'), 'genuine')
    forged = replace(captured, source_id='forged-snapshot', filename='forged-snapshot.md')
    before = world.registry()

    with pytest.raises(KelmoError) as error:
        world.ingress.ingest(forged, world.origins['producer'], 'forged-snapshot')

    assert error.value.code == 'E_EVIDENCE'
    assert world.registry() == before


def test_retimed_capture_of_the_same_output_cannot_reopen_a_retracted_action(git_world):
    from kelmo.errors import KelmoError
    from kelmo.state import source_order
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    captured = capture(world, payload, 'captured-push')
    first = world.ingress.ingest(captured, world.origins['producer'], 'captured-push')
    accepted = world.action('push-feature')
    correction = world.archive('Correction: this push completion is withdrawn.', key='withdraw')
    command_source = world.service.archive_api.get(correction.archive_id, correction.version).source
    world.state.apply(world.update(correction, 'push-feature', expected_revision=first.new_revision,
                                   event_order=source_order(command_source), transition='retract',
                                   correction_of=accepted['accepted_event']['event_id']),
                      world.origins['requester'], 'withdraw')
    withdrawn = world.action('push-feature')
    again = capture(world, payload, 'captured-again', observed_at='2030-01-01T00:00:00Z')
    assert again.content == captured.content

    for _ in range(2):
        with pytest.raises(KelmoError) as error:
            world.ingress.ingest(again, world.origins['producer'], 'captured-again')
        assert error.value.code == 'E_STALE'
    assert world.action('push-feature') == withdrawn


def test_reducer_refusal_replays_after_an_unrelated_transition(world):
    from kelmo.errors import KelmoError
    requester = world.origins['requester']
    first = world.state.apply(world.update(world.archive('I pushed the shared feature.', key='done')),
                              requester, 'done')
    accepted = world.action()
    late = world.update(world.archive('Push is still pending.', key='older'),
                        expected_revision=first.new_revision, transition='pending', event_order=5)
    with pytest.raises(KelmoError) as refused:
        world.state.apply(late, requester, 'late')
    assert refused.value.code == 'E_STALE'
    correction = world.archive('Correction: that completion is withdrawn.', key='withdraw')
    world.state.apply(world.update(correction, expected_revision=first.new_revision, event_order=11,
                                   transition='retract', correction_of=accepted['accepted_event']['event_id']),
                      requester, 'withdraw')
    withdrawn = world.action()

    with pytest.raises(KelmoError) as replayed:
        world.state.apply(late, requester, 'late')

    assert replayed.value.code == 'E_STALE', 'the recorded refusal must replay, not be re-reduced'
    assert world.action() == withdrawn


def test_context_bundle_cannot_claim_completeness_with_omissions():
    from kelmo.contracts import ContextBundle, Generation
    generation = Generation('00000000-0000-4000-8000-00000000000a', 1, 'a' * 64)
    partial = ContextBundle(generation, (), (), ('omitted.md',), 'current', (), False)
    assert ContextBundle.from_dict(partial.to_dict()) == partial
    with pytest.raises(ValueError, match='omitted'):
        ContextBundle(generation, (), (), ('omitted.md',), 'current', (), True)
    claimed = dict(partial.to_dict(), complete=True)
    with pytest.raises(ValueError, match='omitted'):
        ContextBundle.from_dict(claimed)


def test_old_first_push_receipt_under_a_new_key_cannot_complete_a_later_revision(two_revisions):
    from kelmo.errors import KelmoError
    world, repo, _, (r1, r2) = two_revisions
    origin = world.origins['producer']
    git(repo, 'push', 'origin', '--delete', 'feature/shared')
    payload = push_receipt(repo, '-u', 'origin', 'feature/shared')
    assert '[new branch]' in payload['tool_response']['stderr']
    first = world.ingress.capture(payload, origin, 'original-first-push', source_id='original-first-push',
                                  observed_at=OBSERVED)
    move_to(repo, r2)

    with pytest.raises(KelmoError) as error:
        world.ingress.capture(payload, origin, 'different-key', source_id='different-key', observed_at=OBSERVED)

    assert error.value.code == 'E_EVIDENCE'
    assert 'push-r2' not in world.registry()['actions']
    # The original capture still names its own revision and completes only that action.
    assert captured_event(first)[1]['new_sha'] == r1
    world.ingress.ingest(first, origin, 'original-first-push')
    assert world.action('push-r1')['state'] == 'completed' and 'push-r2' not in world.registry()['actions']


def test_quiet_push_output_cannot_attest_a_capture(two_revisions):
    from kelmo.errors import KelmoError
    world, repo, _, _ = two_revisions
    payload = push_receipt(repo, '-q', 'origin', 'feature/shared')
    assert payload['tool_response']['exit_code'] == 0 and not payload['tool_response']['stderr'].strip()

    with pytest.raises(KelmoError) as error:
        capture(world, payload, 'quiet-push')

    assert error.value.code == 'E_EVIDENCE'
    assert world.registry()['actions'] == {}


@pytest.mark.parametrize('route', ['nested-repository', 'symlink'])
def test_command_in_another_git_worktree_cannot_borrow_enrolled_refs(two_revisions, tmp_path, route):
    from kelmo.errors import KelmoError
    world, repo, _, _ = two_revisions
    receipt = push_receipt(repo, '-u', 'origin', 'feature/shared')
    other = tmp_path / 'unenrolled-repository'
    git(tmp_path, 'init', '-b', 'main', other)
    if route == 'nested-repository':
        directory = repo / 'vendor' / 'inner'
        git(tmp_path, 'init', '-b', 'main', directory)
    else:
        directory = repo / 'escape'
        directory.symlink_to(other, target_is_directory=True)
    payload = dict(receipt, cwd=str(directory),
                   tool_input={'command': 'git push -u origin feature/shared'})

    with pytest.raises(KelmoError) as error:
        capture(world, payload, 'borrowed-' + route)

    assert error.value.code == 'E_BINDING'
    assert world.registry()['actions'] == {}


def test_generation_outage_leaves_a_captured_key_retryable(two_revisions, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.publication import StateCoordinator
    world, repo, _, _ = two_revisions
    origin = world.origins['producer']
    captured = capture(world, push_receipt(repo, '-u', 'origin', 'feature/shared'), 'repairable-dependency')

    def unavailable(self):
        raise KelmoError('E_GENERATION', 'Synthetic temporary committed-generation failure')

    with monkeypatch.context() as temporary:
        temporary.setattr(StateCoordinator, '_generation', unavailable)
        with pytest.raises(KelmoError) as failed:
            world.ingress.ingest(captured, origin, 'repairable-dependency')
        assert failed.value.code == 'E_GENERATION'

    receipt = world.ingress.ingest(captured, origin, 'repairable-dependency')
    assert receipt.outcome == 'published' and world.action('push-r1')['state'] == 'completed'


def test_corrupted_then_repaired_committed_generation_admits_the_same_key(two_revisions):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    world, repo, _, _ = two_revisions
    origin = world.origins['producer']
    captured = capture(world, push_receipt(repo, '-u', 'origin', 'feature/shared'), 'repairable-generation')
    manifest = Config.load(world.root).registry.with_name('generation.json')
    original = manifest.read_bytes()
    manifest.write_text('{}\n')
    git(world.root, 'add', '--', manifest)
    git(world.root, 'commit', '--no-verify', '-m', 'test: corrupt the committed generation')

    with pytest.raises(KelmoError) as failed:
        world.ingress.ingest(captured, origin, 'repairable-generation')
    assert failed.value.code == 'E_GENERATION'
    assert 'push-r1' not in world.registry()['actions']

    manifest.write_bytes(original)
    git(world.root, 'add', '--', manifest)
    git(world.root, 'commit', '--no-verify', '-m', 'test: repair the committed generation')
    receipt = world.ingress.ingest(captured, origin, 'repairable-generation')

    assert receipt.outcome == 'published' and world.action('push-r1')['state'] == 'completed'


def test_public_capture_cannot_use_the_internal_intake_namespaces(two_revisions):
    from kelmo.errors import KelmoError
    from kelmo.publication import CAPTURE_NAMESPACE, StateCoordinator
    world, repo, _, _ = two_revisions
    origin = world.origins['producer']
    genuine = capture(world, push_receipt(repo, '-u', 'origin', 'feature/shared'), 'legitimate')
    forged = replace(genuine, source_id='fabricated', filename='fabricated.md')
    request_key = StateCoordinator._request_key('git', origin, 'fabricated')
    for key in (CAPTURE_NAMESPACE + request_key, 'state-intake/' + request_key):
        with pytest.raises(KelmoError) as refused:
            world.service.capture_source(forged, origin, key)
        assert refused.value.code == 'E_INPUT'
        with pytest.raises(KelmoError) as refused:
            world.service.capture('Planted text.', 'planted.md', 'implementer', idempotency_key=key)
        assert refused.value.code == 'E_INPUT'
    before = world.registry()

    with pytest.raises(KelmoError) as error:
        world.ingress.ingest(forged, origin, 'fabricated')

    assert error.value.code == 'E_EVIDENCE'
    assert world.registry() == before


def test_finished_apply_replays_while_another_publication_is_stuck(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    requester = world.origins['requester']
    first = world.state.apply(world.update(world.archive('I pushed the shared feature.', key='done')),
                              requester, 'done')
    page = world.service.config.wiki / APP_PAGE
    committed = page.read_bytes()
    booking = world.update(world.archive('I booked the appointment.', key='booked'), 'book-one')

    def die(*args, **kwargs):
        raise Crash

    # The booking intent is prepared, then the process dies before publishing it.
    with monkeypatch.context() as interrupted:
        interrupted.setattr(engine, 'publish_action', die)
        with pytest.raises(Crash):
            world.state.apply(booking, requester, 'booked')
    page.write_bytes(committed + b'\nHand note while a publication is pending.\n')
    with pytest.raises(KelmoError) as stuck:
        world.state.apply(booking, requester, 'booked')
    assert stuck.value.code == 'E_CONFLICT' and len(world.service.journal.prepared_actions()) == 1

    assert world.state.apply(world.update(world.archive('I pushed the shared feature.', key='done')),
                             requester, 'done') == first
    review = world.update(world.archive('I completed the design review.', key='review'), 'review-one')
    with pytest.raises(KelmoError) as closed:
        world.state.apply(review, requester, 'review')
    assert closed.value.code == 'E_CONFLICT', 'new work still fails closed behind the stuck intent'

    page.write_bytes(committed)
    world.state.apply(review, requester, 'review')
    assert world.action('book-one')['state'] == 'completed'


def test_pending_apply_keeps_its_recorded_owner(world, monkeypatch):
    from kelmo.publication import StateCoordinator
    from kelmo.service import Service
    requester = world.origins['requester']
    ref = world.archive('I pushed the shared feature.', key='owned')
    update = world.update(ref)
    original = StateCoordinator._admit

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(StateCoordinator, '_admit', die)
    with pytest.raises(Crash):
        world.state.apply(update, requester, 'pending-owner')
    monkeypatch.setattr(StateCoordinator, '_admit', original)
    row = world.service.journal.action_request(StateCoordinator._request_key('apply', requester, 'pending-owner'))
    assert row['state'] == 'open'
    recorded = row['source_operation_id']
    # A later capture of the same bytes under the same filename owns the same exact receipt.
    with Service(world.root, bindings=world.bindings, origin=requester) as duplicate:
        operation = duplicate.capture('I pushed the shared feature.', 'owned.md', 'implementer',
                                      idempotency_key='owned-duplicate')
        assert duplicate.run(operation['id'])['durable_stage'] == 'archived'
    assert operation['id'] != recorded

    receipt = world.state.apply(update, requester, 'pending-owner')

    assert receipt.outcome == 'published'
    assert world.service.journal.action_intent(receipt.operation_id)['source_operation_id'] == recorded
