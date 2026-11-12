"""Controller-run behavior cases over the real archive, journal and publisher.

The constructor seam these cases use is ActionState(service, contexts),
GitIngress(service, contexts, state_api), and controller-only ContextBindings
over the existing OriginBindings. ActionBinding contains ActionUpdate's identity
fields only. It is enrollment, never another accepted-state store. Public apply,
publication and evidence records keep adoption/1 signatures and field names.
No fixture runs at import time; no missing capability is skipped.
"""

import copy
import importlib.util
from contextlib import ExitStack
from dataclasses import replace

import pytest
import vault_template


APP_PAGE = 'projects/app/shared-feature.md'
WEB_PAGE = 'projects/web/shared-feature.md'
TOPIC_PAGE = 'projects/app/design-review.md'
WORK_REVISION = 'a' * 40
# Registered v3 pages: the feature box, the web map linking it, the person context for
# general task actions and the topic subject's own page.
PAGES = {APP_PAGE: dict(page_id='shared-feature', layout='feature', title='Shared feature',
                        summary='One feature shared by the app and web repositories.'),
         WEB_PAGE: dict(page_id='web-shared-feature', layout='topic', title='Shared feature in web',
                        links=['shared-feature']),
         'person.md': dict(page_id='person', layout='person', title='Person'),
         TOPIC_PAGE: dict(page_id='design-review', layout='topic', title='Design review')}


def binding(action_id, kind='push', **fields):
    """Independent controller enrollment, without any evidence or accepted state."""
    value = dict(action_id=action_id, subject_kind='feature', subject_id='shared-feature',
                 kind=kind, feature_id='shared-feature')
    if kind in ('push', 'merge'):
        value.update(repository_id='repo-app', work_revision=WORK_REVISION,
                     remote_ref='refs/heads/feature/shared')
    if kind == 'deploy':
        value.update(environment='sandbox', artifact_id='artifact-one')
    value.update(fields)
    return value


class World:
    """Fixture conveniences call real product boundaries, never simulate admission."""

    def archive(self, text, principal='requester', key='source', role='implementer'):
        from kelmo.contracts import EvidenceRef
        from kelmo.service import Service
        with Service(self.root, bindings=self.bindings,
                     origin=self.origins[principal]) as source_service:
            op = source_service.capture(text, key + '.md', role, idempotency_key=key)
            result = source_service.run(op['id'])
            assert result['durable_stage'] == 'archived'
            return EvidenceRef.from_dict(source_service.journal.evidence_record(op['id']))

    def update(self, ref, action_id='push-app', **fields):
        from kelmo.contracts import ActionUpdate
        identity = dict(self.identities[action_id])
        identity.update(expected_revision=None, event_order=10, transition='completed',
                        evidence_ref=ref)
        identity.update(fields)
        return ActionUpdate(**identity)

    def registry(self):
        from kelmo.wiki import engine
        return engine.load_registry(self.root)

    def action(self, action_id='push-app'):
        return self.registry()['actions'][action_id]

    def reopen(self):
        from kelmo.service import Service
        from kelmo.state import ActionState
        self.service.close()
        self.service = self.stack.enter_context(Service(self.root, bindings=self.bindings))
        self.state = ActionState(self.service, self.contexts)


