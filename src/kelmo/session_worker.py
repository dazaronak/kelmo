"""Archive complete session notes before independently considering wiki changes."""
from contextlib import contextmanager
import fcntl
import json
import os
import sqlite3
import stat
from pathlib import Path
import time

from .config import Config
from .errors import KelmoError, sqlite_busy
from .sessions import Sessions
from .session_transcripts import parse
from .service import Service
from .wiki import engine
from .journal import when_free
from . import quota

TERMINAL = {'wiki_applied', 'wiki_unchanged', 'skipped'}
BUSY_RETRY_SECONDS = 60


@contextmanager
def processing_lock(config):
    path = config.internal('.state/session-worker.lock')
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        raise KelmoError('E_STATE', 'Cannot open the session worker lock.') from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise KelmoError('E_STATE', 'Session worker lock must be a regular file.')
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise KelmoError('E_BUSY', 'Session processing is already active.') from error
        yield


def retry(root, job_id):
    config = Config.load(root)
    with processing_lock(config):
        sessions = Sessions(config)
        try:
            job = sessions.job(job_id)
            if job['state'] not in ('needs_review','wiki_needs_review','pending'):
                raise KelmoError('E_STATE', 'Only pending or reviewed session jobs can be retried.')
            with sessions.journal.conn as db:
                db.execute("UPDATE session_jobs SET state='pending',attempts=0 WHERE id=?", (job_id,))
                db.execute('UPDATE session_processing SET attempts=0,eligible=0,error_code=NULL WHERE job_id=?',(job_id,))
            return sessions.job(job_id)
        finally:
            sessions.close()


STUB_FIELDS = ('command', 'file_path', 'path', 'pattern', 'query', 'url', 'description', 'prompt')
ERROR_MARKERS = ('Traceback (most recent call last)', '<tool_use_error>', 'Error:', 'error:', 'FAILED',
                 'Exit code ', 'exit status ', 'Permission denied', 'No such file')


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit] + f' […{len(text) - limit} chars cut]'


def _stub(text, limit):
    """A tool call as one line: its name and the argument that says what it did."""
    try:
        call = json.loads(text)
    except ValueError:
        return _cut(' '.join(text.split()), limit)
    if not isinstance(call, dict):
        return _cut(' '.join(text.split()), limit)
    name = call.get('name') or 'tool'
    arguments = call.get('input') if call.get('input') is not None else call.get('arguments')
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if isinstance(arguments, dict):
        named = next((arguments[key] for key in STUB_FIELDS if isinstance(arguments.get(key), str)), None)
        detail = named if named is not None else json.dumps(arguments, ensure_ascii=False)
    else:
        detail = '' if arguments is None else str(arguments)
    return _cut(name + ': ' + ' '.join(detail.split()), limit)


def reduce(messages, settings):
    """The transcript as extraction reads it: the conversation whole, tool traffic shortened.

    Tool traffic is most of a working session's bytes and little of what a note needs. A tool call becomes one line (its name and the argument that says what it did); a
    result stays whole while it is short, an error result is kept up to its own cap, and any other
    result becomes a marker naming its size. Nothing is dropped silently: every shortened message says
    what was left out.
    """
    reduced = []
    for message in messages:
        role, text = message['role'], message['text']
        if role == 'tool_call':
            text = _stub(text, settings['tool_stub_chars'])
        elif role == 'tool':
            if message.get('error') or any(marker in text[:4000] for marker in ERROR_MARKERS):
                text = _cut(text, settings['tool_error_chars']) if settings['tool_error_chars'] else (
                    f'[tool error, {len(text)} chars omitted]')
            elif len(text) > settings['tool_result_chars']:
                text = f'[tool result, {len(text)} chars omitted]'
        reduced.append(dict(message, text=text))
    return reduced


