"""Real local ArchiveAPI over the archive storage Kelmo owns, with durable text intake.

No storage/native imports happen until construction. Historical evidence stays in
the same archive; this adapter never calls a legacy executable.
"""
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import (ArchiveHistory, ArchiveVersion, ChunkLocator, ChunkWindow,
                        EvidenceRef, OperationReceipt, Origin,
                        SearchHit, SearchResult, SourcePacket)
from .errors import KelmoError

RECORD_INDEX = 'core-record-index/1'
CHUNK_GENERATION = 'chunk:'  # + profile_id: the index generation a chunk locator names
RECORD_POOL = 16  # extra ranked records a configured search keeps to replace ones hidden or changed meanwhile


def _reason(error, default='embedding_unavailable'):
    data = getattr(error, 'data', None) or {}
    return data.get('reason') or data.get('field') or default


class Prepared:
    """Index input computed outside every transaction: the exact text it was cut from, its fitted chunks
    (None when the counting window is unavailable or the text cannot be cut) and their document vectors
    with the verified space they belong to (None while inference is unavailable)."""

    def __init__(self, text, chunks=None, vectors=None, space=None, reason=None):
        self.text, self.chunks, self.vectors, self.space, self.reason = text, chunks, vectors, space, reason


class ChunkIndex:
    """The configured chunk index of one vault: its declared settings, the one provider built from them
    (None while a broker channel cannot be opened) and its chunk policy.

    Chunk prerequisites (the counting window and so the profile) are built from what is declared and
    pinned, never from a live call, so intake keeps text, chunks and FTS while inference is down. Vectors
    need the live provider: they are admitted only for its verified space, after its input path is proven
    to be the profile's. Nothing here falls back to the built-in record model."""

    def __init__(self, settings, provider, *, unavailable=None):
        self.settings = settings
        self.provider = provider
        self.unavailable = unavailable
        self.sizes = dict(target=settings['target_chars'], maximum=settings['max_chars'],
                          overlap=settings['overlap_chars'])
        self._window = self._profile = self._space_id = None

    def window(self):
        if self._window is None:
            from .embeddings import declared_window
            self._window = declared_window(self.settings, self.provider)
        return self._window

    def profile(self):
        if self._profile is None:
            from kelmo.store.memory.chunk import chunk_profile
            self._profile = chunk_profile(self.window(), **self.sizes)
        return self._profile

    def profile_id(self):
        from kelmo.store.memory.embed import identity_id
        return identity_id(self.profile())

    def generation(self):
        return CHUNK_GENERATION + self.profile_id()

    def declared_space_id(self):
        """The id under which this vault's vectors are looked up. Only looked up: a space is recorded as
        verified solely when a live, verified provider's vectors are written."""
        if self._space_id is None:
            from kelmo.store.memory.embed import identity_id
            from .contracts import EmbeddingSpace
            if self.settings['provider'] == 'broker':
                space = EmbeddingSpace.from_dict(self.settings['space'])
            else:
                space = self.provider.describe()
            self._space_id = identity_id(space)
        return self._space_id

    def prepare(self, text, *, require_vectors):
        """Cut and embed outside any transaction. With require_vectors, missing vectors raise and nothing
        is written; otherwise text stays durable and the reason is kept."""
        from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
        from kelmo.store.memory.backfill import embed_documents
        from kelmo.store.memory.chunk import split_to_fit
        from kelmo.store.memory.embed import check_window
        try:
            window = self.window()
            chunks = split_to_fit(text, window, **self.sizes)
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            if require_vectors:
                raise ArchiveEmbeddingError('Indexed vectors are required but the text cannot be cut to the '
                                        'declared window', reason=_reason(error)) from error
            return Prepared(text, reason=_reason(error))
        if not chunks:
            if require_vectors:
                raise ArchiveEmbeddingError('Indexed vectors require text with indexable content',
                                        reason='empty_input')
            return Prepared(text, chunks, reason='empty_input')
        if self.provider is None:
            if require_vectors:
                raise ArchiveEmbeddingError('Indexed vectors are required but no provider is available',
                                        reason=self.unavailable or 'embedding_unavailable')
            return Prepared(text, chunks, reason=self.unavailable)
        try:
            space, _ = check_window(self.provider, window)
            vectors = embed_documents(self.provider, space, [chunk.text for chunk in chunks])
        except ArchiveEmbeddingError as error:
            if require_vectors:
                raise
            return Prepared(text, chunks, reason=_reason(error))
        except ArchiveValidationError as error:
            # The live provider's input path is not the declared profile's: no vector may join these chunks.
            if require_vectors:
                raise ArchiveEmbeddingError('The provider input path differs from the declared chunk profile',
                                        reason='profile_policy_mismatch') from error
            return Prepared(text, chunks, reason='profile_policy_mismatch')
        return Prepared(text, chunks, vectors, space)

    def admit(self, conn, archive_id, prepared):
        """Under the caller's write transaction and lock: write the chunk generation for the record's
        present text when it is not already current, then its vectors after backfill's per-chunk rechecks
        (version, hashes, exact slice, visibility). Text changed since the cut admits nothing."""
        from kelmo.store.memory.backfill import PendingVector, write_vectors
        from kelmo.store.memory.chunk import chunk_set_is_current, content_sha256, register_profile, write_chunks
        from kelmo.store.memory.embed import text_sha256
        if prepared is None or not prepared.chunks:
            return
        head = conn.execute('SELECT version, content FROM memories WHERE id=?', (archive_id,)).fetchone()
        if head is None or head['content'] != prepared.text:
            return
        profile_id = register_profile(conn, self.profile())
        rows = conn.execute('SELECT id, idx, profile_id, content_sha256, start_char, end_char, text, text_sha256 '
                            'FROM chunks WHERE memory_id=? ORDER BY idx', (archive_id,)).fetchall()
        if (chunk_set_is_current(head['content'], rows, profile_id)
                and [row['text'] for row in rows] == [chunk.text for chunk in prepared.chunks]):
            ids = [row['id'] for row in rows]
        else:
            ids = write_chunks(conn, archive_id, prepared.chunks, profile_id=profile_id,
                               memory_version=head['version'], content=head['content'])
        if prepared.vectors is not None:
            digest = content_sha256(head['content'])
            write_vectors(conn, prepared.space, [
                PendingVector(chunk_id, archive_id, chunk.idx, profile_id, text_sha256(chunk.text),
                              head['version'], digest, vector)
                for chunk_id, chunk, vector in zip(ids, prepared.chunks, prepared.vectors)])

    def pending(self, conn, archive_id):
        """True unless every current chunk of the record holds a vector of the declared space (looked up
        as verified only); a record with no current chunk is pending too."""
        from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
        from kelmo.store.memory.chunk import current_chunks
        try:
            profile_id, space_id = self.profile_id(), self.declared_space_id()
        except (ArchiveEmbeddingError, ArchiveValidationError):
            return True
        current = current_chunks(conn, profile_id, 'm.id = ?', (archive_id,))
        if not current:
            return True
        covered = conn.execute(
            'SELECT COUNT(*) FROM chunks c WHERE c.id IN (SELECT value FROM json_each(?)) AND EXISTS ('
            'SELECT 1 FROM chunk_vectors v JOIN embedding_spaces s ON s.space_id = v.space_id AND s.verified = 1 '
            'WHERE v.space_id = ? AND v.chunk_id = c.id AND v.text_sha256 = c.text_sha256)',
            (json.dumps(sorted(current)), space_id)).fetchone()[0]
        return covered < len(current)


