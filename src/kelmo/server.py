"""Trusted enrollment, the shared-client channel and the restricted archive process.

Identity is never read from a request. The trusted launcher of a channel names the
enrolled client; the coordinator re-issues every Origin from controller bindings, and
the archive process re-issues the same Origins from the enrollment its parent sends.
"""
import base64
import hashlib
import json
import os
import re
import selectors
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath

from .contracts import (ArchiveHistory, ArchiveVersion, ChunkLocator, ChunkWindow, EvidenceRef,
                        Generation, OperationReceipt, Origin, Record, SearchFilters, SearchResult,
                        SourcePacket, _integer, _page_path, _text)
from .errors import KelmoError, sqlite_busy

PROTOCOL = 'kelmo-client/1'
ARCHIVE_PROTOCOL = 'kelmo-archive/1'
PROFILE_SCHEMA = 'kelmo-server/1'
MAX_FRAME = 32 * 1048576
ARCHIVE_TIMEOUT_SECONDS = 300
# A confined archive process with no frame for this long exits, freeing its archive connection and
# tokenizer; the coordinator starts a new one for the next operation.
ARCHIVE_IDLE_SECONDS = 300
# Exit status of an archive process that left idle. It read nothing after its last reply, so a frame the
# parent wrote as it left is unread and is sent again to the next process.
ARCHIVE_IDLE_EXIT = 75
CLIENT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
IDEMPOTENCY_KEY = re.compile(r'[\x21-\x7e]{1,200}')
NATIVE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,120}')
SHA256_HEX = re.compile(r'[0-9a-f]{64}')
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
SPOOL_READ_BYTES = 1048576
# A retryable refusal leaves a client outbox entry pending; every other refusal is settled. The server
# decides and sends `retryable` with each bound reply; a client never derives it from a request.
# This list is the fallback for environment failures. A server path that knows better (a state action's
# request row, an unsettled write) sets `retryable` on the error it raises, and that wins.
# E_CLAIM means this attempt lost its claim before settling, so the request is replayed, not refused.
# The daemon's own refusals: a full lane, a deadline that passed in the queue, a core that restarted under the
# call, and a cancel that reached a call before it did anything; none of them had an effect.
RETRYABLE = frozenset({'E_BUSY', 'E_FENCED', 'E_WRITER', 'E_ARCHIVE_PROCESS', 'E_IO', 'E_TRANSPORT',
                       'E_CAPTURE_RETRY', 'E_AUTHORITY', 'E_GIT', 'E_GENERATION', 'E_BUDGET', 'E_CLAIM',
                       'E_OVERLOADED', 'E_DEADLINE', 'E_CORE_RESTARTED', 'E_CANCELLED'})


def _retryable(error):
    """The server's verdict for one refusal: an explicit one set by server code, else the fallback list."""
    value = getattr(error, 'retryable', None)
    return value if isinstance(value, bool) else error.code in RETRYABLE


def _verdict(error, retryable):
    error.retryable = retryable
    return error


_busy = sqlite_busy


def _unsettled(error):
    """The coded refusal for an unexpected failure around unsettled work; the caller sets `retryable`."""
    if isinstance(error, sqlite3.Error):
        if _busy(error):
            return KelmoError('E_BUSY', 'The database is busy; nothing unsettled was dropped.')
        return KelmoError('E_STATE', 'A database operation failed; nothing unsettled was dropped.')
    if isinstance(error, OSError):
        return KelmoError('E_IO', 'Storage could not be accessed; nothing unsettled was dropped.')
    return KelmoError('E_INTERNAL', 'The operation did not complete; nothing unsettled was dropped.')


def _ledger(step, *args):
    """One client-request ledger step. Its refusal (an idle-connection check, a lost claim) leaves the
    request unsettled, so it is retryable; only a key naming a different request is final."""
    try:
        return step(*args)
    except KelmoError as error:
        if error.code == 'E_IDEMPOTENCY':
            raise
        raise _verdict(error, True)


GRAPH_GENERATION_KEY = 'graph.generation_snapshot'


@dataclass(frozen=True)
class WriterIdentity:
    authority_id: str
    writer_id: str
    epoch: int

    def __post_init__(self):
        _text(self.authority_id)
        _text(self.writer_id)
        _integer(self.epoch, 1)


@dataclass(frozen=True)
class RepositoryBinding:
    repository_id: str
    root: str

    def __post_init__(self):
        _text(self.repository_id)
        if not isinstance(self.root, str) or not PurePath(self.root).is_absolute() or '..' in PurePath(self.root).parts:
            raise ValueError('Controller repository roots must be canonical absolute paths.')


@dataclass(frozen=True)
class Enrollment:
    client_id: str
    machine_tag: str
    requester: bool = False
    repository_ids: tuple[str, ...] = ()

    def __post_init__(self):
        _text(self.client_id)
        _text(self.machine_tag)
        if type(self.requester) is not bool or not isinstance(self.repository_ids, tuple):
            raise ValueError('Invalid trusted enrollment.')


class OriginBindings:
    """Identity-based in-process capability, reissued from controller bindings on restart.

    Serialized Origin is an audit record, not an authenticated principal. A transport
    authenticates its channel first and then calls origin(); it never validates a payload
    as one.
    """
    def __init__(self, authority_id, writer, enrollments, repositories=()):
        _text(authority_id)
        if not isinstance(writer, WriterIdentity) or writer.authority_id != authority_id:
            raise ValueError('Writer belongs to a different authority.')
        self.authority_id = authority_id
        self.writer = writer
        self._clients = {item.client_id: item for item in enrollments}
        self._repositories = {item.repository_id: item for item in repositories}
        if len(self._clients) != len(enrollments) or len(self._repositories) != len(repositories):
            raise ValueError('Duplicate controller enrollment.')
        for item in enrollments:
            if set(item.repository_ids) - self._repositories.keys():
                raise ValueError('Client refers to an unenrolled repository.')
        self._issued = {}
        self._origins = {}

    def origin(self, client_id, session_id=None):
        enrolled = self._clients.get(client_id)
        if enrolled is None:
            raise ValueError('Client is not enrolled.')
        if (client_id, session_id) in self._origins:
            return self._origins[client_id, session_id]
        value = Origin(self.authority_id, client_id, enrolled.machine_tag, session_id)
        self._issued[id(value)] = value
        self._origins[client_id, session_id] = value
        return value

    def validate(self, origin):
        if not isinstance(origin, Origin) or self._issued.get(id(origin)) is not origin:
            raise ValueError('Origin was not issued by this trusted binding.')
        return self._clients[origin.client_id]

    def require_requester(self, origin):
        if not self.validate(origin).requester:
            raise ValueError('This binding cannot attest REQUESTER evidence.')

    def repository(self, origin, repository_id):
        if repository_id not in self.validate(origin).repository_ids:
            raise ValueError('Repository is outside client enrollment.')
        return self._repositories[repository_id]

    def check_writer(self, writer):
        if not isinstance(writer, WriterIdentity) or writer != self.writer:
            raise ValueError('Stale or foreign writer identity.')

    def describe(self):
        """The enrollment a trusted parent hands to its restricted archive process."""
        return {'authority_id': self.authority_id,
                'writer': {'writer_id': self.writer.writer_id, 'epoch': self.writer.epoch},
                'enrollments': [{'client_id': item.client_id, 'machine_tag': item.machine_tag,
                                 'requester': item.requester, 'repository_ids': list(item.repository_ids)}
                                for item in self._clients.values()],
                'repositories': [{'repository_id': item.repository_id, 'root': item.root}
                                 for item in self._repositories.values()]}

    @classmethod
    def from_description(cls, value):
        try:
            if set(value) != {'authority_id', 'writer', 'enrollments', 'repositories'}:
                raise ValueError('Unknown enrollment fields.')
            writer = value['writer']
            if set(writer) != {'writer_id', 'epoch'}:
                raise ValueError('Unknown writer fields.')
            enrollments = []
            for item in value['enrollments']:
                if set(item) != {'client_id', 'machine_tag', 'requester', 'repository_ids'}:
                    raise ValueError('Unknown client enrollment fields.')
                enrollments.append(Enrollment(item['client_id'], item['machine_tag'], item['requester'],
                                              tuple(item['repository_ids'])))
            repositories = []
            for item in value['repositories']:
                if set(item) != {'repository_id', 'root'}:
                    raise ValueError('Unknown repository fields.')
                repositories.append(RepositoryBinding(item['repository_id'], item['root']))
            authority = value['authority_id']
            return cls(authority, WriterIdentity(authority, writer['writer_id'], writer['epoch']),
                       tuple(enrollments), tuple(repositories))
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError('Invalid enrollment description.') from error

    @classmethod
    def local(cls, authority_id, machine_tag):
        return cls(authority_id, WriterIdentity(authority_id, 'local', 1),
                   (Enrollment('local', machine_tag),))


# --- Bounded JSON-lines framing shared by the client channel and the archive process ---

def _reject_constant(value):
    raise ValueError('Non-finite JSON numbers are not accepted.')


def _default(value):
    if isinstance(value, Record):
        return value.to_dict()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError('Value is not part of the wire contract.')


def encode_frame(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=_default, allow_nan=False).encode('utf-8') + b'\n'
    if len(data) > MAX_FRAME:
        raise KelmoError('E_LIMIT', 'A request or reply exceeds the bounded frame size.')
    return data


def decode_frame(line):
    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
        raise KelmoError('E_PROTOCOL', 'Frame is unterminated or exceeds the bounded frame size.')
    try:
        value = json.loads(line.decode('utf-8'), parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as error:
        raise KelmoError('E_PROTOCOL', 'Frame is not one UTF-8 JSON object.') from error
    if not isinstance(value, dict):
        raise KelmoError('E_PROTOCOL', 'Frame is not one JSON object.')
    return value


def read_frame(stream):
    """One frame from a blocking binary stream, or None at end of stream."""
    line = stream.readline(MAX_FRAME + 1)
    if not line:
        return None
    return decode_frame(line)


def write_frame(stream, value):
    stream.write(encode_frame(value))
    stream.flush()


def protocol_streams():
    """(stdin, protocol out) for a frame server, with fd 1 and sys.stdout moved onto stderr.

    The protocol keeps a private duplicate of the original stdout; afterwards a stray print, a library's
    write to sys.stdout or a native write to fd 1 lands on stderr and can never corrupt a frame. Streams
    that are not the process's own fd 1 (an embedding caller's replacement) are used as they are."""
    try:
        own = sys.stdout.fileno() == 1
    except (AttributeError, OSError, ValueError):
        own = False
    if not own:
        return sys.stdin.buffer, sys.stdout.buffer
    sys.stdout.flush()
    protocol = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return sys.stdin.buffer, os.fdopen(protocol, 'wb')


class PipeReader:
    """Deadline-bounded line reads from a child's stdout pipe."""

    def __init__(self, stream):
        self.stream = stream
        self.buffer = b''

    def frame(self, timeout):
        deadline = time.monotonic() + timeout
        descriptor = self.stream.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                if len(self.buffer) > MAX_FRAME:
                    raise KelmoError('E_PROTOCOL', 'Reply exceeds the bounded frame size.')
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise KelmoError('E_TRANSPORT', 'The channel did not reply before its deadline.')
                chunk = os.read(descriptor, 1048576)
                if not chunk:
                    raise KelmoError('E_TRANSPORT', 'The channel closed before replying.')
                self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b'\n')
        return decode_frame(line + b'\n')


