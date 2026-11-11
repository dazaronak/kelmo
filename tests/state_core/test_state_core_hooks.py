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

    def record(body):
        served.append(body['payload']['order'])
        return serve_hook(body)

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
