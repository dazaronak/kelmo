"""Shared-client context delivery through the real hook entry point, server session and cache.

The channel between client and server is in process here (Loopback) but every frame still
crosses the bounded JSON framing; test_client_outbox exercises the real stdio channel. The
server side is the product ClientSession over the real journal, archive and publisher.
"""

import io
import json
import subprocess
import sys

import pytest

from test_action_state import APP_PAGE, WEB_PAGE, TOPIC_PAGE, world_factory as world_factory  # noqa: F401


class Loopback:
    """The channel a trusted launcher opens, in process: frames still cross JSON encoding."""

    def __init__(self, hub, client_id):
        self.hub, self.client_id = hub, client_id

    def request(self, frame):
        from kelmo.errors import KelmoError
        from kelmo.server import decode_frame, encode_frame
        if not self.hub.online:
            raise KelmoError('E_TRANSPORT', 'Synthetic network outage.')
        request = decode_frame(encode_frame(frame))
        self.hub.requests.append((self.client_id, request))
        reply = decode_frame(encode_frame(self.hub.session(self.client_id).handle(request)))
        if self.hub.drop_replies and 'idempotency_key' in request:
            self.hub.drop_replies -= 1
            raise KelmoError('E_TRANSPORT', 'Synthetic reply loss after the server acted.')
        return reply

    def close(self):
        pass


class Hub:
    """One server vault with one ClientSession per enrolled client, as separate channels would have."""

    def __init__(self, root, profile):
        self.root, self.profile = root, profile
        self.online, self.drop_replies, self.requests, self.sessions = True, 0, [], {}

    def session(self, client_id):
        from kelmo.server import ClientSession
        if client_id not in self.sessions:
            self.sessions[client_id] = ClientSession(self.root, self.profile, client_id)
        return self.sessions[client_id]

    def factory(self, client_id):
        from kelmo.errors import KelmoError

        def open_channel():
            if not self.online:
                raise KelmoError('E_TRANSPORT', 'Synthetic network outage.')
            return Loopback(self, client_id)
        return open_channel

    def reset(self, profile=None):
        self.close()
        self.profile = profile or self.profile

    def close(self):
        for session in self.sessions.values():
            session.close()
        self.sessions = {}


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(path, remote):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-q', '-b', 'main')
    git(path, 'remote', 'add', 'origin', remote)
    git(path, 'commit', '-q', '--allow-empty', '-m', 'init')
    return path


def server_config(root, pages=(), projects=None, *, max_bytes=None, sessions=None):
    """The server's own HOT and session selection; earlier [hot]/[sessions] blocks are replaced."""
    path = root / 'kelmo.toml'
    text = path.read_text()
    for marker in ('\n[hot]', '\n[sessions]'):
        text = text.split(marker)[0]
    text = text.rstrip('\n') + '\n'
    if sessions is not None:
        text += '\n[sessions]\nmode = ' + json.dumps(sessions) + '\n'
    text += '\n[hot]\npages = ' + json.dumps(list(pages)) + '\n'
    if max_bytes is not None:
        text += f'max_bytes = {max_bytes}\n'
    for name, (roots, selected) in (projects or {}).items():
        text += (f'\n[hot.projects.{name}]\nroots = ' + json.dumps([str(item) for item in roots])
                 + '\npages = ' + json.dumps(selected) + '\n')
    path.write_text(text)


def profile_value(world, tmp_path, **overrides):
    repos = ['repo-app', 'repo-web']
    value = {
        'schema': 'kelmo-server/1', 'authority_id': world.bindings.authority_id,
        'writer': {'writer_id': 'local', 'epoch': 1}, 'archive_process': 'in-process',
        'clients': [{'client_id': 'mac-a', 'machine_tag': 'mac-a', 'repository_ids': repos},
                    {'client_id': 'mac-b', 'machine_tag': 'mac-b', 'repository_ids': repos},
                    {'client_id': 'requester-client', 'machine_tag': 'mac-a', 'requester': True,
                     'repository_ids': repos},
                    {'client_id': 'outsider', 'machine_tag': 'elsewhere'}],
        'repositories': [{'repository_id': 'repo-app', 'root': str((tmp_path / 'app').resolve()),
                          'remote': 'https://example.invalid/repo-app.git', 'project': 'app'},
                         {'repository_id': 'repo-web', 'root': str((tmp_path / 'web').resolve()),
                          'remote': 'https://example.invalid/repo-web.git', 'project': 'web'}],
        'actions': list(world.identities.values()),
        'feature_pages': {'shared-feature': [APP_PAGE, WEB_PAGE]},
    }
    value.update(overrides)
    return value