def pieces(messages, limit):
    """Every message split into pieces that fit an extraction chunk, in transcript order."""
    for message in messages:
        text = message['text']
        offset = 0
        while offset < len(text):
            length = min(len(text)-offset, max(1,limit//8))
            piece = {**message,'text':text[offset:offset+length], 'character_offset':offset}
            if len(json.dumps([piece],ensure_ascii=False).encode()) > limit-256:
                raise KelmoError('E_INPUT','Session message metadata exceeds the input ceiling.')
            yield piece
            offset += length


def chunks(messages, limit):
    """Complete extraction: every piece, packed into chunks of at most `limit` bytes."""
    current = []
    for piece in pieces(messages, limit):
        if current and len(json.dumps(current+[piece],ensure_ascii=False).encode()) > limit-256:
            yield current
            current = []
        current.append(piece)
    if current:
        yield current


def tail(messages, limit):
    """Tail extraction: the newest pieces that fill one chunk, in transcript order, and the number of
    older pieces left out: one model call over the tail, never one per chunk of the whole conversation."""
    kept = []
    remaining = list(pieces(messages, limit))
    while remaining:
        piece = remaining[-1]
        if kept and len(json.dumps([piece]+kept,ensure_ascii=False).encode()) > limit-256:
            break
        kept.insert(0, remaining.pop())
    return kept, len(remaining)


def validate_extraction(reply):
    if not isinstance(reply,dict) or set(reply) != {'note_markdown','candidate_facts','skip'}:
        raise KelmoError('E_EXTRACTION','Extraction response must contain note_markdown, candidate_facts and skip.')
    note = reply['note_markdown']
    if not isinstance(note,str) or type(reply['skip']) is not bool or len(note.encode()) > 1048576:
        raise KelmoError('E_EXTRACTION','Extraction note is invalid or oversized.')
    if reply['skip']:
        if note.strip() or reply['candidate_facts'] != []:
            raise KelmoError('E_EXTRACTION','A skipped extraction cannot contain a note or facts.')
    elif not note.strip():
        raise KelmoError('E_EXTRACTION','A non-skipped extraction must contain the complete note.')
    return reply


def proposal(service, note, candidates, argv, execute, timeout):
    if not isinstance(candidates,list) or len(candidates)>30:
        raise KelmoError('E_PROPOSAL','Session candidates must be a bounded fact list.')
    requests = []
    for index,candidate in enumerate(candidates):
        if not isinstance(candidate,dict) or set(candidate)-{'text','target','status'} or not {'text','target'}<=set(candidate):
            raise KelmoError('E_PROPOSAL','Invalid session fact candidate.')
        change = {'fact_key':f'session.candidate.{index}', 'text':candidate['text'],
                  'target':candidate['target'],'status':candidate.get('status','active'),
                  'expected_revision':None,'replaces':[]}
        try:
            engine.validate_changes([change])
        except (ValueError,TypeError) as error:
            raise KelmoError('E_PROPOSAL','Session fact candidate '+str(index)+': '+str(error)) from error
        search = service.reconcile(candidate['text'], candidate['target'])
        requests.append({'index':index,'candidate':candidate,'search':search})
    if not requests:
        return {'changes':[],'checks':[],'calls':0}
    payload = {'task':'reconcile_session','note':note,'candidates':requests}
    if len(json.dumps(payload,ensure_ascii=False).encode())>2*1048576:
        raise KelmoError('E_PROPOSAL','Session reconciliation requires a smaller reviewed proposal.')
    reply = quota.checked(execute(argv,payload,timeout=timeout))
    decisions = reply.get('decisions') if isinstance(reply,dict) else None
    if not isinstance(decisions,list) or len(decisions)!=len(requests):
        raise KelmoError('E_PROPOSAL','Every candidate requires an explicit reconciliation decision.')
    changes,checks,seen = [],[],set()
    for decision in decisions:
        if (not isinstance(decision,dict) or type(decision.get('index')) is not int
                or decision['index'] in seen or not 0<=decision['index']<len(requests)):
            raise KelmoError('E_PROPOSAL','Invalid candidate reconciliation index.')
        seen.add(decision['index'])
        request = requests[decision['index']]
        action = decision.get('action')
        if action=='skip' and set(decision)=={'index','action'}:
            continue
        if action not in ('new','update') or set(decision)!={'index','action','fact_key'}:
            raise KelmoError('E_PROPOSAL','Unresolved candidate reconciliation: candidate '+str(decision['index'])
                            +' on page '+request['candidate']['target']+' needs action new or update')
        key = decision['fact_key']
        candidate = request['candidate']
        existing = next((item for item in request['search']['candidates'] if item['fact_key']==key),None)
        if action=='update' and existing is None:
            raise KelmoError('E_PROPOSAL','An update must name an actually retrieved fact: candidate '
                            +str(decision['index'])+' named '+key+', which the search for '+candidate['target']
                            +' did not retrieve')
        change = {'fact_key':key,'text':candidate['text'],
                  'target':existing['target'] if action=='update' else candidate['target'],
                  'status':candidate.get('status','active'),
                  'expected_revision':existing['revision'] if action=='update' else None,'replaces':[]}
        check = ({'result':'existing','fact_key':key,'revision':existing['revision']} if action=='update'
                 else {'result':'not_found','search_id':request['search']['search_id']})
        changes.append(change)
        checks.append({'fact_key':key,'verified_match':check})
    try:
        engine.validate_changes(changes)
    except (ValueError,TypeError) as error:
        raise KelmoError('E_PROPOSAL','Reconciled session changes are invalid: '+str(error)) from error
    return {'changes':changes,'checks':checks,'calls':1}


def _note_service(sessions, job, open_service):
    """The Service the session note is archived through: the local vault's own, or under a server profile
    the one open_service builds for the enrolled client that captured the session."""
    if open_service is None:
        return Service(sessions.config.root)
    from .server import session_client
    return open_service(session_client(sessions, job['session_key']))


PAGE_LIST_LIMIT = 300


def _registered_pages(config):
    """Page targets a candidate fact may name: promotion reaches registered pages only. Bounded, so a
    large wiki cannot crowd out the session evidence."""
    try:
        targets = sorted(engine.load_registry(config.root)['pages'])
    except (KelmoError, OSError, ValueError):
        return []
    return targets[:PAGE_LIST_LIMIT]


def _run(sessions, job, settings, argv, execute, timeout, open_service=None):
    db = sessions.journal.conn
    row = db.execute('SELECT * FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
    result = json.loads(row['result']) if row['result'] else None
    if result is None:
        owner = db.execute('SELECT provider FROM sessions WHERE session_key=?',(job['session_key'],)).fetchone()
        parsed = parse(owner['provider'], sessions.snapshot(job['snapshot_hash']))
        if parsed['headless']:
            # A headless run (a helper, a scripted call) reports to the session that started it, which
            # is captured itself; its own transcript is never extracted.
            return 'skipped'
        if job['task']=='final' and parsed['partial_tail']:
            raise KelmoError('E_TRANSCRIPT','Final transcript has an incomplete record; recapture when complete.')
        messages = reduce([item for item in parsed['messages']
                           if job['task']=='final' or item['record']>job['start_record']], settings)
        omitted = 0
        if settings['extraction']=='tail':
            kept, omitted = tail(messages, settings['max_input_bytes'])
            selected = [kept] if kept else []
        else:
            selected = list(chunks(messages, settings['max_input_bytes']))
            if len(selected) > settings['max_calls']:
                # The newest chunks within the call cap; the older pieces are counted, as in tail mode.
                omitted = sum(len(chunk) for chunk in selected[:-settings['max_calls']])
                selected = selected[-settings['max_calls']:]
        saved = json.loads(row['chunks'])
        pages = _registered_pages(sessions.config)
        for index in range(len(saved), len(selected)):
            reply = validate_extraction(quota.checked(execute(argv, {'task':'extract_'+job['task'],
                'messages':selected[index], 'part':index+1,'parts':len(selected),
                'omitted_pieces':omitted, 'pages':pages},timeout=timeout)))
            saved.append(reply)
            with db:
                db.execute('UPDATE session_processing SET chunks=? WHERE job_id=?',(json.dumps(saved),job['id']))
        notes = [part['note_markdown'] for part in saved if not part['skip']]
        candidates = []
        for part in saved:
            if isinstance(part['candidate_facts'],list) and isinstance(candidates,list):
                candidates.extend(part['candidate_facts'])
            else:
                candidates = 'invalid'
        note = notes[0] if len(notes)==1 else '\n\n'.join(f'## Part {i+1}\n\n{value}' for i,value in enumerate(notes))
        if len(note.encode())>1048576:
            raise KelmoError('E_EXTRACTION','Complete session note exceeds the intake ceiling; chunks were retained.')
        # calls: model calls this extraction took (one per input piece), so the cost of a job is read
        # from its row, not from the code.
        result = {'note_markdown':note,'candidate_facts':candidates,'skip':not notes,'omitted_pieces':omitted,
                  'calls':len(saved)}
        with db:
            db.execute('UPDATE session_processing SET result=? WHERE job_id=?',(json.dumps(result),job['id']))
    if result['skip']:
        return 'skipped'
    with _note_service(sessions, job, open_service) as service:
        operation = service.capture(result['note_markdown'], 'session-'+job['id']+'.md', role='lead',
                                    idempotency_key='session-note:'+job['id'])
        with db:
            db.execute('UPDATE session_processing SET archive_operation=? WHERE job_id=?',(operation['id'],job['id']))
        if operation['state']=='failed':
            service.retry(operation['id'], reset_budget=False)
        archived = service.run(operation['id'])
        if archived['state']=='failed' and archived['error_code']=='E_BUSY':
            raise KelmoError('E_BUSY','The archive is busy; the session note waits for its turn.')
        if archived['state']!='archived':
            raise KelmoError('E_ARCHIVE','Complete session note has not been archived.')
        with db:
            db.execute("UPDATE session_jobs SET state='archived' WHERE id=?",(job['id'],))
        generation = db.execute('SELECT generation FROM sessions WHERE session_key=?',(job['session_key'],)).fetchone()[0]
        later = db.execute('''SELECT 1 FROM session_jobs WHERE session_key=? AND generation=?
            AND rowid>(SELECT rowid FROM session_jobs WHERE id=?)
            AND state IN ('wiki_applied','wiki_unchanged')''',(job['session_key'],job['generation'],job['id'])).fetchone()
        if settings['mode']=='archive-only' or generation>job['generation'] or later:
            return 'wiki_unchanged'
        row = db.execute('SELECT proposal FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
        value = json.loads(row['proposal']) if row['proposal'] else None
        if value is None:
            value = proposal(service,result['note_markdown'],result['candidate_facts'],argv,execute,timeout)
            with db:
                db.execute('UPDATE session_processing SET proposal=? WHERE job_id=?',(json.dumps(value),job['id']))
        if not value['changes']:
            return 'wiki_unchanged'
        applied = service.propose_archived_note(operation['id'],value['changes'],value['checks'],'session:'+job['id'])
        return 'wiki_applied' if applied['applied'] else 'wiki_unchanged'


def tick(root, argv=None, *, execute=None, timeout=120, max_attempts=3,
         override_configured_executor=False, open_service=None):
    """One session job. `open_service(client_id)` is given under a server profile (shared_service): each
    note is archived for the client that captured its session, never under a local identity."""
    from .daemon import execute as default_execute, validate_config, validate_timeout
    validate_timeout(timeout)
    if type(max_attempts) is not int or not 1<=max_attempts<=10:
        raise KelmoError('E_CONFIG','Invalid session retry ceiling.')
    execute = execute or default_execute
    config = Config.load(root)
    settings = config.sessions
    if settings['mode']=='off' or settings['paused']:
        return {'jobs':[],'paused':True}
    if settings.get('executor_config') and not override_configured_executor:
        from .install.setup import read_json
        profile = validate_config(read_json(Path(settings['executor_config']),required=True))
        argv,timeout,max_attempts = profile['argv'],profile['timeout_seconds'],profile['max_attempts']
    if not argv:
        return {'jobs':[],'executor_ready':False}
    with processing_lock(config):
        sessions = Sessions(config)
        try:
            db = sessions.journal.conn
            job = db.execute('''SELECT j.* FROM session_jobs j LEFT JOIN session_processing p ON p.job_id=j.id
                WHERE j.state IN ('pending','running','archived') AND COALESCE(p.eligible,0)<=?
                ORDER BY j.rowid LIMIT 1''',(time.time(),)).fetchone()
            if job is None:
                return {'jobs':[]}
            job = dict(job)
            with db:
                db.execute('INSERT OR IGNORE INTO session_processing(job_id) VALUES (?)',(job['id'],))
                progress=db.execute('SELECT * FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
                if progress['attempts']>=max_attempts:
                    archived=db.execute('SELECT archive_id FROM operations WHERE id=?',(progress['archive_operation'],)).fetchone()
                    state='wiki_needs_review' if archived and archived[0] else 'needs_review'
                    db.execute('UPDATE session_jobs SET state=? WHERE id=?',(state,job['id']))
                    db.execute("UPDATE session_processing SET error_code='E_ATTEMPTS' WHERE job_id=?",(job['id'],))
                    return {'jobs':[dict(sessions.job(job['id']),error_code='E_ATTEMPTS')]}
                db.execute('UPDATE session_processing SET attempts=attempts+1 WHERE job_id=?',(job['id'],))
                db.execute("UPDATE session_jobs SET state='running',attempts=attempts+1 WHERE id=?",(job['id'],))
            try:
                state = _run(sessions,job,settings,argv,execute,timeout,open_service)
                with db:
                    db.execute('UPDATE session_jobs SET state=? WHERE id=?',(state,job['id']))
                    db.execute('UPDATE session_processing SET error_code=NULL WHERE job_id=?',(job['id'],))
            except (KelmoError,OSError,ValueError,engine.Conflict,sqlite3.Error) as error:
                code = (error.code if isinstance(error,KelmoError) else 'E_BUSY' if sqlite_busy(error)
                        else 'E_SESSION')
                waiting = isinstance(error,quota.QuotaWait)
                # A held lock or another attempt's claim means another writer is at work; the job queues
                # behind it like a quota wait.
                busy = code in ('E_BUSY','E_CLAIM')
                reset_at = error.reset_at if waiting else None

                def settle():
                    # The refund must land even while the journal is busy, or a wait would cost an attempt.
                    progress = db.execute('SELECT * FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
                    archived = sessions.job(job['id'])['state']=='archived'
                    exhausted = not (waiting or busy) and (progress['attempts']>=max_attempts
                                                           or code in ('E_PROPOSAL','E_TRANSCRIPT'))
                    state = ('wiki_needs_review' if archived else 'needs_review') if exhausted else 'pending'
                    eligible = (reset_at if waiting else time.time()+BUSY_RETRY_SECONDS if busy
                                else time.time()+2**progress['attempts'])
                    with db:
                        db.execute('UPDATE session_jobs SET state=? WHERE id=?',(state,job['id']))
                        db.execute('UPDATE session_processing SET error_code=?,eligible=? WHERE job_id=?',
                                   (code,eligible,job['id']))
                        if waiting or busy:
                            db.execute('UPDATE session_processing SET attempts=MAX(0,attempts-1) WHERE job_id=?',(job['id'],))
                            db.execute('UPDATE session_jobs SET attempts=MAX(0,attempts-1) WHERE id=?',(job['id'],))
                        if code in ('E_FACT_CHECK','E_PROPOSAL'):
                            db.execute('UPDATE session_processing SET proposal=NULL WHERE job_id=?',(job['id'],))
                when_free(settle)
            output = sessions.job(job['id'])
            progress=db.execute('SELECT error_code,archive_operation FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
            output.update(dict(progress))
            if progress['error_code']=='E_QUOTA':
                output['quota_reset_at']=db.execute('SELECT eligible FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()[0]
            receipt=db.execute('SELECT archive_id FROM operations WHERE id=?',(progress['archive_operation'],)).fetchone()
            output['archive_id']=receipt[0] if receipt else None
            return {'jobs':[output]}
        finally:
            sessions.close()
