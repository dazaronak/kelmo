"""Work that depends only on its input is done once per input: type hints per record class, registry
validation per registry content, the committed generation per HOT check. Synthetic data only."""
import json
import subprocess

import pytest


def test_record_type_hints_are_resolved_once_per_class(monkeypatch):
    from kelmo import contracts
    calls = []
    real = contracts.get_type_hints

    def counting(cls):
        calls.append(cls)
        return real(cls)

    monkeypatch.setattr(contracts, 'get_type_hints', counting)
    monkeypatch.setattr(contracts, '_SHAPES', {})
    value = {'archive_id': 'a1', 'version': 1, 'content_sha256': '0' * 64, 'source_id': 's1'}
    first = contracts.EvidenceRef.from_dict(value)
    second = contracts.EvidenceRef.from_dict(dict(value, version=2))
    assert calls == [contracts.EvidenceRef]
    assert (first.version, second.version) == (1, 2)
    with pytest.raises(ValueError, match='Unknown fields'):
        contracts.EvidenceRef.from_dict(dict(value, extra=1))
    with pytest.raises(ValueError, match='contract type'):
        contracts.EvidenceRef.from_dict(dict(value, version='1'))
    assert calls == [contracts.EvidenceRef]


@pytest.fixture
def counted_validation(monkeypatch):
    from kelmo.wiki import engine
    calls = []
    real = engine._validate_v3

    def counting(registry):
        calls.append(1)
        return real(registry)

    monkeypatch.setattr(engine, '_validate_v3', counting)
    monkeypatch.setattr(engine, '_VALIDATED', type(engine._VALIDATED)())
    return calls


def test_registry_bytes_are_validated_once_and_parsed_fresh(counted_validation):
    from kelmo.wiki import engine
    raw = json.dumps(engine.initial_registry('synthetic-authority', 'alpha')).encode()
    other = json.dumps(engine.initial_registry('synthetic-authority', 'beta')).encode()
    counted_validation.clear()  # initial_registry validates what it builds
    first = engine.parse_registry(raw)
    second = engine.parse_registry(raw)
    assert len(counted_validation) == 1
    assert first == second and first is not second
    first['pages'].clear()  # a caller may change its copy; the next parse is unaffected
    assert engine.parse_registry(raw)['pages']
    engine.parse_registry(other)
    assert len(counted_validation) == 2


def test_invalid_registry_bytes_are_refused_every_time(counted_validation):
    from kelmo.wiki import engine
    registry = engine.initial_registry('synthetic-authority', 'alpha')
    registry['pages']['projects/alpha/alpha.md']['revision'] = '0' * 64
    raw = json.dumps(registry).encode()
    counted_validation.clear()  # initial_registry validates what it builds
    for _ in range(2):
        with pytest.raises(ValueError, match='revision'):
            engine.parse_registry(raw)
    assert len(counted_validation) == 2


def test_hot_check_all_reads_each_generation_once_and_selects_as_before(tmp_path, monkeypatch, counted_validation):
    from kelmo import config, hot, projects
    from kelmo.initialization import initialize
    from kelmo.wiki import pages
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    root = tmp_path / 'vault'
    initialize(root, 'hot-reuse', project='alpha', git_name='Test', git_email='test@example.invalid')
    for name in ('beta', 'gamma'):
        projects.add(root, name)
    loaded = config.Config.load(root)
    expected = []
    for project in [None, *sorted(loaded.hot['projects'])]:
        selection = hot.render(loaded, project=project)
        selection.pop('context')
        expected.append(selection)
    loads = []
    real_build = pages.KnowledgeReader._build_snapshot

    def counting_build(self, commit):
        loads.append(commit)
        return real_build(self, commit)

    monkeypatch.setattr(pages.KnowledgeReader, '_build_snapshot', counting_build)
    monkeypatch.setattr(pages, '_SNAPSHOTS', type(pages._SNAPSHOTS)())
    monkeypatch.setattr(pages, '_IN_USE', type(pages._IN_USE)())
    counted_validation.clear()
    result = hot.check_all(loaded)
    assert result['selections'] == expected
    assert len(expected) == 4
    git_root = getattr(loaded, 'knowledge_git_root', loaded.root)
    head = subprocess.run(['git', '-C', str(git_root), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert loads == [head]
    assert counted_validation == []  # the content was validated by the renders above


def test_a_long_lived_process_keeps_two_generation_snapshots_whatever_the_readers(tmp_path, monkeypatch):
    """A daemon's state core has a reader per thread and publishes all day: it holds the current generation and
    the one before, once, not a copy per reader per generation."""
    from kelmo import config, projects
    from kelmo.initialization import initialize
    from kelmo.wiki import pages
    monkeypatch.setattr(config, 'validate_hot', projects.validate_hot)
    monkeypatch.setattr(pages, '_SNAPSHOTS', type(pages._SNAPSHOTS)())
    monkeypatch.setattr(pages, '_IN_USE', type(pages._IN_USE)())
    root = tmp_path / 'vault'
    initialize(root, 'snapshot-bound', project='alpha', git_name='Test', git_email='test@example.invalid')
    readers = [pages.KnowledgeReader(config.Config.load(root)) for _ in range(5)]
    seen = []
    for name in ('beta', 'gamma', 'delta', 'epsilon'):
        projects.add(root, name)
        for reader in readers:
            generation = reader.generation()
            assert reader.page(f'projects/{name}/{name}.md', generation)
        seen.append(generation)
        assert pages.cached_snapshots() <= pages.SNAPSHOT_CACHE_LIMIT
        registries = {id(reader._pinned(generation)['registry']) for reader in readers}
        assert len(registries) == 1
    assert len({generation.publication_id for generation in seen}) == 4
    # A generation that fell out of the cache is still readable: it is loaded again from Git.
    batch = readers[0].pages('beta', seen[0])
    assert batch.generation == seen[0] and batch.total >= 1
    assert pages.cached_snapshots() <= pages.SNAPSHOT_CACHE_LIMIT

