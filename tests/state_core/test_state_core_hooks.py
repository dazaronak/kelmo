"""`state.hook` on the state core: a session start answers through kelmod, an enrolled client is refused, an
internal or sessions-off capture records nothing, and the spool is ingested oldest first."""
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from daemon_harness import KELMOD, Daemon, error_of, wait_for
from state_core_support import profile_path
from test_action_state import world_factory as world_factory  # noqa: F401
from test_session_capture import lines
from test_shared_context import shared as shared  # noqa: F401
from test_state_core_kelmod import state_spec

from kelmo.core.context import Context
from kelmo.session_control import configure

SESSION_TABLES = ('sessions', 'session_snapshots', 'session_jobs', 'session_native_events')


def serve(root, tmp_path, *state_argv):
    """kelmod serving the real state core with `state.hook` added; the caller stops the daemon."""
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    # A Unix socket path must stay short, so the runtime directory goes under /tmp.
    runtime = Path(tempfile.mkdtemp(prefix='kh', dir='/tmp'))
    spec = state_spec(root, runtime, scratch)
    spec['cores'][0]['argv'] += list(state_argv)
    spec['operations'].append({'name': 'state.hook', 'lanes': ['handler'], 'replay': 'never'})
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (tmp_path / 'kelmod.log').open('w')
    proc = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, scratch, log)
    try:
        wait_for(daemon.all_ready, timeout=60, what='both cores ready')
    except BaseException:
        daemon.stop()
        raise
    return daemon


def session_rows(core):
    """Row counts of the session tables that exist; a vault whose sessions never ran has none of them."""
    def count(reader):
        db = reader.journal.conn
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in SESSION_TABLES if name in present}
    return core.storage.read(count)


def capture_frame(order):
    return json.dumps({'action': 'session-stop', 'payload': {'order': order},
                       'context': {'provider': 'claude', 'internal_job': True}})


def test_kelmod_answers_a_session_start_through_the_state_core(world_factory, tmp_path):
    root = world_factory().root
    daemon = serve(root, tmp_path)
    try:
        body = json.dumps({'action': 'start', 'payload': {}, 'context': {'provider': 'claude', 'cwd': str(root)}})
        result = subprocess.run([str(KELMOD), 'call', 'state.hook', '--runtime-dir', str(daemon.runtime),
                                 '--body', body], capture_output=True, text=True, timeout=60)
    finally:
        daemon.stop()
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert output['hookEventName'] == 'SessionStart'
    assert isinstance(output['additionalContext'], str)


def test_kelmod_refuses_a_state_hook_from_an_enrolled_client(shared, tmp_path):
    daemon = serve(shared.world.root, tmp_path, '--server-profile', str(profile_path(shared)))
    try:
        client = daemon.client(origin={'client_id': 'mac-a'})
        try:
            body = {'action': 'start', 'payload': {}, 'context': {'provider': 'claude'}}
            header, _ = client.reply(client.call('state.hook', body))
        finally:
            client.close()
    finally:
        daemon.stop()
    assert error_of(header)['code'] == 'E_FORBIDDEN'


@pytest.mark.parametrize('mode, internal_job', [('auto', True), ('off', False)])
def test_a_capture_that_is_internal_or_has_sessions_off_records_nothing(
        world_factory, core_factory, tmp_path, mode, internal_job):
    root = world_factory().root
    provider = tmp_path / 'native'
    provider.mkdir()
    transcript = provider / 'chat.jsonl'
    transcript.write_bytes(lines('claude'))
    configure(root, {'mode': mode, 'sources': {'claude': [str(provider)]}})
    core = core_factory(root)
    before = session_rows(core)
    body = {'action': 'session-stop', 'payload': {'session_id': 'native', 'transcript_path': str(transcript)},
            'context': {'provider': 'claude', 'internal_job': internal_job}}
    assert core.handle_hook(Context.local('hook', 30), body) == {}
    assert session_rows(core) == before


def test_spooled_hooks_are_ingested_oldest_first_and_unreadable_frames_are_renamed(world_factory, core_factory):
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    served = []
    serve_hook = core.serve_hook

    def record(body, **options):
        served.append(body['payload']['order'])
        return serve_hook(body, **options)

    core.serve_hook = record
    # Written in an order other than the names' order, so only the name sort can make the ingest order.
    frames = {
        '00000000000000000004-101.json': capture_frame('second'),
        '00000000000000000002-101.json': json.dumps({'action': 'start', 'payload': {}, 'context': {}}),
        '00000000000000000003-101.json': '{"action": ',
        '00000000000000000001-101.json': capture_frame('first'),
    }
    for name, text in frames.items():
        (spool / name).write_text(text)

    assert core.ingest_spool() == {'ingested': 2}
    assert served == ['first', 'second']
    assert sorted(path.name for path in spool.iterdir()) == [
        '00000000000000000002-101.bad', '00000000000000000003-101.bad']


