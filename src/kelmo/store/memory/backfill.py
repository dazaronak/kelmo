"""backfill: give every current chunk without a vector in the provider's embedding space its vector.

A chunk written while the model was unavailable has none, and search finds it only by keywords until
this adds it. Each request carries `batch` chunks, then the run rests `rest_factor` times as long as the
request took, so continuous embedding does not hold the CPU. The run stops at once when nothing is
missing or the provider fails for a reason that is not one input's (an outage, a timeout, another
space, a broken channel); `stopped` names that reason and the next run picks up from there. A chunk the
provider refuses on its own (malformed output, an input too long) is retried alone once, reported and
skipped, so it never blocks the chunks after it.
Vectors are written only for a profile whose chunks were cut for the provider's actual tokenizer,
special-token additions, document prefix and input limit.

The model runs outside any transaction. Before a vector is written, under the writer lock, its chunk
must still be the same exact slice (id, record, index, profile, text hash) of the same record version
and text, the record must still be visible and the space must still be the one recorded; anything else
is counted as stale and left for the next run. Nothing here deletes a record, a version or a chunk.
"""
import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from kelmo.store.errors import ArchiveEmbeddingError, ArchiveStorageError
from kelmo.store.memory.chunk import check_profile, content_sha256, current_chunks, read_snapshot
from kelmo.store.memory.embed import (INPUT_REASONS, bound_policy, ensure_space, identity_id, text_sha256,
                                  vector_blobs)

logger = logging.getLogger("kelmo.store")

BATCH = 4
REST_FACTOR = 2.0


@dataclass(frozen=True)
class PendingVector:
    """A chunk exactly as it was read before the model ran, with the vector computed from it."""
    chunk_id: int
    memory_id: str
    idx: int
    profile_id: str
    text_sha256: str
    record_version: int
    content_sha256: str
    embedding: bytes


_CHUNK_STATE_SQL = """
    SELECT c.id AS chunk_id, c.memory_id AS memory_id, c.idx AS idx, c.text AS text,
           c.profile_id AS profile_id, c.text_sha256 AS text_sha256, c.content_sha256 AS chunk_content,
           c.start_char AS start_char, c.end_char AS end_char,
           m.version AS record_version, m.content AS content,
           EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = m.id) AS hidden
    FROM chunks c JOIN memories m ON m.id = c.memory_id
    WHERE c.id = ?
"""


def require_idle(conn: sqlite3.Connection, operation: str) -> None:
    """Refuse to start inside a caller's transaction, leaving that transaction untouched."""
    if conn.in_transaction:
        raise ArchiveStorageError(f"{operation} requires an idle connection; the caller's transaction is kept",
                              operation=operation)


def missing_chunks(conn: sqlite3.Connection, space_id: str, profile_id: str) -> list[int]:
    """Ids of current, visible chunks of the profile without a vector of this space, oldest first."""
    with read_snapshot(conn):
        current = current_chunks(
            conn, profile_id, "NOT EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = m.id)")
        return [row[0] for row in conn.execute(
            """SELECT c.id FROM chunks c WHERE c.id IN (SELECT value FROM json_each(?))
                 AND NOT EXISTS (SELECT 1 FROM chunk_vectors v WHERE v.space_id = ? AND v.chunk_id = c.id
                                 AND v.text_sha256 = c.text_sha256)
               ORDER BY c.id""",
            (json.dumps(sorted(current)), space_id),
        )]


def chunk_states(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    return [row for row in (conn.execute(_CHUNK_STATE_SQL, (chunk_id,)).fetchone() for chunk_id in ids)
            if row is not None]


def pending_from(row, embedding: bytes) -> PendingVector:
    return PendingVector(row["chunk_id"], row["memory_id"], row["idx"], row["profile_id"],
                         row["text_sha256"], row["record_version"], content_sha256(row["content"]), embedding)


def _still_current(row, item: PendingVector) -> bool:
    return (
        row is not None
        and (row["memory_id"], row["idx"], row["profile_id"]) == (item.memory_id, item.idx, item.profile_id)
        and row["text_sha256"] == item.text_sha256 == text_sha256(row["text"])
        and row["record_version"] == item.record_version
        and row["chunk_content"] == item.content_sha256 == content_sha256(row["content"])
        and row["start_char"] is not None
        and row["content"][row["start_char"]:row["end_char"]] == row["text"]
    )


def write_vectors(conn: sqlite3.Connection, space, items: list[PendingVector]) -> dict:
    """Attach vectors under the caller's write transaction after rechecking each chunk; returns counts."""
    from kelmo.store.db import now_iso

    space_id = ensure_space(conn, space, verified=True)
    counts = {"written": 0, "stale": 0, "hidden": 0, "present": 0}
    for item in items:
        row = conn.execute(_CHUNK_STATE_SQL, (item.chunk_id,)).fetchone()
        if not _still_current(row, item):
            counts["stale"] += 1
            continue
        if row["hidden"]:
            counts["hidden"] += 1
            continue
        existing = conn.execute("SELECT text_sha256 FROM chunk_vectors WHERE space_id = ? AND chunk_id = ?",
                                (space_id, item.chunk_id)).fetchone()
        if existing is not None and existing[0] == item.text_sha256:
            counts["present"] += 1
            continue
        if existing is not None:
            # Left by text an earlier chunk with this id had: derived data of this space, replaced.
            conn.execute("DELETE FROM chunk_vectors WHERE space_id = ? AND chunk_id = ?",
                         (space_id, item.chunk_id))
        conn.execute(
            "INSERT INTO chunk_vectors (space_id, chunk_id, memory_id, record_version, content_sha256, "
            "profile_id, idx, text_sha256, embedding, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (space_id, item.chunk_id, item.memory_id, item.record_version, item.content_sha256,
             item.profile_id, item.idx, item.text_sha256, item.embedding, now_iso()),
        )
        counts["written"] += 1
    return counts


