"""Shared StateAPI action publication and the semantic boundary over the one existing publisher.

The coordinator owns request replay, owning-receipt verification, reduction under the worker
lock, an immutable journal intent and its resumption. It publishes only through
engine.publish_action, which re-reads that prepared intent from the journal before any write.
No second registry, journal, lock or publisher is created here.
"""
from __future__ import annotations

import copy
import fcntl
import json
import time
from contextlib import contextmanager

from . import outcomes
from .contracts import EvidenceRef, Generation, KnowledgeProposal, OperationReceipt, SourcePacket
from .core.baton import outside_journal
from .errors import KelmoError
from .state import ActionDecision, ActionFrame, action_revision, event_revision
from .wiki import engine, pages

# Environment failures stay retryable; every other collector refusal is replayed as recorded.
TRANSIENT = frozenset({'E_BUSY', 'E_GIT', 'E_IO', 'E_WRITER'})
# Frame/reducer refusals that replay unchanged under the same key. Anything else raised before an
# intent is prepared (generation, configuration, capability, environment) leaves the row open.
DETERMINISTIC = frozenset({'E_STALE', 'E_STATE', 'E_CORRECTION', 'E_CONFLICT', 'E_RECEIPT', 'E_BINDING',
                           'E_ORIGIN', 'E_INPUT', 'E_EVIDENCE', 'E_OWNERSHIP'})
# Intake key prefix used only by capture_git; its archived packets are foreground captures.
CAPTURE_NAMESPACE = 'state-capture/'
LOCK_WAIT_SECONDS = 60


