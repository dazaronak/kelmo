"""Synthetic, controller-only Git fixtures for the bound publication/read path.

The internal reader
is pages.KnowledgeReader(Config): generation() plus the adoption/1 pages/page/context
read signatures. No new transport record, registry, journal or v3 file schema is
defined here. Shared Config/engine integration remains with the controller.
"""

import copy
import hashlib
import json
import os
import shutil
from types import SimpleNamespace

import pytest

from kelmo.config import Config
from kelmo.contracts import ContextBinding, ContextBudget, Generation, PageBatch, PageSnapshot
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo.wiki import engine, links, pages
from test_editor_context import fact, work
from test_wiki_roots import git, initialize_repository

TARGET = "projects/fixture/work.md"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


AUTHORITY = "00000000-0000-4000-8000-000000000004"
GENERATION = "knowledge/provenance/generation.json"
REGISTRY = "knowledge/provenance/registry.json"
PREFIX = b"---\r\naliases: [work-alias]\r\ncustom: keep  \r\n---\r\n# Work\r\n\r\nNarrative.  \r\n"
SUFFIX = b"\r\n\r\nHand-maintained ending.\t\r\n"


def write_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def managed_digest(repository, tree):
    """Independent oracle for frozen editor_publish.managed_digest's tree contract."""
    prefixes = (REGISTRY, GENERATION, "knowledge/provenance/hot/", "knowledge/hot/", "knowledge/wiki/")
    excluded = {GENERATION, "knowledge/wiki/entities.yml", "knowledge/wiki/_template.md",
                "knowledge/wiki/log.md"}
    rows = []
    # Fixture paths are ASCII without whitespace; the frozen source also uses -z
    # for general paths. Git object IDs/modes, rather than file hashes, are hashed.
    for line in git(repository, "ls-tree", "-r", "--full-tree", tree).splitlines():
        metadata, path = line.split("\t", 1)
        if path in excluded or not any(
            path == item or item.endswith("/") and path.startswith(item) for item in prefixes
        ):
            continue
        mode, _kind, object_id = metadata.split()
        rows.append((path, mode, object_id))
    return sha("".join(f"{path}\0{mode}\0{oid}\n" for path, mode, oid in sorted(rows)).encode())


def configure(layout, relative=False, git_root=None):
    selected = git_root or layout.vault
    paths = {"state_root": layout.state, "archive_root": layout.archive,
             "knowledge_git_root": selected}
    if relative:
        paths = {key: os.path.relpath(path, layout.control) for key, path in paths.items()}
    text = ("schema_version = 2\nwriter = \"local\"\n"
            f"vault_id = {json.dumps(layout.authority)}\nnode_label = \"synthetic\"\n\n[storage]\n")
    text += "".join(f"{key} = {json.dumps(str(path))}\n" for key, path in paths.items())
    text += f"registry_relative = {json.dumps(REGISTRY)}\nwiki_relative = \"knowledge/wiki\"\n"
    (layout.control / "kelmo.toml").write_text(text)


def page_entry(layout_name="legacy", **fields):
    """A registered page entry in the stored format, its revision derived."""
    entry = dict(work(), page_id="fixture-work", layout=layout_name, aliases=[], tags=[], repositories=[], links=[],
                 previous_revision=None, compatibility={})
    entry.update(fields)
    entry["revision"] = engine.entity_revision(entry)
    return entry


def make_layout(root, authority=AUTHORITY):
    """A schema-2 vault: control, state, archive and the knowledge Git repository are separate roots.

    The repository already holds unrelated history and files, a hand-made page and the published
    fixture registry, so every case starts from a store with one real publication behind it.
    """
    import registry_seed
    layout = SimpleNamespace(control=root / "control", state=root / "technical-state",
                             archive=root / "restricted-archive", vault=root / "existing-vault",
                             authority=authority)
    layout.wiki = layout.vault / "knowledge/wiki"
    for path in (layout.control, layout.state, layout.archive):
        path.mkdir(parents=True)
    initialize_repository(layout.vault)
    configure(layout)
    facts = [fact(f"fixture.{index}", TARGET, f"Complete fact {index} with its exception.",
                  role="constraint") for index in range(3)]
    spec = {"facts": {item["fact_key"]: item for item in facts},
            "notes": {item["note_id"]: {"filename": "source.md", "ark_id": item["ark_id"]} for item in facts}}
    layout.registry = registry_seed.build(spec, authority_id=authority,
                                          pages_settings={TARGET: dict(work(), page_id="fixture-work",
                                                                       layout="legacy")})
    layout.original = PREFIX + b"<!-- kb -->\n" + b"\n".join(
        f"- {item['text']} [f:{item['id']}]".encode() for item in facts) + b"\n<!-- /kb -->" + SUFFIX
    write_bytes(layout.wiki / TARGET, layout.original)
    for name, raw in {"documents/keep.bin": b"\x00document\xff", "publishing/draft.md": b"Draft.  \r\n",
                      "knowledge/wiki/_template.md": b"Hand template.\r\n",
                      "knowledge/wiki/log.md": b"Hand log.\n",
                      "knowledge/wiki/entities.yml": b"aliases: keep\n",
                      "knowledge/hot/fixture.md": b"Existing HOT.\n",
                      "knowledge/provenance/hot/fixture.json": b"{\"retained\": true}\n"}.items():
        write_bytes(layout.vault / name, raw)
    write_bytes(layout.archive / "keep.bin", b"restricted archive sentinel")
    layout.hook = layout.vault / ".git/hooks/pre-receive"
    layout.hook.write_bytes(b"#!/bin/sh\n# Synthetic retained hook\nexit 0\n")
    git(layout.vault, "add", ".")
    git(layout.vault, "commit", "-q", "-m", "existing vault history")
    engine.initialize_registry(layout.control, None, "chore: Initialize fact registry")
    enroll(layout)
    registry_seed.publish(layout.control, layout.registry, "chore: Publish existing knowledge")
    # The committed bytes of the first publication are what every later read is compared with.
    layout.original = (layout.wiki / TARGET).read_bytes()
    layout.head = git(layout.vault, "rev-parse", "HEAD")
    layout.history = git(layout.vault, "rev-list", "HEAD").splitlines()
    return layout


