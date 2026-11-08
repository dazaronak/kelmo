"""Publisher and recovery guards: each refusal leaves HEAD, the registry and the pages untouched.

Covers page hierarchy, open work, managed links, the bound knowledge root, unfinished-transaction recovery,
hand-written pages at a fact target, and page creation over an existing file.
"""
import copy
import fcntl
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import registry_seed
from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.service import Service
from kelmo.wiki import engine, pages
from test_editor_context import fact, registry, topic, work
from test_nested_layout import REGISTRY, TARGET, interrupt, make_layout, page_entry
from test_wiki_roots import git, initialize_repository

HAND = "projects/fixture/hand.md"
EXISTING = "projects/fixture/existing.md"
OTHER_VAULT = "00000000-0000-4000-8000-000000000005"
HAND_BYTES = b"# Hand\n\nHand-written text with no managed region.\n"
LOCAL_BYTES = b"Local edit must survive.\n"
LOCK_HOLDER = (
    "import fcntl, sys\n"
    "with open(sys.argv[1], 'a') as stream:\n"
    "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.read()\n"
)


@pytest.fixture
def layout(tmp_path):
    return make_layout(tmp_path)


def skip_ownership_guard(monkeypatch):
    """Let a test's own registry change reach the publisher's file checks, as registry_seed does."""
    monkeypatch.setattr(engine, "_preserve_metadata", lambda previous, proposed, **_: copy.deepcopy(proposed))


def _entry(parent=""):
    value = {"kind": "topic", "category": "projects", "parent": parent, "title": "Page", "summary": "", "priority": ""}
    value["revision"] = pages.entry_revision(value)
    return value


CHAIN = ["projects/fixture/p%d.md" % index for index in range(5)]


def chain(length):
    return {target: _entry(CHAIN[index + 1] if index + 1 < length else "")
            for index, target in enumerate(CHAIN[:length])}


@pytest.mark.parametrize("pages_map, message", [
    ({"projects/fixture/child.md": _entry("projects/fixture/missing.md")},
     "parent projects/fixture/missing.md has no page entry"),
    ({"projects/fixture/a.md": _entry("projects/fixture/b.md"),
      "projects/fixture/b.md": _entry("projects/fixture/a.md")},
     "parent cycle through projects/fixture/a.md"),
    (chain(5), "parent chain deeper than 3"),
], ids=["missing-parent", "cycle", "too-deep"])
def test_page_hierarchy_refuses_a_missing_parent_a_cycle_and_a_chain_deeper_than_three(pages_map, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        pages.validate_pages(pages_map, {})


def test_page_hierarchy_accepts_a_chain_of_four_pages():
    four = chain(4)
    assert pages.validate_pages(four, {}) == four


def test_open_work_on_a_topic_page_is_refused_by_registry_validation():
    target = "projects/fixture/topic.md"
    with pytest.raises(ValueError, match="open work belongs on a work page"):
        registry(fact("fixture.open", target, "Open work.", status="backlog"), entries={target: topic()})


def test_publication_that_introduces_a_broken_wiki_link_is_refused_and_writes_nothing(layout):
    head = git(layout.vault, "rev-parse", "HEAD")
    before = ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes())
    proposed = copy.deepcopy(layout.registry)
    stored = proposed["facts"]["fixture.0"]
    stored["previous_revision"] = stored["revision"]
    stored["text"] = "Depends on [[no-such-page]]."
    stored["revision"] = engine.entity_revision(stored)

    with pytest.raises(ValueError, match="change introduces invalid wiki links") as raised:
        registry_seed.publish(layout.control, proposed, "chore: link to nothing")

    assert "no-such-page" in str(raised.value)
    assert git(layout.vault, "rev-parse", "HEAD") == head
    assert ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes()) == before
    assert not engine.transaction_path(layout.control).exists()


def test_knowledge_root_inside_a_parent_repository_is_refused_before_any_write(tmp_path):
    parent = tmp_path / "parent"
    initialize_repository(parent)
    data = parent / "data"
    Service.initialize(data, "fixture")
    shutil.rmtree(data / ".git")
    (parent / "parent.txt").write_text("parent baseline\n", encoding="utf-8")
    git(parent, "add", "--", "parent.txt")
    git(parent, "commit", "-q", "-m", "parent baseline")
    head = git(parent, "rev-parse", "HEAD")
    status = git(parent, "status", "--porcelain")
    registry_path = data / REGISTRY
    before = registry_path.read_bytes()
    config = Config.load(data)

    with pytest.raises(engine.Conflict, match="Knowledge root must match its bound repository root"):
        engine._commit_registry(config.root, engine.initial_registry(config.vault_id), "chore: must not escape",
                                config.wiki)

    assert git(parent, "rev-parse", "HEAD") == head
    assert git(parent, "status", "--porcelain") == status
    assert registry_path.read_bytes() == before
    assert not engine.transaction_path(data).exists()


def test_recovery_refuses_local_edits_over_an_unfinished_transaction_and_keeps_everything(layout, monkeypatch):
    marker = interrupt(layout, monkeypatch, "before-commit")
    marker_bytes = marker.read_bytes()
    (layout.wiki / TARGET).write_bytes(LOCAL_BYTES)
    head = git(layout.vault, "rev-parse", "HEAD")
    registry_bytes = (layout.vault / REGISTRY).read_bytes()

    with pytest.raises(engine.Conflict, match="Unfinished wiki transaction conflicts with local edits"):
        engine.recover_transaction(layout.control)

    assert marker.read_bytes() == marker_bytes
    assert (layout.wiki / TARGET).read_bytes() == LOCAL_BYTES
    assert (layout.vault / REGISTRY).read_bytes() == registry_bytes
    assert git(layout.vault, "rev-parse", "HEAD") == head