@pytest.fixture
def world_factory(tmp_path, monkeypatch):
    """Only the controller executes native initialization and storage fixtures."""
    for name in ('state', 'context_binding', 'git_ingress'):
        assert importlib.util.find_spec('kelmo.' + name) is not None, (
            'Required explicit action-state capability is absent: kelmo.' + name
        )
    from kelmo.store.errors import ArchiveEmbeddingError
    import registry_seed
    from kelmo.config import Config
    from kelmo.context_binding import ActionBinding, ContextBindings
    from kelmo.git_ingress import GitIngress
    from kelmo.server import Enrollment, OriginBindings, RepositoryBinding, WriterIdentity
    from kelmo.service import Service
    from kelmo.state import ActionState

    def unavailable(*args, **kwargs):
        raise ArchiveEmbeddingError('Synthetic offline embedding provider')

    def forbidden_executor(*args, **kwargs):
        raise AssertionError('Explicit action admission must not invoke a semantic executor')

    monkeypatch.setattr('kelmo.store.memory.store.embed', unavailable)
    monkeypatch.setattr('kelmo.store.memory.search.embed', unavailable)
    monkeypatch.setattr('kelmo.executor.provider_execute', forbidden_executor)
    for key, value in {'GIT_AUTHOR_NAME': 'Synthetic Fixture',
                       'GIT_COMMITTER_NAME': 'Synthetic Fixture',
                       'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                       'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}.items():
        monkeypatch.setenv(key, value)

    def adopted(path):
        Service.initialize(path, 'fixture-node')
        registry_seed.seed(path, pages_settings=PAGES)

    with ExitStack() as stack:
        def make(identities=None, repositories=None, remotes=None, worktrees=None):
            root = tmp_path / 'vault'
            # The fixture registers the pages that show accepted actions in one seeded publication.
            vault_template.copy(root, 'action-state-vault', adopted)
            authority = Config.load(root).vault_id
            repositories = repositories or {'repo-app': tmp_path / 'app',
                                            'repo-web': tmp_path / 'web'}
            repo_ids = tuple(repositories)
            enrollments = (Enrollment('local', 'fixture-node', repository_ids=repo_ids),
                           Enrollment('requester', 'fixture-node', True, repo_ids),
                           Enrollment('producer', 'fixture-node', repository_ids=repo_ids),
                           Enrollment('producer-other', 'fixture-node'),
                           Enrollment('editor', 'fixture-node', repository_ids=repo_ids),
                           Enrollment('outsider', 'fixture-node'))
            trusted = OriginBindings(authority, WriterIdentity(authority, 'local', 1),
                                     enrollments, tuple(RepositoryBinding(k, str(v.resolve()))
                                                        for k, v in repositories.items()))
            identities = identities or [binding('push-app'), binding('merge-app', 'merge'),
                                        binding('deploy-app', 'deploy'),
                                        binding('push-web', repository_id='repo-web'),
                                        dict(action_id='book-one', subject_kind='task',
                                             subject_id='appointment', kind='book'),
                                        dict(action_id='book-two', subject_kind='task',
                                             subject_id='appointment', kind='book'),
                                        dict(action_id='review-one', subject_kind='topic',
                                             subject_id='design-review', kind='review')]
            contexts = ContextBindings(
                trusted, actions=tuple(ActionBinding(**item) for item in identities),
                repository_remotes=remotes or {k: 'https://example.invalid/' + k + '.git'
                                               for k in repositories},
                worktrees=worktrees or {str(v.resolve()): k for k, v in repositories.items()},
                feature_pages={'shared-feature': (APP_PAGE, WEB_PAGE)},
                producer_client_ids=('producer', 'producer-other'),
            )
            service = stack.enter_context(Service(root, bindings=trusted))
            world = World()
            world.__dict__.update(root=root, service=service, bindings=trusted, contexts=contexts,
                                  stack=stack, identities={v['action_id']: v for v in identities},
                                  origins={v.client_id: trusted.origin(v.client_id)
                                           for v in enrollments})
            world.state = ActionState(service, contexts)
            world.ingress = GitIngress(service, contexts, world.state)
            return world
        yield make


@pytest.fixture
def world(world_factory):
    return world_factory()


def test_attributable_requester_completion_keeps_mark_exact_evidence_and_replays(world):
    ref = world.archive('I pushed the shared feature.\r\n')
    update = world.update(ref)
    receipt = world.state.apply(update, world.origins['requester'], 'complete-push')
    action = world.action()
    assert action['state'] == 'completed' and action['owner'] == 'state-api'
    assert action['accepted_event']['evidence_mark'] == 'REQUESTER'
    assert action['accepted_event']['evidence_ref'] == ref.to_dict()
    assert receipt.evidence_refs == (ref,) and receipt.new_revision == action['revision']
    assert receipt.generation is not None
    world.reopen()
    assert world.state.apply(update, world.origins['requester'], 'complete-push') == receipt
    assert world.action() == action