@pytest.fixture
def layout(tmp_path):
    return make_layout(tmp_path)


def enroll(layout):
    config = Config.load(layout.control)
    engine.configure_wiki(layout.control, config.wiki, vault_id=config.vault_id)
    engine.activate_wiki(config.wiki, config.vault_id)
    return config


def updated_registry(layout):
    updated = copy.deepcopy(layout.registry)
    stored = updated["facts"]["fixture.0"]
    stored["previous_revision"] = stored["revision"]
    stored["text"] = "Changed fact, still only with its exception."
    stored["revision"] = engine.entity_revision(stored)
    return updated


def reader(layout):
    return pages.KnowledgeReader(Config.load(layout.control))


def publish(layout, registry=None, title="chore: synthetic publication", **options):
    import registry_seed
    registry_seed.publish(layout.control, updated_registry(layout) if registry is None else registry, title,
                          **options)


def register_empty_parent(layout):
    parent = 'projects/fixture/empty-topic.md'
    layout.registry['pages'][parent] = page_entry(kind='topic', title='Empty registered topic', summary='',
                                                  priority='', page_id='empty-topic')
    child = layout.registry['pages'][TARGET]
    child['parent'] = parent
    child['previous_revision'] = child['revision']
    child['revision'] = engine.entity_revision({key: value for key, value in child.items() if key != 'revision'})
    return parent


def assert_no_competing_storage(layout):
    assert not (layout.wiki / ".git").exists()
    assert not (layout.control / ".git").exists()
    assert not (layout.control / REGISTRY).exists()
    assert not (layout.control / ".state").exists()
    assert not (layout.vault / ".state").exists()
    assert sorted(path.relative_to(layout.archive).as_posix() for path in layout.archive.rglob("*")
                  if path.is_file()) == ["keep.bin"]
    assert (layout.archive / "keep.bin").read_bytes() == b"restricted archive sentinel"


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_config_binds_state_archive_registry_wiki_and_git_independently(layout, relative):
    configure(layout, relative=relative)
    before = (layout.vault / REGISTRY).read_bytes()

    config = Config.load(layout.control)

    assert config.state == layout.state
    assert config.archive == layout.archive / "archive.db"
    assert config.registry == layout.vault / REGISTRY
    assert config.wiki == layout.wiki
    assert config.knowledge_git_root == layout.vault
    assert engine.load_registry(layout.control) == layout.registry
    assert (layout.vault / REGISTRY).read_bytes() == before
    assert_no_competing_storage(layout)


def test_missing_bound_registry_is_refused_instead_of_bootstrapping_an_empty_authority(layout):
    (layout.vault / REGISTRY).unlink()
    head = git(layout.vault, "rev-parse", "HEAD")

    with pytest.raises(KelmoError) as refused:
        engine.load_registry(layout.control)

    assert refused.value.code == 'E_STATE'

    assert not (layout.vault / REGISTRY).exists()
    assert git(layout.vault, "rev-parse", "HEAD") == head
    assert_no_competing_storage(layout)


@pytest.mark.parametrize('case', ['overlap', 'escape', 'root-symlink', 'descendant-symlink'])
def test_schema_two_rejects_unsafe_bindings_without_creating_storage(layout, case):
    config_path = layout.control / 'kelmo.toml'
    text = config_path.read_text()
    if case == 'overlap':
        text = text.replace(json.dumps(str(layout.state)), json.dumps(str(layout.vault / 'state')))
    elif case == 'escape':
        text = text.replace(json.dumps(REGISTRY), json.dumps('../registry.json'))
    elif case == 'root-symlink':
        link = layout.control / 'state-link'
        link.symlink_to(layout.state, target_is_directory=True)
        text = text.replace(json.dumps(str(layout.state)), json.dumps(str(link)))
    else:
        (layout.wiki / 'linked.md').symlink_to(layout.control / 'absent.md')
    config_path.write_text(text)
    head = git(layout.vault, 'rev-parse', 'HEAD')
    original = (layout.vault / REGISTRY).read_bytes()

    with pytest.raises(KelmoError) as rejected:
        Config.load(layout.control)

    assert rejected.value.code == 'E_CONFIG'
    assert (layout.vault / REGISTRY).read_bytes() == original
    assert git(layout.vault, 'rev-parse', 'HEAD') == head
    assert not (layout.control / '.state').exists()