def attach_vectors(conn: sqlite3.Connection, space, items: list[PendingVector], *, writer_check=None) -> dict:
    """write_vectors in its own BEGIN IMMEDIATE under the in-process writer lock; `writer_check` runs
    after the lock is taken (the archive adapter's writer/epoch check) and its refusal rolls back."""
    from kelmo.store.memory.store import _write_lock

    require_idle(conn, "attach_vectors")
    with _write_lock, conn:
        conn.execute("BEGIN IMMEDIATE")
        if writer_check is not None:
            writer_check()
        return write_vectors(conn, space, items)


def _isolable(error) -> bool:
    """Whether a failed batch may be retried chunk by chunk: only when the reason names one input
    (INPUT_REASONS). Identity, protocol, channel and inference failures (space_mismatch, protocol_error,
    embedding_unavailable, timeout, model_missing, ...) stop the run, even if a health check would still
    answer: retrying them per chunk would only repeat the same failure for every chunk."""
    return error.data.get("reason") in INPUT_REASONS


def _stop(error) -> str:
    return f"embedding stopped ({error.data.get('reason') or 'embedding_unavailable'}): {error}"


def embed_documents(provider, space, texts: list[str]) -> list[bytes]:
    return vector_blobs(provider.embed_documents(list(texts)), space, "document", len(texts))


def bound_space(conn, provider, profile_id: str):
    """The provider's verified space, after proving the profile's chunks were cut for its input path.
    ArchiveEmbeddingError: the provider cannot establish space or policy now. ArchiveValidationError: it can, and
    the profile was cut for another tokenizer, other special-token additions, another prefix or a budget
    above its limit (a profile recorded before additions were named is refused, not assumed to match)."""
    space, policy = bound_policy(provider)
    check_profile(conn, profile_id, space, policy)
    return space


def backfill(conn: sqlite3.Connection, provider, *, profile_id: str, batch: int = BATCH,
             rest_factor: float = REST_FACTOR, progress=print, sleep=time.sleep, writer_check=None) -> dict:
    """Embed the missing vectors. Returns a summary; `stopped` names why a run ended early, including a
    provider that cannot establish its space or input policy at all (then `missing` is None)."""
    require_idle(conn, "backfill")
    started = time.monotonic()
    totals = {"written": 0, "stale": 0, "hidden": 0, "present": 0}
    failed: list[int] = []
    try:
        space = bound_space(conn, provider, profile_id)
    except ArchiveEmbeddingError as error:
        return {"missing": None, **totals, "failed": failed, "space_id": None,
                "stopped": f"embedding provider unavailable: {error}",
                "seconds": round(time.monotonic() - started, 1)}
    space_id = identity_id(space)
    missing = missing_chunks(conn, space_id, profile_id)
    stopped = None
    for start in range(0, len(missing), batch):
        rows = chunk_states(conn, missing[start:start + batch])
        if not rows:
            continue
        began = time.monotonic()
        try:
            vectors = embed_documents(provider, space, [row["text"] for row in rows])
            items = [pending_from(row, vector) for row, vector in zip(rows, vectors)]
        except ArchiveEmbeddingError as error:
            if not _isolable(error):
                stopped = _stop(error)
                break
            # One input in this batch is at fault: one chunk at a time, and a chunk the provider refuses is
            # reported instead of blocking every later run. Any other failure met on the way stops the run.
            items = []
            for row in rows:
                try:
                    items.append(pending_from(row, embed_documents(provider, space, [row["text"]])[0]))
                except ArchiveEmbeddingError as single:
                    if not _isolable(single):
                        stopped = _stop(single)
                        break
                    logger.warning("chunk %s cannot be embedded: %s", row["chunk_id"], single)
                    failed.append(row["chunk_id"])
        for key, value in attach_vectors(conn, space, items, writer_check=writer_check).items():
            totals[key] += value
        if stopped is not None:
            break
        progress(f"[{min(start + batch, len(missing))}/{len(missing)}] chunks, {totals['written']} vectors "
                 f"written, {totals['stale']} stale, {len(failed)} failed")
        if start + batch < len(missing):
            sleep((time.monotonic() - began) * rest_factor)
    return {"missing": len(missing), **totals, "failed": failed, "stopped": stopped, "space_id": space_id,
            "seconds": round(time.monotonic() - started, 1)}
