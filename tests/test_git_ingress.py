"""Controller-only fixtures; no product imports or Git operations on collection.

Producer input retains cwd/tool_input/tool_response from the existing collector.
The original receipt bytes travel as SourcePacket, rather than a new producer
wire protocol. GitIngress.ingest(packet, origin, idempotency_key) returns the
existing OperationReceipt. Parser/collector helpers retain their proven names.
These cases stop at accepted publication; native next-session delivery is separate.
"""

import copy
import hashlib
import json
import shlex
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_action_state import binding, world_factory as world_factory


def git(repository, *args, check=True):
    return subprocess.run(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
         '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never',
         '-c', 'protocol.file.allow=always', '-C', str(repository), *map(str, args)],
        capture_output=True, text=True, check=check, timeout=20,
    )


def packet(payload, source_id='push-receipt'):
    from kelmo.contracts import SourcePacket
    return SourcePacket((json.dumps(payload, indent=2) + '\n').encode(), 'git_event', source_id,
                        source_id + '.md', '2026-10-14T10:00:00Z')


def push_receipt(repository, *args, check=True):
    result = git(repository, 'push', *args, check=check)
    return dict(cwd=str(repository),
                tool_input={'command': shlex.join(['git', '-C', str(repository), 'push', *args])},
                tool_response={'exit_code': result.returncode,
                               'stdout': result.stdout, 'stderr': result.stderr})


@pytest.fixture
def git_world(tmp_path, world_factory):
    remote, repo, other = (tmp_path / name for name in ('remote.git', 'repo', 'other-worktree'))
    git(tmp_path, 'init', '--bare', remote)
    git(tmp_path, 'init', '-b', 'main', repo)
    git(repo, 'config', 'user.name', 'Synthetic Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'remote', 'add', 'origin', remote)
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', 'base.txt')
    git(repo, 'commit', '-m', 'chore: base')
    base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'push', '-u', 'origin', 'main')
    # The remote branch exists before the receipt push, so Git's own output names old..new.
    # A first push of a new branch names no revision; test_state_review_boundaries covers it.
    git(repo, 'push', 'origin', 'main:refs/heads/feature/shared')
    git(repo, 'switch', '-c', 'feature/shared')
    (repo / 'feature.txt').write_text('new feature\n')
    git(repo, 'add', 'feature.txt')
    git(repo, 'commit', '-m', 'feat: add shared feature')
    revision = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'worktree', 'add', '-b', 'feature/observer', other, revision)
    identities = [binding('push-feature', work_revision=revision),
                  binding('merge-feature', 'merge', work_revision=revision,
                          remote_ref='refs/heads/main'), binding('deploy-feature', 'deploy')]
    world = world_factory(identities, repositories={'repo-app': repo},
                          remotes={'repo-app': str(remote.resolve())},
                          worktrees={str(repo.resolve()): 'repo-app',
                                     str(other.resolve()): 'repo-app'})
    return SimpleNamespace(world=world, repo=repo, remote=remote, other=other,
                           revision=revision, base=base)


def ingest(fixture, payload, source_id='push-receipt', principal='producer'):
    world = fixture.world
    return world.ingress.ingest(packet(payload, source_id), world.origins[principal], source_id)


@pytest.mark.parametrize('status', [{'exit_code': 0}, {'exitCode': 0}, {'exit_code': '0'}])
def test_successful_push_keeps_exact_receipt_and_enrolled_action(git_world, status):
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    response = payload['tool_response']
    response.pop('exit_code')
    response.update(status)
    source = packet(payload)
    receipt = world.ingress.ingest(source, world.origins['producer'], 'push-receipt')
    action = world.action('push-feature')
    assert action['state'] == 'completed' and action['kind'] == 'push'
    assert action['feature_id'] == 'shared-feature' and action['repository_id'] == 'repo-app'
    assert action['work_revision'] == fixture.revision
    assert action['remote_ref'] == 'refs/heads/feature/shared'
    assert action['accepted_event']['evidence_mark'] == 'PRODUCER'
    assert receipt.generation is not None and len(receipt.evidence_refs) == 1
    ref = receipt.evidence_refs[0]
    assert action['accepted_event']['evidence_ref'] == ref.to_dict()
    assert ref.source_id == source.source_id
    assert ref.content_sha256 == hashlib.sha256(source.content).hexdigest()
    archived = world.service.archive_api.get(ref.archive_id, ref.version)
    assert archived.evidence == ref and archived.source.content == source.content
    assert world.service.journal.evidence_record(receipt.operation_id) == ref.to_dict()
    assert world.service.journal.origin_record(receipt.operation_id) == world.origins['producer'].to_dict()
    assert world.service.archive_api.verify_receipt(
        ref, receipt.operation_id, world.origins['producer'],
    ) == archived
    assert world.ingress.ingest(source, world.origins['producer'], 'push-receipt') == receipt
    assert world.action('push-feature') == action


