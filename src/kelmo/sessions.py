"""Durable session snapshots and idempotent extraction-job intake.

A provider transcript only grows, so a snapshot that extends the previous snapshot of its generation
stores only the bytes after the complete records the two share. session_snapshot_storage records that
base and the digest of the snapshot's own complete records, so the next capture proves it extends a
snapshot without reading it back. A snapshot's bytes are rebuilt from its chain and checked against its
identity. A transcript has no size ceiling.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from .errors import KelmoError
from .journal import Journal, now
from .session_transcripts import _check_deadline, parse
from .wiki import engine


# One row per snapshot captured since this table exists: the digest of its first complete_bytes, and the
# snapshot whose first base_bytes precede its own file (both NULL for a whole file). An older snapshot
# has no row and is a whole file.
SNAPSHOT_STORAGE = '''CREATE TABLE IF NOT EXISTS session_snapshot_storage (
    hash TEXT PRIMARY KEY REFERENCES session_snapshots(hash), complete_sha256 TEXT NOT NULL,
    base_hash TEXT REFERENCES session_snapshots(hash), base_bytes INTEGER)'''
COMPARE_BYTES = 1048576


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _digests(raw, boundaries):
    """The digest of raw[:b] for each boundary and of all of raw, in one pass."""
    view, digests, state, position = memoryview(raw), {}, hashlib.sha256(), 0
    for boundary in sorted(set(boundaries)):
        state.update(view[position:boundary])
        digests[boundary], position = state.hexdigest(), boundary
    state.update(view[position:])
    digests[len(raw)] = state.hexdigest()
    return digests


def _snapshot_file(config, checksum):
    path = config.internal('.state/sessions/transcripts/' + checksum + '.jsonl')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise KelmoError('E_STATE', 'Session snapshot must be a regular file.')
        with os.fdopen(descriptor, 'rb') as stream:
            return stream.read()
    except OSError as error:
        raise KelmoError('E_STATE', 'Session snapshot is missing or unreadable.') from error


def read_snapshot(config, connection, checksum):
    """The exact bytes of one captured snapshot, verified against its identity.

    A snapshot with a base is its base's first `base_bytes` bytes followed by its own file; one without
    is a whole file. `connection` may be read-only; a journal without the storage table has only whole
    snapshots.
    """
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise KelmoError('E_STATE', 'Invalid session snapshot identity.')
    chained = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='session_snapshot_storage'").fetchone()
    chain, seen, current = [], set(), checksum
    while True:
        if current in seen:
            raise KelmoError('E_STATE', 'Session snapshot bases form a cycle.')
        seen.add(current)
        row = connection.execute('SELECT base_hash, base_bytes FROM session_snapshot_storage '
                                 'WHERE hash=? AND base_hash IS NOT NULL', (current,)).fetchone() if chained else None
        chain.append((current, None if row is None else row[1]))
        if row is None:
            break
        current = row[0]
    data = bytearray()
    for name, base_bytes in reversed(chain):
        if base_bytes is not None:
            if type(base_bytes) is not int or not 0 <= base_bytes <= len(data):
                raise KelmoError('E_STATE', 'Session snapshot base is longer than its base snapshot.')
            del data[base_bytes:]
        data += _snapshot_file(config, name)
    if digest(data) != checksum:
        raise KelmoError('E_STATE', 'Session snapshot no longer matches its identity.')
    return bytes(data)


class Sessions:
    def __init__(self, config):
        self.config = config
        self.journal = Journal(config.internal('.state/journal.sqlite3'))
        try:
            with self._lock():
                self._migrate()
                self._processing_schema()
                with self.journal.conn as db:
                    db.execute('''CREATE TABLE IF NOT EXISTS session_capture_origins (
                        session_key TEXT PRIMARY KEY REFERENCES sessions(session_key),
                        client_id TEXT NOT NULL)''')
                    db.execute(SNAPSHOT_STORAGE)
        except BaseException:
            self.journal.close()
            raise

    def close(self):
        self.journal.close()

    @contextmanager
    def _lock(self):
        path = self.config.internal('.state/sessions.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise KelmoError('E_STATE', 'Session lock must be a regular file.')
        with os.fdopen(fd, 'a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'Another session intake is active; retry capture.') from error
            yield

    @contextmanager
    def _capture_transaction(self, deadline):
        db = self.journal.conn
        _check_deadline(deadline)
        previous_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
        remaining_ms = previous_timeout if deadline is None else max(1, int((deadline - time.monotonic()) * 1000))
        db.execute(f'PRAGMA busy_timeout={remaining_ms}')
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield
                _check_deadline(deadline)
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise KelmoError('E_BUSY', 'Session journal is busy; retry capture.') from error
            raise
        finally:
            db.execute(f'PRAGMA busy_timeout={previous_timeout}')

    def _migrate(self):
        connection = self.journal.conn
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'session_schema' in tables:
            versions = [row[0] for row in connection.execute('SELECT version FROM session_schema')]
            if versions not in ([1], [2]) or not {'sessions', 'session_snapshots', 'session_jobs'} <= tables:
                raise KelmoError('E_STATE', 'Unsupported or incomplete session schema.')
            return
        if tables & {'sessions', 'session_snapshots', 'session_jobs'}:
            raise KelmoError('E_STATE', 'Unversioned session tables cannot be adopted.')
        if not {'operations', 'events', 'claims'} <= tables:
            raise KelmoError('E_STATE', 'Session migration requires an existing operation journal.')
        backups = self.config.internal('.state/backups')
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = self.config.internal('.state/backups/sessions-v1-' + uuid.uuid4().hex + '.sqlite3')
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(backup)
        try:
            connection.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise KelmoError('E_STATE', 'Session migration backup failed integrity checking.')
        finally:
            target.close()
        engine.sync_file(backup)
        engine.sync_directory(backups)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE session_schema (version INTEGER NOT NULL)')
            connection.execute('INSERT INTO session_schema VALUES (1)')
            connection.execute('''CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY, provider TEXT NOT NULL, native_id TEXT NOT NULL,
                generation INTEGER NOT NULL, latest_hash TEXT NOT NULL, file_identity TEXT NOT NULL,
                scheduled_record INTEGER NOT NULL, scheduled_turns INTEGER NOT NULL)''')
            connection.execute('''CREATE TABLE session_snapshots (
                hash TEXT PRIMARY KEY, size INTEGER NOT NULL, records INTEGER NOT NULL,
                complete_bytes INTEGER NOT NULL, partial_tail INTEGER NOT NULL)''')
            connection.execute('''CREATE TABLE session_jobs (
                id TEXT PRIMARY KEY, session_key TEXT NOT NULL REFERENCES sessions(session_key),
                generation INTEGER NOT NULL, task TEXT NOT NULL,
                start_record INTEGER NOT NULL, end_record INTEGER NOT NULL,
                snapshot_hash TEXT NOT NULL REFERENCES session_snapshots(hash),
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(session_key,generation,task,end_record))''')

    def _processing_schema(self):
        db = self.journal.conn
        if db.execute('SELECT version FROM session_schema').fetchone()[0] == 2:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='session_processing'").fetchone():
                raise KelmoError('E_STATE', 'Session processing schema is missing.')
            return
        backup = self.config.internal('.state/backups/sessions-v2-' + uuid.uuid4().hex + '.sqlite3')
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(backup)
        try:
            db.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise KelmoError('E_STATE', 'Session migration backup failed.')
        finally:
            target.close()
        engine.sync_file(backup)
        engine.sync_directory(backup.parent)
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE session_processing (
                job_id TEXT PRIMARY KEY REFERENCES session_jobs(id), chunks TEXT NOT NULL DEFAULT '[]',
                result TEXT, archive_operation TEXT, proposal TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                eligible REAL NOT NULL DEFAULT 0, error_code TEXT)''')
            db.execute('UPDATE session_schema SET version=2')

    def snapshot(self, checksum):
        return read_snapshot(self.config, self.journal.conn, checksum)

    def _save_snapshot(self, checksum, raw, base):
        """Write the file of a snapshot the journal does not record yet: raw after its base's bytes, or all
        of it. A file an interrupted capture left behind belongs to no recorded snapshot and is replaced."""
        directory = self.config.internal('.state/sessions/transcripts')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.config.internal('.state/sessions/transcripts/' + checksum + '.jsonl')
        temporary = self.config.internal('.state/sessions/transcripts/.stage-' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(memoryview(raw)[0 if base is None else base[1]:])
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            engine.sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _extends(self, raw, previous, boundary, digests):
        """Whether raw starts with the first `boundary` bytes (the complete records) of snapshot `previous`."""
        row = self.journal.conn.execute('SELECT complete_sha256 FROM session_snapshot_storage WHERE hash=?',
                                        (previous,)).fetchone()
        if row is not None:
            return digests[boundary] == row['complete_sha256']
        old = self.snapshot(previous)  # captured before prefix digests were recorded
        return all(raw[at:min(at + COMPARE_BYTES, boundary)] == old[at:min(at + COMPARE_BYTES, boundary)]
                   for at in range(0, boundary, COMPARE_BYTES))

    def capture(self, provider, native_id, source, source_root, event, *,
                checkpoint_turns=30, time_budget=1.5, capture_client_id=None):
        """Snapshot one transcript and queue the job its event earns.

        `time_budget` bounds a capture that runs inside a provider hook. None leaves it unbounded: the
        server intake, which no hook waits for, captures a transcript of any length.
        """
        # Constructor-side server intake supplies this identity from its channel, never from a native
        # payload. Local callers leave it absent, including local sessions with a client-looking name.
        if capture_client_id is not None and (not isinstance(capture_client_id, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', capture_client_id)):
            raise KelmoError('E_ORIGIN', 'Invalid capturing client identity.')
        if (provider not in ('claude', 'codex') or event not in ('Stop', 'PreCompact', 'SessionEnd')
                or not isinstance(native_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', native_id)):
            raise KelmoError('E_INPUT', 'Invalid provider, session identity or lifecycle event.')
        if (type(checkpoint_turns) is not int or not 1 <= checkpoint_turns <= 1000
                or time_budget is not None and (isinstance(time_budget, bool)
                                                or not isinstance(time_budget, (int, float))
                                                or not 0 < time_budget <= 10)):
            raise KelmoError('E_INPUT', 'Invalid session capture limits.')
        deadline = None if time_budget is None else time.monotonic() + time_budget
        source, root = Path(source).expanduser().absolute(), Path(source_root).expanduser().absolute()
        if (not source.is_relative_to(root) or source.suffix != '.jsonl'
                or '..' in source.parts or '..' in root.parts or not root.is_dir()
                or any(p.is_symlink() for p in (source, *source.parents))):
            raise KelmoError('E_INPUT', 'Transcript must be a regular JSONL file inside the enrolled source root.')
        with self._lock():
            try:
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    os.close(fd)
                    raise KelmoError('E_INPUT', 'Transcript is not a regular file.')
                with os.fdopen(fd, 'rb') as stream:
                    pieces, remaining = [], info.st_size
                    while remaining:
                        _check_deadline(deadline)
                        piece = stream.read(min(65536, remaining))
                        if not piece:
                            raise KelmoError('E_CAPTURE_RETRY', 'Transcript changed while reading; retry capture.')
                        pieces.append(piece)
                        remaining -= len(piece)
                    after = os.fstat(stream.fileno())
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise KelmoError('E_CAPTURE_RETRY', 'Transcript changed while reading; retry capture.')
            except OSError as error:
                raise KelmoError('E_INPUT', 'Transcript could not be opened for capture.') from error
            raw = b''.join(pieces)
            parsed = parse(provider, raw, deadline=deadline)
            key = digest(json.dumps([self.config.vault_id, self.config.label, provider, native_id]).encode())
            identity = f'{info.st_dev}:{info.st_ino}'
            db = self.journal.conn
            previous = db.execute('SELECT * FROM sessions WHERE session_key=?', (key,)).fetchone()
            bound = db.execute('SELECT client_id FROM session_capture_origins WHERE session_key=?',
                               (key,)).fetchone()
            if previous and (bound['client_id'] if bound else None) != capture_client_id:
                raise KelmoError('E_ORIGIN', 'Session capture provenance differs or was never recorded; '
                                            'an existing session cannot be attributed to another origin.')
            old = previous and db.execute('SELECT * FROM session_snapshots WHERE hash=?',
                                          (previous['latest_hash'],)).fetchone()
            boundary = old['complete_bytes'] if old else None
            digests = _digests(raw, [parsed['complete_bytes']] + ([boundary] if old and boundary <= len(raw) else []))
            checksum = digests[len(raw)]
            generation, start, turns, base = 1, 0, 0, None
            if previous:
                generation = previous['generation']
                if (identity == previous['file_identity'] and boundary <= len(raw)
                        and self._extends(raw, previous['latest_hash'], boundary, digests)):
                    start, turns = previous['scheduled_record'], previous['scheduled_turns']
                    base = (previous['latest_hash'], boundary)
                else:
                    generation += 1
            _check_deadline(deadline)
            recorded = bool(db.execute('SELECT 1 FROM session_snapshots WHERE hash=?', (checksum,)).fetchone())
            if not recorded:
                self._save_snapshot(checksum, raw, base)
            _check_deadline(deadline)
            task = 'final' if event == 'SessionEnd' else 'checkpoint'
            eligible = bool(parsed['messages']) and (
                event != 'Stop' or parsed['user_turns'] - turns >= checkpoint_turns)
            job_id = None
            with self._capture_transaction(deadline):
                db.execute('''INSERT OR IGNORE INTO session_snapshots VALUES (?,?,?,?,?)''',
                           (checksum, len(raw), parsed['records'], parsed['complete_bytes'], int(parsed['partial_tail'])))
                if not recorded:
                    db.execute('INSERT OR IGNORE INTO session_snapshot_storage VALUES (?,?,?,?)',
                               (checksum, digests[parsed['complete_bytes']], *(base or (None, None))))
                db.execute('''INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_key) DO UPDATE SET generation=excluded.generation,
                    latest_hash=excluded.latest_hash,file_identity=excluded.file_identity,
                    scheduled_record=excluded.scheduled_record,scheduled_turns=excluded.scheduled_turns''',
                           (key, provider, native_id, generation, checksum, identity, start, turns))
                if not previous and capture_client_id is not None:
                    db.execute('INSERT INTO session_capture_origins VALUES (?,?)', (key, capture_client_id))
                existing = db.execute('''SELECT id FROM session_jobs
                    WHERE session_key=? AND generation=? AND task=? AND end_record=?''',
                                      (key, generation, task, parsed['records'])).fetchone()
                if existing:
                    job_id = existing['id']
                elif eligible:
                    job_id = str(uuid.uuid4())
                    db.execute('''INSERT INTO session_jobs
                        (id,session_key,generation,task,start_record,end_record,snapshot_hash,state,created_at)
                        VALUES (?,?,?,?,?,?,?,'pending',?)''',
                               (job_id, key, generation, task, start, parsed['records'], checksum, now()))
                    db.execute('UPDATE sessions SET scheduled_record=?,scheduled_turns=? WHERE session_key=?',
                               (parsed['records'], parsed['user_turns'], key))
            return {'session_key': key, 'generation': generation, 'snapshot_hash': checksum,
                    'records': parsed['records'], 'partial_tail': parsed['partial_tail'], 'job_id': job_id,
                    'state': 'queued' if job_id else 'captured'}

    def job(self, identity):
        row = self.journal.conn.execute('SELECT * FROM session_jobs WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KelmoError('E_NOT_FOUND', 'Session job not found.')
        return dict(row)

    def list_jobs(self):
        return [dict(row) for row in self.journal.conn.execute('SELECT * FROM session_jobs ORDER BY created_at,id')]
