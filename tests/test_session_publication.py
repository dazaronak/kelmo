"""Automatic session facts go through the editor's prepared-intent path."""
import json
import sqlite3

import pytest

from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.initialization import initialize
from kelmo.service import Service
from kelmo.session_worker import tick
from kelmo.sessions import Sessions
from kelmo.wiki import engine
from test_session_capture import lines

TARGET = 'projects/demo/demo.md'
TEXT = 'The demo session chose SQLite for storage.'
GIT_ENV = {'GIT_AUTHOR_NAME': 'Synthetic Fixture', 'GIT_COMMITTER_NAME': 'Synthetic Fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def unavailable(*args, **kwargs):
    from kelmo.store.errors import ArchiveEmbeddingError
    raise ArchiveEmbeddingError('Synthetic offline embedding provider')


def adopted_session(tmp_path, monkeypatch, mode='auto'):
    """A vault initialized the way the CLI does it (registered project `demo`), one captured session."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('kelmo.store.memory.store.embed', unavailable)
    monkeypatch.setattr('kelmo.store.memory.search.embed', unavailable)
    root = tmp_path / 'vault'
    initialize(root, 'fixture', project='demo', git_name='Synthetic Fixture', git_email='fixture@example.invalid')
    source_root = tmp_path / 'native'
    source_root.mkdir()
    source = source_root / 'chat.jsonl'
    source.write_bytes(lines('claude', 3))
    with (root / 'kelmo.toml').open('a') as file:
        file.write('\n[sessions]\nmode = ' + json.dumps(mode) + '\n')
    sessions = Sessions(Config.load(root))
    captured = sessions.capture('claude', 'chat', source, source_root, 'SessionEnd')
    sessions.close()
    return root, captured['job_id']


def executor(calls):
    def execute(argv, payload, **kwargs):
        calls.append(payload['task'])
        if payload['task'].startswith('extract_'):
            return {'note_markdown': 'Session note. The user asked how the archive is stored; SQLite was kept.',
                    'candidate_facts': [{'text': TEXT, 'target': TARGET}], 'skip': False}
        return {'decisions': [{'index': 0, 'action': 'new', 'fact_key': 'demo.session.0'}]}
    return execute


def commits(root):
    return engine.git(root, 'log', '--format=%s').splitlines()


def note_operation(root, job_id):
    db = sqlite3.connect(root / '.state' / 'journal.sqlite3')
    db.row_factory = sqlite3.Row
    try:
        return db.execute('SELECT archive_operation FROM session_processing WHERE job_id=?', (job_id,)).fetchone()[0]
    finally:
        db.close()


def test_auto_session_publishes_through_prepared_editor_intent(tmp_path, monkeypatch):
    root, job_id = adopted_session(tmp_path, monkeypatch)
    before = commits(root)
    calls = []
    job = tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]
    assert job['state'] == 'wiki_applied'
    assert calls == ['extract_final', 'reconcile_session']
    registry = engine.load_registry(root)
    fact = registry['facts']['demo.session.0']
    assert fact['target'] == TARGET and fact['text'] == TEXT and fact['status'] == 'active'
    with Service(root) as service:
        operation = service.journal.get(note_operation(root, job_id))
        events = service.trace(operation['id'])['events']
    assert operation['durable_stage'] == 'archived' and operation['archive_id']
    assert fact['ark_id'] == operation['archive_id']
    dispositions = [event['details']['disposition'] for event in events if event['stage'] == 'editor_result']
    assert dispositions == ['prepared', 'applied']
    after = commits(root)
    assert len(after) == len(before) + 1
    assert not any('Apply session' in subject for subject in after)
    assert 'session_applications' not in registry or not registry['session_applications']


def test_crash_after_publication_replays_without_a_second_publication(tmp_path, monkeypatch):
    root, job_id = adopted_session(tmp_path, monkeypatch)
    calls = []
    assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]['state'] == 'wiki_applied'
    published = commits(root)
    with Service(root) as service:
        archived_before = service.status()['archive_records']
    db = sqlite3.connect(root / '.state' / 'journal.sqlite3')
    with db:
        db.execute("UPDATE session_jobs SET state='archived' WHERE id=?", (job_id,))
    db.close()
    assert tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]['state'] == 'wiki_applied'
    assert calls == ['extract_final', 'reconcile_session'], 'a replay consults no model'
    assert commits(root) == published
    with Service(root) as service:
        assert service.status()['archive_records'] == archived_before
    assert engine.load_registry(root)['facts']['demo.session.0']['text'] == TEXT


def test_different_proposal_under_the_same_application_is_refused(tmp_path, monkeypatch):
    root, job_id = adopted_session(tmp_path, monkeypatch)
    assert tick(root, ['/synthetic/executor'], execute=executor([]))['jobs'][0]['state'] == 'wiki_applied'
    operation_id = note_operation(root, job_id)
    changes = [{'fact_key': 'demo.session.0', 'target': TARGET, 'text': 'The demo session chose Postgres.',
                'status': 'active', 'expected_revision': None, 'replaces': []}]
    with Service(root) as service:
        with pytest.raises(KelmoError) as refused:
            service.propose_archived_note(operation_id, changes, [], 'session:' + job_id)
    assert refused.value.code == 'E_IDEMPOTENCY'
    assert engine.load_registry(root)['facts']['demo.session.0']['text'] == TEXT


def test_archive_only_never_reconciles_or_edits(tmp_path, monkeypatch):
    root, job_id = adopted_session(tmp_path, monkeypatch, mode='archive-only')
    before = commits(root)
    calls = []
    job = tick(root, ['/synthetic/executor'], execute=executor(calls))['jobs'][0]
    assert job['state'] == 'wiki_unchanged'
    assert calls == ['extract_final']
    assert 'demo.session.0' not in engine.load_registry(root)['facts']
    assert commits(root) == before
    with Service(root) as service:
        operation = service.journal.get(note_operation(root, job_id))
    assert operation['durable_stage'] == 'archived', 'the whole note is still archived'