def test_missing_manifest_is_an_explicit_read_refusal_without_publication(layout):
    (layout.vault / GENERATION).unlink()
    git(layout.vault, 'add', GENERATION)
    git(layout.vault, 'commit', '-q', '-m', 'synthetic legacy store without manifest')
    head = git(layout.vault, 'rev-parse', 'HEAD')

    with pytest.raises(KelmoError) as refused:
        reader(layout).generation()

    assert refused.value.code == 'E_GENERATION'
    assert not (layout.vault / GENERATION).exists()
    assert git(layout.vault, 'rev-parse', 'HEAD') == head


def test_nested_vault_history_and_unrelated_files_are_unchanged(layout):
    unrelated = (layout.vault / "publishing/draft.md")
    unrelated.write_bytes(b"User's uncommitted draft.\r\n")
    git(layout.vault, "add", "publishing/draft.md")
    index_before = git(layout.vault, "ls-files", "--stage", "--", "publishing/draft.md")
    hook_before = layout.hook.read_bytes()
    enrolled = enroll(layout)
    assert git(layout.vault, "rev-parse", "HEAD") == layout.head, "enrollment rewrote history"

    publish(layout)

    assert engine.load_registry(layout.control) == updated_registry(layout)
    rendered = (enrolled.wiki / TARGET).read_bytes()
    assert rendered.split(b"<!-- kb -->", 1)[0] == PREFIX
    assert rendered.split(b"<!-- /kb -->", 1)[1] == SUFFIX
    assert b"Changed fact, still only with its exception." in rendered
    assert unrelated.read_bytes() == b"User's uncommitted draft.\r\n"
    assert git(layout.vault, "show", "HEAD:publishing/draft.md") == "Draft."
    assert git(layout.vault, "ls-files", "--stage", "--", "publishing/draft.md") == index_before
    assert (layout.vault / "documents/keep.bin").read_bytes() == b"\x00document\xff"
    assert layout.hook.read_bytes() == hook_before
    assert git(layout.vault, "rev-list", "HEAD").splitlines()[1:] == layout.history
    changed = set(git(layout.vault, "diff", "--name-only", layout.head, "HEAD").splitlines())
    assert {REGISTRY, GENERATION, "knowledge/wiki/" + TARGET} <= changed
    assert all(path in {REGISTRY, GENERATION} or path.startswith("knowledge/wiki/") for path in changed)
    assert_no_competing_storage(layout)


def test_publication_keeps_the_existing_generation_sequence_and_tree_digest(layout):
    enroll(layout)
    old = json.loads((layout.vault / GENERATION).read_bytes())
    publish(layout)
    raw = (layout.vault / GENERATION).read_bytes()
    current = json.loads(raw)

    assert current["schema"] == 1
    assert current["sequence"] == old["sequence"] + 1 and current["parent_sequence"] == old["sequence"]
    assert set(old) <= set(current), "the legacy generation envelope was replaced"
    assert current["managed_digest"] == managed_digest(layout.vault, "HEAD")
    assert current["counts"] == {"facts": 3, "pages": 1, "notes": 3}
    assert current["sources"] == old["sources"]
    assert reader(layout).generation() == Generation(AUTHORITY, old["sequence"] + 1, sha(raw))
    head = git(layout.vault, "rev-parse", "HEAD")
    publish(layout)
    assert git(layout.vault, "rev-parse", "HEAD") == head, "a replay published another generation"
    assert (layout.vault / GENERATION).read_bytes() == raw


@pytest.mark.parametrize('boundary', ['normal', 'before-commit', 'after-commit'])
def test_empty_registered_parent_is_published_and_recovers_with_its_generation(layout, monkeypatch, boundary):
    enroll(layout)
    sequence = json.loads((layout.vault / GENERATION).read_bytes())["sequence"]
    parent = register_empty_parent(layout)
    path = layout.wiki / parent
    assert not path.exists()
    if boundary == 'normal':
        publish(layout)
    else:
        marker = interrupt(layout, monkeypatch, boundary)
        engine.recover_transaction(layout.control)
        assert not marker.exists()
        if boundary == 'before-commit':
            assert not path.exists()
            assert git(layout.vault, 'rev-parse', 'HEAD') == layout.head
            assert parent not in engine.load_registry(layout.control)['pages']
            publish(layout)

    stored = engine.load_registry(layout.control)
    assert stored == updated_registry(layout)
    assert stored['pages'][parent] == layout.registry['pages'][parent]
    assert pages.page_facts(stored, parent)['facts'] == []
    view = reader(layout)
    pin = view.generation()
    assert pin.publication_id == sequence + 1
    assert view.page(parent, pin).content == path.read_bytes()
    assert b'# Empty registered topic\n' in path.read_bytes()
    raw = (layout.wiki / TARGET).read_bytes()
    assert raw.split(b'<!-- kb -->', 1)[0] == PREFIX
    assert raw.split(b'<!-- /kb -->', 1)[1] == SUFFIX
    assert engine.wiki_committed(layout.control)
    head = git(layout.vault, 'rev-parse', 'HEAD')
    publish(layout)
    assert git(layout.vault, 'rev-parse', 'HEAD') == head
    assert_no_competing_storage(layout)


