import fcntl
import json
import os
import shutil
import subprocess
import uuid
from contextlib import ExitStack

import pytest

from kelmo import graph as graph_module
from kelmo.errors import KelmoError
from kelmo.graph import Graph
from kelmo.service import Service
from test_preservation_retry import legacy_external_vault


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    home = tmp_path / 'isolated-home'
    home.mkdir()
    configuration = home / 'gitconfig'
    configuration.write_text('[user]\n name = Synthetic Graph Tests\n'
                             ' email = graph@example.invalid\n[commit]\n gpgSign = false\n')
    templates = home / 'templates'
    templates.mkdir()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                'GIT_CONFIG_PARAMETERS', 'GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE'):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        'HOME': str(home), 'XDG_CONFIG_HOME': str(home / 'config'),
        'GIT_CONFIG_GLOBAL': str(configuration), 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_COUNT': '0', 'GIT_TEMPLATE_DIR': str(templates),
        'GIT_AUTHOR_NAME': 'Synthetic Graph Tests',
        'GIT_AUTHOR_EMAIL': 'graph@example.invalid',
        'GIT_COMMITTER_NAME': 'Synthetic Graph Tests',
        'GIT_COMMITTER_EMAIL': 'graph@example.invalid',
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def vaults(tmp_path):
    with ExitStack() as stack:
        def create(name='vault', legacy_wiki=None, **options):
            root = tmp_path / name
            if legacy_wiki is not None:
                # An existing personal wiki with its own layout, served from its own repository.
                legacy_external_vault(root, 'synthetic', legacy_wiki)
            else:
                Service.initialize(root, 'synthetic', **options)
            service = stack.enter_context(Service(root))
            if legacy_wiki is None and options.get('wiki_root') is None:
                # Tests start with an intentionally empty, synthetic embedded wiki.
                for path in service.config.wiki.rglob('*.md'):
                    if path.is_file() and '.git' not in path.parts:
                        path.unlink()
            return service, Graph(service)
        yield create


def page(service, relative, content):
    path = service.config.wiki / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def rows(service):
    return (
        [tuple(row) for row in service.archive.execute(
            'SELECT id,name,file_path FROM entities ORDER BY id')],
        [tuple(row) for row in service.archive.execute(
            'SELECT source_id,target_id,relation FROM edges ORDER BY source_id,target_id,relation')],
    )


def record(service):
    row = service.archive.execute(
        "SELECT value FROM archive_meta WHERE key='graph.snapshot'").fetchone()
    return json.loads(row[0]) if row else None