# --- Restricted archive process: worker side ---

_IDLE = object()


class _ParentFrames:
    """The parent's frames on the worker side. With an idle limit and a real descriptor, reads go to the
    descriptor directly, so waiting on it never misses bytes hidden in a stream buffer; otherwise the
    stream is read as given and never idles out."""

    def __init__(self, stream, idle_seconds):
        self.stream = stream
        self.idle_seconds = idle_seconds
        self.buffer = b''
        self.descriptor = None
        if idle_seconds is not None:
            try:
                self.descriptor = stream.fileno()
            except (AttributeError, OSError, ValueError):
                self.descriptor = None

    def next(self):
        """The next frame, None at end of stream, or _IDLE when nothing at all arrived within the idle
        limit. A partly received frame is always waited for."""
        if self.descriptor is None:
            return read_frame(self.stream)
        with selectors.DefaultSelector() as selector:
            selector.register(self.descriptor, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                if len(self.buffer) > MAX_FRAME:
                    raise KelmoError('E_PROTOCOL', 'Frame exceeds the bounded frame size.')
                if not self.buffer and not selector.select(self.idle_seconds):
                    return _IDLE
                chunk = os.read(self.descriptor, 1048576)
                if not chunk:
                    if self.buffer:
                        raise KelmoError('E_PROTOCOL', 'The parent closed the channel inside a frame.')
                    return None
                self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b'\n')
        return decode_frame(line + b'\n')


def _idle_limit(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not value > 0:
        raise KelmoError('E_PROTOCOL', 'Archive process idle limit is invalid.')
    return value

def _decode_origin(bindings, value):
    """Re-issue a principal from the parent's enrollment; a decoded record is only a lookup key."""
    try:
        claimed = Origin.from_dict(value)
        trusted = bindings.origin(claimed.client_id, claimed.session_id)
    except ValueError as error:
        raise KelmoError('E_ORIGIN', 'Archive request names an unenrolled origin.') from error
    if trusted != claimed:
        raise KelmoError('E_ORIGIN', 'Archive request origin differs from its enrollment.')
    return trusted


def _readable(path):
    """Whether this process can read a path; only an OS refusal or invisibility counts as denied."""
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)))
        return True
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return False


def _probe_path(path):
    """readable, denied (the OS refused) or missing (absent from this process's view). A namespace that
    never mounted a path reports it missing, so a caller proves existence on the coordinator side first."""
    if _readable(path):
        return 'readable'
    try:
        os.lstat(path)
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'denied'
    return 'denied'


def _network_probe():
    import socket
    try:
        connection = socket.create_connection(('127.0.0.1', 9), timeout=1)
    except PermissionError:
        return 'denied'
    except ConnectionRefusedError:
        return 'allowed'
    except OSError as error:
        return 'denied' if error.errno in (1, 13) else 'error:' + type(error).__name__
    connection.close()
    return 'allowed'


class ArchiveWorker:
    """Dispatch of the archive contract inside the restricted process (or in process)."""

    def __init__(self, adapter, bindings, scratch, *, confined, fenced=False):
        self.adapter = adapter
        self.bindings = bindings
        self.scratch = Path(scratch)
        self.confined = confined
        # Fixed at startup from the server profile; this process never reloads a fence.
        self.fenced = fenced
        self._legacy = None

    def legacy_verified(self, ref, operation_id, origin):
        return self._legacy == (ref, operation_id, origin)

    def handle(self, frame):
        from kelmo.store.errors import (ArchiveConflictError, ArchiveEmbeddingError, ArchiveError, ArchiveLegacyRecordError,
                                    ArchiveNotFoundError, ArchiveValidationError)
        try:
            if set(frame) != {'method', 'args'} or not isinstance(frame['args'], dict):
                raise KelmoError('E_PROTOCOL', 'Archive frame requires method and args.')
            method = getattr(self, 'op_' + str(frame['method']), None)
            if method is None:
                raise KelmoError('E_PROTOCOL', 'Unknown archive operation.')
            try:
                bound = method.__func__.__code__.co_varnames[1:method.__func__.__code__.co_argcount]
            except AttributeError:
                bound = ()
            if set(frame['args']) - set(bound):
                raise KelmoError('E_PROTOCOL', 'Archive operation arguments do not match its contract.')
            return {'result': method(**frame['args'])}
        except KelmoError as error:
            code, message = error.code, str(error)
        except ArchiveEmbeddingError as error:
            code, message = 'E_EMBEDDING', str(error)
        except ArchiveValidationError as error:
            code, message = 'E_INPUT', str(error)
        except ArchiveConflictError as error:
            code, message = 'E_CONFLICT', str(error)
        except ArchiveNotFoundError as error:
            code, message = 'E_NOT_FOUND', str(error)
        except ArchiveLegacyRecordError as error:
            code, message = 'E_CAPABILITY', str(error)
        except ArchiveError:
            code, message = 'E_PROCESSING', 'Archive operation failed.'
        except ValueError as error:
            code, message = 'E_INPUT', str(error)
        except TypeError:
            code, message = 'E_PROTOCOL', 'Archive operation arguments do not match its contract.'
        except sqlite3.Error as error:
            refusal = _unsettled(error)
            code, message = refusal.code, ('The archive database is busy.' if refusal.code == 'E_BUSY'
                                           else 'Archive database operation failed.')
        except OSError:
            code, message = 'E_IO', 'Archive storage could not be accessed.'
        return {'error': {'code': code, 'message': message}}

    def _origin(self, value):
        return _decode_origin(self.bindings, value)

    def op_capture(self, source, origin, idempotency_key):
        return self.adapter.capture(SourcePacket.from_dict(source), self._origin(origin), idempotency_key)

    def op_get(self, archive_id, version=None):
        return self.adapter.get(archive_id, version)

    def op_update(self, archive_id, expected_version, content, origin, receipt=None):
        return self.adapter.update(archive_id, expected_version, SourcePacket.from_dict(content), self._origin(origin),
                                   receipt=_receipt_key(receipt))

    def op_update_receipt(self, receipt, origin):
        return self.adapter.update_receipt(_receipt_key(receipt), self._origin(origin))

    def op_history(self, archive_id, cursor, limit):
        return self.adapter.history(archive_id, cursor, limit)

    def op_expand(self, locator, neighbors):
        return self.adapter.expand(ChunkLocator.from_dict(locator), neighbors)

    def op_search(self, queries, filters, limit, mode='hybrid'):
        return self.adapter.search(tuple(queries), SearchFilters.from_dict(filters), limit, mode)

    def op_hide(self, archive_id, origin, idempotency_key, reason=None):
        return self.adapter.hide(archive_id, self._origin(origin), idempotency_key, reason=reason)

    def op_verify_receipt(self, ref, operation_id, origin):
        return self.adapter.verify_receipt(EvidenceRef.from_dict(ref), operation_id, self._origin(origin))

    def op_assert_writer(self):
        self.adapter.assert_writer()

    def op_capture_receipt(self, operation_id, origin):
        return self.adapter.capture_receipt(operation_id, self._origin(origin))

    def op_receipt_record(self, ref, operation_id, origin):
        return self.adapter.receipt_record(EvidenceRef.from_dict(ref), operation_id, self._origin(origin))

    def op_bind_legacy_receipt(self, ref, operation_id, origin):
        # Only the parent's coordinator journal can verify ownership; it sends this after that check.
        ref, origin = EvidenceRef.from_dict(ref), self._origin(origin)
        self._legacy = (ref, operation_id, origin)
        try:
            self.adapter.bind_legacy_receipt(ref, operation_id, origin)
        finally:
            self._legacy = None

    def op_public_record(self, archive_id):
        return self.adapter.public_record(archive_id)

    def op_full_record(self, archive_id):
        return self.adapter.full_record(archive_id)

    def op_index_generation(self):
        return self.adapter.index_generation()

    def op_list_records(self, limit, offset):
        return self.adapter.list_records(limit, offset)

    def op_list_filtered(self, filters, limit, offset, order_by, order):
        return filtered_list(self.adapter, SearchFilters.from_dict(filters), limit, offset, order_by, order)

    def op_search_records(self, query, filters, limit, mode='hybrid'):
        return self.adapter.search_records(query, SearchFilters.from_dict(filters), limit, mode)

    def op_counts(self):
        return self.adapter.counts()

    def op_stats(self):
        return self.adapter.stats()

    def op_hidden_at(self, archive_id):
        return self.adapter.hidden_at(archive_id)

    def op_refuse_hidden_target(self, archive_id):
        self.adapter.refuse_hidden_target(archive_id)

    def op_supersede(self, archive_id, superseded_id, origin):
        self.adapter.supersede(archive_id, superseded_id, self._origin(origin))

    def op_probe(self, paths):
        if not isinstance(paths, list) or not all(isinstance(path, str) and os.path.isabs(path) for path in paths):
            raise KelmoError('E_INPUT', 'Probe paths must be absolute.')
        return {'confined': self.confined, 'network': _network_probe(),
                'paths': {path: _probe_path(path) for path in paths}}

    def op_graph_publish(self, snapshot):
        if self.fenced:
            raise KelmoError('E_FENCED', 'This archive is fenced; the derived graph is not rebuilt.')
        return publish_graph_snapshot(self.adapter.connection, self.bindings.authority_id, snapshot, self.scratch,
                                      writer_check=self.adapter.assert_writer)

    def op_graph(self, operation, generation, params):
        return query_graph_snapshot(self.adapter.connection, Generation.from_dict(generation), operation, params,
                                    writer_check=None if self.fenced else self.adapter.assert_writer)

    def op_close(self):
        return None