def test_a_session_end_that_waited_for_the_baton_still_ends_inside_its_deadline(world_factory, core_factory, tmp_path):
    """The hook's deadline runs from its call, not from the moment the writer becomes free: a session end that
    waited for the baton has less time left to try a busy journal again."""
    import sqlite3
    import threading
    import time

    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.sessions import Sessions

    root = world_factory().root
    provider = tmp_path / 'native'
    provider.mkdir()
    transcript = provider / 'chat.jsonl'
    transcript.write_bytes(lines('claude'))
    configure(root, {'mode': 'auto', 'sources': {'claude': [str(provider)]}})
    Sessions(Config.load(root)).close()
    core = core_factory(root)
    taken, release = threading.Event(), threading.Event()

    def publication():
        with core.storage.baton.hold():
            taken.set()
            release.wait(3.0)

    holder = threading.Thread(target=publication, daemon=True)
    holder.start()
    assert taken.wait(5)
    other = sqlite3.connect(Config.load(root).internal('.state/journal.sqlite3'), isolation_level=None)
    other.execute('BEGIN IMMEDIATE')  # busy for the whole hook
    body = {'action': 'session-end', 'payload': {'session_id': 'native', 'transcript_path': str(transcript)},
            'context': {'provider': 'claude', 'internal_job': False}}
    began = time.monotonic()
    try:
        try:
            outcome = core.handle_hook(Context.local('hook', 7), body)
        except KelmoError as error:
            outcome = error
        seconds = time.monotonic() - began
    finally:
        release.set()
        other.execute('ROLLBACK')
        other.close()
        holder.join(5)

    assert isinstance(outcome, KelmoError) and outcome.code == 'E_BUSY', outcome
    assert seconds < 7.0, seconds


def test_a_session_end_whose_baton_stays_held_past_its_deadline_fails_busy_inside_it(world_factory, core_factory,
                                                                                    tmp_path):
    """Another writer keeps the baton longer than the hook's deadline (its own journal statement waits on a lock
    held outside the daemon): the session end does not wait for it past the deadline, it answers E_BUSY."""
    import threading
    import time

    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.sessions import Sessions

    root = world_factory().root
    provider = tmp_path / 'native'
    provider.mkdir()
    transcript = provider / 'chat.jsonl'
    transcript.write_bytes(lines('claude'))
    configure(root, {'mode': 'auto', 'sources': {'claude': [str(provider)]}})
    Sessions(Config.load(root)).close()
    core = core_factory(root)
    taken, release = threading.Event(), threading.Event()

    def writer():
        with core.storage.baton.hold():
            taken.set()
            release.wait(12.0)

    holder = threading.Thread(target=writer, daemon=True)
    holder.start()
    assert taken.wait(5)
    body = {'action': 'session-end', 'payload': {'session_id': 'native', 'transcript_path': str(transcript)},
            'context': {'provider': 'claude', 'internal_job': False}}
    began = time.monotonic()
    try:
        try:
            outcome = core.handle_hook(Context.local('hook', 7), body)
        except KelmoError as error:
            outcome = error
        seconds = time.monotonic() - began
    finally:
        release.set()
        holder.join(15)

    assert seconds < 7.0, seconds
    assert isinstance(outcome, KelmoError) and outcome.code == 'E_BUSY', outcome
def test_a_capture_spooled_after_the_start_is_ingested_once_a_hook_is_served(world_factory, core_factory):
    """`kelmod hook` spooled a capture while the core was running (no daemon answered in time): the next hook the
    core serves starts a pass over the spool, so the capture does not wait for the next start."""
    import time
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    served = []
    serve_hook = core.serve_hook

    def record(body, **options):
        served.append(body['payload'].get('order'))
        return serve_hook(body, **options)

    core.serve_hook = record
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    limit = time.monotonic() + 10
    while list(spool.iterdir()) and time.monotonic() < limit:
        time.sleep(0.05)
    assert list(spool.iterdir()) == [], 'the spooled capture still waits for the next start'
    assert served == ['live', 'late'], served


