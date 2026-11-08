"""Archive adoption behavioral gates. SQLite/Git fixtures are executed only by the controller.

Exercise adoption/1's real local adapter; missing capabilities are failures, never
skips. Embedding boundaries use failures or synthetic bytes, never a model. Legacy
chunks are already-derived fixture data, not a requirement on the retrieval engine.
"""

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace

import pytest


STORED = "2026-10-14T09:00:00+00:00"
UPDATED = "2026-10-14T10:00:00+00:00"
ORIGINAL = "quokka original\r\nЗафиксировано: e\u0301.\r\n".encode()


def packet(content=ORIGINAL, **fields):
    from kelmo.contracts import SourcePacket

    return replace(
        SourcePacket(
            content,
            "raw_note",
            "source-original",
            "2026-04-18_original.md",
            "2026-04-18T12:00:00+02:00",
            kind="decision",
            domain="dev",
            project="fixture",
            tags=("a%_", "kept"),
            valid_from="2026-04-17",
        ),
        **fields,
    )


@pytest.fixture
def offline(monkeypatch):
    from kelmo.store.errors import ArchiveEmbeddingError

    clock = [STORED]

    def now():
        return clock[0]

    def unavailable(*args, **kwargs):
        raise ArchiveEmbeddingError("synthetic model outage")

    monkeypatch.setattr("kelmo.store.db.now_iso", now)
    monkeypatch.setattr("kelmo.store.memory.embed.embed", unavailable)
    monkeypatch.setattr("kelmo.store.memory.store.embed", unavailable)
    monkeypatch.setattr("kelmo.store.memory.search.embed", unavailable)
    # Patch cached clock aliases too, without importing the missing update capability.
    for name in ("kelmo.store.memory.store", "kelmo.store.memory.update"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "now_iso"):
            monkeypatch.setattr(module, "now_iso", now)
    return clock


@pytest.fixture
def archive(tmp_path, offline):
    from kelmo.store.db import init_schema, open_connection
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.server import Enrollment, OriginBindings, WriterIdentity

    path = tmp_path / "archive.db"
    connection = open_connection(str(path))
    try:
        init_schema(connection)
    finally:
        connection.close()
    bindings = OriginBindings(
        "fixture-authority",
        WriterIdentity("fixture-authority", "writer", 1),
        (Enrollment("local", "fixture-node"), Enrollment("other", "other-node")),
    )
    adapter = LocalArchiveAdapter(path, tmp_path / "cache", bindings)
    try:
        yield adapter, bindings.origin("local"), path
    finally:
        adapter.close()


