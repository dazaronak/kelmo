"""The daytime apply under `[editor] batch_apply`: it waits for a ready project and runs as the ready pass.

Without the flag the early apply starts once DREAM_WAITING_NOTES notes wait anywhere and takes them all. With it,
a project is ready once `batch_ready_sources` of its notes wait for their first pass, the scheduled apply starts
only then, and it carries `ready` so the pass publishes the ready projects and leaves the small ones for the
night. The journal here is a real in-memory SQLite database with the columns the scheduler and the readiness
count read."""
import json
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from kelmo.core.jobs import DREAM_WAITING_NOTES, Jobs
from kelmo.errors import KelmoError


class Journal:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE operations(id TEXT PRIMARY KEY, intent TEXT, terminal INTEGER, state TEXT,
                                    created_at REAL);
            CREATE TABLE operation_sources(operation_id TEXT, metadata TEXT);''')
        self.clock = 0

    def note(self, project, identifier=None, state='archived'):
        self.clock += 1
        identifier = identifier or f'{project}-{self.clock}'
        self.conn.execute('INSERT INTO operations VALUES (?,?,?,?,?)', (identifier, 'create', 0, state, self.clock))
        self.conn.execute('INSERT INTO operation_sources VALUES (?,?)',
                          (identifier, json.dumps({'editor_request': {'project': project, 'selected': []}})))
        return identifier

    def finish(self, identifier):
        self.conn.execute('UPDATE operations SET terminal=1 WHERE id=?', (identifier,))


def scheduler(tmp_path, journal, *, batch_apply, ready_at=3, now=None):
    (tmp_path / '.state').mkdir(exist_ok=True)
    reader = SimpleNamespace(journal=journal)
    config = SimpleNamespace(embedding=None, batch_apply=batch_apply, batch_ready_sources=ready_at)
    storage = SimpleNamespace(writer=SimpleNamespace(config=config), read=lambda fn: fn(reader))
    core = SimpleNamespace(root=tmp_path, storage=storage, profile=None, profile_path=None)
    jobs = Jobs(core, clock=lambda: (now or [100])[0])
    jobs._write_state(dream_day=jobs._today())  # the daily run is done: only the early apply can start a job
    return jobs


@pytest.fixture(autouse=True)
def awake(monkeypatch):
    from kelmo import dreaming_cli
    monkeypatch.setattr(dreaming_cli, 'status', lambda root: {'paused': False})


def scheduled(jobs, monkeypatch):
    """The options of the jobs one scheduler tick starts."""
    started = []
    monkeypatch.setattr(jobs, '_background', lambda kind, options: started.append((kind, options)) or True)
    jobs._tick()
    return started


def test_with_batch_apply_many_notes_of_small_projects_start_no_apply(tmp_path, monkeypatch):
    journal = Journal()
    for index in range(DREAM_WAITING_NOTES):
        journal.note(f'p{index % 10}')  # two notes in each of ten projects, ready at three
    jobs = scheduler(tmp_path, journal, batch_apply=True)
    assert jobs._dream_due() is None
    assert scheduled(jobs, monkeypatch) == []


def test_with_batch_apply_a_ready_project_starts_the_ready_pass(tmp_path, monkeypatch):
    journal = Journal()
    journal.note('small')
    for _ in range(3):
        journal.note('big')
    jobs = scheduler(tmp_path, journal, batch_apply=True)
    assert jobs._dream_due() == 'apply'
    assert scheduled(jobs, monkeypatch) == [('dream', {'action': 'apply', 'scheduled': True, 'ready': True})]


def test_without_batch_apply_the_note_count_starts_a_full_apply(tmp_path, monkeypatch):
    journal = Journal()
    for index in range(DREAM_WAITING_NOTES):
        journal.note(f'p{index % 10}')
    jobs = scheduler(tmp_path, journal, batch_apply=False)
    assert jobs._dream_due() == 'apply'
    assert scheduled(jobs, monkeypatch) == [('dream', {'action': 'apply', 'scheduled': True})]


def test_a_parked_note_counts_towards_no_readiness(tmp_path):
    journal = Journal()
    journal.note('big', state='parked')
    journal.note('big')
    journal.note('big')
    jobs = scheduler(tmp_path, journal, batch_apply=True)
    assert jobs._ready_projects() == {}
    journal.note('big')
    assert list(jobs._ready_projects()) == ['big']


def test_a_redirect_that_joins_two_captured_names_makes_their_project_ready(tmp_path, monkeypatch):
    from kelmo import creation
    from kelmo.wiki import engine
    journal = Journal()
    journal.note('old')
    journal.note('old')
    journal.note('new')
    registry = object()
    monkeypatch.setattr(engine, 'load_registry_view', lambda root: registry)
    def redirected(given, request):
        assert given is registry
        return dict(request, project='new') if request.get('project') == 'old' else request

    monkeypatch.setattr(creation, 'current_request', redirected)
    jobs = scheduler(tmp_path, journal, batch_apply=True)
    assert {project: len(ids) for project, ids in jobs._ready_projects().items()} == {'new': 3}


def test_an_unreadable_note_reads_as_no_ready_project_and_the_tick_goes_on(tmp_path, monkeypatch):
    journal = Journal()
    for _ in range(3):
        journal.note('big')
    journal.conn.execute("UPDATE operation_sources SET metadata='not json'")
    jobs = scheduler(tmp_path, journal, batch_apply=True)
    assert jobs._ready_projects() == {}
    assert jobs._dream_due() is None
    assert scheduled(jobs, monkeypatch) == []


def test_too_few_notes_never_read_the_registry(tmp_path, monkeypatch):
    from kelmo.wiki import engine
    journal = Journal()
    journal.note('a')
    journal.note('b')
    monkeypatch.setattr(engine, 'load_registry_view', lambda root: pytest.fail('the registry was read'))
    assert scheduler(tmp_path, journal, batch_apply=True)._ready_projects() == {}


def test_a_ready_pass_is_given_only_the_notes_of_its_ready_projects(tmp_path):
    from kelmo.core.jobs import APPLY_RESUME_SECONDS
    journal = Journal()
    small = journal.note('small')
    big = [journal.note('big') for _ in range(3)]
    now = [100]
    jobs = scheduler(tmp_path, journal, batch_apply=True, now=now)
    jobs._record_apply_start(ready=True)
    state = jobs._read_state()['apply']
    assert state['ids'] == sorted(big) and state['ready'] is True and small not in state['ids']
    for identifier in big:
        journal.finish(identifier)
    now[0] = 130
    jobs._record_apply_end(stopped=True)
    now[0] = 130 + APPLY_RESUME_SECONDS
    # Only the small project's note still waits, and the ready pass was never given it: no resume.
    assert jobs._dream_due() is None


def test_a_cut_ready_pass_whose_project_is_still_ready_comes_back_after_the_resume_wait(tmp_path, monkeypatch):
    from kelmo.core.jobs import APPLY_RESUME_SECONDS
    journal = Journal()
    big = [journal.note('big') for _ in range(7)]
    now = [100]
    jobs = scheduler(tmp_path, journal, batch_apply=True, now=now)
    jobs._record_apply_start(ready=True)
    for identifier in big[:3]:
        journal.finish(identifier)
    now[0] = 130
    jobs._record_apply_end(stopped=True)
    assert jobs._dream_due() is None, 'the cut pass waits its resume interval first'
    now[0] = 130 + APPLY_RESUME_SECONDS
    # Four notes still wait at ready 3: the uncut pass would have taken another batch, and so does this one.
    assert jobs._dream_due() == 'apply'
    assert scheduled(jobs, monkeypatch) == [('dream', {'action': 'apply', 'scheduled': True, 'ready': True})]


def test_a_cut_ready_pass_starts_no_pass_for_what_it_left_below_the_number(tmp_path, monkeypatch):
    from kelmo.core.jobs import APPLY_MIN_INTERVAL_SECONDS
    journal = Journal()
    big = [journal.note('big') for _ in range(4)]
    now = [100]
    jobs = scheduler(tmp_path, journal, batch_apply=True, now=now)
    jobs._record_apply_start(ready=True)
    journal.finish(big[0])
    journal.finish(big[1])
    now[0] = 130
    jobs._record_apply_end(stopped=True)
    now[0] = 130 + APPLY_MIN_INTERVAL_SECONDS
    # Two notes wait below the number of 3. A ready pass would take neither, so none starts: an uncut pass
    # would have left them for the night too.
    assert jobs._dream_due() is None
    assert scheduled(jobs, monkeypatch) == []


@pytest.mark.parametrize('options,code', [({'action': 'run', 'ready': True}, 'E_INPUT'),
                                          ({'action': 'apply', 'ready': 'yes'}, 'E_PROTOCOL')])
def test_the_dream_job_refuses_a_ready_it_cannot_honour(tmp_path, monkeypatch, options, code):
    from kelmo import dreaming_cli
    jobs = scheduler(tmp_path, Journal(), batch_apply=True)
    monkeypatch.setattr(jobs, 'step', nullcontext)
    monkeypatch.setattr(dreaming_cli, 'run_dream', lambda *args, **kwargs: pytest.fail('the run started'))
    with pytest.raises(KelmoError) as raised:
        jobs._dream(options)
    assert raised.value.code == code


@pytest.mark.parametrize('ready', [True, False])
def test_the_dream_job_hands_ready_to_the_run(tmp_path, monkeypatch, ready):
    from kelmo import dreaming_cli
    jobs = scheduler(tmp_path, Journal(), batch_apply=True)
    monkeypatch.setattr(jobs, 'step', nullcontext)
    seen = {}

    def run(service, action, **kwargs):
        seen.update(kwargs, action=action)
        return {'state': 'finished', 'apply': {}}

    monkeypatch.setattr(dreaming_cli, 'run_dream', run)
    options = {'action': 'apply', 'ready': True} if ready else {'action': 'apply'}
    assert jobs._dream(options) == {'state': 'finished', 'apply': {}}
    assert seen['action'] == 'apply' and seen['ready'] is ready


@pytest.mark.parametrize('flags,expected', [([], {'action': 'apply'}),
                                            (['--ready'], {'action': 'apply', 'ready': True})])
def test_dream_apply_through_the_daemon_carries_ready(tmp_path, monkeypatch, flags, expected):
    from kelmo import cli
    from kelmo.core import daemon_client
    from kelmo.interface import build_parser
    parser, _commands = build_parser('0')
    args = parser.parse_args(['dream', 'apply', '--vault', str(tmp_path), *flags])
    calls = []
    monkeypatch.setattr(daemon_client, 'ensure', lambda root, **kwargs: tmp_path / 'kelmod.sock')
    monkeypatch.setattr(daemon_client, 'call', lambda root, op, body, **kwargs: calls.append((op, body))
                        or {'state': 'finished'})
    assert cli.through_daemon(args, [], str(tmp_path / 'kelmo')) == {'state': 'finished'}
    assert calls == [('state.job_run', {'job': 'dream', 'options': expected})]


def test_the_dream_job_answers_busy_while_a_run_outside_the_daemon_holds_the_dreaming_lock(tmp_path, monkeypatch):
    """A `--server-profile` Dreaming call runs in its own process: the daemon's job waits its turn behind it."""
    import fcntl

    from kelmo import dreaming_cli
    jobs = scheduler(tmp_path, Journal(), batch_apply=True)
    monkeypatch.setattr(jobs, 'step', nullcontext)
    monkeypatch.setattr(dreaming_cli, 'run_dream', lambda *args, **kwargs: pytest.fail('the run started'))
    with (tmp_path / '.state' / 'dreaming.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert jobs._dream({'action': 'apply', 'scheduled': True}) == {'state': 'busy'}
        assert 'apply' not in jobs._read_state(), 'a pass that never started records no start'
    monkeypatch.setattr(dreaming_cli, 'run_dream', lambda *args, **kwargs: {'state': 'finished', 'apply': {}})
    assert jobs._dream({'action': 'apply'}) == {'state': 'finished', 'apply': {}}, 'the lock is free again'


def test_a_dream_call_the_service_outlasts_is_answered_as_still_running(tmp_path, monkeypatch):
    """The pass goes on in the service after the call's wait ends: the answer says so instead of E_DEADLINE."""
    from kelmo import cli
    from kelmo.core import daemon_client
    from kelmo.interface import build_parser
    parser, _commands = build_parser('0')
    args = parser.parse_args(['dream', 'apply', '--vault', str(tmp_path)])
    waits = []

    def outlasted(root, op, body, **kwargs):
        waits.append(kwargs['seconds'])
        raise KelmoError('E_DEADLINE', 'The core did not answer before the deadline.')

    monkeypatch.setattr(daemon_client, 'ensure', lambda root, **kwargs: tmp_path / 'kelmod.sock')
    monkeypatch.setattr(daemon_client, 'service_owned', lambda root: True)
    monkeypatch.setattr(daemon_client, 'call', outlasted)
    result = cli.through_daemon(args, [], str(tmp_path / 'kelmo'))
    assert result['state'] == 'running' and 'kelmo daemon status' in result['reason'], result
    assert waits == [cli.WAIT_CALL_SECONDS]


def test_a_dream_call_to_a_daemon_started_for_it_waits_for_the_pass(tmp_path, monkeypatch):
    """That daemon ends when its caller leaves and stops the pass with it, so the call never answers `running`."""
    from kelmo import cli
    from kelmo.core import daemon_client
    from kelmo.interface import build_parser
    parser, _commands = build_parser('0')
    args = parser.parse_args(['dream', 'apply', '--vault', str(tmp_path)])
    waits = []

    def outlasted(root, op, body, **kwargs):
        waits.append(kwargs['seconds'])
        raise KelmoError('E_DEADLINE', 'The core did not answer before the deadline.')

    monkeypatch.setattr(daemon_client, 'ensure', lambda root, **kwargs: tmp_path / 'kelmod.sock')
    monkeypatch.setattr(daemon_client, 'call', outlasted)
    assert not daemon_client.service_owned(tmp_path), 'no kelmod service is installed for this root'
    with pytest.raises(KelmoError) as refused:
        cli.through_daemon(args, [], str(tmp_path / 'kelmo'))
    assert refused.value.code == 'E_DEADLINE'
    assert waits == [daemon_client.LONGEST_CALL_SECONDS]
    assert daemon_client.LONGEST_CALL_SECONDS * 1000 <= 86_400_000, 'the wire refuses a deadline past one day'