def test_a_stopping_core_starts_no_spool_pass_after_a_hook(world_factory, core_factory):
    """Once the core is told to stop, a hook it still serves starts no pass over the spool: the capture stays for
    the next start, and the stop waits for no new thread."""
    from kelmo import stopping
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    stopping.request()
    try:
        assert core.handle_hook(Context.local('hook', 30), live) == {}
        assert core._spool is None
    finally:
        stopping.reset()
    assert [path.name for path in spool.iterdir()] == ['00000000000000000001-101.json']


def test_a_spool_that_cannot_be_listed_does_not_fail_the_hook_already_served(world_factory, core_factory,
                                                                            monkeypatch):
    """The hook was served (its capture recorded); a spool directory that cannot be read then must not turn the
    answer into a failure, which `kelmod hook` would spool and send again."""
    from kelmo.core import spool

    def unreadable(directory):
        raise PermissionError('synthetic unreadable spool')

    core = core_factory(world_factory().root)
    monkeypatch.setattr(spool, 'entries', unreadable)
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    try:
        answer = core.handle_hook(Context.local('hook', 30), live)
    except OSError as error:
        answer = error
    assert answer == {}, 'the served hook was answered with a failure: ' + repr(answer)


def test_a_capture_spooled_during_a_pass_is_ingested_after_a_hook_served_meanwhile(world_factory, core_factory):
    """A pass lists the spool once. A capture spooled after that list, while the pass still runs, and a hook
    served in the meantime: the capture is taken once the running pass ends, not left for the next start."""
    import threading
    import time
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    served = []
    gate = threading.Event()
    inside = threading.Event()
    serve_hook = core.serve_hook

    def record(body, **options):
        order = body['payload'].get('order')
        if order == 'first':
            inside.set()
            assert gate.wait(10)
        served.append(order)
        return serve_hook(body, **options)

    core.serve_hook = record
    (spool / '00000000000000000001-101.json').write_text(capture_frame('first'))
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    assert inside.wait(10), 'the pass did not start'
    (spool / '00000000000000000002-101.json').write_text(capture_frame('late'))
    again = {'action': 'session-stop', 'payload': {'order': 'again'},
             'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), again) == {}
    gate.set()
    limit = time.monotonic() + 10
    while list(spool.iterdir()) and time.monotonic() < limit:
        time.sleep(0.05)
    assert list(spool.iterdir()) == [], 'the capture spooled during the pass still waits for the next start'
    assert served == ['live', 'again', 'first', 'late'], served


def test_a_core_whose_channel_was_told_to_stop_starts_no_spool_pass_after_a_hook(world_factory, core_factory):
    """admin.stop marks the channel draining before the core's own stop flag is set: a hook served in between
    starts no pass either, and the capture stays for the next start."""
    import threading
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    core._draining = threading.Event()
    core._draining.set()
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    assert core._spool is None
    assert [path.name for path in spool.iterdir()] == ['00000000000000000001-101.json']


def test_a_spooled_capture_refused_every_time_does_not_hold_back_the_ones_behind_it(world_factory, core_factory):
    """A capture refused as worth retrying is skipped for the pass and the captures behind it run. A refusal the
    server marked worth retrying (E_FENCED with `retryable`) stays for later passes however often it repeats, as
    the edge core keeps it. A local one (a transcript read over even the spool pass's own budget, E_CAPTURE_RETRY)
    would repeat on every pass while holding the writer: on its third try in this core it is set aside."""
    from kelmo.errors import KelmoError
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    serve_hook = core.serve_hook

    def refused(body, **options):
        order = body['payload'].get('order')
        if order == 'fenced':
            error = KelmoError('E_FENCED', 'Synthetic archive writer fenced.')
            error.retryable = True
            raise error
        if order == 'slow':
            raise KelmoError('E_CAPTURE_RETRY', 'Synthetic transcript read over its time.')
        return serve_hook(body, **options)

    core.serve_hook = refused
    (spool / '00000000000000000001-101.json').write_text(capture_frame('fenced'))
    (spool / '00000000000000000002-101.json').write_text(capture_frame('slow'))
    (spool / '00000000000000000003-101.json').write_text(capture_frame('behind'))
    passes = [core.ingest_spool() for _ in range(2)]
    assert passes[0]['ingested'] == 1, passes
    assert sorted(path.name for path in spool.iterdir()) == [
        '00000000000000000001-101.json', '00000000000000000002-101.json'], passes
    passes += [core.ingest_spool() for _ in range(2)]
    assert sorted(path.name for path in spool.iterdir()) == [
        '00000000000000000001-101.json', '00000000000000000002-101.bad'], passes


def test_a_spooled_capture_refused_for_good_is_set_aside(world_factory, core_factory):
    """A refusal not worth retrying would repeat on every pass: the capture is renamed `*.bad` at once."""
    from kelmo.errors import KelmoError
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    serve_hook = core.serve_hook

    def refused(body, **options):
        if body['payload'].get('order') == 'bad':
            raise KelmoError('E_INPUT', 'Synthetic capture refused for good.')
        return serve_hook(body, **options)

    core.serve_hook = refused
    (spool / '00000000000000000001-101.json').write_text(capture_frame('bad'))
    assert core.ingest_spool() == {'ingested': 0}
    assert [path.name for path in spool.iterdir()] == ['00000000000000000001-101.bad']


def test_a_capture_refused_once_as_worth_retrying_runs_on_a_later_pass(world_factory, core_factory):
    """A transcript that changed while it was read is refused E_CAPTURE_RETRY once; the next pass takes it."""
    from kelmo.errors import KelmoError
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    serve_hook = core.serve_hook
    tries = []

    def changed_once(body, **options):
        tries.append(body['payload'].get('order'))
        if len(tries) == 1:
            raise KelmoError('E_CAPTURE_RETRY', 'Synthetic transcript changed while read.')
        return serve_hook(body, **options)

    core.serve_hook = changed_once
    (spool / '00000000000000000001-101.json').write_text(capture_frame('changing'))
    core.ingest_spool()
    assert [path.name for path in spool.iterdir()] == ['00000000000000000001-101.json'], 'set aside on one refusal'
    core.ingest_spool()
    assert list(spool.iterdir()) == [], 'a later pass did not take the capture'


def test_a_pass_stopped_by_a_busy_journal_runs_again_when_a_hook_asked_meanwhile(world_factory, core_factory):
    """A hook served while a pass runs asks for another pass; that pass stopping on E_BUSY must not drop the ask,
    or the capture spooled before that hook waits for the next hook served."""
    import threading
    import time

    from kelmo.errors import KelmoError
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    gate, inside = threading.Event(), threading.Event()
    serve_hook = core.serve_hook
    busy = []

    def busy_once(body, **options):
        if body['payload'].get('order') == 'first' and not busy:
            busy.append(1)
            inside.set()
            assert gate.wait(10)
            raise KelmoError('E_BUSY', 'Synthetic busy journal.')
        return serve_hook(body, **options)

    core.serve_hook = busy_once
    (spool / '00000000000000000001-101.json').write_text(capture_frame('first'))
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    assert inside.wait(10), 'the pass did not start'
    again = {'action': 'session-stop', 'payload': {'order': 'again'},
             'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), again) == {}
    gate.set()
    limit = time.monotonic() + 10
    while list(spool.iterdir()) and time.monotonic() < limit:
        time.sleep(0.05)
    assert list(spool.iterdir()) == [], 'the capture waits for the next hook after a busy pass dropped the ask'


def test_a_core_told_to_stop_does_not_list_the_spool(world_factory, core_factory, monkeypatch):
    """Listing the spool may remove stale temporary files; a core already told to stop leaves the spool as it is."""
    import threading

    from kelmo.core import spool as spool_module
    core = core_factory(world_factory().root)
    listed = []
    monkeypatch.setattr(spool_module, 'entries', lambda directory: listed.append(directory) or [])
    core._draining = threading.Event()
    core._draining.set()
    core.ingest_spool()
    assert listed == [], 'a core told to stop listed the spool'


def test_a_hook_served_by_a_core_told_to_stop_does_not_list_the_spool(world_factory, core_factory, monkeypatch):
    """A hook a stopping core still serves must leave the spool as it is: listing it may remove stale temporary
    files."""
    import threading

    from kelmo.core import spool as spool_module
    core = core_factory(world_factory().root)
    listed = []
    monkeypatch.setattr(spool_module, 'entries', lambda directory: listed.append(directory) or [])
    core._draining = threading.Event()
    core._draining.set()
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    assert listed == [], 'a hook served by a core told to stop listed the spool'


def test_a_spool_pass_that_cannot_start_does_not_fail_the_hook_already_served(world_factory, core_factory,
                                                                             monkeypatch):
    """The hook was served; a thread that cannot be started for the spool pass must not turn the answer into a
    failure, which `kelmod hook` would spool and send again."""
    import threading
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))

    def no_threads(self):
        raise RuntimeError("synthetic: can't start new thread")

    monkeypatch.setattr(threading.Thread, 'start', no_threads)
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    try:
        answer = core.handle_hook(Context.local('hook', 30), live)
    except RuntimeError as error:
        answer = error
    assert answer == {}, 'the served hook was answered with a failure: ' + repr(answer)
    assert core._spool is None, 'a thread that never started is kept as the running pass'