def no_embedding(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A metadata-only change or no-op must not embed.")

    monkeypatch.setattr("kelmo.store.memory.store.embed", forbidden)


def test_capture_preserves_exact_bytes_metadata_and_distinct_dates(archive):
    adapter, origin, _ = archive
    source = packet()
    ref = adapter.capture(source, origin, "capture")
    stored = adapter.get(ref.archive_id, 1)
    assert (ref.version, ref.content_sha256, ref.source_id) == (
        1,
        hashlib.sha256(ORIGINAL).hexdigest(),
        "source-original",
    )
    assert stored.source == source
    assert stored.source.content == ORIGINAL
    assert stored.origin == origin
    assert stored.stored_at == STORED and stored.vectors_pending is True
    row = adapter.public_record(ref.archive_id)
    assert (row["created_at"], row["updated_at"], row["valid_from"]) == (
        STORED,
        STORED,
        "2026-04-17",
    )
    assert row["raw_note_path"] == hashlib.sha256(ORIGINAL).hexdigest() + "-2026-04-18_original.md"


@pytest.mark.parametrize(
    "source_kind, explicit, wanted",
    [
        ("raw_note", None, "2026-04-18"),
        ("raw_note", "2026-01-02", "2026-01-02"),
        ("session_digest", None, None),
    ],
)
def test_capture_validity_is_explicit_or_raw_note_date(archive, source_kind, explicit, wanted):
    adapter, origin, _ = archive
    source = packet(source_kind=source_kind, valid_from=explicit)
    ref = adapter.capture(source, origin, "dated")
    version = adapter.get(ref.archive_id)
    assert version.source == source
    assert adapter.public_record(ref.archive_id)["valid_from"] == wanted
    assert version.stored_at == STORED
    assert version.source.observed_at == "2026-04-18T12:00:00+02:00"


def test_inferred_validity_keeps_original_metadata_through_update_and_retry(archive, monkeypatch):
    adapter, origin, _ = archive
    source = packet(valid_from=None)
    first = adapter.capture(source, origin, "original-dated")
    no_embedding(monkeypatch)
    explicit = replace(source, valid_from="2026-04-18")
    second = adapter.update(first.archive_id, 1, explicit, origin)
    assert second.version == 2
    assert adapter.get(first.archive_id, 1).source == source
    assert adapter.get(first.archive_id).source == explicit
    assert adapter.capture(source, origin, "original-dated") == first
    assert adapter.verify_receipt(first, "original-dated", origin).source == source
    assert adapter.public_record(first.archive_id)["valid_from"] == "2026-04-18"


@pytest.mark.parametrize("newer_head", [False, True])
def test_capture_prefers_exact_metadata_version_and_keeps_cross_origin_owners(
    archive, monkeypatch, newer_head
):
    adapter, origin, _ = archive
    other = adapter.bindings.origin("other")
    source_a = packet(valid_from=None)
    first = adapter.capture(source_a, origin, "capture-a")
    assert adapter.capture(source_a, other, "other-owner-a") == first
    original = adapter.verify_receipt(first, "capture-a", origin)
    no_embedding(monkeypatch)
    source_b = replace(source_a, tags=("revision-b",), valid_from="2026-04-18")
    second = adapter.update(first.archive_id, 1, source_b, origin)
    if newer_head:
        adapter.update(first.archive_id, 2, replace(source_b, tags=("revision-c",)), origin)
    head = adapter.get(first.archive_id)
    captured_b = adapter.capture(source_b, other, "other-owner-b")
    assert captured_b == second and captured_b.version == 2
    assert adapter.verify_receipt(captured_b, "other-owner-b", other).source == source_b
    assert adapter.verify_receipt(captured_b, "other-owner-b", other).origin == origin
    assert adapter.capture(source_b, other, "other-owner-b") == second
    assert adapter.verify_receipt(first, "capture-a", origin) == original
    assert adapter.verify_receipt(first, "other-owner-a", other) == original
    assert adapter.get(first.archive_id) == head
    assert adapter.counts()["archive_records"] == 1


@pytest.mark.parametrize("require_vectors", [False, True])
def test_new_capture_prefers_an_identical_current_version_over_replaced_history(
    archive, monkeypatch, require_vectors
):
    adapter, origin, _ = archive
    monkeypatch.setattr("kelmo.store.memory.store.embed", lambda *args, **kwargs: bytes(1536))
    source = packet(require_vectors=require_vectors)
    first = adapter.capture(source, origin, "original-a")
    adapter.update(first.archive_id, 1, replace(source, tags=("b",)), origin)
    current = adapter.update(first.archive_id, 2, source, origin)
    no_embedding(monkeypatch)

    assert adapter.capture(source, origin, "new-a") == current
    assert adapter.capture(source, origin, "original-a") == first
    assert adapter.verify_receipt(first, "original-a", origin).source == source
    assert adapter.counts()["archive_records"] == 1


def test_vector_required_capture_reuses_the_indexed_current_bytes_for_legacy_metadata(
    archive, monkeypatch
):
    adapter, origin, _ = archive
    monkeypatch.setattr("kelmo.store.memory.store.embed", lambda *args, **kwargs: bytes(1536))
    source = packet()
    first = adapter.capture(source, origin, "original")
    current = adapter.update(first.archive_id, 1, replace(source, tags=("current",)), origin)
    later = replace(source, observed_at="2026-04-19T12:00:00+02:00", require_vectors=True)
    no_embedding(monkeypatch)

    assert adapter.capture(later, origin, "later") == current
    assert adapter.get(first.archive_id).source.tags == ("current",)
    assert adapter.verify_receipt(first, "original", origin).source == source


def test_dedup_preserves_unattested_legacy_metadata_in_both_source_tables(archive):
    adapter, origin, _ = archive
    original = packet()
    first = adapter.capture(original, origin, "pre-upgrade")
    with adapter.connection:
        adapter.connection.execute("DELETE FROM archive_receipts")
        adapter.connection.execute("DELETE FROM archive_version_sources")
        adapter.connection.execute("DELETE FROM archive_sources")
    legacy = adapter.get(first.archive_id)
    incoming = replace(legacy.source, filename=original.filename,
                       observed_at="2026-04-19T12:00:00+02:00", tags=("new-owner-context",))

    ref = adapter.capture(incoming, origin, "after-upgrade")

    stored = adapter.connection.execute(
        "SELECT source_packet,origin FROM archive_sources WHERE archive_id=?",
        (first.archive_id,),
    ).fetchone()
    assert json.loads(stored["source_packet"]) == legacy.source.to_dict()
    assert json.loads(stored["origin"]) == legacy.origin.to_dict()
    assert adapter.verify_receipt(ref, "after-upgrade", origin).origin == legacy.origin


def test_same_id_update_keeps_all_original_evidence_and_metadata(archive, offline):
    adapter, origin, _ = archive
    first = adapter.capture(packet(), origin, "capture")
    original = adapter.get(first.archive_id)
    fixed = adapter.public_record(first.archive_id)
    offline[0] = UPDATED
    changed = packet(
        b"walrus correction\r\n",
        source_id="source-corrected",
        kind="event",
        domain="life",
        project="other",
        tags=("changed",),
        valid_from="2026-05-02",
        observed_at="2026-05-03T11:00:00+00:00",
    )
    second = adapter.update(first.archive_id, 1, changed, origin)
    assert (second.archive_id, second.version, second.source_id, second.content_sha256) == (
        first.archive_id,
        2,
        "source-corrected",
        hashlib.sha256(b"walrus correction\r\n").hexdigest(),
    )
    assert adapter.get(first.archive_id).source == changed
    assert adapter.get(first.archive_id, 1) == original
    assert adapter.verify_receipt(first, "capture", origin) == original
    historical = adapter.receipt_record(first, "capture", origin)
    for name in (
        "id",
        "content",
        "kind",
        "domain",
        "project",
        "tags_json",
        "valid_from",
        "source",
        "machine_tag",
        "created_at",
        "raw_note_path",
    ):
        assert historical[name] == fixed[name], name
    assert (historical["version"], historical["written_at"], historical["replaced_at"]) == (
        1,
        STORED,
        UPDATED,
    )
    head = adapter.public_record(first.archive_id)
    assert (head["created_at"], head["updated_at"]) == (STORED, UPDATED)
    assert head["raw_note_path"] == fixed["raw_note_path"]
    assert adapter.search_records("quokka", _filters(), 10, "lexical")["items"] == []
    assert [
        row["id"] for row in adapter.search_records("walrus", _filters(), 10, "lexical")["items"]
    ] == [first.archive_id]
    assert adapter.counts() == {
        "archive_records": 1,
        "forgotten_records": 0,
        "superseded_records": 0,
    }


def _filters():
    from kelmo.contracts import SearchFilters

    return SearchFilters()


def test_noop_keeps_version_dates_history_and_receipt_without_embedding(
    archive, monkeypatch, offline
):
    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    before = adapter.public_record(ref.archive_id)
    no_embedding(monkeypatch)
    offline[0] = UPDATED
    assert adapter.update(ref.archive_id, 1, packet(), origin) == ref
    assert adapter.public_record(ref.archive_id) == before
    assert adapter.verify_receipt(ref, "capture", origin).source == packet()
    from kelmo.store.memory.get import memory_get
    from kelmo.store.memory.update import memory_update

    assert memory_get(adapter.connection, id=ref.archive_id, full=True).get("versions", []) == []
    assert memory_update(
        adapter.connection,
        id=ref.archive_id,
        content=ORIGINAL.decode(),
        tags=["a%_", "kept"],
        expected_version=1,
    ) == {"id": ref.archive_id, "version": 1, "unchanged": True}


def test_metadata_only_update_versions_metadata_without_embedding(archive, monkeypatch):
    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    no_embedding(monkeypatch)
    corrected = packet(tags=("corrected",), valid_from="2026-01-02")
    second = adapter.update(ref.archive_id, 1, corrected, origin)
    assert (second.archive_id, second.version, second.content_sha256) == (
        ref.archive_id,
        2,
        ref.content_sha256,
    )
    assert adapter.get(ref.archive_id).source == corrected
    assert adapter.get(ref.archive_id, 1).source == packet()


def test_metadata_update_keeps_current_vector_but_text_update_invalidates_it(archive, monkeypatch):
    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    rowid = adapter.connection.execute(
        "SELECT rowid FROM memories WHERE id=?", (ref.archive_id,)
    ).fetchone()[0]
    vector = bytes(1536)
    with adapter.connection:
        adapter.connection.execute(
            "INSERT INTO memories_vec(rowid,embedding) VALUES (?,?)", (rowid, vector)
        )
    with monkeypatch.context() as metadata_only:
        no_embedding(metadata_only)
        second = adapter.update(ref.archive_id, 1, packet(tags=("corrected",)), origin)
    assert adapter.get(ref.archive_id).vectors_pending is False
    assert (
        adapter.connection.execute(
            "SELECT embedding FROM memories_vec WHERE rowid=?", (rowid,)
        ).fetchone()[0]
        == vector
    )
    adapter.update(ref.archive_id, second.version, packet(b"replacement\n"), origin)
    assert adapter.get(ref.archive_id).vectors_pending is True
    assert adapter.get(ref.archive_id, second.version).vectors_pending is False
    assert adapter.connection.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
    monkeypatch.setattr("kelmo.store.memory.search.embed", lambda *args, **kwargs: vector)
    assert adapter.search_records("quokka", _filters(), 10, "semantic")["items"] == []


def test_reopen_keeps_exact_history_receipt_and_head_metadata(archive):
    from kelmo.archive_adapter import LocalArchiveAdapter

    adapter, origin, path = archive
    first = adapter.capture(packet(), origin, "capture")
    original = adapter.get(first.archive_id)
    corrected = packet(b"new head\r\n", project=None, tags=(), source_id="new-source")
    second = adapter.update(first.archive_id, 1, corrected, origin)
    adapter.hide(first.archive_id, origin, "hide")
    reopened = LocalArchiveAdapter(path, adapter.cache_dir, adapter.bindings)
    try:
        assert reopened.verify_receipt(first, "capture", origin) == original
        assert reopened.capture(packet(), origin, "capture") == first
        assert reopened.capture(packet(), origin, "later-owner") == first
        assert reopened.verify_receipt(first, "later-owner", origin) == original
        head = reopened._row(second.archive_id)
        assert (head["version"], head["project"], head["tags_json"]) == (2, None, "[]")
    finally:
        reopened.close()


def test_update_failure_rolls_back_history_source_metadata_and_text(archive):
    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    before = adapter.get(ref.archive_id)
    with adapter.connection:
        adapter.connection.execute("""CREATE TRIGGER reject_revision BEFORE INSERT ON
            archive_version_sources WHEN new.version=2 BEGIN
            SELECT RAISE(ABORT, 'synthetic receipt storage failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="synthetic receipt storage failure"):
        adapter.update(ref.archive_id, 1, packet(b"must roll back\n"), origin)
    assert adapter.get(ref.archive_id) == before
    assert adapter.history(ref.archive_id, None, 10).versions == ()
    assert adapter.verify_receipt(ref, "capture", origin) == before
    assert [
        item["id"] for item in adapter.search_records("quokka", _filters(), 10, "lexical")["items"]
    ] == [ref.archive_id]


def test_update_rechecks_writer_after_embedding(archive, monkeypatch):
    from kelmo.store.db import open_connection
    from kelmo.errors import KelmoError

    adapter, origin, path = archive
    ref = adapter.capture(packet(), origin, "capture")

    def advance_epoch(*args, **kwargs):
        with closing(open_connection(str(path))) as other, other:
            other.execute("UPDATE archive_writer_state SET epoch=epoch+1")
        return bytes(1536)

    monkeypatch.setattr("kelmo.store.memory.store.embed", advance_epoch)
    with pytest.raises(KelmoError) as stale:
        adapter.update(ref.archive_id, 1, packet(b"stale writer\n"), origin)
    assert stale.value.code == "E_WRITER"
    assert adapter.public_record(ref.archive_id)["version"] == 1
    assert adapter.history(ref.archive_id, None, 10).versions == ()


@pytest.mark.parametrize("mutation", ["update", "capture", "hide", "supersede"])
def test_archive_mutation_refuses_a_caller_transaction_without_finishing_it(archive, mutation):
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    replacement = adapter.capture(packet(b"replacement\n"), origin, "replacement")
    calls = {
        "update": lambda: adapter.update(ref.archive_id, 1, packet(), origin),
        "capture": lambda: adapter.capture(packet(), origin, "pending"),
        "hide": lambda: adapter.hide(ref.archive_id, origin, "pending-hide", reason="fixture"),
        "supersede": lambda: adapter.supersede(replacement.archive_id, ref.archive_id, origin),
    }
    connection = adapter.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("INSERT INTO archive_meta VALUES ('caller-partial','preserve')")
        with pytest.raises(KelmoError) as refused:
            calls[mutation]()
        assert refused.value.code == "E_STATE"
        assert connection.in_transaction
        assert (
            connection.execute("SELECT value FROM archive_meta WHERE key='caller-partial'").fetchone()[
                0
            ]
            == "preserve"
        )
    finally:
        connection.rollback()


@pytest.mark.parametrize("same_as_head", [False, True])
def test_stale_expected_version_refuses_even_an_otherwise_noop(archive, same_as_head):
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    winner = packet(b"winner\n", source_id="winner-source")
    second = adapter.update(ref.archive_id, 1, winner, origin)
    before = adapter.get(ref.archive_id)
    with pytest.raises(KelmoError) as refused:
        adapter.update(ref.archive_id, 1, winner if same_as_head else packet(b"loser\n"), origin)
    assert refused.value.code == "E_CONFLICT"
    assert adapter.get(ref.archive_id) == before and before.evidence == second
    assert adapter.get(ref.archive_id, 1).source.content == ORIGINAL


@pytest.mark.parametrize("requested_content", [b"loser\n", ORIGINAL])
def test_expected_version_is_rechecked_when_a_second_connection_wins(
    archive, monkeypatch, requested_content
):
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.errors import KelmoError

    adapter, origin, path = archive
    ref = adapter.capture(packet(), origin, "capture")
    competitor = LocalArchiveAdapter(path, adapter.cache_dir, adapter.bindings)
    connection = adapter.connection
    winners = []

    class InterleavedConnection:
        def execute(self, sql, *args):
            cursor = connection.execute(sql, *args)
            if (
                "SELECT" in sql.upper()
                and "FROM memories" in sql
                and not connection.in_transaction
                and not winners
            ):
                winners.append(competitor.update(ref.archive_id, 1, packet(b"winner\n"), origin))
            return cursor

        def __getattr__(self, name):
            return getattr(connection, name)

        def __enter__(self):
            connection.__enter__()
            return self

        def __exit__(self, *args):
            return connection.__exit__(*args)

    try:
        with monkeypatch.context() as interleave:
            interleave.setattr(adapter, "connection", InterleavedConnection())
            with pytest.raises(KelmoError) as refused:
                adapter.update(ref.archive_id, 1, packet(requested_content), origin)
            assert refused.value.code == "E_CONFLICT"
        assert len(winners) == 1
        assert adapter.get(ref.archive_id).evidence == winners[0]
        assert adapter.get(ref.archive_id).source.content == b"winner\n"
        assert adapter.get(ref.archive_id, 1).source.content == ORIGINAL
        with pytest.raises(KelmoError) as missing:
            adapter.get(ref.archive_id, 3)
        assert missing.value.code == "E_NOT_FOUND"
    finally:
        competitor.close()


def test_history_pages_return_exact_old_versions_without_duplicates(archive):
    adapter, origin, _ = archive
    first = adapter.capture(packet(), origin, "capture")
    second = adapter.update(first.archive_id, 1, packet(b"second\n"), origin)
    third = adapter.update(first.archive_id, 2, packet(b"third\n"), origin)
    expected_refs = {1: first, 2: second, 3: third}
    expected_sources = {1: packet(), 2: packet(b"second\n"), 3: packet(b"third\n")}
    page = adapter.history(first.archive_id, None, 1)
    assert page == adapter.history(first.archive_id, None, 1)
    versions, cursors = [], set()
    for _ in range(3):
        assert len(page.versions) == 1
        versions.extend(page.versions)
        if page.next_cursor is None:
            break
        assert page.next_cursor not in cursors
        cursors.add(page.next_cursor)
        page = adapter.history(first.archive_id, page.next_cursor, 1)
    else:
        pytest.fail("History did not terminate within the three stored versions.")
    numbers = [item.evidence.version for item in versions]
    assert len(set(numbers)) == len(numbers)
    # Archive history enumerates replaced versions; get returns the current head.
    assert numbers == [1, 2]
    assert adapter.get(first.archive_id).evidence == third
    for item in versions:
        number = item.evidence.version
        assert item.evidence == expected_refs[number]
        assert item.source == expected_sources[number]
        assert item.origin == origin and item.vectors_pending is True


def test_history_cursor_remains_bounded_when_another_update_arrives(archive):
    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    adapter.update(ref.archive_id, 1, packet(b"second\n"), origin)
    adapter.update(ref.archive_id, 2, packet(b"third\n"), origin)
    page = adapter.history(ref.archive_id, None, 1)
    assert [v.evidence.version for v in page.versions] == [1]
    assert page.next_cursor is not None
    adapter.update(ref.archive_id, 3, packet(b"fourth\n"), origin)
    tail = adapter.history(ref.archive_id, page.next_cursor, 1)
    assert [v.evidence.version for v in tail.versions] == [2]
    assert tail.next_cursor is None
    assert [v.evidence.version for v in adapter.history(ref.archive_id, None, 10).versions] == [
        1,
        2,
        3,
    ]


def test_hidden_history_and_exact_reads_require_the_original_owning_receipt(archive):
    from kelmo.contracts import Origin
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    original = adapter.get(ref.archive_id)
    adapter.update(ref.archive_id, 1, packet(b"corrected\n"), origin)
    hidden = adapter.hide(ref.archive_id, origin, "hide")
    assert adapter.hide(ref.archive_id, origin, "hide") == hidden
    for read in (
        lambda: adapter.public_record(ref.archive_id),
        lambda: adapter.get(ref.archive_id),
        lambda: adapter.get(ref.archive_id, 1),
        lambda: adapter.history(ref.archive_id, None, 10),
    ):
        with pytest.raises(KelmoError) as refused:
            read()
        assert refused.value.code == "E_FORGOTTEN"
    assert adapter.list_records(10, 0) == {"items": [], "total": 0}
    assert adapter.search_records("corrected", _filters(), 10, "lexical")["items"] == []
    assert adapter.verify_receipt(ref, "capture", origin) == original
    for forged, owner, principal, code in (
        (ref, "other-operation", origin, "E_RECEIPT"),
        (replace(ref, version=2), "capture", origin, "E_RECEIPT"),
        (replace(ref, content_sha256="0" * 64), "capture", origin, "E_RECEIPT"),
        (replace(ref, source_id="wrong-source"), "capture", origin, "E_RECEIPT"),
        (ref, "capture", adapter.bindings.origin("other"), "E_RECEIPT"),
        (ref, "capture", Origin.from_dict(origin.to_dict()), "E_ORIGIN"),
    ):
        with pytest.raises(KelmoError) as refused:
            adapter.verify_receipt(forged, owner, principal)
        assert refused.value.code == code


def test_capture_replay_after_update_keeps_the_first_exact_version(archive):
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    assert adapter.capture(packet(), origin, "duplicate-owner") == ref
    second = adapter.update(ref.archive_id, 1, packet(b"corrected\n"), origin)
    adapter.hide(ref.archive_id, origin, "hide")
    assert adapter.capture(packet(), origin, "capture") == ref
    assert adapter.capture_receipt("capture", origin) == ref
    assert adapter.capture_receipt("duplicate-owner", origin) == ref
    with pytest.raises(KelmoError) as refused:
        adapter.capture(packet(b"changed request\n"), origin, "capture")
    assert refused.value.code == "E_IDEMPOTENCY"
    assert adapter.verify_receipt(ref, "capture", origin).source.content == ORIGINAL
    assert adapter.verify_receipt(ref, "duplicate-owner", origin).source.content == ORIGINAL
    assert adapter.counts()["archive_records"] == 1
    assert second.version == 2


def test_list_filters_before_pagination_with_fact_dates_and_exact_tags(archive):
    from kelmo.store.memory.list import memory_list

    adapter, origin, _ = archive
    refs = []
    # Each decoy fails exactly one predicate; no other filter may mask a regression.
    sources = [
        packet(),
        packet(b"target two\n"),
        packet(b"fallback date\n", valid_from=None, source_kind="session_digest"),
        packet(b"wrong kind\n", kind="event"),
        packet(b"wrong domain\n", domain="life"),
        packet(b"wrong project\n", project="other"),
        packet(b"wildcard decoy\n", tags=("aXY", "kept")),
        packet(b"missing second tag\n", tags=("a%_",)),
        packet(b"too early\n", valid_from="2026-04-16"),
        packet(b"too late\n", valid_from="2026-04-18"),
        packet(b"wrong machine\n"),
        packet(b"hidden\n"),
    ]
    for index, source in enumerate(sources):
        principal = adapter.bindings.origin("other") if index == 10 else origin
        refs.append(
            adapter.capture(
                replace(source, source_id=f"list-source-{index}"), principal, f"list-{index}"
            )
        )
    adapter.hide(refs[11].archive_id, origin, "hide-list")
    filters = dict(
        kind="decision",
        domain="dev",
        project="fixture",
        tags=["a%_", "kept"],
        machine_tag="fixture-node",
        since="2026-04-17",
        until="2026-04-17",
        order_by="valid_from",
        order="asc",
        exclude_forgotten=True,
    )
    wanted = sorted(ref.archive_id for ref in refs[:2])
    pages = [
        memory_list(adapter.connection, **filters, limit=1, offset=offset) for offset in (0, 1, 2)
    ]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [row["id"] for page in pages for row in page["items"]] == wanted
    assert pages == [
        memory_list(adapter.connection, **filters, limit=1, offset=offset) for offset in (0, 1, 2)
    ]
    desc = memory_list(adapter.connection, **dict(filters, order="desc"))
    assert [row["id"] for row in desc["items"]] == list(reversed(wanted))
    fallback = memory_list(
        adapter.connection,
        project="fixture",
        since="2026-10-14",
        until="2026-10-14",
        exclude_forgotten=True,
    )
    assert [row["id"] for row in fallback["items"]] == [refs[2].archive_id]
    plain = [adapter.list_records(1, offset) for offset in range(12)]
    assert {page["total"] for page in plain} == {11}
    assert [row["id"] for page in plain for row in page["items"]] == sorted(
        (ref.archive_id for ref in refs[:11]), reverse=True
    )


def test_chunk_window_is_bounded_ordered_and_not_fabricated_for_history(archive):
    from kelmo.store.errors import ArchiveValidationError
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    content = "\n\n".join(f"part {i}" for i in range(8)).encode()
    ref = adapter.capture(packet(content), origin, "capture")
    # Simulate imported derived chunks using the current archive storage shape.
    with adapter.connection:
        adapter.connection.execute("""CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(id),
            idx INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(memory_id, idx))""")
        adapter.connection.executemany(
            "INSERT INTO chunks(memory_id, idx, text) VALUES (?,?,?)",
            [(ref.archive_id, i, f"part {i}") for i in range(8)],
        )
    old_locator = _locator(ref)
    expanded = adapter.expand(old_locator, 1)
    assert expanded.locator == old_locator
    assert expanded.start_index == 1 and expanded.chunks == ("part 1", "part 2", "part 3")
    from kelmo.store.memory.get import memory_get

    for index, neighbors, wanted in (
        (2, 0, [2]),
        (2, 1, [1, 2, 3]),
        (0, 2, [0, 1, 2]),
        (7, 2, [5, 6, 7]),
        (4, 50, [1, 2, 3, 4, 5, 6, 7]),
        (4, -5, [4]),
    ):
        window = memory_get(
            adapter.connection, id=ref.archive_id, chunk_idx=index, neighbors=neighbors
        )
        assert window["chunk_total"] == 8 and "content" not in window
        assert window["chunks"] == [{"chunk_idx": i, "text": f"part {i}"} for i in wanted]
    assert (
        memory_get(adapter.connection, id=ref.archive_id, full=True)["content"].encode() == content
    )
    for index in (-1, 8):
        with pytest.raises(ArchiveValidationError):
            memory_get(adapter.connection, id=ref.archive_id, chunk_idx=index)
    adapter.update(ref.archive_id, 1, packet(b"replacement\n"), origin)
    assert adapter.get(ref.archive_id, 1).source.content == content
    with pytest.raises(KelmoError) as refused:
        adapter.expand(old_locator, 1)
    assert refused.value.code == "E_CONFLICT"
    adapter.hide(ref.archive_id, origin, "hide-chunks")
    with pytest.raises(KelmoError) as hidden:
        adapter.expand(old_locator, 1)
    assert hidden.value.code == "E_FORGOTTEN"


def _locator(ref):
    from kelmo.contracts import ChunkLocator

    return ChunkLocator(ref.archive_id, ref.version, ref.content_sha256, "core-record-index/1", 2)


@pytest.fixture
def kelmo_service(tmp_path, monkeypatch, offline):
    from kelmo.service import Service

    for name, value in {
        "GIT_AUTHOR_NAME": "Synthetic Fixture",
        "GIT_COMMITTER_NAME": "Synthetic Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "vault"
    Service.initialize(root, "fixture-node", project='fixture')
    with Service(root) as service:
        yield service


@pytest.mark.parametrize("mutation", ["claim", "release", "handover", "stage", "retry", "capture"])
def test_journal_mutation_refuses_a_caller_transaction_without_finishing_it(kelmo_service, mutation):
    from kelmo.errors import KelmoError

    service = kelmo_service
    journal = service.journal
    operation = service.capture_source(packet(), service.origin, "journal-caller")
    journal.stage(operation["id"], "failed", terminal=True)
    calls = {
        "claim": lambda: journal.claim(operation["id"]),
        "release": lambda: journal.release(operation["id"], "fixture-token"),
        "handover": lambda: journal.handover_local_claim(operation["id"]),
        "stage": lambda: journal.stage(operation["id"], "queued"),
        "retry": lambda: service.retry(operation["id"]),
        "capture": lambda: service.capture_source(packet(), service.origin, "pending-capture"),
    }
    journal.conn.execute("BEGIN IMMEDIATE")
    try:
        journal.event(operation["id"], "caller-partial")
        with pytest.raises(KelmoError) as refused:
            calls[mutation]()
        assert refused.value.code == "E_STATE"
        assert journal.conn.in_transaction
        assert journal.conn.execute(
            "SELECT COUNT(*) FROM events WHERE stage='caller-partial'"
        ).fetchone()[0] == 1
    finally:
        journal.conn.rollback()
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM events WHERE stage='caller-partial'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("read", ["get", "history"])
def test_missing_history_is_refused_without_fabricating_a_version(archive, read):
    from kelmo.errors import KelmoError

    adapter, origin, _ = archive
    ref = adapter.capture(packet(), origin, "capture")
    adapter.update(ref.archive_id, 1, packet(b"changed\n"), origin)
    with adapter.connection:
        adapter.connection.execute("DELETE FROM memory_versions WHERE memory_id=?", (ref.archive_id,))
    with pytest.raises(KelmoError) as refused:
        if read == "get":
            adapter.get(ref.archive_id, 1)
        else:
            adapter.history(ref.archive_id, None, 10)
    assert refused.value.code == "E_CAPABILITY"
    assert adapter.connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0


def test_typed_dedup_keeps_canonical_metadata_and_each_original_operation_packet(kelmo_service):
    service = kelmo_service
    original = packet()
    later = replace(original, observed_at="2026-04-19T12:00:00+02:00", tags=("later-context",))
    first = service.capture_source(original, service.origin, "first-typed")
    second = service.capture_source(later, service.origin, "second-typed")
    archived_first = service.run(first["id"])
    archived_second = service.run(second["id"])
    assert archived_first["state"] == archived_second["state"] == "archived"
    assert archived_first["archive_id"] == archived_second["archive_id"]
    assert service.archive_api.get(archived_first["archive_id"]).source == original
    assert service.source_packet(first["id"]) == original
    assert service.source_packet(second["id"]) == later


def test_missing_internal_exact_reference_refuses_and_retry_recovers_the_real_receipt(
    kelmo_service, monkeypatch
):
    service = kelmo_service
    source = packet(source_id="explicit-synthetic-event")
    operation = service.capture_source(source, service.origin, "missing-reference")
    original_archive = service._archive

    def without_reference(*args):
        receipt = dict(original_archive(*args))
        receipt.pop("evidence_ref")
        return receipt

    monkeypatch.setattr(service, "_archive", without_reference)
    failed = service.run(operation["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "E_RECEIPT"
    monkeypatch.setattr(service, "_archive", original_archive)
    recovered = service.run(service.retry(operation["id"])["id"])
    assert recovered["state"] == "archived"
    assert service.journal.evidence_record(operation["id"])["source_id"] == source.source_id
    assert service.status()["archive_records"] == 1


def test_wiki_retry_after_head_update_uses_original_hidden_receipt_once(kelmo_service, monkeypatch):
    from kelmo.wiki import engine
    from test_service import note

    service = kelmo_service
    content = note(service)
    op = service.capture(content, "retry.md", "lead", "promote")
    with monkeypatch.context() as interruption:

        def fail_commit(*args, **kwargs):
            raise RuntimeError("synthetic publication interruption")

        interruption.setattr(engine, "_commit_registry", fail_commit)
        failed = service.run(op["id"])
    assert failed["state"] == "failed" and failed["durable_stage"] == "archived"
    adapter = service.archive_api
    original = adapter.get(failed["archive_id"])
    adapter.update(
        failed["archive_id"],
        1,
        replace(original.source, content=b"unrelated new head\n", source_id="corrected-source"),
        service.origin,
    )
    service.forget(failed["archive_id"])
    assert service.archive_record(failed["archive_id"], operation_id=op["id"])["content"] == content
    assert service.journal.evidence_record(op["id"]) == original.evidence.to_dict()
    no_embedding(monkeypatch)
    result = service.run(service.retry(op["id"])["id"])
    assert result["state"] == "applied" and result["archive_id"] == failed["archive_id"]
    assert service.run(op["id"])["state"] == "applied"
    assert service.status()["facts"] == 1 and service.status()["archive_records"] == 1
    assert (
        adapter.verify_receipt(
            original.evidence, op["id"], service._operation_origin(op["id"])
        ).source.content
        == content.encode()
    )


def test_legacy_dedup_accepts_changed_role_and_observation_without_rewriting_evidence(
    kelmo_service, monkeypatch
):
    service = kelmo_service
    monkeypatch.setattr("kelmo.service.now", lambda: STORED)
    first = service.capture(ORIGINAL.decode(), "legacy-replay.md", "implementer")
    archived = service.run(first["id"])
    adapter = service.archive_api
    original = adapter.get(archived["archive_id"])
    no_embedding(monkeypatch)
    monkeypatch.setattr("kelmo.service.now", lambda: UPDATED)
    later = service.capture(ORIGINAL.decode(), "legacy-replay.md", "lead")
    assert later["id"] != first["id"]
    replayed = service.run(later["id"])
    assert replayed["state"] == "archived" and replayed["archive_id"] == archived["archive_id"]
    assert later["created_at"] == UPDATED and later["role"] == "lead"
    assert original.source.observed_at == STORED
    assert "role:implementer" in original.source.tags
    assert (
        adapter.capture_receipt(later["id"], service._operation_origin(later["id"]))
        == original.evidence
    )
    assert (
        adapter.verify_receipt(
            original.evidence, first["id"], service._operation_origin(first["id"])
        )
        == original
    )
    assert (
        adapter.verify_receipt(
            original.evidence, later["id"], service._operation_origin(later["id"])
        )
        == original
    )
    assert adapter.get(archived["archive_id"]) == original
    assert service.status()["archive_records"] == 1


def remove_journal_evidence(service, operation_id):
    with service.journal.conn:
        for event in service.trace(operation_id)["events"]:
            if event["stage"] == "archived":
                event["details"].pop("evidence_ref", None)
                service.journal.conn.execute(
                    "UPDATE events SET details=? WHERE sequence=?",
                    (json.dumps(event["details"]), event["sequence"]),
                )


@pytest.mark.parametrize("owned_version", [1, 2])
@pytest.mark.parametrize("journal_has_exact_ref", [True, False])
def test_service_retry_keeps_verified_explicit_source_and_exact_version(
    kelmo_service, monkeypatch, owned_version, journal_has_exact_ref
):
    service = kelmo_service
    adapter = service.archive_api
    source = packet(source_id="event-1", tags=("retry-boundary",), valid_from=None)
    if owned_version == 2:
        seed = adapter.capture(replace(source, tags=("earlier-metadata",)), service.origin, "seed")
        adapter.update(seed.archive_id, 1, source, service.origin)
    op = service.capture_source(source, service.origin, "typed-retry")
    archived = service.run(op["id"])
    assert archived["state"] == "archived"
    original = adapter.get(archived["archive_id"], owned_version)
    ref = original.evidence
    assert (ref.version, ref.source_id) == (owned_version, "event-1")
    assert original.source == source
    assert service.journal.evidence_record(op["id"]) == ref.to_dict()
    adapter.update(
        ref.archive_id,
        owned_version,
        replace(source, content=b"unrelated current head\n", source_id="head-source"),
        service.origin,
    )
    service.forget(ref.archive_id)
    if not journal_has_exact_ref:
        remove_journal_evidence(service, op["id"])
        assert service.journal.evidence_record(op["id"]) is None
    # Model a dependent failure after the accepted archived stage, without
    # changing its captured source, trusted origin or operation identity.
    service.journal.stage(
        op["id"], "failed", terminal=True, error_code="E_PROCESSING", phase="after_archive"
    )
    no_embedding(monkeypatch)
    result = service.run(service.retry(op["id"])["id"])
    assert result["state"] == "archived" and result["archive_id"] == ref.archive_id
    assert service.journal.evidence_record(op["id"]) == ref.to_dict()
    assert adapter.capture_receipt(op["id"], service._operation_origin(op["id"])) == ref
    assert (
        service.archive_record(ref.archive_id, operation_id=op["id"])["content"].encode()
        == source.content
    )
    assert adapter.verify_receipt(ref, op["id"], service._operation_origin(op["id"])) == original
    assert service.run(op["id"])["state"] == "archived"
    assert service.status()["archive_records"] == 1


def test_service_retry_binds_true_legacy_fallback_when_no_exact_receipt_exists(
    kelmo_service, monkeypatch
):
    service = kelmo_service
    op = service.capture(ORIGINAL.decode(), "legacy-retry.md", "implementer")
    archived = service.run(op["id"])
    original = service.archive_api.get(archived["archive_id"])
    ref = original.evidence
    assert (ref.version, ref.source_id) == (1, hashlib.sha256(ORIGINAL).hexdigest())
    remove_journal_evidence(service, op["id"])
    with service.archive:
        service.archive.execute(
            "DELETE FROM archive_receipts WHERE operation_id=?", (op["id"],)
        )
    assert (
        service.archive_api.capture_receipt(op["id"], service._operation_origin(op["id"])) is None
    )
    service.journal.stage(
        op["id"], "failed", terminal=True, error_code="E_PROCESSING", phase="after_archive"
    )
    no_embedding(monkeypatch)
    result = service.run(service.retry(op["id"])["id"])
    assert result["state"] == "archived" and result["archive_id"] == ref.archive_id
    assert service.journal.evidence_record(op["id"]) == ref.to_dict()
    assert (
        service.archive_api.verify_receipt(ref, op["id"], service._operation_origin(op["id"]))
        == original
    )
    assert (
        service.archive_record(ref.archive_id, operation_id=op["id"])["content"].encode()
        == ORIGINAL
    )
    assert service.status()["archive_records"] == 1


@pytest.fixture
def legacy_archive(tmp_path, offline):
    from kelmo.store.db import init_schema, open_connection
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.server import OriginBindings

    path = tmp_path / "legacy.db"
    connection = open_connection(str(path))
    try:
        connection.executescript("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL, domain TEXT NOT NULL,
            project TEXT, tags_json TEXT NOT NULL, source TEXT, valid_from TEXT,
            machine_tag TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            raw_note_path TEXT, version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE memory_versions (
            memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL,
            content TEXT NOT NULL, kind TEXT NOT NULL, domain TEXT NOT NULL, project TEXT,
            tags_json TEXT NOT NULL, valid_from TEXT, written_at TEXT NOT NULL,
            replaced_at TEXT NOT NULL, PRIMARY KEY(memory_id, version));""")
        connection.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-id",
                "new head\n",
                "event",
                "dev",
                "new-project",
                '["new"]',
                "raw_note",
                "2026-05-01",
                "legacy-node",
                STORED,
                UPDATED,
                "notes/2026-04-18_legacy.md",
                2,
            ),
        )
        connection.execute(
            "INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-id",
                1,
                ORIGINAL.decode(),
                "decision",
                "life",
                None,
                '["old", "a%_"]',
                None,
                STORED,
                UPDATED,
            ),
        )
        connection.commit()
        init_schema(connection)
        init_schema(connection)
    finally:
        connection.close()
    bindings = OriginBindings.local("legacy-authority", "fixture-node")
    adapter = LocalArchiveAdapter(path, tmp_path / "cache", bindings)
    try:
        yield adapter, bindings.origin("local"), path
    finally:
        adapter.close()


