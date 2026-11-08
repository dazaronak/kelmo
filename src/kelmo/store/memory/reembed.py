"""reembed: rebuild the derived chunk data (chunks, FTS rows and, when a provider is given, vectors).

Safe against a live database: cutting and embedding happen outside any transaction, and each record is
written in its own short BEGIN IMMEDIATE under the writer lock, after rechecking that its version and
text are still the ones that were cut. A record changed meanwhile is reported as stale and left for the
next run. Only records without current chunks of the profile are processed (all of them with force),
which makes a run resumable. Chunks need only the tokenizer: with the model unavailable - whether its
space, its input policy or the embedding call itself fails - they are written without vectors, keyword
search finds them at once, and the backfill adds the vectors later. A provider that does answer, but whose
tokenizer, special-token additions, document prefix or input limit disagrees with the counting window, is
refused before anything is written: chunks counted for another tokenizer would carry its vectors. The
additions are part of the profile, so a window over the raw tokenizer.json and the provider's own window
cut under different profiles, and chunks of one are re-cut, never taken as current, for the other.

Records, their versions and their text are never written. An explicit reembed first sweeps every vector
an earlier archive database's own vec0 tables hold, orphans included, into unverified_vectors in one transaction
(db.preserve_legacy_vectors), so no new chunk can reuse an id before its old vector is kept; write_chunks
then copies again, per record, before replacing that record's chunks.
"""
import logging
import sqlite3
import time

from kelmo.store.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS, CHUNK_TARGET_CHARS
from kelmo.store.db import now_iso, preserve_legacy_vectors
from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.memory.backfill import PendingVector, embed_documents, require_idle, write_vectors
from kelmo.store.memory.chunk import (ChunkBudgetError, chunk_profile, chunk_set_is_current, content_sha256,
                                  register_profile, split_to_fit, write_chunks)
from kelmo.store.memory.embed import check_window, identity_id, text_sha256

logger = logging.getLogger("kelmo.store")

_BATCH_SQL = """
    SELECT m.rowid AS rowid, m.id AS id, m.version AS version, m.content AS content,
           EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = m.id) AS hidden
    FROM memories m WHERE m.rowid > ? ORDER BY m.rowid LIMIT ?
"""
_CHUNK_SET_SQL = ("SELECT profile_id, content_sha256, start_char, end_char, text, text_sha256 "
                  "FROM chunks WHERE memory_id = ?")


def _pending(conn, rows, profile_id: str, force: bool) -> list:
    """Records of a batch whose stored chunks are not exactly the current set for their whole text."""
    return [row for row in rows
            if force or not chunk_set_is_current(row["content"], conn.execute(_CHUNK_SET_SQL, (row["id"],))
                                                 .fetchall(), profile_id)]