def _receipt_key(value):
    """An update receipt identity on the wire: {'operation_id', 'fingerprint'}, or None."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'operation_id', 'fingerprint'}
            or not all(isinstance(item, str) and 1 <= len(item) <= 300 for item in value.values())):
        raise KelmoError('E_PROTOCOL', 'An update receipt names operation_id and fingerprint only.')
    return value['operation_id'], value['fingerprint']


def filtered_list(adapter, filters, limit, offset, order_by='created_at', order='desc'):
    """Deterministic filtered listing of visible records, the old memory_list shape."""
    from kelmo.store.errors import ArchiveLegacyRecordError
    from kelmo.store.memory.list import memory_list
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise KelmoError('E_INPUT', 'List limit must be 1..500 and offset 0 or more.')
    if order_by not in ('created_at', 'updated_at', 'valid_from') or order not in ('asc', 'desc'):
        raise KelmoError('E_INPUT', 'List order must be created_at, updated_at or valid_from, asc or desc.')
    backend = filters.backend()
    with adapter._read_snapshot():
        try:
            return memory_list(adapter.connection, limit=limit, offset=offset, order_by=order_by,
                               order=order, exclude_forgotten=True, **backend)
        except ArchiveLegacyRecordError as error:
            raise KelmoError('E_CAPABILITY', str(error)) from error


def snapshot_digest(pages):
    return hashlib.sha256(b''.join(path.encode('utf-8') + b'\0' + sha.encode('ascii') + b'\n'
                                   for path, sha in sorted(pages))).hexdigest()


def graph_snapshot(service, generation):
    """The allowlisted immutable wiki snapshot of one committed generation, never a directory."""
    reader = service.knowledge
    pinned = reader._pinned(generation)
    pages = []
    for path in pinned['paths']:
        page = reader._page(pinned, path)
        pages.append({'path': path, 'sha256': page.content_sha256,
                      'content': base64.b64encode(page.content).decode('ascii')})
    return {'generation': generation.to_dict(),
            'snapshot_sha256': snapshot_digest((page['path'], page['sha256']) for page in pages),
            'pages': pages}


def publish_graph_snapshot(connection, authority_id, snapshot, scratch, *, writer_check):
    """Validate a snapshot by hash, then switch the derived graph generation atomically.

    The writer epoch is checked inside the publishing transaction, so a stale writer never publishes."""
    from kelmo.store.graph.analysis import assignment_digest, implementation
    from kelmo.store.graph.build import ensure_schema, publish_graph, scan_wiki
    from .graph import _savepoint
    from .journal import now
    if not isinstance(snapshot, dict) or set(snapshot) != {'generation', 'snapshot_sha256', 'pages'}:
        raise KelmoError('E_INPUT', 'Graph snapshot requires generation, hash and pages.')
    generation = Generation.from_dict(snapshot['generation'])
    if generation.authority_id != authority_id:
        raise KelmoError('E_GENERATION', 'Graph snapshot belongs to another authority.')
    pages, seen = [], set()
    for page in snapshot['pages']:
        if not isinstance(page, dict) or set(page) != {'path', 'sha256', 'content'}:
            raise KelmoError('E_INPUT', 'Graph snapshot page requires path, hash and content.')
        try:
            _page_path(page['path'])
            content = base64.b64decode(page['content'], validate=True)
        except (TypeError, ValueError) as error:
            raise KelmoError('E_INPUT', 'Graph snapshot page is not canonical.') from error
        if page['path'] in seen or hashlib.sha256(content).hexdigest() != page['sha256']:
            raise KelmoError('E_SOURCE', 'Graph snapshot page differs from its hash.')
        seen.add(page['path'])
        pages.append((page['path'], page['sha256'], content))
    if snapshot_digest((path, sha) for path, sha, _ in pages) != snapshot['snapshot_sha256']:
        raise KelmoError('E_SOURCE', 'Graph snapshot differs from its declared hash.')
    directory = Path(tempfile.mkdtemp(prefix='graph-', dir=scratch))
    try:
        for path, _, content in pages:
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        scanned = scan_wiki(directory)
        if not isinstance(scanned, dict) or scanned.get('errors'):
            raise KelmoError('E_GRAPH_SCAN', 'Graph snapshot could not be scanned.')
        record = {'generation': generation.to_dict(), 'snapshot_sha256': snapshot['snapshot_sha256'],
                  'source_digest': scanned['source_digest'], 'built_at': now()}
        if connection.in_transaction:
            raise KelmoError('E_STATE', 'Graph publication requires an idle archive connection.')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            writer_check()
            with _savepoint(connection):
                ensure_schema(connection)
                publish_graph(connection, scanned)
                record.update(assignment_sha256=assignment_digest(connection),
                              analysis_implementation=implementation())
                connection.execute('''INSERT INTO archive_meta (key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                                   (GRAPH_GENERATION_KEY, json.dumps(record, sort_keys=True)))
        return record
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def query_graph_snapshot(connection, generation, operation, params, *, writer_check=None):
    """Read the derived graph. The schema is created only by a current writer (writer_check), inside a
    transaction that checks its epoch; without one (fenced) a never-built graph reads as absent."""
    from kelmo.store.graph.analysis import analysis_stats, implementation
    from kelmo.store.graph.build import ensure_schema
    from kelmo.store.graph.query import graph_query, resolve_entity
    from kelmo.store.graph.stats import graph_stats
    row = connection.execute('SELECT value FROM archive_meta WHERE key=?', (GRAPH_GENERATION_KEY,)).fetchone()
    if row is None and writer_check is not None:
        if connection.in_transaction:
            raise KelmoError('E_STATE', 'Graph schema creation requires an idle archive connection.')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            writer_check()
            ensure_schema(connection)
    record = json.loads(row[0]) if row else None
    # A snapshot published before its analytics were recorded, or by another analysis build, is stale:
    # the next query republishes it rather than label groups with an assignment nobody computed.
    current = (record is not None and record.get('generation') == generation.to_dict()
               and isinstance(record.get('assignment_sha256'), str)
               and record.get('analysis_implementation') == implementation())
    status = 'current' if current else ('stale' if record else 'absent')
    metadata = {'status': status, 'generation': generation.to_dict(),
                'source_digest': record.get('source_digest') if record else None,
                'snapshot_sha256': record.get('snapshot_sha256') if record else None,
                'built_at': record.get('built_at') if record else None,
                'built_generation': record.get('generation') if record else None,
                'derived': 'topology from links, hierarchy and tags; not verified facts'}
    if operation == 'stats':
        try:
            counts = graph_stats(connection)
        except sqlite3.OperationalError:
            if record is not None:
                raise
            counts = None  # never built, and this fenced reader does not create the schema
        result = {'graph': metadata, 'counts': counts}
        if status == 'current':
            result['analysis'] = dict(analysis_stats(connection), **_analysis_label(record))
        return result
    if status != 'current':
        raise KelmoError('E_GRAPH_STALE', 'The derived graph is not built from the current generation.')
    if operation == 'query':
        # Navigation nodes carry the derived community_id, surprise_score and is_god_node too, and
        # exclude_god_nodes filters by derived hubs: every answer names the assignment behind them.
        relations = (sorted({item.casefold() for item in params['relation_filter']})
                     if params.get('relation_filter') is not None and params.get('action') == 'surprises'
                     else None)
        return {'result': graph_query(connection, **params), 'graph': metadata,
                'analysis': _analysis_label(record, relations)}
    if operation == 'resolve':
        page_id, error = resolve_entity(connection, params['entity_id'])
        if error:
            raise KelmoError('E_AMBIGUOUS', 'Page identifier is ambiguous; use an explicit wiki-relative ID.')
        if not isinstance(page_id, str):
            raise KelmoError('E_NOT_FOUND', 'Requested entity is not a page of this generation.')
        return {'id': page_id, 'graph': metadata}
    raise KelmoError('E_INPUT', 'Unknown graph operation.')


def _analysis_label(record, relations=None):
    """Snapshot identity of derived topology, as the local graph labels it: groups, hubs and scores are
    structural suggestions of this assignment, never verified facts."""
    from kelmo.store.graph.analysis import LABEL
    label = dict(LABEL, snapshot=record.get('source_digest'), built_at=record.get('built_at'),
                 implementation=record.get('analysis_implementation'),
                 assignment_sha256=record.get('assignment_sha256'))
    if relations is not None:
        label['relations'] = relations
    return label


def _check_pins(result, pins):
    """A caller holding a named older snapshot or analytics assignment is refused, never silently answered
    from a different one."""
    if pins.get('snapshot') is not None and pins['snapshot'] != result['graph'].get('source_digest'):
        raise KelmoError('E_GRAPH_STALE', 'The named graph snapshot is no longer current; '
                        'query again without it, or with the current source_digest.')
    if pins.get('assignment') is not None and pins['assignment'] != result['analysis'].get('assignment_sha256'):
        raise KelmoError('E_GRAPH_STALE', 'The named analytics assignment is no longer current; '
                        'query again without it, or with the current assignment_sha256.')


def snapshot_graph(service, operation, params=None):
    """Graph reads through the restricted archive: the coordinator supplies the snapshot, the options are
    validated as the local graph validates them, and a snapshot or assignment pin is checked here."""
    from .graph import graph_params
    generation = service.knowledge.generation()
    archive = service.archive_api
    pins = {}
    if operation == 'query':
        params = graph_params(params or {})
        pins = {name: params.pop(name) for name in ('snapshot', 'assignment')}
    elif operation == 'read':
        if not isinstance((params or {}).get('entity_id'), str) or not params['entity_id'].strip():
            raise KelmoError('E_INPUT', 'entity_id must be a nonempty string.', 2)
    fenced = getattr(archive, 'fenced', False)
    if operation == 'rebuild':
        if fenced:
            raise KelmoError('E_FENCED', 'This server is fenced; the derived graph is not rebuilt.')
        archive.graph_publish(graph_snapshot(service, generation))
        return archive.graph('stats', generation, {})
    if operation == 'stats':
        return archive.graph('stats', generation, {})
    remote = 'resolve' if operation == 'read' else 'query'
    for attempt in (0, 1):
        try:
            result = archive.graph(remote, generation, params)
            break
        except KelmoError as error:
            # A fenced server reads a current snapshot but never publishes a new one.
            if error.code != 'E_GRAPH_STALE' or attempt or fenced:
                raise
            archive.graph_publish(graph_snapshot(service, generation))
    if operation == 'read':
        page = service.knowledge.page(result['id'] + '.md', generation)
        return {'id': result['id'], 'file_path': page.path, 'content': page.content.decode('utf-8'),
                'graph': result['graph']}
    if operation == 'query':
        _check_pins(result, pins)
    return result


def chunk_index(settings):
    """The archive's configured chunk index from validated [embedding] settings, built as Service builds
    it: no settings, no index; a provider that cannot be opened leaves vectors pending, never replaced."""
    if settings is None:
        return None
    from kelmo.store.errors import ArchiveEmbeddingError
    from .archive_adapter import ChunkIndex
    from .embeddings import provider_from_settings
    try:
        return ChunkIndex(settings, provider_from_settings(settings))
    except ArchiveEmbeddingError as error:
        return ChunkIndex(settings, None, unavailable=error.data.get('reason') or 'embedding_unavailable')


def embedding_launch(settings, *, poison=False):
    """What the restricted archive process needs for the configured provider, and nothing else.

    Returns (wire settings, read-only paths, exact files, channel descriptors, environment). The settings
    are the coordinator's validated [embedding] values; a local model adds its directory read-only, a
    broker adds its tokenizer file and passes the launcher's channel descriptors under the same variable
    name. No credential, host or other environment value crosses.

    `poison` follows an archive process that died during a call: it may have left half a request in the
    broker pipes, so the pair is poisoned for this process and never passed to a child again; the new
    child keeps text and chunks durable with vectors pending."""
    if settings is None:
        return None, [], [], (), {}
    wire = json.loads(json.dumps(settings, default=str))
    if settings['provider'] == 'local':
        return wire, [Path(settings['model_dir']).expanduser().resolve()], [], (), {}
    from kelmo.store.config import get_embed_fds
    from .embeddings import channel_poisoned, poison_channel
    name = settings['fds_env']
    try:
        fds = get_embed_fds(name)
    except ValueError as error:
        raise KelmoError('E_CONFIG', f'{name} does not name an embedding channel.') from error
    if fds is not None:
        try:
            if poison:
                poison_channel(*fds)
            withheld = channel_poisoned(*fds)
        except OSError as error:
            raise KelmoError('E_CONFIG', f'{name} names unusable descriptors.') from error
        if withheld:
            fds = None
    if fds is None:
        # The child opens no provider; text and chunks stay durable and vectors stay pending.
        return wire, [], [Path(settings['tokenizer']).resolve()], (), {}
    return wire, [], [Path(settings['tokenizer']).resolve()], tuple(fds), {name: os.environ[name]}


def archive_worker_main(stdin, stdout, *, confined):
    """Serve one parent over stdin/stdout after checking the OS boundary is really in force."""
    from .archive_adapter import LocalArchiveAdapter
    try:
        start = read_frame(stdin)
        if start is None:
            return 1
        if set(start) != {'protocol', 'enrollment', 'archive', 'cache', 'scratch', 'canaries', 'fenced',
                          'embedding', 'idle_seconds'} or start['protocol'] != ARCHIVE_PROTOCOL \
                or type(start['fenced']) is not bool:
            raise KelmoError('E_PROTOCOL', 'Archive process startup frame is invalid.')
        # The parent writes nothing after the startup frame until it has read the ready reply, so the
        # stream's buffer is empty here and later frames can be read from its descriptor.
        frames = _ParentFrames(stdin, _idle_limit(start['idle_seconds']))
        if confined:
            readable = [path for path in start['canaries'] if _readable(path)]
            if readable:
                raise KelmoError('E_ISOLATION', 'The archive process can read coordinator data; '
                                'native confinement is absent, so it refuses to serve.')
        bindings = OriginBindings.from_description(start['enrollment'])
        worker = ArchiveWorker(None, bindings, start['scratch'], confined=confined, fenced=start['fenced'])
        worker.adapter = LocalArchiveAdapter(start['archive'], start['cache'], bindings,
                                             legacy_receipt_verifier=worker.legacy_verified,
                                             chunk_index=chunk_index(start['embedding']))
    except (KelmoError, ValueError, OSError, sqlite3.Error) as error:
        if isinstance(error, KelmoError):
            code = error.code
        elif isinstance(error, (OSError, sqlite3.Error)):
            # A busy archive at startup is contention (E_BUSY), not a broken boundary.
            code = _unsettled(error).code
        else:
            code = 'E_PROTOCOL'
        write_frame(stdout, {'ready': False, 'error': {'code': code, 'message': str(error)}})
        return 1
    write_frame(stdout, {'ready': True, 'confined': confined})
    try:
        while True:
            try:
                frame = frames.next()
            except KelmoError as error:
                write_frame(stdout, {'error': {'code': error.code, 'message': str(error)}})
                return 1
            if frame is _IDLE:
                return ARCHIVE_IDLE_EXIT
            if frame is None:
                return 0
            write_frame(stdout, worker.handle(frame))
            if frame.get('method') == 'close':
                return 0
    finally:
        worker.adapter.close()


# --- Restricted archive process: coordinator side ---

class _Loopback:
    """The same archive contract in process: each frame still crosses JSON encoding."""

    def __init__(self, worker):
        self.worker = worker

    def call(self, frame):
        return decode_frame(encode_frame(self.worker.handle(decode_frame(encode_frame(frame)))))

    def close(self):
        try:
            self.worker.adapter.close()
        finally:
            shutil.rmtree(self.worker.scratch, ignore_errors=True)


class _Pipe:
    def __init__(self, process, scratch):
        self.process = process
        self.scratch = scratch
        self.reader = PipeReader(process.stdout)
        self.failed = None
        # True once the process failed with a frame in flight: its effect, and anything it was writing to
        # a shared channel, is unknown.
        self.uncertain = False
        # True when the frame in flight met an idle exit: the process read nothing, the frame is unread.
        self.unread = False

    def idle_exit(self):
        """Whether the process exited while no call was in flight; checked before the next frame is
        written, so that frame goes to a new process and is never lost to a dead one."""
        if not self.failed and self.process.poll() is not None:
            self.failed = 'The restricted archive process exited while idle.'
            self.close()
        return bool(self.failed) and not self.uncertain

    def start(self, startup):
        try:
            self.process.stdin.write(encode_frame(startup))
            self.process.stdin.flush()
            ready = self.reader.frame(60)
        except (KelmoError, OSError) as error:
            self.close()
            raise KelmoError('E_ISOLATION', 'The restricted archive process did not start.') from error
        if ready.get('ready') is not True or ready.get('confined') is not True:
            self.close()
            failure = ready.get('error') if isinstance(ready.get('error'), dict) else {}
            raise KelmoError(str(failure.get('code') or 'E_ISOLATION'),
                            str(failure.get('message') or 'The restricted archive process refused to start.'))

    def call(self, frame):
        data = encode_frame(frame)  # an unencodable frame never reaches the process
        if not self.failed and self.process.poll() is not None:
            self.failed = 'The restricted archive process exited.'
            self.close()
        if self.failed:
            raise KelmoError('E_ARCHIVE_PROCESS', self.failed)
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
            return self.reader.frame(ARCHIVE_TIMEOUT_SECONDS)
        except (KelmoError, OSError) as error:
            self.failed = 'The restricted archive process stopped responding; no unconfined fallback is used.'
            self.close()
            if self.process.returncode == ARCHIVE_IDLE_EXIT:
                self.failed = 'The restricted archive process left idle before reading the frame.'
                self.unread = True
            else:
                self.uncertain = True
            raise KelmoError('E_ARCHIVE_PROCESS', self.failed) from error

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(encode_frame({'method': 'close', 'args': {}}))
                    self.process.stdin.flush()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=5)
                except Exception:  # noqa: BLE001 - a stuck child is terminated, never left running.
                    self.process.kill()
                    self.process.wait()
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            shutil.rmtree(self.scratch, ignore_errors=True)


class ArchiveProcess:
    """ArchiveCoordinatorAPI over the restricted archive process; never opens archive SQLite here.

    A process that exited while idle is found before the next frame is written and started again, under
    the same confinement, so that operation runs on the new process. A process leaves idle on its own after
    ARCHIVE_IDLE_SECONDS; a frame written as it left is unread (ARCHIVE_IDLE_EXIT) and goes once to the new
    process. A process that died otherwise with a frame in flight fails that operation with E_ARCHIVE_PROCESS and it is never resent here, because its effect may
    already have happened; the caller's ledger decides whether it is retried. The process started after
    such a death gets no broker channel, which may hold half a request (embedding_launch)."""
    snapshot_graph = True

    def __init__(self, transport, bindings, description, *, fenced=False, restart=None):
        self._transport = transport
        self._bindings = bindings
        self.description = description
        self.fenced = fenced
        self._restart = restart
        self.legacy_receipt_verifier = None

    def _live(self):
        transport = self._transport
        idle_exit = getattr(transport, 'idle_exit', None)
        if idle_exit is not None:
            idle_exit()
        if getattr(transport, 'failed', None) and self._restart is not None:
            self._transport = (self._restart(uncertain=True) if getattr(transport, 'uncertain', False)
                               else self._restart())
        return self._transport

    def _call(self, method, **args):
        frame = {'method': method, 'args': args}
        transport = self._live()
        try:
            reply = transport.call(frame)
        except KelmoError:
            if not getattr(transport, 'unread', False):
                raise
            reply = self._live().call(frame)  # the old process never read it, so it runs once
        if not isinstance(reply, dict) or set(reply) not in ({'result'}, {'error'}):
            raise KelmoError('E_ARCHIVE_PROCESS', 'The archive process returned an invalid reply.')
        if 'error' in reply:
            error = reply['error']
            raise KelmoError(str(error.get('code', 'E_ARCHIVE_PROCESS')), str(error.get('message', '')))
        return reply['result']

    def _origin(self, origin):
        try:
            self._bindings.validate(origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', str(error)) from error
        return origin.to_dict()

    def capture(self, source, origin, idempotency_key):
        return EvidenceRef.from_dict(self._call('capture', source=source.to_dict(), origin=self._origin(origin),
                                                idempotency_key=idempotency_key))

    def get(self, archive_id, version=None):
        return ArchiveVersion.from_dict(self._call('get', archive_id=archive_id, version=version))

    def update(self, archive_id, expected_version, content, origin, *, receipt=None):
        args = dict(archive_id=archive_id, expected_version=expected_version, content=content.to_dict(),
                    origin=self._origin(origin))
        if receipt is not None:
            args['receipt'] = {'operation_id': receipt[0], 'fingerprint': receipt[1]}
        return EvidenceRef.from_dict(self._call('update', **args))

    def update_receipt(self, receipt, origin):
        value = self._call('update_receipt', receipt={'operation_id': receipt[0], 'fingerprint': receipt[1]},
                           origin=self._origin(origin))
        return None if value is None else EvidenceRef.from_dict(value)

    def history(self, archive_id, cursor, limit):
        return ArchiveHistory.from_dict(self._call('history', archive_id=archive_id, cursor=cursor, limit=limit))

    def expand(self, locator, neighbors):
        if not isinstance(locator, ChunkLocator):
            raise KelmoError('E_INPUT', 'Expand requires an exact chunk locator.')
        return ChunkWindow.from_dict(self._call('expand', locator=locator.to_dict(), neighbors=neighbors))

    def search(self, queries, filters, limit, mode='hybrid'):
        if isinstance(queries, str):
            raise KelmoError('E_INPUT', 'Supply query variants as a sequence.')
        return SearchResult.from_dict(self._call('search', queries=list(queries), filters=filters.to_dict(),
                                                 limit=limit, mode=mode))

    def hide(self, archive_id, origin, idempotency_key, *, reason=None):
        return OperationReceipt.from_dict(self._call('hide', archive_id=archive_id, origin=self._origin(origin),
                                                     idempotency_key=idempotency_key, reason=reason))

    def verify_receipt(self, ref, operation_id, origin):
        return ArchiveVersion.from_dict(self._call('verify_receipt', ref=ref.to_dict(), operation_id=operation_id,
                                                   origin=self._origin(origin)))

    def assert_writer(self):
        self._call('assert_writer')

    def capture_receipt(self, operation_id, origin):
        value = self._call('capture_receipt', operation_id=operation_id, origin=self._origin(origin))
        return None if value is None else EvidenceRef.from_dict(value)

    def receipt_record(self, ref, operation_id, origin):
        return self._call('receipt_record', ref=ref.to_dict(), operation_id=operation_id, origin=self._origin(origin))

    def bind_legacy_receipt(self, ref, operation_id, origin):
        # Ownership is proven against the coordinator journal here, before the archive is asked.
        if self.legacy_receipt_verifier is None or self.legacy_receipt_verifier(ref, operation_id, origin) is not True:
            raise KelmoError('E_RECEIPT', 'Legacy binding requires verified coordinator journal/source ownership.')
        self._call('bind_legacy_receipt', ref=ref.to_dict(), operation_id=operation_id, origin=self._origin(origin))

    def public_record(self, archive_id):
        return self._call('public_record', archive_id=archive_id)

    def full_record(self, archive_id):
        return self._call('full_record', archive_id=archive_id)

    def index_generation(self):
        return self._call('index_generation')

    def list_records(self, limit, offset):
        return self._call('list_records', limit=limit, offset=offset)

    def list_filtered(self, filters, limit, offset, order_by='created_at', order='desc'):
        return self._call('list_filtered', filters=filters.to_dict(), limit=limit, offset=offset,
                          order_by=order_by, order=order)

    def search_records(self, query, filters, limit, mode='hybrid'):
        return self._call('search_records', query=query, filters=filters.to_dict(), limit=limit, mode=mode)

    def counts(self):
        return self._call('counts')

    def stats(self):
        return self._call('stats')

    def hidden_at(self, archive_id):
        return self._call('hidden_at', archive_id=archive_id)

    def refuse_hidden_target(self, archive_id):
        self._call('refuse_hidden_target', archive_id=archive_id)

    def supersede(self, archive_id, superseded_id, origin):
        self._call('supersede', archive_id=archive_id, superseded_id=superseded_id, origin=self._origin(origin))

    def graph_publish(self, snapshot):
        return self._call('graph_publish', snapshot=snapshot)

    def graph(self, operation, generation, params):
        return self._call('graph', operation=operation, generation=generation.to_dict(), params=params)

    def probe(self, paths):
        return self._call('probe', paths=[str(path) for path in paths])

    def close(self):
        self._transport.close()


def import_roots():
    """Package and dependency directories the archive process may import from, read-only."""
    import sysconfig
    import kelmo.store
    import kelmo
    roots = [Path(kelmo.__file__).resolve().parents[1], Path(kelmo.store.__file__).resolve().parents[1]]
    for name in ('purelib', 'platlib'):
        value = sysconfig.get_paths().get(name)
        if value and Path(value).is_dir():
            roots.append(Path(value).resolve())
    return list(dict.fromkeys(roots))


def coordinator_canaries(config):
    """Coordinator data the archive process must not be able to read, each one readable by this
    (unconfined) coordinator, so a refusal inside the child cannot be a missing or mistyped path.

    Includes one loose Git object when the knowledge repository has one. Ancestors of granted paths
    are excluded: a namespace may show them as empty mount points.
    """
    git = config.knowledge_git_root / '.git'
    candidates = [config.state / 'journal.sqlite3', config.state, config.registry, config.wiki,
                  git, git / 'HEAD', config.root / 'kelmo.toml']
    if (git / 'objects').is_dir():
        candidates += sorted(path for path in (git / 'objects').glob('[0-9a-f][0-9a-f]/*') if path.is_file())[:1]
    if config.wiki.is_dir():
        candidates += sorted(config.wiki.glob('*.md'))[:1]
    granted = (config.archive.parent, config.cache)
    return [str(path) for path in dict.fromkeys(candidates)
            if _readable(path) and not any(path == item or path.is_relative_to(item) or item.is_relative_to(path)
                                           for item in granted)]


def archive_paths(config, python, roots, scratch, *, model_roots=(), model_files=()):
    """Grants for the archive process: its own data writable, imports and model read-only."""
    from .runtime import IsolationError, RuntimePaths, system_paths
    protected = [config.root, config.state, config.knowledge_git_root]
    read_only = [Path(path).resolve() for path in roots]
    for path in read_only:
        if any(path == item or path.is_relative_to(item) or item.is_relative_to(path) for item in protected):
            raise IsolationError('An archive import root overlaps vault or coordinator data.')
    # A configured model may live inside the vault (a relative model_dir) but never inside, or above,
    # coordinator data: state, wiki, registry, the knowledge Git directory or the vault configuration.
    coordinator = [config.state, config.wiki, config.registry, config.knowledge_git_root / '.git',
                   config.root / 'kelmo.toml']
    models = [Path(path).resolve() for path in model_roots]
    exact = [Path(path).resolve() for path in model_files]
    for path in (*models, *exact):
        if path == config.root or any(path == item or path.is_relative_to(item) or item.is_relative_to(path)
                                      for item in coordinator):
            raise IsolationError('A configured model path overlaps vault or coordinator data.')
    read_only += models
    read_only += [path.resolve() for path in system_paths(python) if path.exists()]
    if config.cache.is_dir():
        read_only.append(config.cache.resolve())
    executables = [python]
    app = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    if app.is_file():
        executables.append(app.resolve())
    return RuntimePaths(tuple(dict.fromkeys(read_only)), (config.archive.parent.resolve(), scratch),
                        tuple(dict.fromkeys(executables)), tuple(dict.fromkeys(exact)))


def open_archive(config, bindings, mode, *, platform=None, fenced=False):
    """The archive behind a coordinator: confined child process, or explicitly in process.

    The returned proxy starts a dead confined process again on its next operation."""
    from .archive_adapter import LocalArchiveAdapter
    settings = getattr(config, 'embedding', None)
    if mode == 'in-process':
        scratch = Path(tempfile.mkdtemp(prefix='kelmo-archive-'))
        worker = ArchiveWorker(None, bindings, scratch, confined=False, fenced=fenced)
        try:
            worker.adapter = LocalArchiveAdapter(config.archive, config.cache, bindings,
                                                 legacy_receipt_verifier=worker.legacy_verified,
                                                 chunk_index=chunk_index(settings))
        except sqlite3.Error as error:
            shutil.rmtree(scratch, ignore_errors=True)
            raise _unsettled(error) from error
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return ArchiveProcess(_Loopback(worker), bindings, {'mode': 'in-process', 'confined': False},
                              fenced=fenced)
    if mode != 'confined':
        raise KelmoError('E_CONFIG', 'Archive process mode must be confined or in-process.')
    if not config.archive.is_file():
        raise KelmoError('E_STATE', 'Archive database is missing; run kelmo doctor.')

    def start(*, uncertain=False):
        return _spawn_archive(config, bindings, platform=platform, fenced=fenced, settings=settings,
                              poison=uncertain)
    return ArchiveProcess(start(), bindings, {'mode': 'confined', 'confined': True,
                                              'platform': platform or sys.platform},
                          fenced=fenced, restart=start)


def _spawn_archive(config, bindings, *, platform, fenced, settings, poison=False):
    """One confined archive process, started and checked; the launcher seam for the embedding channel is
    `pass_fds` plus the one environment variable naming it. `poison`: the previous process died during a
    call, so the broker channel is withheld (embedding_launch)."""
    from .runtime import IsolationError, confined_spawn, environment
    python = Path(sys.executable).resolve()
    scratch = Path(tempfile.mkdtemp(prefix='kelmo-archive-')).resolve()
    try:
        wire, model_roots, model_files, fds, channel_env = embedding_launch(settings, poison=poison)
        paths = archive_paths(config, python, import_roots(), scratch, model_roots=model_roots,
                              model_files=model_files)
        argv = [str(python), '-I', '-S', '-X', 'utf8=1',
                str(Path(__file__).resolve().with_name('archive_worker.py')), '--confined']
        for root in import_roots():
            argv += ['--import-root', str(root)]
        process = confined_spawn(paths, argv, env=dict(environment(scratch, [python.parent]), **channel_env),
                                 cwd=scratch, platform=platform, pass_fds=fds)
    except IsolationError as error:
        shutil.rmtree(scratch, ignore_errors=True)
        raise KelmoError('E_ISOLATION', str(error)) from error
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    pipe = _Pipe(process, scratch)
    pipe.start({'protocol': ARCHIVE_PROTOCOL, 'enrollment': bindings.describe(),
                'archive': str(config.archive.resolve()), 'cache': str(config.cache),
                'scratch': str(scratch), 'canaries': coordinator_canaries(config),
                'fenced': fenced, 'embedding': wire, 'idle_seconds': ARCHIVE_IDLE_SECONDS})
    return pipe


# --- Shared server: trusted profile and one channel per enrolled client ---

@dataclass(frozen=True)
class ServerProfile:
    """Deployment-owned server enrollment; the launcher selects it, never a request."""
    bindings: OriginBindings
    contexts: object
    projects: dict
    remote_ids: dict
    fenced: bool
    archive_process: str
    enrollment_revision: int = 0

    FIELDS = frozenset({'schema', 'authority_id', 'writer', 'fenced', 'archive_process', 'clients',
                        'repositories', 'actions', 'feature_pages', 'producer_client_ids', 'author_emails',
                        'enrollment_revision'})

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise KelmoError('E_CONFIG', 'The server profile must be an absolute regular file.')
        raw = path.read_bytes()
        if len(raw) > 1048576:
            raise KelmoError('E_CONFIG', 'The server profile exceeds one MiB.')
        try:
            return cls.from_dict(json.loads(raw.decode('utf-8'), parse_constant=_reject_constant))
        except (UnicodeError, ValueError) as error:
            raise KelmoError('E_CONFIG', 'The server profile is not valid JSON.') from error

    @classmethod
    def from_dict(cls, value):
        from .context_binding import ActionBinding, ContextBindings, canonical_remote
        try:
            if not isinstance(value, dict) or set(value) - cls.FIELDS or value.get('schema') != PROFILE_SCHEMA:
                raise ValueError('Unsupported server profile fields or schema.')
            authority = value['authority_id']
            writer = value['writer']
            if set(writer) != {'writer_id', 'epoch'}:
                raise ValueError('Writer requires writer_id and epoch.')
            mode = value.get('archive_process', 'confined')
            fenced = value.get('fenced', False)
            revision = value.get('enrollment_revision', 0)
            if type(revision) is not int or revision < 0:
                raise ValueError('Enrollment revision must be a nonnegative integer.')
            if mode not in ('confined', 'in-process') or type(fenced) is not bool:
                raise ValueError('Invalid archive process mode or fence.')
            repositories, remotes, projects = [], {}, {}
            for item in value.get('repositories', []):
                if set(item) - {'repository_id', 'root', 'remote', 'project'} or not {'repository_id', 'root', 'remote'} <= set(item):
                    raise ValueError('Repository requires id, root and remote.')
                repositories.append(RepositoryBinding(item['repository_id'], item['root']))
                remotes[item['repository_id']] = item['remote']
                if item.get('project') is not None:
                    _text(item['project'])
                    projects[item['repository_id']] = item['project']
            enrollments = []
            for item in value['clients']:
                if set(item) - {'client_id', 'machine_tag', 'requester', 'repository_ids'}:
                    raise ValueError('Unknown client enrollment fields.')
                if not isinstance(item.get('client_id'), str) or not CLIENT_ID.fullmatch(item['client_id']):
                    raise ValueError('Client IDs use letters, digits, underscores and hyphens.')
                enrollments.append(Enrollment(item['client_id'], item['machine_tag'], item.get('requester', False),
                                              tuple(item.get('repository_ids', ()))))
            bindings = OriginBindings(authority, WriterIdentity(authority, writer['writer_id'], writer['epoch']),
                                      tuple(enrollments), tuple(repositories))
            contexts = ContextBindings(
                bindings, actions=tuple(ActionBinding(**item) for item in value.get('actions', [])),
                repository_remotes=remotes,
                worktrees={item.root: item.repository_id for item in repositories},
                feature_pages={key: tuple(paths) for key, paths in value.get('feature_pages', {}).items()},
                producer_client_ids=tuple(value.get('producer_client_ids', ())),
                author_emails=tuple(value.get('author_emails', ())))
            remote_ids = {canonical_remote(url): repository for repository, url in remotes.items()}
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise KelmoError('E_CONFIG', 'Invalid server profile: ' + str(error)) from error
        return cls(bindings, contexts, projects, remote_ids, fenced, mode, revision)


def shared_service(root, profile, client_id=None):
    """A Service for shared background work, built from an explicit server profile only.

    It uses the profile's bindings, its writer and the same restricted archive factory a client channel
    uses (open_archive), never a local identity or an unconfined archive. Without client_id it is
    background work with no capture origin (a worker or daemon); with one, captures belong to that
    enrolled client (the note of a session that client captured). A fenced profile performs no
    background writes."""
    from .config import Config
    from .service import Service
    if not isinstance(profile, ServerProfile):
        raise KelmoError('E_CONFIG', 'Shared background work requires a trusted server profile.')
    if profile.fenced:
        raise KelmoError('E_FENCED', 'This server profile is fenced; background writes wait for the cutover.')
    config = Config.load(root)
    if config.vault_id != profile.bindings.authority_id:
        raise KelmoError('E_ORIGIN', 'The server profile belongs to another vault authority.')
    origin = None
    if client_id is not None:
        try:
            origin = profile.bindings.origin(client_id)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'Shared background work names a client that is not enrolled.') from error
    archive = open_archive(config, profile.bindings, profile.archive_process, fenced=False)
    try:
        service = Service(root, archive_api=archive, bindings=profile.bindings, origin=origin,
                          background=origin is None, context_bindings=profile.contexts)
    except BaseException:
        archive.close()
        raise
    archive.legacy_receipt_verifier = service._legacy_receipt_allowed
    return service


def session_client(sessions, session_key):
    """Read server-recorded capture provenance; native session text never grants a client identity."""
    row = sessions.journal.conn.execute(
        'SELECT client_id FROM session_capture_origins WHERE session_key=?', (session_key,)).fetchone()
    if row is None:
        raise KelmoError('E_ORIGIN', 'This session has no recorded capturing client; under a server profile '
                                    'it has no origin to archive its note with.')
    return row['client_id']


OPERATIONS = {
    # name: (mutating, {argument: (types, required)})
    'hello': (False, {}),
    'status': (False, {}),
    'search': (False, {'query': ((str,), True), 'limit': ((int,), False), 'project': ((str, type(None)), False),
                       'tags': ((list, type(None)), False), 'since': ((str, type(None)), False),
                       'until': ((str, type(None)), False)}),
    'list': (False, {'limit': ((int,), False), 'offset': ((int,), False)}),
    'show': (False, {'memory_id': ((str,), True)}),
    'trace': (False, {'operation_id': ((str,), True)}),
    'reconcile': (False, {'text': ((str,), True), 'target': ((str,), True)}),
    'pages': (False, {'projects': ((list, type(None)), False)}),
    'page': (False, {'target': ((str,), True)}),
    'editor_context': (False, {'project': ((str,), True), 'budget_tokens': ((int,), True),
                              'selected': ((list,), False)}),
    'context': (False, {'repository': ((str, type(None)), False), 'work': ((str, type(None)), False),
                        'work_project': ((str, type(None)), False), 'branch': ((str, type(None)), False),
                        'binding_status': ((str, type(None)), False), 'repositories': ((list,), False)}),
    'sessions_inspect': (False, {}),
    'doctor': (False, {}),
    'graph_query': (False, {'params': ((dict,), True)}),
    'graph_stats': (False, {}),
    'graph_read': (False, {'entity_id': ((str,), True)}),
    'graph_rebuild': (False, {}),
    'compat': (None, {'tool': ((str,), True), 'arguments': ((dict,), True)}),
    'capture': (True, {'content': ((str,), True), 'filename': ((str,), True), 'role': ((str,), False),
                       'promote': ((bool,), False), 'supersedes': ((str, type(None)), False)}),
    'create': (True, {'content': ((str,), True), 'filename': ((str,), True), 'project': ((str,), True),
                      'selected': ((list,), False), 'rule_change': ((bool,), False),
                      'budget_tokens': ((int,), False), 'new_work': ((dict,), False),
                      'pending': ((str,), False)}),
    # Compatibility for older clients that explicitly requested processing.
    'process': (True, {'capture_key': ((str,), True)}),
    'forget': (True, {'memory_id': ((str,), True), 'reason': ((str, type(None)), False)}),
    # A whole transcript in one frame, as clients before `session_append` send it.
    'session_capture': (True, {'provider': ((str,), True), 'native_id': ((str,), True), 'event': ((str,), True),
                               'content': ((str,), True), 'sha256': ((str,), True), 'size': ((int,), True),
                               'file_identity': ((str,), True), 'client_generation': ((int,), True)}),
    # One piece of a transcript: `size` bytes at `offset`, after bytes with digest `base_sha256`; `sha256`
    # is the digest of the transcript through this piece, and the final piece of an event runs the intake.
    'session_append': (True, {'provider': ((str,), True), 'native_id': ((str,), True), 'event': ((str,), True),
                              'file_identity': ((str,), True), 'client_generation': ((int,), True),
                              'offset': ((int,), True), 'base_sha256': ((str,), True), 'content': ((str,), True),
                              'size': ((int,), True), 'sha256': ((str,), True), 'final': ((bool,), True)}),
    'state_apply': (True, {'update': ((dict,), True)}),
    # `kelmo register`: the entries are previewed by a read and published by a write.
    'register_preview': (False, {'targets': ((list,), True), 'kind': ((str,), False), 'summary': ((str,), False)}),
    'register': (True, {'targets': ((list,), True), 'kind': ((str,), False), 'summary': ((str,), False)}),
    # `queue retry`: requeue a failed operation this client captured, as the local repair does.
    'retry': (True, {'operation_id': ((str,), True)}),
}


def _request_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_request(request):
    """Strict request schema: a request names an operation and its bounded arguments, nothing else."""
    if not isinstance(request, dict) or set(request) - {'protocol', 'request_id', 'op', 'args', 'idempotency_key'}:
        raise KelmoError('E_PROTOCOL', 'Requests carry only protocol, request_id, op, args and idempotency_key.')
    if request.get('protocol') != PROTOCOL:
        raise KelmoError('E_PROTOCOL', 'Unsupported client protocol version.')
    if not isinstance(request.get('request_id'), str) or not REQUEST_ID.fullmatch(request['request_id']):
        raise KelmoError('E_PROTOCOL', 'Request ID must be a short opaque token.')
    op = request.get('op')
    if op not in OPERATIONS:
        raise KelmoError('E_PROTOCOL', 'Unknown shared operation.')
    args = request.get('args', {})
    _, spec = OPERATIONS[op]
    if not isinstance(args, dict) or set(args) - set(spec):
        raise KelmoError('E_PROTOCOL', 'Operation arguments are outside its contract; identity, paths, '
                                      'providers and writer state are never request fields.')
    for name, (types, required) in spec.items():
        if name not in args:
            if required:
                raise KelmoError('E_PROTOCOL', 'Missing required argument ' + name + '.')
            continue
        value = args[name]
        if type(value) not in types:
            raise KelmoError('E_PROTOCOL', 'Argument ' + name + ' has an unsupported type.')
    key = request.get('idempotency_key')
    if key is not None and (not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key)):
        raise KelmoError('E_PROTOCOL', 'Idempotency key must be 1..200 printable ASCII characters.')
    return op, args, key


def _enrollment_digest(profile):
    description = profile.bindings.describe()
    identity = {'clients': description['enrollments'], 'repositories': description['repositories'],
                'projects': profile.projects, 'remotes': profile.remote_ids}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ClientSession:
    """One enrolled client channel over the same Service the local CLI uses."""

    def __init__(self, root, profile, client_id, *, archive=None, profile_path=None):
        from .service import Service
        if not isinstance(profile, ServerProfile):
            raise KelmoError('E_CONFIG', 'A shared session requires a trusted server profile.')
        if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
            raise KelmoError('E_ORIGIN', 'The launcher named an invalid client ID.')
        try:
            self.origin = profile.bindings.origin(client_id)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'The launcher named a client that is not enrolled.') from error
        self.profile = profile
        self._context_revision = profile.enrollment_revision
        self._context_enrollment = _enrollment_digest(profile)
        self.profile_path = profile_path
        self.client_id = client_id
        from .config import Config
        config = Config.load(root)
        if config.vault_id != profile.bindings.authority_id:
            raise KelmoError('E_ORIGIN', 'The server profile belongs to another vault authority.')
        self.archive = archive if archive is not None else open_archive(config, profile.bindings,
                                                                        profile.archive_process,
                                                                        fenced=profile.fenced)
        try:
            self.service = Service(root, archive_api=self.archive, bindings=profile.bindings, origin=self.origin,
                                   context_bindings=profile.contexts)
        except BaseException:
            self.archive.close()
            raise
        if isinstance(self.archive, ArchiveProcess) and profile.fenced:
            self.archive.fenced = True
        self.archive.legacy_receipt_verifier = self.service._legacy_receipt_allowed

    def close(self):
        self.service.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _fenced(self):
        """The fence as the profile file says now, so a fence set for a backup also holds a channel that
        was opened before it; an unreadable profile writes nothing."""
        if self.profile_path is None:
            return self.profile.fenced
        try:
            return ServerProfile.load(self.profile_path).fenced
        except KelmoError:
            return True

    def handle(self, request):
        request_id = request.get('request_id') if isinstance(request, dict) else None
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            request_id = None
        try:
            op, args, key = validate_request(request)
            outcome = self._dispatch(op, args, key)
        except KelmoError as error:
            outcome = {'ok': False, 'error': {'code': error.code, 'message': str(error),
                                              'retryable': _retryable(error)}}
        except ValueError as error:
            outcome = {'ok': False, 'error': {'code': 'E_INPUT', 'message': str(error), 'retryable': False}}
        except sqlite3.Error as error:
            busy = _busy(error)
            code = 'E_BUSY' if busy else 'E_STATE'
            outcome = {'ok': False, 'error': {'code': code, 'message': 'Server database operation failed.',
                                              'retryable': busy}}
        except OSError:
            outcome = {'ok': False, 'error': {'code': 'E_IO', 'message': 'Server storage could not be accessed.',
                                              'retryable': True}}
        except Exception:  # noqa: BLE001 - one faulty request must not end the channel or loop a retry.
            outcome = {'ok': False, 'error': {'code': 'E_INTERNAL', 'message': 'The server could not process '
                                              'this request.', 'retryable': False}}
        return dict(outcome, protocol=PROTOCOL, request_id=request_id)

    def _dispatch(self, op, args, key):
        mutating, _ = OPERATIONS[op]
        if op == 'compat':
            from .mcp import COMPAT_WRITES
            mutating = args['tool'] in COMPAT_WRITES
        if not mutating:
            return {'ok': True, 'result': getattr(self, 'read_' + op)(**args)}
        if key is None:
            raise KelmoError('E_PROTOCOL', 'A mutating request requires an idempotency key.')
        return self._mutate(op, args, key)

    def _mutate(self, op, args, key):
        """Run one write at most once per scoped key.

        The ledger row is written before the effect and settled after it. While the effect runs this
        process holds the row's exclusive lock file; the OS releases it if the process dies, and only then
        can another attempt take the row over. There is no time-based expiry, so a slow attempt that is
        still running is answered E_BUSY and never executed twice. A taken-over row runs the effect again
        under the same scoped key, which the archive, journal and update receipts already deduplicate."""
        journal = self.service.journal
        request_key = _request_digest([self.client_id, key])
        fingerprint = _request_digest({'op': op, 'args': args})
        try:
            stored = journal.client_request(request_key)
            if stored is not None:
                if stored['fingerprint'] != fingerprint:
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different request.')
                if stored['state'] == 'finished':
                    return dict(json.loads(stored['reply']), replayed=True)
            if self._fenced():
                raise KelmoError('E_FENCED', 'This server is fenced; writes stay queued until the cutover completes.')
            self.service.archive_api.assert_writer()
            with self._request_lock(request_key) as release:
                row, claimed = _ledger(journal.client_request_claim, request_key, self.client_id, op, fingerprint)
                if claimed is None:
                    return dict(json.loads(row['reply']), replayed=True)
                try:
                    outcome = {'ok': True, 'result': getattr(self, 'write_' + op)(key=key, **args)}
                except KelmoError as error:
                    if _retryable(error):
                        raise
                    outcome = {'ok': False, 'error': {'code': error.code, 'message': str(error), 'retryable': False}}
                try:
                    outcome = json.loads(encode_frame(outcome))
                except KelmoError as error:
                    # The effect is done but its reply cannot be stored; the row stays open for the key.
                    raise _verdict(error, True) from error
                _ledger(journal.client_request_finish, request_key, outcome)
                release()
                return outcome
        except KelmoError:
            raise
        except Exception as error:  # noqa: BLE001 - classified here, never swallowed.
            # A ledger read, claim, effect, encoding or settlement failed. The effect may already have
            # committed and the row stays open (or was never written); only a retry under the same key
            # can settle it, so the refusal is retryable rather than a final answer.
            raise _verdict(KelmoError(_unsettled(error).code, 'The write did not settle; it is retried under '
                                                             'the same key.'), True) from error

    @contextmanager
    def _request_lock(self, request_key):
        """The exclusive lock of one ledger row, refused at once (E_BUSY, retryable) while another live
        attempt holds it. The file is removed after the row is settled: a later opener of the old or a new
        file then finds the finished row and replays it."""
        import fcntl
        directory = self.service.config.internal('.state/client-requests')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.service.config.internal('.state/client-requests/' + request_key + '.lock')
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        settled = []
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'Another attempt of this request is still running; it stays '
                                          'queued.') from error
            yield lambda: settled.append(True)
            if settled:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            os.close(descriptor)

    def _scoped(self, key):
        return 'client/' + self.client_id + '/' + key

    # Reads

    def read_hello(self):
        return {'protocol': PROTOCOL, 'authority_id': self.profile.bindings.authority_id,
                'writer_id': self.profile.bindings.writer.writer_id,
                'writer_epoch': self.profile.bindings.writer.epoch,
                'client_id': self.client_id, 'machine_tag': self.origin.machine_tag,
                'fenced': self.profile.fenced, 'archive': self.archive.description,
                'operations': sorted(OPERATIONS)}

    def read_status(self):
        writer = self.profile.bindings.writer
        # Operation metadata belongs to the origin that captured it.
        return dict(self.service.status(), failed_operations=self.service.failed_operations(origin=self.origin),
                    writer='shared', remote_configured=True,
                    writer_id=writer.writer_id, writer_epoch=writer.epoch, fenced=self.profile.fenced,
                    archive_process=self.archive.description, client_id=self.client_id)

    def read_search(self, query, limit=10, project=None, tags=None, since=None, until=None):
        return self.service.search(query, limit, with_metadata=True, project=project, tags=tags,
                                   since=since, until=until, client=self.client_id)

    def read_list(self, limit=20, offset=0):
        return self.service.list(limit, offset)

    def read_show(self, memory_id):
        return self.service.show(memory_id)

    def read_trace(self, operation_id):
        # Operation metadata belongs to the origin that captured it.
        if self.service.journal.origin_record(operation_id) != self.origin.to_dict():
            raise KelmoError('E_NOT_FOUND', 'Operation not found.')
        return self._operation_result(self.service.trace(operation_id))

    def read_reconcile(self, text, target):
        return self.service.reconcile(text, target)

    def read_pages(self, projects=None):
        return self.service.page_index(projects)

    def read_page(self, target):
        return self.service.page_facts(target)

    def read_editor_context(self, project, budget_tokens, selected=()):
        return self.service.editor_context(project, budget_tokens, selected)

    def read_context(self, repository=None, work=None, work_project=None, branch=None, binding_status=None,
                     repositories=None):
        if self.profile_path is None:
            profile, origin, before = self.profile, self.origin, None
        else:
            before = Path(self.profile_path).read_bytes()
            profile = ServerProfile.from_dict(json.loads(before))
            if (profile.bindings.authority_id != self.profile.bindings.authority_id
                    or profile.enrollment_revision < self._context_revision):
                raise KelmoError('E_CONFIG', 'Server enrollment authority or revision moved backwards.')
            if (profile.enrollment_revision == self._context_revision
                    and _enrollment_digest(profile) != self._context_enrollment):
                raise KelmoError('E_CONFIG', 'Server enrollment changed without incrementing its revision.')
            try:
                origin = profile.bindings.origin(self.client_id)
            except ValueError as error:
                raise KelmoError('E_ORIGIN', 'Client enrollment was removed.') from error
        result = shared_context(self.service, profile, origin, repository=repository, work=work,
                                work_project=work_project, client_binding=binding_status, repositories=repositories)
        if before is not None and Path(self.profile_path).read_bytes() != before:
            raise KelmoError('E_CONFLICT', 'Server enrollment changed during HOT delivery; retry.')
        self._context_revision = profile.enrollment_revision
        self._context_enrollment = _enrollment_digest(profile)
        return result

    def read_sessions_inspect(self):
        from .session_control import inspect
        result = inspect(self.service.root)
        keys = {row['session_key'] for row in self.service.journal.conn.execute(
            "SELECT session_key FROM sessions WHERE native_id LIKE ? ESCAPE '\\'",
            (self.client_id.replace('_', '\\_') + ':%',))} if self.service.journal.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() else set()
        return dict(result, jobs=[job for job in result['jobs'] if job['session_key'] in keys])

    def read_doctor(self):
        return self.service.doctor()

    def read_graph_query(self, params):
        return self.service.graph_query(**params)

    def read_graph_stats(self):
        return self.service.graph_stats()

    def read_graph_read(self, entity_id):
        return self.service.graph_read(entity_id)

    def read_graph_rebuild(self):
        # A derived-index write: refused by a fenced profile even when the archive was injected.
        if self._fenced():
            raise KelmoError('E_FENCED', 'This server is fenced; the derived graph is not rebuilt.')
        return self.service.graph_rebuild()

    def read_compat(self, tool, arguments):
        from .mcp import compat
        return compat(self.service, tool, arguments)

    def read_register_preview(self, targets, kind='topic', summary=''):
        return self.service.register(targets, kind=kind, summary=summary)

    # Writes; every one passes the ledger, the fence and the archive writer check first.

    def write_compat(self, key, tool, arguments):
        from .mcp import compat
        return compat(self.service, tool, arguments, idempotency_key=self._scoped(key))

    def write_capture(self, key, content, filename, role='unknown', promote=False, supersedes=None):
        # Capture only, as a local `add` without --wait: queued work stays queued for a worker.
        operation = self.service.capture(content, filename, role, 'promote' if promote else 'archive',
                                         self._scoped(key), supersedes=supersedes)
        return self._operation_result({'operation': operation})

    def write_create(self, key, content, filename, project, selected=(), rule_change=False,
                     budget_tokens=None, new_work=None, pending=None):
        operation = self.service.create(content, filename, project=project, selected=selected,
                                        rule_change=rule_change, budget_tokens=budget_tokens,
                                        idempotency_key=self._scoped(key), new_work=new_work, pending=pending)
        return self._operation_result({'operation': operation})

    def write_process(self, key, capture_key):
        """The --wait step of a local add, for the operation this client captured under capture_key."""
        from .wiki import engine
        if not IDEMPOTENCY_KEY.fullmatch(capture_key):
            raise KelmoError('E_INPUT', 'The capture key is not a valid idempotency key.')
        # The same request-key derivation Service.capture applies to a provided key, in this client's scope.
        operation_id = self.service.journal.operation_for_request_key(
            engine.digest('provided:' + self._scoped(capture_key)))
        if operation_id is None or self.service.journal.origin_record(operation_id) != self.origin.to_dict():
            raise KelmoError('E_NOT_FOUND', 'This client has no capture under that key.')
        from .daemon import TRANSIENT_FAILURES, resume_transient
        try:
            operation = self.service.run(operation_id)
        except KelmoError as error:
            if error.code not in ('E_BUSY', 'E_CLAIM'):
                raise
            return self._operation_result({'operation': self.service.journal.get(operation_id),
                                           'processing_deferred': True})
        # Backoff E_BUSY must leave this request open, unlike contention with an active run above.
        # A charged retry can fail again: keep it pending until the shared budget is spent.
        while operation['state'] == 'failed' and operation['error_code'] in TRANSIENT_FAILURES:
            operation = resume_transient(self.service, operation)
        result = {'operation': operation}
        if operation['state'] == 'captured':
            result.update(processing_scheduled=False,
                          next_step='Captured evidence is not scheduled; an authorized writer must submit it '
                                    'for archival or checked promotion.')
        return self._operation_result(result)

    def write_retry(self, key, operation_id):
        """The repair a failed operation's envelope names, for an operation this client's origin captured.
        Another origin's operation is not found, exactly as trace answers it."""
        if self.service.journal.origin_record(operation_id) != self.origin.to_dict():
            raise KelmoError('E_NOT_FOUND', 'Operation not found.')
        return self._operation_result({'operation': self.service.retry(operation_id)})

    def _operation_result(self, result):
        # The exact archive receipt, when the operation already has one.
        evidence = self.service.journal.evidence_record(result['operation']['id'])
        if evidence is not None:
            result['evidence_ref'] = evidence
        return result

    def write_register(self, key, targets, kind='topic', summary=''):
        # Registration is idempotent by itself: a registered target is reported, never registered twice.
        return self.service.register(targets, kind=kind, summary=summary, apply=True)

    def write_forget(self, key, memory_id, reason=None):
        return self.service._forget(memory_id, self.origin, reason=reason, idempotency_key=self._scoped(key))

    def write_state_apply(self, key, update):
        from .contracts import ActionUpdate
        from .state import ActionState
        try:
            action = ActionUpdate.from_dict(update)
        except ValueError as error:
            raise KelmoError('E_INPUT', 'Invalid action update.') from error
        try:
            return ActionState(self.service, self.profile.contexts).apply(action, self.origin, key).to_dict()
        except KelmoError as error:
            raise _verdict(error, self._action_retryable(error, key))

    def _action_retryable(self, error, key):
        """Whether a refused state action is retried: decided by the coordinator's own record of it.

        DirtyKnowledge (uncommitted knowledge edits) is an environment condition that never settles a
        request, so it is retried even though its code is E_CONFLICT. Otherwise the action request row
        decides: `refused` is a settled refusal the coordinator replays (an expected-revision conflict
        among them), `open` or `prepared` is unsettled work that the same key resumes. With no row the
        request never reached admission, and the fallback list classifies the environment failure."""
        from .publication import StateCoordinator
        from .wiki.engine import DirtyKnowledge
        if isinstance(error, DirtyKnowledge):
            return True
        try:
            row = self.service.journal.action_request(StateCoordinator._request_key('apply', self.origin, key))
        except KelmoError:
            row = None
        if row is not None and row['state'] == 'refused':
            return False
        if row is not None and row['state'] in ('open', 'prepared'):
            return True
        return error.code in RETRYABLE

    def write_session_capture(self, key, provider, native_id, event, content, sha256, size,
                              file_identity, client_generation):
        return session_capture(self.service, self.client_id, provider=provider, native_id=native_id,
                               event=event, content=content, sha256=sha256, size=size,
                               file_identity=file_identity, client_generation=client_generation,
                               offset=0, base_sha256=EMPTY_SHA256, final=True)

    def write_session_append(self, key, **piece):
        return session_capture(self.service, self.client_id, **piece)