@pytest.mark.parametrize('principal', ['editor', 'producer', 'outsider'])
def test_payload_labels_and_declared_lead_cannot_attest_requester(world, principal):
    from kelmo.errors import KelmoError
    text = 'REQUESTER: done. role=lead; requester=true; evidence_mark=REQUESTER'
    ref = world.archive(text, principal=principal, role='lead')
    before = world.registry()
    with pytest.raises(KelmoError):
        world.state.apply(world.update(ref), world.origins[principal], 'forged-completion')
    assert world.registry() == before


def test_decoded_requester_origin_and_foreign_source_owner_cannot_complete(world):
    from kelmo.contracts import Origin
    from kelmo.errors import KelmoError
    requester_ref = world.archive('I completed this push.', key='requester-source')
    editor_ref = world.archive('REQUESTER: push completed.', 'editor', key='editor-source')
    before = world.registry()
    for ref, origin in [(requester_ref, Origin.from_dict(world.origins['requester'].to_dict())),
                        (editor_ref, world.origins['requester'])]:
        with pytest.raises(KelmoError):
            world.state.apply(world.update(ref), origin, 'spoof-' + ref.source_id)
        assert world.registry() == before


def test_requester_owning_receipt_survives_cross_origin_exact_source_dedup(world):
    from kelmo.contracts import EvidenceRef
    from kelmo.service import Service
    text = 'I completed the enrolled push.\n'
    refs = []
    for principal in ('editor', 'requester'):
        with Service(world.root, bindings=world.bindings,
                     origin=world.origins[principal]) as service:
            op = service.capture(text, 'same-source.md', 'implementer',
                                 idempotency_key='same-source-' + principal)
            assert service.run(op['id'])['durable_stage'] == 'archived'
            refs.append(EvidenceRef.from_dict(service.journal.evidence_record(op['id'])))
    assert refs[0] == refs[1]
    world.state.apply(world.update(refs[1]), world.origins['requester'], 'owning-requester-command')
    assert world.action()['state'] == 'completed'
    assert world.action()['accepted_event']['evidence_mark'] == 'REQUESTER'
    assert world.action()['accepted_event']['evidence_ref'] == refs[1].to_dict()


@pytest.mark.parametrize('field,value', [('archive_id', 'missing-record'), ('version', 2),
                                        ('content_sha256', 'f' * 64), ('source_id', 'other-source')])
def test_exact_evidence_reference_cannot_float_or_be_rebound(world, field, value):
    from kelmo.errors import KelmoError
    ref = world.archive('I completed the enrolled push.')
    before = world.registry()
    with pytest.raises(KelmoError):
        world.state.apply(world.update(replace(ref, **{field: value})),
                          world.origins['requester'], 'wrong-reference')
    assert world.registry() == before


@pytest.mark.parametrize('kind,action_id', [('book', 'book-one'), ('review', 'review-one')])
def test_ordinary_action_completion_has_no_invented_git_or_deployment_fields(world, kind, action_id):
    ref = world.archive('I completed the ' + kind + '.', key=kind)
    world.state.apply(world.update(ref, action_id), world.origins['requester'], 'complete-' + kind)
    action = world.action(action_id)
    assert action['state'] == 'completed' and action['kind'] == kind
    assert action['accepted_event']['evidence_mark'] == 'REQUESTER'
    assert not ({'repository_id', 'work_revision', 'remote_ref', 'environment', 'artifact_id'}
                & action.keys())


def test_merge_and_deploy_remain_separate(world):
    pending = world.archive('Merge and deployment are still pending.', key='pending')
    for action_id in ('merge-app', 'deploy-app'):
        world.state.apply(world.update(pending, action_id, transition='pending', event_order=1),
                          world.origins['requester'], 'pending-' + action_id)
    before = copy.deepcopy(world.registry()['actions'])
    pushed = world.archive('I pushed the feature.', key='pushed')
    world.state.apply(world.update(pushed), world.origins['requester'], 'push-only')
    assert world.action()['state'] == 'completed'
    for action_id in ('merge-app', 'deploy-app'):
        assert world.action(action_id) == before[action_id]