def test_legacy_history_preserves_missing_fields_dates_and_unattested_origin(legacy_archive):
    adapter, _, _ = legacy_archive
    old = adapter.get("legacy-id", 1)
    assert old.source.content == ORIGINAL
    assert (
        old.source.kind,
        old.source.domain,
        old.source.project,
        old.source.tags,
        old.source.valid_from,
    ) == ("decision", "life", None, ("old", "a%_"), None)
    assert old.stored_at == STORED
    assert old.origin.client_id == "legacy-unattested" and old.origin.machine_tag == "legacy-node"
    assert old.evidence.source_id == "notes/2026-04-18_legacy.md"
    assert adapter.get("legacy-id").evidence.version == 2
    from kelmo.store.memory.get import memory_get

    row = memory_get(adapter.connection, id="legacy-id", version=1)
    assert (
        row["created_at"],
        row["written_at"],
        row["replaced_at"],
        row["raw_note_path"],
        row["source"],
    ) == (STORED, STORED, UPDATED, "notes/2026-04-18_legacy.md", "raw_note")
    assert row["project"] is None and row["valid_from"] is None


def test_legacy_receipt_binding_stays_unresolved_until_exact_controller_attestation(legacy_archive):
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.contracts import EvidenceRef
    from kelmo.errors import KelmoError

    adapter, origin, path = legacy_archive
    ref = EvidenceRef(
        "legacy-id", 1, hashlib.sha256(ORIGINAL).hexdigest(), "notes/2026-04-18_legacy.md"
    )
    assert adapter.capture_receipt("legacy-operation", origin) is None
    with pytest.raises(KelmoError) as unresolved:
        adapter.bind_legacy_receipt(ref, "legacy-operation", origin)
    assert unresolved.value.code == "E_RECEIPT"
    assert adapter.capture_receipt("legacy-operation", origin) is None
    attested = LocalArchiveAdapter(
        path,
        adapter.cache_dir,
        adapter.bindings,
        legacy_receipt_verifier=lambda evidence, operation, principal: (
            evidence == ref and operation == "legacy-operation" and principal is origin
        ),
    )
    try:
        with pytest.raises(KelmoError) as wrong:
            attested.bind_legacy_receipt(replace(ref, version=2), "legacy-operation", origin)
        assert wrong.value.code == "E_RECEIPT"
        attested.bind_legacy_receipt(ref, "legacy-operation", origin)
        attested.hide("legacy-id", origin, "hide-legacy")
        assert attested.verify_receipt(ref, "legacy-operation", origin).source.content == ORIGINAL
    finally:
        attested.close()