def _spool_states(descriptor, offset, length):
    """The digest states of the spool's first `offset` bytes and of all `length` of them."""
    state, at, base = hashlib.sha256(), 0, None
    for end in (offset, length):
        while at < end:
            block = os.pread(descriptor, min(SPOOL_READ_BYTES, end - at), at)
            if not block:
                raise KelmoError('E_CAPTURE_RETRY', 'The server spool changed while it was read; retry.')
            state.update(block)
            at += len(block)
        base = base or state.copy()
    return base, state


def _extend_spool(descriptor, offset, base_sha256, piece, sha256):
    """Make the spool hold exactly the client transcript through this piece, or refuse.

    Offset 0 starts the spool again. A later piece must follow exactly the bytes the spool holds; a spool
    that already ends with this piece is a retried request and is accepted without a write."""
    length = os.fstat(descriptor).st_size
    state = hashlib.sha256()
    if offset:
        if length in (offset, offset + len(piece)):
            state, whole = _spool_states(descriptor, offset, length)
            if length == offset + len(piece) and whole.hexdigest() == sha256:
                return
        if length != offset or state.hexdigest() != base_sha256:
            raise KelmoError('E_CAPTURE_BASE', 'The server holds a different part of this transcript than the '
                                              'piece follows; the client sends the transcript again from its start.')
    state.update(piece)
    if state.hexdigest() != sha256:
        raise KelmoError('E_SOURCE', 'Transcript bytes differ from their declared hash.')
    os.ftruncate(descriptor, offset)
    view, at = memoryview(piece), offset
    while view:
        written = os.pwrite(descriptor, view, at)
        view, at = view[written:], at + written
    os.fsync(descriptor)
    back, at = [], offset
    while at < offset + len(piece):
        block = os.pread(descriptor, min(SPOOL_READ_BYTES, offset + len(piece) - at), at)
        if not block:
            break
        back.append(block)
        at += len(block)
    if b''.join(back) != piece or os.fstat(descriptor).st_size != offset + len(piece):
        os.ftruncate(descriptor, offset)
        os.fsync(descriptor)
        raise KelmoError('E_CAPTURE_RETRY', 'The server spool differs from the received transcript; retry.')