def test_a_spool_pass_that_fails_ends_quietly_and_leaves_no_running_pass(world_factory, core_factory, monkeypatch):
    """A pass whose spool listing fails ends without an uncaught exception in its thread, clears itself, and the
    next hook served starts a new pass that takes the capture."""
    import threading
    import time

    from kelmo.core import spool as spool_module
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    real = spool_module.entries
    calls = []

    def fails_in_the_pass(directory):
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == 'hook-spool' and calls.count('hook-spool') == 1:
            raise PermissionError('synthetic unreadable spool in the pass')
        return real(directory)

    uncaught = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: uncaught.append(args.exc_type))
    monkeypatch.setattr(spool_module, 'entries', fails_in_the_pass)
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    limit = time.monotonic() + 10
    while core._spool is not None and time.monotonic() < limit:
        time.sleep(0.05)
    assert uncaught == [], uncaught
    assert core._spool is None, 'the failed pass is still held as the running pass'
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    limit = time.monotonic() + 10
    while list(spool.iterdir()) and time.monotonic() < limit:
        time.sleep(0.05)
    assert list(spool.iterdir()) == [], 'the next hook did not take the capture'


def test_a_spool_pass_captures_with_a_budget_of_its_own(world_factory, core_factory):
    """No caller waits for the spool pass, so a capture there gets a longer budget than a hook's 1.5 s."""
    from kelmo.core.state_core import SPOOL_CAPTURE_SECONDS
    core = core_factory(world_factory().root)
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    seen = []
    core.serve_hook = lambda body, **options: seen.append(options) or {}
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    assert core.ingest_spool() == {'ingested': 1}
    assert seen == [{'capture_budget': SPOOL_CAPTURE_SECONDS}], seen
    assert 1.5 < SPOOL_CAPTURE_SECONDS <= 5.5, 'a hook capture waiting behind it must still answer inside 7 s'


