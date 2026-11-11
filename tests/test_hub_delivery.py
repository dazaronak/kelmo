import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from kelmo.hub_delivery import assemble, install_set, read_set, refresh, published_set
from kelmo.errors import KelmoError


def snapshot(generation, revision, projects=('alpha',)):
    files = {'mandatory.md': f'<!-- kelmo generation {generation} -->\nmandatory'}
    for project in projects:
        files[f'{project}.md'] = f'<!-- kelmo generation {generation} -->\nhub {project}'
        files[f'{project}.full.md'] = f'<!-- kelmo generation {generation} -->\nmandatory\nhub {project}\nfull {project}'
    return {'generation': generation, 'enrollment_revision': revision, 'manifest_sha256': 'a' * 64,
            'projects': list(projects), 'files': {name: {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
                                                 for name, text in files.items()}}


def test_atomic_set_rejects_incomplete_or_older_without_replacing_current(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(2, 3))
    broken = snapshot(3, 3)
    del broken['files']['alpha.full.md']
    with pytest.raises(KelmoError):
        install_set(root, broken)
    with pytest.raises(KelmoError):
        install_set(root, snapshot(1, 9))
    assert read_set(root)['generation'] == 2
    assert (root / 'current' / 'alpha.md').read_text().endswith('hub alpha')


def test_corrupted_cache_is_not_delivered_and_same_revision_cannot_change_contents(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(2, 3))
    changed = snapshot(2, 3, ('beta',))
    with pytest.raises(KelmoError):
        install_set(root, changed)
    (root / 'current' / 'alpha.md').write_text('corrupted')
    with pytest.raises(KelmoError):
        read_set(root)


def test_cache_metadata_cannot_read_a_path_outside_the_set(tmp_path):
    root = tmp_path / 'hot'
    install_set(root, snapshot(2, 3))
    metadata_path = root / 'current' / 'set.json'
    metadata = json.loads(metadata_path.read_text())
    metadata['files']['../outside.md'] = 'a' * 64
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(KelmoError):
        read_set(root)


def test_budget_includes_pending_and_never_splits_mandatory():
    pending = [{'pending': f'line {i}', 'project': 'alpha'} for i in range(30)]
    result = assemble('header', 'M' * 500, 'H' * 9800, 'work', pending, provider='claude',
                      pointer='/tmp/hot/current/alpha.md')
    assert len(result) <= 10000
    assert 'M' * 500 in result
    assert '/tmp/hot/current/alpha.md' in result
    assert '+20 more' in result


def test_pending_digest_changes_even_when_only_an_omitted_row_changes():
    pending = [{'pending': f'line {i}', 'project': 'alpha'} for i in range(11)]
    before = assemble('header', 'mandatory', 'hub', '', pending, provider='codex', pointer='hub')
    pending[-1]['pending'] = 'changed outside visible rows'
    after = assemble('header', 'mandatory', 'hub', '', pending, provider='codex', pointer='hub')
    assert before != after
    assert 'changed outside visible rows' not in after


def test_mandatory_over_budget_is_named_whole_instead_of_truncated():
    pending = [{'pending': 'STILL_SHOWN', 'project': 'alpha'}]
    result = assemble('header', 'M' * 10000, 'hub', '', pending, provider='claude', pointer='alpha.md',
                      mandatory_pointer='mandatory.md')
    assert len(result) <= 10000 and 'MM' not in result
    assert 'not repeated here' in result and 'mandatory.md' in result and 'STILL_SHOWN' in result


def test_refuses_only_when_even_the_omission_notice_cannot_fit():
    with pytest.raises(KelmoError) as failure:
        assemble('header', 'M' * 10000, 'hub', '', [], provider='claude', pointer='p' * 10000)
    assert failure.value.code == 'E_BUDGET'


def test_refresh_retries_until_output_confirmed_and_never_suppresses_stale(tmp_path):
    state = tmp_path / 'session.json'
    first = refresh(state, 'bundle', verified=True)
    assert first['context'] == 'bundle'
    assert refresh(state, 'bundle', verified=True)['context'] == 'bundle'
    first['commit']()
    assert refresh(state, 'bundle', verified=True)['context'] == ''
    assert refresh(state, 'bundle', verified=False)['context'] == 'bundle'
    assert refresh(state, 'bundle', verified=True, stale=True)['context'] == 'bundle'
    assert refresh(state, 'bundle', verified=True, compacted=True)['context'] == 'bundle'
    assert 'restart' in refresh(state, 'changed', verified=True)['context'].lower()


def test_local_prompt_refresh_reads_new_content_without_a_database(tmp_path):
    from kelmo.integration import hook
    root = tmp_path / 'vault'
    wiki = root / 'knowledge/wiki'
    wiki.mkdir(parents=True)
    (root / 'kelmo.toml').write_text('schema_version = 1\nwriter = "local"\n'
        'vault_id = "00000000-0000-0000-0000-000000000001"\nnode_label = "test"\n'
        '[hot]\npages = ["global.md"]\n')
    page = wiki / 'global.md'
    page.write_text('before')
    service = SimpleNamespace(root=root)
    hook(service, 'start', {})
    page.write_text('after')
    result = hook(service, 'prompt', {})['hookSpecificOutput']
    assert result['hookEventName'] == 'UserPromptSubmit'
    assert 'after' in result['additionalContext']


def test_published_set_reads_every_project_from_one_pinned_generation(tmp_path):
    generation = SimpleNamespace(publication_id=7, manifest_sha256='b' * 64)
    class Reader:
        wiki_path = 'knowledge/wiki'
        def _pinned(self, selected):
            assert selected is generation
            return {'entries': {}}
        def _blob(self, entries, path):
            return (tmp_path / path).read_bytes()
    for name in ('mandatory.md', 'alpha.md', 'alpha.full.md', 'beta.md', 'beta.full.md'):
        path = tmp_path / 'knowledge/hot' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'<!-- kelmo generation 7 -->\n{name}')
    result = published_set(Reader(), generation, ['beta', 'alpha'], 4)
    install_set(tmp_path / 'cache', result)
    assert read_set(tmp_path / 'cache')['files']['beta.md']['text'].endswith('beta.md')
    (tmp_path / 'knowledge/hot/beta.md').unlink()
    with pytest.raises(KelmoError):
        published_set(Reader(), generation, ['alpha', 'beta'], 4)


