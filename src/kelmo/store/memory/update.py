"""Same-ID replacement, preserving each replaced row in the existing archive."""

import json

from kelmo.store.db import now_iso, preserve_legacy_vectors
from kelmo.store.errors import (
    ArchiveConflictError,
    ArchiveEmbeddingError,
    ArchiveError,
    ArchiveNotFoundError,
    ArchiveStorageError,
    ArchiveValidationError,
)
from kelmo.store.memory import store
from kelmo.store.memory.taxonomy import validate_kind_domain

_FIELDS = frozenset(
    ("content", "kind", "domain", "project", "tags_json", "valid_from", "source", "machine_tag")
)


def check_version(row, expected_version, *, id=None):
    if row is None:
        raise ArchiveNotFoundError(f"Memory not found: '{id}'", id=id)
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise ArchiveValidationError(
            "expected_version must be a positive integer", field="expected_version"
        )
    if expected_version is not None and expected_version != row["version"]:
        raise ArchiveConflictError(
            f"memory {row['id']} is at version {row['version']}, not {expected_version}",
            id=row["id"],
            expected_version=expected_version,
            current_version=row["version"],
        )


def _changes(current, content, kind, domain, project, tags, valid_from):
    if content is not None and (not isinstance(content, str) or not content.strip()):
        raise ArchiveValidationError("content is empty", field="content", received=content)
    validate_kind_domain(
        kind if kind is not None else current["kind"],
        domain if domain is not None else current["domain"],
    )
    wanted = dict(
        content=content,
        kind=kind,
        domain=domain,
        project=project,
        tags_json=json.dumps(tags) if tags is not None else None,
        valid_from=store.resolve_valid_from(valid_from, None, None)
        if valid_from is not None
        else None,
    )
    return {
        col: value for col, value in wanted.items() if value is not None and value != current[col]
    }


def replace_current(conn, row, changes, stamp, *, content_changed=False, vector=None):
    """Replace under the caller's write transaction; never commit its receipt separately.

    Empty changes may still represent a Kelmo source/origin metadata revision.
    Full bytes live in memories/history; existing chunk windows are invalidated
    only when content changes. Metadata-only revisions keep their vectors.
    """
    if not conn.in_transaction or not set(changes) <= _FIELDS:
        raise ValueError("Replacement requires a write transaction and known columns")
    columns = (
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
    )
    conn.execute(
        "INSERT INTO memory_versions (memory_id,version,"
        + ",".join(columns)
        + ",written_at,replaced_at) VALUES ("
        + ",".join("?" for _ in range(14))
        + ")",
        (row["id"], row["version"], *(row[col] for col in columns), row["updated_at"], stamp),
    )
    if content_changed:
        rowid = conn.execute("SELECT rowid FROM memories WHERE id=?", (row["id"],)).fetchone()[0]
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE memory_id=?", (row["id"],))]
        # Vectors with no recorded space keep their exact bytes as history, in this same transaction and
        # while the record still holds the text they were observed with, before the deletes below (and
        # the earlier archive's chunks_ad trigger) drop them; only this record's rows.
        preserve_legacy_vectors(conn, chunk_ids=chunk_ids, memory_rowids=[rowid])
    assignments = "".join(f"{col}=?," for col in changes)
    conn.execute(
        f"UPDATE memories SET {assignments}version=version+1,updated_at=? WHERE id=?",
        (*changes.values(), stamp, row["id"]),
    )
    if content_changed:
        # A vector derived from old bytes must never rank the new content.
        conn.execute("DELETE FROM memories_vec WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM chunks WHERE memory_id=?", (row["id"],))
        if vector is not None:
            conn.execute("INSERT INTO memories_vec(rowid,embedding) VALUES (?,?)", (rowid, vector))


def memory_update(
    conn,
    *,
    id: str,
    content: str | None = None,
    kind: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    valid_from: str | None = None,
    expected_version: int | None = None,
) -> dict:
    """Update passed fields; None keeps the stored value, matching the earlier archive.

    Embedding happens only for changed text, outside the transaction. A model
    failure leaves durable text with pending vectors. Even an apparent no-op
    rechecks expected_version after BEGIN IMMEDIATE before returning unchanged.
    """
    if conn.in_transaction:
        raise ArchiveStorageError("Update requires an idle connection", operation="memory_update")
    current = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
    check_version(current, expected_version, id=id)
    changes = _changes(current, content, kind, domain, project, tags, valid_from)
    vector = None
    if "content" in changes:
        try:
            vector = store.embed(changes["content"])
        except ArchiveEmbeddingError:
            pass
    try:
        with store._write_lock, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
            check_version(row, expected_version, id=id)
            changes = _changes(row, content, kind, domain, project, tags, valid_from)
            if not changes:
                return {"id": id, "version": row["version"], "unchanged": True}
            stamp = now_iso()
            replace_current(
                conn, row, changes, stamp, content_changed="content" in changes, vector=vector
            )
    except ArchiveError:
        raise
    except Exception as error:
        raise ArchiveStorageError(
            str(error), operation="memory_update", underlying_error=type(error).__name__
        ) from error
    result = {
        "id": id,
        "version": row["version"] + 1,
        "updated_at": stamp,
        "changed": sorted(changes),
    }
    if "content" in changes:
        # Segmentation is a separate capability; replacement invalidates the
        # previous windows and reports the actual current derived chunk count.
        result["chunks"] = 0
        if vector is None:
            result["vectors_pending"] = True
    return result
