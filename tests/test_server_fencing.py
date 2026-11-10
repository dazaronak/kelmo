"""Server identity, writer fencing and the restricted archive-process boundary.

Python-level checks here establish request strictness, writer epochs and the grants the
coordinator computes. They are not evidence of OS isolation: the native cases at the end
run only when the controller sets KELMO_NATIVE_ISOLATION=1 on a real macOS or Linux
host, and their skip is an incomplete gate, never a pass.
"""

import io
import json
import os
import sqlite3
import sys
import threading
import time

import pytest

from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

NATIVE = os.environ.get('KELMO_NATIVE_ISOLATION') == '1'


def test_client_probe_checks_current_authority_without_flushing(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    client = shared.client('mac-a')
    shared.hub.online = False
    ClientService(client).capture('Queued offline.', 'probe.md', role='collector',
                                  idempotency_key='probe-offline')
    shared.hub.online = True
    shared.hub.requests.clear()
    answer = client.probe()
    assert answer['verified'] and answer['writer_epoch'] == 1
    assert answer['client_id'] == 'mac-a' and answer['writes_flushed'] is False
    assert [frame['op'] for _, frame in shared.hub.requests] == ['hello']
    shared.hub.online = False
    with pytest.raises(KelmoError):
        client.probe()


def request(op, args=None, key=None, **extra):
    from kelmo.server import PROTOCOL
    value = {'protocol': PROTOCOL, 'request_id': 'r1', 'op': op, 'args': args or {}}
    if key is not None:
        value['idempotency_key'] = key
    value.update(extra)
    return value


@pytest.mark.parametrize('frame', [
    request('capture', {'content': 'x', 'filename': 'x.md'}, 'k', origin={'client_id': 'requester-client'}),
    request('capture', {'content': 'x', 'filename': 'x.md'}, 'k', client_id='requester-client'),
    request('capture', {'content': 'x', 'filename': 'x.md'}, 'k', writer_epoch=2),
    request('capture', {'content': 'x', 'filename': 'x.md', 'origin': {'client_id': 'requester-client'}}, 'k'),
    request('capture', {'content': 'x', 'filename': 'x.md', 'source_path': '/etc/hosts'}, 'k'),
    request('context', {'cwd': '/'}),
    request('context', {'repository': 'example.invalid/repo-app', 'path': '/'}),
    request('session_capture', {'provider': 'claude', 'native_id': 'n', 'event': 'Stop', 'content': '',
                                'sha256': '0' * 64, 'size': 0, 'file_identity': 'i', 'client_generation': 1,
                                'transcript_path': '/etc/hosts'}, 'k'),
    request('status', {'executable': '/bin/sh'}),
    request('graph_query', {'params': {}, 'wiki_root': '/'}),
])
def test_payload_cannot_choose_origin_paths_providers_or_writer(shared, frame):
    session = shared.hub.session('mac-a')
    before = shared.world.service.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
    reply = session.handle(frame)
    assert reply['ok'] is False and reply['error']['code'] == 'E_PROTOCOL'
    assert shared.world.service.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == before


def test_compat_arguments_cannot_name_providers_or_paths(shared):
    session = shared.hub.session('mac-a')
    reply = session.handle(request('compat', {'tool': 'memory_search',
                                              'arguments': {'query': 'x', 'provider_command': ['/bin/sh']}}))
    assert reply['ok'] is False and reply['error']['code'] == 'E_INPUT'


def test_operation_origin_is_the_channel_binding_not_the_text(shared):
    from kelmo.client import ClientService
    client = shared.client('mac-a')
    result = ClientService(client).capture('REQUESTER: origin=requester-client; client_id=requester-client',
                                           'claim.md', role='lead', idempotency_key='claim')
    origin = shared.world.service.journal.origin_record(result['id'])
    assert origin['client_id'] == 'mac-a' and origin['machine_tag'] == 'mac-a'


def test_requester_authority_is_not_granted_by_transport(shared, tmp_path, monkeypatch, capsys):
    from kelmo.contracts import EvidenceRef
    from kelmo.errors import KelmoError
    world = shared.world
    for client_id in ('mac-a', 'requester-client'):
        from kelmo.client import ClientService
        client = shared.client(client_id)
        service = ClientService(client)
        captured = service.add('I pushed the shared feature.', 'push.md', role='implementer',
                               idempotency_key='push-' + client_id)
        shared.hub.session(client_id).service.run(captured['operation']['id'])
        captured = service.wait(captured, timeout=1)
        ref = EvidenceRef.from_dict(captured['evidence_ref'])
        update = world.update(ref)
        if client_id == 'mac-a':
            before = world.registry()
            with pytest.raises(KelmoError):
                ClientService(client).apply_action(update.to_dict(), 'apply-' + client_id)
            assert world.registry() == before
            continue
        receipt = ClientService(client).apply_action(update.to_dict(), 'apply-' + client_id)
        assert receipt['outcome'] == 'published'
        action = world.action()
        assert action['state'] == 'completed' and action['accepted_event']['evidence_mark'] == 'REQUESTER'
        assert action['accepted_event']['evidence_ref'] == ref.to_dict()
        assert world.service.knowledge.generation().to_dict() == receipt['generation']
        import json
        from contextlib import nullcontext
        from kelmo.cli import call
        document = tmp_path / 'action.json'
        document.write_text(json.dumps(update.to_dict()))
        monkeypatch.setattr('kelmo.client.SharedClient', lambda profile: nullcontext(client))
        assert call(['--vault', str(client.profile.root), 'action', 'apply', '--file', str(document),
                     '--idempotency-key', 'apply-' + client_id, '--json']) == 0
        assert json.loads(capsys.readouterr().out)['result'] == receipt


def _cutover(world, writer_id, epoch):
    """The controller's activation of a new writer epoch on the synthetic archive."""
    from kelmo.config import Config
    connection = sqlite3.connect(Config.load(world.root).archive)
    try:
        with connection:
            connection.execute('UPDATE archive_writer_state SET writer_id=?, epoch=?', (writer_id, epoch))
    finally:
        connection.close()


def test_stale_writer_epoch_is_rejected(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    from kelmo.server import ClientSession
    client = shared.client('mac-a')
    service = ClientService(client)
    service.capture('Before cutover.', 'before.md', role='collector', idempotency_key='before')
    _cutover(shared.world, 'server', 2)
    # A server that started before the cutover is fenced on its next write.
    queued = service.capture('After cutover, old writer.', 'stale.md', role='collector', idempotency_key='stale')
    assert queued['queued'] is True and queued['pending_reason'] == 'E_WRITER'
    with pytest.raises(KelmoError) as error:
        ClientSession(shared.world.root, shared.hub.profile, 'mac-a')
    assert error.value.code == 'E_WRITER'
    shared.hub.reset(shared.profile(writer={'writer_id': 'server', 'epoch': 2}))
    assert client.flush()['delivered'] == 1
    assert shared.world.service.journal.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE filename='stale.md'").fetchone()[0] == 1


def test_fenced_server_keeps_reads_and_replays_and_queues_writes(shared):
    from kelmo.client import ClientService
    client = shared.client('mac-a')
    service = ClientService(client)
    first = service.capture('Accepted before the fence.', 'fence.md', role='collector', idempotency_key='fence-1')
    shared.hub.reset(shared.profile(fenced=True))
    assert client.call('hello')['fenced'] is True
    ok, replay = client._send(client._connect(), 'capture', json.loads(client.outbox.get('fence-1')['args']),
                              'fence-1')
    assert ok and replay['operation'] == first
    queued = service.capture('Written while fenced.', 'fenced.md', role='collector', idempotency_key='fence-2')
    assert queued['queued'] is True and queued['pending_reason'] == 'E_FENCED'
    # Reads stay open: the local Service contract, a list by default and the envelope on request.
    assert isinstance(service.search('fence'), list)
    searched = service.search('fence', with_metadata=True)
    assert set(searched) >= {'items', 'search'}
    assert shared.world.service.journal.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE filename='fenced.md'").fetchone()[0] == 0


def test_a_fence_set_after_a_channel_opened_holds_its_writes(shared, tmp_path):
    from kelmo.server import ClientSession, ServerProfile
    from kelmo.server_admin import set_fence
    from test_shared_context import profile_value
    path = tmp_path / 'server.json'
    path.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
    args = {'content': 'Written through a long-lived channel.', 'filename': 'live.md'}
    with ClientSession(shared.world.root, ServerProfile.load(path), 'mac-a', profile_path=path) as session:
        assert session._dispatch('capture', args, 'live-1')['ok']
        set_fence(shared.world.root, path, True, apply=True)
        with pytest.raises(Exception) as fenced:
            session._dispatch('capture', dict(args, content='Held by the fence.'), 'live-2')
        assert getattr(fenced.value, 'code', None) == 'E_FENCED'
        path.write_text('not json')
        with pytest.raises(Exception) as unreadable:
            session._dispatch('capture', dict(args, content='Held by the fence.'), 'live-2')
        assert getattr(unreadable.value, 'code', None) == 'E_FENCED'
        path.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
        assert session._dispatch('capture', dict(args, content='Held by the fence.'), 'live-2')['ok']


def test_a_running_worker_pauses_while_the_profile_is_fenced(shared, tmp_path):
    from kelmo.cli import shared_opener
    from kelmo.daemon import run
    from kelmo.server_admin import set_fence
    from test_shared_context import profile_value
    path = tmp_path / 'server.json'
    path.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
    opener = shared_opener(shared.world.root, path)
    with opener(None) as service:
        assert 'fenced' not in run(service, None, once=True, open_service=opener)
        set_fence(shared.world.root, path, True, apply=True)
        assert run(service, None, once=True, open_service=opener) == {
            'operations': 0, 'resolutions': 0, 'fenced': True}
        with pytest.raises(Exception) as fenced:
            opener('mac-a')
        assert getattr(fenced.value, 'code', None) == 'E_FENCED'
        path.write_text('not json')
        assert run(service, None, once=True, open_service=opener)['fenced'] is True
        path.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
        assert 'fenced' not in run(service, None, once=True, open_service=opener)


def test_a_looping_worker_says_once_that_it_waits_and_when_it_resumes(tmp_path, monkeypatch, capsys):
    import os
    import signal
    from kelmo import daemon
    states = iter([True, True, False])

    def opener(client_id):
        raise AssertionError('the pass is replaced in this test')

    def one_pass_then_stop(service, argv, **options):
        os.kill(os.getpid(), signal.SIGTERM)
        return {'operations': 0, 'resolutions': 0}

    opener.fenced = lambda: next(states)
    monkeypatch.setattr(daemon, 'tick', one_pass_then_stop)
    (tmp_path / 'worker.json').write_text(json.dumps({'poll_seconds': 0.1}))
    assert daemon.run(None, tmp_path / 'worker.json', open_service=opener) == {'stopped': True}
    errors = capsys.readouterr().err
    assert errors.count('fenced; waiting') == 1 and errors.count('fence is lifted; resuming') == 1


def test_process_step_is_scoped_to_the_capturing_client_and_fenced(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    owner, other = shared.client('mac-a'), shared.client('mac-b')
    queued = ClientService(owner).capture('Process me later.', 'later.md', role='implementer',
                                          idempotency_key='later')
    assert queued['state'] == 'queued'
    # Another client's key namespace never reaches this capture.
    with pytest.raises(KelmoError) as error:
        other.submit('process', {'capture_key': 'later'}, 'steal-later')
    assert error.value.code == 'E_NOT_FOUND'
    assert shared.world.service.journal.get(queued['id'])['state'] == 'queued'
    shared.hub.reset(shared.profile(fenced=True))
    pending = owner.submit('process', {'capture_key': 'later'}, 'process-later')
    assert pending['queued'] is True and pending['pending_reason'] == 'E_FENCED'
    assert shared.world.service.journal.get(queued['id'])['state'] == 'queued'
    shared.hub.reset(shared.profile())
    assert owner.flush()['delivered'] == 1
    assert shared.world.service.journal.get(queued['id'])['durable_stage'] == 'archived'


def test_trace_is_limited_to_the_capturing_origin(shared):
    from kelmo.client import ClientService
    from kelmo.errors import KelmoError
    owner, other = shared.client('mac-a'), shared.client('mac-b')
    result = ClientService(owner).capture('Mine.', 'mine.md', role='collector', idempotency_key='mine')
    operation_id = result['id']
    assert ClientService(owner).trace(operation_id)['operation']['id'] == operation_id
    with pytest.raises(KelmoError) as error:
        ClientService(other).trace(operation_id)
    assert error.value.code == 'E_NOT_FOUND'


def test_server_profile_is_strict_and_confined_by_default(shared):
    from kelmo.errors import KelmoError
    from kelmo.server import ServerProfile
    from test_shared_context import profile_value
    value = profile_value(shared.world, shared.tmp_path)
    del value['archive_process']
    assert ServerProfile.from_dict(value).archive_process == 'confined'
    for change in ({'archive_process': 'unconfined'}, {'listen': '0.0.0.0:80'},
                   {'clients': [{'client_id': 'a/b', 'machine_tag': 'm'}]}):
        with pytest.raises(KelmoError) as error:
            ServerProfile.from_dict(dict(value, **change))
        assert error.value.code == 'E_CONFIG'
    with pytest.raises(KelmoError):
        ServerProfile.load('relative-profile.json')


def test_archive_process_grants_exclude_coordinator_data(shared, tmp_path):
    """Structural only: the grants the launcher computes. OS denial is the native case below."""
    from kelmo.config import Config
    from kelmo.server import archive_paths, coordinator_canaries, import_roots
    config = Config.load(shared.world.root)
    scratch = (tmp_path / 'scratch').resolve()
    scratch.mkdir()
    paths = archive_paths(config, __import__('pathlib').Path(sys.executable).resolve(), import_roots(), scratch)
    assert paths.writable == (config.archive.parent.resolve(), scratch)
    protected = [config.root, config.state, config.knowledge_git_root / '.git', config.wiki, config.registry]
    for granted in (*paths.read_only, *paths.writable, *paths.executables):
        for item in protected:
            assert not (granted == item or item.is_relative_to(granted)), (granted, item)
    canaries = coordinator_canaries(config)
    for required in (config.state / 'journal.sqlite3', config.registry, config.root / 'kelmo.toml'):
        assert str(required) in canaries
    assert not any(str(config.archive.parent) == item or str(config.root) == item for item in canaries)


def test_missing_native_confinement_fails_closed(shared, monkeypatch):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.runtime import IsolationError
    from kelmo.server import open_archive
    config = Config.load(shared.world.root)
    with pytest.raises(KelmoError) as error:
        open_archive(config, shared.hub.profile.bindings, 'confined', platform='win32')
    assert error.value.code == 'E_ISOLATION'

    def no_bubblewrap():
        raise IsolationError('Install bubblewrap before starting the Linux runtime.')
    monkeypatch.setattr('kelmo.linux_runtime.launcher', no_bubblewrap)
    with pytest.raises(KelmoError) as error:
        open_archive(config, shared.hub.profile.bindings, 'confined', platform='linux')
    assert error.value.code == 'E_ISOLATION'
    with pytest.raises(KelmoError) as error:
        open_archive(config, shared.hub.profile.bindings, 'unconfined')
    assert error.value.code == 'E_CONFIG'


def test_worker_refuses_to_serve_when_it_can_read_coordinator_data(shared):
    """Run unconfined on purpose: the worker's own startup check must refuse, not serve."""
    from kelmo.config import Config
    from kelmo.server import ARCHIVE_PROTOCOL, archive_worker_main, coordinator_canaries, encode_frame
    config = Config.load(shared.world.root)
    startup = {'protocol': ARCHIVE_PROTOCOL, 'enrollment': shared.hub.profile.bindings.describe(),
               'archive': str(config.archive), 'cache': str(config.cache), 'scratch': str(shared.tmp_path),
               'canaries': coordinator_canaries(config), 'fenced': False, 'embedding': None,
               'idle_seconds': None}
    stdout = io.BytesIO()
    assert archive_worker_main(io.BytesIO(encode_frame(startup)), stdout, confined=True) == 1
    reply = json.loads(stdout.getvalue())
    assert reply['ready'] is False and reply['error']['code'] == 'E_ISOLATION'


def _worker_startup(shared, idle_seconds):
    from kelmo.config import Config
    from kelmo.server import ARCHIVE_PROTOCOL
    config = Config.load(shared.world.root)
    return {'protocol': ARCHIVE_PROTOCOL, 'enrollment': shared.hub.profile.bindings.describe(),
            'archive': str(config.archive), 'cache': str(config.cache), 'scratch': str(shared.tmp_path),
            'canaries': [], 'fenced': False, 'embedding': None, 'idle_seconds': idle_seconds}


def test_an_idle_worker_exits_on_its_own_without_reading(shared):
    """An idle session's worker frees its archive connection and tokenizer; the parent restarts it on demand."""
    from kelmo.server import ARCHIVE_IDLE_EXIT, archive_worker_main, encode_frame
    read_end, write_end = os.pipe()
    os.write(write_end, encode_frame(_worker_startup(shared, 0.2)))
    stdout = io.BytesIO()
    started = time.monotonic()
    try:
        with os.fdopen(read_end, 'rb') as stdin:
            assert archive_worker_main(stdin, stdout, confined=False) == ARCHIVE_IDLE_EXIT
    finally:
        os.close(write_end)
    assert 0.2 <= time.monotonic() - started < 5
    assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [{'ready': True, 'confined': False}]


def test_an_idle_limit_keeps_serving_frames_that_arrive_in_time(shared):
    from kelmo.server import ARCHIVE_IDLE_EXIT, archive_worker_main, encode_frame
    read_end, write_end = os.pipe()
    os.write(write_end, encode_frame(_worker_startup(shared, 0.5)))
    stdout = io.BytesIO()

    def parent():
        for _ in range(3):
            time.sleep(0.2)
            os.write(write_end, encode_frame({'method': 'counts', 'args': {}}))
    sender = threading.Thread(target=parent)
    sender.start()
    try:
        with os.fdopen(read_end, 'rb') as stdin:
            assert archive_worker_main(stdin, stdout, confined=False) == ARCHIVE_IDLE_EXIT
    finally:
        sender.join()
        os.close(write_end)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(replies) == 4 and all('result' in reply for reply in replies[1:])


@pytest.mark.parametrize('idle_seconds', [0, -1, True, '60', [60]])
def test_a_startup_frame_with_a_bad_idle_limit_is_refused(shared, idle_seconds):
    from kelmo.server import archive_worker_main, encode_frame
    stdout = io.BytesIO()
    startup = _worker_startup(shared, idle_seconds)
    assert archive_worker_main(io.BytesIO(encode_frame(startup)), stdout, confined=False) == 1
    assert json.loads(stdout.getvalue())['error']['code'] == 'E_PROTOCOL'


def test_archive_frames_reissue_origins_and_reject_unknown_ones(shared):
    from kelmo.config import Config
    from kelmo.server import open_archive
    archive = open_archive(Config.load(shared.world.root), shared.hub.profile.bindings, 'in-process')
    try:
        forged = {'authority_id': shared.world.bindings.authority_id, 'client_id': 'mac-a',
                  'machine_tag': 'requester-machine', 'session_id': None}
        reply = archive._transport.call({'method': 'capture_receipt',
                                         'args': {'operation_id': 'x', 'origin': forged}})
        assert reply['error']['code'] == 'E_ORIGIN'
        reply = archive._transport.call({'method': 'counts', 'args': {'path': '/'}})
        assert reply['error']['code'] == 'E_PROTOCOL'
    finally:
        archive.close()


def test_state_action_retryability_comes_from_its_request_row(shared):
    """Dirty knowledge is retried though its code is E_CONFLICT; a refusal the coordinator settled is final."""
    from kelmo.errors import KelmoError
    from kelmo.publication import StateCoordinator
    from kelmo.wiki.engine import DirtyKnowledge
    session = shared.hub.session('requester-client')
    owner = shared.world.service.capture('Owner of the synthetic rows.', 'row-owner.md', 'collector',
                                         idempotency_key='row-owner')['id']
    for key, error in (('settled', ('E_CONFLICT', 'Action revision changed.')), ('pending', None)):
        session.service.journal.action_begin(StateCoordinator._request_key('apply', session.origin, key), 'apply',
                                             'fingerprint-' + key, session.origin, owner, error=error)
    conflict = KelmoError('E_CONFLICT', 'Action revision changed; a stale command cannot publish.')
    assert session._action_retryable(DirtyKnowledge(['projects/app/page.md']), 'never-recorded') is True
    assert session._action_retryable(conflict, 'settled') is False
    assert session._action_retryable(conflict, 'pending') is True
    assert session._action_retryable(conflict, 'never-recorded') is False
    assert session._action_retryable(KelmoError('E_GIT', 'Synthetic.'), 'never-recorded') is True


def test_dirty_knowledge_keeps_a_state_apply_queued(shared, monkeypatch):
    from kelmo.wiki.engine import DirtyKnowledge
    ref = shared.world.archive('I pushed the shared feature.', key='dirty-evidence')
    update = shared.world.update(ref)

    def dirty(self, action, origin, key):
        raise DirtyKnowledge(['projects/app/shared-feature.md'])

    monkeypatch.setattr('kelmo.state.ActionState.apply', dirty)
    client = shared.client('requester-client')
    queued = client.submit('state_apply', {'update': update.to_dict()}, 'dirty-apply')
    assert queued['queued'] is True and queued['pending_reason'] == 'E_CONFLICT'
    assert client.outbox.get('dirty-apply')['state'] == 'pending'


def test_broker_launch_passes_only_its_channel_and_model_grants_stay_read_only(shared, tmp_path, monkeypatch):
    from kelmo.config import Config
    from kelmo.runtime import IsolationError
    from kelmo.server import archive_paths, embedding_launch, import_roots
    config = Config.load(shared.world.root)
    tokenizer = tmp_path / 'broker' / 'tokenizer.json'
    tokenizer.parent.mkdir()
    tokenizer.write_text('{}')
    request, answer = os.pipe()
    try:
        monkeypatch.setenv('SYNTHETIC_EMBED_FDS', f'{request},{answer}')
        monkeypatch.setenv('SYNTHETIC_UNRELATED_SECRET', 'never-forwarded')
        settings = {'provider': 'broker', 'fds_env': 'SYNTHETIC_EMBED_FDS', 'tokenizer': str(tokenizer),
                    'space': {}, 'input': {}}
        wire, roots, files, fds, env = embedding_launch(settings)
        assert wire == settings and roots == [] and files == [tokenizer.resolve()]
        assert fds == (request, answer) and env == {'SYNTHETIC_EMBED_FDS': f'{request},{answer}'}
        monkeypatch.delenv('SYNTHETIC_EMBED_FDS')
        assert embedding_launch(settings)[3] == ()  # no channel: vectors stay pending, nothing is passed
    finally:
        os.close(request)
        os.close(answer)
    scratch = (tmp_path / 'scratch').resolve()
    scratch.mkdir()
    python = __import__('pathlib').Path(sys.executable).resolve()
    model = config.root / 'models' / 'pinned'
    model.mkdir(parents=True)
    paths = archive_paths(config, python, import_roots(), scratch, model_roots=[model], model_files=[tokenizer])
    assert model.resolve() in paths.read_only and tokenizer.resolve() in paths.read_exact
    assert model.resolve() not in paths.writable
    for inside in (config.state / 'model', config.wiki):
        with pytest.raises(IsolationError):
            archive_paths(config, python, import_roots(), scratch, model_roots=[inside])


def _graph_record(world):
    from kelmo.config import Config
    from kelmo.server import GRAPH_GENERATION_KEY
    connection = sqlite3.connect(Config.load(world.root).archive)
    try:
        row = connection.execute('SELECT value FROM archive_meta WHERE key=?', (GRAPH_GENERATION_KEY,)).fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def test_fenced_server_never_publishes_a_graph_but_reads_a_current_one(shared):
    from test_shared_context import publish_push
    session = shared.hub.session('mac-a')
    assert session.handle(request('graph_rebuild'))['ok'] is True
    built = _graph_record(shared.world)
    shared.hub.reset(shared.profile(fenced=True))
    session = shared.hub.session('mac-a')
    for frame in (request('graph_rebuild'),):
        reply = session.handle(frame)
        assert reply['ok'] is False and reply['error']['code'] == 'E_FENCED' and reply['error']['retryable']
    current = session.handle(request('graph_query', {'params': {'action': 'search', 'query': 'person'}}))
    assert current['ok'] is True and current['result']['graph']['status'] == 'current'
    publish_push(shared.world, 'fenced-graph')
    stale = session.handle(request('graph_query', {'params': {'action': 'search', 'query': 'person'}}))
    assert stale['ok'] is False and stale['error']['code'] == 'E_GRAPH_STALE'
    assert _graph_record(shared.world) == built, 'a fenced read never rebuilds'


def test_stale_writer_cannot_publish_a_graph(shared):
    session = shared.hub.session('mac-a')
    before = _graph_record(shared.world)
    _cutover(shared.world, 'server', 2)
    reply = session.handle(request('graph_rebuild'))
    assert reply['ok'] is False and reply['error']['code'] == 'E_WRITER'
    assert _graph_record(shared.world) == before


def test_a_live_attempt_holds_its_request_and_is_never_run_twice(shared):
    import fcntl
    from kelmo.config import Config
    from kelmo.server import _request_digest
    session = shared.hub.session('mac-a')
    path = Config.load(shared.world.root).internal('.state/client-requests')
    path.mkdir(parents=True, exist_ok=True)
    frame = request('capture', {'content': 'Held.', 'filename': 'held.md', 'role': 'collector'}, 'held')
    with (path / (_request_digest(['mac-a', 'held']) + '.lock')).open('a') as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        busy = session.handle(frame)
    assert busy['ok'] is False and busy['error'] == {'code': 'E_BUSY', 'retryable': True,
                                                     'message': busy['error']['message']}
    assert shared.world.service.journal.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE filename='held.md'").fetchone()[0] == 0
    assert session.handle(frame)['ok'] is True
    assert session.handle(frame)['replayed'] is True


def test_dead_archive_process_is_restarted_by_the_next_operation(shared, tmp_path):
    import subprocess
    from kelmo.errors import KelmoError
    from kelmo.server import ArchiveProcess, _Pipe
    exited = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    exited.wait()
    pipe = _Pipe(exited, tmp_path / 'scratch')
    with pytest.raises(KelmoError) as error:
        pipe.call({'method': 'counts', 'args': {}})
    assert error.value.code == 'E_ARCHIVE_PROCESS' and pipe.failed

    class Fresh:
        failed = None
        frames = []

        def call(self, frame):
            self.frames.append(frame)
            return {'result': {'archive_records': 0}}

        def close(self):
            pass

    archive = ArchiveProcess(pipe, shared.hub.profile.bindings, {'mode': 'confined'}, restart=Fresh)
    assert archive.counts() == {'archive_records': 0}
    assert [frame['method'] for frame in Fresh.frames] == ['counts']


def test_protocol_stdout_is_isolated_from_prints_and_fd1():
    import subprocess
    from pathlib import Path
    import kelmo
    source = str(Path(kelmo.__file__).resolve().parents[1])
    script = ('import os, sys; sys.path.insert(0, sys.argv[1]); '
              'from kelmo.server import protocol_streams, write_frame; '
              '_, out = protocol_streams(); print("stray print", flush=True); os.write(1, b"stray fd1\\n"); '
              'write_frame(out, {"ok": True})')
    done = subprocess.run([sys.executable, '-c', script, source], capture_output=True, timeout=60)
    assert done.returncode == 0 and done.stdout == b'{"ok":true}\n'
    assert b'stray print' in done.stderr and b'stray fd1' in done.stderr


# A stand-in archive process speaking the frame protocol: it logs every method it receives, answers with
# the method name, and exits without answering on `die`.
ECHO_ARCHIVE = '''
import json, sys
log = open(sys.argv[1], 'a')
for line in sys.stdin.buffer:
    method = json.loads(line)['method']
    log.write(method + '\\n')
    log.flush()
    if method == 'close':
        break
    if method == 'die':
        raise SystemExit(3)
    sys.stdout.buffer.write(json.dumps({'result': method}).encode() + b'\\n')
    sys.stdout.buffer.flush()
'''


def _echo_archive(tmp_path, log):
    import subprocess
    from kelmo.server import _Pipe
    started = []

    def start(*, uncertain=False):
        scratch = tmp_path / ('scratch-' + str(len(started)))
        scratch.mkdir()
        process = subprocess.Popen([sys.executable, '-c', ECHO_ARCHIVE, str(log)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        started.append({'process': process, 'uncertain': uncertain})
        return _Pipe(process, scratch)
    return start, started


def _received(log):
    return [line for line in log.read_text().split() if line != 'close']


def test_an_idle_archive_exit_is_restarted_before_the_next_frame(shared, tmp_path):
    from kelmo.server import ArchiveProcess
    log = tmp_path / 'frames.log'
    start, started = _echo_archive(tmp_path, log)
    archive = ArchiveProcess(start(), shared.hub.profile.bindings, {'mode': 'confined'}, restart=start)
    try:
        assert archive._call('first') == 'first'
        started[0]['process'].kill()
        started[0]['process'].wait()
        assert archive._call('second') == 'second', 'the operation after an idle exit runs on a new process'
        assert [item['uncertain'] for item in started] == [False, False]
    finally:
        archive.close()
    assert _received(log) == ['first', 'second']


def test_a_frame_that_meets_an_idle_exit_runs_once_on_a_new_process(shared, tmp_path):
    """The worker left idle as the frame was written: its exit code proves it read nothing, so the frame is
    sent again to a new process, which keeps the broker channel."""
    import subprocess
    from kelmo.server import ARCHIVE_IDLE_EXIT, ArchiveProcess, _Pipe
    log = tmp_path / 'frames.log'
    echo, started = _echo_archive(tmp_path, log)
    leaving = subprocess.Popen([sys.executable, '-c', f'import sys, time; time.sleep(0.3); sys.exit({ARCHIVE_IDLE_EXIT})'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    (tmp_path / 'scratch-idle').mkdir()
    archive = ArchiveProcess(_Pipe(leaving, tmp_path / 'scratch-idle'), shared.hub.profile.bindings,
                             {'mode': 'confined'}, restart=echo)
    try:
        assert archive._call('first') == 'first'
        assert [item['uncertain'] for item in started] == [False]
    finally:
        archive.close()
    assert _received(log) == ['first']


def test_a_death_during_a_call_is_never_replayed_and_restarts_uncertain(shared, tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.server import ArchiveProcess
    log = tmp_path / 'frames.log'
    start, started = _echo_archive(tmp_path, log)
    archive = ArchiveProcess(start(), shared.hub.profile.bindings, {'mode': 'confined'}, restart=start)
    try:
        with pytest.raises(KelmoError) as error:
            archive._call('die')
        assert error.value.code == 'E_ARCHIVE_PROCESS'
        assert archive._call('after') == 'after'
        assert [item['uncertain'] for item in started] == [False, True]
    finally:
        archive.close()
    assert _received(log) == ['die', 'after'], 'the frame in flight was never sent again'


def test_an_uncertain_death_withholds_the_broker_channel_for_good(tmp_path, monkeypatch):
    """Synthetic pipes only: the launcher stops passing a channel that may hold half a request, and this
    process's own channel over it is closed. Not a live-broker proof."""
    import test_embedding_contract as embedding
    from kelmo.embeddings import channel_poisoned, shared_channel
    from kelmo.server import embedding_launch
    monkeypatch.setattr('kelmo.embeddings._poisoned', set())
    broker = embedding.FakeBroker(embedding.well_behaved(embedding.broker_space()))
    try:
        monkeypatch.setenv('SYNTHETIC_POISON_FDS', ','.join(str(fd) for fd in broker.fds))
        tokenizer = tmp_path / 'tokenizer.json'
        tokenizer.write_bytes(embedding.TOKENIZER)
        settings = {'provider': 'broker', 'fds_env': 'SYNTHETIC_POISON_FDS', 'tokenizer': str(tokenizer)}
        assert embedding_launch(settings)[3] == tuple(broker.fds)
        _, _, _, fds, env = embedding_launch(settings, poison=True)
        assert fds == () and env == {}
        assert embedding_launch(settings)[3] == (), 'a later restart stays without the channel'
        assert channel_poisoned(*broker.fds) and shared_channel(*broker.fds).closed is True
    finally:
        broker.close()


def test_a_busy_archive_database_is_retryable_contention(tmp_path):
    from kelmo.errors import KelmoError
    from kelmo.server import ArchiveWorker, _retryable

    class Locked:
        def counts(self):
            raise sqlite3.OperationalError('database is locked')

        def hidden_at(self, archive_id):
            raise sqlite3.OperationalError('no such table: memories')

    worker = ArchiveWorker(Locked(), None, tmp_path, confined=False)
    busy = worker.handle({'method': 'counts', 'args': {}})['error']
    assert busy['code'] == 'E_BUSY' and _retryable(KelmoError(busy['code'], busy['message'])) is True
    broken = worker.handle({'method': 'hidden_at', 'args': {'archive_id': 'x'}})['error']
    assert broken['code'] == 'E_STATE' and _retryable(KelmoError(broken['code'], broken['message'])) is False


def _capture_with_vectors(shared, content, key):
    from kelmo.client import ClientService
    shared.hub.reset(shared.profile(archive_process='confined'))
    client = ClientService(shared.client('mac-a'))
    captured = client.add(content, key + '.md', role='implementer', idempotency_key=key)
    shared.hub.session('mac-a').service.run(captured['operation']['id'])
    captured = client.wait(captured, timeout=1)
    assert captured['operation']['durable_stage'] == 'archived', captured
    archive = shared.hub.session('mac-a').archive
    assert archive.description['confined'] is True
    record = archive.get(captured['operation']['archive_id'])
    assert record.vectors_pending is False, 'the restricted worker must actually produce vectors'
    return archive, captured['operation']['archive_id']


@pytest.mark.skipif(not NATIVE or not os.environ.get('KELMO_NATIVE_MODEL_DIR'),
                    reason='Native gate: KELMO_NATIVE_ISOLATION=1 and KELMO_NATIVE_MODEL_DIR set to a '
                           'provisioned pinned model; a skip is incomplete.')
def test_native_restricted_archive_loads_the_hash_pinned_local_model(shared):
    from kelmo.contracts import SearchFilters
    from kelmo.model_store import MODEL
    from test_retrieval_integration import set_embedding
    model_dir = os.environ['KELMO_NATIVE_MODEL_DIR']
    set_embedding(shared.world.root, '[embedding]\nprovider = "local"\n'
                  + f'model = {json.dumps(MODEL)}\nmodel_dir = {json.dumps(model_dir)}\n')
    archive, archive_id = _capture_with_vectors(
        shared, 'The integration build failed because the test process timed out.', 'local-provider')
    found = archive.search(['integration build timed out'], SearchFilters(), 1, 'semantic')
    assert found.degraded is False and found.hits[0].evidence.archive_id == archive_id
    assert archive.expand(found.hits[0].chunks[0], 1).chunks


@pytest.mark.skipif(not NATIVE, reason='Native gate: KELMO_NATIVE_ISOLATION=1; a skip is incomplete. '
                                       'The broker is the synthetic FakeBroker, not a live one.')
def test_native_restricted_archive_uses_inherited_broker_pipes(shared, tmp_path, monkeypatch):
    import test_embedding_contract as embedding
    from kelmo.contracts import SearchFilters
    from test_retrieval_integration import set_embedding
    tokenizer = tmp_path / 'tokenizer.json'
    tokenizer.write_bytes(embedding.TOKENIZER)
    broker = embedding.FakeBroker(embedding.well_behaved(embedding.broker_space()))
    monkeypatch.setenv('SYNTHETIC_EMBED_FDS', ','.join(str(fd) for fd in broker.fds))
    settings = embedding.broker_section().replace('tokenizer = "/t.json"',
                                                  'tokenizer = ' + json.dumps(str(tokenizer)))
    set_embedding(shared.world.root, settings)
    try:
        archive, archive_id = _capture_with_vectors(shared, 'alpha beta gamma', 'broker-provider')
        found = archive.search(['alpha beta'], SearchFilters(), 1, 'semantic')
        assert found.degraded is False and found.hits[0].evidence.archive_id == archive_id
        kinds = {request['kind'] for request in broker.requests}
        assert {'health', 'passage', 'query'} <= kinds
    finally:
        shared.hub.close()
        broker.close()


@pytest.mark.skipif(not NATIVE, reason='Native confinement gate: run by the controller with '
                                       'KELMO_NATIVE_ISOLATION=1 on macOS and Linux; a skip is incomplete.')
def test_native_archive_process_cannot_read_vault_git_or_credentials(shared, tmp_path):
    from kelmo.client import ClientService
    from kelmo.config import Config
    from kelmo.server import ClientSession
    config = Config.load(shared.world.root)
    import subprocess
    from kelmo.server import _readable
    credential = tmp_path / 'credentials' / 'token'
    credential.parent.mkdir()
    credential.write_text('synthetic-secret\n')
    # A real loose object in the knowledge repository, written for this check.
    git = config.knowledge_git_root
    digest = subprocess.run(['git', '-C', str(git), 'hash-object', '-w', '--stdin'], input=b'synthetic canary\n',
                            capture_output=True, check=True).stdout.decode().strip()
    git_object = git / '.git/objects' / digest[:2] / digest[2:]
    shared.hub.reset(shared.profile(archive_process='confined'))
    session = shared.hub.session('mac-a')
    assert session.archive.description['confined'] is True
    client = shared.client('mac-a')
    captured = ClientService(client).add('Archived under confinement.', 'confined.md', role='implementer',
                                         idempotency_key='confined')
    session.service.run(captured['operation']['id'])
    captured = ClientService(client).wait(captured, timeout=1)
    assert captured['operation']['durable_stage'] == 'archived' and captured['evidence_ref']['version'] == 1
    denied = [config.state / 'journal.sqlite3', config.registry, config.root / 'kelmo.toml',
              git / '.git/HEAD', git_object, config.wiki / 'person.md', credential]
    # Positive control: every canary exists and this unconfined test process can read it, so a
    # refusal below is the boundary, never a wrong path.
    for path in denied:
        assert path.is_file() and _readable(str(path)), path
    probe = session.archive.probe([*denied, config.archive])
    assert probe['confined'] is True and probe['network'] == 'denied'
    for path in denied:
        # macOS refuses (denied); a Linux namespace never mounts it (missing). Both are confinement here.
        assert probe['paths'][str(path)] in ('denied', 'missing'), path
    assert probe['paths'][str(config.archive)] == 'readable'
    graph = ClientService(client).graph_rebuild()
    assert graph['graph']['status'] == 'current'
    assert ClientService(client).graph_query(action='search', query='person')['graph']['status'] == 'current'
    restarted = ClientSession(shared.world.root, shared.hub.profile, 'mac-b')
    try:
        assert restarted.archive.description['confined'] is True
    finally:
        restarted.close()
