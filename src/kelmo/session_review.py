"""Read-only preservation review for a session before client history is cleared."""
import hashlib
import json
import os
import re
import sqlite3
import stat

from .config import Config
from .errors import KelmoError
from .session_transcripts import enrolled_transcript, parse
from .sessions import read_snapshot


_PENDING = {"pending", "running", "archived"}
_FAILED = {"needs_review", "wiki_needs_review"}
_TERMINAL = {"wiki_applied", "wiki_unchanged", "skipped"}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _base(status, message):
    return {
        "status": status,
        "preservation_ready": False,
        "current_transcript_checked": False,
        "semantic_completeness_guaranteed": False,
        "capture": {"status": "not_checked"},
        "processing": {
            "status": "not_checked",
            "pending_jobs": [],
            "failures": [],
            "unscheduled_records": None,
            "incomplete_tail": None,
        },
        "message": message,
    }


def _read_regular(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise KelmoError("E_TRANSCRIPT", "Session transcript is not a regular file.")
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()
    except OSError as error:
        raise KelmoError("E_TRANSCRIPT", "Session transcript is unavailable for review.") from error


def _source(config, provider, transcript_path):
    if provider not in ("claude", "codex"):
        raise KelmoError("E_TRANSCRIPT", "A supported provider is required to review a transcript.")
    source = enrolled_transcript(transcript_path, config.sessions["sources"].get(provider, []),
                                 "Transcript is outside enrolled provider locations.")
    return _read_regular(source)


def _processing(rows, unscheduled_records=0, incomplete_tail=False):
    pending = [
        {"id": row["id"], "state": row["state"]}
        for row in rows if row["state"] in _PENDING
    ]
    failures = [
        {"id": row["id"], "state": row["state"], "error_code": row["error_code"]}
        for row in rows if row["state"] in _FAILED or row["state"] not in _PENDING | _TERMINAL
    ]
    status = (
        "failed" if failures
        else "pending" if pending or unscheduled_records or incomplete_tail
        else "complete"
    )
    return {
        "status": status,
        "pending_jobs": pending,
        "failures": failures,
        "unscheduled_records": unscheduled_records,
        "incomplete_tail": incomplete_tail,
    }


def review(root, *, provider=None, session_id=None, transcript_path=None):
    """Return metadata-only capture and processing readiness without changing the vault."""
    config = Config.load(root)
    if config.sessions["mode"] == "off":
        return _base("disabled", "Session memory is disabled; no capture coverage can be asserted.")
    if config.sessions["paused"]:
        return _base("paused", "Session memory is paused; no new capture coverage can be asserted.")

    current_raw = None
    if transcript_path is not None:
        try:
            current_raw = _source(config, provider, transcript_path)
        except KelmoError as error:
            result = _base("attention", "The supplied transcript is unavailable or not enrolled; capture it before clearing if preservation matters.")
            result["capture"] = {"status": "not_captured", "error_code": error.code}
            return result

    journal = config.internal(".state/journal.sqlite3")
    if not journal.is_file():
        return _base("unavailable", "The session journal is unavailable; preservation cannot be reviewed.")
    database = None
    try:
        database = sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"sessions", "session_snapshots", "session_jobs"}
        if not required <= tables:
            return _base("unavailable", "Session review state is unavailable; preservation cannot be reviewed.")

        progress_join = (
            "LEFT JOIN session_processing p ON p.job_id=j.id" if "session_processing" in tables
            else "LEFT JOIN (SELECT NULL job_id, NULL error_code) p ON 0"
        )
        if provider is None or session_id is None:
            rows = list(database.execute(
                f"SELECT j.id,j.state,p.error_code FROM session_jobs j {progress_join} ORDER BY j.rowid"
            ))
            unscheduled = database.execute("""SELECT COALESCE(SUM(
                CASE WHEN ss.records>s.scheduled_record THEN ss.records-s.scheduled_record ELSE 0 END),0)
                FROM sessions s JOIN session_snapshots ss ON ss.hash=s.latest_hash""").fetchone()[0]
            incomplete_tail = bool(database.execute("""SELECT 1 FROM sessions s
                JOIN session_snapshots ss ON ss.hash=s.latest_hash
                WHERE ss.partial_tail=1 LIMIT 1""").fetchone())
            processing = _processing(rows, unscheduled, incomplete_tail)
            result = _base("identity_required", "Queue state is available, but provider and session ID are required to prove the current transcript was captured.")
            result["processing"] = processing
            return result

        if provider not in ("claude", "codex") or not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", session_id):
            result = _base("attention", "The session identity is invalid; capture it before clearing if preservation matters.")
            result["capture"] = {"status": "not_captured", "error_code": "E_INPUT"}
            return result
        key = _digest(json.dumps([config.vault_id, config.label, provider, session_id]).encode())
        session = database.execute("SELECT * FROM sessions WHERE session_key=?", (key,)).fetchone()
        if session is None:
            result = _base("attention", "No durable snapshot exists for this session; capture it before clearing if preservation matters.")
            result["capture"] = {"status": "not_captured"}
            return result
        snapshot = database.execute("SELECT * FROM session_snapshots WHERE hash=?", (session["latest_hash"],)).fetchone()
        saved_raw = damaged = None
        if snapshot is not None:
            try:
                saved_raw = read_snapshot(config, database, session["latest_hash"])
                if len(saved_raw) != snapshot["size"]:
                    raise KelmoError("E_STATE", "Snapshot identity does not match its bytes.")
            except KelmoError as error:
                damaged = error
        rows = list(database.execute(
            f"SELECT j.id,j.state,p.error_code FROM session_jobs j {progress_join} "
            "WHERE j.session_key=? ORDER BY j.rowid",
            (key,),
        ))
    except sqlite3.Error:
        return _base("unavailable", "The session journal cannot be read; preservation cannot be reviewed.")
    finally:
        if database is not None:
            database.close()

    if snapshot is None:
        result = _base("unavailable", "Snapshot metadata is unavailable; preservation cannot be reviewed.")
        result["processing"] = _processing(rows)
        return result
    if damaged is not None:
        result = _base("unavailable", "The durable snapshot is missing or damaged; preservation cannot be asserted.")
        result["capture"] = {"status": "not_captured", "error_code": damaged.code}
        result["processing"] = _processing(rows)
        return result

    capture = {
        "status": "captured",
        "session_key": key,
        "generation": session["generation"],
        "snapshot_hash": session["latest_hash"],
        "records": snapshot["records"],
        "partial_tail": bool(snapshot["partial_tail"]),
    }
    preserved = True
    if current_raw is not None and current_raw != saved_raw:
        parsed = parse(provider, current_raw)
        capture = {
            "status": "uncaptured_tail" if current_raw.startswith(saved_raw[:snapshot["complete_bytes"]]) else "not_captured",
            "session_key": key,
            "generation": session["generation"],
            "snapshot_hash": session["latest_hash"],
            "captured_records": snapshot["records"],
            "current_records": parsed["records"],
            "partial_tail": bool(parsed["partial_tail"]),
        }
        preserved = False

    unscheduled = max(0, snapshot["records"] - session["scheduled_record"])
    processing = _processing(rows, unscheduled, bool(snapshot["partial_tail"]))
    if not preserved or processing["status"] == "failed":
        status = "attention"
        message = "Session memory has uncaptured transcript changes or failed jobs; run capture or retry before clearing if preservation matters."
    elif processing["status"] == "pending":
        status = "pending"
        count = len(processing["pending_jobs"])
        unscheduled = processing["unscheduled_records"]
        details = []
        if count:
            details.append(f"{count} job(s) still need processing")
        if unscheduled:
            details.append(f"{unscheduled} record(s) await scheduling")
        if processing["incomplete_tail"]:
            details.append("an incomplete record awaits a later capture")
        message = "Session snapshot is preserved; " + "; ".join(details) + ". Semantic completeness is not guaranteed."
    else:
        status = "ready"
        message = "Session snapshot is preserved; processing is complete. Semantic completeness is not guaranteed."
    current_checked = current_raw is not None
    if not current_checked:
        message += " The current transcript was not compared with that snapshot."
    return {
        "status": status,
        "preservation_ready": preserved,
        "current_transcript_checked": current_checked,
        "semantic_completeness_guaranteed": False,
        "capture": capture,
        "processing": processing,
        "message": message,
    }