class Shared:
    def __init__(self, world, tmp_path):
        from kelmo.server import ServerProfile
        self.world, self.tmp_path = world, tmp_path
        self.app = repository(tmp_path / 'app', 'https://example.invalid/repo-app.git')
        self.web = repository(tmp_path / 'web', 'https://example.invalid/repo-web.git')
        server_config(world.root, [], {'app': ([self.app], [APP_PAGE]), 'web': ([self.web], [WEB_PAGE])})
        self.hub = Hub(world.root, ServerProfile.from_dict(profile_value(world, tmp_path)))
        self.clients = []

    def profile(self, **overrides):
        from kelmo.server import ServerProfile
        return ServerProfile.from_dict(profile_value(self.world, self.tmp_path, **overrides))

    def client(self, client_id='mac-a', root=None, authority=None, sessions=None):
        from kelmo.client import SharedClient, prepare_profile
        from kelmo.config import ClientProfile
        root = root or self.tmp_path / ('client-' + client_id)
        if not (root / 'client.toml').exists():
            prepare_profile(root, authority or self.world.bindings.authority_id, ['/bin/false'],
                            sessions=sessions)
        client = SharedClient(ClientProfile.load(root), channel_factory=self.hub.factory(client_id))
        self.clients.append(client)
        return client

    def close(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a test may already have closed it.
                pass
        self.hub.close()


@pytest.fixture
def shared(world_factory, tmp_path):
    fixture = Shared(world_factory(), tmp_path)
    yield fixture
    fixture.close()


def local_start(world, cwd=None):
    from kelmo.integration import hook
    return hook(world.service, 'start', {} if cwd is None else {'cwd': str(cwd)})['hookSpecificOutput']['additionalContext']


def client_start(client, cwd=None):
    from kelmo.client import hook
    output = hook(client, 'start', {} if cwd is None else {'cwd': str(cwd)})['hookSpecificOutput']
    assert output['hookEventName'] == 'SessionStart'
    return output['additionalContext']


def publish_push(world, key='complete-push'):
    ref = world.archive('I pushed the shared feature.', key=key)
    return world.state.apply(world.update(ref), world.origins['requester'], key)


def test_two_clients_read_same_generation(shared):
    first, second = shared.client('mac-a'), shared.client('mac-b')
    a, b = first.context(str(shared.app)), second.context(str(shared.app))
    generation = shared.world.service.knowledge.generation()
    assert a['generation'] == b['generation'] == generation.to_dict()
    assert a['freshness'] == b['freshness'] == 'current' and a['complete'] is True
    assert a['context'] == b['context'] == local_start(shared.world, shared.app)
    assert f'## {APP_PAGE}' in a['context'] and '## person.md' in a['context']
    assert a['local_binding']['repository'] == 'example.invalid/repo-app'


def test_compact_resume_refreshes_current_state(shared):
    client = shared.client()
    before = client_start(client, shared.app)
    assert '[a:push-app]' not in before
    receipt = publish_push(shared.world)
    # A compact or resume fires SessionStart again; it reads the new generation, not the cache.
    after = client_start(client, shared.app)
    assert f'generation {receipt.generation.publication_id} ' in after.split('\n\n', 1)[0]
    assert 'current, complete' in after.split('\n\n', 1)[0]
    assert '[a:push-app]' in after
    assert after == local_start(shared.world, shared.app)


def test_stale_cache_is_explicit_and_never_current_proof(shared):
    from kelmo.errors import KelmoError
    client = shared.client()
    current = client.context(str(shared.app))
    shared.hub.online = False
    stale = client.context(str(shared.app))
    header = stale['context'].split('\n\n', 1)[0]
    assert header.startswith('Kelmo HOT STALE CACHE (E_TRANSPORT)')
    assert 'not current' in header and 'current, complete' not in stale['context']
    assert stale['freshness'] == 'stale' and stale['complete'] is False
    assert stale['generation'] == current['generation'] and stale['cache']['reason'] == 'E_TRANSPORT'
    # Person content stays whole in the stale copy, byte for byte.
    person = shared.world.service.knowledge.page('person.md', shared.world.service.knowledge.generation())
    assert '## person.md\n\n' + person.content.decode('utf-8') in stale['context']
    assert client_start(client, shared.app).startswith('Kelmo HOT STALE CACHE')

    # A stale cache is not a writer: the write is queued, not accepted.
    operations = shared.world.service.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
    queued = client.submit('capture', {'content': 'Offline note.', 'filename': 'offline.md',
                                       'role': 'collector'}, 'offline-note')
    assert queued['queued'] is True and queued['accepted'] is False and queued['pending_reason'] == 'E_TRANSPORT'
    assert shared.world.service.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == operations

    cache = next((client.profile.state / 'cache').glob('context-*.json'))
    record = json.loads(cache.read_text())
    record['pages'][0]['text'] += 'TAMPERED'
    cache.write_text(json.dumps(record))
    with pytest.raises(KelmoError) as error:
        client.context(str(shared.app))
    assert error.value.code == 'E_CACHE'
    refused = client_start(client, shared.app)
    assert refused.startswith('Kelmo HOT unavailable (E_CACHE)') and 'TAMPERED' not in refused


def test_no_cache_and_no_server_delivers_nothing(shared):
    client = shared.client()
    shared.hub.online = False
    context = client_start(client, shared.app)
    assert context.startswith('Kelmo HOT unavailable (E_TRANSPORT)') and '## person.md' not in context


@pytest.mark.parametrize('cached', [True, False])
def test_missing_context_capability_does_not_claim_the_server_is_unreachable(shared, cached):
    from kelmo.errors import KelmoError
    client = shared.client()
    if cached:
        client.context(str(shared.app))
    else:
        client.call('hello')
    client.server = dict(client.server, operations=[op for op in client.server['operations'] if op != 'context'])
    if cached:
        result = client.context(str(shared.app))
        assert result['freshness'] == 'stale' and result['complete'] is False
        assert result['cache']['reason'] == 'E_CAPABILITY'
        message = result['context'].split('\n\n', 1)[0]
    else:
        with pytest.raises(KelmoError) as error:
            client.context(str(shared.app))
        assert error.value.code == 'E_CAPABILITY'
        message = str(error.value)
    assert 'does not advertise' in message
    assert 'unreachable' not in message and 'could not be reached' not in message
    assert client._channel is not None


def test_worktree_resolves_bound_work(shared):
    client = shared.client()
    plain = client.context(str(shared.app))
    assert f'## {TOPIC_PAGE}' not in plain['context']
    from pathlib import Path
    common = str(Path(git(shared.app, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve())
    gitdir = str(Path(git(shared.app, 'rev-parse', '--absolute-git-dir')).resolve())
    binding = {'version': 2, 'gitdir': gitdir, 'common_dir': common, 'branch': 'main',
               'project_id': 'app', 'work': 'design-review', 'state': 'bound'}
    (shared.app / '.git/hot-work-binding.json').write_text(json.dumps(binding))
    bound = client.context(str(shared.app))
    assert bound['work_status'] == 'bound' and bound['local_binding']['binding_status'] == 'bound'
    assert f'## {TOPIC_PAGE}' in bound['context'] and f'## {APP_PAGE}' in bound['context']

    # A second worktree of the same repository has its own binding, here none.
    other = shared.tmp_path / 'app-other'
    git(shared.app, 'worktree', 'add', '-q', '-b', 'other', str(other))
    unbound = client.context(str(other))
    assert unbound['local_binding']['binding_status'] == 'unbound'
    assert unbound['project'] == 'app' and f'## {TOPIC_PAGE}' not in unbound['context']

    git(shared.app, 'checkout', '-q', '-b', 'moved')
    stale = client.context(str(shared.app))
    assert stale['local_binding']['binding_status'] == 'stale-branch' and f'## {TOPIC_PAGE}' not in stale['context']

    binding['project_id'], binding['branch'] = 'web', 'moved'
    (shared.app / '.git/hot-work-binding.json').write_text(json.dumps(binding))
    mismatched = client.context(str(shared.app))
    assert mismatched['work_status'] == 'stale-project' and f'## {TOPIC_PAGE}' not in mismatched['context']


def test_repository_switch_selects_its_own_map(shared):
    client = shared.client()
    app, web = client.context(str(shared.app)), client.context(str(shared.web))
    assert app['project'] == 'app' and web['project'] == 'web'
    assert f'## {APP_PAGE}' in app['context'] and f'## {APP_PAGE}' not in web['context']
    assert f'## {WEB_PAGE}' in web['context'] and app['generation'] == web['generation']
    for result in (app, web):
        assert result['mandatory'] == ['person.md'] and '## person.md' in result['context']
    assert web['context'] == local_start(shared.world, shared.web)


def test_unenrolled_repository_gets_global_context_only(shared):
    client = shared.client()
    unknown = repository(shared.tmp_path / 'unknown', 'https://example.invalid/unknown.git')
    result = client.context(str(unknown))
    assert result['binding_status'] == 'unenrolled-repository' and result['project'] is None
    assert f'## {APP_PAGE}' not in result['context'] and '## person.md' in result['context']


def test_repository_outside_client_enrollment_is_refused(shared):
    from kelmo.errors import KelmoError
    client = shared.client('outsider')
    with pytest.raises(KelmoError) as error:
        client.context(str(shared.app))
    assert error.value.code == 'E_BINDING'


def test_mandatory_overflow_is_incomplete_not_replaced_by_cache(shared):
    from kelmo.errors import KelmoError
    client = shared.client()
    client.context(str(shared.app))
    server_config(shared.world.root, [], {'app': ([shared.app], [APP_PAGE]), 'web': ([shared.web], [WEB_PAGE])},
                  max_bytes=64)
    shared.hub.reset()
    with pytest.raises(KelmoError) as error:
        client.context(str(shared.app))
    assert error.value.code == 'E_BUDGET'
    context = client_start(client, shared.app)
    assert context.startswith('Kelmo HOT incomplete (E_BUDGET)') and '## person.md' not in context


def test_missing_profile_is_reported_not_assumed(shared, tmp_path, monkeypatch, capsys):
    from kelmo import cli
    from kelmo.config import ClientProfile
    from kelmo.errors import KelmoError
    from kelmo.server import ClientSession
    empty = tmp_path / 'no-profile'
    empty.mkdir()
    assert ClientProfile.selected(empty) is False
    with pytest.raises(KelmoError) as error:
        ClientProfile.load(empty)
    assert error.value.code == 'E_NOT_INITIALIZED'
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    assert cli.call(['client', 'status', '--vault', str(empty), '--json']) != 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_NOT_INITIALIZED'
    for client_id in ('nobody', 'bad/id'):
        with pytest.raises(KelmoError) as error:
            ClientSession(shared.world.root, shared.hub.profile, client_id)
        assert error.value.code == 'E_ORIGIN'


def test_foreign_authority_is_refused_and_cache_is_labelled(shared):
    import uuid
    from kelmo.errors import KelmoError
    pinned = shared.client()
    pinned.context(str(shared.app))
    cache = next((pinned.profile.state / 'cache').glob('context-*.json'))
    foreign_root = shared.tmp_path / 'client-foreign'
    foreign = shared.client('mac-a', root=foreign_root, authority=str(uuid.uuid4()))
    with pytest.raises(KelmoError) as error:
        foreign.call('status')
    assert error.value.code == 'E_AUTHORITY'
    (foreign.profile.state / 'cache').mkdir(parents=True, exist_ok=True)
    # Another authority's cache can never be presented for this client.
    (foreign.profile.state / 'cache' / cache.name).write_text(cache.read_text())
    with pytest.raises(KelmoError) as error:
        foreign.context(str(shared.app))
    assert error.value.code == 'E_CACHE'


def test_cli_hook_start_runs_through_the_shared_client(shared, monkeypatch, capsys):
    """The installed `hook start --vault <client root>` entry point, against a real stdio server."""
    from kelmo import cli
    from kelmo.client import prepare_profile
    import kelmo
    profile = shared.tmp_path / 'server-profile.json'
    profile.write_text(json.dumps(profile_value(shared.world, shared.tmp_path)))
    source = str(__import__('pathlib').Path(kelmo.__file__).resolve().parents[1])
    boot = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kelmo.cli import main; raise SystemExit(main(sys.argv[1:]))'
    root = shared.tmp_path / 'client-cli'
    prepare_profile(root, shared.world.bindings.authority_id,
                    [sys.executable, '-c', boot, source, 'serve-client', '--profile', str(profile),
                     '--client-id', 'mac-a', '--vault', str(shared.world.root)])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'cwd': str(shared.app)})))
    assert cli.call(['hook', 'start', '--vault', str(root)]) == 0
    delivered = json.loads(capsys.readouterr().out)['hookSpecificOutput']['additionalContext']
    assert delivered == local_start(shared.world, shared.app)


