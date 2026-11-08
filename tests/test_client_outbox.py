"""Durable client outbox: the write is stored before transport and settles exactly once."""

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import profile_value, server_config, shared as shared  # noqa: F401


class Crash(BaseException):
    """Process death at one boundary; nothing below it runs."""


def operations(world, filename=None):
    query = 'SELECT id FROM operations' + (' WHERE filename=?' if filename else '') + ' ORDER BY created_at, id'
    return [row['id'] for row in world.service.journal.conn.execute(query, (filename,) if filename else ())]


def capture(service, content, filename, key, role='implementer', wait=False):
    """Capture, optionally exercising the older protocol's explicit process request."""
    from kelmo.client import digest
    result = service.add(content, filename, role=role, idempotency_key=key)
    return service.client.submit('process', {'capture_key': key}, 'process/' + digest(key)) if wait else result


def test_offline_outbox_survives_restart(shared):
    from kelmo.client import ClientService, SharedClient
    from kelmo.config import ClientProfile
    client = shared.client()
    shared.hub.online = False
    queued = ClientService(client).add('Decided to keep the outbox.', 'outbox-note.md', role='implementer',
                                       idempotency_key='note-1', wait=True)
    assert queued['queued'] is True and queued['op'] == 'capture'
    assert operations(shared.world, 'outbox-note.md') == []
    client.close()

    shared.hub.online = True
    reopened = SharedClient(ClientProfile.load(client.profile.root), channel_factory=shared.hub.factory('mac-a'))
    shared.clients.append(reopened)
    assert [entry['op'] for entry in reopened.outbox.pending()] == ['capture']
    flushed = reopened.flush()
    assert flushed['delivered'] == 1 and flushed['pending'] == 0
    [operation_id] = operations(shared.world, 'outbox-note.md')
    captured = json.loads(reopened.outbox.get('note-1')['outcome'])
    assert captured['operation']['id'] == operation_id
    assert captured['operation']['state'] == 'queued', 'capture alone does not process, as locally'
    # Identity is the server's binding of the channel, never a request field.
    origin = shared.world.service.journal.origin_record(operation_id)
    assert origin['client_id'] == 'mac-a' and origin['machine_tag'] == 'mac-a'
    assert origin['authority_id'] == shared.world.bindings.authority_id


def test_wait_observes_worker_without_enqueuing_process(shared, monkeypatch):
    from kelmo.client import ClientService
    client = shared.client()

    def worker_tick(_):
        [operation_id] = operations(shared.world, 'observed.md')
        shared.hub.session('mac-a').service.run(operation_id)

    monkeypatch.setattr('kelmo.client.time.sleep', worker_tick)
    result = ClientService(client).add('Worker owns execution.', 'observed.md', role='implementer',
                                       idempotency_key='observe', wait=True)
    assert result['operation']['durable_stage'] == 'archived'
    assert result['evidence_ref']['version'] == 1
    assert not any(request['op'] == 'process' for _, request in shared.hub.requests)
    assert client.outbox.pending() == []


def test_wait_deadline_does_not_block_later_captures(shared):
    from kelmo.client import ClientService
    client = shared.client()
    service = ClientService(client)
    pending = service.add('Worker has not run yet.', 'deadline.md', role='implementer',
                          idempotency_key='deadline', wait=True, wait_timeout=0)
    assert pending['accepted'] and pending['wait_timed_out']
    assert pending['operation']['state'] == 'queued'
    following = service.add('Next independent source.', 'following.md', role='implementer',
                            idempotency_key='following')
    assert following['operation']['state'] == 'queued'
    assert client.outbox.pending() == []


def test_lost_response_replays_without_duplicate_work(shared):
    from kelmo.client import ClientService
    client = shared.client()
    shared.hub.drop_replies = 1
    first = capture(ClientService(client), 'The reply to this capture is lost.', 'lost.md', 'lost-1')
    assert first['queued'] is True and first['pending_reason'] == 'E_TRANSPORT'
    assert len(operations(shared.world, 'lost.md')) == 1, 'the server acted before the reply was lost'
    replayed = client.flush()
    assert replayed['delivered'] == 1
    outcome = json.loads(client.outbox.get('lost-1')['outcome'])
    assert outcome['operation']['id'] == operations(shared.world, 'lost.md')[0]
    last = shared.hub.requests[-1][1]
    assert last['idempotency_key'] == 'lost-1' and last['op'] == 'capture'
    assert len(operations(shared.world, 'lost.md')) == 1
    rows = shared.world.service.journal.conn.execute('SELECT state FROM client_requests').fetchall()
    assert [row['state'] for row in rows] == ['finished']