@pytest.mark.parametrize('response', [None, 'pushed successfully', {}, {'exit_code': None},
                                     {'exitCode': None}, {'exit_code': 1}, {'exit_code': '1'},
                                     {'exit_code': False}, {'exit_code': True},
                                     {'exit_code': 0, 'exitCode': 1}])
def test_failed_or_missing_status_cannot_borrow_existing_remote_success(git_world, response):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    payload['tool_response'] = response
    before = world.registry()
    with pytest.raises(KelmoError):
        ingest(fixture, payload)
    assert world.registry() == before


def test_real_failed_push_does_not_complete_action(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, str(fixture.repo.parent / 'missing.git'),
                           'feature/shared', check=False)
    assert payload['tool_response']['exit_code'] != 0
    before = world.registry()
    with pytest.raises(KelmoError):
        ingest(fixture, payload)
    assert world.registry() == before


def test_real_noop_push_does_not_create_a_completion(git_world):
    fixture, world = git_world, git_world.world
    push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    no_op = push_receipt(fixture.repo, 'origin', 'feature/shared')
    before = world.registry()
    receipt = ingest(fixture, no_op)
    assert receipt.outcome == 'ignored'
    assert world.registry() == before


@pytest.mark.parametrize('principal', ['editor', 'producer-other'])
def test_success_payload_requires_enrolled_producer_and_repository_origin(git_world, principal):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    payload.update(requester=True, evidence_mark='REQUESTER', client_id='producer')
    before = world.registry()
    with pytest.raises(KelmoError):
        ingest(fixture, payload, principal=principal)
    assert world.registry() == before


