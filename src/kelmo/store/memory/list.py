"""memory_list: deterministic filter without search."""
import sqlite3
from datetime import UTC, datetime

from kelmo.store.db import define_function
from kelmo.store.errors import ArchiveLegacyRecordError, ArchiveValidationError

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FACT_TIME = "COALESCE(valid_from, created_at)"
_NAMED = 20
_UNREADABLE = (AttributeError, OverflowError, TypeError, ValueError)


def _timestamp_microseconds(value):
    if value is None:
        return None
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    elapsed = moment.astimezone(UTC) - _EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds


def _malformed(value):
    """1 for a stored value an older archive accepted that is not an instant, such as 2026-02-30."""
    try:
        _timestamp_microseconds(value)
    except _UNREADABLE:
        return 1
    return 0


def _bound(name, value):
    try:
        return _timestamp_microseconds(value)
    except _UNREADABLE as error:
        raise ArchiveValidationError(f"{name} must be an ISO date or timestamp",
                                 field=name, received=value) from error


def memory_list(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    machine_tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_at",
    order: str = "desc",
    exclude_forgotten: bool = False,
) -> dict:
    """List memories with deterministic filtering. Returns {items, total}.

    A row whose ordering or filtering timestamp cannot be read as an instant
    raises ArchiveLegacyRecordError naming it; it is never skipped or reordered.
    """
    define_function(conn, 'archive_timestamp', 1, _timestamp_microseconds)
    define_function(conn, 'archive_timestamp_malformed', 1, _malformed)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if order_by not in ("created_at", "updated_at", "valid_from"):
        order_by = "created_at"
    order_column = _FACT_TIME if order_by == "valid_from" else order_by
    order_expr = f"archive_timestamp({order_column})"
    if order not in ("asc", "desc"):
        order = "desc"

    clauses, params = [], []
    if exclude_forgotten:
        # A hidden record keeps its bytes, so the exclusion is a marker lookup that must run before the limit.
        clauses.append("NOT EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = memories.id)")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    if project:
        clauses.append("project = ?")
        params.append(project)
    if machine_tag:
        clauses.append("machine_tag = ?")
        params.append(machine_tag)
    if tags:
        for tag in tags:
            # Exact membership: LIKE would read % and _ in a tag as wildcards.
            clauses.append("EXISTS (SELECT 1 FROM json_each(memories.tags_json) WHERE json_each.value = ?)")
            params.append(tag)

    checked = {order_column}
    time_clauses, time_params = [], []
    if since:
        time_clauses.append(f"archive_timestamp({_FACT_TIME}) >= ?")
        time_params.append(_bound("since", since))
        checked.add(_FACT_TIME)
    if until:
        bound = _bound("until", until[:10] + 'T23:59:59.999999+00:00' if isinstance(until, str) else until)
        time_clauses.append(f"archive_timestamp({_FACT_TIME}) <= ?")
        time_params.append(bound)
        checked.add(_FACT_TIME)

    where = " AND ".join(clauses) if clauses else "1=1"
    tests = " OR ".join(f"archive_timestamp_malformed({column})" for column in sorted(checked))
    malformed = [row[0] for row in conn.execute(
        f"SELECT id FROM memories WHERE {where} AND ({tests}) ORDER BY id", params)]
    if malformed:
        fields = sorted("valid_from/created_at" if column == _FACT_TIME else column for column in checked)
        named = ", ".join(malformed[:_NAMED])
        if len(malformed) > _NAMED:
            named += f" and {len(malformed) - _NAMED} more"
        raise ArchiveLegacyRecordError(
            f"Stored record(s) {named} hold a {' or '.join(fields)} value that is not an ISO instant, so this "
            "listing cannot order or filter them; no record was skipped. These preserved values need repair "
            "through an explicit archive migration; the raw row stays readable through memory_get.",
            records=malformed, fields=fields)

    where = " AND ".join(clauses + time_clauses) if clauses or time_clauses else "1=1"
    params = [*params, *time_params]
    total = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        # id breaks ties, so pages by offset neither repeat nor skip records stored in the same instant.
        f"SELECT * FROM memories WHERE {where} ORDER BY {order_expr} {order}, id {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return {"items": [dict(r) for r in rows], "total": total}
