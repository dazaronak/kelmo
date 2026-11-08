"""Archive-first session processing, including independent wiki failure."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from kelmo.service import Service
from kelmo.config import Config
from kelmo.sessions import Sessions
from kelmo.errors import KelmoError
from test_session_capture import lines


@pytest.mark.parametrize('native_id', ['chat', 'mac-b:native-1', 'agent:chat'])
def test_unbound_session_names_never_grant_a_shared_origin(tmp_path, native_id):
    from kelmo.session_worker import _note_service
    root, job_id = prepared(tmp_path, native_id=native_id)
    sessions = Sessions(Config.load(root))
    opened = []
    try:
        job = sessions.job(job_id)
        with pytest.raises(KelmoError) as error:
            _note_service(sessions, job, opened.append)
        assert error.value.code == 'E_ORIGIN'
        assert opened == []
        # The same local agent session retains the ordinary local processing path.
        with _note_service(sessions, job, None) as service:
            assert service.origin.client_id == 'local'
    finally:
        sessions.close()


def prepared(tmp_path, mode='auto', count=1, native_id='chat'):
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    source_root = tmp_path / 'native'
    source_root.mkdir()
    source = source_root / 'chat.jsonl'
    source.write_bytes(lines('claude', count))
    with (root / 'kelmo.toml').open('a') as file:
        file.write('\n[sessions]\nmode = '+json.dumps(mode)+'\n')
    sessions = Sessions(Config.load(root))
    captured = sessions.capture('claude', native_id, source, source_root, 'SessionEnd')
    sessions.close()
    return root, captured['job_id']


def executor(calls, facts=None):
    def execute(argv, payload, **kwargs):
        calls.append(payload['task'])
        if payload['task'].startswith('extract_'):
            return {'note_markdown': 'Full note: chose B, only after approval. A was rejected.',
                    'candidate_facts': facts or [], 'skip': False}
        return {'decisions': [{'index': i, 'action': 'new', 'fact_key': 'fixture.'+str(i)}
                              for i, _ in enumerate(payload['candidates'])]}
    return execute


def test_complete_note_archived_when_no_wiki_facts(tmp_path):
    from kelmo.session_worker import tick
    root, job_id = prepared(tmp_path)
    calls = []
    before = (root / 'knowledge/provenance/registry.json').read_bytes()
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        result = tick(root, ['/synthetic/executor'], execute=executor(calls))
        assert result['jobs'][0]['state'] == 'wiki_unchanged'
        with Service(root) as service:
            assert service.status()['archive_records'] == 1
            assert 'A was rejected' in service.list(10)['items'][0]['content']
        assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'] == []
    assert calls == ['extract_final']
    assert (root / 'knowledge/provenance/registry.json').read_bytes() == before


def test_full_note_and_checked_fact_share_one_archive_receipt(tmp_path):
    from kelmo.session_worker import tick
    root, job_id = prepared(tmp_path)
    calls = []
    facts = [{'text': 'Use B only after approval.', 'target':'projects/fixture/fixture.md'}]
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        result = tick(root, ['/synthetic/executor'], execute=executor(calls))
        # Run a second session with candidates so the archive-only path stays independent.
        source = tmp_path / 'native/chat.jsonl'
        source.write_bytes(lines('claude', 2))
        s = Sessions(Config.load(root))
        s.capture('claude', 'chat', source, source.parent, 'SessionEnd')
        s.close()
        result = tick(root, ['/synthetic/executor'], execute=executor(calls, facts))
        assert result['jobs'][0]['state'] == 'wiki_applied'
        registry = json.loads((root / 'knowledge/provenance/registry.json').read_text())
        fact = registry['facts']['fixture.0']
        with Service(root) as service:
            assert service.show(fact['ark_id'])['content'].endswith('A was rejected.')
            assert service.status()['archive_records'] == 2


@pytest.mark.parametrize('candidates', ['broken', [None], [{'text': 'x', 'target': '../out.md'}]])
def test_bad_candidates_do_not_prevent_archive(tmp_path, candidates):
    from kelmo.session_worker import tick
    root, _ = prepared(tmp_path)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        result = tick(root, ['/synthetic/executor'], execute=executor([], candidates))
        assert result['jobs'][0]['state'] == 'wiki_needs_review'
        with Service(root) as service:
            assert service.status()['archive_records'] == 1


def test_archive_only_mode_never_reconciles(tmp_path):
    from kelmo.session_worker import tick
    root, _ = prepared(tmp_path, 'archive-only')
    calls = []
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        result = tick(root, ['/synthetic/executor'], execute=executor(calls, [{'text':'B','target':'projects/fixture/fixture.md'}]))
    assert result['jobs'][0]['state'] == 'wiki_unchanged'
    assert calls == ['extract_final']


def test_model_outage_retries_then_reuses_saved_extraction(tmp_path):
    from kelmo.session_worker import tick, retry
    root, job_id = prepared(tmp_path)
    calls = []
    def broken(*args, **kwargs):
        raise KelmoError('E_EXECUTOR', 'Synthetic outage')
    assert tick(root, ['/synthetic/executor'], execute=broken, max_attempts=1)['jobs'][0]['state'] == 'needs_review'
    retry(root, job_id)
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]['state'] == 'wiki_unchanged'
    assert calls == ['extract_final']


def test_wiki_failure_preserves_archive_and_model_response(tmp_path, monkeypatch):
    from kelmo.session_worker import tick, retry
    root, job_id = prepared(tmp_path)
    calls = []
    real = Service.propose_archived_note
    def broken(*args, **kwargs):
        raise KelmoError('E_GIT', 'Synthetic wiki outage')
    monkeypatch.setattr(Service, 'propose_archived_note', broken)
    facts = [{'text':'Use B only after approval.','target':'projects/fixture/fixture.md'}]
    with patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        assert tick(root, ['/synthetic/executor'], execute=executor(calls, facts), max_attempts=1)['jobs'][0]['state'] == 'wiki_needs_review'
        with Service(root) as service:
            assert service.status()['archive_records'] == 1
        monkeypatch.setattr(Service, 'propose_archived_note', real)
        retry(root, job_id)
        assert tick(root, ['/synthetic/executor'], execute=executor(calls, facts))['jobs'][0]['state'] == 'wiki_applied'
        with Service(root) as service:
            assert service.status()['archive_records'] == 1
    assert calls.count('extract_final') == 1
    assert calls.count('reconcile_session') == 1


def test_off_mode_does_not_run_saved_jobs(tmp_path):
    from kelmo.session_worker import tick
    root, _ = prepared(tmp_path, 'off')
    calls = []
    assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'] == []
    assert calls == []


def test_skip_does_not_create_empty_archive_note(tmp_path):
    from kelmo.session_worker import tick
    root, _ = prepared(tmp_path)
    def skip(*a, **k):
        return {'skip': True, 'note_markdown':'', 'candidate_facts':[]}
    result = tick(root, ['/synthetic/executor'], execute=skip)
    assert result['jobs'][0]['state'] == 'skipped'
    with Service(root) as service:
        assert service.status()['archive_records'] == 0


def test_a_headless_run_is_skipped_without_a_model_call(tmp_path):
    from kelmo.session_worker import tick
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture', project='fixture')
    (tmp_path / 'native').mkdir()
    source = tmp_path / 'native' / 'helper.jsonl'
    rows = [json.loads(line) for line in lines('claude', 2).splitlines()]
    rows[0]['entrypoint'] = 'sdk-cli'  # how Claude Code records a `claude -p` start
    source.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
    with (root / 'kelmo.toml').open('a') as file:
        file.write('\n[sessions]\nmode = "auto"\n')
    sessions = Sessions(Config.load(root))
    try:
        sessions.capture('claude', 'helper', source, tmp_path / 'native', 'SessionEnd')
    finally:
        sessions.close()
    calls = []
    assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]['state'] == 'skipped'
    assert calls == []
    with Service(root) as service:
        assert service.status()['archive_records'] == 0


@pytest.mark.parametrize('point',['capture','wiki'])
def test_crash_after_durable_effect_replays_without_duplicate(tmp_path,monkeypatch,point):
    from kelmo.session_worker import tick
    root,job_id=prepared(tmp_path)
    method='capture' if point=='capture' else 'propose_archived_note'
    real=getattr(Service,method)
    def crash(*a,**k):
        real(*a,**k)
        raise SystemExit(99)
    monkeypatch.setattr(Service,method,crash)
    calls=[]
    facts=[{'text':'Use B only after approval.','target':'projects/fixture/fixture.md'}]
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        with pytest.raises(SystemExit):
            tick(root,['/synthetic/executor'],execute=executor(calls,facts))
        monkeypatch.setattr(Service,method,real)
        assert tick(root,['/synthetic/executor'],execute=executor(calls,facts))['jobs'][0]['state']=='wiki_applied'
        with Service(root) as service:
            assert service.status()['archive_records']==1
        assert tick(root,['/synthetic/executor'],execute=executor(calls,facts))['jobs']==[]
    assert calls.count('extract_final')==1


def test_repeated_crashes_exhaust_execution_budget(tmp_path):
    from kelmo.session_worker import tick
    root,job_id=prepared(tmp_path)
    calls=[]
    def crash(*a,**k):
        calls.append(True)
        raise SystemExit(99)
    for _ in range(2):
        with pytest.raises(SystemExit):
            tick(root,['/synthetic/executor'],execute=crash,max_attempts=2)
    assert tick(root,['/synthetic/executor'],execute=crash,max_attempts=2)['jobs'][0]['state']=='needs_review'
    assert len(calls)==2


def test_long_input_records_every_chunk_without_truncation(tmp_path):
    from kelmo.session_worker import tick
    from kelmo.session_control import configure
    root,job_id=prepared(tmp_path,count=100)
    configure(root,{'max_input_bytes':1024,'extraction':'complete','max_calls':64})
    inputs=[]
    def extract(argv,payload,**kwargs):
        inputs.append(payload)
        return {'skip':False,'note_markdown':'part '+str(payload['part']),'candidate_facts':[]}
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        assert tick(root,['/synthetic/executor'],execute=extract)['jobs'][0]['state']=='wiki_unchanged'
    assert len(inputs)>1
    assert sorted({m['record'] for p in inputs for m in p['messages']})==list(range(1,101))
    assert all(len(json.dumps(p).encode())<=1024 for p in inputs)


def test_complete_extraction_keeps_the_newest_chunks_within_the_call_cap(tmp_path):
    from kelmo.session_worker import tick
    from kelmo.session_control import configure
    root,job_id=prepared(tmp_path,count=100)
    configure(root,{'max_input_bytes':1024,'extraction':'complete','max_calls':3})
    inputs=[]
    def extract(argv,payload,**kwargs):
        inputs.append(payload)
        return {'skip':False,'note_markdown':'part '+str(payload['part']),'candidate_facts':[]}
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        assert tick(root,['/synthetic/executor'],execute=extract)['jobs'][0]['state']=='wiki_unchanged'
    assert len(inputs)==3 and all(p['parts']==3 for p in inputs)
    records=[m['record'] for p in inputs for m in p['messages']]
    assert max(records)==100 and min(records)>1, 'the newest evidence is kept, the oldest left out'
    assert inputs[0]['omitted_pieces']>0


def test_extraction_reads_the_conversation_whole_and_tool_traffic_short():
    from kelmo.session_settings import validate
    from kelmo.session_worker import reduce
    settings=validate({},None)
    call=json.dumps({'name':'Bash','input':{'command':'pytest -q tests','description':'Run tests'}})
    messages=[{'record':1,'role':'user','text':'Fix the export. '*200},
              {'record':2,'role':'tool_call','text':call},
              {'record':3,'role':'tool','text':'x'*50000},
              {'record':4,'role':'tool','text':'ok'},
              {'record':5,'role':'tool','text':'Traceback (most recent call last)\n'+'y'*9000},
              {'record':6,'role':'tool','text':'z'*900,'error':True},
              {'record':7,'role':'assistant','text':'Done. '*300}]
    reduced=reduce(messages,settings)
    assert reduced[0]['text']==messages[0]['text'] and reduced[6]['text']==messages[6]['text']
    assert reduced[1]['text']=='Bash: pytest -q tests'
    assert reduced[2]['text']=='[tool result, 50000 chars omitted]'
    assert reduced[3]['text']=='ok'
    assert reduced[4]['text'].startswith('Traceback') and reduced[4]['text'].endswith('chars cut]')
    assert len(reduced[4]['text'])<settings['tool_error_chars']+40
    assert reduced[5]['text']=='z'*900, 'an error result under its cap stays whole'


def test_tail_extraction_makes_one_call_over_the_newest_evidence(tmp_path):
    """The default: a long session costs one model call per job, over the newest messages that fill one
    chunk; the call and the saved result say how much older evidence was left out."""
    from kelmo.session_worker import tick
    from kelmo.session_control import configure
    root,job_id=prepared(tmp_path,count=100)
    configure(root,{'max_input_bytes':1024})
    inputs=[]
    def extract(argv,payload,**kwargs):
        inputs.append(payload)
        return {'skip':False,'note_markdown':'tail note','candidate_facts':[]}
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        assert tick(root,['/synthetic/executor'],execute=extract)['jobs'][0]['state']=='wiki_unchanged'
    assert len(inputs)==1 and inputs[0]['part']==1 and inputs[0]['parts']==1
    records=[m['record'] for m in inputs[0]['messages']]
    assert records==sorted(records) and records[-1]==100 and 1<len(records)<100
    assert inputs[0]['omitted_pieces']==100-len(records)
    assert len(json.dumps(inputs[0]['messages']).encode())<=1024-256
    db=sqlite3.connect(root/'.state'/'journal.sqlite3')
    saved=json.loads(db.execute('SELECT result FROM session_processing WHERE job_id=?',(job_id,)).fetchone()[0])
    assert saved['omitted_pieces']==100-len(records)
    assert saved['calls']==1


def test_late_checkpoint_cannot_revert_final_decision(tmp_path):
    from kelmo.session_worker import tick,retry
    root,first=prepared(tmp_path)
    source=tmp_path/'native/chat.jsonl'
    sessions=Sessions(Config.load(root))
    with sessions.journal.conn:
        sessions.journal.conn.execute("UPDATE session_jobs SET state='needs_review' WHERE id=?",(first,))
    source.write_bytes(lines('claude',2))
    second=sessions.capture('claude','chat',source,source.parent,'SessionEnd')['job_id']
    sessions.close()
    facts=[{'text':'Use B only after approval.','target':'projects/fixture/fixture.md'}]
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        assert tick(root,['/synthetic/executor'],execute=executor([],facts))['jobs'][0]['id']==second
        before=(root/'knowledge/provenance/registry.json').read_bytes()
        retry(root,first)
        assert tick(root,['/synthetic/executor'],execute=executor([],facts))['jobs'][0]['state']=='wiki_unchanged'
        assert (root/'knowledge/provenance/registry.json').read_bytes()==before


def test_update_reuses_retrieved_fact_identity(tmp_path):
    from kelmo.session_worker import tick
    root,_=prepared(tmp_path)
    facts=[{'text':'Use A.','target':'projects/fixture/fixture.md'}]
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        tick(root,['/synthetic/executor'],execute=executor([],facts))
        source=tmp_path/'native/chat.jsonl'
        source.write_bytes(lines('claude',2))
        sessions=Sessions(Config.load(root))
        sessions.capture('claude','chat',source,source.parent,'SessionEnd')
        sessions.close()
        def update(argv,payload,**kwargs):
            if payload['task'].startswith('extract_'):
                return {'skip':False,'note_markdown':'Replaced A with B, only after approval.',
                        'candidate_facts':[{'text':'Use B only after approval.','target':'projects/fixture/fixture.md'}]}
            assert payload['candidates'][0]['search']['candidates'][0]['fact_key']=='fixture.0'
            return {'decisions':[{'index':0,'action':'update','fact_key':'fixture.0'}]}
        assert tick(root,['/synthetic/executor'],execute=update)['jobs'][0]['state']=='wiki_applied'
    registry=json.loads((root/'knowledge/provenance/registry.json').read_text())
    assert list(registry['facts'])==['fixture.0']
    assert registry['facts']['fixture.0']['text']=='Use B only after approval.'


def test_archive_failure_does_not_repeat_extraction_or_attempt_twice_per_tick(tmp_path):
    from kelmo.session_worker import tick,retry
    root,job_id=prepared(tmp_path)
    calls=[]
    embedded=[]
    def embed(*a,**k):
        embedded.append(True)
        if len(embedded)==1:
            raise RuntimeError('Synthetic model unavailable')
        return bytes(1536)
    with patch('kelmo.store.memory.store.embed',side_effect=embed):
        first=tick(root,['/synthetic/executor'],execute=executor(calls))
        assert first['jobs'][0]['state']=='pending'
        assert len(embedded)==1
        retry(root,job_id)
        assert tick(root,['/synthetic/executor'],execute=executor(calls))['jobs'][0]['state']=='wiki_unchanged'
    assert len(embedded)==2
    assert calls==['extract_final']


@pytest.mark.parametrize('applications',[[],{'bad':{}},{'0'*64:{'note':'missing','proposal':'bad'}}])
def test_invalid_application_receipts_fail_registry_validation(applications):
    from kelmo.wiki.engine import validate_registry
    with pytest.raises(ValueError):
        validate_registry({'version':1,'facts':{},'notes':{},'session_applications':applications})


@pytest.mark.parametrize('decision',[
    [],[{'index':True,'action':'skip'}],[{'index':0,'action':'update','fact_key':'missing'}],
    [{'index':0,'action':'new','fact_key':'bad space'}],[{'index':0,'action':'unresolved'}],
])
def test_invalid_or_uncertain_decisions_keep_note_without_wiki(tmp_path,decision):
    from kelmo.session_worker import tick
    root,_=prepared(tmp_path)
    normal=executor([],[{'text':'Use B.','target':'projects/fixture/fixture.md'}])
    def execute(argv,payload,**kwargs):
        return normal(argv,payload,**kwargs) if payload['task'].startswith('extract_') else {'decisions':decision}
    with patch('kelmo.store.memory.store.embed',return_value=bytes(1536)):
        assert tick(root,['/synthetic/executor'],execute=execute,max_attempts=1)['jobs'][0]['state']=='wiki_needs_review'
        with Service(root) as service:
            assert service.status()['archive_records']==1
    assert json.loads((root/'knowledge/provenance/registry.json').read_text())['facts']=={}


@pytest.mark.parametrize('kind',['directory','fifo'])
def test_nonregular_worker_lock_is_rejected(tmp_path,kind):
    import os
    from kelmo.session_worker import tick
    root,_=prepared(tmp_path)
    path=root/'.state/session-worker.lock'
    if kind=='directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(KelmoError):
        tick(root,['/synthetic/executor'],execute=executor([]))
