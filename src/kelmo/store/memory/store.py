"""memory_store: atomic write of memory + vector + FTS."""
import json
import re
import threading
import uuid
from datetime import date

from kelmo.store.config import get_machine_tag
from kelmo.store.db import now_iso
from kelmo.store.errors import ArchiveError, ArchiveStorageError, ArchiveValidationError
from kelmo.store.memory.embed import embed
from kelmo.store.memory.taxonomy import validate_kind_domain

RAW_NOTE_SOURCE = "raw_note"
_write_lock = threading.RLock()
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A raw note's filename starts with the day it was written: 2026-09-04_233500_project_slug.md
# (a few early notes have no time part: 2026-06-09_slug.md)
DATE_IN_NAME_RE = re.compile(r"(?:^|/)([0-9]{4}-[0-9]{2}-[0-9]{2})_")


def resolve_valid_from(valid_from: str | None, source: str | None, raw_note_path: str | None) -> str | None:
    """The day the fact became true: the explicit value, else the date in a raw note's filename."""
    value = valid_from
    if value in (None, "") and source == RAW_NOTE_SOURCE and raw_note_path:
        matched = DATE_IN_NAME_RE.search(raw_note_path)
        if matched:
            value = matched.group(1)
    if value in (None, ""):
        return None
    try:
        if not isinstance(value, str) or not DATE_RE.fullmatch(value):
            raise ValueError("not an ISO day")
        date.fromisoformat(value)
    except ValueError as error:
        raise ArchiveValidationError("valid_from must be a real YYYY-MM-DD calendar day",
                                 field="valid_from", received=value) from error
    return value


def find_raw_note(conn, raw_note_path: str) -> dict | None:
    """The memory holding a raw note, by its filename, or None."""
    row = conn.execute(
        "SELECT id, created_at, valid_from FROM memories WHERE source = ? AND raw_note_path = ?",
        (RAW_NOTE_SOURCE, raw_note_path),
    ).fetchone()
    return {"id": row["id"], "created_at": row["created_at"], "valid_from": row["valid_from"]} if row else None


def memory_store(
    conn,
    *,
    content: str,
    kind: str,
    domain: str,
    project: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    raw_note_path: str | None = None,
    machine_tag: str | None = None,
    valid_from: str | None = None,
    cache_dir: str | None = None,
) -> dict:
    """Store a memory with embedding. Returns {id, created_at, valid_from}.

    valid_from is the day the fact became true (YYYY-MM-DD), distinct from
    created_at, the moment it was stored; a raw note gets it from its filename.
    since/until in search and list read valid_from first, created_at as the
    fallback, so a backfilled April note answers as April, not as the day of
    the backfill.

    A raw note is identified by its filename: storing the same
    (source="raw_note", raw_note_path) twice returns the existing row with
    duplicate=True instead of writing a second copy, so a replayed spool or a
    repeated backfill is idempotent.
    """
    if conn.in_transaction:
        raise ArchiveStorageError("Store requires an idle connection", operation="memory_store")
    validate_kind_domain(kind, domain)
    valid_from = resolve_valid_from(valid_from, source, raw_note_path)

    if source == RAW_NOTE_SOURCE and raw_note_path:
        with _write_lock:
            existing = find_raw_note(conn, raw_note_path)
            if existing:
                return {**existing, "duplicate": True}

    new_id = str(uuid.uuid4())
    now = now_iso()
    # Callers outside the MCP server (hook extractors) have no KELMO_MACHINE_TAG
    # in their environment; without an explicit tag they fall through to the
    # hostname slug, which two machines can share.
    machine_tag = machine_tag or get_machine_tag()
    tags_json = json.dumps(tags or [])

    # Embed outside the transaction so the write lock is not held during inference
    embedding = embed(content) if cache_dir is None else embed(content, cache_dir=cache_dir)

    try:
        with _write_lock, conn:
            if not conn.in_transaction:
                conn.execute('BEGIN IMMEDIATE')
            if source == RAW_NOTE_SOURCE and raw_note_path:
                existing = find_raw_note(conn, raw_note_path)
                if existing:
                    return {**existing, 'duplicate': True}
            cursor = conn.execute(
                "INSERT INTO memories "
                "(id, content, kind, domain, project, tags_json, source, "
                "machine_tag, created_at, updated_at, raw_note_path, valid_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, content, kind, domain, project, tags_json, source,
                 machine_tag, now, now, raw_note_path, valid_from),
            )
            rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, embedding),
            )
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveStorageError(
            str(e), operation="memory_store", underlying_error=type(e).__name__
        ) from e

    return {"id": new_id, "created_at": now, "valid_from": valid_from}