def test_changed_remote_is_not_the_enrolled_repository_even_at_same_path(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    foreign = fixture.repo.parent / 'foreign.git'
    git(fixture.repo.parent, 'init', '--bare', foreign)
    git(fixture.repo, 'remote', 'set-url', 'origin', foreign)
    before = world.registry()
    with pytest.raises(KelmoError):
        ingest(fixture, payload)
    assert world.registry() == before


def test_unenrolled_worktree_payload_cannot_claim_an_enrolled_repository(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    foreign = fixture.repo.parent / 'not-enrolled'
    git(fixture.repo, 'worktree', 'add', '-b', 'feature/unbound', foreign, fixture.revision)
    payload['cwd'] = str(foreign)
    payload['tool_input']['command'] = shlex.join(['git', '-C', str(foreign), 'push',
                                                 'origin', 'HEAD:refs/heads/feature/shared'])
    payload['repository_id'] = 'repo-app'
    before = world.registry()
    with pytest.raises(KelmoError):
        ingest(fixture, payload)
    assert world.registry() == before


def test_real_merge_completes_only_merge_and_keeps_deployment_pending(git_world):
    fixture, world = git_world, git_world.world
    pending = world.archive('Deployment is pending.', key='deploy-pending')
    world.state.apply(world.update(pending, 'deploy-feature', transition='pending', event_order=1),
                      world.origins['requester'], 'deploy-pending')
    deployment = world.action('deploy-feature')
    pushed = ingest(fixture, push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'))
    push = world.action('push-feature')
    git(fixture.repo, 'switch', 'main')
    result = git(fixture.repo, 'merge', '--ff-only', 'feature/shared')
    payload = dict(cwd=str(fixture.repo),
                   tool_input={'command': 'git merge --ff-only feature/shared'},
                   tool_response={'exit_code': result.returncode, 'stdout': result.stdout,
                                  'stderr': result.stderr})
    merged = ingest(fixture, payload, 'merge-receipt')
    assert merged.operation_id != pushed.operation_id
    assert world.action('merge-feature')['state'] == 'completed'
    assert world.action('merge-feature')['kind'] == 'merge'
    assert world.action('push-feature') == push
    assert world.action('deploy-feature') == deployment


def test_unenrolled_work_revision_is_unresolved_instead_of_guessed(git_world):
    fixture, world = git_world, git_world.world
    (fixture.repo / 'later.txt').write_text('new work\n')
    git(fixture.repo, 'add', 'later.txt')
    git(fixture.repo, 'commit', '-m', 'fix: next revision needs separate action')
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    before = world.registry()
    receipt = ingest(fixture, payload)
    assert receipt.outcome == 'unresolved'
    assert receipt.evidence_refs and receipt.generation is None
    assert world.registry() == before
    assert world.service.trace(receipt.operation_id)['operation']['outcome']['disposition'] == 'unresolved'


def test_second_worktree_receipt_maps_to_same_action(git_world):
    fixture, world = git_world, git_world.world
    first = ingest(fixture, push_receipt(fixture.repo, '-u', 'origin', 'feature/shared'))
    accepted = world.action('push-feature')
    # Move the disposable remote back, then really republish the same revision
    # from another branch/worktree. Neither a path nor branch creates new work.
    rollback = push_receipt(fixture.repo, '--force-with-lease', 'origin',
                             fixture.base + ':refs/heads/feature/shared')
    ignored = ingest(fixture, rollback, 'rollback-receipt')
    assert ignored.outcome == 'ignored' and world.action('push-feature') == accepted
    second_payload = push_receipt(fixture.other, 'origin', 'HEAD:refs/heads/feature/shared')
    second = ingest(fixture, second_payload, 'other-worktree-receipt')
    assert second.new_revision == first.new_revision and second.generation == first.generation
    assert world.action('push-feature') == accepted
    assert set(world.registry()['actions']) == {'push-feature'}


def test_changed_receipt_under_same_idempotency_key_is_rejected(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    source = packet(payload)
    world.ingress.ingest(source, world.origins['producer'], 'push-key')
    before = world.registry()
    changed = copy.deepcopy(payload)
    changed['tool_response']['stderr'] += '\nchanged receipt'
    with pytest.raises(KelmoError):
        world.ingress.ingest(packet(changed), world.origins['producer'], 'push-key')
    assert world.registry() == before


def test_source_metadata_cannot_rebind_a_push_to_another_feature(git_world):
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    payload.update(feature_id='payload-feature', repository_id='payload-repository',
                   action_id='payload-action', state='completed', evidence_mark='REQUESTER')
    source = replace(packet(payload), project='payload-project', tags=('REQUESTER',))
    world.ingress.ingest(source, world.origins['producer'], 'metadata-push')
    assert set(world.registry()['actions']) == {'push-feature'}
    action = world.action('push-feature')
    assert action['feature_id'] == 'shared-feature' and action['repository_id'] == 'repo-app'
    assert action['accepted_event']['evidence_mark'] == 'PRODUCER'


def test_old_git_completion_cannot_resurrect_a_retracted_action(git_world):
    from kelmo.errors import KelmoError
    from kelmo.state import source_order
    fixture, world = git_world, git_world.world
    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    source = replace(packet(payload), observed_at='2000-01-01T00:00:00Z')
    first = world.ingress.ingest(source, world.origins['producer'], 'original-push')
    accepted = world.action('push-feature')
    correction = world.archive('Correction: this push completion is withdrawn.', key='withdraw')
    command_source = world.service.archive_api.get(correction.archive_id, correction.version).source
    update = world.update(correction, 'push-feature', expected_revision=first.new_revision,
                          event_order=source_order(command_source), transition='retract',
                          correction_of=accepted['accepted_event']['event_id'])
    world.state.apply(update, world.origins['requester'], 'withdraw')
    withdrawn = world.action('push-feature')
    replay = replace(source, source_id='late-copy', filename='late-copy.md')
    with pytest.raises(KelmoError):
        world.ingress.ingest(replay, world.origins['producer'], 'late-copy')
    assert world.action('push-feature') == withdrawn
    assert withdrawn['state'] != 'completed'


@pytest.mark.parametrize('command', ['git push', 'command git push', '/usr/bin/git push',
                                    "git -C '/synthetic/repo' push -o ci.skip origin HEAD:feature/shared",
                                    "cd '/synthetic/repo' && git push"])
def test_existing_invocation_forms_are_preserved(command):
    from kelmo.git_ingress import parse_git_invocation
    invocation = parse_git_invocation(command, '/synthetic/repo')
    assert invocation.operation == 'push' and invocation.repository == '/synthetic/repo'


@pytest.mark.parametrize('command', ['git push --dry-run', 'git push origin --delete feature/shared',
                                    'git push origin :feature/shared', 'git merge --abort',
                                    "rg 'git push' docs", "echo 'git merge feature/shared'",
                                    'git status | git push', 'git push && false'])
def test_command_text_or_non_effectful_invocation_is_not_completion(command):
    from kelmo.git_ingress import parse_git_invocation
    assert parse_git_invocation(command, '/synthetic/repo') is None
