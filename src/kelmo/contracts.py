"""Provider-neutral adoption/1 records. Deserialization never grants authority."""
from __future__ import annotations

import base64
import hashlib
import math
import re
import types
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol, Sequence, get_args, get_origin, get_type_hints, runtime_checkable

CONTRACT_VERSION = 'adoption/1'
INTAKE_BEHAVIOR_VERSION = 'durable-text/1'
PUBLIC_COMPATIBILITY_VERSION = 1


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Expected nonempty text.')


ISO_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def iso_day(value):
    """A real calendar day in ASCII YYYY-MM-DD; strptime alone also reads non-ASCII digits."""
    if not isinstance(value, str) or not ISO_DAY.fullmatch(value):
        raise ValueError('Expected an ASCII ISO day such as 2026-10-10.')
    datetime.strptime(value, '%Y-%m-%d')
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Expected lowercase SHA-256.')


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Expected bounded integer.')


def _encode(value):
    if is_dataclass(value):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, bytes):
        return base64.b64encode(value).decode('ascii')
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def _decode(kind, value):
    if get_origin(kind) is types.UnionType:
        for candidate in get_args(kind):
            try:
                return _decode(candidate, value)
            except (ValueError, TypeError):
                pass
        raise ValueError('Value does not match its contract type.')
    if kind is type(None):
        if value is not None:
            raise ValueError('Expected null.')
        return None
    if get_origin(kind) is tuple:
        if not isinstance(value, (tuple, list)):
            raise ValueError('Expected array.')
        return tuple(_decode(get_args(kind)[0], item) for item in value)
    if isinstance(kind, type) and issubclass(kind, Record):
        return kind.from_dict(value)
    if kind is bytes:
        if not isinstance(value, str):
            raise ValueError('Expected base64 bytes.')
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as error:
            raise ValueError('Invalid base64 bytes.') from error
    if kind is float:
        if type(value) not in (int, float):
            raise ValueError('Expected number.')
        return float(value)
    if type(value) is not kind:
        raise ValueError('Value does not match its contract type.')
    return value


_SHAPES = {}


def _shape(cls):
    """A record class's field names and resolved type hints. `get_type_hints` compiles and evaluates every
    annotation string on each call, so it runs once per class, not once per decoded object."""
    shape = _SHAPES.get(cls)
    if shape is None:
        shape = _SHAPES[cls] = (frozenset(field.name for field in fields(cls)), get_type_hints(cls))
    return shape


class Record:
    def to_dict(self):
        return _encode(self)

    @classmethod
    def from_dict(cls, value):
        names, hints = _shape(cls)
        if not isinstance(value, dict) or set(value) - names:
            raise ValueError('Unknown fields in ' + cls.__name__ + '.')
        try:
            return cls(**{key: _decode(hints[key], item) for key, item in value.items()})
        except TypeError as error:
            raise ValueError('Missing or invalid fields in ' + cls.__name__ + '.') from error


@dataclass(frozen=True)
class EvidenceRef(Record):
    archive_id: str
    version: int
    content_sha256: str
    source_id: str

    def __post_init__(self):
        _text(self.archive_id)
        _integer(self.version, 1)
        _hash(self.content_sha256)
        _text(self.source_id)


@dataclass(frozen=True)
class Origin(Record):
    authority_id: str
    client_id: str
    machine_tag: str
    session_id: str | None = None

    def __post_init__(self):
        for value in (self.authority_id, self.client_id, self.machine_tag):
            _text(value)
        if self.session_id is not None:
            _text(self.session_id)


@dataclass(frozen=True)
class Generation(Record):
    authority_id: str
    publication_id: int
    manifest_sha256: str

    def __post_init__(self):
        _text(self.authority_id)
        _integer(self.publication_id)
        _hash(self.manifest_sha256)


