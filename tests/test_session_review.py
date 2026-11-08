"""Pre-clear review proves durable capture and queue state without reading content out."""
import hashlib

from kelmo.service import Service
from kelmo.session_control import configure
from kelmo.session_review import review
from kelmo.session_worker import TERMINAL
from kelmo.sessions import Sessions
from kelmo.config import Config
from test_session_capture import lines


def captured(tmp_path):
    root = tmp_path / "vault"
    Service.initialize(root, "fixture")
    source_root = tmp_path / "native"
    source_root.mkdir()
    source = source_root / "chat.jsonl"
    source.write_bytes(lines("claude", 2))
    configure(root, {"mode": "auto", "sources": {"claude": [str(source_root)]}})
    sessions = Sessions(Config.load(root))
    result = sessions.capture("claude", "native-1", source, source_root, "PreCompact")
    sessions.close()
    return root, source, result


def test_identity_review_distinguishes_preserved_snapshot_from_pending_processing(tmp_path):
    root, source, captured_result = captured(tmp_path)

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert result["status"] == "pending"
    assert result["preservation_ready"] is True
    assert result["current_transcript_checked"] is True
    assert result["semantic_completeness_guaranteed"] is False
    assert result["capture"] == {
        "status": "captured",
        "session_key": captured_result["session_key"],
        "generation": 1,
        "snapshot_hash": captured_result["snapshot_hash"],
        "records": 2,
        "partial_tail": False,
    }
    assert result["processing"]["status"] == "pending"
    assert result["processing"]["pending_jobs"][0]["state"] == "pending"
    assert "content" not in repr(result).lower()


def test_review_reports_uncaptured_tail_separately_from_jobs(tmp_path):
    root, source, _ = captured(tmp_path)
    source.write_bytes(lines("claude", 3))

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert result["status"] == "attention"
    assert result["preservation_ready"] is False
    assert result["capture"]["status"] == "uncaptured_tail"
    assert result["capture"]["captured_records"] == 2
    assert result["capture"]["current_records"] == 3
    assert result["processing"]["status"] == "pending"


def test_review_reports_terminal_and_failed_jobs_without_claiming_semantics(tmp_path):
    root, source, captured_result = captured(tmp_path)
    sessions = Sessions(Config.load(root))
    with sessions.journal.conn:
        sessions.journal.conn.execute(
            "UPDATE session_jobs SET state='needs_review' WHERE id=?", (captured_result["job_id"],)
        )
        sessions.journal.conn.execute(
            "INSERT INTO session_processing(job_id,error_code) VALUES (?,?)",
            (captured_result["job_id"], "E_EXECUTOR"),
        )
    sessions.close()

    failed = review(root, provider="claude", session_id="native-1", transcript_path=source)
    assert failed["status"] == "attention"
    assert failed["preservation_ready"] is True
    assert failed["processing"]["status"] == "failed"
    assert failed["processing"]["failures"] == [
        {"id": captured_result["job_id"], "state": "needs_review", "error_code": "E_EXECUTOR"}
    ]

    sessions = Sessions(Config.load(root))
    with sessions.journal.conn:
        sessions.journal.conn.execute(
            "UPDATE session_jobs SET state=? WHERE id=?", (next(iter(TERMINAL)), captured_result["job_id"])
        )
    sessions.close()
    complete = review(root, provider="claude", session_id="native-1", transcript_path=source)
    assert complete["status"] == "ready"
    assert complete["processing"] == {
        "status": "complete",
        "pending_jobs": [],
        "failures": [],
        "unscheduled_records": 0,
        "incomplete_tail": False,
    }
    assert complete["semantic_completeness_guaranteed"] is False


def test_global_review_reports_queue_but_cannot_assert_current_capture(tmp_path):
    root, _, captured_result = captured(tmp_path)

    result = review(root)

    assert result["status"] == "identity_required"
    assert result["preservation_ready"] is False
    assert result["current_transcript_checked"] is False
    assert result["capture"] == {"status": "not_checked"}
    assert result["processing"]["pending_jobs"][0]["id"] == captured_result["job_id"]


def test_review_is_read_only_and_does_not_create_or_migrate_state(tmp_path):
    root, source, _ = captured(tmp_path)
    journal = root / ".state/journal.sqlite3"
    before = hashlib.sha256(journal.read_bytes()).hexdigest()
    backups = sorted((root / ".state/backups").iterdir())

    review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before
    assert sorted((root / ".state/backups").iterdir()) == backups


def test_disabled_paused_and_unavailable_states_are_explicit(tmp_path):
    root, _, _ = captured(tmp_path)
    configure(root, {"mode": "off"})
    assert review(root)["status"] == "disabled"
    configure(root, {"mode": "auto", "paused": True})
    assert review(root)["status"] == "paused"

    missing = tmp_path / "missing"
    Service.initialize(missing, "missing")
    configure(missing, {"mode": "auto", "sources": {"claude": [str(tmp_path / "native")]}})
    (missing / ".state/journal.sqlite3").unlink()
    assert review(missing)["status"] == "unavailable"