@pytest.mark.parametrize('event_order', [1, 50])
def test_rebased_older_pending_does_not_reopen_push(world, event_order):
    from kelmo.errors import KelmoError
    old = world.archive('Push is pending.', key='older-pending')
    pending = world.update(old, transition='pending', event_order=1)
    initial = world.state.apply(pending, world.origins['requester'], 'pending')
    done = world.archive('I pushed it.', key='completion')
    world.state.apply(world.update(done, expected_revision=initial.new_revision, event_order=2),
                      world.origins['requester'], 'completed')
    accepted = world.action()
    assert world.state.apply(pending, world.origins['requester'], 'pending') == initial
    assert world.action() == accepted
    for revision in (initial.new_revision, accepted['revision']):
        rebased = replace(pending, expected_revision=revision, event_order=event_order)
        with pytest.raises(KelmoError):
            world.state.apply(rebased, world.origins['requester'], 'late-' + str(event_order) + revision)
        assert world.action() == accepted


@pytest.mark.parametrize('transition', ['correct', 'retract'])
def test_correction_can_retract_named_completion_without_erasing_receipt(world, transition):
    from kelmo.errors import KelmoError
    ref = world.archive('I completed this push.', key='done')
    original = world.state.apply(world.update(ref), world.origins['requester'], 'done')
    accepted = world.action()
    correction = world.archive('Correction: that completion is withdrawn.', key='correction')
    update = world.update(correction, expected_revision=accepted['revision'], event_order=11,
                          transition=transition, correction_of='unrelated-event')
    with pytest.raises(KelmoError):
        world.state.apply(update, world.origins['requester'], 'wrong-target')
    assert world.action() == accepted
    update = replace(update, correction_of=accepted['accepted_event']['event_id'])
    receipt = world.state.apply(update, world.origins['requester'], 'named-' + transition)
    corrected = world.action()
    assert corrected['state'] != 'completed'
    assert corrected['accepted_event']['correction_of'] == accepted['accepted_event']['event_id']
    assert corrected['accepted_event']['evidence_ref'] == correction.to_dict()
    assert receipt.old_revision == original.new_revision
    assert world.state.apply(world.update(ref), world.origins['requester'], 'done') == original
    assert world.action() == corrected


def test_duplicate_key_rejects_changed_input_and_stale_competitor_has_no_effect(world):
    from kelmo.errors import KelmoError
    ref = world.archive('I completed the push.')
    update = world.update(ref)
    receipt = world.state.apply(update, world.origins['requester'], 'same-key')
    accepted = world.action()
    assert world.state.apply(update, world.origins['requester'], 'same-key') == receipt
    for candidate, key in [(replace(update, transition='pending'), 'same-key'),
                           (replace(update, transition='in-progress'), 'concurrent-key')]:
        with pytest.raises(KelmoError):
            world.state.apply(candidate, world.origins['requester'], key)
        assert world.action() == accepted


def test_new_work_cannot_reuse_closed_action_identity(world):
    from kelmo.errors import KelmoError
    done = world.archive('I pushed the first revision.', key='first')
    receipt = world.state.apply(world.update(done), world.origins['requester'], 'first')
    before = world.action()
    later = world.archive('The next revision needs a push.', key='next-work')
    with pytest.raises(KelmoError):
        world.state.apply(world.update(later, expected_revision=receipt.new_revision,
                                       work_revision='b' * 40, transition='pending', event_order=20),
                          world.origins['requester'], 'reuse-id')
    assert world.action() == before


def test_new_ordinary_work_uses_new_action_id_for_the_same_subject(world):
    done = world.archive('I booked the appointment.', key='first-booking')
    world.state.apply(world.update(done, 'book-one'), world.origins['requester'], 'first-booking')
    original = world.action('book-one')
    next_work = world.archive('Another appointment needs booking.', key='next-booking')
    world.state.apply(world.update(next_work, 'book-two', transition='pending', event_order=11),
                      world.origins['requester'], 'next-booking')
    assert world.action('book-one') == original
    assert world.action('book-two')['state'] == 'pending'
    assert world.action('book-two')['subject_id'] == original['subject_id']


@pytest.mark.parametrize('fields', [dict(action_id='unenrolled-action'), dict(kind='merge'),
                                    dict(subject_id='other-feature', feature_id='other-feature'),
                                    dict(repository_id='repo-web'), dict(work_revision='b' * 40),
                                    dict(remote_ref='refs/heads/unrelated')])