def reembed(conn: sqlite3.Connection, window, *, provider=None, batch: int = 64, force: bool = False,
            progress=print, writer_check=None, target: int = CHUNK_TARGET_CHARS,
            maximum: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> dict:
    """Cut every record whose chunks are not current for this window's profile, and embed them when a
    provider is given. Returns a summary; non-empty `failed_ids` or `stale_ids` mean unfinished."""
    from kelmo.store.memory.store import _write_lock

    require_idle(conn, "reembed")
    started = time.monotonic()
    profile = chunk_profile(window, target=target, maximum=maximum, overlap=overlap)
    profile_id = identity_id(profile)
    space, unavailable = None, None
    if provider is not None:
        try:
            space, _ = check_window(provider, window)
        except ArchiveEmbeddingError as error:
            # Lexical durability does not wait for the model; semantic readiness does.
            unavailable = error.data.get("reason") or "embedding_unavailable"
            logger.warning("chunking without vectors: %s", error)
    with _write_lock, conn:
        conn.execute("BEGIN IMMEDIATE")
        if writer_check is not None:
            writer_check()
        # Capture orphan rows before new chunks can reuse their numeric identities.
        preserved = preserve_legacy_vectors(conn)
        register_profile(conn, profile)
        # An empty chunks_built_at marks a build in progress for the recorded profile.
        for key, value in {"chunk_profile_id": profile_id, "chunks_built_at": ""}.items():
            conn.execute("INSERT OR REPLACE INTO archive_meta (key, value) VALUES (?, ?)", (key, value))

    summary = {"memories": 0, "chunks": 0, "vectors": 0, "failed_ids": [], "stale_ids": [],
               "vectors_pending": 0, "legacy_vectors_preserved": preserved, "profile_id": profile_id,
               "space_id": identity_id(space) if space is not None else None,
               "vectors_unavailable": unavailable}
    if space is None:
        provider = None
    last_rowid = -1
    while True:
        batch_rows = conn.execute(_BATCH_SQL, (last_rowid, max(1, batch))).fetchall()
        if not batch_rows:
            break
        last_rowid = batch_rows[-1]["rowid"]
        rows = _pending(conn, batch_rows, profile_id, force)
        if not rows:
            continue
        failed_before = len(summary["failed_ids"])
        _rebuild(conn, rows, window, profile_id, provider, space, summary, writer_check,
                 dict(target=target, maximum=maximum, overlap=overlap))
        progress(f"[{summary['memories']}] memories, {summary['chunks']} chunks, {summary['vectors']} vectors, "
                 f"{len(summary['failed_ids'])} failed, {len(summary['stale_ids'])} stale")
        for memory_id in summary["failed_ids"][failed_before:]:
            progress(f"  failed: {memory_id}")

    if not summary["failed_ids"] and not summary["stale_ids"]:
        with _write_lock, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE archive_meta SET value = ? WHERE key = 'chunks_built_at'", (now_iso(),))
    summary["failed"] = len(summary["failed_ids"])
    summary["seconds"] = round(time.monotonic() - started, 1)
    return summary


def _rebuild(conn, rows, window, profile_id, provider, space, summary, writer_check, budget) -> None:
    """Cut a batch, embed it in one model call when possible, write each record on its own."""
    from kelmo.store.memory.store import _write_lock

    cut = []
    for row in rows:
        try:
            chunks = split_to_fit(row["content"], window, **budget)
        except ChunkBudgetError as error:
            logger.warning("record %s cannot be cut to the token window: %s", row["id"], error)
            chunks = []
        if chunks:
            cut.append((row, chunks))
        else:
            # Blank text or a refused cut: no chunk, and it must not pass as done.
            summary["failed_ids"].append(row["id"])

    vectors = None
    visible_texts = [chunk.text for row, chunks in cut if not row["hidden"] for chunk in chunks]
    if provider is not None and visible_texts:
        try:
            vectors = iter(embed_documents(provider, space, visible_texts))
        except ArchiveEmbeddingError as error:
            logger.warning("writing %d chunks without vectors: %s", len(visible_texts), error)

    for row, chunks in cut:
        record_vectors = ([next(vectors) for _ in chunks] if vectors is not None and not row["hidden"]
                          else None)
        with _write_lock, conn:
            conn.execute("BEGIN IMMEDIATE")
            if writer_check is not None:
                writer_check()
            current = conn.execute("SELECT version, content FROM memories WHERE id = ?", (row["id"],)).fetchone()
            if current is None:
                continue  # Deleted while the model ran: nothing to attach the chunks to.
            if (current["version"], current["content"]) != (row["version"], row["content"]):
                summary["stale_ids"].append(row["id"])
                continue
            ids = write_chunks(conn, row["id"], chunks, profile_id=profile_id,
                               memory_version=row["version"], content=row["content"])
            if record_vectors is not None:
                digest = content_sha256(row["content"])
                written = write_vectors(conn, space, [
                    PendingVector(chunk_id, row["id"], chunk.idx, profile_id, text_sha256(chunk.text),
                                  row["version"], digest, vector)
                    for chunk_id, chunk, vector in zip(ids, chunks, record_vectors)
                ])
                summary["vectors"] += written["written"]
            elif not row["hidden"]:
                summary["vectors_pending"] += len(chunks)
        summary["memories"] += 1
        summary["chunks"] += len(chunks)
