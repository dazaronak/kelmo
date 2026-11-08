"""Final archive boundaries: supersession links, import refusal, ISO days and failure reporting."""
from pathlib import Path
import sqlite3
import sys

import kelmo
import pytest

from kelmo.contracts import SearchFilters, SourcePacket
from kelmo.errors import KelmoError
from kelmo.service import Service

sys.path.insert(0, str(Path(kelmo.__file__).resolve().parents[2]/'tests'))
import test_archive_adoption as fixtures  # noqa: E402

archive = fixtures.archive
offline = fixtures.offline


@pytest.fixture
def service(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    Service.initialize(root, "fixture")
    monkeypatch.setattr("kelmo.store.memory.store.embed", lambda *args, **kwargs: bytes(1536))
    with Service(root) as value:
        yield value


def archived(service, content):
    return service.run(service.capture(content, "source.md", "implementer")["id"])["archive_id"]


def test_reverse_supersession_is_refused(service):
    left, right = archived(service, "Original evidence"), archived(service, "Corrected evidence")
    service.archive_api.supersede(right, left, service.origin)
    with pytest.raises(KelmoError) as refused:
        service.archive_api.supersede(left, right, service.origin)
    assert refused.value.code == "E_CONFLICT"
    assert service.show(left)["supersedes"] is None


def test_longer_supersession_cycle_is_refused(service):
    first, second, third = (archived(service, text) for text in ("First", "Second", "Third"))
    service.archive_api.supersede(second, first, service.origin)
    service.archive_api.supersede(third, second, service.origin)
    with pytest.raises(KelmoError) as refused:
        service.archive_api.supersede(first, third, service.origin)
    assert refused.value.code == "E_CONFLICT"
    assert service.show(first)["supersedes"] is None
    assert service.show(third)["superseded_by"] == []


def test_partial_supersession_cannot_grow_two_visible_successors(service, monkeypatch):
    original = archived(service, "Original evidence")
    first = service.capture("First correction", "source.md", "implementer", supersedes=original)
    second = service.capture("Second correction", "source.md", "implementer", supersedes=original)
    with monkeypatch.context() as failing:
        def cannot_hide(*args, **kwargs):
            raise KelmoError("E_IO", "Synthetic interrupted hide")
        failing.setattr(service, "_forget", cannot_hide)
        interrupted = service.run(first["id"])
    assert interrupted["state"] == "failed" and interrupted["error_code"] == "E_IO"
    assert service.archive_api.capture_receipt(first["id"], service.origin) is not None
    rejected = service.run(second["id"])
    assert rejected["state"] == "failed" and rejected["error_code"] == "E_CONFLICT"
    assert not rejected["archive_id"]
    retried = service.run(service.retry(first["id"])["id"])
    assert retried["state"] == "archived"
    assert service.show(retried["archive_id"])["superseded_by"] == []


def test_capture_refuses_a_target_that_already_has_a_linked_correction(service, monkeypatch):
    original = archived(service, "Original evidence")
    first = service.capture("First correction", "source.md", "implementer", supersedes=original)
    with monkeypatch.context() as failing:
        failing.setattr(service, "_forget", lambda *args, **kwargs: (_ for _ in ()).throw(
            KelmoError("E_IO", "Synthetic interrupted hide")))
        service.run(first["id"])
    with pytest.raises(KelmoError) as refused:
        service.capture("Late correction", "source.md", "implementer", supersedes=original)
    assert refused.value.code == "E_CONFLICT"


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("key", [None, "client-correction"])
def test_identical_correction_capture_replays_after_its_effect(service, monkeypatch, interrupted, key):
    original = archived(service, "Original evidence")
    def capture():
        return service.capture("Correction", "source.md", "implementer",
                               supersedes=original, idempotency_key=key)
    operation = capture()
    with monkeypatch.context() as interruption:
        if interrupted:
            def fail(*args, **kwargs):
                raise KelmoError("E_IO", "Synthetic interrupted hide")
            interruption.setattr(service.archive_api, "hide", fail)
        result = service.run(operation["id"])
    assert result["state"] == ("failed" if interrupted else "archived")
    before = service.status()["archive_records"]
    assert capture()["id"] == operation["id"]
    assert service.status()["archive_records"] == before == 2
    if interrupted:
        assert service.run(service.retry(operation["id"])["id"])["state"] == "archived"


@pytest.mark.parametrize("day", ["٢٠٢٦-04-17", "2026-04-1٧"])
def test_non_ascii_dates_fail_at_the_public_contract(day):
    with pytest.raises(ValueError):
        SourcePacket(b"Evidence", "raw_note", "source", "source.md", "2026-04-17T10:00:00Z", valid_from=day)
    with pytest.raises(ValueError):
        SearchFilters(since=day)


@pytest.mark.parametrize("day", ["٢٠٢٦-04-17", "2026-04-1٧"])
def test_non_ascii_search_dates_are_input_errors(service, day):
    with pytest.raises(KelmoError) as refused:
        service.search("evidence", since=day)
    assert refused.value.code == "E_INPUT"


def test_ascii_iso_days_keep_their_serialized_contract():
    packet = SourcePacket(b"Evidence", "raw_note", "source", "source.md", "2026-04-17T10:00:00Z",
                          valid_from="2026-04-17")
    assert SourcePacket.from_dict(packet.to_dict()) == packet
    assert SearchFilters(since="2026-04-17").backend() == {"since": "2026-04-17"}


def test_schema_failure_closes_the_new_journal(tmp_path, monkeypatch):
    from kelmo import service as module
    root = tmp_path / "vault"
    Service.initialize(root, "fixture")
    journals = []
    original = module.Journal
    def track(*args, **kwargs):
        journal = original(*args, **kwargs)
        journals.append(journal)
        return journal
    def fail(*args, **kwargs):
        raise KelmoError("E_IO", "Synthetic schema failure")
    monkeypatch.setattr(module, "Journal", track)
    monkeypatch.setattr(module.fact_checks, "schema", fail)
    try:
        with pytest.raises(KelmoError):
            Service(root)
        assert len(journals) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            journals[0].conn.execute("SELECT 1")
    finally:
        for journal in journals:
            journal.close()


def test_resolution_creation_failure_is_staged_with_the_first_failure(service, monkeypatch):
    from kelmo import fact_checks
    from kelmo.resolution import Resolution
    from test_service import note
    operation = service.capture(note(service), "promotion.md", "lead", "promote")
    def stale(*args, **kwargs):
        raise KelmoError("E_FACT_CHECK", "Synthetic stale fact check")
    def busy(*args, **kwargs):
        raise KelmoError("E_BUSY", "Synthetic resolution queue busy")
    monkeypatch.setattr(fact_checks, "validate", stale)
    monkeypatch.setattr(Resolution, "create", busy)
    failed = service.run(operation["id"])
    assert failed["state"] == "failed" and failed["terminal"] is True
    assert failed["error_code"] == "E_BUSY" and failed["durable_stage"] == "archived"
    last = service.trace(operation["id"])["events"][-1]
    assert last["stage"] == "failed" and last["details"]["phase"] == "resolution_create"
    assert "E_FACT_CHECK" in failed["outcome"]["reason"] and "queue busy" in failed["outcome"]["reason"]
    assert service.journal.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert service.source_path(failed).read_text(encoding="utf-8") == service.source_packet(
        operation["id"]).content.decode()
    assert service.archive_api.capture_receipt(operation["id"], service.origin) is not None
    assert service.status()["archive_records"] == 1


def test_plain_duplicate_capture_does_not_embed(archive, monkeypatch):
    adapter, origin, _ = archive
    first = adapter.capture(fixtures.packet(), origin, "first-plain")
    def forbidden(*args, **kwargs):
        raise AssertionError("A plain duplicate must reuse the stored record without embedding.")
    monkeypatch.setattr("kelmo.store.memory.store.embed", forbidden)
    assert adapter.capture(fixtures.packet(), origin, "second-plain") == first
    assert adapter.counts()["archive_records"] == 1
