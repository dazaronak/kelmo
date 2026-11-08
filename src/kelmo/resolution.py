"""Durable requests and revision-checked proposals from external executors."""
import fcntl
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import KelmoError
from .wiki import engine
from . import fact_checks, outcomes, quota


@dataclass(frozen=True)
class _EditorSubmission:
    row: object
    request: dict
    key: str
    token: str


def validate_editor_claim(service, operation_id, claim, executor, key):
    """Only the saved, scoped resolution response may enter the editor under its live claim."""
    from .promotion import DeclaredChanges, FACT_FIELDS
    try:
        job_id, token = claim
        row = service.journal.conn.execute("SELECT * FROM resolutions WHERE id=?", (job_id,)).fetchone()
        if (row is None or row['operation_id'] != operation_id or row['state'] != 'claimed'
                or row['token'] != token or row['expires'] <= time.time() or not row['response']
                or key != 'resolution:' + job_id + ':' + engine.digest(row['response'])):
            raise ValueError()
        changes = json.loads(row['response'])['changes']
        expected = [{name: change[name] for name in FACT_FIELDS} for change in changes]
        if type(executor) is not DeclaredChanges or executor.proposal['facts'] != expected:
            raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError):
        raise KelmoError('E_CLAIM', 'Editor publication requires the saved response of a current resolution claim.') from None


def revision_snapshot(facts, keys):
    return engine.digest(engine.encoded(
        {key: facts.get(key, {}).get('revision') for key in sorted(keys)}))


def persistence_conflict():
    return KelmoError(
        'E_CONFLICT',
        'Wiki repository or transaction state conflicts with this write; '
        'run kelmo doctor, reconcile the repository state, then retry.',
    )