def git(root, *arguments):
    result = subprocess.run(['git', '-C', str(root), *arguments],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_two_vaults_and_external_enrollment_use_only_selected_wikis(tmp_path, vaults):
    external = tmp_path / 'personal-wiki'
    external.mkdir()
    git(external, 'init', '-q', '-b', 'master')
    original = '# External topic\n\nPersonal prose.\n'
    selected = external / 'my-layout/Topic Ω.md'
    selected.parent.mkdir()
    selected.write_text(original, encoding='utf-8')
    git(external, 'add', '.')
    git(external, 'commit', '-q', '-m', 'synthetic baseline')
    head = git(external, 'rev-parse', 'HEAD')

    one, first = vaults('first')
    two, second = vaults('second', legacy_wiki=external)
    page(one, 'Internal.md', '# Internal\n')
    (two.root / 'knowledge/wiki/decoy.md').write_text('# Not selected\n')
    assert second.stats()['graph']['status'] == 'absent'
    assert rows(two) == ([], [])
    assert first.read('Internal')['content'] == '# Internal\n'
    read = second.read('my-layout/Topic Ω')
    assert read['content'] == original
    assert read['file_path'] == 'my-layout/Topic Ω.md'
    assert read['graph']['wiki'] == str(external.resolve())
    assert read['graph']['vault_id'] == two.config.vault_id != one.config.vault_id
    assert second.query(action='details', entity_id='Internal')['result'] == []
    assert first.query(action='details', entity_id='my-layout/Topic Ω')['result'] == []
    assert [row[0] for row in rows(two)[0]] == ['my-layout/Topic Ω']
    assert selected.read_text(encoding='utf-8') == original
    assert git(external, 'rev-parse', 'HEAD') == head
    assert one.archive.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    assert two.archive.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0


def test_stats_does_not_rebuild_and_same_length_edit_rename_delete_refresh(vaults):
    service, graph = vaults()
    source = page(service, 'Alpha.md', '# Alpha\n\nold\n')
    absent = graph.stats()
    assert absent['graph']['status'] == 'absent'
    assert absent['graph']['source_digest'] is None
    assert absent['graph']['observed_source_digest']
    assert rows(service) == ([], [])
    initial = graph.read('Alpha')
    indexed = record(service)
    before = rows(service)
    timestamp = source.stat()
    source.write_text('# Alpha\n\nnew\n', encoding='utf-8')
    os.utime(source, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    assert source.stat().st_size == timestamp.st_size

    stale = graph.stats()
    assert stale['graph']['status'] == 'stale'
    assert stale['graph']['source_digest'] == initial['graph']['source_digest']
    assert stale['graph']['observed_source_digest'] != initial['graph']['source_digest']
    assert record(service) == indexed and rows(service) == before
    assert graph.read('Alpha')['content'] == '# Alpha\n\nnew\n'
    assert graph.stats()['graph']['status'] == 'current'

    renamed = source.with_name('Renamed.md')
    source.rename(renamed)
    assert graph.stats()['graph']['status'] == 'stale'
    assert [row[0] for row in rows(service)[0]] == ['Alpha']
    assert graph.read('Renamed')['file_path'] == 'Renamed.md'
    assert [row[0] for row in rows(service)[0]] == ['Renamed']
    renamed.unlink()
    assert graph.stats()['graph']['status'] == 'stale'
    empty = graph.query(action='search', query='anything')
    assert empty['result'] == [] and empty['graph']['status'] == 'current'
    assert rows(service) == ([], [])


def test_empty_wiki_is_current_but_missing_wiki_fails_and_recovers(vaults):
    service, graph = vaults()
    empty = graph.rebuild()
    assert empty['graph']['status'] == 'current'
    assert rows(service) == ([], [])
    indexed = record(service)
    hidden = service.config.wiki.with_name('wiki-away')
    service.config.wiki.rename(hidden)
    for operation in (lambda: graph.query(action='search', query='anything'), graph.rebuild):
        with pytest.raises(KelmoError) as caught:
            operation()
        assert caught.value.code == 'E_GRAPH_ROOT'
    missing = graph.stats()
    assert missing['graph']['status'] == 'failed'
    assert missing['graph']['diagnostics'][0]['code'] == 'E_GRAPH_ROOT'
    assert missing['counts'] == empty['counts']
    assert record(service) == indexed
    hidden.rename(service.config.wiki)
    assert graph.query(action='search', query='anything')['graph']['status'] == 'current'
    assert graph.stats()['graph']['diagnostics'] == []


@pytest.mark.parametrize('bad_bytes', [
    b'---\naliases: [SECRET\n---\n# Broken\n',
    b'\xff\xfeSECRET',
])
def test_failed_scan_retains_index_persists_safe_diagnostics_and_recovers(vaults, bad_bytes):
    service, graph = vaults()
    page(service, 'Good.md', '# Good\n\n[[Target]]\n')
    page(service, 'Target.md', '# Target\n')
    baseline = graph.rebuild()
    indexed, before = record(service), rows(service)
    bad = service.config.wiki / 'bad.md'
    bad.write_bytes(bad_bytes)
    for operation in (lambda: graph.query(action='details', entity_id='Good'),
                      lambda: graph.read('Good')):
        with pytest.raises(KelmoError) as caught:
            operation()
        assert caught.value.code == 'E_GRAPH_SCAN'
        assert 'SECRET' not in str(caught.value)
    failed = graph.stats()
    assert failed['graph']['status'] == 'failed'
    assert failed['counts'] == baseline['counts']
    assert 'SECRET' not in json.dumps(failed['graph'])
    assert record(service) == indexed and rows(service) == before
    with Service(service.root) as reopened:
        assert Graph(reopened).stats()['graph']['status'] == 'failed'
    bad.unlink()
    recovered = graph.read('Good')
    assert recovered['content'] == '# Good\n\n[[Target]]\n'
    assert recovered['graph']['status'] == 'current'
    assert recovered['graph']['diagnostics'] == []
    assert service.archive.execute(
        "SELECT value FROM archive_meta WHERE key='graph.failure'").fetchone() is None


def test_scanner_exception_is_safe_and_retains_previous_graph(vaults, monkeypatch):
    service, graph = vaults()
    page(service, 'Good.md', '# Good\n')
    graph.rebuild()
    indexed, before = record(service), rows(service)

    def fail_scan(_wiki):
        raise OSError('SECRET source path and race detail')

    with monkeypatch.context() as patch:
        patch.setattr(graph_module, 'scan_wiki', fail_scan)
        with pytest.raises(KelmoError) as caught:
            graph.read('Good')
        assert caught.value.code == 'E_GRAPH_SCAN'
        assert 'SECRET' not in str(caught.value)
        assert graph.stats()['graph']['status'] == 'failed'
    assert record(service) == indexed and rows(service) == before
    assert graph.read('Good')['graph']['status'] == 'current'


def test_read_never_opens_legacy_or_tampered_database_paths(tmp_path, vaults):
    service, graph = vaults()
    content = '---\naliases: [safe-alias]\n---\n# Real\n\nFull **Markdown**.\n'
    page(service, 'notes/Real.md', content)
    outside = tmp_path / 'private.md'
    outside.write_text('SECRET outside content\n')
    service.archive.execute(
        '''INSERT INTO entities(id,name,type,file_path,created_at,updated_at)
           VALUES (?,?,?,?,?,?)''',
        ('unsafe', 'unsafe', 'page', str(outside), 'legacy', 'legacy'))
    service.archive.commit()
    assert graph.stats()['graph']['status'] == 'absent'
    with pytest.raises(KelmoError) as caught:
        graph.read('unsafe')
    assert caught.value.code == 'E_NOT_FOUND'
    assert graph.stats()['graph']['status'] == 'current'
    service.archive.execute('UPDATE entities SET file_path=? WHERE id=?',
                            (str(outside), 'notes/Real'))
    service.archive.commit()
    read = graph.read('safe-alias')
    assert read['id'] == 'notes/Real'
    assert read['file_path'] == 'notes/Real.md'
    assert read['content'] == content
    for untrusted in (str(outside), '../private'):
        with pytest.raises(KelmoError) as caught:
            graph.read(untrusted)
        assert caught.value.code == 'E_NOT_FOUND'
    assert outside.read_text() == 'SECRET outside content\n'


def test_read_uses_scanned_bytes_when_a_later_edit_occurs(vaults, monkeypatch):
    service, graph = vaults()
    original = '# Page\n\nSnapshot content.\n'
    later = '# Page\n\nLater filesystem edit.\n'
    source = page(service, 'Page.md', original)
    real_scan = graph_module.scan_wiki
    digests = []

    def edit_after_scan(wiki):
        snapshot = real_scan(wiki)
        digests.append(snapshot['source_digest'])
        source.write_text(later, encoding='utf-8')
        return snapshot

    with monkeypatch.context() as patch:
        patch.setattr(graph_module, 'scan_wiki', edit_after_scan)
        read = graph.read('Page')
    assert len(digests) == 1
    assert read['content'] == original
    assert read['graph']['source_digest'] == digests[0]
    assert source.read_text(encoding='utf-8') == later
    next_read = graph.read('Page')
    assert next_read['content'] == later
    assert next_read['graph']['source_digest'] != digests[0]


def test_alias_ambiguity_and_limit_dictionary_keep_current_metadata(vaults):
    service, graph = vaults()
    page(service, 'a/Topic.md', '---\naliases: [shared]\n---\n# First\n')
    page(service, 'b/Topic.md', '---\naliases: [shared]\n---\n# Second\n')
    ambiguous = graph.query(action='details', entity_id='shared')
    assert isinstance(ambiguous['result'], dict)
    assert 'a/Topic' in str(ambiguous['result']['candidates'])
    assert 'b/Topic' in str(ambiguous['result']['candidates'])
    assert ambiguous['graph']['status'] == 'current'
    with pytest.raises(KelmoError) as caught:
        graph.read('shared')
    assert caught.value.code == 'E_AMBIGUOUS'
    assert graph.read('a/Topic')['file_path'] == 'a/Topic.md'
    page(service, 'shared.md', '# Explicit root page\n')
    assert graph.read('shared')['id'] == 'shared'

    page(service, 'A.md', '# A\n\n[[B]]\n')
    page(service, 'B.md', '# B\n\n[[C]]\n')
    page(service, 'C.md', '# C\n')
    limited = graph.query(action='path', entity_id='A', target_id='C', limit=1)
    assert isinstance(limited['result'], dict)
    assert limited['graph']['status'] == 'current'


@pytest.mark.parametrize('operation', ['query', 'rebuild', 'read'])
def test_each_operation_scans_once_while_holding_selected_graph_lock(vaults, monkeypatch, operation):
    service, graph = vaults()
    page(service, 'Page.md', '# Page\n')
    real_scan = graph_module.scan_wiki
    calls = []

    def locked_scan(wiki):
        calls.append(wiki)
        with service.config.internal('.state/graph.lock').open('a') as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return real_scan(wiki)

    monkeypatch.setattr(graph_module, 'scan_wiki', locked_scan)
    operations = {
        'query': lambda: graph.query(action='details', entity_id='Page'),
        'rebuild': graph.rebuild,
        'read': lambda: graph.read('Page'),
    }
    result = operations[operation]()
    assert calls == [service.config.wiki]
    assert result['graph']['status'] == 'current'


def test_stats_works_on_a_readonly_connection_without_creating_cache_state(vaults):
    service, graph = vaults()
    page(service, 'Page.md', '# Page\n')
    before = service.archive.total_changes
    service.archive.execute('PRAGMA query_only=ON')
    assert graph.stats()['graph']['status'] == 'absent'
    page(service, 'Broken.md', '---\naliases: [broken\n---\n')
    assert graph.stats()['graph']['status'] == 'failed'
    assert service.archive.total_changes == before
    assert record(service) is None
    assert not service.config.internal('.state/graph.lock').exists()


def test_refresh_savepoint_does_not_commit_caller_transaction(vaults):
    service, graph = vaults()
    page(service, 'Before.md', '# Before\n')
    graph.rebuild()
    indexed, before = record(service), rows(service)
    global_version = service.archive.execute(
        "SELECT value FROM archive_meta WHERE key='schema_version'").fetchone()[0]
    service.archive.execute("INSERT INTO archive_meta(key,value) VALUES ('caller.pending','yes')")
    page(service, 'After.md', '# After\n')
    assert graph.rebuild()['graph']['status'] == 'current'
    assert service.archive.in_transaction
    assert 'After' in [row[0] for row in rows(service)[0]]
    service.archive.rollback()
    assert record(service) == indexed and rows(service) == before
    assert service.archive.execute(
        "SELECT value FROM archive_meta WHERE key='caller.pending'").fetchone() is None
    assert service.archive.execute(
        "SELECT value FROM archive_meta WHERE key='schema_version'").fetchone()[0] == global_version
    assert graph.read('After')['content'] == '# After\n'
    with Service(service.root) as reopened:
        assert Graph(reopened).stats()['graph']['status'] == 'current'


def test_metadata_write_failure_rolls_back_graph_and_persists_failure(vaults):
    service, graph = vaults()
    source = page(service, 'Page.md', '# Page\n\nBefore.\n')
    baseline = graph.rebuild()
    indexed, before = record(service), rows(service)
    service.archive.execute('''CREATE TRIGGER reject_graph_snapshot
        BEFORE INSERT ON archive_meta WHEN NEW.key='graph.snapshot'
        BEGIN SELECT RAISE(ABORT, 'SECRET write failure'); END''')
    service.archive.commit()
    source.write_text('# Page\n\nAfter.\n', encoding='utf-8')
    with pytest.raises(KelmoError) as caught:
        graph.rebuild()
    assert caught.value.code == 'E_GRAPH_DB'
    assert 'SECRET' not in str(caught.value)
    assert record(service) == indexed and rows(service) == before
    failed = graph.stats()
    assert failed['graph']['status'] == 'failed'
    assert failed['counts'] == baseline['counts']
    assert 'SECRET' not in json.dumps(failed['graph'])
    service.archive.execute('DROP TRIGGER reject_graph_snapshot')
    service.archive.commit()
    assert graph.read('Page')['content'] == '# Page\n\nAfter.\n'
    assert graph.stats()['graph']['status'] == 'current'


def test_sqlite_failure_is_not_an_empty_stats_result(vaults):
    service, graph = vaults()
    service.archive.close()
    with pytest.raises(KelmoError) as caught:
        graph.stats()
    assert caught.value.code == 'E_GRAPH_DB'


def test_restore_to_new_root_rebinds_even_when_bytes_and_vault_id_match(tmp_path):
    source = tmp_path / 'source'
    Service.initialize(source, 'synthetic')
    with Service(source) as service:
        for path in service.config.wiki.rglob('*.md'):
            path.unlink()
        page(service, 'Page.md', '# Page\n\nRestored bytes.\n')
        original = Graph(service).read('Page')
    restored = tmp_path / 'restored'
    shutil.copytree(source, restored)
    (source / 'knowledge/wiki/Page.md').write_text('SECRET old-root edit\n')
    with Service(restored) as service:
        graph = Graph(service)
        stale = graph.stats()
        assert stale['graph']['status'] == 'stale'
        assert stale['graph']['vault_id'] == original['graph']['vault_id']
        assert stale['graph']['observed_source_digest'] == original['graph']['source_digest']
        assert stale['graph']['source_digest'] is None
        read = graph.read('Page')
        assert read['content'] == '# Page\n\nRestored bytes.\n'
        assert read['graph']['wiki'] == str(service.config.wiki.resolve())
        assert read['graph']['status'] == 'current'


def test_vault_id_is_part_of_binding(vaults):
    service, graph = vaults()
    page(service, 'Page.md', '# Page\n')
    original = graph.read('Page')
    replacement = service.config.vault_id
    changed = record(service)
    changed['vault_id'] = str(uuid.uuid4())
    # Only the cached graph belongs to another vault; archive ownership stays valid.
    service.archive.execute("UPDATE archive_meta SET value=? WHERE key='graph.snapshot'",
                            (json.dumps(changed),))
    service.archive.commit()
    with Service(service.root) as reopened:
        selected = Graph(reopened)
        stale = selected.stats()
        assert stale['graph']['status'] == 'stale'
        assert stale['graph']['observed_source_digest'] == original['graph']['source_digest']
        assert selected.read('Page')['graph']['vault_id'] == replacement
        assert record(reopened)['vault_id'] == replacement


@pytest.mark.parametrize('field', ['schema_version', 'algorithm_version'])
def test_graph_versions_are_part_of_binding(vaults, field):
    service, graph = vaults()
    page(service, 'Page.md', '# Page\n')
    graph.rebuild()
    old = record(service)
    changed = dict(old)
    changed[field] += 1
    service.archive.execute("UPDATE archive_meta SET value=? WHERE key='graph.snapshot'",
                            (json.dumps(changed),))
    service.archive.commit()
    assert graph.stats()['graph']['status'] == 'stale'
    assert graph.read('Page')['graph']['status'] == 'current'
    assert record(service)[field] == old[field]


def test_invalid_inputs_fail_before_scanning_or_lock_creation(vaults):
    service, graph = vaults()
    valid = {'action': 'details', 'entity_id': 'Page'}
    cases = [{}, {'action': None}, {'action': True}, {'action': []},
             {'action': 'unknown'}, {**valid, 'unknown': 1}]
    cases += [{'action': action} for action in ('search', 'neighbors', 'impact', 'path', 'details')]
    cases += [{'action': 'search', 'query': value} for value in (True, 3, [], '', '  ')]
    cases += [{**valid, 'entity_id': value} for value in (True, 3, [], '', '  ')]
    cases += [{**valid, 'action': 'path', 'target_id': value}
              for value in (None, True, 3, [], '', '  ')]
    cases += [{**valid, 'radius': value} for value in (True, 0, 3, 1.0, '1')]
    cases += [{**valid, 'limit': value} for value in (False, 0, 201, 1.0, '1')]
    cases += [{**valid, 'relation_filter': value}
              for value in (True, 3, 'links_to', '', [1], [''], ['  '])]
    cases += [{**valid, 'query': []}, {**valid, 'target_id': True}]
    for arguments in cases:
        with pytest.raises(KelmoError) as caught:
            graph.query(**arguments)
        assert caught.value.code == 'E_INPUT', arguments
    for entity_id in (None, True, 3, [], '', '  '):
        with pytest.raises(KelmoError) as caught:
            graph.read(entity_id)
        assert caught.value.code == 'E_INPUT'
    with pytest.raises(KelmoError) as caught:
        graph.read()
    assert caught.value.code == 'E_INPUT'
    assert record(service) is None
    assert not service.config.internal('.state/graph.lock').exists()
    assert graph.query(action='search', query='Page', radius=2, limit=200,
                       relation_filter=['links_to'])['graph']['status'] == 'current'


def test_relation_filters_are_lists_and_reach_the_graph_traversal(vaults):
    service, graph = vaults()
    page(service, 'Source.md', '---\ntags: [blue]\n---\n# Source\n[[Target]]\n')
    page(service, 'Target.md', '# Target\n')
    found = graph.query(action='neighbors', entity_id='Source', relation_filter=['links_to'])
    assert [node['id'] for node in found['result']] == ['Target']
    assert graph.query(action='neighbors', entity_id='Source', relation_filter=[])['result'] == []


def test_graph_lock_cannot_follow_an_internal_symlink(tmp_path, vaults):
    service, graph = vaults()
    outside = tmp_path / 'outside-lock'
    outside.write_text('unchanged\n')
    service.config.internal('.state/graph.lock').symlink_to(outside)
    with pytest.raises(KelmoError) as caught:
        graph.query(action='search', query='anything')
    assert caught.value.code == 'E_STATE'
    assert outside.read_text() == 'unchanged\n'
    assert record(service) is None