def test_action_cannot_escape_controller_enrolled_identity(world, fields):
    from kelmo.errors import KelmoError
    ref = world.archive('I completed the enrolled push.')
    update = replace(world.update(ref), **fields)
    before = world.registry()
    with pytest.raises(KelmoError):
        world.state.apply(update, world.origins['requester'], 'wrong-binding')
    assert world.registry() == before


@pytest.mark.parametrize('path', [APP_PAGE, WEB_PAGE])
def test_semantic_proposal_with_fresh_revision_cannot_bypass_state_owner(world, path):
    from kelmo.contracts import KnowledgeChange, KnowledgeProposal
    from kelmo.errors import KelmoError
    ref = world.archive('I completed the push.')
    receipt = world.state.apply(world.update(ref), world.origins['requester'], 'complete')
    knowledge = world.service.knowledge_api
    page = knowledge.page(path, receipt.generation)
    assert b'push-app' in page.content and b'completed' in page.content
    forged = page.content.replace(b'completed', b'pending')
    change = KnowledgeChange(path, page.content_sha256, forged, (ref,))
    proposal = KnowledgeProposal('semantic-reopen', receipt.generation, (change,))
    before = world.registry()
    with pytest.raises(KelmoError):
        knowledge.publish(proposal, receipt.generation)
    assert world.registry() == before
    assert knowledge.page(path, receipt.generation) == page


def test_both_repo_maps_share_one_feature_state(world):
    ref = world.archive('I pushed the app half of the shared feature.')
    receipt = world.state.apply(world.update(ref), world.origins['requester'], 'shared-push')
    pages = [world.service.knowledge_api.page(path, receipt.generation)
             for path in (APP_PAGE, WEB_PAGE)]
    assert all(page.generation == receipt.generation for page in pages)
    assert all(b'push-app' in page.content and b'completed' in page.content for page in pages)
    assert set(world.registry()['actions']) == {'push-app'}


def test_generic_registry_publication_cannot_restore_a_valid_old_pending_record(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    old = world.archive('Push pending.', key='old')
    pending = world.state.apply(world.update(old, transition='pending', event_order=1),
                                world.origins['requester'], 'old-pending')
    old_record = copy.deepcopy(world.action())
    ref = world.archive('I pushed it.', key='new')
    world.state.apply(world.update(ref, expected_revision=pending.new_revision, event_order=2),
                      world.origins['requester'], 'new-completion')
    before = world.registry()
    forged = copy.deepcopy(before)
    forged['actions']['push-app'] = old_record
    with pytest.raises(KelmoError):
        engine._commit_registry(world.root, forged, 'chore: semantic refresh', world.service.config.wiki)
    assert world.registry() == before


def test_two_controllers_racing_from_same_revision_accept_one_effect(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    from kelmo.state import ActionState
    pending_ref = world.archive('Push pending.', key='pending')
    pending = world.state.apply(world.update(pending_ref, transition='pending', event_order=1),
                                world.origins['requester'], 'pending')
    ref = world.archive('I completed the push.', key='done')
    update = world.update(ref, expected_revision=pending.new_revision)
    barrier = Barrier(2)

    def attempt(index):
        with Service(world.root, bindings=world.bindings) as service:
            state = ActionState(service, world.contexts)
            barrier.wait(timeout=10)
            try:
                return state.apply(update, world.origins['requester'], 'race-' + str(index))
            except KelmoError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))
    accepted = [value for value in outcomes if not isinstance(value, KelmoError)]
    assert len(accepted) == 1
    action = world.action()
    assert action['state'] == 'completed'
    assert action['previous_revision'] == pending.new_revision
    assert action['revision'] == accepted[0].new_revision