@contextmanager
def worker_lock(service):
    path = service.config.internal('.state/worker.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise KelmoError('E_BUSY', 'Another worker is processing this vault.') from error
        yield


class Resolution:
    def __init__(self, service):
        self.service = service
        self.conn = service.journal.conn
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS resolutions (
                id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, snapshot TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', token TEXT, expires REAL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                response TEXT, error TEXT
            )''')

    @contextmanager
    def _ownership_lock(self, job_id):
        directory = self.service.config.internal('.state/resolution-locks')
        directory.mkdir(mode=0o700, exist_ok=True)
        path = self.service.config.internal(
            '.state/resolution-locks/' + engine.digest(str(job_id)) + '.lock')
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'This resolution is being published.') from error
            yield

    def create(self, operation, content):
        registry = engine.load_registry(self.service.root)
        path = self.service.source_path(operation)
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            raise KelmoError('E_SOURCE', 'Captured source could not be read.') from None
        if source != content or engine.digest(source) != operation['source_hash']:
            raise KelmoError('E_SOURCE', 'Captured source hash no longer matches.')
        changes = engine.extract_changes(content)
        keys = {c['fact_key'] for c in changes} | {k for c in changes for k in c['replaces']}
        payload = {'task': 'resolve_facts', 'changes': changes,
                   'facts': {k: v for k, v in registry['facts'].items() if k in keys},
                   'candidates': self._candidates(registry, changes),
                   'source_hash': operation['source_hash'], 'archive_id': operation['archive_id'],
                   'source': source}
        with self.conn:
            self.conn.execute('''INSERT OR IGNORE INTO resolutions(id,operation_id,payload,snapshot)
                VALUES (?,?,?,?)''', (uuid.uuid4().hex, operation['id'], json.dumps(payload),
                                     revision_snapshot(registry['facts'], keys)))

    def _candidates(self, registry, changes):
        """Stored facts similar to each new fact, keyed by the new fact. The resolver reads these before it
        declares a fact new, and a submission is accepted only against this same view."""
        view = {}
        for change in changes:
            if change['fact_key'] not in registry['facts']:
                found = fact_checks.search(self.conn, registry['facts'], change['text'], change['target'])
                if found['candidates']:
                    view[change['fact_key']] = found['candidates']
        return view

    def list(self):
        return [dict(r) for r in self.conn.execute(
            'SELECT id,operation_id,state,attempts,next_attempt,error FROM resolutions ORDER BY rowid')]

    def _review(self, row, code, detail=None):
        self.conn.execute("""UPDATE resolutions SET state='review',token=NULL,
            expires=NULL,error=? WHERE id=?""", (code, row['id']))
        self.conn.execute("""UPDATE operations SET state='needs_review',terminal=0,
            error_code=? WHERE id=?""", (code, row['operation_id']))
        self.service.journal.event(row['operation_id'], 'needs_review', error_code=code)
        outcomes.write(self.conn, row['operation_id'], 'unresolved',
                       outcomes.repair_reason(code, self._cause(row, code, detail)))

    def _cause(self, row, code, detail=None):
        keys = ', '.join(sorted({change['fact_key'] for change in json.loads(row['payload'])['changes']}))
        sentence = outcomes.cause(code) + ' for ' + keys + ' after ' + str(row['attempts']) + ' attempt(s)'
        if detail:
            sentence += ': ' + ' '.join(str(detail).split())
        return sentence

    def claim(self, lease_seconds=300):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise KelmoError('E_INPUT', 'Invalid resolution lease duration.')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            rows = self.conn.execute('''SELECT * FROM resolutions WHERE
                (state='pending' OR (state='claimed' AND expires<=?)) AND next_attempt<=?
                ORDER BY rowid''', (time.time(), time.time())).fetchall()
            for row in rows:
                try:
                    with self._ownership_lock(row['id']):
                        token = uuid.uuid4().hex
                        payload = json.loads(row['payload'])
                        if not row['response']:
                            # A request about to be asked again shows the similar facts as they are now.
                            payload['candidates'] = self._candidates(engine.load_registry(self.service.root),
                                                                     payload['changes'])
                        self.conn.execute("UPDATE resolutions SET state='claimed',token=?,expires=?,"
                                          "attempts=attempts+1,payload=? WHERE id=?",
                                          (token, time.time() + lease_seconds, json.dumps(payload), row['id']))
                        return {'id': row['id'], 'operation_id': row['operation_id'], 'token': token,
                                'payload': payload,
                                'response': json.loads(row['response']) if row['response'] else None}
                except KelmoError as error:
                    if error.code != 'E_BUSY':
                        raise
            return None

    def submit(self, job_id, token, response):
        with self._ownership_lock(job_id):
            result = self._submit(job_id, token, response)
            if not isinstance(result, _EditorSubmission):
                return result
            from . import promotion
            row, request = result.row, result.request
            try:
                self.service.editor.edit(
                    row['operation_id'], origin=self.service._operation_origin(row['operation_id']),
                    idempotency_key=result.key, project=request['project'], selected=request['selected'],
                    executor=promotion.DeclaredChanges(request['proposal']),
                    budget_tokens=promotion.BUDGET_TOKENS, rule_change=request['rule_change'],
                    resolution_claim=(row['id'], result.token), contexts=self.service.context_bindings)
            except KelmoError as error:
                if error.code in ('E_BUSY', 'E_CLAIM', 'E_CONFIG', 'E_CONFLICT', 'E_IO', 'E_GIT', 'E_WRITER'):
                    raise
                with self.conn:
                    self._review(row, error.code, str(error))
                return self.service.journal.get(row['operation_id'])
            return self._applied(row)

    def _editor_submission(self, row, operation, registry, changes, encoded, token):
        from . import promotion
        checks = []
        for change in changes:
            prior = registry['facts'].get(change['fact_key'])
            if change['expected_revision'] != (prior['revision'] if prior else None):
                raise KelmoError('E_PROPOSAL', 'Proposal revisions do not match the resolution baseline.')
            if prior:
                match = {'result': 'existing', 'fact_key': change['fact_key'], 'revision': prior['revision']}
            else:
                receipt = self.service.reconcile(change['text'], change['target'])
                shown = json.loads(row['payload']).get('candidates', {}).get(change['fact_key'], [])
                if receipt['candidates'] != shown:
                    # The resolver declared the fact new against other similar facts than exist now; its
                    # answer is dropped and the request is asked again with the current view.
                    with self.conn:
                        self.conn.execute('UPDATE resolutions SET response=NULL WHERE id=?', (row['id'],))
                    raise KelmoError('E_STALE', 'Similar facts changed after the resolver read them; the request '
                                     'is asked again with the current candidates.')
                match = {'result': 'not_found', 'search_id': receipt['search_id']}
            checks.append({'fact_key': change['fact_key'], 'verified_match': match})
        content = ('```wiki-changes\n' + json.dumps(changes) + '\n```\n```fact-checks\n'
                   + json.dumps(checks) + '\n```\n')
        request = promotion.declared_request(registry, content,
                                             self.service.source_packet(operation['id']).project)
        with self.conn:
            self.conn.execute('UPDATE resolutions SET response=? WHERE id=?', (encoded, row['id']))
        return _EditorSubmission(row, request, 'resolution:' + row['id'] + ':' + engine.digest(encoded), token)

    def _applied(self, row):
        operation_id = row['operation_id']
        self.service.journal.stage(operation_id, 'applied', terminal=True)
        outcomes.record(self.conn, operation_id, *outcomes.promotion_outcome(
            engine.load_registry(self.service.root)['facts'],
            self.service.journal.get(operation_id)['source_hash']))
        with self.conn:
            self.conn.execute("UPDATE resolutions SET state='applied',error=NULL WHERE id=?", (row['id'],))
        return self.service.journal.get(operation_id)

    def _submit(self, job_id, token, response):
        with worker_lock(self.service):
            encoded = engine.encoded(response)
            if len(encoded.encode()) > 1048576:
                raise KelmoError('E_INPUT', 'Resolution response exceeds size limit.')
            with self.conn:
                self.conn.execute('BEGIN IMMEDIATE')
                row = self.conn.execute('SELECT * FROM resolutions WHERE id=?', (job_id,)).fetchone()
                if row is None or row['token'] != token:
                    raise KelmoError('E_CLAIM', 'Resolution claim is not current.')
                if row['state'] in ('applied', 'review') and row['response'] == encoded:
                    return self.service.journal.get(row['operation_id'])
                if row['state'] != 'claimed' or row['expires'] <= time.time():
                    raise KelmoError('E_CLAIM', 'Resolution claim expired.')
                if row['response'] is not None and row['response'] != encoded:
                    raise KelmoError('E_CLAIM', 'A different response was already accepted.')
                if quota.read(response) is not None:
                    raise KelmoError('E_QUOTA', 'The executor answered with a quota wait; the request stays '
                                    'pending until the reset time it named, and no attempt is charged.')
                if response != {'unresolved': True}:
                    try:
                        if not isinstance(response, dict) or set(response) != {'changes'}:
                            raise ValueError()
                        changes = engine.validate_changes(response['changes'])
                        payload = json.loads(row['payload'])
                        permitted = {c['fact_key']: c['target'] for c in payload['changes']}
                        permitted.update({k: v['target'] for k, v in payload['facts'].items()})
                        if {c['fact_key'] for c in changes} != set(permitted):
                            raise ValueError()
                        if any(c['replaces'] or c['target'] != permitted[c['fact_key']] for c in changes):
                            raise ValueError()
                    except (ValueError, TypeError, KeyError) as error:
                        raise KelmoError('E_PROPOSAL', 'Proposal schema or fact scope is invalid: '
                                        + (str(error) or 'the proposal names facts it was not asked about')) from None
            operation = self.service.journal.get(row['operation_id'])
            self.service.journal.handover_local_claim(operation['id'])
            if response == {'unresolved': True}:
                with self.conn:
                    self.conn.execute('BEGIN IMMEDIATE')
                    self._review(row, 'E_UNRESOLVED')
                return self.service.journal.get(operation['id'])
            try:
                engine.recover_transaction(self.service.root)
            except engine.Conflict:
                raise persistence_conflict() from None
            registry = engine.load_registry(self.service.root)
            if operation['source_hash'] not in registry['notes']:
                payload = json.loads(row['payload'])
                keys = ({c['fact_key'] for c in payload['changes']}
                        | {key for change in payload['changes'] for key in change['replaces']}
                        | set(payload['facts']))
                if revision_snapshot(registry['facts'], keys) != row['snapshot']:
                    with self.conn:
                        self.conn.execute('BEGIN IMMEDIATE')
                        self._review(row, 'E_STALE')
                    return self.service.journal.get(operation['id'])
                return self._editor_submission(row, operation, registry, changes, encoded, token)
            return self._applied(row)

    def fail(self, job_id, token, code, max_attempts, detail=None):
        try:
            with self._ownership_lock(job_id):
                return self._fail(job_id, token, code, max_attempts, detail)
        except KelmoError as error:
            if error.code != 'E_BUSY':
                raise
            return None

    def _fail(self, job_id, token, code, max_attempts, detail=None):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT * FROM resolutions WHERE id=?', (job_id,)).fetchone()
            if (row is None or row['state'] != 'claimed' or row['token'] != token
                    or row['expires'] <= time.time()):
                return None
            exhausted = row['attempts'] >= max_attempts
            if exhausted:
                self._review(row, code, detail)
            else:
                self.conn.execute('''UPDATE resolutions SET state='pending',token=NULL,expires=NULL,
                    next_attempt=?,error=? WHERE id=?''',
                    (time.time() + 2 ** row['attempts'], code, job_id))
        return exhausted

    def release_busy(self, job_id, token):
        try:
            with self._ownership_lock(job_id):
                return self._release_busy(job_id, token)
        except KelmoError as error:
            if error.code != 'E_BUSY':
                raise
            return False

    def _release_busy(self, job_id, token):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state,token,expires FROM resolutions WHERE id=?',
                                    (job_id,)).fetchone()
            if (row is None or row['state'] != 'claimed' or row['token'] != token
                    or row['expires'] <= time.time()):
                return False
            self.conn.execute('''UPDATE resolutions SET state='pending',token=NULL,expires=NULL,
                attempts=MAX(0,attempts-1),next_attempt=?,error='E_BUSY' WHERE id=?''',
                (time.time() + 1, job_id))
        return True

    def quota_wait(self, job_id, token, reset_at):
        try:
            with self._ownership_lock(job_id):
                return self._quota_wait(job_id, token, reset_at)
        except KelmoError as error:
            if error.code != 'E_BUSY':
                raise
            return False

    def _quota_wait(self, job_id, token, reset_at):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT state,token,expires FROM resolutions WHERE id=?',
                                    (job_id,)).fetchone()
            if (row is None or row['state'] != 'claimed' or row['token'] != token
                    or row['expires'] <= time.time()):
                return False
            self.conn.execute('''UPDATE resolutions SET state='pending',token=NULL,expires=NULL,
                attempts=MAX(0,attempts-1),next_attempt=?,error='E_QUOTA' WHERE id=?''',
                (reset_at, job_id))
        return True

    def reopen(self, job_id):
        with self._ownership_lock(job_id):
            return self._reopen(job_id)

    def _reopen(self, job_id):
        with worker_lock(self.service):
            with self.conn:
                self.conn.execute('BEGIN IMMEDIATE')
                row = self.conn.execute('SELECT * FROM resolutions WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    raise KelmoError('E_NOT_FOUND', 'Resolution request not found.')
                operation = self.service.journal.get(row['operation_id'])
                if row['state'] != 'review' or operation['state'] != 'needs_review':
                    raise KelmoError('E_STATE', 'Only a review resolution can be reopened.')
                payload = json.loads(row['payload'])
                if row['response'] is None:
                    registry = engine.load_registry(self.service.root)
                    changes = payload['changes']
                    for change in changes:
                        prior = registry['facts'].get(change['fact_key'])
                        change['expected_revision'] = prior['revision'] if prior else None
                    keys = {c['fact_key'] for c in changes} | {k for c in changes for k in c['replaces']}
                    payload['facts'] = {k: v for k, v in registry['facts'].items() if k in keys}
                    snapshot = revision_snapshot(registry['facts'], keys)
                else:
                    snapshot = row['snapshot']
                self.conn.execute('''UPDATE resolutions SET payload=?,snapshot=?,state='pending',
                    token=NULL,expires=NULL,attempts=0,next_attempt=0,error=NULL WHERE id=?''',
                    (json.dumps(payload), snapshot, job_id))
                self.conn.execute("""UPDATE operations SET state='needs_resolution',terminal=0,
                    error_code=NULL WHERE id=?""", (operation['id'],))
                self.service.journal.event(operation['id'], 'resolution_reopened',
                                           resolution_id=job_id, prior_attempts=row['attempts'],
                                           response_preserved=row['response'] is not None)
                outcomes.clear(self.conn, operation['id'])
            return dict(self.conn.execute('''SELECT id,operation_id,state,attempts,next_attempt,error
                FROM resolutions WHERE id=?''', (job_id,)).fetchone())