def test_recovery_refuses_an_invalid_marker_and_preserves_it(layout, monkeypatch):
    marker = interrupt(layout, monkeypatch, "before-commit")
    marker.write_text('{"version": 2, "title"', encoding="utf-8")
    marker_bytes = marker.read_bytes()
    before = ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes())
    head = git(layout.vault, "rev-parse", "HEAD")

    with pytest.raises(engine.Conflict, match="Invalid unfinished wiki transaction"):
        engine.recover_transaction(layout.control)

    assert marker.read_bytes() == marker_bytes
    assert ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes()) == before
    assert git(layout.vault, "rev-parse", "HEAD") == head


def with_hand_fact(layout):
    """The registered state plus one fact whose target is a hand-written page the registry does not own yet."""
    facts = [fact(f"fixture.{index}", TARGET, f"Complete fact {index} with its exception.", role="constraint")
             for index in range(3)]
    facts.append(fact("fixture.hand", HAND, "A fact on a page a person wrote by hand."))
    spec = {"facts": {item["fact_key"]: item for item in facts},
            "notes": {item["note_id"]: {"filename": "source.md", "ark_id": item["ark_id"]} for item in facts}}
    return registry_seed.build(spec, authority_id=layout.authority, pages_settings={
        TARGET: dict(work(), page_id="fixture-work", layout="legacy"),
        HAND: dict(topic(), page_id="fixture-hand", layout="legacy"),
    })


def test_untracked_hand_written_page_at_a_fact_target_is_refused_not_adopted(layout):
    # The first-adoption branch of the publisher cannot run: a publication whose wiki is not inside the data
    # repository is refused, and the wiki repository is always the data repository once it passes.
    # So the observable behavior for this case is the refusal, pinned here.
    (layout.wiki / HAND).write_bytes(HAND_BYTES)
    head = git(layout.vault, "rev-parse", "HEAD")
    before = (layout.vault / REGISTRY).read_bytes()

    with pytest.raises(engine.DirtyKnowledge) as raised:
        registry_seed.publish(layout.control, with_hand_fact(layout), "chore: fact on a hand page")

    assert "knowledge/wiki/" + HAND in str(raised.value)
    assert git(layout.vault, "rev-parse", "HEAD") == head
    assert (layout.vault / REGISTRY).read_bytes() == before
    assert (layout.wiki / HAND).read_bytes() == HAND_BYTES
    assert git(layout.vault, "status", "--porcelain", "--", "knowledge/wiki/" + HAND) == "?? knowledge/wiki/" + HAND
    assert not engine.transaction_path(layout.control).exists()


def test_created_page_cannot_claim_an_existing_file_and_the_file_stays_as_it_was(layout, monkeypatch):
    existing = layout.wiki / EXISTING
    existing.write_bytes(HAND_BYTES)
    head = git(layout.vault, "rev-parse", "HEAD")
    before = (layout.vault / REGISTRY).read_bytes()
    proposed = copy.deepcopy(layout.registry)
    proposed["pages"][EXISTING] = page_entry(kind="topic", title="Existing", summary="", priority="",
                                             page_id="fixture-existing")
    skip_ownership_guard(monkeypatch)

    with pytest.raises(engine.OwnershipConflict, match="a created page cannot claim an existing file") as raised:
        engine._commit_registry(layout.control, proposed, "chore: create page", layout.wiki)

    assert EXISTING in str(raised.value)
    assert existing.read_bytes() == HAND_BYTES
    assert git(layout.vault, "rev-parse", "HEAD") == head
    assert (layout.vault / REGISTRY).read_bytes() == before
    assert not engine.transaction_path(layout.control).exists()


def test_recovery_waits_for_a_wiki_lock_held_by_another_process(layout, monkeypatch):
    marker = interrupt(layout, monkeypatch, "before-commit")
    lock = engine._wiki_metadata(layout.wiki) / "kelmo.lock"
    holder = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, str(lock)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    original = fcntl.flock
    waited = []

    def contended(stream, flags):
        if Path(stream.name).name != "kelmo.lock":
            return original(stream, flags)
        with pytest.raises(BlockingIOError):
            original(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert marker.exists()
        holder.stdin.close()
        assert holder.wait(timeout=20) == 0
        waited.append(True)
        return original(stream, flags)

    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(engine.fcntl, "flock", contended)
        engine.recover_transaction(layout.control)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=20)
        holder.stdin.close()
        holder.stdout.close()

    assert waited == [True]
    assert not marker.exists()
    assert (layout.wiki / TARGET).read_bytes() == layout.original
    assert git(layout.vault, "rev-parse", "HEAD") == layout.head


def test_recovery_refuses_an_owner_record_that_names_another_vault(layout, monkeypatch):
    marker = interrupt(layout, monkeypatch, "before-commit")
    marker_bytes = marker.read_bytes()
    engine._owner_path(layout.wiki).write_text(engine.encoded({"version": 1, "vault_id": OTHER_VAULT}),
                                               encoding="utf-8")
    before = ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes())
    head = git(layout.vault, "rev-parse", "HEAD")

    with pytest.raises(KelmoError) as raised:
        engine.recover_transaction(layout.control)

    assert raised.value.code == "E_WIKI_OWNER"
    assert marker.read_bytes() == marker_bytes
    assert ((layout.vault / REGISTRY).read_bytes(), (layout.wiki / TARGET).read_bytes()) == before
    assert git(layout.vault, "rev-parse", "HEAD") == head
