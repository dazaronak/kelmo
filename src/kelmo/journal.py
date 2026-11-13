"""Durable operations and metadata-only stage history."""
import contextvars
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from .errors import KelmoError, sqlite_busy

BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


JOURNAL_WAIT_SECONDS = 30
WHEN_FREE_TRIES = 20


def when_free(write, *, tries=WHEN_FREE_TRIES, sleep=time.sleep):
    """Run a write that must land (an attempt refund, a wait marker) until the database is free.

    Each try already waits JOURNAL_WAIT_SECONDS for the lock; a refund lost to a busy journal would
    charge a note for another writer's turn, so it is tried again instead of given up."""
    for attempt in range(tries):
        try:
            return write()
        except sqlite3.Error as error:
            if not sqlite_busy(error) or attempt == tries - 1:
                raise
            sleep(1)


def now():
    return datetime.now(UTC).isoformat()


def _boot_id():
    try:
        return BOOT_ID.read_text(encoding='ascii').strip() or None
    except OSError:
        return None


_TRACKED_CLAIMS = contextvars.ContextVar('kelmo_tracked_claims', default=None)


@contextmanager
def tracked_claims(tokens):
    """Records in `tokens` the token of every claim taken inside the block (a job releases them all when it
    ends, whatever ends it)."""
    reset = _TRACKED_CLAIMS.set(tokens)
    try:
        yield tokens
    finally:
        _TRACKED_CLAIMS.reset(reset)


def claim_owner():
    """This process as a claim owner: host, pid and, where the system names it, the boot."""
    return socket.gethostname(), os.getpid(), _boot_id()