@contextmanager
def coordinator_lock(config, timeout=LOCK_WAIT_SECONDS):
    """The existing worker lock, waited for briefly rather than refused at the first contention."""
    with config.internal('.state/worker.lock').open('a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise KelmoError('E_BUSY', 'Another worker is processing this vault.') from error
                # Never wait for the worker lock holding the baton: its holder publishes with the baton
                # released and needs it back to write the receipt.
                with outside_journal():
                    time.sleep(0.05)
        yield


def _fingerprint(value):
    return engine.digest(engine.encoded(value))


def recover_publications(service):
    """The one recovery every publishing path runs under the worker lock its caller already holds.

    It finishes the common wiki transaction, then every prepared StateAPI and editor intent in
    preparation order, so neither kind can change the baseline of an earlier one. Each intent is
    replayed from the journal; no model runs. A typed refusal settles only its own request; an
    intent still prepared after a failure stops recovery and the caller, keeping later work behind it.
    """
    engine.recover_transaction(service.root)
    for operation_id, kind in service.journal.prepared_intents():
        owner = service.editor if kind == 'semantic' else service.action_coordinator
        try:
            owner._complete(operation_id)
        except KelmoError:
            if (service.journal.action_row(operation_id) or {}).get('state') == 'prepared':
                raise

    from .projects import _resume_rename, _resume_add
    _resume_rename(service)
    _resume_add(service)


def _git_action(registry, observation):
    """The accepted action a matched observation would advance, read from accepted state only.

    The reducer still resolves enrollment itself; a different answer there refuses the frame.
    """
    if observation.disposition != 'matched':
        return None
    event = observation.event()
    identity = (event['operation'], observation.repository_id, event_revision(event),
                'refs/heads/' + event['target_branch'])
    matches = [key for key, action in registry['actions'].items()
               if (action['kind'], action.get('repository_id'), action.get('work_revision'),
                   action.get('remote_ref')) == identity]
    return matches[0] if len(matches) == 1 else None


class StateCoordinator:
    """Service.action_coordinator: one lock, one journal, one registry and one publisher."""

    def __init__(self, service):
        self.service = service

    @property
    def journal(self):
        return self.service.journal

    def _idle(self):
        if self.journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Action admission requires an idle journal connection.')

    def _issued(self, origin):
        try:
            self.service.bindings.validate(origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'Action origin is not an issued controller principal.') from error

    @staticmethod
    def _request_key(kind, origin, idempotency_key):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise KelmoError('E_INPUT', 'An action request requires an idempotency key.')
        return _fingerprint({'kind': kind, 'origin': origin.to_dict(), 'key': idempotency_key})

    @staticmethod
    def _replay(row, fingerprint):
        if row is None:
            return None
        if row['fingerprint'] != fingerprint:
            raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different action request.')
        if row['state'] == 'finished':
            return OperationReceipt.from_dict(json.loads(row['receipt']))
        if row['state'] == 'refused':
            raise KelmoError(row['error_code'], row['error_message'])
        return None

    def _registry(self, generation):
        """The committed registry this pinned generation carries, never the working file."""
        return self.service.knowledge.registry(generation)

    def _generation(self):
        return self.service.knowledge.generation()

    def _baseline(self):
        """(generation, committed registry), refused while its working copies carry local edits.

        An uncommitted registry or managed-page edit is an environment condition: DirtyKnowledge is
        raised before any reduction or settlement, so the request stays open under the same key.
        """
        self._require_adopted()
        generation = self._generation()
        registry = self._registry(generation)
        engine.require_clean_baseline(self.service.root, registry)
        return generation, registry

    def _require_adopted(self):
        # Read from the committed registry at HEAD, so a store that never published a generation
        # is refused as having no registry rather than as a broken generation.
        if not self.service.knowledge.adopted():
            raise KelmoError('E_CAPABILITY', 'Accepted actions need a committed registry; kelmo init '
                                            'creates one.')

    @staticmethod
    def _outcome(row, disposition, reason):
        """The source outcome a git request settles with, written in the settlement's transaction."""
        return (disposition, reason) if row['kind'] == 'git' else None

    def _recover(self):
        """Finish or refuse every prepared intent before any new reduction reads the baseline."""
        recover_publications(self.service)

    def _receipt_of(self, operation_id, origin):
        if self.journal.get(operation_id)['durable_stage'] not in ('archived', 'applied'):
            return None
        exact = self.journal.evidence_record(operation_id)
        return (EvidenceRef.from_dict(exact) if exact is not None
                else self.service.archive_api.capture_receipt(operation_id, origin))

    def _owner(self, reference, origin):
        """The existing archived operation of this origin whose receipt is exactly this evidence."""
        if not isinstance(reference, EvidenceRef):
            raise KelmoError('E_INPUT', 'An action cites one exact EvidenceRef.')
        for operation_id in self.journal.action_owners(reference.archive_id, origin):
            if self._receipt_of(operation_id, origin) == reference:
                return operation_id, self.service.archive_api.verify_receipt(reference, operation_id, origin)
        raise KelmoError('E_RECEIPT', 'No archived operation of this trusted origin owns this exact evidence.')

    def _owned(self, operation_id, reference, origin):
        """Verify that one recorded owner still holds exactly this receipt for this origin."""
        if (not isinstance(reference, EvidenceRef) or operation_id not in
                self.journal.action_owners(reference.archive_id, origin)
                or self._receipt_of(operation_id, origin) != reference):
            raise KelmoError('E_RECEIPT', 'The recorded request is owned by different evidence.')
        return self.service.archive_api.verify_receipt(reference, operation_id, origin)

    def _frame(self, row, evidence, owner, origin, registry, action_id):
        trusted = self.service._operation_origin(owner)
        if trusted is not origin:
            raise KelmoError('E_RECEIPT', 'The evidence owner differs from the trusted request origin.')
        try:
            source = self.service.source_packet(owner)
        except OSError as error:
            # Spooled bytes unreadable now is a repairable storage condition, never invalid evidence.
            raise KelmoError('E_IO', 'The owning source bytes cannot be read; restore the source spool and '
                                    'retry the same request.') from error
        if (source.source_id, source.content_sha256) != (evidence.evidence.source_id,
                                                          evidence.evidence.content_sha256):
            raise KelmoError('E_RECEIPT', 'The owning source differs from its archived evidence.')
        history = self.journal.action_history(action_id) if action_id is not None else ()
        return ActionFrame(row['operation_id'], evidence, owner, source, trusted, copy.deepcopy(registry),
                           row['created_at'], history)

    def apply_action(self, update, origin, idempotency_key, decide):
        self._idle()
        self._issued(origin)
        request_key = self._request_key('apply', origin, idempotency_key)
        fingerprint = _fingerprint({'update': update.to_dict(), 'origin': origin.to_dict(),
                                    'key': idempotency_key})
        # A settled receipt replays even while an unrelated prepared publication cannot finish.
        receipt = self._replay(self.journal.action_request(request_key), fingerprint)
        if receipt is not None:
            return receipt
        with coordinator_lock(self.service.config):
            self._recover()
            row = self.journal.action_request(request_key)
            receipt = self._replay(row, fingerprint)
            if receipt is not None:
                return receipt
            generation, registry = self._baseline()
            if row is None:
                owner, evidence = self._owner(update.evidence_ref, origin)
                row = self.journal.action_begin(request_key, 'apply', fingerprint, origin, owner)
            else:
                # A pending request keeps the owner it recorded; a later duplicate cannot replace it.
                owner = row['source_operation_id']
                evidence = self._owned(owner, update.evidence_ref, origin)
            with self._refusing(row):
                frame = self._frame(row, evidence, owner, origin, registry, update.action_id)
                settled = self._admit(row, frame, decide(frame), idempotency_key, registry, generation)
            return settled if isinstance(settled, OperationReceipt) else self._publish(row, settled)

    @contextmanager
    def _refusing(self, row):
        """Settle a deterministic frame or reducer refusal on its request row; replays return it.

        Only the stage before an intent is prepared is settled here, and only for DETERMINISTIC
        codes. Generation, configuration and environment failures leave the row open for a retry,
        and publication or recovery failures after preparation keep their intent.
        """
        try:
            yield
        except KelmoError as error:
            if error.code in DETERMINISTIC and not self.journal.conn.in_transaction:
                self.journal.action_refuse(row['operation_id'], error.code, str(error), self._outcome(
                    row, 'unresolved', outcomes.repair_reason(error.code, error)))
            raise

    def _git_request(self, source, origin, idempotency_key):
        self._idle()
        self._issued(origin)
        if not isinstance(source, SourcePacket):
            raise KelmoError('E_INPUT', 'Git intake requires the original SourcePacket.')
        request_key = self._request_key('git', origin, idempotency_key)
        fingerprint = _fingerprint({'source': source.to_dict(), 'origin': origin.to_dict(),
                                    'key': idempotency_key})
        return request_key, fingerprint

    def _git_intake(self, source, origin, request_key, row, namespace='state-intake/'):
        if row is not None:
            return row['source_operation_id']
        # Capture and archive take their own locks, so they run before the worker lock.
        intake = self.service._state_intake(source, origin, namespace + request_key)
        settled = self.service.run(intake['id'])
        if settled['durable_stage'] not in ('archived', 'applied'):
            code = settled.get('error_code') or 'E_PROCESSING'
            if settled['terminal'] and settled['state'] == 'failed':
                # Run returns a terminal operation unchanged, so resubmitting alone cannot retry it.
                raise KelmoError(code, f"Git evidence intake operation {intake['id']} failed and is terminal; "
                                      f"fix the cause, run `kelmo queue retry {intake['id']}`, then resubmit "
                                      'the same request.')
            raise KelmoError(code, 'Git evidence was not archived; retry the same request.')
        return intake['id']

    def _verified_intake(self, source, origin, intake_id):
        exact = self.journal.evidence_record(intake_id)
        if exact is None:
            raise KelmoError('E_RECEIPT', 'Git intake has no exact archived receipt.')
        evidence = self.service.archive_api.verify_receipt(EvidenceRef.from_dict(exact), intake_id, origin)
        try:
            spooled = self.service.source_packet(intake_id)
        except OSError as error:
            raise KelmoError('E_IO', 'The intake source bytes cannot be read; restore the source spool and '
                                    'retry the same request.') from error
        if spooled != source:
            raise KelmoError('E_RECEIPT', 'The intake source differs from the original packet.')
        return evidence

    def _observe(self, request_key, fingerprint, origin, intake_id, collect):
        """Collect once for a new intake; the observation or refusal is stored on its request row."""
        from .git_ingress import GitObservation
        try:
            observation = collect()
        except KelmoError as error:
            if error.code in TRANSIENT:
                raise
            self.journal.action_begin(request_key, 'git', fingerprint, origin, intake_id,
                                      operation_id=intake_id, error=(error.code, str(error)),
                                      outcome=('unresolved', outcomes.repair_reason(error.code, error)))
            raise
        if not isinstance(observation, GitObservation):
            raise KelmoError('E_CAPABILITY', 'The Git collector returned no observation.')
        return self.journal.action_begin(request_key, 'git', fingerprint, origin, intake_id,
                                         operation_id=intake_id, observation=observation.to_dict())

    def captured_git(self, origin, idempotency_key):
        """(packet, row) of an earlier capture under this key, read without observing any ref.

        The packet's provenance is the capture-only intake namespace, which public capture paths
        refuse (Service._state_intake is its only writer), bound to the same issued origin and to
        typed git_event source metadata recorded at capture. (None, row) means the key belongs to
        a different intake.
        """
        self._idle()
        self._issued(origin)
        request_key = self._request_key('git', origin, idempotency_key)
        row = self.journal.action_request(request_key)
        intake_id = self.journal.operation_for_request_key(
            self.service.capture_request_key(CAPTURE_NAMESPACE + request_key))
        if intake_id is None:
            return None, row
        if self.journal.origin_record(intake_id) != origin.to_dict():
            raise KelmoError('E_IDEMPOTENCY', 'The capture belongs to a different trusted origin.')
        recorded = self.journal.source_metadata(intake_id)
        if (recorded is None or recorded['metadata'].get('capture_kind') != 'typed'
                or recorded['metadata'].get('packet', {}).get('source_kind') != 'git_event'):
            raise KelmoError('E_RECEIPT', 'The capture operation lacks its typed git_event source record.')
        if row is not None and row['source_operation_id'] != intake_id:
            raise KelmoError('E_RECEIPT', 'The recorded request is owned by a different intake.')
        try:
            return self.service.source_packet(intake_id), row
        except OSError as error:
            raise KelmoError('E_IO', 'The captured source bytes cannot be read; restore the source spool and '
                                    'retry the same request.') from error

    def capture_git(self, source, origin, idempotency_key, collect):
        """Foreground producer stage: archive the packet and store its observation, nothing more.

        No registry baseline is reduced and nothing is published; a later ingest_git with the same
        packet and key consumes the stored observation instead of collecting again. Called again
        with the archived packet, it replays a recorded refusal, or writes the request row a crash
        left missing after the archive capture, from that packet alone.
        """
        request_key, fingerprint = self._git_request(source, origin, idempotency_key)
        row = self.journal.action_request(request_key)
        self._replay(row, fingerprint)  # a different packet or a recorded refusal raises here
        if row is not None:
            return row
        intake_id = self._git_intake(source, origin, request_key, None, CAPTURE_NAMESPACE)
        with coordinator_lock(self.service.config):
            row = self.journal.action_request(request_key)
            self._replay(row, fingerprint)
            if row is not None:
                return row
            self._verified_intake(source, origin, intake_id)
            self._require_adopted()
            return self._observe(request_key, fingerprint, origin, intake_id, collect)

    def ingest_git(self, source, origin, idempotency_key, collect, decide):
        from .git_ingress import GitObservation
        request_key, fingerprint = self._git_request(source, origin, idempotency_key)
        row = self.journal.action_request(request_key)
        receipt = self._replay(row, fingerprint)
        if receipt is not None:
            return receipt
        intake_id = self._git_intake(source, origin, request_key, row)
        with coordinator_lock(self.service.config):
            self._recover()
            row = self.journal.action_request(request_key)
            receipt = self._replay(row, fingerprint)
            if receipt is not None:
                return receipt
            if row is not None and row['source_operation_id'] != intake_id:
                raise KelmoError('E_RECEIPT', 'The recorded request is owned by a different intake.')
            evidence = self._verified_intake(source, origin, intake_id)
            generation, registry = self._baseline()
            if row is None:
                row = self._observe(request_key, fingerprint, origin, intake_id, collect)
            observation = GitObservation.from_dict(json.loads(row['observation']))
            with self._refusing(row):
                frame = self._frame(row, evidence, intake_id, origin, registry, _git_action(registry, observation))
                settled = self._admit(row, frame, decide(frame, observation), idempotency_key, registry,
                                      generation)
            return settled if isinstance(settled, OperationReceipt) else self._publish(row, settled)

    def _publish(self, row, intent):
        self.journal.action_prepare(row['operation_id'], intent)
        return self._complete(row['operation_id'])

    def _admit(self, row, frame, decision, idempotency_key, registry, generation):
        """A settled receipt for ignored/unresolved/unchanged outcomes, else the intent to prepare.

        `generation` was read before refusal settlement began, so a generation failure is never
        recorded as this request's deterministic refusal.
        """
        if not isinstance(decision, ActionDecision):
            raise KelmoError('E_CAPABILITY', 'The reducer returned no ActionDecision.')
        operation_id = row['operation_id']
        reference = frame.evidence.evidence
        common = dict(operation_id=operation_id, idempotency_key=idempotency_key,
                      source_id=frame.source.source_id, evidence_refs=(reference,))
        if decision.outcome in ('ignored', 'unresolved'):
            receipt = OperationReceipt(outcome=decision.outcome, **common)
            disposition = 'unresolved' if decision.outcome == 'unresolved' else 'archive-only'
            self.journal.action_finish(operation_id, receipt.to_dict(),
                                       outcome=self._outcome(row, disposition, decision.reason))
            return receipt
        current = registry['actions'].get(decision.action_id)
        if decision.outcome == 'unchanged':
            if current is None or decision.changed or decision.record != current:
                raise KelmoError('E_CAPABILITY', 'An unchanged decision must name the accepted record.')
            receipt = OperationReceipt(outcome='unchanged', old_revision=current['revision'],
                                       new_revision=current['revision'], generation=generation, **common)
            self.journal.action_finish(operation_id, receipt.to_dict(),
                                       outcome=self._outcome(row, 'archive-only', decision.reason))
            return receipt
        if decision.outcome != 'published' or not decision.changed or not isinstance(decision.record, dict):
            raise KelmoError('E_CAPABILITY', 'Unsupported action decision outcome.')
        record = decision.record
        expected = current['revision'] if current else None
        event = record.get('accepted_event') or {}
        if (record.get('action_id') != decision.action_id or decision.expected_revision != expected
                or record.get('previous_revision') != expected or record.get('owner') != 'state-api'
                or event.get('event_id') != operation_id or event.get('operation_receipt_id') != operation_id
                or event.get('evidence_ref') != reference.to_dict()
                or record.get('revision') != action_revision(record)
                or (current and frame.history[-1:] != (current['accepted_event'],))):
            raise KelmoError('E_RECEIPT', 'The reduced record differs from its verified frame.')
        if 'repository_id' in record:
            # The same OriginBindings enrollment ContextBindings wraps; payload text adds none.
            try:
                self.service.bindings.repository(frame.source_origin, record['repository_id'])
            except (KeyError, ValueError) as error:
                raise KelmoError('E_BINDING', 'The action repository is outside trusted enrollment.') from error
        intent = {'request_kind': row['kind'], 'action_id': decision.action_id, 'expected_revision': expected,
                  'record': record, 'evidence_ref': reference.to_dict(), 'idempotency_key': idempotency_key,
                  'source_id': frame.source.source_id, 'source_operation_id': frame.source_operation_id}
        try:
            projected = engine.validate_registry(engine.apply_action_intent(registry, intent))
        except ValueError as error:
            raise KelmoError('E_STATE', 'The reduced action cannot enter the registry: ' + str(error)) from error
        targets = pages.action_targets(projected, record)
        if not targets:
            # An accepted action every page omits would be invisible; no page is guessed for it.
            raise KelmoError('E_BINDING', 'No registered page shows this action subject; register its page first.')
        if record['subject_kind'] == 'feature' and set(decision.page_paths) != set(targets):
            raise KelmoError('E_BINDING', 'Enrolled feature maps differ from the registered pages showing it.')
        try:
            # A budget refusal predictable at admission is diagnosed before any intent exists, so it
            # cannot leave a prepared intent behind. E_BUDGET stays retryable. A change after
            # preparation can still refuse at publication and block recovery until repaired.
            engine.check_budget(self.service.root, projected)
        except ValueError:
            pass  # a page the renderer refuses keeps its existing publication-time refusal
        intent.update(page_paths=list(targets), expected_generation=generation.to_dict())
        return intent

    def _complete(self, operation_id):
        """Publish one prepared intent, or recognize its committed generation, then store the receipt."""
        intent = self.journal.action_intent(operation_id)
        # Compared with the committed registry of the current generation: a working-file edit can
        # neither fake a moved baseline nor hide a committed publication.
        committed = self._generation()
        baseline = self._registry(committed)
        current = baseline['actions'].get(intent['action_id'])
        if current != intent['record']:
            if ((current or {}).get('revision') != intent['expected_revision']
                    or committed.to_dict() != intent['expected_generation']):
                message = 'Prepared action intent is stale: its baseline changed before publication.'
                outcome = (('unresolved', outcomes.repair_reason('E_STALE', message))
                           if intent['request_kind'] == 'git' else None)
                self.journal.action_refuse(operation_id, 'E_STALE', message, outcome)
                raise KelmoError('E_STALE', message)
            # Only an unpublished intent reaches the publisher, which reads the working registry: a
            # local edit there is named as DirtyKnowledge with its paths, not mislabelled by that read.
            # A publication already committed was recognized above and settles despite later edits.
            # The publication (renders, registry, Git commit) reads the prepared intent through its own connection
            # and runs under the worker lock the caller holds, so interactive writes go first meanwhile; the
            # receipt below is written with the baton again.
            with outside_journal():
                engine.require_clean_baseline(self.service.root, baseline)
                try:
                    engine.publish_action(self.service.root, operation_id)
                except KelmoError:
                    raise
                except ValueError as error:
                    # A page the publisher cannot render stays a typed refusal; the intent stays prepared.
                    raise KelmoError('E_CONFLICT', 'The prepared action cannot be published: ' + str(error)
                                    + ' Its intent stays pending until the page is repaired.') from error
        generation = self._generation()
        if self.service.knowledge.registry(generation)['actions'].get(intent['action_id']) != intent['record']:
            raise KelmoError('E_PUBLICATION', 'The committed generation lacks the prepared action record.')
        receipt = OperationReceipt(operation_id, intent['idempotency_key'], intent['source_id'], 'published',
                                   (EvidenceRef.from_dict(intent['evidence_ref']),), intent['expected_revision'],
                                   intent['record']['revision'], generation)
        outcome = (('applied', 'StateAPI accepted action ' + intent['action_id'])
                   if intent['request_kind'] == 'git' else None)
        self.journal.action_finish(operation_id, receipt.to_dict(),
                                   (intent['action_id'], intent['record']['accepted_event']), outcome)
        return receipt


def _managed_region(content):
    try:
        text = content.decode('utf-8')
    except UnicodeError as error:
        raise KelmoError('E_INPUT', 'Proposed page bytes are not UTF-8.') from error
    regions = []
    for start, end in ((pages.KB_START, pages.KB_END), (pages.NARRATIVE_START, pages.NARRATIVE_END)):
        count = text.count(start)
        if count != text.count(end) or count > 1 or (count and text.index(end) < text.index(start)):
            raise KelmoError('E_OWNERSHIP', 'Managed region markers are missing, repeated or reversed.')
        regions.append(text[text.index(start):text.index(end) + len(end)] if count else None)
    header = pages.leading_header(text)
    # A contract frontmatter is the publisher's projection; a legacy header stays unmanaged.
    return (header if pages._contract_header(header) else None, *regions)


class KnowledgeService:
    """Service.knowledge_api: pinned reads and the semantic publication safety boundary.

    Facts, StateAPI actions and their generated panels live in the publisher-owned managed region.
    A semantic proposal that changes it is refused; any other semantic page publication is not
    installed yet and refuses explicitly instead of reporting success.
    """

    def __init__(self, service):
        self.service = service

    @property
    def reader(self):
        return self.service.knowledge

    def pages(self, project, generation, cursor=None, limit=100):
        return self.reader.pages(project, generation, cursor, limit)

    def page(self, path, generation):
        return self.reader.page(path, generation)

    def context(self, binding, generation, budget):
        return self.reader.context(binding, generation, budget)

    def publish(self, proposal, expected_generation):
        if not isinstance(proposal, KnowledgeProposal) or not isinstance(expected_generation, Generation):
            raise KelmoError('E_INPUT', 'Publish one typed proposal against a pinned generation.')
        if proposal.baseline != expected_generation or self.reader.generation() != expected_generation:
            raise KelmoError('E_STALE', 'The proposal baseline is not the current accepted generation.')
        for change in proposal.changes:
            try:
                current = self.reader.page(change.path, expected_generation)
            except KelmoError as error:
                if error.code != 'E_NOT_FOUND':
                    raise
                current = None
            if current is not None and (current.path != change.path
                                        or current.content_sha256 != change.baseline_sha256):
                raise KelmoError('E_STALE', 'A proposed page changed after its baseline.')
            if _managed_region(current.content if current else b'') != _managed_region(change.content):
                raise KelmoError('E_OWNERSHIP', 'Semantic proposals cannot edit publisher-owned facts, '
                                               'actions or generated panels.')
        raise KelmoError('E_CAPABILITY', 'Semantic page publication is not installed; nothing was published.')