def fictional_push_record():
    """Literal fictional record demo.action.preview-push from the reference records.json."""
    return {
        'accepted_event': {
            'correction_of': None,
            'event_id': 'demo.event.preview-push',
            'event_order': 3,
            'evidence_mark': 'PRODUCER',
            'evidence_ref': {
                'archive_id': 'demo.archive.events',
                'content_sha256': 'bcb29bb84fd13f09088a2183da3dc70838663043fb9f374c65bfe5fe2fd91917',
                'source_id': 'demo-source-events',
                'version': 1,
            },
            'ingested_at': '2030-01-02T10:01:00Z',
            'occurred_at': '2030-01-02T10:00:00Z',
            'operation_receipt_id': 'demo.operation.push',
            'transition': 'completed',
        },
        'action_id': 'demo.action.preview-push',
        'compatibility': {},
        'feature_id': 'demo.feature.update-preview',
        'kind': 'push',
        'owner': 'state-api',
        'previous_revision': None,
        'remote_ref': 'origin/preview',
        'repository_id': 'demo.repo.harbor-web',
        'revision': 'eaced99f551d8bf682d192c634a0276ea6f147b5453f372b9dc7d12a362b7376',
        'state': 'completed',
        'subject_id': 'demo.feature.update-preview',
        'subject_kind': 'feature',
        'work_revision': '1111111111111111111111111111111111111111',
    }


def test_action_revision_matches_the_literal_fictional_record_digest():
    from kelmo.state import action_revision
    record = fictional_push_record()
    assert action_revision(record) == record['revision'] == (
        'eaced99f551d8bf682d192c634a0276ea6f147b5453f372b9dc7d12a362b7376')


def test_action_revision_hashes_unicode_as_the_file_contract_bytes():
    import hashlib
    import json
    from kelmo.state import action_revision
    record = fictional_push_record()
    record['compatibility'] = {'note': 'Проверка «кириллица» — ünïcode'}
    body = {key: value for key, value in record.items() if key not in ('revision', 'previous_revision')}
    expected = hashlib.sha256((json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
                               + '\n').encode('utf-8')).hexdigest()
    ascii_only = hashlib.sha256((json.dumps(body, sort_keys=True, indent=2)
                                 + '\n').encode('utf-8')).hexdigest()
    assert action_revision(record) == expected != ascii_only


def test_action_revision_ignores_predecessor_pointer_and_stored_revision():
    from kelmo.state import action_revision
    record = fictional_push_record()
    base = action_revision(record)
    assert action_revision({**record, 'previous_revision': 'c' * 64}) == base
    assert action_revision({**record, 'revision': 'd' * 64}) == base


def test_action_revision_changes_when_an_authoritative_field_changes():
    from kelmo.state import action_revision
    record = fictional_push_record()
    assert action_revision({**record, 'state': 'pending'}) != action_revision(record)
    assert action_revision({**record, 'remote_ref': 'origin/main'}) != action_revision(record)


def test_an_action_publication_lets_go_of_the_baton_and_keeps_the_journal_inside_it(world, monkeypatch):
    """A StateAPI action publishes its pages and Git commit like an editor note: with the baton released, so an
    interactive writer goes first meanwhile, and every use of the shared journal connection still holds it."""
    import threading
    import time
    from kelmo.core import baton as batons
    from kelmo.service import Service
    from kelmo.state import ActionState
    from kelmo.wiki import engine
    ref = world.archive('I pushed the shared feature.\r\n')
    update = world.update(ref)
    world.service.close()
    world.service = world.stack.enter_context(Service(world.root, bindings=world.bindings, shared_journal=True))
    world.state = ActionState(world.service, world.contexts)
    baton = batons.Baton()
    entered, release, wrote = threading.Event(), threading.Event(), threading.Event()
    real = engine.publish_action

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(30):
            raise AssertionError('the publication was never let go')
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, 'publish_action', held)
    outcome = {}

    def act():
        try:
            with baton.hold():
                outcome['receipt'] = world.state.apply(update, world.origins['requester'], 'complete-push')
        except Exception as error:  # noqa: BLE001 - reported below
            outcome['error'] = error

    def writer():
        with baton.hold():
            wrote.set()

    world.service.journal.guard = baton.check
    batons.install(baton)
    act_thread = threading.Thread(target=act, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    try:
        act_thread.start()
        assert entered.wait(30), 'the publication never started'
        began = time.monotonic()
        writer_thread.start()
        fast = wrote.wait(3)
        took = time.monotonic() - began
        release.set()
        writer_thread.join(30)
        act_thread.join(60)
    finally:
        release.set()
        batons.install(None)
        world.service.journal.guard = None
    assert fast, f'the writer waited for the action publication ({took:.1f} s)'
    assert 'error' not in outcome, outcome.get('error')
    assert world.action()['state'] == 'completed'
    assert outcome['receipt'].generation is not None