def session_capture(service, client_id, *, provider, native_id, event, content, sha256, size,
                    file_identity, client_generation, offset, base_sha256, final):
    """Extend a server-private spool by one piece of a client transcript; the final piece of a lifecycle
    event runs the existing session intake on the whole spool.

    A piece names the bytes it follows (`offset`, `base_sha256`), so a lost, reordered or foreign piece
    is refused (E_CAPTURE_BASE) and the client sends the transcript again from offset 0, which rewrites
    the spool. The server never opens a client path. A different client file identity is a different
    spool file, so the intake starts a new generation exactly as it would locally. The intake has no
    time budget here: no hook waits for it, and the request ledger replays its settled answer.
    """
    from .journal import now
    from .sessions import Sessions
    from .wiki import engine
    config = service.config
    settings = config.sessions
    if provider not in ('claude', 'codex') or event not in ('Stop', 'PreCompact', 'SessionEnd'):
        raise KelmoError('E_INPUT', 'Unsupported provider or lifecycle event.')
    if not NATIVE_ID.fullmatch(native_id) or not isinstance(file_identity, str) or not 1 <= len(file_identity) <= 200:
        raise KelmoError('E_INPUT', 'Invalid session or file identity.')
    if type(client_generation) is not int or client_generation < 1:
        raise KelmoError('E_INPUT', 'Client generation must be a positive integer.')
    if (type(offset) is not int or offset < 0 or not isinstance(base_sha256, str) or not isinstance(sha256, str)
            or not SHA256_HEX.fullmatch(base_sha256) or not SHA256_HEX.fullmatch(sha256)
            or (offset == 0 and base_sha256 != EMPTY_SHA256) or type(final) is not bool):
        raise KelmoError('E_INPUT', 'Invalid transcript piece position or digest.')
    try:
        piece = base64.b64decode(content, validate=True)
    except ValueError as error:
        raise KelmoError('E_INPUT', 'Transcript bytes must be base64.') from error
    if len(piece) != size:
        raise KelmoError('E_SOURCE', 'Transcript bytes differ from their declared size.')
    if settings['mode'] == 'off' or settings['paused']:
        return {'captured': False, 'appended': False, 'reason': 'session memory is off or paused on the server'}
    import fcntl
    directory = config.internal('.state/client-sessions/' + engine.digest(json.dumps([client_id, provider, native_id])))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = engine.digest(file_identity)
    spool = config.internal('.state/client-sessions/' + directory.name + '/' + name + '.jsonl')
    # One writer per spool from the write until the intake has read it. The spool is extended and rewritten
    # in place, never replaced: the intake names a transcript generation by the file's inode, so a new
    # inode per capture would start a new generation every time. The lock keeps any reader off a partial
    # file, and each written piece is read back from disk before the intake reads the spool.
    with config.internal('.state/client-sessions/' + directory.name + '/' + name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        descriptor = os.open(spool, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _extend_spool(descriptor, offset, base_sha256, piece, sha256)
        finally:
            os.close(descriptor)
        engine.sync_directory(directory)
        if not final:
            return {'captured': False, 'appended': True, 'client_generation': client_generation,
                    'range': [0, offset + size], 'sha256': sha256}
        sessions = Sessions(config)
        try:
            result = sessions.capture(provider, client_id + ':' + native_id, spool, directory, event,
                                      checkpoint_turns=settings['checkpoint_turns'], time_budget=None,
                                      capture_client_id=client_id)
            with sessions.journal.conn as db:
                db.execute('''CREATE TABLE IF NOT EXISTS session_native_events (
                    provider TEXT NOT NULL,event TEXT NOT NULL,received_at TEXT NOT NULL,PRIMARY KEY(provider,event))''')
                db.execute('INSERT OR REPLACE INTO session_native_events VALUES (?,?,?)', (provider, event, now()))
        finally:
            sessions.close()
    return dict(result, captured=True, appended=True, client_generation=client_generation,
                range=[0, offset + size], sha256=sha256)


def _select_shared_repository(profile, origin, repository, repositories, *, project_map=None):
    values = repositories if repositories is not None else ([repository] if repository is not None else [])
    if (not isinstance(values, list) or len(values) > 100
            or any(not isinstance(value, str) or not value or len(value) > 2048 for value in values)):
        raise KelmoError('E_INPUT', 'Repository identities must be a list of canonical remotes.')
    if repository is not None and repository not in values:
        values = [*values, repository]
    matched = {profile.remote_ids[value] for value in values if value in profile.remote_ids}
    for item in matched:
        profile.contexts.require_repository(origin, item)
    selected_projects = profile.projects if project_map is None else project_map
    projects = {selected_projects[item] for item in matched if selected_projects.get(item) is not None}
    if len(projects) > 1:
        raise KelmoError('E_CONFLICT', 'Repository remotes map to multiple projects: ' + ', '.join(sorted(projects)))
    preferred = profile.remote_ids.get(repository)
    mapped = {item for item in matched if item in profile.projects}
    choices = mapped or matched
    return preferred if preferred in choices else next(iter(sorted(choices)), None)


def shared_context(service, profile, origin, *, repository=None, work=None, work_project=None,
                   client_binding=None, repositories=None):
    """Context for an enrolled repository from the committed generation; never from client paths."""
    from .hot import _render_generation, adopted
    from .config import Config
    config = Config.load(service.root)
    hot = config.hot
    if hot is None:
        return {'configured': False, 'project': None, 'pages': [], 'context': '', 'bytes': 0,
                'binding_status': 'unconfigured', 'client_binding': client_binding}
    if not adopted(config):
        raise KelmoError('E_CAPABILITY', 'Shared context delivery reads the committed registry; '
                                        'this vault has none yet (run kelmo init).')
    from .projects import canonical_project, resolve_bound_work, project_aliases
    reader = service.knowledge
    generation = reader.generation()
    registry = reader._pinned(generation)['registry']
    effective_projects = {key: canonical_project(registry, value) or value
                          for key, value in profile.projects.items()}
    project, status, repository_id = None, 'global', None
    if repository is not None or repositories:
        repository_id = _select_shared_repository(profile, origin, repository, repositories, project_map=effective_projects)
        if repository_id is None:
            status = 'unenrolled-repository'
        else:
            project = effective_projects.get(repository_id)
            status = 'repository' if project else 'repository-without-project'
            if project is not None and project not in hot['projects']:
                raise KelmoError('E_CONFIG', 'The repository maps to a HOT project the server does not configure.')
    paths = list(hot['pages']) + (list(hot['projects'][project]['pages']) if project else [])
    work_status = None
    if work is not None:
        if not isinstance(work, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*', work):
            work_status = 'invalid-work'
        elif project is None:
            work_status = 'stale-project'
        else:
            page = resolve_bound_work(registry, project, work_project, work)
            if page is not None and page in reader._pinned(generation)['paths']:
                paths.append(page)
                work_status = 'bound'
            else:
                work_status = 'stale-project' if work_project not in (None, project) else 'unknown-work'
    # Hub delivery has its own whole-bundle budget; the legacy context is retained for older clients.
    budget = max(hot['max_bytes'], 16 * 1048576) if hot.get('views') else hot['max_bytes']
    result = _render_generation(config, project, paths, budget, reader, generation=generation)
    from .creation import pending_summaries
    enrolled = profile.bindings.validate(origin)
    projects = {effective_projects[r] for r in enrolled.repository_ids if effective_projects.get(r)}
    result.update(binding_status=status, repository_id=repository_id, work=work, work_status=work_status,
                  client_binding=client_binding, pending=[item for item in pending_summaries(service, project, registry=registry)
                                                        if item.get('project') in projects],
                  context_sha256=hashlib.sha256(result['context'].encode('utf-8')).hexdigest())
    from .hot import has_hub_generation
    if hot.get('views') and has_hub_generation(config, reader, generation):
        from .hub_delivery import published_set
        from .creation import pending_operations
        result['hub_set'] = published_set(reader, generation, projects, profile.enrollment_revision)
        result['project_aliases'] = {old: new for old, new in project_aliases(registry).items() if new in projects}
        result['hub_work'] = (_published_work(reader, generation, page) if work_status == 'bound' else '')
        result['hub_work_path'] = page if work_status == 'bound' else None
        result['pending'] = [{'operation_id': operation['id'], 'project': request.get('project'),
                              'state': operation['state'], 'pending': request['pending']}
                             for operation, request in pending_operations(service, project, include_unarchived=True, registry=registry)
                             if request.get('pending') and request.get('project') in projects]
    if reader.generation() != generation:
        raise KelmoError('E_GENERATION', 'Knowledge changed while context was assembled; retry the next prompt.')
    return result


def _published_work(reader, generation, page):
    return reader.page(page, generation).content.decode('utf-8')


def serve_client(root, profile_path, client_id, stdin, stdout):
    """One channel: the trusted launcher supplies profile and client; stdout carries frames only."""
    try:
        try:
            session = ClientSession(root, ServerProfile.load(profile_path), client_id, profile_path=profile_path)
        except (sqlite3.Error, OSError) as error:
            raise _unsettled(error) from error
    except KelmoError as error:
        write_frame(stdout, {'protocol': PROTOCOL, 'request_id': None, 'ok': False,
                             'error': {'code': error.code, 'message': str(error),
                                       'retryable': _retryable(error)}})
        return 1
    with session:
        while True:
            try:
                request = read_frame(stdin)
            except KelmoError as error:
                write_frame(stdout, {'protocol': PROTOCOL, 'request_id': None, 'ok': False,
                                     'error': {'code': error.code, 'message': str(error), 'retryable': False}})
                return 1
            if request is None:
                return 0
            reply = session.handle(request)
            try:
                data = encode_frame(reply)
            except KelmoError as error:
                data = encode_frame({'protocol': PROTOCOL, 'request_id': reply.get('request_id'), 'ok': False,
                                     'error': {'code': error.code, 'message': str(error), 'retryable': False}})
            stdout.write(data)
            stdout.flush()
