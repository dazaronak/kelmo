"""Exact full versions and bounded windows over already stored current chunks."""

import sqlite3

from kelmo.store.errors import ArchiveNotFoundError, ArchiveValidationError

MAX_NEIGHBORS = 3


def _old_version(conn: sqlite3.Connection, row, version: int) -> dict:
    old = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?",
        (row["id"], version),
    ).fetchone()
    if old is None:
        raise ArchiveNotFoundError(
            f"Memory '{row['id']}' has no version {version}",
            id=row["id"],
            version=version,
            current_version=row["version"],
        )
    result = dict(row)
    result.update(dict(old))
    del result["memory_id"], result["updated_at"]
    result["current_version"] = row["version"]
    return result


def memory_get(
    conn: sqlite3.Connection,
    *,
    id: str,
    chunk_idx: int | None = None,
    neighbors: int = 0,
    full: bool = False,
    version: int | None = None,
) -> dict:
    """Read full current/exact content or current chunks with at most 3 neighbors.

    `versions` enumerates replaced versions in ascending order. The current
    version is returned by get itself. Historical reads are always full text;
    old chunk positions are never reconstructed from current derived chunks.
    """
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
    if row is None:
        raise ArchiveNotFoundError(f"Memory not found: '{id}'", id=id)
    if version is not None:
        if type(version) is not int or version < 1:
            raise ArchiveValidationError("version must be a positive integer", field="version")
        if version != row["version"]:
            return _old_version(conn, row, version)
    result = dict(row)
    chunk_total = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id = ?",
        (id,),
    ).fetchone()[0]
    result["chunk_total"] = chunk_total
    if row["version"] > 1:
        result["versions"] = [
            dict(v)
            for v in conn.execute(
                "SELECT version, written_at, replaced_at FROM memory_versions "
                "WHERE memory_id = ? ORDER BY version",
                (id,),
            )
        ]
    if full or version is not None:
        return result
    if type(chunk_idx) is not int or not 0 <= chunk_idx < chunk_total:
        raise ArchiveValidationError(
            "chunk_idx must identify a stored current chunk; use full=true for full content",
            field="chunk_idx",
            received=chunk_idx,
            chunk_total=chunk_total,
        )
    if type(neighbors) is not int:
        raise ArchiveValidationError("neighbors must be an integer", field="neighbors")
    neighbors = max(0, min(neighbors, MAX_NEIGHBORS))
    chunks = conn.execute(
        "SELECT idx, text FROM chunks WHERE memory_id = ? AND idx BETWEEN ? AND ? ORDER BY idx",
        (id, chunk_idx - neighbors, chunk_idx + neighbors),
    ).fetchall()
    if not any(c["idx"] == chunk_idx for c in chunks):
        raise ArchiveValidationError(
            "Requested chunk is missing", field="chunk_idx", received=chunk_idx
        )
    del result["content"]
    result["chunks"] = [{"chunk_idx": c["idx"], "text": c["text"]} for c in chunks]
    return result