def test_a_capture_budget_given_to_a_hook_reaches_the_session_capture(world_factory, core_factory, monkeypatch):
    from kelmo import session_control
    core = core_factory(world_factory().root)
    seen = []
    monkeypatch.setattr(session_control, 'capture_hook',
                        lambda root, provider, action, payload, **options: seen.append(options) or {})
    body = json.loads(capture_frame('late'))
    assert core.serve_hook(body, capture_budget=4.0) == {}
    assert seen and seen[0].get('time_budget') == 4.0, seen
    assert core.serve_hook(json.loads(capture_frame('live'))) == {}
    assert 'time_budget' not in seen[1] or seen[1]['time_budget'] == 1.5, seen


def test_a_stop_that_lands_while_the_spool_thread_starts_runs_no_pass(world_factory, core_factory, monkeypatch):
    """admin.stop can mark the channel draining after the hook's stop check and before the thread starts: the
    thread then neither lists the spool (listing may remove stale temporary files) nor serves anything."""
    import threading
    import time

    from kelmo.core import spool as spool_module
    core = core_factory(world_factory().root)
    real_entries = spool_module.entries
    listed_by = []
    monkeypatch.setattr(spool_module, 'entries', lambda directory: listed_by.append(
        threading.current_thread().name) or real_entries(directory))
    spool = core.root / '.state' / 'hook-spool'
    spool.mkdir(parents=True)
    (spool / '00000000000000000001-101.json').write_text(capture_frame('late'))
    core._draining = threading.Event()
    served = []
    serve_hook = core.serve_hook
    core.serve_hook = lambda body, **options: served.append(body['payload'].get('order')) or serve_hook(body)
    real_start = threading.Thread.start

    def stop_lands(thread):
        if thread.name == 'hook-spool':
            core._draining.set()
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, 'start', stop_lands)
    live = {'action': 'session-stop', 'payload': {'order': 'live'},
            'context': {'provider': 'claude', 'internal_job': True}}
    assert core.handle_hook(Context.local('hook', 30), live) == {}
    limit = time.monotonic() + 5
    while core._spool is not None and time.monotonic() < limit:
        time.sleep(0.05)
    assert served == ['live'], served
    assert 'hook-spool' not in listed_by, listed_by
    assert [path.name for path in spool.iterdir()] == ['00000000000000000001-101.json']