@pytest.mark.parametrize('defect', ['missing-page', 'bad-manifest'])
def test_invalid_projected_generation_is_refused_before_commit_or_marker(layout, monkeypatch, defect):
    enroll(layout)
    parent = register_empty_parent(layout)
    unrelated = layout.vault / 'knowledge/hot/fixture.md'
    unrelated.write_bytes(b'Unrelated staged HOT.  \r\n')
    git(layout.vault, 'add', 'knowledge/hot/fixture.md')
    index = engine.git(layout.vault, 'ls-files', '--stage', '-z', binary=True)
    originals = {path: path.read_bytes() for path in layout.wiki.rglob('*') if path.is_file()}
    originals.update({path: path.read_bytes() for path in (layout.vault / REGISTRY, layout.vault / GENERATION)})
    if defect == 'missing-page':
        render = engine.render_v3

        def omit_page(*args, **kwargs):
            rendered = render(*args, **kwargs)
            return {path: text for path, text in rendered.items() if not path.as_posix().endswith('/' + parent)}

        monkeypatch.setattr(engine, 'render_v3', omit_page)
    else:
        generate = engine._generation

        def corrupt_manifest(*args, **kwargs):
            text, content_tree = generate(*args, **kwargs)
            manifest = json.loads(text)
            manifest['counts']['pages'] += 1
            return engine.encoded(manifest), content_tree

        monkeypatch.setattr(engine, '_generation', corrupt_manifest)

    with pytest.raises(KelmoError) as refused:
        publish(layout)

    assert refused.value.code == 'E_GENERATION'
    assert git(layout.vault, 'rev-parse', 'HEAD') == layout.head
    assert engine.git(layout.vault, 'ls-files', '--stage', '-z', binary=True) == index
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not (layout.wiki / parent).exists()
    assert not engine.transaction_path(layout.control).exists()
    assert not any(layout.state.iterdir())
    assert unrelated.read_bytes() == b'Unrelated staged HOT.  \r\n'


def test_fact_publication_refuses_a_staged_registered_page_without_touching_it(layout):
    """Every registered page is projected on each publication, so a staged hand edit in any of them stops
    the publication before anything is written: working bytes, index entry and HEAD stay as they were."""
    enroll(layout)
    parent = register_empty_parent(layout)
    publish(layout, layout.registry, 'chore: register the empty parent')
    path = layout.wiki / parent
    original = path.read_bytes()
    relative = 'knowledge/wiki/' + parent
    path.write_bytes(original + b'Unpublished work.\t\r\n')
    git(layout.vault, 'add', relative)
    index = engine.git(layout.vault, 'ls-files', '--stage', '-z', '--', relative, binary=True)
    head = engine.git(layout.vault, 'rev-parse', 'HEAD')

    with pytest.raises(engine.DirtyKnowledge) as raised:
        publish(layout)

    assert relative in str(raised.value)
    assert engine.git(layout.vault, 'rev-parse', 'HEAD') == head
    assert path.read_bytes() == original + b'Unpublished work.\t\r\n'
    assert engine.git_blob(layout.vault, 'HEAD:' + relative) == original
    assert engine.git(layout.vault, 'ls-files', '--stage', '-z', '--', relative, binary=True) == index
    assert not engine.transaction_path(layout.control).exists()


def test_generation_uses_committed_hot_bytes_and_preserves_unrelated_staged_hot(layout):
    enroll(layout)
    sequence = json.loads((layout.vault / GENERATION).read_bytes())["sequence"]
    relative = 'knowledge/hot/fixture.md'
    path = layout.vault / relative
    baseline = path.read_bytes()
    path.write_bytes(b'Unrelated staged HOT.  \r\n')
    git(layout.vault, 'add', relative)
    staged = git(layout.vault, 'ls-files', '--stage', '--', relative)

    publish(layout)

    assert path.read_bytes() == b'Unrelated staged HOT.  \r\n'
    assert engine.git_blob(layout.vault, 'HEAD:' + relative) == baseline
    assert git(layout.vault, 'ls-files', '--stage', '--', relative) == staged
    manifest = json.loads((layout.vault / GENERATION).read_bytes())
    assert manifest['managed_digest'] == managed_digest(layout.vault, 'HEAD')
    assert reader(layout).generation().publication_id == sequence + 1


