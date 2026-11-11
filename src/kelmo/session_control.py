"""Session configuration, native intake and read-only queue inspection."""
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import tomllib

from .config import Config
from .errors import KelmoError
from .session_settings import validate


def configure(root, changes):
    from .install.setup import write
    config=Config.load(root)
    path=config.root/'kelmo.toml'
    prior=path.read_bytes()
    data=tomllib.loads(prior.decode())
    settings=validate({**config.sessions,**changes},config.root)
    if data.get('sessions')==settings:
        return settings
    text=prior.decode()
    sections=list(re.finditer(r'(?m)^\s*\[[^\n]+\][ \t]*(?:#.*)?$',text))
    ranges=[]
    for index,match in enumerate(sections):
        header=match.group().strip().split('#')[0].strip()
        if header in ('[sessions]','[sessions.sources]'):
            ranges.append((match.start(),sections[index+1].start() if index+1<len(sections) else len(text)))
    for start,end in reversed(ranges):
        text=text[:start]+text[end:]
    block='\n[sessions]\n'
    for key,value in settings.items():
        if key!='sources':
            block+=key+' = '+json.dumps(value)+'\n'
    block+='\n[sessions.sources]\n'
    for key,value in settings['sources'].items():
        block+=key+' = '+json.dumps(value)+'\n'
    text+=block
    expected=copy.deepcopy(data)
    expected['sessions']=settings
    try:
        if tomllib.loads(text)!=expected:
            raise ValueError()
    except ValueError as error:
        raise KelmoError('E_CONFIG','Unsupported sessions TOML shape; retain unrelated values and use [sessions].') from error
    write(path,text.encode(),expected_prior=prior)
    return settings


def inspect(root):
    config=Config.load(root)
    result={'mode':config.sessions['mode'],'paused':config.sessions['paused'],
            'jobs':[],'native_events':[], 'native_hooks_verified':False}
    try:
        path=config.internal('.state/journal.sqlite3')
    except KelmoError:
        return dict(result,error_code='E_STATE')
    if not path.is_file():
        return dict(result,error_code='E_STATE')
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'session_jobs' in tables:
                result['jobs']=[dict(r) for r in db.execute('''SELECT j.id,j.session_key,j.generation,j.task,
                    j.start_record,j.end_record,j.state,j.created_at FROM session_jobs j ORDER BY j.rowid''')]
                if 'session_processing' in tables:
                    for job in result['jobs']:
                        progress=db.execute('SELECT attempts,error_code,archive_operation,result,proposal FROM session_processing WHERE job_id=?',(job['id'],)).fetchone()
                        if progress:
                            job.update({k:progress[k] for k in ('attempts','error_code','archive_operation')})
                            # Model calls the job has made so far: extraction pieces plus the reconciliation.
                            job['calls']=sum((json.loads(progress[k]) or {}).get('calls',0) if progress[k] else 0
                                             for k in ('result','proposal'))
            if 'session_native_events' in tables:
                result['native_events']=[dict(r) for r in db.execute('SELECT provider,event,received_at FROM session_native_events')]
        finally:
            db.close()
    except sqlite3.Error:
        return dict(result,error_code='E_STATE')
    expected={(p,e) for p in config.sessions['sources'] for e in ('Stop','PreCompact','SessionEnd')}
    observed={(r['provider'],r['event']) for r in result['native_events']}
    result['native_hooks_verified']=bool(expected) and expected<=observed
    return result


def capture_hook(root,provider,action,payload,*,internal_job=None,journal=None):
    """`internal_job` is what the hook's own process saw (None: this process's environment); `journal` lends the
    state core's writer journal."""
    if internal_job is None:
        internal_job=os.environ.get('KELMO_INTERNAL_JOB')=='1'
    if internal_job:
        return {}
    config=Config.load(root)
    if config.sessions['mode']=='off' or config.sessions['paused']:
        return {}
    if not isinstance(payload,dict) or not isinstance(payload.get('transcript_path'),str):
        raise KelmoError('E_TRANSCRIPT','Native event lacks a transcript path; session capture was not accepted.')
    from .session_transcripts import enrolled_transcript
    roots=config.sessions['sources'].get(provider,[])
    source=enrolled_transcript(payload['transcript_path'],roots,'Native transcript is outside enrolled provider locations.')
    selected=next(real for real in (Path(r).expanduser().resolve() for r in roots) if source.is_relative_to(real))
    from .sessions import Sessions
    from .journal import now
    sessions=Sessions(config,journal=journal)
    event={'session-stop':'Stop','session-checkpoint':'PreCompact','session-end':'SessionEnd'}[action]
    try:
        sessions.capture(provider,payload.get('session_id') or payload.get('thread_id'),source,selected,event,
                         checkpoint_turns=config.sessions['checkpoint_turns'])
        with sessions.journal.conn as db:
            db.execute('''CREATE TABLE IF NOT EXISTS session_native_events (
                provider TEXT NOT NULL,event TEXT NOT NULL,received_at TEXT NOT NULL,PRIMARY KEY(provider,event))''')
            db.execute('INSERT OR REPLACE INTO session_native_events VALUES (?,?,?)',(provider,event,now()))
    finally:
        sessions.close()
    return {}