def test_legacy_binding_refuses_caller_transaction_without_rolling_back_pending_writes(
    legacy_archive,
):
    from kelmo.archive_adapter import LocalArchiveAdapter
    from kelmo.contracts import EvidenceRef
    from kelmo.errors import KelmoError

    adapter, origin, path = legacy_archive
    ref = EvidenceRef(
        "legacy-id", 1, hashlib.sha256(ORIGINAL).hexdigest(), "notes/2026-04-18_legacy.md"
    )
    attested = LocalArchiveAdapter(
        path,
        adapter.cache_dir,
        adapter.bindings,
        legacy_receipt_verifier=lambda evidence, operation, principal: (
            evidence == ref and operation == "legacy-operation" and principal is origin
        ),
    )
    connection = attested.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO archive_meta VALUES ('caller-binding-partial','preserve')")
        with pytest.raises(KelmoError) as refused:
            attested.bind_legacy_receipt(ref, "legacy-operation", origin)
        assert refused.value.code == "E_STATE"
        assert connection.in_transaction
        assert (
            connection.execute(
                "SELECT value FROM archive_meta WHERE key='caller-binding-partial'"
            ).fetchone()[0]
            == "preserve"
        )
        assert connection.execute("SELECT COUNT(*) FROM archive_receipts").fetchone()[0] == 0
        connection.rollback()
        assert attested.capture_receipt("legacy-operation", origin) is None
        attested.bind_legacy_receipt(ref, "legacy-operation", origin)
        assert attested.verify_receipt(ref, "legacy-operation", origin).source.content == ORIGINAL
    finally:
        connection.rollback()
        attested.close()