def test_partial_durable_write_resumes_once(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.journal import Journal
    client = shared.client()
    original = Journal.client_request_finish
    calls = []

    def die_once(self, request_key, reply):
        calls.append(request_key)
        if len(calls) == 1:
            raise Crash
        return original(self, request_key, reply)

    monkeypatch.setattr(Journal, 'client_request_finish', die_once)
    with pytest.raises(Crash):
        capture(ClientService(client), 'Partial durable write.', 'partial.md', 'partial-1')
    assert client.outbox.get('partial-1')['state'] == 'pending'
    [operation_id] = operations(shared.world, 'partial.md')
    shared.hub.reset()
    assert client.flush()['delivered'] == 1
    assert operations(shared.world, 'partial.md') == [operation_id]
    assert json.loads(client.outbox.get('partial-1')['outcome'])['operation']['id'] == operation_id


def test_retry_preserves_original_source_and_key_identity(shared):
    from kelmo.errors import KelmoError
    from kelmo.client import ClientService
    client = shared.client()
    shared.hub.online = False
    capture(ClientService(client), 'Original bytes.\n', 'original.md', 'same-key')
    with pytest.raises(KelmoError) as error:
        capture(ClientService(client), 'Different bytes.', 'original.md', 'same-key')
    assert error.value.code == 'E_IDEMPOTENCY'
    shared.hub.online = True
    client.flush()
    [operation_id] = operations(shared.world, 'original.md')
    assert shared.world.service.source_packet(operation_id).content == b'Original bytes.\n'
    # The same client key from another client is a different request with its own origin.
    other = shared.client('mac-b')
    capture(ClientService(other), 'Original bytes.\n', 'original.md', 'same-key')
    owners = {shared.world.service.journal.origin_record(item)['client_id']
              for item in operations(shared.world, 'original.md')}
    assert owners == {'mac-a', 'mac-b'}


def test_refusal_is_settled_once_and_order_is_kept(shared):
    from kelmo.errors import KelmoError
    from kelmo.client import ClientService
    client = shared.client()
    shared.hub.online = False
    capture(ClientService(client), 'First queued.', 'first.md', 'order-1')
    client.outbox.enqueue('capture', {'content': 'Bad name.', 'filename': 'bad name.md', 'role': 'collector'},
                          'order-2')
    capture(ClientService(client), 'Third queued.', 'third.md', 'order-3')
    shared.hub.online = True
    result = client.flush()
    assert result['delivered'] == 2 and [item['code'] for item in result['refused']] == ['E_INPUT']
    first, third = operations(shared.world, 'first.md'), operations(shared.world, 'third.md')
    order = operations(shared.world)
    assert order.index(first[0]) < order.index(third[0])
    sent = len(shared.hub.requests)
    assert client.flush() == {'delivered': 0, 'refused': [], 'pending': 0, 'stopped': None, 'reached': None}
    assert len(shared.hub.requests) == sent, 'a settled refusal is never sent again'
    with pytest.raises(KelmoError) as error:
        client._entry_result(client.outbox.get('order-2'))
    assert error.value.code == 'E_INPUT'


def _transcript(lines, partial=b''):
    return b''.join(json.dumps(line).encode() + b'\n' for line in lines) + partial


def _user(text):
    return {'type': 'user', 'message': {'content': text}}


def _assistant(text):
    return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def test_transcript_regrowth_to_old_bytes_preserves_the_new_generation(shared, tmp_path):
    from kelmo.client import hook
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    root = tmp_path / 'provider'
    root.mkdir()
    client = shared.client(sessions={'claude': [str(root)]})
    path = root / 'session.jsonl'
    payload = {'transcript_path': str(path), 'session_id': 'regrow-1'}
    short = _transcript([_user('First source.')])
    long = short + _transcript([_assistant('A second source record.')])
    identity = None
    for index, raw in enumerate((long, short, long)):
        path.write_bytes(raw)
        identity = identity or path.stat().st_ino
        assert path.stat().st_ino == identity
        if index == 2:
            shared.hub.drop_replies = 1
        hook(client, 'session-checkpoint', payload, 'claude')
    [pending] = client.outbox.pending()
    retry_key = pending['idempotency_key']
    assert json.loads(pending['args'])['client_generation'] == 2
    assert client.flush()['delivered'] == 1
    assert client.outbox.get(retry_key)['attempts'] == 2
    outcomes = [json.loads(row['outcome']) for row in client.outbox.conn.execute(
        "SELECT outcome FROM entries WHERE op='session_append' ORDER BY seq")]
    assert len(outcomes) == 3
    assert [row['client_generation'] for row in outcomes] == [1, 2, 2]
    assert outcomes[-1]['sha256'] == outcomes[0]['sha256']
    assert [row['generation'] for row in outcomes] == [1, 2, 2]
    assert outcomes[-1]['job_id'] != outcomes[0]['job_id']
    from kelmo.config import Config
    from kelmo.sessions import Sessions
    sessions = Sessions(Config.load(shared.world.root))
    try:
        assert len(sessions.list_jobs()) == 3, 'the lost reply did not duplicate the new generation job'
        assert sessions.snapshot(outcomes[-1]['snapshot_hash']) == long
    finally:
        sessions.close()
    # Reopening the client and capturing identical bytes in generation 2 sends no bytes: one empty final
    # piece carries the event, and the server answers with the job it already holds for that range.
    from kelmo.client import SharedClient
    from kelmo.config import ClientProfile
    client.close()
    reopened = SharedClient(ClientProfile.load(client.profile.root), channel_factory=shared.hub.factory('mac-a'))
    shared.clients.append(reopened)
    hook(reopened, 'session-checkpoint', payload, 'claude')
    assert reopened.outbox.summary()['delivered'] == 4
    assert reopened.outbox.pending() == []
    repeated = json.loads(reopened.outbox.conn.execute('SELECT args, outcome FROM entries ORDER BY seq DESC')
                          .fetchone()['outcome'])
    assert repeated['range'] == [0, len(long)] and repeated['job_id'] == outcomes[-1]['job_id']
    assert json.loads(reopened.outbox.conn.execute('SELECT args FROM entries ORDER BY seq DESC').fetchone()['args'])['size'] == 0


def test_transient_wait_keeps_the_process_request_pending(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.service import Service
    client = shared.client()
    original = Service.run
    failed_once = False

    def fail_first(self, operation_id, *args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            self.journal.stage(operation_id, 'failed', terminal=True, error_code='E_IO')
            return self.journal.get(operation_id)
        return original(self, operation_id, *args, **kwargs)

    monkeypatch.setattr(Service, 'run', fail_first)
    result = capture(ClientService(client), 'Retain the pending retry.', 'pending.md', 'pending-key', wait=True)
    assert result['queued'] is True
    pending = client.outbox.pending()
    assert len(pending) == 1 and pending[0]['op'] == 'process'
    session = shared.hub.session('mac-a')
    assert session.service.journal.conn.execute(
        "SELECT state FROM client_requests WHERE op='process'").fetchone()[0] == 'open'
    [operation_id] = operations(shared.world, 'pending.md')
    assert client.flush()['stopped'] == 'E_BUSY'
    assert session.service.journal.conn.execute(
        'SELECT attempts FROM worker_retries WHERE operation_id=?', (operation_id,)).fetchone()[0] == 1
    with session.service.journal.conn as db:
        db.execute('UPDATE worker_retries SET eligible=0 WHERE operation_id=?', (operation_id,))
    assert client.flush()['delivered'] == 1
    assert json.loads(client.outbox.get(pending[0]['idempotency_key'])['outcome'])['operation']['state'] == 'archived'
    assert session.service.journal.conn.execute(
        'SELECT attempts FROM worker_retries WHERE operation_id=?', (operation_id,)).fetchone()[0] == 2


def test_repeated_transient_process_failures_exhaust_the_existing_budget(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.service import Service
    original = Service.run
    runs = []

    def failing(self, operation_id):
        if self.journal.get(operation_id)['state'] == 'queued':
            runs.append(operation_id)
            self.journal.stage(operation_id, 'failed', terminal=True, error_code='E_IO')
            return self.journal.get(operation_id)
        return original(self, operation_id)

    monkeypatch.setattr(Service, 'run', failing)
    client = shared.client()
    assert capture(ClientService(client), 'Repeated outage.', 'budget.md', 'budget-key', wait=True)['queued']
    [operation_id] = operations(shared.world, 'budget.md')
    for attempt in (2, 3):
        with shared.hub.session('mac-a').service.journal.conn as db:
            db.execute('UPDATE worker_retries SET eligible=0 WHERE operation_id=?', (operation_id,))
        result = client.flush()
        if attempt == 2:
            assert result['pending'] == 1 and result['stopped'] == 'E_BUSY'
        else:
            assert result['pending'] == 0 and result['refused'][0]['code'] == 'E_ATTEMPTS'
    assert runs == [operation_id] * 3


@pytest.mark.parametrize('code', ['E_BUSY', 'E_CLAIM'])
def test_process_run_contention_keeps_the_deferred_meaning(shared, monkeypatch, code):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    from kelmo.service import Service

    def contended(*args):
        raise KelmoError(code, 'Another worker owns the run.')

    monkeypatch.setattr(Service, 'run', contended)
    client = shared.client()
    result = capture(ClientService(client), 'Contended run.', 'contended.md', 'contended-key', wait=True)
    assert result['processing_deferred'] is True and result['operation']['state'] == 'queued'
    assert client.outbox.pending() == []


def test_nontransient_process_failure_keeps_its_terminal_result(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.service import Service

    def refused(self, operation_id):
        self.journal.stage(operation_id, 'failed', terminal=True, error_code='E_SOURCE')
        return self.journal.get(operation_id)

    monkeypatch.setattr(Service, 'run', refused)
    client = shared.client()
    result = capture(ClientService(client), 'Definitive refusal.', 'refused.md', 'refused-key', wait=True)
    assert result['operation']['state'] == 'failed' and result['operation']['error_code'] == 'E_SOURCE'
    assert 'processing_deferred' not in result and client.outbox.pending() == []


def test_session_capture_keeps_generation_range_hash_and_tail(shared, tmp_path):
    from kelmo.client import hook
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    transcripts = tmp_path / 'provider' / 'projects'
    transcripts.mkdir(parents=True)
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    first = _transcript([_user('Plan the outbox.'), _assistant('Stored first.')], b'{"type": "user", "mes')
    source.write_bytes(first)
    payload = {'transcript_path': str(source), 'session_id': 'native-1'}

    shared.hub.drop_replies = 1
    assert hook(client, 'session-checkpoint', payload, 'claude') == {}
    pending = client.outbox.pending()
    assert len(pending) == 1 and json.loads(pending[0]['args'])['sha256'] == hashlib.sha256(first).hexdigest()
    client.flush()
    delivered = json.loads(client.outbox.get(pending[0]['idempotency_key'])['outcome'])
    assert delivered['generation'] == 1 and delivered['partial_tail'] is True and delivered['records'] == 2
    assert delivered['range'] == [0, len(first)] and delivered['client_generation'] == 1

    grown = first[:first.rindex(b'\n') + 1] + _transcript([_user('Continue.')])
    source.write_bytes(grown)
    hook(client, 'session-checkpoint', payload, 'claude')
    second = json.loads(client.outbox.get(client.outbox.conn.execute(
        'SELECT idempotency_key FROM entries ORDER BY seq DESC').fetchone()[0])['outcome'])
    assert second['generation'] == 1 and second['records'] == 3 and second['partial_tail'] is False

    # Created while the old file still exists, so the replacement has its own file identity.
    staged = transcripts / 'replacement.tmp'
    staged.write_bytes(_transcript([_user('A replaced file starts again.')]))
    os.replace(staged, source)
    hook(client, 'session-end', payload, 'claude')
    third = json.loads(client.outbox.get(client.outbox.conn.execute(
        'SELECT idempotency_key FROM entries ORDER BY seq DESC').fetchone()[0])['outcome'])
    assert third['generation'] == 2 and third['client_generation'] == 2

    from kelmo.sessions import Sessions
    from kelmo.config import Config
    sessions = Sessions(Config.load(shared.world.root))
    try:
        row = sessions.journal.conn.execute('SELECT * FROM sessions').fetchone()
        assert row['native_id'] == 'mac-a:native-1' and row['generation'] == 2
        # Earlier generations keep their exact snapshots.
        assert sessions.snapshot(delivered['snapshot_hash']) == first
        assert sessions.snapshot(second['snapshot_hash']) == grown
    finally:
        sessions.close()
    spools = list((Config.load(shared.world.root).state / 'client-sessions').rglob('*.jsonl'))
    assert len(spools) == 2 and all(str(source) not in str(path) for path in spools)


@pytest.mark.parametrize('boundary', ['before-commit', 'after-commit'])
def test_session_capture_provenance_recovers_with_the_capture(shared, tmp_path, monkeypatch, boundary):
    from contextlib import contextmanager
    from kelmo.client import hook
    from kelmo.config import Config
    from kelmo.journal import Journal
    from kelmo.server import shared_service
    from kelmo.sessions import Sessions
    from kelmo.session_worker import _note_service
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    root = tmp_path / 'provider'
    root.mkdir()
    path = root / 'session.jsonl'
    path.write_bytes(_transcript([_user('Captured by the enrolled channel.')]))
    # A prefix naming a more privileged client remains opaque native input.
    payload = {'transcript_path': str(path), 'session_id': 'requester-client:claimed'}
    client = shared.client('mac-b', sessions={'claude': [str(root)]})
    with monkeypatch.context() as crash:
        if boundary == 'before-commit':
            original = Sessions._capture_transaction

            @contextmanager
            def interrupted(self, deadline):
                with original(self, deadline):
                    yield
                    raise Crash

            crash.setattr(Sessions, '_capture_transaction', interrupted)
        else:
            def interrupted(*args):
                raise Crash

            crash.setattr(Journal, 'client_request_finish', interrupted)
        with pytest.raises(Crash):
            hook(client, 'session-checkpoint', payload, 'claude')
    sessions = Sessions(Config.load(shared.world.root))
    try:
        count = sessions.journal.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        bound = sessions.journal.conn.execute('SELECT COUNT(*) FROM session_capture_origins').fetchone()[0]
        assert count == bound == (0 if boundary == 'before-commit' else 1)
    finally:
        sessions.close()
    shared.hub.reset()
    assert client.flush()['delivered'] == 1
    sessions = Sessions(Config.load(shared.world.root))
    try:
        [job] = sessions.list_jobs()
        with _note_service(sessions, job, lambda owner: shared_service(
                shared.world.root, shared.hub.profile, owner)) as service:
            assert service.origin.client_id == 'mac-b'
            note = service.capture('Saved session note.', 'session-note.md', role='lead',
                                   idempotency_key='provenance-note')
            archived = service.run(note['id'])
            assert archived['state'] == 'archived'
            assert service.journal.origin_record(note['id'])['client_id'] == 'mac-b'
        hook(client, 'session-checkpoint', payload, 'claude')
        assert len(sessions.list_jobs()) == 1
        from kelmo.errors import KelmoError
        with pytest.raises(KelmoError) as error:
            sessions.capture('claude', 'mac-b:requester-client:claimed', path, root, 'PreCompact')
        assert error.value.code == 'E_ORIGIN'
    finally:
        sessions.close()


def test_old_unbound_session_is_not_adopted_by_a_new_shared_capture(shared, tmp_path):
    from kelmo.client import hook
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.sessions import Sessions
    from kelmo.session_worker import _note_service
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    root = tmp_path / 'provider'
    root.mkdir()
    path = root / 'session.jsonl'
    path.write_bytes(_transcript([_user('Historical unbound session.')]))
    sessions = Sessions(Config.load(shared.world.root))
    try:
        old = sessions.capture('claude', 'mac-a:old', path, root, 'PreCompact')
        # A journal predating capture provenance has only its native ID, session and job.
        with sessions.journal.conn as db:
            db.execute('DROP TABLE session_capture_origins')
    finally:
        sessions.close()
    sessions = Sessions(Config.load(shared.world.root))
    try:
        job = sessions.job(old['job_id'])
        with pytest.raises(KelmoError) as error:
            _note_service(sessions, job, lambda owner: pytest.fail('Unbound session acquired an origin.'))
        assert error.value.code == 'E_ORIGIN'
        client = shared.client(sessions={'claude': [str(root)]})
        with pytest.raises(KelmoError) as error:
            hook(client, 'session-checkpoint', {'transcript_path': str(path), 'session_id': 'old'}, 'claude')
        assert error.value.code == 'E_ORIGIN'
        assert len(sessions.list_jobs()) == 1
        assert sessions.journal.conn.execute('SELECT COUNT(*) FROM session_capture_origins').fetchone()[0] == 0
    finally:
        sessions.close()


SPOOL_DEATH = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from kelmo import server
from kelmo.service import Service
arguments = json.loads(sys.argv[3])
with Service(sys.argv[2]) as service:
    real = os.pwrite
    def dying(fd, data, offset):
        real(fd, bytes(data)[:len(data) // 2], offset)
        os._exit(17)  # process death in the middle of the spool write
    os.pwrite = dying
    server.session_capture(service, 'mac-a', offset=0, base_sha256=server.EMPTY_SHA256, final=True, **arguments)
'''


def test_death_while_writing_the_spool_never_reaches_capture(shared, tmp_path):
    """Fresh process: a server killed mid-write leaves a torn spool and no capture; the client's
    durable outbox input is written whole, verified and captured once on retry."""
    import subprocess
    import kelmo
    from kelmo.config import Config
    from kelmo.sessions import Sessions
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    raw = _transcript([_user('Torn write.'), _assistant('Recovered whole.')])
    checksum = hashlib.sha256(raw).hexdigest()
    args = {'provider': 'claude', 'native_id': 'native-torn', 'event': 'Stop',
            'content': base64.b64encode(raw).decode(), 'sha256': checksum, 'size': len(raw),
            'file_identity': '7:9', 'client_generation': 1}
    source = str(Path(kelmo.__file__).resolve().parents[1])
    died = subprocess.run([sys.executable, '-c', SPOOL_DEATH, source, str(shared.world.root), json.dumps(args)],
                          capture_output=True, timeout=120)
    assert died.returncode == 17, died.stderr.decode()[-2000:]
    config = Config.load(shared.world.root)
    [spool] = list((config.state / 'client-sessions').rglob('*.jsonl'))
    torn = spool.read_bytes()
    assert torn != raw and raw.startswith(torn), 'the death left a partial spool'
    assert not config.internal('.state/sessions/transcripts/' + checksum + '.jsonl').exists()

    client = shared.client()
    delivered = client.submit('session_capture', args, 'torn-capture')
    assert delivered['captured'] is True and delivered['sha256'] == checksum and delivered['generation'] == 1
    assert spool.read_bytes() == raw and len(list((config.state / 'client-sessions').rglob('*.jsonl'))) == 1
    sessions = Sessions(config)
    try:
        assert sessions.snapshot(checksum) == raw
        rows = sessions.journal.conn.execute("SELECT * FROM sessions WHERE native_id='mac-a:native-torn'").fetchall()
        assert [(row['generation'], row['latest_hash']) for row in rows] == [(1, checksum)]
    finally:
        sessions.close()


def test_internal_job_skip_is_recorded_not_queued(shared, tmp_path, monkeypatch):
    from kelmo.client import hook
    transcripts = tmp_path / 'enrolled'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'job.jsonl'
    source.write_bytes(_transcript([_user('Executor job transcript.')]))
    monkeypatch.setenv('KELMO_INTERNAL_JOB', '1')
    assert hook(client, 'session-stop', {'transcript_path': str(source), 'session_id': 'job-1'}, 'claude') == {}
    assert client.outbox.pending() == [] and client.local_status()['outbox']['skipped'] == {'internal-job': 1}


def test_local_outbox_failure_at_start_still_delivers_the_labelled_cache(shared, monkeypatch):
    import sqlite3
    from kelmo.client import hook
    client = shared.client()
    client.context(str(shared.app))

    def broken_outbox():
        raise sqlite3.OperationalError('disk I/O error')

    monkeypatch.setattr(client, 'flush', broken_outbox)
    context = hook(client, 'start', {'cwd': str(shared.app)})['hookSpecificOutput']['additionalContext']
    assert context.startswith('Kelmo HOT STALE CACHE (E_STATE)')


def test_transcript_outside_enrolled_roots_is_refused_locally(shared, tmp_path):
    from kelmo.client import hook
    from kelmo.errors import KelmoError
    client = shared.client(sessions={'claude': [str(tmp_path / 'enrolled')]})
    outside = tmp_path / 'outside.jsonl'
    outside.write_bytes(_transcript([_user('Not enrolled.')]))
    with pytest.raises(KelmoError) as error:
        hook(client, 'session-stop', {'transcript_path': str(outside), 'session_id': 's'}, 'claude')
    assert error.value.code == 'E_TRANSCRIPT' and client.outbox.pending() == []


def test_transcript_through_a_linked_profile_directory_is_captured(shared, tmp_path):
    """A second provider profile whose projects directory links to the shared transcript store names the
    link in its hook payload; the real file inside the enrolled store is captured."""
    from kelmo.client import hook
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    store = tmp_path / 'claude' / 'projects'
    (store / 'repo').mkdir(parents=True)
    (tmp_path / 'claude-second').mkdir()
    (tmp_path / 'claude-second' / 'projects').symlink_to(store)
    client = shared.client(sessions={'claude': [str(tmp_path / 'claude-second' / 'projects')]})
    (store / 'repo' / 'session.jsonl').write_bytes(_transcript([_user('Through the profile link.')]))
    payload = {'transcript_path': str(tmp_path / 'claude-second' / 'projects' / 'repo' / 'session.jsonl'),
               'session_id': 'linked'}
    assert hook(client, 'session-end', payload, 'claude') == {}
    assert client.outbox.pending() == [] and client.outbox.conn.execute(
        "SELECT state FROM entries WHERE op='session_append'").fetchone()['state'] == 'delivered'


def test_link_inside_an_enrolled_location_cannot_reach_outside(shared, tmp_path):
    from kelmo.client import hook
    from kelmo.errors import KelmoError
    enrolled = tmp_path / 'enrolled'
    enrolled.mkdir()
    (tmp_path / 'outside.jsonl').write_bytes(_transcript([_user('Not enrolled.')]))
    (enrolled / 'escape.jsonl').symlink_to(tmp_path / 'outside.jsonl')
    (enrolled / 'tunnel').symlink_to(tmp_path)
    client = shared.client(sessions={'claude': [str(enrolled)]})
    for path in (enrolled / 'escape.jsonl', enrolled / 'tunnel' / 'outside.jsonl'):
        with pytest.raises(KelmoError) as error:
            hook(client, 'session-stop', {'transcript_path': str(path), 'session_id': 's'}, 'claude')
        assert error.value.code == 'E_TRANSCRIPT', path
    assert client.outbox.pending() == []


def test_server_refuses_transcript_bytes_that_differ_from_their_hash(shared):
    from kelmo.errors import KelmoError
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    client = shared.client()
    raw = _transcript([_user('Hash me.')])
    args = {'provider': 'claude', 'native_id': 'native-2', 'event': 'Stop',
            'content': base64.b64encode(raw).decode(), 'sha256': '0' * 64, 'size': len(raw),
            'file_identity': '1:2', 'client_generation': 1}
    with pytest.raises(KelmoError) as error:
        client.submit('session_capture', args, 'bad-hash')
    assert error.value.code == 'E_SOURCE'


def test_no_server_verdict_never_settles_a_write(shared):
    """Launcher, hello and unbound replies keep every queued write pending, in order."""
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    from kelmo.server import PROTOCOL
    client = shared.client()
    shared.hub.online = False
    for key in ('first', 'second'):
        assert ClientService(client).capture('Pending ' + key, key + '.md', idempotency_key=key)['queued']
    original = client._factory

    def refused_launcher():
        raise KelmoError('E_CONFIG', 'Synthetic repairable launcher configuration.')

    class Unbound:
        """A channel whose replies answer no request: a startup refusal with request_id null."""
        def request(self, frame):
            return {'protocol': PROTOCOL, 'request_id': None, 'ok': False,
                    'error': {'code': 'E_INPUT', 'message': 'not about this request', 'retryable': False}}

        def close(self):
            pass

    for factory in (refused_launcher, Unbound):
        client._factory = factory
        failed = client.flush()
        assert failed['delivered'] == 0 and failed['refused'] == [] and failed['reached'] is False
        assert [row['idempotency_key'] for row in client.outbox.pending()] == ['first', 'second']
    client._factory = original
    shared.hub.online = True
    assert client.flush()['delivered'] == 2


def test_server_decides_retryability(shared):
    """E_GIT, E_GENERATION and E_BUDGET stay pending; a final E_CONFLICT is settled once."""
    from kelmo.server import RETRYABLE
    assert {'E_GIT', 'E_GENERATION', 'E_BUDGET'} <= RETRYABLE and 'E_CONFLICT' not in RETRYABLE
    client = shared.client()
    session = shared.hub.session('mac-a')
    original = session.write_capture

    def environment_failure(**_):
        from kelmo.errors import KelmoError
        raise KelmoError('E_GIT', 'Synthetic publication failure.')

    session.write_capture = environment_failure
    queued = client.submit('capture', {'content': 'Retry me.', 'filename': 'retry.md'}, 'retry-1')
    assert queued['queued'] is True and queued['pending_reason'] == 'E_GIT'
    session.write_capture = original
    assert client.flush()['delivered'] == 1


def test_flush_is_serialized_across_processes(shared):
    import fcntl
    from kelmo.client import ClientService
    client = shared.client()
    shared.hub.online = False
    ClientService(client).capture('Locked out.', 'locked.md', idempotency_key='locked')
    shared.hub.online = True
    with (client.profile.state / 'flush.lock').open('a') as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with client.deadline(0.3):
            busy = client.flush()
    assert busy['stopped'] == 'E_BUSY' and busy['delivered'] == 0 and busy['pending'] == 1
    assert client.flush()['delivered'] == 1


def test_session_start_has_one_deadline_and_falls_back_to_the_labelled_cache(shared, monkeypatch):
    import time
    from kelmo import client as module
    from kelmo.client import ClientService, hook
    # Claude Code's SessionStart hook timeout; the whole start must end before it.
    assert module.START_DEADLINE_SECONDS < 20
    client = shared.client()
    client.context(str(shared.app))
    attempts = []

    def failing_launcher():
        attempts.append(time.monotonic())
        time.sleep(0.2)
        from kelmo.errors import KelmoError
        raise KelmoError('E_TRANSPORT', 'Synthetic failing launcher.')

    client._drop()
    client._factory = failing_launcher
    monkeypatch.setattr(module, 'START_DEADLINE_SECONDS', 5)
    ClientService(client).capture('Queued offline.', 'offline-start.md', idempotency_key='offline-start')
    started = time.monotonic()
    context = hook(client, 'start', {'cwd': str(shared.app)})['hookSpecificOutput']['additionalContext']
    assert time.monotonic() - started < 5
    assert context.startswith('Kelmo HOT STALE CACHE (E_TRANSPORT)')
    assert len(attempts) == 2, 'one attempt for the capture, exactly one more for SessionStart'
    assert client.outbox.get('offline-start')['state'] == 'pending', 'a stale start never acknowledges a write'


def test_a_piece_that_cannot_fit_one_frame_is_refused_before_queueing(shared, tmp_path, monkeypatch):
    """A transcript has no size ceiling; only a frame limit below one piece refuses, and then nothing is
    queued, because an entry that can never be sent would block every write behind it."""
    from kelmo.client import PIECE_BYTES, hook
    from kelmo.errors import KelmoError
    from kelmo import server
    assert (PIECE_BYTES + 2) // 3 * 4 + 1048576 <= server.MAX_FRAME
    transcripts = tmp_path / 'sessions'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'big.jsonl'
    source.write_bytes(_transcript([_user('x' * 4096)]))
    monkeypatch.setattr(server, 'MAX_FRAME', 4096)
    with pytest.raises(KelmoError) as error:
        hook(client, 'session-stop', {'transcript_path': str(source), 'session_id': 'big'}, 'claude')
    assert error.value.code == 'E_INPUT' and client.outbox.pending() == []


def test_compat_update_survives_a_lost_delivery_receipt(shared, monkeypatch):
    from kelmo.client import ClientService
    from kelmo.journal import Journal
    client = shared.client()
    service = ClientService(client)
    stored = service.compat('memory_store', {'content': 'Original version.', 'kind': 'rule', 'domain': 'dev',
                                             'project': 'app'})
    original = Journal.client_request_finish
    crashed = []

    def die_after_effect(self, key, reply):
        if not crashed:
            crashed.append(key)
            raise Crash
        return original(self, key, reply)

    monkeypatch.setattr(Journal, 'client_request_finish', die_after_effect)
    with pytest.raises(Crash):
        service.compat('memory_update', {'id': stored['id'], 'content': 'Applied exactly once.',
                                         'expected_version': 1})
    assert shared.world.service.show(stored['id'])['content'] == 'Applied exactly once.'
    shared.hub.reset()
    retried = client.flush()
    assert retried['delivered'] == 1 and retried['refused'] == []
    assert shared.world.service.show(stored['id'])['version'] == 2
    # Equal content under another request identity is not this request's receipt.
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as error:
        service.compat('memory_update', {'id': stored['id'], 'content': 'Applied exactly once.',
                                         'expected_version': 1})
    assert error.value.code == 'E_CONFLICT'


def test_offline_compat_update_is_queued_and_checks_the_version_on_delivery(shared):
    from kelmo.client import ClientService
    from kelmo.mcp import compat
    client = shared.client()
    service = ClientService(client)
    stored = service.compat('memory_store', {'content': 'Original.', 'kind': 'rule', 'domain': 'dev'})
    shared.hub.online = False
    queued = service.compat('memory_update', {'id': stored['id'], 'content': 'Queued replacement.',
                                             'expected_version': 1})
    assert queued['queued'] is True and queued['accepted'] is False
    assert not {'id', 'version', 'evidence_ref'} & set(queued)
    assert shared.world.service.show(stored['id'])['version'] == 1
    compat(shared.world.service, 'memory_update', {'id': stored['id'], 'content': 'Moved head.',
                                                   'expected_version': 1})
    shared.hub.online = True
    result = client.flush()
    assert result['refused'][0]['code'] == 'E_CONFLICT' and result['pending'] == 0
    assert shared.world.service.show(stored['id'])['content'] == 'Moved head.'


def test_real_stdio_channel_round_trip_and_replay(shared):
    """The configured channel is a real child process speaking frames over stdin/stdout."""
    import kelmo
    from kelmo.client import ClientService, SharedClient, prepare_profile
    from kelmo.config import ClientProfile
    profile = shared.tmp_path / 'server-profile.json'
    profile.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
    source = str(Path(kelmo.__file__).resolve().parents[1])
    boot = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from kelmo.cli import main; '
            'raise SystemExit(main(sys.argv[1:]))')
    root = shared.tmp_path / 'client-stdio'
    prepare_profile(root, shared.world.bindings.authority_id,
                    [sys.executable, '-c', boot, source, 'serve-client', '--profile', str(profile),
                     '--client-id', 'mac-b', '--vault', str(shared.world.root)])
    with SharedClient(ClientProfile.load(root)) as client:
        service = ClientService(client)
        hello = client.call('hello')
        assert hello['client_id'] == 'mac-b' and hello['archive'] == {'mode': 'in-process', 'confined': False}
        first = service.capture('Collected over stdio.', 'stdio.md', role='collector', idempotency_key='stdio-1')
        assert first['state'] == 'captured'
        # The same key is answered from the server ledger, even through a fresh channel.
        client._drop()
        ok, replay = client._send(client._connect(), 'capture', json.loads(client.outbox.get('stdio-1')['args']),
                                  'stdio-1')
        assert ok and replay['operation'] == first
    assert len(operations(shared.world, 'stdio.md')) == 1
    with SharedClient(ClientProfile.load(root)) as client:
        status = client.call('status')
        assert status['writer'] == 'shared' and status['client_id'] == 'mac-b'
    assert os.environ.get('KELMO_INTERNAL_JOB') != '1'


@pytest.mark.parametrize('step', ['client_request', 'client_request_claim', 'client_request_finish'])
def test_a_ledger_failure_around_a_write_keeps_it_pending(shared, monkeypatch, step):
    """A busy ledger read, claim or settlement (finish runs after the effect committed) is retryable:
    the entry stays pending and the retry settles the one operation under the same key."""
    import sqlite3
    from kelmo.client import ClientService
    from kelmo.journal import Journal
    client = shared.client()
    original = getattr(Journal, step)
    failed = []

    def locked_once(self, *args):
        if not failed:
            failed.append(step)
            raise sqlite3.OperationalError('database is locked')
        return original(self, *args)

    monkeypatch.setattr(Journal, step, locked_once)
    key = 'ledger-' + step.replace('_', '-')
    queued = capture(ClientService(client), 'Ledger failure at ' + step + '.', key + '.md', key)
    assert queued['queued'] is True and queued['pending_reason'] == 'E_BUSY'
    assert client.outbox.get(key)['state'] == 'pending'
    assert client.flush()['delivered'] == 1
    [operation_id] = operations(shared.world, key + '.md')
    assert json.loads(client.outbox.get(key)['outcome'])['operation']['id'] == operation_id


def test_queue_retry_reaches_the_server_for_its_own_client_only(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    owner = ClientService(shared.client('mac-a'))
    operation_id = capture(owner, 'Needs a repair.', 'repair.md', 'repair-1')['operation']['id']
    shared.world.service.journal.stage(operation_id, 'failed', terminal=True, error_code='E_SOURCE')
    with pytest.raises(KelmoError) as error:
        ClientService(shared.client('mac-b')).retry(operation_id)
    assert error.value.code == 'E_NOT_FOUND'
    assert shared.world.service.journal.get(operation_id)['state'] == 'failed'
    repaired = owner.retry(operation_id)
    assert repaired['operation']['id'] == operation_id and repaired['operation']['state'] == 'queued'


def test_an_operation_the_server_does_not_offer_is_never_sent(shared):
    from kelmo.client import ClientService
    client = shared.client()
    client.call('status')
    client.server = dict(client.server, operations=[op for op in client.server['operations'] if op != 'forget'])
    sent = len(shared.hub.requests)
    queued = ClientService(client).forget('any-record')
    assert queued['queued'] is True and queued['pending_reason'] == 'E_CAPABILITY'
    assert len(shared.hub.requests) == sent, 'nothing reached the server'
    assert client._channel is not None, 'a local refusal keeps the healthy channel'


def test_a_write_that_cannot_fit_one_frame_is_refused_before_it_is_queued(shared, monkeypatch):
    from kelmo import server
    from kelmo.errors import KelmoError
    client = shared.client()
    monkeypatch.setattr(server, 'MAX_FRAME', 4096)
    with pytest.raises(KelmoError) as error:
        client.submit('capture', {'content': 'x' * 5000, 'filename': 'big.md', 'role': 'collector'}, 'big-1')
    assert error.value.code == 'E_INPUT' and client.outbox.get('big-1') is None


def test_session_start_keeps_time_for_the_cache_when_the_flush_lock_is_held(shared, monkeypatch):
    import fcntl
    import time
    from kelmo import client as module
    client = shared.client()
    client.context(str(shared.app))
    monkeypatch.setattr(module, 'START_DEADLINE_SECONDS', 1.5)
    with (client.profile.state / 'flush.lock').open('a') as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        started = time.monotonic()
        context = module.hook(client, 'start', {'cwd': str(shared.app)})['hookSpecificOutput']['additionalContext']
        elapsed = time.monotonic() - started
    assert context.startswith('Kelmo HOT STALE CACHE (E_BUSY)')
    assert elapsed < 1.5, 'the flush stopped a reserve early and the cache was emitted inside the deadline'


def _pieces(client, since=0):
    rows = client.outbox.conn.execute('SELECT seq, state, args, outcome FROM entries WHERE seq > ? ORDER BY seq',
                                      (since,)).fetchall()
    return [dict(row, args=json.loads(row['args']), outcome=json.loads(row['outcome']) if row['outcome'] else None)
            for row in rows]


def test_a_transcript_travels_in_pieces_from_the_last_delivered_position(shared, tmp_path, monkeypatch):
    """A capture sends the bytes after the ones already delivered, in bounded pieces that each name the
    bytes they follow; the server rebuilds the transcript and captures it on the final piece. Delivered
    pieces keep their receipt, not their bytes."""
    from kelmo import client as module
    from kelmo.client import hook
    from kelmo.config import Config
    from kelmo.sessions import Sessions
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    monkeypatch.setattr(module, 'PIECE_BYTES', 64)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    first = _transcript([_user('Piece one of the transcript.'), _assistant('Piece two follows it.')])
    source.write_bytes(first)
    payload = {'transcript_path': str(source), 'session_id': 'pieces'}
    assert hook(client, 'session-checkpoint', payload, 'claude') == {}
    rows = _pieces(client)
    assert {row['state'] for row in rows} == {'delivered'}
    assert [row['args']['offset'] for row in rows] == list(range(0, len(first), 64))
    assert [row['args']['final'] for row in rows] == [False] * (len(rows) - 1) + [True]
    assert all(row['args']['content'] == '' for row in rows)
    assert [row['outcome']['captured'] for row in rows] == [False] * (len(rows) - 1) + [True]
    assert all(row['outcome']['appended'] for row in rows)
    last = rows[-1]['outcome']
    assert last['sha256'] == hashlib.sha256(first).hexdigest() and last['range'] == [0, len(first)]
    assert last['records'] == 2 and last['generation'] == 1

    grown = first + _transcript([_user('Only this tail travels now.')])
    source.write_bytes(grown)
    hook(client, 'session-checkpoint', payload, 'claude')
    tail = _pieces(client, rows[-1]['seq'])
    assert tail[0]['args']['offset'] == len(first)
    assert tail[0]['args']['base_sha256'] == hashlib.sha256(first).hexdigest()
    assert sum(row['args']['size'] for row in tail) == len(grown) - len(first)
    assert tail[-1]['args']['sha256'] == hashlib.sha256(grown).hexdigest()
    position = client.outbox.conn.execute('SELECT * FROM transcripts').fetchone()
    assert position['sent_bytes'] == len(grown) and position['generation'] == 1
    assert position['sent_sha256'] == hashlib.sha256(grown).hexdigest()

    sessions = Sessions(Config.load(shared.world.root))
    try:
        latest = sessions.journal.conn.execute('SELECT latest_hash, generation FROM sessions').fetchone()
        assert latest['generation'] == 1 and sessions.snapshot(latest['latest_hash']) == grown
    finally:
        sessions.close()


def test_an_event_without_new_bytes_still_reaches_the_server(shared, tmp_path):
    from kelmo.client import hook
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    raw = _transcript([_user('Nothing changes after this.')])
    source.write_bytes(raw)
    payload = {'transcript_path': str(source), 'session_id': 'quiet'}
    hook(client, 'session-checkpoint', payload, 'claude')
    hook(client, 'session-end', payload, 'claude')
    [checkpoint, final] = _pieces(client)
    assert final['args']['offset'] == len(raw) and final['args']['size'] == 0 and final['args']['content'] == ''
    assert final['args']['base_sha256'] == final['args']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert final['outcome']['captured'] is True and final['outcome']['state'] == 'queued'
    assert final['outcome']['job_id'] != checkpoint['outcome']['job_id'], 'the final job was recorded'


def test_a_piece_whose_base_the_server_lost_restarts_the_transcript(shared, tmp_path, monkeypatch):
    """The server refuses a piece that does not follow the bytes it holds (E_CAPTURE_BASE, final); the
    client forgets its sent position and sends the transcript again from its start."""
    from kelmo import client as module
    from kelmo.client import hook
    from kelmo.config import Config
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    monkeypatch.setattr(module, 'PIECE_BYTES', 64)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    first = _transcript([_user('Held by the server until it is not.')])
    source.write_bytes(first)
    payload = {'transcript_path': str(source), 'session_id': 'lost-base'}
    hook(client, 'session-checkpoint', payload, 'claude')
    before = _pieces(client)[-1]['seq']
    [spool] = list((Config.load(shared.world.root).state / 'client-sessions').rglob('*.jsonl'))
    spool.write_bytes(spool.read_bytes()[:-5])
    grown = first + _transcript([_user('Written after the loss.')])
    source.write_bytes(grown)
    hook(client, 'session-checkpoint', payload, 'claude')
    rows = _pieces(client, before)
    refused = [row for row in rows if row['state'] == 'refused']
    assert refused and {row['outcome']['code'] for row in refused} == {'E_CAPTURE_BASE'}
    assert refused[0]['args']['offset'] == len(first)
    delivered = [row for row in rows if row['state'] == 'delivered']
    assert delivered[0]['args']['offset'] == 0 and delivered[0]['args']['client_generation'] == 1
    assert delivered[-1]['args']['final'] and delivered[-1]['outcome']['sha256'] == hashlib.sha256(grown).hexdigest()
    assert spool.read_bytes() == grown and client.outbox.pending() == []
    position = client.outbox.conn.execute('SELECT * FROM transcripts').fetchone()
    assert position['sent_bytes'] == len(grown) and position['generation'] == 1


def test_a_lost_reply_to_a_piece_replays_without_a_second_write(shared, tmp_path, monkeypatch):
    from kelmo import client as module
    from kelmo.client import hook
    from kelmo.config import Config
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    monkeypatch.setattr(module, 'PIECE_BYTES', 64)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    first = _transcript([_user('Delivered before the reply was lost.')])
    source.write_bytes(first)
    payload = {'transcript_path': str(source), 'session_id': 'replay'}
    hook(client, 'session-checkpoint', payload, 'claude')
    before = _pieces(client)[-1]['seq']
    grown = first + _transcript([_user('Three pieces of tail, one reply lost.'), _assistant('Replayed.')])
    source.write_bytes(grown)
    shared.hub.drop_replies = 1
    hook(client, 'session-checkpoint', payload, 'claude')
    pending = client.outbox.pending()
    assert len(pending) == len(_pieces(client, before)) and pending[0]['last_error'] == 'E_TRANSPORT'
    assert client.flush()['delivered'] == len(pending)
    rows = _pieces(client, before)
    assert rows[-1]['outcome']['captured'] is True
    sent = [request['idempotency_key'] for _, request in shared.hub.requests if request.get('op') == 'session_append']
    assert sent.count(pending[0]['idempotency_key']) == 2, 'the lost piece was sent again under its key'
    ledger = shared.world.service.journal.conn.execute('SELECT state FROM client_requests').fetchall()
    assert {row['state'] for row in ledger} == {'finished'}
    [spool] = list((Config.load(shared.world.root).state / 'client-sessions').rglob('*.jsonl'))
    assert spool.read_bytes() == grown


def test_a_hook_with_a_flusher_queues_and_detaches_delivery(shared, tmp_path, monkeypatch):
    """Under a provider's hook timeout the hook only queues; the detached flusher it starts owns delivery."""
    from kelmo import client as module
    from kelmo.client import hook
    started = []
    monkeypatch.setattr(module.subprocess, 'Popen', lambda argv, **kwargs: started.append((argv, kwargs)))
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    source.write_bytes(_transcript([_user('Queued, not yet sent.')]))
    payload = {'transcript_path': str(source), 'session_id': 'detached'}
    flusher = ['/prefix/bin/kelmo', 'client', 'flush', '--vault', str(client.profile.root)]
    assert hook(client, 'session-stop', payload, 'claude', flusher=flusher) == {}
    assert [row['state'] for row in _pieces(client)] == ['pending']
    [(argv, kwargs)] = started
    assert argv == flusher and kwargs['start_new_session'] is True
    assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == module.subprocess.DEVNULL
    assert client.flush()['delivered'] == 1


def test_settled_transcript_entries_drop_their_bytes_on_open(tmp_path):
    """An outbox written by the older client holds every delivered transcript whole and lacks the sent
    position; opening it sheds the bytes and adds the columns."""
    import sqlite3
    from kelmo.client import Outbox, canonical
    directory = tmp_path / 'client'
    directory.mkdir()
    db = sqlite3.connect(directory / 'outbox.sqlite3')
    with db:
        db.execute("""CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
            op TEXT NOT NULL, args TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, settled_at TEXT, outcome TEXT)""")
        db.execute("""CREATE TABLE transcripts (provider TEXT NOT NULL, native_id TEXT NOT NULL, file_identity TEXT NOT NULL,
            size INTEGER NOT NULL, generation INTEGER NOT NULL, PRIMARY KEY (provider, native_id))""")
        for key, state in (('old-delivered', 'delivered'), ('old-pending', 'pending')):
            db.execute("INSERT INTO entries (idempotency_key,op,args,fingerprint,state,created_at) VALUES (?,?,?,?,?,?)",
                       (key, 'session_capture', canonical({'content': 'QUJD', 'size': 3}), 'f', state, 't'))
        db.execute('INSERT INTO transcripts VALUES (?,?,?,?,?)', ('claude', 'old', '1:2', 3, 4))
    db.close()
    outbox = Outbox(directory)
    try:
        assert json.loads(outbox.get('old-delivered')['args'])['content'] == ''
        assert json.loads(outbox.get('old-pending')['args'])['content'] == 'QUJD', 'a pending entry keeps its bytes'
        row = outbox.conn.execute('SELECT * FROM transcripts').fetchone()
        assert (row['generation'], row['sent_bytes'], row['sent_sha256']) == (4, 0, '')
    finally:
        outbox.close()


def _growing_os(monkeypatch, module, grown_after_calls):
    """An `os` for the client module whose post-read stat reports a larger file for the first
    `grown_after_calls` reads: a provider appending while the hook reads."""
    import types
    real = module.os
    proxy = types.SimpleNamespace(**{name: getattr(real, name) for name in dir(real) if not name.startswith('__')})
    calls = []

    def fstat(descriptor):
        info = real.fstat(descriptor)
        calls.append(info.st_size)
        if len(calls) % 2 == 0 and len(calls) // 2 <= grown_after_calls:
            fields = list(info)
            fields[6] = info.st_size + 7
            return real.stat_result(tuple(fields))
        return info

    proxy.fstat = fstat
    monkeypatch.setattr(module, 'os', proxy)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    return calls


def _hook_client(shared, tmp_path):
    server_config(shared.world.root, sessions='archive-only')
    shared.hub.reset()
    transcripts = tmp_path / 'provider' / 'projects'
    transcripts.mkdir(parents=True)
    client = shared.client(sessions={'claude': [str(transcripts)]})
    source = transcripts / 'session.jsonl'
    content = _transcript([_user('Plan the outbox.'), _assistant('Stored first.')])
    source.write_bytes(content)
    return client, source, content


def test_transcript_appended_during_the_read_is_captured_after_a_retry(shared, tmp_path, monkeypatch):
    from kelmo import client as module
    client, source, content = _hook_client(shared, tmp_path)
    calls = _growing_os(monkeypatch, module, 2)
    shared.hub.drop_replies = 1
    assert module.hook(client, 'session-checkpoint', {'transcript_path': str(source), 'session_id': 'native-1'},
                       'claude') == {}
    pending = client.outbox.pending()
    assert len(pending) == 1 and json.loads(pending[0]['args'])['sha256'] == hashlib.sha256(content).hexdigest()
    assert len(calls) == 6  # two reads saw growth, the third was stable
    assert client.local_status()['outbox']['skipped'] == {}


def test_transcript_that_keeps_growing_is_captured_up_to_the_first_size(shared, tmp_path, monkeypatch):
    from kelmo import client as module
    client, source, content = _hook_client(shared, tmp_path)
    calls = _growing_os(monkeypatch, module, 100)
    shared.hub.drop_replies = 1
    assert module.hook(client, 'session-checkpoint', {'transcript_path': str(source), 'session_id': 'native-1'},
                       'claude') == {}
    pending = client.outbox.pending()
    assert len(pending) == 1 and json.loads(pending[0]['args'])['sha256'] == hashlib.sha256(content).hexdigest()
    assert len(calls) == 2 * module.CAPTURE_READ_ATTEMPTS


def test_transcript_replaced_during_the_read_is_skipped_quietly(shared, tmp_path, monkeypatch):
    import types
    from kelmo import client as module
    client, source, content = _hook_client(shared, tmp_path)
    real = module.os
    proxy = types.SimpleNamespace(**{name: getattr(real, name) for name in dir(real) if not name.startswith('__')})
    calls = []

    def fstat(descriptor):
        info = real.fstat(descriptor)
        calls.append(info.st_size)
        if len(calls) % 2 == 0:
            fields = list(info)
            fields[6] = info.st_size - 1  # shrank: a replaced or truncated file, never a safe prefix
            return real.stat_result(tuple(fields))
        return info

    proxy.fstat = fstat
    monkeypatch.setattr(module, 'os', proxy)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    assert module.hook(client, 'session-checkpoint', {'transcript_path': str(source), 'session_id': 'native-1'},
                       'claude') == {}
    assert client.outbox.pending() == []
    assert client.local_status()['outbox']['skipped'] == {'transcript-changing': 1}
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as error:
        client.capture_session('claude', 'session-checkpoint',
                               {'transcript_path': str(source), 'session_id': 'native-1'})
    assert error.value.code == 'E_CAPTURE_RETRY'


def test_a_headless_run_never_leaves_the_machine(shared, tmp_path):
    from kelmo import client as module
    client, source, _ = _hook_client(shared, tmp_path)
    helper = source.with_name('helper.jsonl')
    helper.write_bytes(_transcript([dict(_user('Research one question.'), entrypoint='sdk-cli'),
                                    _assistant('Answer.')]))
    payload = {'transcript_path': str(helper), 'session_id': 'helper-1'}
    assert module.hook(client, 'session-end', payload, 'claude') == {}
    assert client.outbox.pending() == []
    assert client.local_status()['outbox']['skipped'] == {'headless': 1}


def test_a_session_that_ends_before_its_transcript_exists_is_skipped_quietly(shared, tmp_path):
    from kelmo import client as module
    from kelmo.errors import KelmoError
    client, source, _ = _hook_client(shared, tmp_path)
    absent = source.with_name('never-written.jsonl')
    payload = {'transcript_path': str(absent), 'session_id': 'native-2'}
    assert module.hook(client, 'session-end', payload, 'claude') == {}
    assert client.outbox.pending() == []
    assert client.local_status()['outbox']['skipped'] == {'transcript-absent': 1}
    # An unreadable file that does exist still fails the hook.
    source.chmod(0)
    try:
        with pytest.raises(KelmoError) as error:
            module.hook(client, 'session-end', {'transcript_path': str(source), 'session_id': 'native-1'},
                        'claude')
    finally:
        source.chmod(0o600)
    assert error.value.code == 'E_INPUT'


def test_the_outbox_gives_back_the_space_of_settled_pieces_and_counts_old_refusals(tmp_path):
    from kelmo.client import REFUSED_SHOWN, Outbox
    outbox = Outbox(tmp_path / 'client')
    try:
        # Pieces queue faster than they settle, so their bytes sit in the file together.
        for index in range(40):
            outbox.enqueue('session_append', {'content': 'x' * 400000, 'provider': 'claude'}, f'piece-{index}')
        for index in range(40):
            outbox.settle(f'piece-{index}', 'delivered', {'ok': True})
        assert outbox.compact(min_free_bytes=1 << 30) == 0, 'below the threshold nothing is rewritten'
        before = (tmp_path / 'client/outbox.sqlite3').stat().st_size
        given = outbox.compact(min_free_bytes=1048576)
        assert given > 10 * 1048576 and (tmp_path / 'client/outbox.sqlite3').stat().st_size < before - given // 2
        assert outbox.compact(min_free_bytes=1048576) == 0
        for index in range(REFUSED_SHOWN + 5):
            key = f'refused-{index}'
            outbox.enqueue('create', {'n': index}, key)
            outbox.settle(key, 'refused', {'code': 'E_INPUT' if index % 2 else 'E_FACT_CHECK', 'message': 'no'})
        summary = outbox.summary()
        assert len(summary['refused']) == REFUSED_SHOWN and summary['refused_total'] == REFUSED_SHOWN + 5
        assert summary['refused'][-1]['idempotency_key'] == f'refused-{REFUSED_SHOWN + 4}', 'newest last'
        assert summary['refused_by_code'] == {'E_INPUT': 12, 'E_FACT_CHECK': 13}
    finally:
        outbox.close()


def test_refused_notes_are_resent_under_new_keys_only_on_apply(tmp_path):
    import json
    from kelmo.client import Outbox
    outbox = Outbox(tmp_path / 'client')
    try:
        args = {'content': 'A note for life.', 'project': 'life', 'selected': ['life/life']}
        outbox.enqueue('create', args, 'note_1%')
        outbox.settle('note_1%', 'refused', {'code': 'E_INPUT', 'message': 'Page is not registered: life/life'})
        outbox.enqueue('capture', {'content': 'Archive only.'}, 'capture-1')
        outbox.settle('capture-1', 'refused', {'code': 'E_INPUT', 'message': 'no'})
        outbox.enqueue('create', {'content': 'Stale.', 'project': 'app'}, 'stale')
        outbox.settle('stale', 'refused', {'code': 'E_FACT_CHECK', 'message': 'no'})
        preview = outbox.resend(('E_INPUT',))
        assert [(item['idempotency_key'], item['resend_as']) for item in preview] == [('note_1%', 'note_1%#resend1')]
        assert outbox.get('note_1%')['state'] == 'refused' and outbox.get('note_1%#resend1') is None
        outbox.resend(('E_INPUT',), apply=True)
        assert outbox.resend(('E_INPUT',), apply=True) == []
        assert json.loads(outbox.get('note_1%#resend1')['args']) == args
        assert outbox.get('note_1%#resend1')['state'] == 'pending'
        assert json.loads(outbox.get('note_1%')['outcome'])['resent_as'] == 'note_1%#resend1'
        # A resend refused again goes out once more under the next key.
        outbox.settle('note_1%#resend1', 'refused', {'code': 'E_INPUT', 'message': 'still'})
        assert outbox.resend(('E_INPUT',), apply=True)[0]['resend_as'] == 'note_1%#resend2'
        summary = outbox.summary()
        assert summary['resent'] == 2 and summary['pending'] == 1
        assert {item['idempotency_key'] for item in summary['refused']} == {'capture-1', 'stale'}
    finally:
        outbox.close()


def test_a_resend_sends_each_text_once(tmp_path):
    import json
    from kelmo.client import Outbox
    outbox = Outbox(tmp_path / 'client')
    try:
        def refused(key, content, target):
            outbox.enqueue('create', {'content': content, 'project': 'app', 'selected': [target]}, key)
            outbox.settle(key, 'refused', {'code': 'E_INPUT', 'message': 'Page is not registered'})

        refused('first-spelling', 'Delivered later.', 'app')
        outbox.enqueue('create', {'content': 'Delivered later.', 'project': 'app', 'selected': ['app.md']}, 'fixed')
        outbox.settle('fixed', 'delivered', {'id': 'op-1'})
        refused('older', 'Never delivered.\n', 'app')
        refused('newer', 'Never delivered.', 'app.md')
        items = {item['idempotency_key']: item for item in outbox.resend(('E_INPUT',), apply=True)}
        assert items['first-spelling'] == dict(items['first-spelling'], resend_as='fixed', duplicate=True)
        assert items['newer']['resend_as'] == 'newer#resend1' and 'duplicate' not in items['newer']
        assert items['older'] == dict(items['older'], resend_as='newer#resend1', duplicate=True)
        assert [row['idempotency_key'] for row in outbox.pending()] == ['newer#resend1']
        assert json.loads(outbox.get('newer#resend1')['args'])['selected'] == ['app.md']
        assert outbox.summary()['refused'] == []
    finally:
        outbox.close()


def test_two_resend_runs_at_once_send_a_note_once(tmp_path):
    import threading
    from kelmo.client import Outbox
    first, second = Outbox(tmp_path / 'client'), Outbox(tmp_path / 'client')
    try:
        first.enqueue('create', {'content': 'A note.', 'project': 'app'}, 'note')
        first.settle('note', 'refused', {'code': 'E_INPUT', 'message': 'no'})
        results, insert = {}, first._insert

        def insert_while_another_run_starts(op, args, key):
            # The second run starts while the first holds the write lock, then the first inserts.
            results['thread'] = threading.Thread(
                target=lambda: results.setdefault('second', second.resend(('E_INPUT',), apply=True)))
            results['thread'].start()
            results['thread'].join(0.5)
            insert(op, args, key)

        first._insert = insert_while_another_run_starts
        assert [item['resend_as'] for item in first.resend(('E_INPUT',), apply=True)] == ['note#resend1']
        results['thread'].join(10)
        assert results['second'] == [] and first.get('note#resend2') is None
    finally:
        first.close()
        second.close()


def test_an_outbox_head_waiting_over_an_hour_is_named(tmp_path):
    import time
    from kelmo.client import OUTBOX_STALL_SECONDS, Outbox, outbox_stall_warning
    outbox = Outbox(tmp_path / 'outbox')
    try:
        assert 'oldest_pending' not in outbox.summary() and outbox_stall_warning(outbox.summary()) is None
        outbox.enqueue('capture', {'content': 'first'}, 'key-1')
        outbox.enqueue('capture', {'content': 'second'}, 'key-2')
        outbox.note('key-1', 'E_BUSY')
        summary = outbox.summary()
        assert summary['oldest_pending']['idempotency_key'] == 'key-1'
        assert outbox_stall_warning(summary) is None, 'a fresh head is the queue working'
        warning = outbox_stall_warning(summary, clock=lambda: time.time() + OUTBOX_STALL_SECONDS + 120)
        assert '2 write(s)' in warning and 'key-1' in warning and 'E_BUSY' in warning
    finally:
        outbox.close()