def test_unenrolled_or_incomplete_identity_is_not_treated_as_captured(tmp_path):
    root, _, _ = captured(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(lines("claude"))

    invalid = review(root, provider="claude", transcript_path=outside)
    assert invalid["status"] == "attention"
    assert invalid["capture"] == {"status": "not_captured", "error_code": "E_TRANSCRIPT"}
    result = review(root, provider="claude", session_id="unknown")
    assert result["status"] == "attention"
    assert result["capture"] == {"status": "not_captured"}


def test_identity_without_transcript_only_proves_the_latest_recorded_snapshot(tmp_path):
    root, _, _ = captured(tmp_path)

    result = review(root, provider="claude", session_id="native-1")

    assert result["preservation_ready"] is True
    assert result["current_transcript_checked"] is False
    assert result["capture"]["status"] == "captured"
    assert "current transcript was not compared" in result["message"].lower()


def test_unresolved_prior_generation_remains_visible(tmp_path):
    root, source, first = captured(tmp_path)
    sessions = Sessions(Config.load(root))
    with sessions.journal.conn:
        sessions.journal.conn.execute(
            "UPDATE session_jobs SET state='needs_review' WHERE id=?", (first["job_id"],)
        )
    sessions.close()
    source.unlink()
    source.write_bytes(lines("claude", 3))
    sessions = Sessions(Config.load(root))
    second = sessions.capture("claude", "native-1", source, source.parent, "PreCompact")
    with sessions.journal.conn:
        sessions.journal.conn.execute(
            "UPDATE session_jobs SET state='wiki_unchanged' WHERE id=?", (second["job_id"],)
        )
    sessions.close()

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert result["status"] == "attention"
    assert result["processing"]["status"] == "failed"
    assert result["processing"]["failures"][0]["id"] == first["job_id"]


def test_sqlite_connection_closes_when_inspection_query_fails(tmp_path, monkeypatch):
    root, _, _ = captured(tmp_path)
    closed = []

    class BrokenConnection:
        row_factory = None

        def execute(self, _query, _parameters=()):
            raise __import__("sqlite3").DatabaseError("synthetic read failure")

        def close(self):
            closed.append(True)

    monkeypatch.setattr("kelmo.session_review.sqlite3.connect", lambda *_args, **_kwargs: BrokenConnection())

    assert review(root)["status"] == "unavailable"
    assert closed == [True]


def test_stop_below_threshold_reports_preserved_but_unscheduled_records(tmp_path):
    root = tmp_path / "vault"
    Service.initialize(root, "fixture")
    source_root = tmp_path / "native"
    source_root.mkdir()
    source = source_root / "chat.jsonl"
    source.write_bytes(lines("claude", 2))
    configure(root, {"mode": "auto", "sources": {"claude": [str(source_root)]}})
    sessions = Sessions(Config.load(root))
    captured_result = sessions.capture(
        "claude", "native-1", source, source_root, "Stop", checkpoint_turns=30
    )
    sessions.close()
    assert captured_result["job_id"] is None

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert result["status"] == "pending"
    assert result["preservation_ready"] is True
    assert result["processing"] == {
        "status": "pending",
        "pending_jobs": [],
        "failures": [],
        "unscheduled_records": 2,
        "incomplete_tail": False,
    }
    assert "2 record(s) await scheduling" in result["message"]


def test_preserved_partial_record_remains_pending_after_scheduled_work_completes(tmp_path):
    root, source, captured_result = captured(tmp_path)
    source.write_bytes(source.read_bytes() + b'{"type":')
    sessions = Sessions(Config.load(root))
    partial = sessions.capture("claude", "native-1", source, source.parent, "PreCompact")
    with sessions.journal.conn:
        sessions.journal.conn.execute(
            "UPDATE session_jobs SET state='wiki_unchanged' WHERE id IN (?,?)",
            (captured_result["job_id"], partial["job_id"]),
        )
    sessions.close()

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)

    assert result["status"] == "pending"
    assert result["preservation_ready"] is True
    assert result["capture"]["partial_tail"] is True
    assert result["processing"]["unscheduled_records"] == 0
    assert result["processing"]["incomplete_tail"] is True
    assert "incomplete record awaits a later capture" in result["message"]


def test_review_reads_a_snapshot_stored_as_a_delta_over_its_base(tmp_path):
    """A later capture of a grown transcript stores only its delta; the read-only review still proves the
    whole current transcript is preserved."""
    root, source, first = captured(tmp_path)
    grown = lines("claude", 2) + lines("claude", 1)
    source.write_bytes(grown)
    sessions = Sessions(Config.load(root))
    second = sessions.capture("claude", "native-1", source, source.parent, "PreCompact")
    stored = sessions.config.internal(".state/sessions/transcripts/" + second["snapshot_hash"] + ".jsonl")
    assert stored.read_bytes() == grown[len(lines("claude", 2)):]
    sessions.close()

    result = review(root, provider="claude", session_id="native-1", transcript_path=source)
    assert result["preservation_ready"] is True and result["current_transcript_checked"] is True
    assert result["capture"]["status"] == "captured"
    assert result["capture"]["snapshot_hash"] == second["snapshot_hash"] != first["snapshot_hash"]
    assert result["capture"]["records"] == 3