def test_shared_hook_delivers_real_set_and_acknowledges_after_output(tmp_path):
    from contextlib import contextmanager
    from kelmo.client import hook
    class Client:
        profile = SimpleNamespace(state=tmp_path)
        _deadline = None
        _channel = object()
        files = threading.RLock()
        @contextmanager
        def deadline(self, seconds):
            import time
            self._deadline = time.monotonic() + seconds
            yield
        def flush(self):
            return {'reached': True, 'stopped': None}
        def context(self, cwd, **kwargs):
            return {'hub_set': snapshot(2, 1), 'project': 'alpha', 'hub_work': 'bound work',
                    'pending': [{'pending': 'pending row', 'project': 'alpha'}], 'freshness': 'current'}
    callbacks = []
    result = hook(Client(), 'prompt', {'session_id': 'x'}, 'codex', acknowledge=callbacks.append)
    context = result['hookSpecificOutput']['additionalContext']
    assert 'hub alpha' in context and 'bound work' in context and 'pending row' in context
    assert not list(tmp_path.glob('hub-sessions/*'))
    callbacks[0]()
    assert list(tmp_path.glob('hub-sessions/*'))


def test_shared_context_installs_complete_set_and_uses_it_offline(tmp_path):
    from kelmo.client import SharedClient, Unbound
    authority = '00000000-0000-0000-0000-000000000001'
    class Client(SharedClient):
        def __init__(self):
            self.profile = SimpleNamespace(state=tmp_path, authority_id=authority)
            self._deadline = None
            self.generation = 2
        def call(self, operation, args):
            header = f'Kelmo knowledge generation {self.generation} (manifest aaaaaaaaaaaa): current, complete; mandatory pages: person.md.'
            context = header + '\n\n## person.md\n\nmandatory'
            return {'configured': True, 'generation': {'authority_id': authority, 'publication_id': self.generation,
                    'manifest_sha256': 'a' * 64}, 'context': context,
                    'context_sha256': hashlib.sha256(context.encode()).hexdigest(),
                    'pages': [{'path': 'person.md', 'bytes': 9,
                               'sha256': hashlib.sha256(b'mandatory').hexdigest()}],
                    'mandatory': ['person.md'], 'project': 'alpha', 'hub_set': snapshot(self.generation, 1),
                    'hub_work': 'bound work', 'pending': [{'pending': 'queued'}], 'freshness': 'current'}
    binding = {'repository': 'repo', 'work': 'task', 'work_project': 'alpha', 'branch': 'feature',
               'binding_status': 'bound'}
    client = Client()
    online = client.context(binding=binding)
    assert (tmp_path / 'hot/current/alpha.md').read_text().endswith('hub alpha')
    client.generation = 3
    client.context(binding=dict(binding, work='other-task'))
    from pathlib import Path
    assert Path(online['hub_directory']).name != 'current'
    assert 'generation 2' in (Path(online['hub_directory']) / 'alpha.md').read_text()
    offline = client.context(binding=binding, unreachable=Unbound('E_TRANSPORT', 'offline'))
    assert offline['freshness'] == 'stale'
    assert offline['hub_set']['files']['alpha.md']['text'].endswith('hub alpha')
    assert offline['hub_work'] == 'bound work' and offline['pending'][0]['pending'] == 'queued'
    assert offline['hub_set']['generation'] == 2
    from pathlib import Path
    cached = Path(offline['hub_work_pointer'].split(' (', 1)[0])
    assert json.loads(cached.read_text())['hub_work'] == offline['hub_work']
    (tmp_path / 'hot/current/alpha.md').write_text('damaged newer set')
    assert client.context(binding=binding, unreachable=Unbound('E_TRANSPORT', 'offline'))['hub_set']['generation'] == 2
    install_set(tmp_path / 'hot', snapshot(3, 2, ()))
    with pytest.raises(KelmoError):
        client.context(binding=binding, unreachable=Unbound('E_TRANSPORT', 'offline'))
    for path in (tmp_path / 'cache').glob('context-*.json'):
        record = json.loads(path.read_text())
        if record.get('hub_generation') == 2:
            record = {key: value for key, value in record.items() if not key.startswith('hub_')}
            path.write_text(json.dumps(record))
    with pytest.raises(KelmoError):
        client.context(binding=binding, unreachable=Unbound('E_TRANSPORT', 'offline'))