@dataclass(frozen=True)
class SourcePacket(Record):
    content: bytes
    source_kind: str
    source_id: str
    filename: str
    observed_at: str
    kind: str = 'event'
    domain: str = 'dev'
    project: str | None = None
    tags: tuple[str, ...] = ()
    valid_from: str | None = None
    require_vectors: bool = False

    def __post_init__(self):
        if not isinstance(self.content, bytes) or not self.content.strip() or len(self.content) > 1048576:
            raise ValueError('Source requires original bytes within one MiB.')
        self.content.decode('utf-8')
        for value in (self.source_kind, self.source_id, self.kind, self.domain):
            _text(value)
        if not isinstance(self.filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,239}\.md', self.filename):
            raise ValueError('Source filename must be a Markdown basename, never a client path.')
        _text(self.observed_at)
        if datetime.fromisoformat(self.observed_at.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Observed time requires a timezone.')
        if self.valid_from is not None:
            iso_day(self.valid_from)
        if self.project is not None:
            _text(self.project)
        if type(self.require_vectors) is not bool:
            raise ValueError('Vector admission policy must be boolean.')
        if not isinstance(self.tags, tuple):
            raise ValueError('Tags must be immutable.')
        for tag in self.tags:
            _text(tag)

    @property
    def content_sha256(self):
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class OperationReceipt(Record):
    operation_id: str
    idempotency_key: str
    source_id: str
    outcome: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    old_revision: str | None = None
    new_revision: str | None = None
    generation: Generation | None = None

    def __post_init__(self):
        for value in (self.operation_id, self.idempotency_key, self.source_id, self.outcome):
            _text(value)
        if not isinstance(self.evidence_refs, tuple) or not all(isinstance(ref, EvidenceRef) for ref in self.evidence_refs):
            raise ValueError('Receipt requires exact evidence references.')


@dataclass(frozen=True)
class ActionUpdate(Record):
    action_id: str
    subject_kind: str
    subject_id: str
    kind: str
    expected_revision: str | None
    event_order: int
    transition: str
    evidence_ref: EvidenceRef
    correction_of: str | None = None
    feature_id: str | None = None
    repository_id: str | None = None
    environment: str | None = None
    work_revision: str | None = None
    remote_ref: str | None = None
    artifact_id: str | None = None

    def __post_init__(self):
        for value in (self.action_id, self.subject_kind, self.subject_id, self.kind):
            _text(value)
        _integer(self.event_order)
        if self.subject_kind not in ('feature', 'topic', 'person', 'task'):
            raise ValueError('Unsupported action subject kind.')
        if not isinstance(self.evidence_ref, EvidenceRef):
            raise ValueError('Action requires exact evidence.')
        if self.expected_revision is not None:
            _hash(self.expected_revision)
        if self.transition not in ('pending', 'in-progress', 'completed', 'cancelled', 'unresolved', 'correct', 'retract'):
            raise ValueError('Unsupported action transition.')
        if self.transition in ('correct', 'retract'):
            _text(self.correction_of)
        if self.subject_kind == 'feature':
            if self.feature_id is not None and self.feature_id != self.subject_id:
                raise ValueError('Feature binding must identify the feature subject.')
        elif self.feature_id is not None:
            raise ValueError('Non-feature subjects must not claim feature identity.')
        if self.kind in ('push', 'merge'):
            for value in (self.repository_id, self.work_revision, self.remote_ref):
                _text(value)
        if self.kind == 'deploy':
            _text(self.environment)
            _text(self.artifact_id)
            if (self.repository_id is None) != (self.work_revision is None):
                raise ValueError('Git-derived deployments require repository and work revision together.')


@dataclass(frozen=True)
class EmbeddingSpace(Record):
    contract_version: str
    artifact_identity: str
    tokenizer_identity: str
    query_transform: str
    document_transform: str
    pooling: str
    projection: str
    normalization: str
    dimension: int
    dtype: str
    byte_order: str

    def __post_init__(self):
        for field in fields(self):
            if field.name != 'dimension':
                _text(getattr(self, field.name))
        _integer(self.dimension, 1)
        if self.dtype not in ('float32', 'float64') or self.byte_order not in ('little', 'big'):
            raise ValueError('Unsupported dense representation.')


@dataclass(frozen=True)
class ChunkProfile(Record):
    version: str
    segmentation: str
    token_budget: int
    overlap: int
    text_preparation: str
    tokenizer_identity: str

    def __post_init__(self):
        _integer(self.token_budget, 1)
        _integer(self.overlap)
        if self.overlap >= self.token_budget:
            raise ValueError('Overlap must be less than token budget.')
        for value in (self.version, self.segmentation, self.text_preparation, self.tokenizer_identity):
            _text(value)


@dataclass(frozen=True)
class VectorBatch(Record):
    space: EmbeddingSpace
    purpose: str
    vectors: tuple[tuple[float, ...], ...]

    def __post_init__(self):
        if not isinstance(self.space, EmbeddingSpace) or self.purpose not in ('query', 'document'):
            raise ValueError('Vector batch requires space and query/document purpose.')
        if not isinstance(self.vectors, tuple):
            raise ValueError('Vectors must be ordered immutable arrays.')
        for vector in self.vectors:
            if not isinstance(vector, tuple) or len(vector) != self.space.dimension:
                raise ValueError('Vector dimension mismatch.')
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise ValueError('Vector values must be finite numbers.')

    def validate_count(self, count):
        _integer(count)
        if len(self.vectors) != count:
            raise ValueError('Vector count does not match ordered inputs.')


@dataclass(frozen=True)
class ArchiveVersion(Record):
    evidence: EvidenceRef
    source: SourcePacket
    origin: Origin
    stored_at: str
    vectors_pending: bool

    def __post_init__(self):
        if not isinstance(self.evidence, EvidenceRef) or not isinstance(self.source, SourcePacket):
            raise ValueError('Archive version requires exact evidence and original source.')
        if (self.source.source_id, self.source.content_sha256) != (self.evidence.source_id, self.evidence.content_sha256):
            raise ValueError('Archive source does not match the evidence identity.')
        if not isinstance(self.origin, Origin) or type(self.vectors_pending) is not bool:
            raise ValueError('Invalid archive origin or vector status.')
        _text(self.stored_at)


@dataclass(frozen=True)
class ArchiveHistory(Record):
    versions: tuple[ArchiveVersion, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class ChunkLocator(Record):
    archive_id: str
    version: int
    content_sha256: str
    index_generation: str
    chunk_index: int

    def __post_init__(self):
        _text(self.archive_id)
        _integer(self.version, 1)
        _hash(self.content_sha256)
        _text(self.index_generation)
        _integer(self.chunk_index)


@dataclass(frozen=True)
class ChunkWindow(Record):
    locator: ChunkLocator
    start_index: int
    chunks: tuple[str, ...]


@dataclass(frozen=True)
class SearchFilters(Record):
    kind: str | None = None
    domain: str | None = None
    project: str | None = None
    tags: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    machine_tag: str | None = None

    def __post_init__(self):
        for value in (self.kind, self.domain, self.project, self.machine_tag):
            if value is not None:
                _text(value)
        if not isinstance(self.tags, tuple):
            raise ValueError('Filters require immutable tags.')
        for tag in self.tags:
            _text(tag)
        for value in (self.since, self.until):
            if value is not None:
                iso_day(value)

    def backend(self):
        return {key: value for key, value in self.to_dict().items() if value is not None and value != []}


@dataclass(frozen=True)
class SearchHit(Record):
    evidence: EvidenceRef
    content: str
    chunks: tuple[ChunkLocator, ...] = ()


@dataclass(frozen=True)
class SearchResult(Record):
    hits: tuple[SearchHit, ...]
    mode: str
    degraded: bool
    reason: str | None
    index_identity: str

    def __post_init__(self):
        if self.mode not in ('hybrid', 'semantic', 'lexical'):
            raise ValueError('Unsupported search mode.')
        if type(self.degraded) is not bool or self.degraded != (self.reason is not None):
            raise ValueError('Degradation requires an explicit reason.')
        _text(self.index_identity)


@dataclass(frozen=True)
class PageSnapshot(Record):
    path: str
    content: bytes
    content_sha256: str
    generation: Generation

    def __post_init__(self):
        _page_path(self.path)
        _hash(self.content_sha256)
        if not isinstance(self.content, bytes) or hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError('Page hash does not match exact bytes.')
        if not isinstance(self.generation, Generation):
            raise ValueError('Page requires publication generation.')


def _page_path(value):
    _text(value)
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or path.suffix != '.md' or any(part.startswith('.') for part in path.parts):
        raise ValueError('Page path must be canonical wiki-relative Markdown.')


@dataclass(frozen=True)
class PageBatch(Record):
    entries: tuple[PageSnapshot, ...]
    total: int
    next_cursor: str | None
    generation: Generation

    def __post_init__(self):
        _integer(self.total)
        if not isinstance(self.entries, tuple) or self.total < len(self.entries):
            raise ValueError('Page batch total must include all entries.')
        if any(not isinstance(page, PageSnapshot) or page.generation != self.generation for page in self.entries):
            raise ValueError('Page batch contains a mixed generation.')


@dataclass(frozen=True)
class ContextBinding(Record):
    repository_id: str
    worktree_id: str | None = None
    project: str | None = None


@dataclass(frozen=True)
class ContextBudget(Record):
    max_tokens: int

    def __post_init__(self):
        _integer(self.max_tokens, 1)


@dataclass(frozen=True)
class ContextBundle(Record):
    generation: Generation
    mandatory_rules: tuple[str, ...]
    pages: tuple[PageSnapshot, ...]
    omissions: tuple[str, ...]
    freshness: str
    source_mappings: tuple[EvidenceRef, ...]
    complete: bool

    def __post_init__(self):
        if type(self.complete) is not bool or self.freshness not in ('current', 'stale', 'missing'):
            raise ValueError('Context must expose completeness and freshness.')
        if self.complete and self.freshness != 'current':
            raise ValueError('Stale or missing context cannot be labelled complete.')
        if self.complete and self.omissions:
            raise ValueError('Context with omitted pages cannot be labelled complete.')
        if any(not isinstance(page, PageSnapshot) or page.generation != self.generation for page in self.pages):
            raise ValueError('Context contains a mixed generation.')


@dataclass(frozen=True)
class KnowledgeChange(Record):
    path: str
    baseline_sha256: str
    content: bytes
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self):
        _page_path(self.path)
        _hash(self.baseline_sha256)
        if not isinstance(self.content, bytes) or not self.evidence_refs or any(not isinstance(ref, EvidenceRef) for ref in self.evidence_refs):
            raise ValueError('Knowledge change requires original bytes and exact sources.')


@dataclass(frozen=True)
class KnowledgeProposal(Record):
    proposal_id: str
    baseline: Generation
    changes: tuple[KnowledgeChange, ...]

    def __post_init__(self):
        _text(self.proposal_id)
        if not isinstance(self.baseline, Generation) or not isinstance(self.changes, tuple):
            raise ValueError('Knowledge proposal requires a pinned baseline.')
        if any(not isinstance(change, KnowledgeChange) for change in self.changes):
            raise ValueError('Knowledge proposal requires typed changes.')
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ValueError('Duplicate proposed paths.')


@runtime_checkable
class EmbeddingProvider(Protocol):
    def describe(self) -> EmbeddingSpace: ...
    def embed_queries(self, texts: Sequence[str]) -> VectorBatch: ...
    def embed_documents(self, texts: Sequence[str]) -> VectorBatch: ...


@runtime_checkable
class ArchiveAPI(Protocol):
    def capture(self, source: SourcePacket, origin: Origin, idempotency_key: str) -> EvidenceRef: ...
    def get(self, archive_id: str, version: int | None = None) -> ArchiveVersion: ...
    def update(self, archive_id: str, expected_version: int, content: SourcePacket, origin: Origin) -> EvidenceRef: ...
    def history(self, archive_id: str, cursor: str | None, limit: int) -> ArchiveHistory: ...
    def expand(self, locator: ChunkLocator, neighbors: int) -> ChunkWindow: ...
    def search(self, queries: Sequence[str], filters: SearchFilters, limit: int, mode: str = 'hybrid') -> SearchResult: ...
    def hide(self, archive_id: str, origin: Origin, idempotency_key: str) -> OperationReceipt: ...


@runtime_checkable
class ArchiveCoordinatorAPI(ArchiveAPI, Protocol):
    """Local compatibility and owning-receipt operations, never public tools."""
    def verify_receipt(self, ref: EvidenceRef, operation_id: str, origin: Origin) -> ArchiveVersion: ...
    def assert_writer(self) -> None: ...
    def capture_receipt(self, operation_id: str, origin: Origin) -> EvidenceRef | None: ...
    def hide(self, archive_id: str, origin: Origin, idempotency_key: str,
             *, reason: str | None = None) -> OperationReceipt: ...
    def receipt_record(self, ref: EvidenceRef, operation_id: str, origin: Origin) -> dict: ...
    def bind_legacy_receipt(self, ref: EvidenceRef, operation_id: str, origin: Origin) -> None: ...
    def public_record(self, archive_id: str) -> dict: ...
    def list_records(self, limit: int, offset: int) -> dict: ...
    def search_records(self, query: str, filters: SearchFilters, limit: int, mode: str = 'hybrid') -> dict: ...
    def counts(self) -> dict: ...
    def hidden_at(self, archive_id: str) -> str | None: ...
    def refuse_hidden_target(self, archive_id: str) -> None: ...
    def supersede(self, archive_id: str, superseded_id: str, origin: Origin) -> None: ...
    def close(self) -> None: ...


class ArchiveAdminAPI(Protocol):
    """Separate controller capability; adoption clients never receive purge authority."""
    def purge(self, archive_id: str, disposition_id: str) -> OperationReceipt: ...


class StateAPI(Protocol):
    def apply(self, update: ActionUpdate, origin: Origin, idempotency_key: str) -> OperationReceipt: ...


class KnowledgeAPI(Protocol):
    def pages(self, project: str, generation: Generation, cursor: str | None, limit: int) -> PageBatch: ...
    def page(self, path: str, generation: Generation) -> PageSnapshot: ...
    def context(self, binding: ContextBinding, generation: Generation, budget: ContextBudget) -> ContextBundle: ...
    def publish(self, proposal: KnowledgeProposal, expected_generation: Generation) -> OperationReceipt: ...