def import_source(path, *, memory_type="architectural_decision", metadata=None):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""CREATE TABLE memory (
            id INTEGER PRIMARY KEY, content TEXT NOT NULL, tags TEXT, memory_type TEXT,
            metadata TEXT, created_at TEXT NOT NULL)""")
        connection.execute(
            "INSERT INTO memory VALUES (?,?,?,?,?,?)",
            (
                7,
                ORIGINAL.decode(),
                '["kept"]',
                memory_type,
                json.dumps(
                    metadata
                    or {"domain": "life", "project": "fixture", "machine_tag": "legacy-node"}
                ),
                "2026-04-18T12:00:00+02:00",
            ),
        )


def import_v10_source(path, rows):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""CREATE TABLE memories (
            id INTEGER PRIMARY KEY, content TEXT NOT NULL, tags TEXT, memory_type TEXT,
            metadata TEXT, created_at TEXT NOT NULL, created_at_iso TEXT, deleted_at TEXT)""")
        connection.executemany(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    identity,
                    ORIGINAL.decode(),
                    '["kept"]',
                    memory_type,
                    '{"domain":"dev"}',
                    "2026-04-18T12:00:00+02:00",
                    "2026-04-18T12:00:00+02:00",
                    deleted_at,
                )
                for identity, memory_type, deleted_at in rows
            ],
        )