def test_stale_cache_is_sealed_to_the_whole_delivered_context(shared):
    """Each page hash holding is not enough: a dropped, reordered or relabelled page, a changed mandatory
    set or an older schema is refused."""
    from kelmo.errors import KelmoError
    client = shared.client()
    client.context(str(shared.app))
    cache = next((client.profile.state / 'cache').glob('context-*.json'))
    original = json.loads(cache.read_text())
    assert original['schema'] == 2 and [page['path'] for page in original['pages']] == ['person.md', APP_PAGE]
    shared.hub.online = False
    tampers = [lambda record: record['pages'].pop(),
               lambda record: record['pages'].reverse(),
               lambda record: record.update(mandatory=[]),
               lambda record: record.update(mandatory=['person.md', APP_PAGE]),
               lambda record: record.update(schema=1)]
    for tamper in tampers:
        record = json.loads(json.dumps(original))
        tamper(record)
        cache.write_text(json.dumps(record))
        with pytest.raises(KelmoError) as error:
            client.context(str(shared.app))
        assert error.value.code == 'E_CACHE'
    cache.write_text(json.dumps(original))
    assert client.context(str(shared.app))['freshness'] == 'stale'


def test_shared_background_work_keeps_its_origins(shared):
    """Under a server profile: a worker has no capture origin of its own, a session note belongs to the
    client that captured the session, and nothing falls back to a local identity."""
    from kelmo.errors import KelmoError
    from kelmo.server import shared_service
    profile = shared.profile()
    with shared_service(shared.world.root, profile) as background:
        assert background.origin is None and background.archive_api.description['mode'] == 'in-process'
        with pytest.raises(KelmoError) as error:
            background.capture('No origin of its own.', 'no-origin.md', 'implementer')
        assert error.value.code == 'E_ORIGIN'
    with shared_service(shared.world.root, profile, 'mac-b') as note:
        operation = note.capture('Session note.', 'session-note.md', role='lead', idempotency_key='session-note:x')
        assert note.journal.origin_record(operation['id'])['client_id'] == 'mac-b'
    with pytest.raises(KelmoError) as error:
        shared_service(shared.world.root, profile, 'nobody')
    assert error.value.code == 'E_ORIGIN'
    with pytest.raises(KelmoError) as error:
        shared_service(shared.world.root, shared.profile(fenced=True))
    assert error.value.code == 'E_FENCED'
