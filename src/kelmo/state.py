"""Explicit action admission and pure reduction, using the existing coordinator.

Storage, receipt replay, CAS locks and publication recovery belong to Service's
action_coordinator. This module neither opens storage nor commits a registry.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable, Protocol

from .context_binding import ActionBinding, ContextBindings
from .contracts import ActionUpdate, ArchiveVersion, OperationReceipt, Origin, SourcePacket, _text
from .errors import KelmoError

if TYPE_CHECKING:
    from .git_ingress import GitObservation


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def action_revision(record: dict) -> str:
    """File-contract revision: the predecessor pointer and the revision itself are excluded."""
    body = {key: value for key, value in record.items() if key not in ('revision', 'previous_revision')}
    text = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def event_revision(event: dict) -> str:
    """The enrolled work revision a captured Git event attests: the merged tip for a local merge."""
    return event.get('merged_sha') or event['new_sha']


def tool_receipt_digest(source: SourcePacket) -> str | None:
    """Digest of what a Git packet's tool receipt attests, independent of its transport.

    A raw packet is the receipt itself; a foreground capture wraps it as `tool_receipt`. Both
    reduce to the projection the producer parser reads (`git_ingress.attested_receipt`): command,
    cwd, normalized exit status and the non-blank output lines. Hook envelope fields, serialization
    and ignored whitespace therefore do not make the same output new. None when the bytes are not
    a JSON receipt the parser could read.
    """
    from .git_ingress import CAPTURE_PRODUCER, attested_receipt
    try:
        value = json.loads(source.content)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(value, dict) and value.get('producer') == CAPTURE_PRODUCER:
        value = value.get('tool_receipt')
    projection = attested_receipt(value)
    if projection is None:
        return None
    return hashlib.sha256(canonical_json(projection).encode('utf-8')).hexdigest()


def utc_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise KelmoError('E_EVIDENCE', 'Action time requires an explicit timezone.')
    return parsed.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def source_order(source: SourcePacket) -> int:
    """Stable UTC-microsecond source order, reusable by requester command issuers.

    A controller may retain an existing trusted per-action sequence instead;
    commands sharing an action must use the same ordering convention. Rebasing
    a revision must never renumber the source event.
    """
    observed = datetime.fromisoformat(utc_time(source.observed_at).replace('Z', '+00:00'))
    delta = observed - datetime(1, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) + 1


@dataclass(frozen=True)
class ActionFrame:
    """Supplied under the coordinator lock, after exact owning-receipt verification.

    history contains accepted_event dictionaries for this action in accepted order,
    including the current head. It excludes proposals and uncommitted intents.
    registry is the recovered current snapshot, not a semantic proposal baseline.
    """
    operation_id: str
    evidence: ArchiveVersion
    source_operation_id: str
    source: SourcePacket
    source_origin: Origin
    registry: dict
    ingested_at: str
    history: tuple[dict, ...]


@dataclass(frozen=True)
class ActionDecision:
    """An intent for the single publisher, never proof that publication happened."""
    outcome: str
    reason: str
    action_id: str | None = None
    record: dict | None = None
    expected_revision: str | None = None
    page_paths: tuple[str, ...] = ()
    changed: bool = False


class ActionCoordinator(Protocol):
    """Internal shared integration; methods recover/replay before calling decide.

    apply_action finds the evidence's owning journal operation for the same issued
    origin and calls ArchiveCoordinatorAPI.verify_receipt with that owner. It must
    not claim ownership merely by attaching a supplied EvidenceRef to a new row.
    Frame.source is that operation's original SourcePacket and source_origin is
    its reissued principal. ArchiveVersion.origin is first-capture audit metadata,
    not an exclusive owner: exact-source dedup can retain multiple owning receipts.

    ingest_git fingerprints the full SourcePacket plus origin/key. collect is
    called only for a new intake: its observation is durably saved and reused on
    retry. It captures the unchanged SourcePacket with the intake operation ID,
    verifies the archive receipt, then calls decide. Neither callback may publish.

    Both methods serialize recovery, request replay, frame construction, reduction,
    journal prepare and the existing publisher transaction. Receipts are durable
    only after the matching generation is committed. A crash resumes the saved
    intent through existing publication recovery, without rerunning a reducer on
    a different baseline or minting a different event timestamp.
    """
    def apply_action(self, update: ActionUpdate, origin: Origin, idempotency_key: str,
                     decide: Callable[[ActionFrame], ActionDecision]) -> OperationReceipt: ...

    def ingest_git(self, source: SourcePacket, origin: Origin, idempotency_key: str,
                   collect: Callable[[], GitObservation],
                   decide: Callable[[ActionFrame, GitObservation], ActionDecision]
                   ) -> OperationReceipt: ...


def feature_actions(registry: dict, feature_id: str) -> tuple[dict, ...]:
    """One source projection for every enrolled map of a shared feature."""
    return tuple(copy.deepcopy(record) for _, record in sorted(registry.get('actions', {}).items())
                 if record['subject_kind'] == 'feature' and record['subject_id'] == feature_id)


class ActionState:
    def __init__(self, service, contexts: ContextBindings):
        if not isinstance(contexts, ContextBindings) or contexts.bindings is not service.bindings:
            raise KelmoError('E_ORIGIN', 'Action state must use the Service origin authority.')
        self.service = service
        self.contexts = contexts

    def coordinator(self) -> ActionCoordinator:
        value = getattr(self.service, 'action_coordinator', None)
        if value is None or any(not callable(getattr(value, name, None))
                                for name in ('apply_action', 'ingest_git')):
            raise KelmoError('E_CAPABILITY', 'Action journal/CAS/publication integration is not installed.')
        return value

    def apply(self, update: ActionUpdate, origin: Origin, idempotency_key: str) -> OperationReceipt:
        self.contexts.require_origin(origin, requester=True)
        try:
            if not isinstance(update, ActionUpdate):
                raise ValueError('Use adoption/1 ActionUpdate.')
            update = ActionUpdate.from_dict(update.to_dict())
            _text(idempotency_key)
        except ValueError as error:
            raise KelmoError('E_INPUT', 'Invalid action command or idempotency key.') from error
        binding = self.contexts.require_action(update, origin)
        self.service.archive_api.assert_writer()
        receipt = self.coordinator().apply_action(
            update, origin, idempotency_key,
            lambda frame: self._reduce(frame, update, binding, origin, producer=False),
        )
        if not isinstance(receipt, OperationReceipt) or receipt.generation is None:
            raise KelmoError('E_PUBLICATION', 'Action coordinator returned no committed generation receipt.')
        return receipt

    def producer_decision(self, frame: ActionFrame, observation: GitObservation,
                          source: SourcePacket, origin: Origin) -> ActionDecision:
        """Internal ingress callback. No payload string can select an evidence mark."""
        self.contexts.require_origin(origin, producer=True)
        if not isinstance(frame, ActionFrame) or not isinstance(frame.evidence, ArchiveVersion):
            raise KelmoError('E_CAPABILITY', 'Coordinator must supply a verified ActionFrame.')
        if frame.source != source or frame.source_origin is not origin:
            raise KelmoError('E_RECEIPT', 'Git receipt differs from its original source or origin.')
        if observation.disposition != 'matched':
            return ActionDecision(observation.disposition, observation.reason)
        self.contexts.require_repository(origin, observation.repository_id)
        if self.contexts.repository_remotes.get(observation.repository_id) != observation.remote_identity:
            raise KelmoError('E_BINDING', 'Recorded Git observation belongs to a different remote.')
        event = observation.event()
        binding = self.contexts.match_git(origin, event['operation'], observation.repository_id,
                                         event_revision(event), 'refs/heads/' + event['target_branch'])
        if binding is None:
            raise KelmoError('E_BINDING', 'Recorded Git observation no longer has one enrolled action.')
        current = frame.registry.get('actions', {}).get(binding.action_id)
        update = ActionUpdate(**binding.to_dict(), expected_revision=current['revision'] if current else None,
                              event_order=source_order(source), transition='completed',
                              evidence_ref=frame.evidence.evidence)
        return self._reduce(frame, update, binding, origin, producer=True)

    def _reused_receipt(self, frame: ActionFrame) -> bool:
        """Whether an accepted PRODUCER event of this action came from the same tool receipt.

        Each accepted event's operation receipt is its own intake operation, so its original
        packet is read back exactly; only the canonical parsed receipt is compared.
        """
        digest = tool_receipt_digest(frame.source)
        if digest is None:
            return False
        for event in frame.history:
            if event['evidence_mark'] != 'PRODUCER':
                continue
            try:
                earlier = self.service.source_packet(event['operation_receipt_id'])
            except OSError as error:
                # Missing spool bytes are a repairable storage condition, never a new receipt.
                raise KelmoError('E_IO', 'An accepted event\'s source bytes cannot be read; restore the '
                                        'source spool and retry the same request.') from error
            if tool_receipt_digest(earlier) == digest:
                return True
        return False

    def _reduce(self, frame: ActionFrame, update: ActionUpdate, binding: ActionBinding,
                origin: Origin, *, producer: bool) -> ActionDecision:
        if not isinstance(frame, ActionFrame) or not isinstance(frame.evidence, ArchiveVersion):
            raise KelmoError('E_CAPABILITY', 'Coordinator must supply a verified ActionFrame.')
        if (not isinstance(frame.source, SourcePacket) or not frame.source_operation_id
                or frame.evidence.evidence != update.evidence_ref
                or frame.source.source_id != update.evidence_ref.source_id
                or frame.source.content_sha256 != update.evidence_ref.content_sha256
                or frame.source_origin is not origin):
            raise KelmoError('E_RECEIPT', 'Action must cite exact evidence owned by its trusted origin.')
        self.contexts.require_origin(origin, producer=producer, requester=not producer)
        self.contexts.require_action(update, origin)
        if update.event_order < 1:
            raise KelmoError('E_STALE', 'Action source order must be positive.')
        current = frame.registry.get('actions', {}).get(update.action_id)
        previous = current['revision'] if current else None
        if current:
            try:
                stored = ActionBinding(**{f.name: current.get(f.name) for f in fields(ActionBinding)})
            except ValueError as error:
                raise KelmoError('E_STATE', 'Accepted action identity is invalid.') from error
            if (not binding.same_identity(stored) or current.get('owner') != 'state-api'
                    or action_revision(current) != previous):
                raise KelmoError('E_STATE', 'Accepted action identity, owner or revision is inconsistent.')
            if not frame.history or frame.history[-1] != current['accepted_event']:
                raise KelmoError('E_RECEIPT', 'Action history does not identify the accepted registry head.')
        elif frame.history:
            raise KelmoError('E_STATE', 'Action journal history has no matching registry head.')
        if update.expected_revision != previous:
            raise KelmoError('E_CONFLICT', 'Action revision changed; a stale command cannot publish.')
        if producer and current and current['state'] == 'completed':
            return ActionDecision('unchanged', 'This exact enrolled Git action is already completed.',
                                  binding.action_id, copy.deepcopy(current), previous,
                                  self.contexts.pages_for(binding))
        correction = update.transition in ('correct', 'retract')
        if correction:
            if producer or not current or update.correction_of != current['accepted_event']['event_id']:
                raise KelmoError('E_CORRECTION', 'Correction must name this action\'s current accepted event.')
        elif update.correction_of is not None:
            raise KelmoError('E_CORRECTION', 'A referenced correction requires correct or retract.')
        if current and update.event_order <= current['accepted_event']['event_order']:
            raise KelmoError('E_STALE', 'Older source order is not advanced by arrival or rebase.')
        if current and not correction and current['state'] in ('completed', 'cancelled'):
            raise KelmoError('E_STATE', 'A closed action requires a named correction; new work needs a new ID.')
        if any(event['evidence_ref'] == update.evidence_ref.to_dict() for event in frame.history):
            raise KelmoError('E_STALE', 'Previously accepted evidence cannot be retimed into a new transition.')
        if producer and any(event['evidence_ref']['content_sha256'] == update.evidence_ref.content_sha256
                            for event in frame.history):
            # A re-archived copy of the same producer output is the same evidence under a new record.
            raise KelmoError('E_STALE', 'The same producer output cannot be reused as a new transition.')
        if producer and self._reused_receipt(frame):
            # The same tool receipt re-serialized, or wrapped in a capture envelope, is still that output.
            raise KelmoError('E_STALE', 'The same producer tool receipt cannot be reused as a new transition.')
        occurred_at = utc_time(frame.source.observed_at)
        if not correction and current:
            cutoff = max(utc_time(event['occurred_at']) for event in frame.history)
            if occurred_at < cutoff:
                raise KelmoError('E_STALE', 'Older evidence cannot replace a later accepted state.')
        # A correction invalidates a named claim. It does not invent a replacement
        # task status that the command did not carry.
        state = 'unresolved' if correction else update.transition
        record = {**binding.record_fields(), 'owner': 'state-api', 'state': state,
                  'previous_revision': previous,
                  'compatibility': copy.deepcopy(current.get('compatibility', {})) if current else {},
                  'accepted_event': dict(event_id=frame.operation_id, event_order=update.event_order,
                                         occurred_at=occurred_at, ingested_at=utc_time(frame.ingested_at),
                                         transition=state, evidence_ref=update.evidence_ref.to_dict(),
                                         evidence_mark='PRODUCER' if producer else 'REQUESTER',
                                         correction_of=update.correction_of,
                                         operation_receipt_id=frame.operation_id)}
        record['revision'] = action_revision(record)
        return ActionDecision('published', 'Explicit action accepted for publication.', binding.action_id,
                              record, previous, self.contexts.pages_for(binding), changed=True)
