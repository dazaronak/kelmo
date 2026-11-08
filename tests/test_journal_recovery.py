"""Journal recovery and transactional idempotency."""
import json
import os
from pathlib import Path
import sys
from threading import Barrier, Thread

import pytest


def _product_import_root():
    configured = os.environ.get("KELMO_IMPORT_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1] / "src"


def _service_type():
    import_root = _product_import_root()
    assert import_root.is_dir(), f"Selected source root does not exist: {import_root}"
    sys.path.insert(0, str(import_root))
    from kelmo.service import Service
    import kelmo.service as selected_service

    assert Path(selected_service.__file__).resolve().is_relative_to(import_root)
    return Service


@pytest.fixture
def service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Synthetic Fixture",
        "GIT_COMMITTER_NAME": "Synthetic Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(key, raising=False)

    Service = _service_type()
    from kelmo.store.memory import store

    monkeypatch.setattr(store, "embed", lambda *args, **kwargs: bytes(1536))
    root = tmp_path / "synthetic-vault"
    Service.initialize(root, "journal-recovery-fixture")
    with Service(root) as instance:
        yield instance


def _stage_events(journal, operation_id, stage):
    rows = journal.conn.execute(
        "SELECT details FROM events WHERE operation_id=? AND stage=? ORDER BY sequence",
        (operation_id, stage),
    ).fetchall()
    return [json.loads(row["details"]) for row in rows]


def test_forget_retry_repairs_missing_forgotten_event(service, monkeypatch):
    operation = service.capture("synthetic forget evidence\n", "forget.md", "implementer")
    archived = service.run(operation["id"])
    assert archived["state"] == "archived" and archived["archive_id"]
    assert service.archive_api.hidden_at(archived["archive_id"]) is None
    assert service.status()["archive_records"] == 1

    original_event = service.journal.event

    def fail_after_hide(operation_id, stage, **details):
        if stage == "forgotten":
            raise RuntimeError("injected missing forgotten event")
        return original_event(operation_id, stage, **details)

    monkeypatch.setattr(service.journal, "event", fail_after_hide)
    with pytest.raises(RuntimeError, match="injected missing forgotten event"):
        service.forget(archived["archive_id"], reason="synthetic test")
    marker = service.archive_api.hidden_at(archived["archive_id"])
    assert marker is not None
    assert _stage_events(service.journal, operation["id"], "forgotten") == []

    monkeypatch.setattr(service.journal, "event", original_event)
    repeated = service.forget(archived["archive_id"], reason="synthetic test")
    assert repeated["forgotten"] is False
    events = _stage_events(service.journal, operation["id"], "forgotten")
    assert events == [{"record_id": archived["archive_id"], "forgotten_at": marker}]
    service.forget(archived["archive_id"], reason="synthetic test")
    assert _stage_events(service.journal, operation["id"], "forgotten") == events


def test_supersession_retry_repairs_both_events(service, monkeypatch):
    predecessor = service.capture("synthetic predecessor\n", "old.md", "implementer")
    predecessor_result = service.run(predecessor["id"])
    old_record = predecessor_result["archive_id"]
    assert predecessor_result["state"] == "archived" and old_record
    assert service.archive_api.hidden_at(old_record) is None

    replacement = service.capture(
        "synthetic replacement\n", "new.md", "implementer", supersedes=old_record
    )
    assert replacement["state"] == "queued" and replacement["supersedes"] == old_record
    assert service.status()["archive_records"] == 1

    original_event = service.journal.event

    def fail_after_hide(operation_id, stage, **details):
        if stage == "forgotten":
            raise RuntimeError("injected missing forgotten event")
        return original_event(operation_id, stage, **details)

    monkeypatch.setattr(service.journal, "event", fail_after_hide)
    failed = service.run(replacement["id"])
    assert failed["state"] == "failed"
    assert service.archive_api.hidden_at(old_record) is not None
    assert _stage_events(service.journal, predecessor["id"], "forgotten") == []
    assert _stage_events(service.journal, replacement["id"], "superseded") == []

    monkeypatch.setattr(service.journal, "event", original_event)
    service.retry(replacement["id"])
    finished = service.run(replacement["id"])
    assert finished["state"] == "archived" and finished["archive_id"]
    assert service.archive_api.hidden_at(old_record) is not None
    assert service.status()["archive_records"] == 2
    assert _stage_events(service.journal, predecessor["id"], "forgotten") == [
        {"record_id": old_record, "forgotten_at": service.archive_api.hidden_at(old_record)}
    ]
    assert _stage_events(service.journal, replacement["id"], "superseded") == [
        {"record_id": finished["archive_id"], "superseded_id": old_record}
    ]


def test_event_once_serializes_concurrent_retries_without_duplicate(service):
    from kelmo.journal import Journal

    operation = service.capture("synthetic concurrent event\n", "event.md", "implementer")
    barrier = Barrier(3)
    results = []
    errors = []

    def retry_event():
        journal = Journal(service.config.state / "journal.sqlite3")
        try:
            barrier.wait(timeout=10)
            results.append(
                journal.event_once(
                    operation["id"], "forgotten", check_writer=lambda: None,
                    record_id="synthetic-record", forgotten_at="synthetic-marker",
                )
            )
        except BaseException as error:  # propagate thread failures to pytest
            errors.append(error)
        finally:
            journal.close()

    threads = [Thread(target=retry_event, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results) == [False, True]
    assert _stage_events(service.journal, operation["id"], "forgotten") == [
        {"record_id": "synthetic-record", "forgotten_at": "synthetic-marker"}
    ]
    assert service.journal.event_once(
        operation["id"], "forgotten", check_writer=lambda: None,
        record_id="synthetic-record", forgotten_at="synthetic-marker",
    ) is False
    assert len(_stage_events(service.journal, operation["id"], "forgotten")) == 1


def test_event_once_rejects_stale_writer_before_insert(service):
    from kelmo.errors import KelmoError

    operation = service.capture("synthetic stale writer\n", "stale.md", "implementer")

    def stale_writer():
        raise KelmoError("E_WRITER", "Synthetic stale writer epoch.")

    with pytest.raises(KelmoError) as refused:
        service.journal.event_once(
            operation["id"], "forgotten", check_writer=stale_writer,
            record_id="synthetic-record", forgotten_at="synthetic-marker",
        )
    assert refused.value.code == "E_WRITER"
    assert _stage_events(service.journal, operation["id"], "forgotten") == []


def test_event_once_conflict_preserves_existing_event_details(service):
    from kelmo.errors import KelmoError

    operation = service.capture("synthetic conflicting event\n", "conflict.md", "implementer")
    original = {"record_id": "synthetic-record", "forgotten_at": "first-marker"}
    assert service.journal.event_once(
        operation["id"], "forgotten", check_writer=lambda: None, **original
    ) is True

    with pytest.raises(KelmoError) as refused:
        service.journal.event_once(
            operation["id"], "forgotten", check_writer=lambda: None,
            record_id="synthetic-record", forgotten_at="different-marker",
        )
    assert refused.value.code == "E_IDEMPOTENCY"
    assert _stage_events(service.journal, operation["id"], "forgotten") == [original]


def test_event_once_and_forget_preserve_caller_transaction(service):
    from kelmo.errors import KelmoError

    operation = service.capture("synthetic transaction boundary\n", "transaction.md", "implementer")
    archived = service.run(operation["id"])
    record_id = archived["archive_id"]
    assert record_id and service.archive_api.hidden_at(record_id) is None

    service.journal.conn.execute("BEGIN")
    service.journal.event(operation["id"], "caller-owned-sentinel", marker="pending")
    with pytest.raises(KelmoError) as helper_refused:
        service.journal.event_once(
            operation["id"], "forgotten", check_writer=lambda: None,
            record_id=record_id, forgotten_at="synthetic-marker",
        )
    assert helper_refused.value.code == "E_STATE"
    assert service.journal.conn.in_transaction
    assert _stage_events(service.journal, operation["id"], "caller-owned-sentinel") == [
        {"marker": "pending"}
    ]
    service.journal.conn.rollback()
    assert _stage_events(service.journal, operation["id"], "caller-owned-sentinel") == []

    service.journal.conn.execute("BEGIN")
    service.journal.event(operation["id"], "caller-owned-sentinel", marker="pending")
    with pytest.raises(KelmoError) as forget_refused:
        service.forget(record_id, reason="must refuse before archive mutation")
    assert forget_refused.value.code == "E_STATE"
    assert service.journal.conn.in_transaction
    assert service.archive_api.hidden_at(record_id) is None
    assert _stage_events(service.journal, operation["id"], "caller-owned-sentinel") == [
        {"marker": "pending"}
    ]
    service.journal.conn.rollback()
    assert service.archive_api.hidden_at(record_id) is None
    assert _stage_events(service.journal, operation["id"], "caller-owned-sentinel") == []