def test_external_state_preserves_new_and_legacy_source_identities(tmp_path, monkeypatch):
    from kelmo.contracts import SourcePacket

    for name, value in {'GIT_AUTHOR_NAME': 'Synthetic Fixture',
                        'GIT_COMMITTER_NAME': 'Synthetic Fixture',
                        'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                        'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}.items():
        monkeypatch.setenv(name, value)
    legacy_root = tmp_path / 'legacy'
    Service.initialize(legacy_root, 'synthetic')
    with Service(legacy_root) as legacy:
        old = legacy.capture('Legacy original.  \r\n', 'legacy.md')
        old_packet = legacy.source_packet(old['id'])
        authority = legacy.config.vault_id
    bound = make_layout(tmp_path / 'bound', authority)
    bound.state.rmdir()
    shutil.move(legacy_root / '.state', bound.state)
    shutil.move(legacy_root / 'archive/archive.db', bound.archive / 'archive.db')
    packet = SourcePacket(b'External state original.  \r\n', 'raw_note', 'source:external',
                          'external.md', '2026-10-14T00:00:00Z')

    with Service(bound.control) as selected:
        assert selected.source_packet(old['id']) == old_packet
        legacy_absolute = dict(old, source_path=str(legacy_root / old['source_path']))
        assert selected.source_path(legacy_absolute) == bound.state / 'sources' / (old['source_hash'] + '.md')
        operation = selected.capture_source(packet, selected.origin, 'external-source')
        assert operation['source_path'] == '.state/sources/' + operation['source_hash'] + '.md'
        assert selected.source_path(operation).is_relative_to(bound.state)
        assert selected.source_packet(operation['id']) == packet
        pin = selected.knowledge.generation()
        assert selected.knowledge.page(TARGET, pin).content == bound.original
        with pytest.raises(KelmoError, match='enrollment'):
            selected.knowledge.context(ContextBinding('payload-repository'), pin, ContextBudget(800))
    def resolve(binding):
        if (binding.repository_id, binding.worktree_id) != ('trusted-repository', 'trusted-worktree'):
            raise KelmoError('E_BINDING', 'Unknown controller enrollment.')
        return ContextBinding(authority, project='fixture')

    with Service(bound.control, resolve_context_binding=resolve) as reopened:
        assert reopened.source_packet(operation['id']) == packet
        assert reopened.capture_source(packet, reopened.origin, 'external-source')['id'] == operation['id']
        assert reopened.source_packet(old['id']) == old_packet
        pin = reopened.knowledge.generation()
        context = reopened.knowledge.context(ContextBinding('trusted-repository', 'trusted-worktree'),
                                             pin, ContextBudget(800))
        assert [page.path for page in context.pages] == [TARGET]
        with pytest.raises(KelmoError, match='Unknown controller enrollment'):
            reopened.knowledge.context(ContextBinding('payload-repository'), pin, ContextBudget(800))
    assert not (bound.control / '.state').exists()
    assert not (bound.vault / '.state').exists()


def test_ordinary_fact_publication_cannot_invent_action_state(layout):
    enroll(layout)
    proposed = updated_registry(layout)
    proposed['actions'] = {'payload-action': {'state': 'completed'}}
    originals = {path: path.read_bytes() for path in (layout.vault / REGISTRY,
                 layout.vault / GENERATION, layout.wiki / TARGET)}

    # Ordinary publication runs the ownership guard on its proposal before any write. The record here is
    # not a valid action, so the publisher's own validation would refuse it earlier; the guard is checked alone.
    with pytest.raises(engine.Conflict, match='cannot modify accepted actions'):
        engine._preserve_metadata(engine.load_registry(layout.control), proposed)

    assert git(layout.vault, 'rev-parse', 'HEAD') == layout.head
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not engine.transaction_path(layout.control).exists()


class PublicationInterrupted(BaseException):
    pass


def interrupt(layout, monkeypatch, boundary):
    original_git = engine.git
    original_recover = engine._recover_transaction

    def failpoint(root, *args, **kwargs):
        if boundary == "before-commit" and args[0] == "add":
            raise PublicationInterrupted
        result = original_git(root, *args, **kwargs)
        if boundary == "after-commit" and args[0] == "commit":
            raise PublicationInterrupted
        return result

    # Defer only the exception cleanup, so the interrupted publication leaves its marker on disk; every
    # later call, the test's own recovery included, runs the real implementation.
    calls = 0

    def defer_cleanup(root):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_recover(root)

    with monkeypatch.context() as scoped:
        scoped.setattr(engine, "git", failpoint)
        scoped.setattr(engine, "_recover_transaction", defer_cleanup)
        with pytest.raises(PublicationInterrupted):
            publish(layout)
    marker = engine.transaction_path(layout.control)
    assert marker.is_file(), "the failpoint did not leave a durable transaction"
    assert marker.is_relative_to(layout.state)
    return marker


@pytest.mark.parametrize("boundary", ["before-commit", "after-commit"])
def test_interrupted_publication_recovers_bound_paths(layout, monkeypatch, boundary):
    enroll(layout)
    sequence = json.loads((layout.vault / GENERATION).read_bytes())["sequence"]
    original_registry = (layout.vault / REGISTRY).read_bytes()
    original_generation = (layout.vault / GENERATION).read_bytes()
    marker = interrupt(layout, monkeypatch, boundary)

    engine.recover_transaction(layout.control)
    first_head = git(layout.vault, "rev-parse", "HEAD")
    engine.recover_transaction(layout.control)

    assert git(layout.vault, "rev-parse", "HEAD") == first_head
    assert not marker.exists()
    if boundary == "before-commit":
        assert first_head == layout.head
        assert (layout.vault / REGISTRY).read_bytes() == original_registry
        assert (layout.vault / GENERATION).read_bytes() == original_generation
        assert (layout.wiki / TARGET).read_bytes() == layout.original
    else:
        assert engine.load_registry(layout.control) == updated_registry(layout)
        generation = json.loads((layout.vault / GENERATION).read_bytes())
        assert generation["sequence"] == sequence + 1 and generation["parent_sequence"] == sequence
        assert generation["managed_digest"] == managed_digest(layout.vault, "HEAD")
        assert git(layout.vault, "rev-list", "HEAD").splitlines()[1:] == layout.history
    assert_no_competing_storage(layout)