def owner_gone(row):
    """True only when the claim's owner provably no longer runs: same host, and either another boot or
    no process with its pid. An unknown owner (an older row, another host) keeps the lease rule."""
    host, _, boot = claim_owner()
    if row['owner_host'] != host or row['owner_pid'] is None:
        return False
    if row['owner_boot'] and boot and row['owner_boot'] != boot:
        return True
    if row['owner_pid'] == os.getpid():
        return False
    try:
        os.kill(row['owner_pid'], 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class Journal:
    @property
    def conn(self):
        if self.guard is not None:
            self.guard()
        return self._conn

    def __init__(self, path, *, create=False, read_only=False, check_same_thread=True):
        """`read_only`: a reader's connection (SQLite `mode=ro`) that runs no schema work and sees committed
        rows only; a write through it fails. `check_same_thread=False` is for the one writer connection the
        state core's baton hands between threads."""
        if not create and not path.is_file():
            raise KelmoError('E_STATE', 'Operation journal is missing; run kelmo doctor.')
        if read_only and create:
            raise KelmoError('E_STATE', 'A read-only journal cannot create one.')
        self.read_only = read_only
        # Writers wait for each other instead of refusing: a doctor read or a long publication holds
        # the file for seconds, never long enough to justify failing a note.
        if read_only:
            from urllib.parse import quote
            self._conn = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True,
                                        timeout=JOURNAL_WAIT_SECONDS, check_same_thread=check_same_thread)
        else:
            self._conn = sqlite3.connect(path, timeout=JOURNAL_WAIT_SECONDS, check_same_thread=check_same_thread)
        # Called on every use of the connection when set: the state core's baton check in a debug build.
        self.guard = None
        self.conn.row_factory = sqlite3.Row
        self._client_claims = {}  # request key -> claim token held by this connection's attempt
        # The archive writer check a Service binds here; stage() runs it inside its own transaction, so a
        # writer that lost its epoch cannot move an operation to another stage. None for a bare journal.
        self.writer_check = None
        self.conn.execute('PRAGMA foreign_keys=ON')
        if read_only:
            return
        self.conn.execute('PRAGMA synchronous=FULL')
        if create:
            self.conn.executescript('''
                CREATE TABLE operations (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    source_hash TEXT NOT NULL, source_path TEXT NOT NULL, filename TEXT NOT NULL,
                    role TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL,
                    terminal INTEGER NOT NULL DEFAULT 0, archive_id TEXT, error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(id),
                    stage TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            self.conn.commit()
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS operation_origins (
                operation_id TEXT PRIMARY KEY REFERENCES operations(id), origin TEXT NOT NULL
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS operation_sources (
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                metadata TEXT NOT NULL, packet_sha256 TEXT NOT NULL
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS claims (
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                token TEXT NOT NULL, expires_at REAL NOT NULL, local_worker INTEGER NOT NULL DEFAULT 0
            )''')
            columns = {row[1] for row in self.conn.execute('PRAGMA table_info(claims)')}
            # The process holding a claim, so a claim whose owner died is taken over instead of waiting out
            # its lease; older rows have no owner and keep the lease rule.
            for column, definition in (('local_worker', 'INTEGER NOT NULL DEFAULT 0'), ('owner_host', 'TEXT'),
                                       ('owner_pid', 'INTEGER'), ('owner_boot', 'TEXT')):
                if column in columns:
                    continue
                try:
                    self.conn.execute(f'ALTER TABLE claims ADD COLUMN {column} {definition}')
                except sqlite3.OperationalError as error:
                    if 'duplicate column' not in str(error):
                        raise
            # StateAPI requests: one row per origin-scoped idempotency key, an immutable prepared
            # intent before publication, and accepted history only after its generation committed.
            self.conn.execute('''CREATE TABLE IF NOT EXISTS action_requests (
                operation_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL, origin TEXT NOT NULL,
                source_operation_id TEXT NOT NULL REFERENCES operations(id), observation TEXT,
                state TEXT NOT NULL, error_code TEXT, error_message TEXT, receipt TEXT,
                created_at TEXT NOT NULL
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS action_intents (
                operation_id TEXT PRIMARY KEY REFERENCES action_requests(operation_id),
                intent TEXT NOT NULL, prepared_at TEXT NOT NULL
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS action_history (
                action_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                operation_id TEXT NOT NULL UNIQUE REFERENCES action_requests(operation_id),
                accepted_event TEXT NOT NULL, PRIMARY KEY (action_id, sequence)
            )''')
            # Named purge dispositions an operator authorized: one record, the plan hash reviewed and one action
            # per dependent. Settlement writes the state and the purge receipt in one transaction.
            self.conn.execute('''CREATE TABLE IF NOT EXISTS purge_dispositions (
                disposition_id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, operator TEXT NOT NULL,
                archive_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, actions TEXT NOT NULL,
                authorized_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('authorized', 'settled')),
                settled_at TEXT, receipt TEXT
            )''')
            # A purge executing its disposition: the operations it will stage purged. While the intent is
            # executing, claim refuses them, so no claim can appear between the archive commit and the
            # journal half; settlement marks it finished in the same transaction as the disposition.
            self.conn.execute('''CREATE TABLE IF NOT EXISTS purge_intents (
                disposition_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, operations TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('executing', 'finished')), started_at TEXT NOT NULL
            )''')
            # The archive record an operation replaces once it is archived; journals before it get the column empty.
            if 'supersedes' not in {row[1] for row in self.conn.execute('PRAGMA table_info(operations)')}:
                try:
                    self.conn.execute('ALTER TABLE operations ADD COLUMN supersedes TEXT')
                except sqlite3.OperationalError as error:
                    # Another process opening the same old journal added it first.
                    if 'duplicate column' not in str(error):
                        raise

    def claim(self, operation_id, *, lease_seconds=300, local_worker=False):
        """Local claims require the caller to hold worker.lock until release."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal claim requires an idle connection.')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise KelmoError('E_INPUT', 'Claim duration must be between 1 and 3600 seconds.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            operation = self.get(operation_id)
            if operation['terminal'] or operation['state'] == 'captured' or self._purging(operation_id):
                return None
            timestamp = time.time()
            current = self.conn.execute('SELECT * FROM claims WHERE operation_id=?', (operation_id,)).fetchone()
            if current:
                if current['local_worker']:
                    if not local_worker:
                        return None
                elif current['expires_at'] > timestamp:
                    if not owner_gone(current):
                        return None
                    self.event(operation_id, 'claim_recovered', owner_host=current['owner_host'],
                               owner_pid=current['owner_pid'])
            token = uuid.uuid4().hex
            tracked = _TRACKED_CLAIMS.get()
            if tracked is not None:
                tracked.add(token)
            host, pid, boot = claim_owner()
            self.conn.execute('''INSERT INTO claims(operation_id,token,expires_at,local_worker,owner_host,owner_pid,
                owner_boot) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(operation_id) DO UPDATE SET token=excluded.token,
                expires_at=excluded.expires_at,local_worker=excluded.local_worker,owner_host=excluded.owner_host,
                owner_pid=excluded.owner_pid,owner_boot=excluded.owner_boot''',
                (operation_id, token, timestamp + lease_seconds, int(local_worker), host, pid, boot))
            return token

    def stale_claims(self):
        """Unexpired claims whose owning process is gone: taken over by the next claim, listed by doctor."""
        return [{'operation_id': row['operation_id'], 'owner_host': row['owner_host'], 'owner_pid': row['owner_pid'],
                 'expires_at': row['expires_at']}
                for row in self.conn.execute('SELECT * FROM claims WHERE local_worker=0 AND expires_at>?',
                                             (time.time(),))
                if owner_gone(row)]

    def release(self, operation_id, token):
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal claim release requires an idle connection.')
        with self.conn:
            self.conn.execute('DELETE FROM claims WHERE operation_id=? AND token=?',
                              (operation_id, token))

    def handover_local_claim(self, operation_id):
        """Clear a dead local worker claim while its exclusive worker lock is held."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal claim handover requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            claim = self.conn.execute('SELECT local_worker FROM claims WHERE operation_id=?',
                                      (operation_id,)).fetchone()
            if claim is None:
                return
            if not claim['local_worker']:
                raise KelmoError('E_CLAIM', 'An external attempt still owns this operation.')
            self.conn.execute('DELETE FROM claims WHERE operation_id=?', (operation_id,))

    def get(self, operation_id):
        row = self.conn.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise KelmoError('E_NOT_FOUND', 'Operation not found.')
        result = dict(row)
        result['terminal'] = bool(result['terminal'])
        # A purged operation's archive evidence is gone; that outranks every stage it reached before.
        ranking = {'captured': 0, 'queued': 1, 'archived': 2, 'applied': 3, 'purged': 4}
        stages = [event[0] for event in self.conn.execute(
            'SELECT DISTINCT stage FROM events WHERE operation_id=?', (operation_id,))]
        durable = [stage for stage in stages if stage in ranking]
        result['durable_stage'] = max(durable, key=ranking.get) if durable else 'unknown'
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_outcomes'").fetchone():
            outcome = self.conn.execute('SELECT disposition,reason FROM source_outcomes WHERE operation_id=?',
                                        (operation_id,)).fetchone()
            if outcome is not None:
                result['outcome'] = dict(outcome)
        return result

    def stage(self, operation_id, stage, *, terminal=False, archive_id=None, error_code=None,
              error_type=None, phase=None, claim_token=None, evidence_ref=None, vectors_pending=None,
              outcome=None):
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal stage transition requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if self.writer_check is not None:
                self.writer_check()
            claim = self.conn.execute('SELECT token,expires_at,local_worker FROM claims WHERE operation_id=?',
                                      (operation_id,)).fetchone()
            if (claim_token is not None or claim is not None) and (
                    claim is None or claim['token'] != claim_token
                    or (not claim['local_worker'] and claim['expires_at'] <= time.time())):
                raise KelmoError('E_CLAIM', 'The job claim is missing, expired or belongs to another attempt.')
            self.conn.execute('''UPDATE operations SET state=?, terminal=?,
                archive_id=COALESCE(?,archive_id), error_code=? WHERE id=?''',
                (stage, int(terminal), archive_id, error_code, operation_id))
            self.event(operation_id, stage, archive_id=archive_id, error_code=error_code,
                       error_type=error_type, phase=phase, evidence_ref=evidence_ref, vectors_pending=vectors_pending)
            if outcome is not None:
                from .outcomes import write
                write(self.conn, operation_id, *outcome)
            if terminal:
                self.conn.execute('DELETE FROM claims WHERE operation_id=?', (operation_id,))

    def event(self, operation_id, stage, **details):
        details = {key: value for key, value in details.items() if value is not None}
        self.conn.execute('INSERT INTO events(operation_id,stage,details,created_at) VALUES (?,?,?,?)',
                          (operation_id, stage, json.dumps(details), now()))

    def event_once(self, operation_id, stage, *, check_writer, **details):
        """Reconcile one durable effect without rewriting history or duplicating retries."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal event reconciliation requires an idle connection.')
        details = {key: value for key, value in details.items() if value is not None}
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            check_writer()
            existing = self.conn.execute('SELECT details FROM events WHERE operation_id=? AND stage=?',
                                         (operation_id, stage)).fetchall()
            if existing:
                if any(json.loads(row['details']) != details for row in existing):
                    raise KelmoError('E_IDEMPOTENCY', 'Existing journal event identifies a different effect.')
                return False
            self.event(operation_id, stage, **details)
            return True

    def bind_origin(self, operation_id, origin):
        """Called inside capture's transaction; origin has already been authenticated."""
        self.conn.execute('INSERT INTO operation_origins VALUES (?,?)',
                          (operation_id, json.dumps(origin.to_dict(), sort_keys=True)))

    def origin_record(self, operation_id):
        row = self.conn.execute('SELECT origin FROM operation_origins WHERE operation_id=?',
                                (operation_id,)).fetchone()
        return json.loads(row['origin']) if row else None

    def bind_source(self, operation_id, metadata, packet_sha256):
        """Bind original metadata inside the source capture transaction."""
        self.conn.execute('INSERT INTO operation_sources VALUES (?,?,?)',
                          (operation_id, json.dumps(metadata, sort_keys=True), packet_sha256))

    def source_metadata(self, operation_id):
        row = self.conn.execute('SELECT metadata,packet_sha256 FROM operation_sources WHERE operation_id=?',
                                (operation_id,)).fetchone()
        if row is None:
            return None
        return {'metadata': json.loads(row['metadata']), 'packet_sha256': row['packet_sha256']}

    def evidence_record(self, operation_id):
        """Most recent exact archived receipt; older journals may have only archive_id."""
        for row in self.conn.execute('SELECT details FROM events WHERE operation_id=? AND stage=? ORDER BY sequence DESC',
                                     (operation_id, 'archived')):
            ref = json.loads(row['details']).get('evidence_ref')
            if ref is not None:
                return ref
        return None

    def trace(self, operation_id):
        operation = self.get(operation_id)
        events = [dict(row) for row in self.conn.execute(
            'SELECT sequence,stage,details,created_at FROM events WHERE operation_id=? ORDER BY sequence',
            (operation_id,))]
        for event in events:
            event['details'] = json.loads(event['details'])
        return {'operation': operation, 'events': events}

    def action_owners(self, archive_id, origin):
        """Archived operations of one issued origin naming this archive record, oldest first.

        Candidates only: the caller compares their exact receipts and verifies one with the archive.
        """
        return [row['id'] for row in self.conn.execute(
            '''SELECT operations.id FROM operations JOIN operation_origins
               ON operation_origins.operation_id=operations.id
               WHERE operations.archive_id=? AND operation_origins.origin=?
               ORDER BY operations.created_at, operations.id''',
            (archive_id, json.dumps(origin.to_dict(), sort_keys=True)))]

    def operation_for_request_key(self, request_key):
        """The captured operation recorded under one derived request key, if any; a read only."""
        row = self.conn.execute('SELECT id FROM operations WHERE request_key=?', (request_key,)).fetchone()
        return row['id'] if row else None

    def action_request(self, request_key):
        row = self.conn.execute('SELECT * FROM action_requests WHERE request_key=?', (request_key,)).fetchone()
        return dict(row) if row else None

    def action_row(self, operation_id):
        row = self.conn.execute('SELECT * FROM action_requests WHERE operation_id=?', (operation_id,)).fetchone()
        return dict(row) if row else None

    def action_requests_of(self, source_operation_ids):
        """Every request row of these sources, under whatever key and in whatever state; a read only."""
        ids = list(source_operation_ids)
        if not ids:
            return []
        return [dict(row) for row in self.conn.execute(
            'SELECT * FROM action_requests WHERE source_operation_id IN (' + ','.join('?' for _ in ids) + ')', ids)]

    def action_begin(self, request_key, kind, fingerprint, origin, source_operation_id, *,
                     operation_id=None, observation=None, error=None, outcome=None):
        """Record a request once; its creation time is the action's single ingestion time.

        A request recorded already refused carries its source outcome in the same transaction.
        """
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal action admission requires an idle connection.')
        operation_id = operation_id or str(uuid.uuid4())
        outcome = self._settled_outcome(outcome)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if self.conn.execute('SELECT 1 FROM action_requests WHERE request_key=?', (request_key,)).fetchone():
                raise KelmoError('E_IDEMPOTENCY', 'This action request was recorded concurrently.')
            self.conn.execute(
                'INSERT INTO action_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (operation_id, request_key, kind, fingerprint, json.dumps(origin.to_dict(), sort_keys=True),
                 source_operation_id, json.dumps(observation, sort_keys=True) if observation is not None else None,
                 'refused' if error else 'open', error[0] if error else None, error[1] if error else None,
                 None, datetime.now(UTC).isoformat(timespec='microseconds')))
            self._source_effects({'operation_id': operation_id, 'kind': kind,
                                  'source_operation_id': source_operation_id}, outcome, None)
        return self.action_request(request_key)

    def _lease_update(self, operation_id, accept, observation):
        """Replace an open request's observation when `accept(current)` holds; one BEGIN IMMEDIATE."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal lease update requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state,observation FROM action_requests WHERE operation_id=?',
                                    (operation_id,)).fetchone()
            current = json.loads(row['observation']) if row and row['observation'] else None
            if row is None or row['state'] != 'open' or not accept(current):
                return False
            self.conn.execute('UPDATE action_requests SET observation=? WHERE operation_id=?',
                              (json.dumps(observation, sort_keys=True), operation_id))
            return True

    def action_lease(self, operation_id, token, expires_at):
        """Take the consulting lease of an open request whose answer is unrecorded and lease lapsed."""
        return self._lease_update(
            operation_id, lambda current: current is not None and 'consulting' in current
            and current['expires_at'] <= time.time(), {'consulting': token, 'expires_at': expires_at})

    def action_renew(self, operation_id, token, expires_at):
        """Extend this attempt's live lease; False when the lease lapsed, moved or the request settled."""
        return self._lease_update(
            operation_id, lambda current: current is not None and current.get('consulting') == token
            and current['expires_at'] > time.time(), {'consulting': token, 'expires_at': expires_at})

    def action_release(self, operation_id, token):
        """End this attempt's lease so the same request can be retried at once."""
        return self._lease_update(
            operation_id, lambda current: current is not None and current.get('consulting') == token,
            {'consulting': token, 'expires_at': 0})

    def action_observe(self, operation_id, token, observation):
        """Record the normalized answer only while this attempt still holds the lease of an open request."""
        return self._lease_update(
            operation_id, lambda current: current is not None and current.get('consulting') == token,
            observation)

    def action_end_attempt(self, operation_id, token):
        """End the attempt of an open request whose recorded answer is a batch's, leaving the `action_release` form.

        `action_release` can only end a lease the caller still holds, and a batch member's row no longer
        holds one: the recorded answer replaced its lease. This is the other half — the row keeps its open
        state and gets back the released-lease observation, so the same request key is retryable at once
        and `action_lease` takes it again. It accepts only a row whose observation carries a batch answer
        with no live lease, so it never releases an attempt another caller holds and never ends a row
        carrying an answer of its own, which its own settle path owns.
        """
        return self._lease_update(
            operation_id, lambda current: current is not None and 'consulting' not in current
            and current.get('batch'), {'consulting': token, 'expires_at': 0})

    def action_history(self, action_id):
        """Accepted events of one action in accepted order; proposals and open intents are absent."""
        return tuple(json.loads(row['accepted_event']) for row in self.conn.execute(
            'SELECT accepted_event FROM action_history WHERE action_id=? ORDER BY sequence', (action_id,)))

    @staticmethod
    def _settled_outcome(outcome):
        """Validate a source outcome before its transaction opens, so a bad reason writes nothing."""
        if outcome is None:
            return None
        from .outcomes import validate
        return validate(*outcome)

    def _write_outcome(self, operation_id, outcome):
        if outcome is not None:
            from .outcomes import write
            write(self.conn, operation_id, *outcome)

    def _dismissed(self, operation_id):
        return self.conn.execute("SELECT 1 FROM events WHERE operation_id=? AND stage='editor_dismissed' LIMIT 1",
                                 (operation_id,)).fetchone() is not None

    def _source_effects(self, row, outcome, result):
        """Inside the request's own transaction: its result event and the outcome it settles.

        A StateAPI or Git request's outcome targets the request's own
        `operation_id` (for Git intake that is the intake operation) and is written as given. An
        editor request instead settles the archived source it cites, a different operation, and
        monotonically: an applied source never downgrades and a dismissed source keeps its
        dismissal, while the `editor_result` event still records what this request ended with.
        """
        if row is None:
            return
        if row['kind'] != 'semantic':
            self._write_outcome(row['operation_id'], outcome)
            return
        source_operation_id = row['source_operation_id']
        if result is not None:
            self.event(source_operation_id, 'editor_result', **result)
        if outcome is None:
            return
        if outcome[0] != 'applied':
            current = self.conn.execute('SELECT disposition FROM source_outcomes WHERE operation_id=?',
                                        (source_operation_id,)).fetchone()
            if (current is not None and current['disposition'] == 'applied') or self._dismissed(source_operation_id):
                return
        self._write_outcome(source_operation_id, outcome)

    def _request_row(self, operation_id):
        row = self.conn.execute('SELECT operation_id,kind,source_operation_id FROM action_requests '
                                'WHERE operation_id=?', (operation_id,)).fetchone()
        return dict(row) if row else None

    def action_settle_source(self, source_operation_id, outcome, *, result=None, event=None):
        """Record an editor result and monotonic source outcome without changing any request row."""
        outcome = self._settled_outcome(outcome)
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal outcome recording requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if event is not None:
                self.event(source_operation_id, event[0], **event[1])
            self._source_effects({'operation_id': None, 'kind': 'semantic',
                                  'source_operation_id': source_operation_id}, outcome, result)

    def action_prepare(self, operation_id, intent, *, outcome=None, result=None):
        """Store the immutable intent; its source outcome and result commit in the same transaction."""
        outcome = self._settled_outcome(outcome)
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal action preparation requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state FROM action_requests WHERE operation_id=?',
                                    (operation_id,)).fetchone()
            if row is None or row['state'] != 'open':
                raise KelmoError('E_STATE', 'Only an open action request can be prepared.')
            self.conn.execute('INSERT INTO action_intents VALUES (?,?,?)',
                              (operation_id, json.dumps(intent, sort_keys=True), now()))
            self.conn.execute("UPDATE action_requests SET state='prepared' WHERE operation_id=?", (operation_id,))
            self._source_effects(self._request_row(operation_id), outcome, result)

    def action_intent(self, operation_id):
        row = self.conn.execute('SELECT intent FROM action_intents WHERE operation_id=?', (operation_id,)).fetchone()
        return json.loads(row['intent']) if row else None

    def prepared_actions(self):
        """Prepared StateAPI intents; a semantic editor intent is never an action publication."""
        return [row['operation_id'] for row in self.conn.execute(
            '''SELECT action_requests.operation_id FROM action_requests JOIN action_intents
               ON action_intents.operation_id=action_requests.operation_id
               WHERE action_requests.state='prepared' AND action_requests.kind!='semantic'
               ORDER BY action_intents.prepared_at''')]

    def prepared_semantic(self):
        """Prepared editor intents, oldest first; they resume through the same publisher."""
        return [row['operation_id'] for row in self.conn.execute(
            '''SELECT action_requests.operation_id FROM action_requests JOIN action_intents
               ON action_intents.operation_id=action_requests.operation_id
               WHERE action_requests.state='prepared' AND action_requests.kind='semantic'
               ORDER BY action_intents.prepared_at''')]

    def prepared_intents(self):
        """Every prepared StateAPI and editor intent as (operation_id, kind), in preparation order."""
        return [(row['operation_id'], row['kind']) for row in self.conn.execute(
            '''SELECT action_requests.operation_id, action_requests.kind FROM action_requests JOIN action_intents
               ON action_intents.operation_id=action_requests.operation_id
               WHERE action_requests.state='prepared'
               ORDER BY action_intents.prepared_at, action_intents.rowid''')]

    def latest_event(self, operation_id, stage):
        row = self.conn.execute('SELECT details FROM events WHERE operation_id=? AND stage=? '
                                'ORDER BY sequence DESC LIMIT 1', (operation_id, stage)).fetchone()
        return json.loads(row['details']) if row else None

    def semantic_requests(self, source_operation_id):
        """Editor requests citing one archived source, oldest first."""
        return [dict(row) for row in self.conn.execute(
            '''SELECT * FROM action_requests WHERE kind='semantic' AND source_operation_id=?
               ORDER BY created_at, operation_id''', (source_operation_id,))]

    def action_finish(self, operation_id, receipt, accepted=None, outcome=None, *, result=None):
        """Store the durable receipt; an accepted event, request result and source outcome join it atomically."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal action completion requires an idle connection.')
        outcome = self._settled_outcome(outcome)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state FROM action_requests WHERE operation_id=?',
                                    (operation_id,)).fetchone()
            if row is None or row['state'] not in ('open', 'prepared'):
                raise KelmoError('E_STATE', 'This action request is already settled.')
            if accepted is not None:
                action_id, event = accepted
                sequence = self.conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM action_history WHERE action_id=?',
                                             (action_id,)).fetchone()[0]
                self.conn.execute('INSERT INTO action_history VALUES (?,?,?,?)',
                                  (action_id, sequence, operation_id, json.dumps(event, sort_keys=True)))
            self.conn.execute("UPDATE action_requests SET state='finished', receipt=? WHERE operation_id=?",
                              (json.dumps(receipt, sort_keys=True), operation_id))
            self._source_effects(self._request_row(operation_id), outcome, result)

    def action_refuse(self, operation_id, code, message, outcome=None, *, token=None, result=None):
        """Settle a request that can no longer publish; replays return the same refusal.

        The source outcome is written only when this call settled the request, in the same
        transaction. With `token`, only the attempt still holding the consulting lease settles an
        open request, so a loser or an expired attempt cannot refuse the winner. Returns whether
        this call settled it.
        """
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal action refusal requires an idle connection.')
        outcome = self._settled_outcome(outcome)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state,observation FROM action_requests WHERE operation_id=?',
                                    (operation_id,)).fetchone()
            if row is None or row['state'] not in ('open', 'prepared'):
                return False
            if token is not None:
                current = json.loads(row['observation']) if row['observation'] else {}
                if row['state'] != 'open' or current.get('consulting') != token:
                    return False
            self.conn.execute('''UPDATE action_requests SET state='refused', error_code=?, error_message=?
                                 WHERE operation_id=?''', (code, message, operation_id))
            self._source_effects(self._request_row(operation_id), outcome, result)
            return True

    def dismiss_semantic(self, source_operation_id, *, by, reason, outcome):
        """Refuse every open editor request of a source, record the dismissal and its outcome at once."""
        outcome = self._settled_outcome(outcome)
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal dismissal requires an idle connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            self.conn.execute('''UPDATE action_requests SET state='refused', error_code='E_DISMISSED',
                                 error_message=? WHERE kind='semantic' AND source_operation_id=? AND state='open' ''',
                              ('Dismissed: ' + reason, source_operation_id))
            self.event(source_operation_id, 'editor_dismissed', by=by, reason=reason)
            self._write_outcome(source_operation_id, outcome)

    @staticmethod
    def _disposition(row):
        result = dict(row)
        result['actions'] = [tuple(item) for item in json.loads(result['actions'])]
        result['receipt'] = json.loads(result['receipt']) if result['receipt'] else None
        return result

    def purge_disposition(self, disposition_id):
        """The stored purge disposition with this id, or None."""
        row = self.conn.execute('SELECT * FROM purge_dispositions WHERE disposition_id=?',
                                (disposition_id,)).fetchone()
        return self._disposition(row) if row else None

    def authorize_purge(self, disposition_id, *, vault_id, operator, archive_id, plan_sha256, actions,
                        check_writer):
        """Store one named disposition. The same id with the same payload returns the stored row
        (replayed True); the id with any other payload is refused. The caller validated the plan."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge authorization requires an idle journal connection.')
        payload = (vault_id, operator, archive_id, plan_sha256, json.dumps([list(item) for item in actions]))
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            check_writer()
            row = self.conn.execute('SELECT * FROM purge_dispositions WHERE disposition_id=?',
                                    (disposition_id,)).fetchone()
            if row is not None:
                if (row['vault_id'], row['operator'], row['archive_id'], row['plan_sha256'],
                        row['actions']) != payload:
                    raise KelmoError('E_IDEMPOTENCY', f'Disposition {disposition_id} already authorizes a different '
                                    'record, plan or set of actions; choose a new disposition id.')
                return self._disposition(row), True
            self.conn.execute('''INSERT INTO purge_dispositions (disposition_id, vault_id, operator, archive_id,
                plan_sha256, actions, authorized_at, state) VALUES (?,?,?,?,?,?,?,'authorized')''',
                (disposition_id, *payload, now()))
            return self.purge_disposition(disposition_id), False

    def _purging(self, operation_id):
        """Whether an executing purge intent names this operation."""
        return self.conn.execute(
            "SELECT 1 FROM purge_intents WHERE state='executing' AND EXISTS ("
            'SELECT 1 FROM json_each(purge_intents.operations) WHERE value=?)', (operation_id,)).fetchone() is not None

    def purge_intent(self, disposition_id):
        row = self.conn.execute('SELECT * FROM purge_intents WHERE disposition_id=?', (disposition_id,)).fetchone()
        if row is None:
            return None
        return dict(row, operations=json.loads(row['operations']))

    def external_claims(self, operations):
        """The operations among these that an external attempt holds a claim row on, expired or not."""
        return [row['operation_id'] for row in self.conn.execute(
            'SELECT operation_id FROM claims WHERE local_worker=0 AND operation_id IN '
            '(SELECT value FROM json_each(?)) ORDER BY operation_id', (json.dumps(sorted(operations)),))]

    def begin_purge(self, disposition_id, archive_id, operations, *, check_writer):
        """Record the execution intent, under the caller's worker lock, before the archive half. In the
        same transaction the planned operations are rechecked for external claim rows: one refuses
        (E_CAPABILITY, the refused lease dependent) and nothing is recorded. Claims are never cleared."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge execution requires an idle journal connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            check_writer()
            leases = self.external_claims(operations)
            if leases:
                raise KelmoError('E_CAPABILITY', 'Purge refused before any change; external claims hold dependent '
                                'operations: ' + ', '.join('lease:' + item for item in leases))
            row = self.conn.execute('SELECT * FROM purge_intents WHERE disposition_id=?',
                                    (disposition_id,)).fetchone()
            if row is not None and (row['state'] != 'executing' or row['archive_id'] != archive_id):
                raise KelmoError('E_CONFLICT', f'Disposition {disposition_id} already has a purge intent for '
                                'another record or a finished one; nothing executed.')
            self.conn.execute('''INSERT INTO purge_intents VALUES (?,?,?,'executing',?)
                ON CONFLICT(disposition_id) DO UPDATE SET operations=excluded.operations,
                started_at=excluded.started_at''', (disposition_id, archive_id, json.dumps(sorted(operations)), now()))

    def abandon_purge(self, disposition_id, *, check_writer):
        """Withdraw an executing intent whose archive half never committed (the caller checked this under
        the worker lock); its operations become claimable again. A finished intent is kept."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge withdrawal requires an idle journal connection.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            check_writer()
            self.conn.execute("DELETE FROM purge_intents WHERE disposition_id=? AND state='executing'",
                              (disposition_id,))

    def settle_purge(self, disposition_id, archive_id, receipt, operations, *, check_writer):
        """Once every operation is staged purged, in one transaction: finish the execution intent and mark a
        stored disposition settled with its purge receipt. Returns the disposition, or None when it came
        from another source. A repeat with the same receipt is a no-op; another record or receipt refuses."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge settlement requires an idle journal connection.')
        encoded = json.dumps(receipt, sort_keys=True)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            check_writer()
            row = self.conn.execute('SELECT * FROM purge_dispositions WHERE disposition_id=?',
                                    (disposition_id,)).fetchone()
            intent = self.conn.execute('SELECT archive_id FROM purge_intents WHERE disposition_id=?',
                                       (disposition_id,)).fetchone()
            if (row is not None and row['archive_id'] != archive_id) or (intent and intent['archive_id'] != archive_id):
                raise KelmoError('E_CONFLICT', f'Disposition {disposition_id} names another record; nothing settled.')
            if row is not None and row['state'] == 'settled':
                if row['receipt'] != encoded:
                    raise KelmoError('E_IDEMPOTENCY', 'This disposition was settled with a different receipt.')
                return self._disposition(row)
            unsettled = [operation_id for operation_id in operations
                         if self.conn.execute('SELECT state FROM operations WHERE id=?',
                                              (operation_id,)).fetchone()['state'] != 'purged']
            if unsettled:
                raise KelmoError('E_STATE', 'Operations are not yet staged purged: ' + ', '.join(unsettled))
            self.conn.execute("UPDATE purge_intents SET state='finished' WHERE disposition_id=?", (disposition_id,))
            if row is None:
                return None
            self.conn.execute("UPDATE purge_dispositions SET state='settled', settled_at=?, receipt=? "
                              'WHERE disposition_id=?', (now(), encoded, disposition_id))
            return self.purge_disposition(disposition_id)

    def _client_requests(self):
        # Created on first shared use, so a local-only journal keeps its schema.
        self.conn.execute('''CREATE TABLE IF NOT EXISTS client_requests (
            request_key TEXT PRIMARY KEY, client_id TEXT NOT NULL, op TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, reply TEXT,
            created_at TEXT NOT NULL, finished_at TEXT, claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0)''')
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(client_requests)')}
        for name, declaration in (('claim_token', 'TEXT'), ('attempts', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                try:
                    self.conn.execute(f'ALTER TABLE client_requests ADD COLUMN {name} {declaration}')
                except sqlite3.OperationalError as error:
                    if 'duplicate column' not in str(error):
                        raise

    def client_request(self, request_key):
        """A shared client's request under its scoped idempotency key, if recorded."""
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_requests'").fetchone():
            return None
        row = self.conn.execute('SELECT * FROM client_requests WHERE request_key=?', (request_key,)).fetchone()
        return dict(row) if row else None

    def client_request_claim(self, request_key, client_id, op, fingerprint):
        """Record or take over a request before its effect: (row, claim token), or (row, None) when it is
        already finished. The caller holds the row's exclusive lock, so an open row found here belongs to
        an attempt that is gone; its effect may be partial and runs again under the same scoped key."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Client request admission requires an idle connection.')
        token = uuid.uuid4().hex
        with self.conn:
            self._client_requests()
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT * FROM client_requests WHERE request_key=?', (request_key,)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint or row['client_id'] != client_id:
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different request.')
                if row['state'] == 'finished':
                    return dict(row), None
                self.conn.execute('UPDATE client_requests SET claim_token=?, attempts=attempts+1 WHERE request_key=?',
                                  (token, request_key))
            else:
                self.conn.execute('''INSERT INTO client_requests
                    (request_key,client_id,op,fingerprint,state,reply,created_at,finished_at,claim_token,attempts)
                    VALUES (?,?,?,?,'open',NULL,?,NULL,?,1)''', (request_key, client_id, op, fingerprint, now(), token))
        self._client_claims[request_key] = token
        return self.client_request(request_key), token

    def client_request_finish(self, request_key, reply):
        """Store the settled reply once, only for the attempt of this connection that holds the claim; a
        lost response replays exactly this."""
        if self.conn.in_transaction:
            raise KelmoError('E_STATE', 'Client request completion requires an idle connection.')
        token = self._client_claims.pop(request_key, None)
        if token is None:
            raise KelmoError('E_CLAIM', 'This connection holds no claim on the request; nothing was settled.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            changed = self.conn.execute('''UPDATE client_requests SET state='finished', reply=?, finished_at=?,
                                           claim_token=NULL WHERE request_key=? AND state='open' AND claim_token=?''',
                                        (json.dumps(reply, sort_keys=True), now(), request_key, token)).rowcount
            if changed != 1:
                raise KelmoError('E_CLAIM', 'This request attempt no longer holds its claim; nothing was settled.')

    def close(self):
        self.conn.close()


def prepared_action_intent(path, operation_id, *, semantic=False):
    """Read one prepared intent without creating or migrating a journal; the publisher's ownership proof.

    An action publication accepts only a StateAPI request and a semantic publication only an editor
    request, so neither kind of intent can authorize the other's registry transform.
    """
    if not path.is_file() or path.is_symlink():
        return None
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_intents'").fetchone():
            return None
        row = connection.execute('''SELECT action_intents.intent, action_requests.kind FROM action_intents
            JOIN action_requests ON action_requests.operation_id=action_intents.operation_id
            WHERE action_intents.operation_id=? AND action_requests.state='prepared' ''',
                                 (operation_id,)).fetchone()
        if row is None or (row['kind'] == 'semantic') != semantic:
            return None
        return json.loads(row['intent'])
    finally:
        connection.close()