def test_embed_refresh_reinjects_changes_and_stale_does_not_overwrite(tmp_path):
    from kelmo.hub_delivery import prepare
    from kelmo.hub_embed import select
    config = SimpleNamespace(state=tmp_path / 'state')
    target = tmp_path / 'AGENTS.md'
    target.write_text('outside\n')
    current = tmp_path / 'hot/current'
    select(config, 'alpha', 'codex', target, current)
    callbacks = []
    args = dict(config=config, payload={'session_id': 'one'}, project='alpha', work='', pending=[],
                provider='codex', current=current, stale=False, acknowledge=callbacks.append)
    first = prepare(snapshot=snapshot(2, 1), **args)
    assert 'hub alpha' in first and 'hub alpha' in target.read_text()
    callbacks.pop()()
    assert prepare(snapshot=snapshot(2, 1), **args) == ''
    callbacks.pop()()
    changed = prepare(snapshot=snapshot(3, 1), **args)
    assert 'generation 3' in changed and 'restart' in changed.lower()
    callbacks.pop()()
    saved = target.read_text()
    args['stale'] = True
    old = prepare(snapshot=snapshot(2, 1), **args)
    assert 'STALE' in old and target.read_text() == saved


def test_server_profile_rejects_invalid_enrollment_revisions():
    from kelmo.server import ServerProfile, PROFILE_SCHEMA, _enrollment_digest
    value = {'schema': PROFILE_SCHEMA, 'authority_id': '00000000-0000-0000-0000-000000000001',
             'writer': {'writer_id': 'local', 'epoch': 1}, 'clients': [], 'repositories': [],
             'enrollment_revision': 4}
    assert ServerProfile.from_dict(value).enrollment_revision == 4
    assert len(_enrollment_digest(ServerProfile.from_dict(value))) == 64
    for invalid in (-1, True, '4'):
        value['enrollment_revision'] = invalid
        with pytest.raises(KelmoError):
            ServerProfile.from_dict(value)