def test_recovery_refuses_a_changed_root_binding_without_touching_either_vault(layout, tmp_path, monkeypatch):
    enroll(layout)
    marker = interrupt(layout, monkeypatch, "after-commit")
    marker_bytes = marker.read_bytes()
    other = make_layout(tmp_path / "other", "00000000-0000-4000-8000-000000000005")
    enroll(other)
    configure(layout, git_root=other.vault)
    before = {path: path.read_bytes() for root in (layout.vault, other.vault)
              for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    heads = {root: git(root, "rev-parse", "HEAD") for root in (layout.vault, other.vault)}

    with pytest.raises((engine.Conflict, KelmoError)):
        engine.recover_transaction(layout.control)

    assert marker.read_bytes() == marker_bytes
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert {root: git(root, "rev-parse", "HEAD") for root in heads} == heads


def test_recovery_refuses_changed_config_with_marker_and_committed_bytes_preserved(layout, monkeypatch):
    enroll(layout)
    marker = interrupt(layout, monkeypatch, 'after-commit')
    marker_bytes = marker.read_bytes()
    head = git(layout.vault, 'rev-parse', 'HEAD')
    artifacts = {path: path.read_bytes() for path in (layout.vault / REGISTRY,
                 layout.vault / GENERATION, layout.wiki / TARGET)}
    config_path = layout.control / 'kelmo.toml'
    config_path.write_text(config_path.read_text().replace('node_label = "synthetic"', 'node_label = "changed"'))

    with pytest.raises(engine.Conflict, match='binding changed'):
        engine.recover_transaction(layout.control)

    assert marker.read_bytes() == marker_bytes
    assert git(layout.vault, 'rev-parse', 'HEAD') == head
    assert all(path.read_bytes() == raw for path, raw in artifacts.items())


def test_recovery_must_refuse_when_state_root_changes_with_a_pending_marker(layout, monkeypatch):
    config = enroll(layout)
    marker = interrupt(layout, monkeypatch, 'after-commit')
    assert git(layout.vault, 'status', '--porcelain', '--', REGISTRY, GENERATION, 'knowledge/wiki') == ''
    marker_bytes = marker.read_bytes()
    recorded = json.loads(marker_bytes)
    assert recorded['binding']['roots']['state'] == str(layout.state)
    owner_path = engine._owner_path(config.wiki)
    owner_bytes = owner_path.read_bytes()
    assert json.loads(owner_bytes) == {'version': 1, 'vault_id': config.vault_id}
    new_state = layout.control.parent / 'replacement-state'
    new_state.mkdir()
    config_path = layout.control / 'kelmo.toml'
    config_path.write_text(config_path.read_text().replace(
        json.dumps(str(layout.state)), json.dumps(str(new_state))))
    assert Config.load(layout.control).state == new_state
    assert engine.transaction_path(layout.control) == new_state / marker.name
    artifacts = {path: path.read_bytes() for path in (layout.vault / REGISTRY,
                 layout.vault / GENERATION, layout.wiki / TARGET)}
    head = git(layout.vault, 'rev-parse', 'HEAD')
    index = engine.git(layout.vault, 'ls-files', '--stage', '-z', binary=True)

    try:
        with pytest.raises(engine.Conflict):
            engine.recover_transaction(layout.control)
    finally:
        assert marker.read_bytes() == marker_bytes
        assert owner_path.read_bytes() == owner_bytes
        assert not any(new_state.iterdir())
        assert git(layout.vault, 'rev-parse', 'HEAD') == head
        assert engine.git(layout.vault, 'ls-files', '--stage', '-z', binary=True) == index
        assert all(path.read_bytes() == raw for path, raw in artifacts.items())


def add_discovery_pages(layout):
    value = copy.deepcopy(layout.registry)
    paths = [f"projects/fixture/p{index:03}.md" for index in range(200)]
    paths += ["projects/fixture/a/detail.md", "projects/fixture/b/detail.md"]
    for path in paths:
        value["pages"][path] = page_entry(kind="topic", summary="", priority="",
                                          page_id=path[:-3].replace("/", "."))
        aliases = "aliases: [collision]\n" if path.endswith("detail.md") else ""
        write_bytes(layout.wiki / path, f"---\n{aliases}---\n# Exact {path}\n".encode())
    write_bytes(layout.vault / REGISTRY, engine.encoded(value).encode())
    git(layout.vault, "add", "knowledge")
    git(layout.vault, "commit", "-q", "-m", "synthetic discovery baseline")
    manifest = json.loads((layout.vault / GENERATION).read_bytes())
    sequence = manifest["sequence"]
    manifest.update(sequence=sequence + 1, parent_sequence=sequence,
                    managed_digest=managed_digest(layout.vault, "HEAD"),
                    counts={"facts": 3, "pages": 203, "notes": 3})
    write_bytes(layout.vault / GENERATION, engine.encoded(manifest).encode())
    git(layout.vault, "add", GENERATION)
    git(layout.vault, "commit", "-q", "-m", "pin discovery fixture")
    layout.registry = value
    return sorted([TARGET, *paths])


def test_project_over_200_pages_is_fully_discoverable(layout):
    expected = add_discovery_pages(layout)
    assert [row["target"] for row in pages.page_index(layout.registry, ["fixture"])["fixture"]] == expected
    view = reader(layout)
    generation = view.generation()
    first = view.pages("fixture", generation, None, 100)
    second = view.pages("fixture", generation, first.next_cursor, 100)
    third = view.pages("fixture", generation, second.next_cursor, 100)

    assert all(isinstance(batch, PageBatch) for batch in (first, second, third))
    assert [len(batch.entries) for batch in (first, second, third)] == [100, 100, 3]
    assert all(batch.total == 203 and batch.generation == generation for batch in (first, second, third))
    assert first.next_cursor and second.next_cursor and third.next_cursor is None
    assert [page.path for batch in (first, second, third) for page in batch.entries] == expected
    assert view.pages("fixture", generation, first.next_cursor, 100) == second
    assert reader(layout).pages("fixture", generation, first.next_cursor, 100) == second
    empty = view.pages("absent", generation, None, 100)
    assert empty.total == 0 and empty.entries == () and empty.next_cursor is None


def test_cursor_cannot_be_reused_for_another_generation_authority_or_project(layout):
    add_discovery_pages(layout)
    view = reader(layout)
    generation = view.generation()
    cursor = view.pages("fixture", generation, None, 2).next_cursor
    assert cursor
    for project, pin, token in (
        ("absent", generation, cursor),
        ("fixture", Generation(AUTHORITY, generation.publication_id + 1,
                               generation.manifest_sha256), cursor),
        ("fixture", Generation(AUTHORITY, generation.publication_id, "f" * 64), cursor),
        ("fixture", Generation("foreign", generation.publication_id,
                               generation.manifest_sha256), cursor),
        ("fixture", generation, "corrupt:" + cursor),
    ):
        with pytest.raises(KelmoError):
            view.pages(project, pin, token, 2)


def test_pinned_reads_survive_publication_but_old_cursor_cannot_select_the_new_generation(layout):
    enroll(layout)
    view = reader(layout)
    add_discovery_pages(layout)
    old = view.generation()
    cursor = view.pages("fixture", old, None, 2).next_cursor
    publish(layout)
    current = reader(layout).generation()

    assert current != old and current.publication_id == old.publication_id + 1
    assert reader(layout).page(TARGET, old).content == layout.original
    assert b"Changed fact" in reader(layout).page(TARGET, current).content
    with pytest.raises(KelmoError):
        reader(layout).pages("fixture", current, cursor, 2)


def test_bounded_context_names_every_omission_and_exact_expansion_returns_all_bytes(layout):
    expected = add_discovery_pages(layout)
    view = reader(layout)
    pin = view.generation()
    bundle = view.context(ContextBinding(repository_id=AUTHORITY, project="fixture"), pin,
                          ContextBudget(max_tokens=800))

    assert bundle.generation == pin
    assert bundle.freshness == "current" and bundle.complete is False
    returned = {page.path for page in bundle.pages}
    omitted = set(bundle.omissions)
    assert omitted and len(bundle.omissions) == len(omitted)
    assert not returned & omitted
    assert returned | omitted == set(expected), "bounded selection silently lost a page"
    for path in omitted | {TARGET}:
        exact = view.page(path, pin)
        assert isinstance(exact, PageSnapshot) and exact.generation == pin
        assert exact.content == (layout.wiki / path).read_bytes()
        assert exact.content_sha256 == sha(exact.content)
    exact = view.page(TARGET, pin)
    for index in range(3):
        assert f"Complete fact {index} with its exception.".encode() in exact.content


def test_discovery_resolves_aliases_and_qualified_paths_without_choosing_an_ambiguous_page(layout):
    add_discovery_pages(layout)
    view = reader(layout)
    pin = view.generation()
    assert view.page("work-alias", pin).path == TARGET
    assert view.page("projects/fixture/a/detail.md", pin).path == "projects/fixture/a/detail.md"
    assert view.page("a/detail", pin).path == "projects/fixture/a/detail.md"
    for name in ("detail", "collision"):
        with pytest.raises(KelmoError) as error:
            view.page(name, pin)
        assert error.value.code == "E_AMBIGUOUS"
        assert "projects/fixture/a/detail.md" in str(error.value)
        assert "projects/fixture/b/detail.md" in str(error.value)


def test_bound_link_audit_keeps_alias_relative_and_root_qualified_resolution(layout):
    add_discovery_pages(layout)
    source = layout.wiki / "projects/fixture/a/source.md"
    write_bytes(source, b"[[work-alias]] [[projects/fixture/b/detail]] [[detail]]\n")

    result = links.audit(layout.control, wiki_root=layout.wiki,
                         source_paths={"projects/fixture/a/source.md"})

    assert result["broken"] == [] and result["ambiguous"] == []
    assert result["edges"] == 3


def test_unrelated_bad_alias_metadata_does_not_block_literal_paths_or_hide_uncertainty(layout):
    write_bytes(layout.wiki / "old.md", b"---\naliases: [\n---\nUnmanaged bytes.\n")
    source = layout.wiki / "source.md"
    write_bytes(source, f"[[{TARGET}]]\n[[work-alias]]\n".encode())

    result = links.audit(layout.control, wiki_root=layout.wiki, source_paths={"source.md"})

    assert result["edges"] == 1
    assert result["metadata_errors"] == ["old.md"]
    assert result["unresolved"][0]["kind"] == "unavailable"
    assert result["unresolved"][0]["reason"]
    assert (layout.wiki / "old.md").read_bytes() == b"---\naliases: [\n---\nUnmanaged bytes.\n"


def test_reader_refuses_a_generation_whose_tree_digest_does_not_match(layout):
    manifest = json.loads((layout.vault / GENERATION).read_bytes())
    manifest["managed_digest"] = "0" * 64
    write_bytes(layout.vault / GENERATION, engine.encoded(manifest).encode())
    git(layout.vault, "add", GENERATION)
    git(layout.vault, "commit", "-q", "-m", "synthetic invalid digest")

    with pytest.raises(KelmoError):
        reader(layout).generation()


def test_exact_generation_reads_preserve_committed_bytes_despite_unpublished_edits(layout):
    view = reader(layout)
    pin = view.generation()
    write_bytes(layout.wiki / TARGET, b"Unpublished working copy.\n")

    assert view.page(TARGET, pin).content == layout.original
    assert reader(layout).page(TARGET, pin).content == layout.original
    assert (layout.wiki / TARGET).read_bytes() == b"Unpublished working copy.\n"


def test_context_keeps_all_ordinary_person_bytes_even_without_a_mandatory_flag(layout):
    person = fact("person.ordinary", "person.md", "An ordinary fact with an exception.")
    raw = (b"# Person\r\n\r\nOrdinary narrative.  \r\n<!-- kb -->\r\n"
           + f"- An ordinary fact with an exception. [f:{person['id']}]".encode()
           + b"\r\n<!-- /kb -->\r\nCondition and exception.\r\n")
    write_bytes(layout.wiki / "person.md", raw)
    # Stored form: an unresolved legacy fact that is not marked mandatory, on a registered person page.
    stored = dict(person, owner="legacy-import", protected=False, mandatory=False,
                  evidence_resolution="unresolved", evidence_refs=[], compatibility={})
    stored["revision"] = engine.entity_revision(stored)
    layout.registry["facts"][person["fact_key"]] = stored
    layout.registry["notes"][person["note_id"]] = {"filename": "person.md", "ark_id": person["ark_id"],
                                                   "evidence_resolution": "unresolved", "evidence_refs": [],
                                                   "compatibility": {}}
    layout.registry["pages"]["person.md"] = page_entry(layout_name="person", kind="topic", category="you",
                                                       title="Person", summary="", priority="", page_id="person")
    write_bytes(layout.vault / REGISTRY, engine.encoded(layout.registry).encode())
    git(layout.vault, "add", "knowledge")
    git(layout.vault, "commit", "-q", "-m", "ordinary person fixture")
    manifest = json.loads((layout.vault / GENERATION).read_bytes())
    sequence = manifest["sequence"]
    manifest.update(sequence=sequence + 1, parent_sequence=sequence,
                    managed_digest=managed_digest(layout.vault, "HEAD"),
                    counts={"facts": 4, "pages": 2, "notes": 4})
    write_bytes(layout.vault / GENERATION, engine.encoded(manifest).encode())
    git(layout.vault, "add", GENERATION)
    git(layout.vault, "commit", "-q", "-m", "pin ordinary person fixture")
    view = reader(layout)
    pin = view.generation()
    binding = ContextBinding(repository_id=AUTHORITY, project="fixture")

    bundle = view.context(binding, pin, ContextBudget(max_tokens=4000))

    assert bundle.mandatory_rules == (raw.decode("utf-8"),)
    assert view.page("person.md", pin).content == raw
    with pytest.raises(KelmoError) as error:
        view.context(binding, pin, ContextBudget(max_tokens=1))
    assert error.value.code == "E_BUDGET"


def test_non_utf8_tree_path_is_a_typed_generation_refusal(layout):
    import subprocess
    blob = subprocess.run(["git", "-C", str(layout.vault), "hash-object", "-w", "--stdin"],
                          input=b"Latin-1 named page.\n", capture_output=True, check=True).stdout.strip()
    # Written through the index: the name never has to exist on a UTF-8-only filesystem.
    subprocess.run([b"git", b"-C", os.fsencode(layout.vault), b"update-index", b"--add", b"--cacheinfo",
                    b"100644," + blob + b",knowledge/wiki/caf\xe9.md"], capture_output=True, check=True)
    git(layout.vault, "commit", "-q", "-m", "synthetic non-UTF-8 page name")

    with pytest.raises(KelmoError) as refused:
        reader(layout).generation()

    assert refused.value.code == "E_GENERATION"