class LocalArchiveAdapter:
    def __init__(self, archive_path, cache_dir, bindings, writer=None, *, connection_factory=None,
                 legacy_receipt_verifier=None, chunk_index=None):
        from kelmo.store.db import init_schema, open_connection
        from . import supersession, visibility
        self.bindings = bindings
        self.writer = writer or bindings.writer
        self.cache_dir = str(cache_dir)
        self._legacy_receipt_verifier = legacy_receipt_verifier
        # Configured [embedding]: capture, update, search and expand use this index and its provider.
        # None: the accepted record API (record vectors from the pinned record model) is unchanged.
        self.chunk_index = chunk_index
        if not Path(archive_path).is_file():
            raise KelmoError('E_STATE', 'Archive database is missing; run kelmo doctor.')
        factory = connection_factory if connection_factory is not None else open_connection
        self.connection = factory(str(archive_path))
        try:
            if self.connection.in_transaction:
                raise KelmoError('E_STATE', 'Archive startup requires an idle connection.')
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_writer_state'").fetchone():
                self._writer()
            visibility.ensure(self.connection)
            supersession.ensure(self.connection)
            with self.connection:
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_writer_state (
                    authority_id TEXT PRIMARY KEY, writer_id TEXT NOT NULL, epoch INTEGER NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_sources (
                    archive_id TEXT PRIMARY KEY REFERENCES memories(id), source_id TEXT NOT NULL,
                    source_packet TEXT NOT NULL, origin TEXT NOT NULL, vectors_pending INTEGER NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_receipts (
                    operation_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL REFERENCES memories(id),
                    version INTEGER NOT NULL, content_sha256 TEXT NOT NULL, source_id TEXT NOT NULL,
                    origin TEXT NOT NULL, fingerprint TEXT NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_hides (
                    operation_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL REFERENCES memories(id),
                    origin TEXT NOT NULL, receipt TEXT NOT NULL)''')
                # Purge receipts outlive the record they name, so they reference nothing. The keys are the
                # tombstone capture consults: a purged operation, content hash or source never re-enters.
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_purges (
                    archive_id TEXT PRIMARY KEY, disposition_id TEXT NOT NULL UNIQUE,
                    plan_sha256 TEXT NOT NULL, purged_at TEXT NOT NULL, receipt TEXT NOT NULL,
                    effects TEXT NOT NULL)''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_purged_keys (
                    kind TEXT NOT NULL, value TEXT NOT NULL, archive_id TEXT NOT NULL,
                    PRIMARY KEY(kind, value))''')
                owners = self.connection.execute('SELECT authority_id FROM archive_writer_state').fetchall()
                if owners and any(row['authority_id'] != bindings.authority_id for row in owners):
                    raise KelmoError('E_WRITER', 'Archive belongs to another authority.')
                self.connection.execute('INSERT OR IGNORE INTO archive_writer_state VALUES (?,?,?)',
                                        (bindings.authority_id, self.writer.writer_id, self.writer.epoch))
                self._writer()
            init_schema(self.connection)
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                self._writer()
                self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_version_sources (
                    archive_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL,
                    source_id TEXT NOT NULL, source_packet TEXT NOT NULL, origin TEXT NOT NULL,
                    stored_at TEXT NOT NULL, vectors_pending INTEGER NOT NULL,
                    PRIMARY KEY(archive_id, version))''')
                # Only v1 has a complete packet in the unversioned source table.
                self.connection.execute('''INSERT OR IGNORE INTO archive_version_sources
                    SELECT s.archive_id,1,s.source_id,s.source_packet,s.origin,m.created_at,s.vectors_pending
                    FROM archive_sources s JOIN memories m ON m.id=s.archive_id WHERE m.version=1''')
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def assert_writer(self):
        self._writer()

    @contextmanager
    def _read_snapshot(self):
        """Keep multi-query public reads coherent without finishing caller work."""
        if self.connection.in_transaction:
            yield
            return
        self.connection.execute('BEGIN')
        try:
            yield
        finally:
            self.connection.rollback()

    def _writer(self):
        try:
            self.bindings.check_writer(self.writer)
        except ValueError as error:
            raise KelmoError('E_WRITER', str(error)) from error
        row = self.connection.execute('SELECT * FROM archive_writer_state WHERE authority_id=?',
                                      (self.bindings.authority_id,)).fetchone()
        if row is None or (row['writer_id'], row['epoch']) != (self.writer.writer_id, self.writer.epoch):
            raise KelmoError('E_WRITER', 'Stale archive writer epoch or identity.')

    def _origin(self, origin):
        try:
            self.bindings.validate(origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', str(error)) from error
        self._writer()

    @staticmethod
    def _origin_json(origin):
        return json.dumps(origin.to_dict(), sort_keys=True)

    def _row(self, archive_id):
        row = self.connection.execute('SELECT * FROM memories WHERE id=?', (archive_id,)).fetchone()
        if row is None:
            purged = self.purge_record(archive_id)
            if purged is not None:
                raise KelmoError('E_PURGED', f"Archive record {archive_id} was physically purged under disposition "
                                f"{purged['disposition_id']}; it cannot be read, hidden, updated or recaptured.")
            raise KelmoError('E_NOT_FOUND', 'Archive record not found.')
        record = dict(row)
        self._current_version(record)
        return record

    @staticmethod
    def _current_version(record):
        if type(record.get('version')) is not int or record['version'] < 1:
            raise KelmoError('E_CAPABILITY', 'Archive row has no valid version identity.')

    def _exact_row(self, archive_id, version):
        from kelmo.store.errors import ArchiveNotFoundError
        from kelmo.store.memory.get import memory_get
        try:
            return memory_get(self.connection, id=archive_id, version=version)
        except ArchiveNotFoundError as error:
            head = self._row(archive_id)
            if version < head['version']:
                raise KelmoError('E_CAPABILITY', 'This replaced version is missing from stored history; it cannot be reconstructed.') from error
            raise KelmoError('E_NOT_FOUND', str(error)) from error

    def _source_row(self, row):
        return self.connection.execute(
            'SELECT * FROM archive_version_sources WHERE archive_id=? AND version=?',
            (row['id'], row['version'])).fetchone()

    def _ref(self, row):
        self._current_version(row)
        stored = self._source_row(row)
        source_id = stored['source_id'] if stored else self._legacy_source_id(row)
        return EvidenceRef(row['id'], row['version'], hashlib.sha256(row['content'].encode()).hexdigest(), source_id)

    @staticmethod
    def _legacy_source_id(row):
        path = row.get('raw_note_path') or ''
        prefix = path.split('-', 1)[0]
        if len(prefix) == 64 and all(char in '0123456789abcdef' for char in prefix):
            return prefix
        return path or row['id']

    def _version(self, row):
        stored = self._source_row(row)
        if stored:
            source = SourcePacket.from_dict(json.loads(stored['source_packet']))
            origin = Origin.from_dict(json.loads(stored['origin']))
        else:
            try:
                source = SourcePacket(row['content'].encode(), row['source'] or 'legacy',
                                      self._legacy_source_id(row), 'legacy.md', row['created_at'],
                                      row['kind'], row['domain'], row['project'],
                                      tuple(json.loads(row['tags_json'])), row['valid_from'])
                origin = Origin(self.bindings.authority_id, 'legacy-unattested', row['machine_tag'])
            except (AttributeError, TypeError, ValueError) as error:
                # An older archive accepted values such as 2026-02-30, empty metadata or trailing
                # whitespace; they are reported, never normalized into a different packet.
                raise KelmoError('E_CAPABILITY', f"Legacy archive record {row['id']} version {row['version']} "
                                'holds metadata a typed source cannot represent (' + str(error) + '). No packet '
                                'is fabricated: the preserved metadata needs repair through an explicit archive '
                                'migration, and this exact version stays readable as its raw row through '
                                'memory_get.') from error
        current = self.connection.execute('SELECT rowid,version FROM memories WHERE id=?', (row['id'],)).fetchone()
        if current['version'] == row['version']:
            pending = self._head_pending(row['id'], current['rowid'])
        else:
            # No historical vector is inferred from the current head's index.
            pending = bool(stored['vectors_pending']) if stored else True
        ref = self._ref(row)
        if source.content_sha256 != ref.content_sha256:
            raise KelmoError('E_SOURCE', 'Stored source packet differs from archive bytes.')
        if (source.kind, source.domain, source.project, source.tags, source.source_kind) != (
                row['kind'], row['domain'], row['project'], tuple(json.loads(row['tags_json'])), row['source'] or 'legacy'):
            raise KelmoError('E_SOURCE', 'Stored source packet differs from archive metadata.')
        return ArchiveVersion(ref, source, origin,
                              stored['stored_at'] if stored else row.get('written_at', row.get('updated_at', row['created_at'])), pending)

    def _head_pending(self, archive_id, rowid):
        """Whether the current version still waits for vectors: of the configured chunk index when one is
        configured (never the built-in record model's), else of the record index."""
        if self.chunk_index is not None:
            return self.chunk_index.pending(self.connection, archive_id)
        return self.connection.execute('SELECT 1 FROM memories_vec WHERE rowid=?', (rowid,)).fetchone() is None

    def _save_version(self, row, version):
        self.connection.execute('''INSERT INTO archive_version_sources VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(archive_id,version) DO UPDATE SET vectors_pending=excluded.vectors_pending''',
            (row['id'], row['version'], version.source.source_id,
             json.dumps(version.source.to_dict(), sort_keys=True), self._origin_json(version.origin),
             version.stored_at, int(version.vectors_pending)))

    def public_record(self, archive_id):
        from . import supersession, visibility
        row = self._row(archive_id)
        chain = supersession.links(self.connection, archive_id)
        if visibility.is_forgotten(self.connection, archive_id):
            newer = (' It is superseded by ' + ', '.join(chain['superseded_by']) + '.') if chain['superseded_by'] else ''
            raise KelmoError('E_FORGOTTEN', 'This record was forgotten; it is hidden from search and list and retained on disk.' + newer)
        return dict(row, **chain)

    def full_record(self, archive_id):
        """The visible current record as the old full read returned it: its row with chunk_total and the
        versions listing (from memory_get full=true), plus its supersession links."""
        from kelmo.store.memory.get import memory_get
        with self._read_snapshot():
            chain = {key: value for key, value in self.public_record(archive_id).items()
                     if key in ('supersedes', 'superseded_by')}
            row = memory_get(self.connection, id=archive_id, full=True)
            self._current_version(row)
            return dict(row, **chain)

    def get(self, archive_id, version=None):
        with self._read_snapshot():
            row = self.public_record(archive_id)
            if version is not None and (type(version) is not int or version < 1):
                raise KelmoError('E_INPUT', 'Version must be a positive integer.')
            return self._version(self._exact_row(archive_id, row['version'] if version is None else version))

    def _capture_prior(self, source, raw_note_path):
        if source.source_kind != 'raw_note':
            return None
        # A fixed raw-note identity may now own a different head. Dedup against
        # exact bytes in its replaced versions as well, never against that head.
        rows = self.connection.execute('''SELECT id,version,content,created_at,valid_from,raw_note_path,1 AS is_current FROM memories
            WHERE source='raw_note' AND raw_note_path=? UNION ALL
            SELECT memory_id AS id,version,content,created_at,valid_from,raw_note_path,0 AS is_current FROM memory_versions
            WHERE source='raw_note' AND raw_note_path=? ORDER BY is_current DESC,version''',
            (raw_note_path, raw_note_path)).fetchall()
        matched_bytes = False
        fallback = None
        exact_history = None
        current = None
        for row in rows:
            if row['content'].encode() == source.content:
                matched_bytes = True
                exact = dict(row)
                if self._ref(exact).source_id == source.source_id:
                    if fallback is None:
                        fallback = exact
                    if row['is_current']:
                        current = exact
                    stored = self._source_row(exact)
                    if stored and SourcePacket.from_dict(json.loads(stored['source_packet'])) == source:
                        if row['is_current']:
                            return exact
                        if exact_history is None:
                            exact_history = exact
        # Legacy captures may differ in observed time or declared role tags.
        # Keep their byte/source dedup without rewriting an existing version.
        if source.require_vectors and current is not None:
            return current
        if exact_history is not None or fallback is not None:
            return exact_history if exact_history is not None else fallback
        if matched_bytes:
            raise KelmoError('E_SOURCE', 'Stored raw-note identity belongs to a different source.')
        return None

    def capture(self, source, origin, idempotency_key):
        from kelmo.store.db import now_iso
        from kelmo.store.errors import ArchiveEmbeddingError
        from kelmo.store.memory import store
        from kelmo.store.memory.taxonomy import validate_kind_domain
        self._origin(origin)
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive mutation requires an idle connection.')
        if not isinstance(source, SourcePacket) or not isinstance(idempotency_key, str) or not idempotency_key:
            raise KelmoError('E_INPUT', 'Capture requires source packet and idempotency identity.')
        validate_kind_domain(source.kind, source.domain)
        fingerprint = hashlib.sha256(json.dumps([source.to_dict(), origin.to_dict()], sort_keys=True).encode()).hexdigest()
        existing = self.connection.execute('SELECT * FROM archive_receipts WHERE operation_id=?',
                                           (idempotency_key,)).fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise KelmoError('E_IDEMPOTENCY', 'Archive key already identifies different input.')
            ref = EvidenceRef(existing['archive_id'], existing['version'], existing['content_sha256'], existing['source_id'])
            self.verify_receipt(ref, idempotency_key, origin)
            return ref
        self._refuse_purged(source, idempotency_key)
        raw_note_path = source.content_sha256 + '-' + source.filename
        valid_from = store.resolve_valid_from(source.valid_from, source.source_kind, source.filename)
        # Embedding is outside the write transaction; the writer is rechecked after it.
        prior = self._capture_prior(source, raw_note_path)
        vector = None
        prepared = None
        prior_pending = False
        if prior is not None and source.require_vectors:
            if self._row(prior['id'])['version'] != prior['version']:
                raise KelmoError('E_CAPABILITY', 'Historical vector admission is unavailable; exact bytes remain readable.')
            prior_rowid = self.connection.execute('SELECT rowid FROM memories WHERE id=?', (prior['id'],)).fetchone()[0]
            prior_pending = self._head_pending(prior['id'], prior_rowid)
        if prior is None or prior_pending:
            if self.chunk_index is not None:
                # The configured provider only; inference runs here, outside every transaction.
                prepared = self.chunk_index.prepare(source.content.decode('utf-8'),
                                                    require_vectors=source.require_vectors)
            else:
                try:
                    vector = store.embed(source.content.decode('utf-8'), cache_dir=self.cache_dir)
                except ArchiveEmbeddingError:
                    if source.require_vectors:
                        raise
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._origin(origin)
            # The same key may have arrived while embedding; a replay must not add a row.
            raced = self.connection.execute('SELECT * FROM archive_receipts WHERE operation_id=?',
                                             (idempotency_key,)).fetchone()
            if raced:
                if raced['fingerprint'] != fingerprint:
                    raise KelmoError('E_IDEMPOTENCY', 'Archive key raced with different input.')
                ref = EvidenceRef(raced['archive_id'], raced['version'], raced['content_sha256'], raced['source_id'])
                self.verify_receipt(ref, idempotency_key, origin)
                return ref
            # Rechecked under the lock: a purge may have committed while this capture embedded.
            self._refuse_purged(source, idempotency_key)
            prior = self._capture_prior(source, raw_note_path)
            if prior:
                record_id = prior['id']
            else:
                record_id, stamp = str(uuid.uuid4()), now_iso()
                inserted = self.connection.execute('''INSERT INTO memories
                    (id,content,kind,domain,project,tags_json,source,machine_tag,created_at,updated_at,raw_note_path,valid_from)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (record_id, source.content.decode('utf-8'), source.kind, source.domain, source.project,
                     json.dumps(source.tags), source.source_kind, origin.machine_tag, stamp, stamp,
                     raw_note_path, valid_from))
                if vector is not None:
                    self.connection.execute('INSERT INTO memories_vec(rowid, embedding) VALUES (?,?)',
                                            (inserted.lastrowid, vector))
            row = self._exact_row(record_id, prior['version']) if prior else self._row(record_id)
            if row['content'].encode() != source.content:
                raise KelmoError('E_SOURCE', 'Existing raw-note identity has different bytes.')
            rowid = self.connection.execute('SELECT rowid FROM memories WHERE id=?', (record_id,)).fetchone()[0]
            if self.chunk_index is not None and row['version'] == self._row(record_id)['version']:
                # Rechecked under this lock: the present text, version and profile admit the prepared
                # chunks and vectors, or nothing.
                self.chunk_index.admit(self.connection, record_id, prepared)
            pending = self._head_pending(record_id, rowid)
            if prior and row['version'] != self._row(record_id)['version']:
                if source.require_vectors:
                    raise KelmoError('E_CAPABILITY', 'Historical vector admission is unavailable; exact bytes remain readable.')
                pending = self._version(row).vectors_pending
            if source.require_vectors and pending:
                if self.chunk_index is not None or vector is None:
                    raise ArchiveEmbeddingError('Replacement requires indexed vectors before supersession.')
                self.connection.execute('INSERT INTO memories_vec(rowid, embedding) VALUES (?,?)', (rowid, vector))
                pending = False
            if self._source_row(row) is None:
                if prior:
                    # Owning a new capture receipt does not attest the origin
                    # or metadata of an older, otherwise unbound archive row.
                    version = self._version(row)
                else:
                    version = ArchiveVersion(
                        EvidenceRef(row['id'], row['version'], source.content_sha256, source.source_id),
                        source, origin,
                        row.get('written_at', row.get('updated_at', row['created_at'])), pending)
                self._save_version(row, version)
            if row['version'] == self._row(record_id)['version']:
                canonical = self._version(row)
                self.connection.execute('INSERT OR IGNORE INTO archive_sources VALUES (?,?,?,?,?)',
                                        (row['id'], canonical.source.source_id,
                                         json.dumps(canonical.source.to_dict(), sort_keys=True),
                                         self._origin_json(canonical.origin), int(pending)))
                self.connection.execute('UPDATE archive_sources SET vectors_pending=? WHERE archive_id=?',
                                        (int(pending), row['id']))
            ref = self._ref(row)
            if ref.source_id != source.source_id:
                raise KelmoError('E_SOURCE', 'Stored record belongs to a different source identity.')
            self.connection.execute('INSERT OR IGNORE INTO archive_receipts VALUES (?,?,?,?,?,?,?)',
                                    (idempotency_key, ref.archive_id, ref.version, ref.content_sha256,
                                     ref.source_id, self._origin_json(origin), fingerprint))
            bound = self.connection.execute('SELECT fingerprint FROM archive_receipts WHERE operation_id=?',
                                            (idempotency_key,)).fetchone()
            if bound['fingerprint'] != fingerprint:
                raise KelmoError('E_IDEMPOTENCY', 'Archive key raced with different input.')
        return ref

    def verify_receipt(self, ref, operation_id, origin):
        self._origin(origin)
        row = self.connection.execute('SELECT * FROM archive_receipts WHERE operation_id=?',
                                      (operation_id,)).fetchone()
        if row is None or (row['archive_id'], row['version'], row['content_sha256'], row['source_id'], row['origin']) != (
                ref.archive_id, ref.version, ref.content_sha256, ref.source_id, self._origin_json(origin)):
            raise KelmoError('E_RECEIPT', 'Hidden evidence requires its exact owning operation receipt.')
        version = self._version(self._exact_row(ref.archive_id, ref.version))
        if version.evidence != ref:
            raise KelmoError('E_SOURCE', 'Archive receipt no longer matches its exact version/hash.')
        return version

    def receipt_record(self, ref, operation_id, origin):
        self.verify_receipt(ref, operation_id, origin)
        return self._exact_row(ref.archive_id, ref.version)

    def capture_receipt(self, operation_id, origin):
        self._origin(origin)
        row = self.connection.execute('SELECT * FROM archive_receipts WHERE operation_id=?',
                                      (operation_id,)).fetchone()
        if row is None:
            return None
        ref = EvidenceRef(row['archive_id'], row['version'], row['content_sha256'], row['source_id'])
        self.verify_receipt(ref, operation_id, origin)
        return ref

    def bind_legacy_receipt(self, ref, operation_id, origin):
        """Coordinator-only bridge after checking the existing journal/source bytes.

        This is not a public API or a way to acquire authority from a request body.
        """
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive mutation requires an idle connection.')
        self._origin(origin)
        if self._legacy_receipt_verifier is None or self._legacy_receipt_verifier(ref, operation_id, origin) is not True:
            raise KelmoError('E_RECEIPT', 'Legacy binding requires verified coordinator journal/source ownership.')
        row = self._exact_row(ref.archive_id, ref.version)
        if self._ref(row) != ref:
            raise KelmoError('E_RECEIPT', 'Legacy receipt does not identify these stored bytes.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._origin(origin)
            if self._ref(self._exact_row(ref.archive_id, ref.version)) != ref:
                raise KelmoError('E_RECEIPT', 'Legacy receipt no longer identifies the exact stored bytes.')
            self.connection.execute('INSERT OR IGNORE INTO archive_receipts VALUES (?,?,?,?,?,?,?)',
                                    (operation_id, ref.archive_id, ref.version, ref.content_sha256,
                                     ref.source_id, self._origin_json(origin), 'legacy-owning-receipt'))
            self.verify_receipt(ref, operation_id, origin)

    def hide(self, archive_id, origin, idempotency_key, *, reason=None):
        self._origin(origin)
        self._row(archive_id)
        previous = self.connection.execute('SELECT * FROM archive_hides WHERE operation_id=?',
                                            (idempotency_key,)).fetchone()
        if previous:
            if previous['archive_id'] != archive_id or previous['origin'] != self._origin_json(origin):
                raise KelmoError('E_IDEMPOTENCY', 'Hide key already identifies another request.')
        # Never finish a transaction owned by the caller when acquiring our lock.
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive mutation requires an idle connection.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._origin(origin)
            row = self._row(archive_id)
            previous = self.connection.execute('SELECT * FROM archive_hides WHERE operation_id=?',
                                                (idempotency_key,)).fetchone()
            if previous:
                if previous['archive_id'] != archive_id or previous['origin'] != self._origin_json(origin):
                    raise KelmoError('E_IDEMPOTENCY', 'Hide key already identifies another request.')
                return OperationReceipt.from_dict(json.loads(previous['receipt']))
            changed = self.connection.execute('INSERT OR IGNORE INTO record_visibility VALUES (?,?,?)',
                                              (archive_id, self._timestamp(), reason)).rowcount == 1
            receipt = OperationReceipt(idempotency_key, idempotency_key, self._ref(row).source_id,
                                       'hidden' if changed else 'already-hidden', (self._ref(row),))
            self.connection.execute('INSERT INTO archive_hides VALUES (?,?,?,?)',
                                    (idempotency_key, archive_id, self._origin_json(origin), json.dumps(receipt.to_dict())))
        return receipt

    @staticmethod
    def _timestamp():
        from kelmo.store.db import now_iso
        return now_iso()

    def hidden_at(self, archive_id):
        from .visibility import forgotten_at
        return forgotten_at(self.connection, archive_id)

    def counts(self):
        from . import supersession, visibility
        return {'archive_records': self.connection.execute('SELECT COUNT(*) FROM memories').fetchone()[0],
                'forgotten_records': visibility.count(self.connection),
                'superseded_records': supersession.count(self.connection)}

    def stats(self):
        """What the archive holds, for callers: visible records by kind, domain and project, hidden and
        superseded counts, the archive file size, chunk and vector coverage of the configured index, and
        logged searches. Read-only; no path or provider setting is returned."""
        from kelmo.store.memory.stats import _index_stats, _table
        visible = 'NOT EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = m.id)'
        index = self.chunk_index
        identity = {'provider': None, 'model': None, 'dimension': None, 'profile_id': None, 'space_id': None}
        if index is not None:
            space = index.settings.get('space') or {}
            identity.update(provider=index.settings['provider'],
                            model=index.settings.get('model') or space.get('artifact_identity'),
                            dimension=space.get('dimension') or index.settings.get('dimension'))
            for name, read in (('profile_id', index.profile_id), ('space_id', index.declared_space_id)):
                try:
                    identity[name] = read()
                except Exception:  # noqa: BLE001 - An identity that cannot be established is reported unknown.
                    identity[name] = None
        path = self.connection.execute('PRAGMA database_list').fetchone()[2]
        with self._read_snapshot():
            connection = self.connection

            def grouped(column):
                return dict(connection.execute(
                    f"SELECT COALESCE({column}, '(none)'), COUNT(*) FROM memories m WHERE {visible} "
                    'GROUP BY 1 ORDER BY 1').fetchall())

            total, latest = connection.execute(
                f'SELECT COUNT(*), MAX(created_at) FROM memories m WHERE {visible}').fetchone()
            meta = dict(connection.execute(
                "SELECT key, value FROM archive_meta WHERE key='schema_version'").fetchall())
            result = {'total': total, 'by_kind': grouped('kind'), 'by_domain': grouped('domain'),
                      'by_project': grouped('project'), 'latest_created_at': latest,
                      'db_size_bytes': os.path.getsize(path) if path else None,
                      'schema_version': int(meta['schema_version']) if 'schema_version' in meta else None,
                      'embedding_model': identity['model'], 'embedding_dim': identity['dimension'],
                      'embedding_library': identity['provider'], 'embedding': identity}
            result.update(self.counts())
            result.update(_index_stats(connection, identity['profile_id'], identity['space_id']))
            if _table(connection, 'search_log'):
                searches, last = connection.execute('SELECT COUNT(*), MAX(at) FROM search_log').fetchone()
                result.update(searches_logged=searches, last_search_at=last)
        return result

    def list_records(self, limit, offset):
        from kelmo.store.errors import ArchiveLegacyRecordError
        from kelmo.store.memory.list import memory_list
        with self._read_snapshot():
            try:
                result = memory_list(self.connection, limit=limit, offset=offset, order_by='created_at',
                                     order='desc', exclude_forgotten=True)
            except ArchiveLegacyRecordError as error:
                raise KelmoError('E_CAPABILITY', str(error)) from error
        for row in result['items']:
            self._current_version(row)
        return result

    def search_records(self, query, filters, limit, mode='hybrid'):
        if self.chunk_index is not None:
            if not isinstance(query, str) or not query.strip():
                raise KelmoError('E_INPUT', 'Search requires nonempty query and limit 1..100.')
            found = self._chunk_search([query], filters, limit, mode)
            items = [record for record, _ in found['records']]
            metadata = {'mode': found['mode'], 'degraded': found['reason'] is not None, 'reason': found['reason']}
            if filters.backend():
                metadata['filters'] = filters.backend()
            return {'items': items, 'search': metadata}
        with self._read_snapshot():
            return self._search_records(query, filters, limit, mode)

    def _chunk_search(self, queries, filters, limit, mode):
        """Configured search over the chunk index, without an enclosing snapshot: search_chunks embeds the
        variants before opening its own read snapshot, so no transaction is held during inference.

        Returns one entry per record in fused order, (record row of the present version, the record's
        chunk rows), and the mode/reason. Records the chunk index cannot answer for (no current chunks)
        are never silently missed: hybrid and lexical add their record-index keyword matches and every
        mode is labelled degraded (reason unindexed_records unless a stronger one applies). Semantic
        failure is the typed E_EMBEDDING refusal."""
        from kelmo.store.config import MAX_CHUNKS_PER_MEMORY, MAX_RECORD_POOL
        from kelmo.store.errors import (ArchiveEmbeddingError, ArchiveLegacyRecordError, ArchiveStorageError,
                                    ArchiveValidationError)
        from kelmo.store.memory.search import search_chunks
        if mode not in ('hybrid', 'semantic', 'lexical'):
            raise KelmoError('E_INPUT', 'Search mode must be hybrid, semantic or lexical.')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise KelmoError('E_INPUT', 'Search requires nonempty query and limit 1..100.')
        if isinstance(queries, str) or not isinstance(queries, (list, tuple)):
            raise KelmoError('E_INPUT', 'Search requires a list of query variants.')
        backend = filters.backend()
        try:
            profile_id = self.chunk_index.profile_id()
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            return self._unindexed_search(queries, filters, limit, mode, _reason(error))
        try:
            # Ranked by records, not chunk rows: `limit` records may hold up to twice as many chunks. The
            # bounded pool beyond `limit` replaces records the re-read below drops as hidden or changed.
            result = search_chunks(self.connection, queries=list(queries), profile_id=profile_id,
                                   provider=self.chunk_index.provider, mode=mode,
                                   limit=min(100, limit * MAX_CHUNKS_PER_MEMORY), exclude_forgotten=True,
                                   record_limit=min(MAX_RECORD_POOL, limit + RECORD_POOL), **backend)
        except ArchiveEmbeddingError as error:
            raise KelmoError('E_EMBEDDING', f'Semantic search is unavailable ({_reason(error)}); request hybrid '
                            'or lexical.') from error
        except ArchiveStorageError as error:
            if error.data.get('underlying_error') != 'chunks_not_built':
                raise
            return self._unindexed_search(queries, filters, limit, mode, 'chunks_not_built')
        except ArchiveLegacyRecordError as error:
            raise KelmoError('E_CAPABILITY', str(error)) from error
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        records, order = {}, []
        for chunk in result['items']:
            record = records.get(chunk['id'])
            if record is None:
                current = self.connection.execute(
                    'SELECT * FROM memories m WHERE m.id=? AND NOT EXISTS '
                    '(SELECT 1 FROM record_visibility rv WHERE rv.id = m.id)', (chunk['id'],)).fetchone()
                # The present, still visible version only: a record changed or hidden after the snapshot is
                # left out, never mixed or exposed.
                if current is None or current['version'] != chunk['version'] or hashlib.sha256(
                        current['content'].encode()).hexdigest() != chunk['record_content_sha256']:
                    continue
                if len(order) == limit:
                    continue
                record = dict(current)
                self._current_version(record)
                if 'distance' in chunk:
                    record['distance'] = chunk['distance']
                records[chunk['id']] = (record, [])
                order.append(chunk['id'])
            records[chunk['id']][1].append(chunk)
        found = [records[archive_id] for archive_id in order]
        reason = result['search']['reason']
        if result['search']['unindexed_records']:
            reason = reason or 'unindexed_records'
            if mode != 'semantic' and len(found) < limit:
                seen = set(order)
                for row in self._record_keywords(queries, filters, limit):
                    if len(found) == limit:
                        break
                    if row['id'] not in seen and not self._has_current_chunks(profile_id, row['id']):
                        seen.add(row['id'])
                        found.append((row, []))
        return {'records': found, 'mode': result['search']['mode'], 'reason': reason,
                'index_identity': result['search']['index_identity'], 'profile_id': profile_id}

    def _has_current_chunks(self, profile_id, archive_id):
        from kelmo.store.memory.chunk import current_chunks
        return bool(current_chunks(self.connection, profile_id, 'm.id = ?', (archive_id,)))

    def _record_keywords(self, queries, filters, limit):
        """Record-index keyword matches of every variant, visible records only, first match order."""
        from kelmo.store.memory.search import memory_search
        rows, seen = [], set()
        with self._read_snapshot():
            for query in queries:
                for row in memory_search(self.connection, query=query, mode='lexical', limit=limit,
                                         exclude_forgotten=True, **filters.backend()):
                    if row['id'] not in seen:
                        seen.add(row['id'])
                        self._current_version(row)
                        rows.append(row)
        return rows

    def _unindexed_search(self, queries, filters, limit, mode, reason):
        """The chunk index cannot answer at all (its profile cannot be built, or no chunk was cut yet):
        semantic is refused, hybrid and lexical answer from the record index's keywords, labelled."""
        if mode == 'semantic':
            raise KelmoError('E_EMBEDDING', f'Semantic search is unavailable ({reason}); request hybrid or lexical.')
        rows = self._record_keywords(queries, filters, limit)[:limit]
        return {'records': [(row, []) for row in rows], 'mode': 'lexical', 'reason': reason,
                'index_identity': RECORD_INDEX, 'profile_id': None}

    def _search_records(self, query, filters, limit, mode):
        from kelmo.store.errors import ArchiveEmbeddingError
        from kelmo.store.memory.search import memory_search
        if mode not in ('hybrid', 'semantic', 'lexical'):
            raise KelmoError('E_INPUT', 'Search mode must be hybrid, semantic or lexical.')
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 100:
            raise KelmoError('E_INPUT', 'Search requires nonempty query and limit 1..100.')
        kwargs = dict(filters.backend(), query=query, limit=limit, exclude_forgotten=True)
        metadata = {'mode': mode, 'degraded': False, 'reason': None}
        try:
            items = memory_search(self.connection, mode=mode, cache_dir=self.cache_dir, **kwargs)
        except ArchiveEmbeddingError as error:
            if mode == 'semantic':
                raise KelmoError('E_EMBEDDING', 'Semantic search is unavailable; request hybrid or lexical.') from error
            items = memory_search(self.connection, mode='lexical', **kwargs)
            metadata = {'mode': 'lexical', 'degraded': True, 'reason': 'embedding_unavailable'}
        if filters.backend():
            metadata['filters'] = filters.backend()
        for row in items:
            self._current_version(row)
        return {'items': items, 'search': metadata}

    def search(self, queries, filters, limit, mode='hybrid'):
        if self.chunk_index is not None:
            return self._chunk_result(queries, filters, limit, mode)
        from kelmo.store.config import RRF_K
        from kelmo.store.errors import ArchiveValidationError
        from kelmo.store.memory.search import query_variants
        try:
            variants = query_variants(list(queries) if isinstance(queries, tuple) else queries)
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        # Each variant ranks the record index on its own; reciprocal-rank fusion merges the rankings with
        # equal weights, a record first seen earlier winning a tie.
        results = [self.search_records(variant, filters, limit, mode) for variant in variants]
        scores, rows = {}, {}
        for result in results:
            for rank, row in enumerate(result['items']):
                scores[row['id']] = scores.get(row['id'], 0.0) + 1.0 / (RRF_K + rank + 1)
                rows.setdefault(row['id'], row)
        order = sorted(rows, key=lambda archive_id: -scores[archive_id])[:limit]
        degraded = [result['search'] for result in results if result['search']['degraded']]
        ranked = degraded[0] if degraded else results[0]['search']
        return SearchResult(tuple(SearchHit(self._ref(rows[archive_id]), rows[archive_id]['content'])
                                  for archive_id in order),
                            ranked['mode'], bool(degraded), ranked['reason'], 'core-record-index/1')

    def _chunk_result(self, queries, filters, limit, mode):
        """Caller-supplied variants over the chunk index. Each hit is one record: its exact present
        EvidenceRef, the text of its best chunk and a ChunkLocator per returned chunk, naming this index
        generation (chunk:<profile_id>). A keyword-only addition for an unindexed record carries its full
        text and no locator."""
        from kelmo.store.errors import ArchiveValidationError
        from kelmo.store.memory.search import query_variants
        try:
            variants = query_variants(list(queries) if isinstance(queries, tuple) else queries)
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        found = self._chunk_search(variants, filters, limit, mode)
        hits = []
        for record, chunks in found['records']:
            ref = self._ref(record)
            locators = tuple(ChunkLocator(ref.archive_id, ref.version, ref.content_sha256,
                                          CHUNK_GENERATION + found['profile_id'], chunk['chunk_idx'])
                             for chunk in chunks)
            hits.append(SearchHit(ref, chunks[0]['content'] if chunks else record['content'], locators))
        return SearchResult(tuple(hits), found['mode'], found['reason'] is not None, found['reason'],
                            found['index_identity'])

    def backfill_vectors(self, *, batch=None, sleep=None):
        """Coordinator maintenance: embed the missing vectors of the configured profile with the configured
        provider (backfill's per-chunk rechecks under the writer lock and epoch). Stops, never fails over,
        when the provider is unavailable."""
        from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
        from kelmo.store.memory import backfill as backfill_module
        index = self._require_index()
        try:
            profile_id = index.profile_id()
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            return {'missing': None, 'written': 0, 'stopped': f'chunk profile unavailable ({_reason(error)})'}
        if index.provider is None:
            return {'missing': None, 'written': 0,
                    'stopped': f'embedding provider unavailable ({index.unavailable or "embedding_unavailable"})'}
        options = {key: value for key, value in (('batch', batch), ('sleep', sleep)) if value is not None}
        return backfill_module.backfill(self.connection, index.provider, profile_id=profile_id,
                                        progress=lambda line: None, writer_check=self._writer, **options)

    def rebuild_chunks(self, *, force=False, batch=None):
        """Coordinator maintenance: cut every record whose chunks are not current for the configured profile,
        with the vault's own split sizes, and embed them when the provider answers (reembed)."""
        from kelmo.store.memory.reembed import reembed
        from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
        index = self._require_index()
        try:
            window = index.window()
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            raise KelmoError('E_CAPABILITY', f'The declared chunk window is unavailable: {error}') from error
        options = {} if batch is None else {'batch': batch}
        return reembed(self.connection, window, provider=index.provider, force=force,
                       progress=lambda line: None, writer_check=self._writer, **index.sizes, **options)

    # --- physical purge: coordinator-only, reached through ArchiveAdmin with a checked disposition ---

    def purge_record(self, archive_id):
        """The purge receipt of a record, or None while it was never purged."""
        row = self.connection.execute('SELECT * FROM archive_purges WHERE archive_id=?', (archive_id,)).fetchone()
        if row is None:
            return None
        return dict(row, receipt=json.loads(row['receipt']), effects=json.loads(row['effects']))

    def _refuse_purged(self, source, operation_id=None):
        """Capture and update consult the tombstone; an update carries no operation key."""
        keys = [('content', source.content_sha256), ('source', source.source_id)]
        if operation_id is not None:
            keys.insert(0, ('operation', operation_id))
        for kind, value in keys:
            row = self.connection.execute('SELECT archive_id FROM archive_purged_keys WHERE kind=? AND value=?',
                                          (kind, value)).fetchone()
            if row is not None:
                raise KelmoError('E_PURGED', f'This {kind} belongs to purged record {row["archive_id"]}; '
                                'purged evidence cannot re-enter the archive.')

    def purge_dependents(self, archive_id):
        """Every archive-side row that names the record: versions, owning and hide receipts, supersession
        links, other records holding identical bytes, and derived index rows.
        Read in one snapshot; plan_sha256 pins it for apply_purge."""
        with self._read_snapshot():
            return self._dependents(archive_id)

    def _dependents(self, archive_id):
        from . import supersession
        head = self._row(archive_id)
        versions = [{'version': row['version'], 'content_sha256': hashlib.sha256(row['content'].encode()).hexdigest()}
                    for row in self.connection.execute(
                        'SELECT version, content FROM memory_versions WHERE memory_id=? ORDER BY version', (archive_id,))]
        versions.append({'version': head['version'],
                         'content_sha256': hashlib.sha256(head['content'].encode()).hexdigest()})
        for item in versions:
            stored = self.connection.execute(
                'SELECT source_id FROM archive_version_sources WHERE archive_id=? AND version=?',
                (archive_id, item['version'])).fetchone()
            item['source_id'] = stored['source_id'] if stored else self._legacy_source_id(head)
        hashes = sorted({item['content_sha256'] for item in versions})
        identical = sorted({row['id'] for row in self.connection.execute(
            'SELECT id, content FROM memories WHERE id != ?', (archive_id,))
            if hashlib.sha256(row['content'].encode()).hexdigest() in hashes})
        links = supersession.links(self.connection, archive_id)

        def column(sql):
            return sorted(row[0] for row in self.connection.execute(sql, (archive_id,)))
        derived = {name: self.connection.execute(sql, (archive_id,)).fetchone()[0] for name, sql in (
            ('chunks', 'SELECT COUNT(*) FROM chunks WHERE memory_id=?'),
            ('chunk_vectors', 'SELECT COUNT(*) FROM chunk_vectors WHERE memory_id=?'),
            ('unverified_vectors', 'SELECT COUNT(*) FROM unverified_vectors WHERE memory_id=?'))}
        return {'archive_id': archive_id, 'versions': versions, 'content_sha256': hashes,
                'source_ids': sorted({item['source_id'] for item in versions}),
                'receipts': column('SELECT operation_id FROM archive_receipts WHERE archive_id=?'),
                'hides': column('SELECT operation_id FROM archive_hides WHERE archive_id=?'),
                'successors': links['superseded_by'], 'predecessor': links['supersedes'],
                'hidden': self.hidden_at(archive_id) is not None,
                'identical_content_records': identical, 'derived': derived}

    @staticmethod
    def plan_digest(dependents):
        return hashlib.sha256(json.dumps(dependents, sort_keys=True).encode()).hexdigest()

    def apply_purge(self, archive_id, disposition_id, plan_sha256, operations):
        """Physically remove the record under a disposition the administrator already checked.

        Under BEGIN IMMEDIATE the writer and the dependents are rechecked against plan_sha256, so anything
        that changed after the check refuses instead of being purged unseen. Receipts, source packets and
        the record's archive rows leave in one transaction with the purge receipt and its tombstone keys
        (operations, content hashes, source ids). A repeat with the same disposition returns the stored
        receipt; another disposition is refused. Not an ArchiveAdminAPI by itself: it holds no authority.

        Freed pages of the main database file are zeroed (secure_delete). Older WAL frames until the next
        checkpoint, backups, dumps and the captured source spool are outside this file and are retained."""
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive purge requires an idle connection.')
        self._writer()
        # Every page this purge frees, source packets and receipts included, is overwritten with zeros:
        # secure_delete is on before the first delete and the caller's setting is restored afterwards.
        secure = int(self.connection.execute('PRAGMA secure_delete').fetchone()[0])
        self.connection.execute('PRAGMA secure_delete=1')
        try:
            return self._apply_purge(archive_id, disposition_id, plan_sha256, operations)
        finally:
            self.connection.execute(f'PRAGMA secure_delete={secure}')

    def _apply_purge(self, archive_id, disposition_id, plan_sha256, operations):
        from kelmo.store.errors import ArchiveNotFoundError, ArchiveStorageError
        from kelmo.store.memory import store
        from kelmo.store.memory.delete import purge_rows
        with store._write_lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._writer()
            stored = self.purge_record(archive_id)
            if stored is not None:
                if stored['disposition_id'] != disposition_id:
                    raise KelmoError('E_IDEMPOTENCY', 'This record was purged under a different disposition.')
                return OperationReceipt.from_dict(stored['receipt']), stored['effects'], True
            dependents = self._dependents(archive_id)
            if self.plan_digest(dependents) != plan_sha256:
                raise KelmoError('E_CONFLICT', 'The record or its dependents changed after the disposition was '
                                'checked; inspect the purge plan again.')
            effects = {}
            for table in ('archive_hides', 'archive_receipts', 'archive_version_sources',
                          'archive_sources'):
                effects[table] = self.connection.execute(f'DELETE FROM {table} WHERE archive_id=?',
                                                         (archive_id,)).rowcount
            try:
                effects.update(purge_rows(self.connection, archive_id))
            except (ArchiveNotFoundError, ArchiveStorageError) as error:
                raise KelmoError('E_CAPABILITY', f'Purge refused by the archive: {error}') from error
            # What the administrator reports and a replay reconciles, kept with the receipt.
            effects.update(journal_operations=sorted(operations), content_sha256=dependents['content_sha256'],
                           identical_content_records=dependents['identical_content_records'],
                           links_removed={'successors': dependents['successors'],
                                          'predecessor': dependents['predecessor']})
            refs = tuple(EvidenceRef(archive_id, item['version'], item['content_sha256'], item['source_id'])
                         for item in dependents['versions'])
            receipt = OperationReceipt('purge/' + disposition_id, disposition_id, refs[-1].source_id,
                                       'purged', refs)
            self.connection.execute('INSERT INTO archive_purges VALUES (?,?,?,?,?,?)',
                                    (archive_id, disposition_id, plan_sha256, self._timestamp(),
                                     json.dumps(receipt.to_dict(), sort_keys=True),
                                     json.dumps(effects, sort_keys=True)))
            keys = ([('operation', key) for key in sorted(set(operations) | set(dependents['receipts'])
                                                          | set(dependents['hides']))]
                    + [('content', value) for value in dependents['content_sha256']]
                    + [('source', value) for value in dependents['source_ids']])
            self.connection.executemany('INSERT OR IGNORE INTO archive_purged_keys VALUES (?,?,?)',
                                        [(kind, value, archive_id) for kind, value in keys])
        return receipt, effects, False

    def _require_index(self):
        if self.chunk_index is None:
            raise KelmoError('E_CAPABILITY', 'No [embedding] chunk index is configured for this vault.')
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Index maintenance requires an idle connection.')
        self._writer()
        return self.chunk_index

    def refuse_hidden_target(self, archive_id):
        """A target must be visible and not yet corrected; a linked successor counts even before its hide lands."""
        from . import supersession, visibility
        hidden = visibility.is_forgotten(self.connection, archive_id)
        if not hidden and not supersession.links(self.connection, archive_id)['superseded_by']:
            return
        current = supersession.head(self.connection, archive_id)
        if current != archive_id:
            raise KelmoError('E_CONFLICT', f'Record {archive_id} is already superseded; the current record of this chain is {current}. Supersede that one.')
        if hidden:
            raise KelmoError('E_CONFLICT', f'Record {archive_id} is forgotten; store the correction as a new record.')
        raise KelmoError('E_CONFLICT', f'Record {archive_id} is part of a supersession cycle; its links need repair.')

    def _refuse_cycle(self, archive_id, superseded_id):
        """Refuse a link whose target already replaces, directly or through a chain, the new record."""
        from . import supersession
        seen, current = set(), superseded_id
        while current is not None and current not in seen:
            if current == archive_id:
                raise KelmoError('E_CONFLICT', f'Record {superseded_id} already replaces {archive_id}; '
                                'a reverse supersession would create a cycle.')
            seen.add(current)
            current = supersession.links(self.connection, current)['supersedes']

    def supersede(self, archive_id, superseded_id, origin):
        from . import supersession, visibility
        self._origin(origin)
        self._row(archive_id)
        self._row(superseded_id)
        if archive_id == superseded_id:
            raise KelmoError('E_INPUT', 'A record cannot supersede itself; its content is already that record.')
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive mutation requires an idle connection.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._origin(origin)
            self._row(archive_id)
            self._row(superseded_id)
            if visibility.is_forgotten(self.connection, archive_id):
                raise KelmoError('E_CONFLICT', 'This source is a forgotten record; store the correction as new text.')
            linked = supersession.links(self.connection, archive_id)['supersedes']
            if linked is None:
                # Rechecked under the write lock: a competing correction may have linked first.
                self.refuse_hidden_target(superseded_id)
                self._refuse_cycle(archive_id, superseded_id)
            elif linked != superseded_id:
                raise KelmoError('E_CONFLICT', 'This record already supersedes a different record.')
            self.connection.execute('INSERT INTO record_supersession (id, supersedes, created_at) VALUES (?,?,?) '
                                    'ON CONFLICT(id) DO NOTHING', (archive_id, superseded_id, self._timestamp()))

    def update_receipt(self, receipt, origin):
        """The version an earlier update under this receipt identity created, or None.

        `receipt` is (operation_id, request fingerprint). The receipt is written in the same transaction as
        the version it names, so a found receipt proves this request's effect, not merely equal content; a
        different fingerprint under the same identity is refused."""
        operation_id, fingerprint = receipt
        self._origin(origin)
        row = self.connection.execute('SELECT * FROM archive_receipts WHERE operation_id=?',
                                      (operation_id,)).fetchone()
        if row is None:
            return None
        if row['fingerprint'] != 'update:' + fingerprint:
            raise KelmoError('E_IDEMPOTENCY', 'This update identity already names a different request.')
        ref = EvidenceRef(row['archive_id'], row['version'], row['content_sha256'], row['source_id'])
        self.verify_receipt(ref, operation_id, origin)
        return ref

    def update(self, archive_id, expected_version, content, origin, *, receipt=None):
        """Same-ID update under an expected version. With `receipt` (operation_id, fingerprint) the new
        version and its receipt commit together, and a repeat of that request returns the version it
        created instead of failing the version check it already advanced."""
        from kelmo.store.db import now_iso
        from kelmo.store.errors import ArchiveConflictError, ArchiveEmbeddingError, ArchiveValidationError
        from kelmo.store.memory import store
        from kelmo.store.memory.taxonomy import validate_kind_domain
        from kelmo.store.memory.update import check_version, replace_current
        self._origin(origin)
        if self.connection.in_transaction:
            raise KelmoError('E_STATE', 'Archive mutation requires an idle connection.')
        if not isinstance(content, SourcePacket) or type(expected_version) is not int or expected_version < 1:
            raise KelmoError('E_INPUT', 'Update requires a source packet and positive expected version.')
        if receipt is not None:
            done = self.update_receipt(receipt, origin)
            if done is not None:
                return done
        validate_kind_domain(content.kind, content.domain)
        resolved_validity = store.resolve_valid_from(content.valid_from, content.source_kind, content.filename)
        # Checked before inference to spare it, and again under the write lock below.
        self._refuse_purged(content)
        initial = self.public_record(archive_id)
        if initial['version'] != expected_version:
            raise KelmoError('E_CONFLICT', 'Expected version differs from the current archive version.')
        text_changed = initial['content'].encode() != content.content
        vector = prepared = None
        if text_changed and self.chunk_index is not None:
            # The replacement chunk generation and its vectors, computed outside the transaction.
            prepared = self.chunk_index.prepare(content.content.decode('utf-8'),
                                                require_vectors=content.require_vectors)
        elif text_changed:
            try:
                vector = store.embed(content.content.decode('utf-8'), cache_dir=self.cache_dir)
            except ArchiveEmbeddingError:
                if content.require_vectors:
                    raise
        try:
            with store._write_lock, self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                self._origin(origin)
                # A purge may have committed while this update embedded.
                self._refuse_purged(content)
                if receipt is not None:
                    # The same request may have committed while this attempt prepared vectors.
                    done = self.update_receipt(receipt, origin)
                    if done is not None:
                        return done
                row = self.public_record(archive_id)
                # This is required even when the optimistic read looked unchanged.
                check_version(row, expected_version)
                old = self._version(row)
                if old.source == content and old.origin == origin:
                    if content.require_vectors and old.vectors_pending:
                        raise ArchiveEmbeddingError('Update requires indexed vectors; current vectors are pending.')
                    return old.evidence
                wanted = dict(content=content.content.decode('utf-8'), kind=content.kind,
                              domain=content.domain, project=content.project,
                              tags_json=json.dumps(content.tags), valid_from=resolved_validity,
                              source=content.source_kind, machine_tag=origin.machine_tag)
                changes = {key: value for key, value in wanted.items() if row[key] != value}
                pending = (vector is None) if text_changed else old.vectors_pending
                if content.require_vectors and pending and not (text_changed and self.chunk_index is not None):
                    raise ArchiveEmbeddingError('Metadata-only update cannot admit pending vectors; backfill is required.')
                stamp = now_iso()
                self._save_version(row, old)
                # A text change retires the old chunk generation (unknown vector bytes are kept first);
                # metadata-only revisions keep chunks and vectors, which then answer for the new version.
                replace_current(self.connection, row, changes, stamp,
                                content_changed=text_changed, vector=vector)
                if prepared is not None:
                    self.chunk_index.admit(self.connection, archive_id, prepared)
                    pending = self._head_pending(archive_id, None)
                    if content.require_vectors and pending:
                        raise ArchiveEmbeddingError('Update requires indexed vectors for the replacement text.')
                head = self._row(archive_id)
                ref = EvidenceRef(archive_id, head['version'], content.content_sha256, content.source_id)
                self._save_version(head, ArchiveVersion(ref, content, origin, stamp, pending))
                self.connection.execute('''INSERT INTO archive_sources VALUES (?,?,?,?,?)
                    ON CONFLICT(archive_id) DO UPDATE SET source_id=excluded.source_id,
                    source_packet=excluded.source_packet,origin=excluded.origin,
                    vectors_pending=excluded.vectors_pending''',
                    (archive_id, content.source_id, json.dumps(content.to_dict(), sort_keys=True),
                     self._origin_json(origin), int(pending)))
                if receipt is not None:
                    self.connection.execute('INSERT INTO archive_receipts VALUES (?,?,?,?,?,?,?)',
                                            (receipt[0], ref.archive_id, ref.version, ref.content_sha256,
                                             ref.source_id, self._origin_json(origin), 'update:' + receipt[1]))
                return ref
        except ArchiveConflictError as error:
            raise KelmoError('E_CONFLICT', str(error)) from error
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error

    def history(self, archive_id, cursor, limit):
        """Replaced versions, ascending, with a cursor pinned to the initial upper version.

        Current is read with get(), including its explicit version. New updates
        never move an existing page boundary or append to an in-flight history.
        """
        with self._read_snapshot():
            return self._history(archive_id, cursor, limit)

    def _history(self, archive_id, cursor, limit):
        row = self.public_record(archive_id)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise KelmoError('E_INPUT', 'History limit must be an integer in 1..500.')
        after, upper = 0, row['version'] - 1
        if cursor is not None:
            try:
                token = json.loads(cursor)
                if not isinstance(token, list) or len(token) != 4 or token[:2] != ['history/1', archive_id]:
                    raise ValueError('Cursor belongs to another history.')
                after, upper = token[2:]
                if (type(after) is not int or type(upper) is not int or
                        not 0 <= after <= upper < row['version']):
                    raise ValueError('Invalid cursor bounds.')
            except (TypeError, ValueError) as error:
                raise KelmoError('E_INPUT', 'Invalid archive history cursor.') from error
        count = self.connection.execute('''SELECT COUNT(*) FROM memory_versions
            WHERE memory_id=? AND version BETWEEN 1 AND ?''', (archive_id, upper)).fetchone()[0]
        if count != upper:
            raise KelmoError('E_CAPABILITY', 'Stored replaced history is incomplete; no versions are fabricated.')
        rows = self.connection.execute('''SELECT version FROM memory_versions
            WHERE memory_id=? AND version>? AND version<=? ORDER BY version LIMIT ?''',
            (archive_id, after, upper, limit + 1)).fetchall()
        selected = rows[:limit]
        versions = tuple(self._version(self._exact_row(archive_id, item['version'])) for item in selected)
        next_cursor = (json.dumps(['history/1', archive_id, selected[-1]['version'], upper])
                       if len(rows) > limit else None)
        return ArchiveHistory(versions, next_cursor)

    def index_generation(self):
        """The chunk-index generation this archive serves now: `chunk:<profile_id>` with [embedding], else
        the record index. Old-shaped chunk reads bind their locator to it; nothing is guessed."""
        if self.chunk_index is None:
            return RECORD_INDEX
        from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
        try:
            return self.chunk_index.generation()
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            raise KelmoError('E_CAPABILITY', f'The configured chunk index is unavailable ({_reason(error)}).') from error

    def expand(self, locator, neighbors):
        if self.chunk_index is not None:
            return self._expand_chunks(locator, neighbors)
        with self._read_snapshot():
            return self._expand(locator, neighbors)

    def _expand_chunks(self, locator, neighbors):
        """A window of the configured chunk generation, resolved against the exact profile, version and
        content hash the locator names (expand_window reads them in one snapshot). A locator of another
        generation, a replaced version (a metadata revision included) or changed text is refused."""
        from kelmo.store.errors import (ArchiveConflictError, ArchiveEmbeddingError, ArchiveNotFoundError,
                                    ArchiveValidationError)
        from kelmo.store.memory.chunk import expand_window
        from . import visibility
        if not isinstance(locator, ChunkLocator) or type(neighbors) is not int:
            raise KelmoError('E_INPUT', 'Expand requires an exact chunk locator and integer neighbors.')
        try:
            generation = self.chunk_index.generation()
        except (ArchiveEmbeddingError, ArchiveValidationError) as error:
            raise KelmoError('E_CAPABILITY', f'The configured chunk index is unavailable ({_reason(error)}).') from error
        if locator.index_generation != generation:
            raise KelmoError('E_CAPABILITY', 'Chunk locator names an index generation this vault does not serve; '
                            'search again.')
        try:
            window = expand_window(self.connection, memory_id=locator.archive_id, chunk_idx=locator.chunk_index,
                                   neighbors=neighbors, profile_id=generation[len(CHUNK_GENERATION):],
                                   content_sha256_expected=locator.content_sha256)
        except ArchiveNotFoundError as error:
            if visibility.is_forgotten(self.connection, locator.archive_id):
                raise KelmoError('E_FORGOTTEN', 'This record was forgotten; it is hidden from search and expand.') from error
            raise KelmoError('E_NOT_FOUND', str(error)) from error
        except ArchiveConflictError as error:
            raise KelmoError('E_CONFLICT', 'Chunk locator is stale; read its exact full version instead.') from error
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        if window['version'] != locator.version:
            raise KelmoError('E_CONFLICT', 'Chunk locator names a replaced version; search again.')
        return ChunkWindow(locator, window['chunks'][0]['chunk_idx'],
                           tuple(chunk['text'] for chunk in window['chunks']))

    def _expand(self, locator, neighbors):
        from kelmo.store.errors import ArchiveValidationError
        from kelmo.store.memory.get import memory_get
        if not isinstance(locator, ChunkLocator) or type(neighbors) is not int:
            raise KelmoError('E_INPUT', 'Expand requires an exact chunk locator and integer neighbors.')
        row = self.public_record(locator.archive_id)
        ref = self._ref(row)
        if (locator.version, locator.content_sha256) != (ref.version, ref.content_sha256):
            raise KelmoError('E_CONFLICT', 'Chunk locator is stale; read its exact full version instead.')
        if locator.index_generation != 'core-record-index/1':
            raise KelmoError('E_CAPABILITY', 'Chunk locator names an unavailable index generation.')
        try:
            window = memory_get(self.connection, id=locator.archive_id,
                                chunk_idx=locator.chunk_index, neighbors=neighbors)
        except ArchiveValidationError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        # Recheck the identity of the row read with its chunks, not just the
        # optimistic locator read before it.
        if (window['version'], hashlib.sha256(row['content'].encode()).hexdigest()) != (
                locator.version, locator.content_sha256):
            raise KelmoError('E_CONFLICT', 'Chunk locator changed while reading its window.')
        chunks = window['chunks']
        return ChunkWindow(locator, chunks[0]['chunk_idx'], tuple(chunk['text'] for chunk in chunks))