def test_changed_claude_bundle_including_restart_warning_fits_limit(tmp_path):
    from kelmo.hub_delivery import prepare
    config = SimpleNamespace(state=tmp_path)
    items = [{'pending': f'item {i}', 'project': 'alpha'} for i in range(30)]
    callbacks = []
    args = dict(config=config, payload={'session_id': 'one'}, project='alpha', work='', pending=items,
                provider='claude', current=tmp_path / 'hot/current', stale=False, acknowledge=callbacks.append)
    prepare(snapshot=snapshot(2, 1), **args)
    callbacks.pop()()
    newer = snapshot(3, 1)
    text = '<!-- kelmo generation 3 -->\n' + 'H' * 11000
    newer['files']['alpha.md'] = {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    output = prepare(snapshot=newer, **args)
    assert len(output) <= 10000 and 'restart' in output.lower()
    assert '+20 more' in output and 'mandatory' in output


def test_shared_remote_selection_refuses_multiple_project_matches():
    from kelmo.server import ServerProfile, PROFILE_SCHEMA, _select_shared_repository
    from kelmo.context_binding import canonical_remote
    value = {'schema': PROFILE_SCHEMA, 'authority_id': '00000000-0000-0000-0000-000000000001',
             'writer': {'writer_id': 'local', 'epoch': 1}, 'enrollment_revision': 1,
             'clients': [{'client_id': 'test', 'machine_tag': 'synthetic', 'repository_ids': ['a', 'b']}],
             'repositories': [{'repository_id': 'a', 'root': '/private/tmp/a',
                               'remote': 'https://github.com/example/a.git', 'project': 'alpha'},
                              {'repository_id': 'b', 'root': '/private/tmp/b',
                               'remote': 'https://github.com/example/b.git', 'project': 'beta'}]}
    profile = ServerProfile.from_dict(value)
    origin = profile.bindings.origin('test')
    a, b = [canonical_remote('https://github.com/example/' + name + '.git') for name in ('a', 'b')]
    assert _select_shared_repository(profile, origin, a, [a]) == 'a'
    with pytest.raises(KelmoError, match='multiple projects'):
        _select_shared_repository(profile, origin, a, [a, b])


def test_oversized_mandatory_points_to_a_file_that_contains_it(tmp_path):
    from kelmo.hub_delivery import deliver
    value = snapshot(1, 0)
    for name in ('mandatory.md', 'alpha.full.md'):
        content = '<!-- kelmo generation 1 -->\n' + 'M' * 10000
        value['files'][name] = {'text': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
    result = deliver(value, 'alpha', '', [], provider='claude', current=tmp_path)
    assert str(tmp_path / 'mandatory.md') in result and len(result) <= 10000


def test_embed_only_persists_published_context_and_hook_keeps_session_context(tmp_path):
    from kelmo.hub_delivery import prepare
    from kelmo.hub_embed import select
    config = SimpleNamespace(state=tmp_path / 'state')
    target = tmp_path / 'AGENTS.md'
    select(config, 'alpha', 'codex', target, tmp_path / 'hot')
    output = prepare(config, {'session_id': 'one'}, snapshot(2, 1), 'alpha', 'SESSION_WORK',
                     [{'pending': 'PENDING_NOTE', 'project': 'alpha'}], provider='codex',
                     current=tmp_path / 'hot', stale=False, acknowledge=lambda callback: None)
    assert 'SESSION_WORK' in output and 'PENDING_NOTE' in output
    saved = target.read_text()
    assert 'full alpha' in saved and 'SESSION_WORK' not in saved and 'PENDING_NOTE' not in saved


def test_fresh_set_repairs_corrupted_files_without_losing_revision_floor(tmp_path):
    install_set(tmp_path, snapshot(2, 3))
    (tmp_path / 'current/alpha.md').write_text('damaged')
    with pytest.raises(KelmoError):
        install_set(tmp_path, snapshot(1, 3))
    with pytest.raises(KelmoError):
        install_set(tmp_path, snapshot(3, 2))
    install_set(tmp_path, snapshot(3, 3))
    assert read_set(tmp_path)['generation'] == 3
    (tmp_path / 'current/alpha.md').write_text('damaged again')
    install_set(tmp_path, snapshot(3, 3))
    assert read_set(tmp_path)['files']['alpha.md']['text'].endswith('hub alpha')
    (tmp_path / 'current/set.json').write_text('{}')
    with pytest.raises(KelmoError):
        install_set(tmp_path, snapshot(4, 4))


def test_cache_repair_survives_interruption_after_first_mutation(tmp_path, monkeypatch):
    from kelmo import hub_delivery
    install_set(tmp_path, snapshot(2, 3))
    (tmp_path / 'current/alpha.md').write_text('damaged')
    interrupted = []
    def failing(original):
        def invoke(source, destination):
            result = original(source, destination)
            if not interrupted:
                interrupted.append(True)
                raise RuntimeError('interrupted repair')
            return result
        return invoke
    with monkeypatch.context() as scoped:
        scoped.setattr(hub_delivery.os, 'rename', failing(hub_delivery.os.rename))
        scoped.setattr(hub_delivery.os, 'replace', failing(hub_delivery.os.replace))
        with pytest.raises(RuntimeError, match='interrupted repair'):
            install_set(tmp_path, snapshot(2, 3))
    install_set(tmp_path, snapshot(2, 3))
    assert read_set(tmp_path)['files']['alpha.md']['text'].endswith('hub alpha')
    with pytest.raises(KelmoError):
        install_set(tmp_path, snapshot(3, 2))


def test_truncated_work_is_not_acknowledged_or_suppressed(tmp_path, monkeypatch):
    from kelmo import hub_delivery, hub_embed
    monkeypatch.setattr(hub_embed, 'sync', lambda *args, **kwargs: {'verified': True, 'changed': False})
    callbacks = []
    args = dict(config=SimpleNamespace(state=tmp_path), payload={'session_id': 'session'},
                snapshot=snapshot(2, 1), project='alpha', work='WORK_' + 'x' * 12000, pending=[],
                provider='claude', current=tmp_path, stale=False, acknowledge=callbacks.append,
                work_path='projects/alpha/work.md')
    mutable = tmp_path / 'binding-cache.json'
    mutable.write_text('old cache')
    args['work_pointer'] = str(mutable) + ' (hub_work field)'
    first = hub_delivery.prepare(**args)
    assert 'read bound work' in first
    mutable.write_text('new generation work')
    copies = list((tmp_path / 'hub-context').glob('*.json'))
    assert len(copies) == 1 and str(copies[0]) in first and str(mutable) not in first
    assert json.loads(copies[0].read_text())['work'] == args['work']
    for callback in callbacks:
        callback()
    second = hub_delivery.prepare(**args)
    assert 'read bound work' in second
    assert not callbacks


def test_mandatory_without_room_for_the_notices_is_named(tmp_path):
    from kelmo.hub_delivery import deliver
    value = snapshot(1, 0)
    content = '<!-- kelmo generation 1 -->\n' + 'M' * 9740
    value['files']['mandatory.md'] = {'text': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
    result = deliver(value, 'alpha', 'W' * 1000, [], provider='claude', current=tmp_path,
                     work_path='projects/alpha/' + 'long-task-' * 30)
    assert str(tmp_path/'mandatory.md') in result and 'M' * 9740 not in result and len(result) <= 10000


def test_omitted_pending_has_an_immutable_readable_source(tmp_path):
    from pathlib import Path
    from kelmo.hub_delivery import prepare
    value = snapshot(1, 0)
    content = '<!-- kelmo generation 1 -->\n' + 'M' * 8600
    value['files']['mandatory.md'] = {'text': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
    pending = [{'pending': str(index) + '_' + 'P' * 239} for index in range(10)]
    output = prepare(SimpleNamespace(state=tmp_path), {}, value, 'alpha', '', pending,
                     provider='claude', current=tmp_path/'hot', stale=True, acknowledge=None)
    paths = [path for path in tmp_path.rglob('*.json') if json.loads(path.read_text()).get('pending') == pending]
    assert len(paths) == 1 and str(paths[0]) in output
    assert len(output) <= 10000 and 'STALE' in output
    assert json.loads(Path(paths[0]).read_text())['pending'][-1] == pending[-1]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_fitting_bundle_needs_no_omission_storage(tmp_path, monkeypatch, provider):
    from kelmo import hub_delivery
    def unavailable(*args, **kwargs):
        raise OSError('synthetic unavailable context store')
    monkeypatch.setattr(hub_delivery, 'context_reference', unavailable)
    output = hub_delivery.prepare(SimpleNamespace(state=tmp_path), {}, snapshot(1, 0),
        'alpha', 'WHOLE_WORK', [{'pending': 'WHOLE_PENDING'}], provider=provider,
        current=unavailable, stale=False, acknowledge=None)
    assert 'mandatory' in output and 'WHOLE_WORK' in output and 'WHOLE_PENDING' in output


def test_hub_only_omission_does_not_require_pending_storage(tmp_path, monkeypatch):
    from kelmo import hub_delivery
    def unavailable(*args, **kwargs):
        raise OSError('synthetic unavailable context store')
    monkeypatch.setattr(hub_delivery, 'context_reference', unavailable)
    value = snapshot(1, 0)
    text = '<!-- kelmo generation 1 -->\n' + 'H' * 11000
    value['files']['alpha.md'] = {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    directory = install_set(tmp_path / 'hot', value)
    callbacks = []
    output = hub_delivery.prepare(SimpleNamespace(state=tmp_path), {'session_id': 'test'}, value,
        'alpha', '', [{'pending': 'WHOLE_PENDING'}], provider='claude', current=directory,
        stale=False, acknowledge=callbacks.append)
    assert 'mandatory' in output and 'WHOLE_PENDING' in output and str(directory / 'alpha.md') in output
    assert len(output) <= 10000 and not callbacks


@pytest.mark.parametrize('size', [9800, 10100])
def test_oversized_mandatory_keeps_pending_and_is_not_resent_every_prompt(tmp_path, monkeypatch, size):
    from kelmo import hub_delivery, hub_embed
    monkeypatch.setattr(hub_embed, 'sync', lambda *args, **kwargs: {'verified': True, 'changed': False, 'mode': 'hook'})
    value = snapshot(1, 0)
    mandatory = '<!-- kelmo generation 1 -->\n' + 'M' * size
    value['files']['mandatory.md'] = {'text': mandatory, 'sha256': hashlib.sha256(mandatory.encode()).hexdigest()}
    directory = install_set(tmp_path / 'hot', value)
    callbacks = []
    args = dict(config=SimpleNamespace(state=tmp_path), payload={'session_id': 'test'}, snapshot=value,
                project='alpha', work='WHOLE_WORK', pending=[{'pending': 'WHOLE_PENDING'}], provider='claude',
                current=directory, stale=False, acknowledge=callbacks.append)
    first = hub_delivery.prepare(**args)
    assert 'M' * size not in first and len(first) <= 10000
    assert hub_delivery.MANDATORY_OMITTED in first and str(directory / 'mandatory.md') in first
    assert 'WHOLE_PENDING' in first and str(directory / 'alpha.md') in first
    copies = list((tmp_path / 'hub-context').glob('*.json'))
    assert len(copies) == 1 and str(copies[0]) in first and 'work field' in first
    assert json.loads(copies[0].read_text())['work'] == 'WHOLE_WORK'
    assert len(callbacks) == 1
    callbacks.pop()()
    second = hub_delivery.prepare(**args)
    assert 'unchanged' in second and hub_delivery.MANDATORY_OMITTED not in second


def test_unlinked_directory_gets_a_notice_instead_of_a_bare_generation_line():
    from kelmo.hub_delivery import deliver, UNLINKED_NOTICE
    told = deliver(snapshot(4, 0), None, '', [], provider='claude', current=None, unlinked=True)
    assert told.startswith('Kelmo HOT generation 4') and UNLINKED_NOTICE in told
    assert 'kelmo link --project NAME' in told
    # No cwd at all (nothing to link) keeps the plain mandatory delivery.
    plain = deliver(snapshot(4, 0), None, '', [], provider='claude', current=None)
    assert UNLINKED_NOTICE not in plain and 'mandatory' in plain


def test_claude_hook_refreshes_the_codex_block_embedded_in_the_same_repository(tmp_path):
    from kelmo.hub_delivery import prepare
    from kelmo.hub_embed import select
    config = SimpleNamespace(state=tmp_path / 'state')
    repo = tmp_path / 'repo'
    repo.mkdir()
    agents = repo / 'AGENTS.md'
    agents.write_text('# Agent notes\n\nUser-owned line.\n')
    current = tmp_path / 'hot/current'
    select(config, 'alpha', 'codex', agents, current)
    callbacks = []
    args = dict(config=config, payload={'session_id': 'one', 'cwd': str(repo)}, project='alpha', work='',
                pending=[], provider='claude', current=current, stale=False, acknowledge=callbacks.append)
    prepare(snapshot=snapshot(2, 1), **args)
    text = agents.read_text()
    assert text.startswith('# Agent notes\n\nUser-owned line.\n\n<!-- kelmo:hub begin -->')
    assert 'generation 2' in text
    callbacks.pop()()
    changed = prepare(snapshot=snapshot(3, 1), **args)
    assert 'generation 3' in agents.read_text() and 'generation 3' in changed
    # A stale set never rewrites the block.
    callbacks.pop()()
    saved = agents.read_text()
    prepare(snapshot=snapshot(2, 1), **dict(args, stale=True))
    assert agents.read_text() == saved


def _hooks_only(tmp_path, acks, **overrides):
    base = dict(config=SimpleNamespace(state=tmp_path / 'state'), payload={'session_id': 'one'}, project='alpha',
                work='', pending=[], provider='claude', current=tmp_path / 'hot/current', stale=False,
                acknowledge=acks.append)
    return dict(base, **overrides)


def test_hooks_only_session_delivers_once_then_a_marker_and_again_on_change(tmp_path):
    from kelmo.hub_delivery import prepare
    acks = []
    args = _hooks_only(tmp_path, acks)
    first = prepare(snapshot=snapshot(2, 1), action='start', **args)
    assert 'hub alpha' in first and 'restart' not in first.lower()
    acks.pop()()
    marker = prepare(snapshot=snapshot(2, 1), **args)
    assert 'hub alpha' not in marker and 'generation 2 unchanged' in marker
    assert str(tmp_path / 'hot/current/alpha.md') in marker and len(marker) < 300
    acks.pop()()
    changed = prepare(snapshot=snapshot(3, 1), **args)
    assert 'hub alpha' in changed and 'generation 3' in changed and 'restart' in changed.lower()
    acks.pop()()
    # Every SessionStart delivers again, whatever started the session; none of them warns.
    for source in ('compact', 'resume', 'clear', None):
        payload = {'session_id': 'one', **({'source': source} if source else {})}
        again = prepare(snapshot=snapshot(3, 1), action='start', **dict(args, payload=payload))
        assert 'hub alpha' in again and 'restart' not in again.lower()
        acks.pop()()
    # Codex hooks-only behaves the same: its compaction fires SessionStart with source compact.
    codex = dict(args, provider='codex')
    assert 'hub alpha' in prepare(snapshot=snapshot(3, 1), action='start', **codex)
    acks.pop()()
    assert 'unchanged' in prepare(snapshot=snapshot(3, 1), **codex)


def test_hooks_only_unacknowledged_or_unrecorded_sessions_resend(tmp_path):
    from kelmo.hub_delivery import prepare
    acks = []
    args = _hooks_only(tmp_path, acks)
    prepare(snapshot=snapshot(2, 1), **args)
    acks.clear()  # the output never reached the client: no record, so the next turn delivers again
    assert 'hub alpha' in prepare(snapshot=snapshot(2, 1), **args)
    acks.pop()()
    # Another session gets its own first delivery; a payload without a session id is never suppressed.
    assert 'hub alpha' in prepare(snapshot=snapshot(2, 1), **dict(args, payload={'session_id': 'two'}))
    acks.pop()()
    assert 'hub alpha' in prepare(snapshot=snapshot(2, 1), **dict(args, payload={})) and not acks
    # A corrupt record delivers again, without a restart warning.
    for record in (tmp_path / 'state' / 'hub-sessions').glob('*.json'):
        record.write_bytes(b'{')
    recovered = prepare(snapshot=snapshot(2, 1), **args)
    assert 'hub alpha' in recovered and 'restart' not in recovered.lower()


def test_hooks_only_pending_change_delivers_the_pending_block_alone(tmp_path):
    from kelmo.hub_delivery import prepare
    acks = []
    args = _hooks_only(tmp_path, acks)
    prepare(snapshot=snapshot(2, 1), action='start', **args)
    acks.pop()()
    waiting = [{'pending': 'Export rework waits for publication.', 'project': 'alpha'}]
    pending = prepare(snapshot=snapshot(2, 1), **dict(args, pending=waiting))
    assert pending.startswith('Kelmo HOT generation 2') and 'Export rework waits' in pending
    assert 'hub alpha' not in pending and 'restart' not in pending.lower()
    acks.pop()()
    cleared = prepare(snapshot=snapshot(2, 1), **args)
    assert 'none remain' in cleared and 'hub alpha' not in cleared
    acks.pop()()
    assert 'unchanged' in prepare(snapshot=snapshot(2, 1), **args)
    acks.pop()()
    # A hub change together with a pending change is a real change: the whole bundle, with the warning.
    both = prepare(snapshot=snapshot(3, 1), **dict(args, pending=waiting))
    assert 'hub alpha' in both and 'Export rework waits' in both and 'restart' in both.lower()


def test_stale_or_failed_sync_never_raises_and_never_suppresses(tmp_path, monkeypatch):
    from kelmo import hub_delivery, hub_embed
    acks = []
    args = _hooks_only(tmp_path, acks, stale=True)
    for provider in ('claude', 'codex'):
        for _ in range(2):
            out = hub_delivery.prepare(snapshot=snapshot(2, 1), **dict(args, provider=provider))
            assert 'STALE' in out and 'hub alpha' in out
            while acks:
                acks.pop()()

    def broken(*_args, **_kwargs):
        raise OSError('synthetic embed failure')
    monkeypatch.setattr(hub_embed, 'sync', broken)
    out = hub_delivery.prepare(snapshot=snapshot(2, 1), **_hooks_only(tmp_path, acks))
    assert 'hub alpha' in out and 'Hub embed update failed' in out and not acks


def test_unverified_embed_selection_still_resends_every_turn(tmp_path, monkeypatch):
    from kelmo import hub_delivery, hub_embed
    status = {'mode': 'embed', 'verified': False, 'changed': False, 'repair': 'kelmo hub import --project alpha'}
    monkeypatch.setattr(hub_embed, 'sync', lambda *_args, **_kwargs: dict(status))
    acks = []
    args = _hooks_only(tmp_path, acks)
    for _ in range(2):
        out = hub_delivery.prepare(snapshot=snapshot(2, 1), **args)
        assert 'hub alpha' in out and 'kelmo hub import' in out
        acks.pop()()
