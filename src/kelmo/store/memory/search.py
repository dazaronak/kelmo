"""memory_search: semantic + hybrid (BM25 + vec + RRF) search.

Two indexes, two entry points. memory_search is the record index (memories_fts + memories_vec, one
vector per record from the pinned local model) and keeps its behaviour. search_chunks is the chunk index:
it ranks the current chunks of one profile by BM25 and by vectors of one exact embedding space, for
every query variant, fuses the rankings by reciprocal rank and returns its search metadata with the rows.
"""
import json
import re
import sqlite3

from kelmo.store.config import HYBRID_CANDIDATES, MAX_CHUNKS_PER_MEMORY, MAX_QUERY_VARIANTS, MAX_RECORD_POOL, RRF_K
from kelmo.store.db import define_function
from kelmo.store.errors import ArchiveEmbeddingError, ArchiveLegacyRecordError, ArchiveStorageError, ArchiveValidationError
from kelmo.store.memory.chunk import check_profile, current_chunks, read_snapshot
from kelmo.store.memory.embed import bound_policy, embed, identity_id, vector_blobs


def fts5_escape(query: str) -> str:
    """Escape user input for safe use in FTS5 MATCH expression."""
    if not query or not query.strip():
        return '""'
    tokens = []
    for tok in query.split():
        escaped = tok.replace('"', '""')
        tokens.append(f'"{escaped}"')
    return " ".join(tokens)


def _build_filter_clause(
    kind: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    machine_tag: str | None = None,
    exclude_forgotten: bool = False,
) -> tuple[str, list]:
    """Build WHERE clause fragments and params for memory filters."""
    clauses = []
    params = []
    if exclude_forgotten:
        # A hidden record keeps its bytes, so the exclusion is a marker lookup that must run before the limit.
        clauses.append("NOT EXISTS (SELECT 1 FROM record_visibility rv WHERE rv.id = m.id)")
    if kind:
        clauses.append("m.kind = ?")
        params.append(kind)
    if domain:
        clauses.append("m.domain = ?")
        params.append(domain)
    if project:
        clauses.append("m.project = ?")
        params.append(project)
    if machine_tag:
        clauses.append("m.machine_tag = ?")
        params.append(machine_tag)
    # The fact's own date when it has one, the storage moment otherwise;
    # until is inclusive of its whole day.
    if since:
        clauses.append("COALESCE(m.valid_from, m.created_at) >= ?")
        params.append(since)
    if until:
        clauses.append("substr(COALESCE(m.valid_from, m.created_at), 1, 10) <= substr(?, 1, 10)")
        params.append(until)
    if tags:
        # An exact element of the stored list: LIKE would read `_` as a wildcard and ignore ASCII case.
        for tag in tags:
            clauses.append("EXISTS (SELECT 1 FROM json_each(m.tags_json) WHERE json_each.value = ?)")
            params.append(tag)

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def memory_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    mode: str = "semantic",
    limit: int = 10,
    kind: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    machine_tag: str | None = None,
    cache_dir: str | None = None,
    exclude_forgotten: bool = False,
) -> list[dict]:
    """Search the record index. mode is semantic, hybrid, or lexical.

    Query variants, chunk profiles and embedding providers belong to search_chunks, which always returns
    its search metadata; this record API has no path that would drop it."""
    if mode not in ("semantic", "hybrid", "lexical"):
        raise ArchiveValidationError(
            f"Unknown search mode: '{mode}'",
            field="mode", received=mode,
            valid_values=["semantic", "hybrid", "lexical"],
        )

    limit = max(1, min(limit, 100))
    where, filter_params = _build_filter_clause(
        kind=kind, domain=domain, project=project,
        tags=tags, since=since, until=until, machine_tag=machine_tag,
        exclude_forgotten=exclude_forgotten,
    )

    if mode == "semantic":
        return _search_semantic(conn, query, where, filter_params, limit, cache_dir)
    if mode == "hybrid":
        return _search_hybrid(conn, query, where, filter_params, limit, cache_dir=cache_dir)
    return _search_lexical(conn, query, where, filter_params, limit)


def _search_semantic(conn, query, where, filter_params, limit, cache_dir=None):
    qvec = embed(query) if cache_dir is None else embed(query, cache_dir=cache_dir)
    # sqlite-vec requires k=? in WHERE clause for knn queries. The filter runs inside the knn as well, so a rare
    # project or tag is not lost behind nearer records the filter would drop afterwards.
    sql = f"""
        SELECT m.*, v.distance
        FROM (
            SELECT rowid, distance FROM memories_vec
            WHERE embedding MATCH ? AND k = ?
              AND rowid IN (SELECT m.rowid FROM memories m WHERE {where})
        ) v
        JOIN memories m ON m.rowid = v.rowid
        WHERE {where}
        ORDER BY v.distance
        LIMIT ?
    """
    k_candidates = min(limit * 5, 500)
    rows = conn.execute(sql, [qvec, k_candidates, *filter_params, *filter_params, limit]).fetchall()
    return [_row_to_dict(r) for r in rows]


def _search_hybrid(conn, query, where, filter_params, limit, k=60, cache_dir=None):
    """BM25 + vector + RRF fusion."""
    qvec = embed(query) if cache_dir is None else embed(query, cache_dir=cache_dir)
    candidate_size = 200

    vec_sql = f"""
        SELECT m.rowid, v.distance
        FROM (
            SELECT rowid, distance FROM memories_vec
            WHERE embedding MATCH ? AND k = ?
              AND rowid IN (SELECT m.rowid FROM memories m WHERE {where})
        ) v
        JOIN memories m ON m.rowid = v.rowid
        WHERE {where}
        ORDER BY v.distance
    """
    vec_rows = conn.execute(vec_sql, [qvec, candidate_size, *filter_params, *filter_params]).fetchall()

    # BM25: bm25() returns NEGATIVE, more negative = better. ORDER BY ASC.
    escaped_query = fts5_escape(query)
    fts_sql = f"""
        SELECT m.rowid, bm25(memories_fts) AS score
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE f.content MATCH ?
          AND {where}
        ORDER BY score ASC
        LIMIT ?
    """
    fts_rows = conn.execute(fts_sql, [escaped_query, *filter_params, candidate_size]).fetchall()

    # RRF fusion
    scores: dict[int, float] = {}
    for rank, row in enumerate(vec_rows):
        rid = row["rowid"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    for rank, row in enumerate(fts_rows):
        rid = row["rowid"]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)

    top_rowids = [rid for rid, _ in sorted(scores.items(), key=lambda x: -x[1])[:limit]]

    if not top_rowids:
        return []

    placeholders = ",".join("?" * len(top_rowids))
    rows = conn.execute(
        f"SELECT rowid, * FROM memories WHERE rowid IN ({placeholders})",
        top_rowids,
    ).fetchall()

    row_map = {r["rowid"]: _row_to_dict(r) for r in rows}
    return [row_map[rid] for rid in top_rowids if rid in row_map]


def _search_lexical(conn, query, where, filter_params, limit):
    """Search the FTS index by BM25 without producing vector-derived scores."""
    escaped_query = fts5_escape(query)
    sql = f"""
        SELECT m.*
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE f.content MATCH ?
          AND {where}
        ORDER BY bm25(memories_fts) ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [escaped_query, *filter_params, limit]).fetchall()
    return [_row_to_dict(row) for row in rows]


# --- chunk index ---

_WORD_RE = re.compile(r"\w+")
_MAX_QUERY_WORDS = 48


def fts5_any_word(query: str) -> str:
    """FTS5 MATCH expression that matches a text containing ANY word of the query.

    Requiring every word makes keyword search useless for a natural-language question: a note almost
    never contains all of them. BM25 already ranks by how many words match and how rare they are.
    Every word is quoted, so NOT/OR/NEAR in a query are words, never operators."""
    found = list(dict.fromkeys(_WORD_RE.findall(query.lower())))
    # Short words add little to BM25 next to longer ones, but a query of short words alone still ORs them.
    words = [w for w in found if len(w) > 2] or found
    if not words:
        return fts5_escape(query)
    return " OR ".join(f'"{w}"' for w in words[:_MAX_QUERY_WORDS])


def _check_filters(kind, domain, project, tags, since, until, machine_tag) -> None:
    """Refuse a filter the chunk index would otherwise read as something else, e.g. a bare tag string
    taken character by character. Days use the shared adoption contract's iso_day: ASCII digits only,
    the whole value, a real calendar day."""
    from kelmo.contracts import iso_day

    for field, value in (("kind", kind), ("domain", domain), ("project", project),
                         ("machine_tag", machine_tag)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ArchiveValidationError(f"{field} must be nonempty text", field=field, received=value)
    if tags is not None and (not isinstance(tags, (list, tuple))
                             or any(not isinstance(tag, str) or not tag.strip() for tag in tags)):
        raise ArchiveValidationError("tags must be a list of nonempty texts", field="tags", received=tags)
    for field, value in (("since", since), ("until", until)):
        if value is None:
            continue
        try:
            iso_day(value)
        except ValueError:
            raise ArchiveValidationError(f"{field} must be an ISO day YYYY-MM-DD", field=field,
                                     received=value) from None


_FACT_TIME = "COALESCE(m.valid_from, m.created_at)"


def _precise_dates(conn, where, params, since, until):
    """since/until as the accepted archive list reads them: the fact's day (valid_from), else the storage
    instant (created_at), compared as UTC instants; until covers its whole UTC day. A selected record whose
    date is not an instant is refused by name (ArchiveLegacyRecordError), never skipped or compared as text."""
    from kelmo.store.memory.list import _bound, _malformed, _timestamp_microseconds

    if not since and not until:
        return where, params
    define_function(conn, "archive_timestamp", 1, _timestamp_microseconds)
    define_function(conn, "archive_timestamp_malformed", 1, _malformed)
    malformed = [row[0] for row in conn.execute(
        f"SELECT m.id FROM memories m WHERE {where} AND archive_timestamp_malformed({_FACT_TIME}) ORDER BY m.id",
        params)]
    if malformed:
        raise ArchiveLegacyRecordError(
            f"Stored record(s) {', '.join(malformed[:20])} hold a valid_from/created_at value that is not an ISO "
            "instant, so this search cannot filter them by date; no record was skipped.",
            records=malformed, fields=["valid_from/created_at"])
    clauses, bounds = [], []
    if since:
        clauses.append(f"archive_timestamp({_FACT_TIME}) >= ?")
        bounds.append(_bound("since", since))
    if until:
        clauses.append(f"archive_timestamp({_FACT_TIME}) <= ?")
        bounds.append(_bound("until", until + "T23:59:59.999999+00:00"))
    return " AND ".join([where, *clauses]), [*params, *bounds]


def query_variants(queries) -> list[str]:
    """The query texts to rank, in order; a variant that repeats an earlier one (ignoring case) is
    searched once. One to MAX_QUERY_VARIANTS nonempty texts, e.g. the same question in two languages."""
    if isinstance(queries, str) or not isinstance(queries, (list, tuple)) or not queries:
        raise ArchiveValidationError("queries must be a nonempty list of texts", field="queries", received=queries)
    variants: list[str] = []
    for value in queries:
        if not isinstance(value, str) or not value.strip():
            raise ArchiveValidationError("Every query variant must be nonempty text", field="queries",
                                     received=value)
        if value.strip().casefold() not in {v.casefold() for v in variants}:
            variants.append(value.strip())
    if len(variants) > MAX_QUERY_VARIANTS:
        raise ArchiveValidationError(f"At most {MAX_QUERY_VARIANTS} query variants", field="queries")
    return variants


def search_chunks(
    conn: sqlite3.Connection,
    *,
    queries,
    profile_id: str,
    provider=None,
    mode: str = "hybrid",
    limit: int = 10,
    kind: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    machine_tag: str | None = None,
    exclude_forgotten: bool = True,
    record_limit: int | None = None,
) -> dict:
    """Rank the current chunks of `profile_id` for every query variant and fuse the rankings.

    `limit` counts chunk rows (1..100). A caller that groups rows into records passes `record_limit`
    instead (1..MAX_RECORD_POOL): fusion then stops at that many distinct records, each still holding at
    most MAX_CHUNKS_PER_MEMORY rows, so a limit of N records is not cut short by records that contribute
    two chunks each. `limit` is still validated and otherwise unused then.

    Filters and visibility apply before any ranking, so a rare tag or project is found however far
    its records lie from the query; hidden records are excluded unless exclude_forgotten=False. A chunk
    is current only while its record's whole present text is the text it was cut from. Each ranking and
    the fused list hold at most MAX_CHUNKS_PER_MEMORY chunks of one record; ties keep chunk order.

    The provider is asked first, outside any transaction: its verified space must name its input path
    (embed.bound_policy) and the stored profile must have been cut for that path (otherwise reason
    profile_policy_mismatch: explicit reembed is the recovery), then the queries are embedded. Everything
    the rows are ranked and bound by is then read in one snapshot. A row is the record's columns with `content` holding the chunk
    text, plus chunk_idx, chunk_total and chunk_start/chunk_end over the same current set, the record's
    present `version` and `record_content_sha256` (enough to bind a locator without reading again) and,
    in semantic mode, distance: the chunk's nearest L2 distance to any query variant, a diagnostic. Rows
    are in fused reciprocal-rank order across the variants, so with several variants distance need not
    increase down the list.

    Vectors come only from `provider`'s exact embedding space. Semantic mode raises ArchiveEmbeddingError
    when the provider fails or no current chunk of the search holds a verified vector of that space;
    hybrid then ranks by BM25 alone and says so: search.degraded is true and search.reason carries the
    provider's reason, for example embedding_unavailable, embedding_identity_unverified,
    no_compatible_vectors, input_too_long, timeout or space_mismatch (an open set, not an enum).
    search.vectorless_chunks counts the current chunks (after filters, visibility and the version check)
    without such a vector, or is None when no space was established. When some have vectors and some do
    not, the vectors that exist are still ranked, and the search is degraded with reason
    partial_vector_coverage: chunks waiting for the backfill are missing from the vector ranking (hybrid
    still finds them by keywords). Database and FTS errors propagate and are never reported as degradation.

    Returns {"items": [...], "search": {mode, degraded, reason, index_identity, unindexed_records,
    vectorless_chunks}}.
    """
    if mode not in ("semantic", "hybrid", "lexical"):
        raise ArchiveValidationError(
            f"Unknown search mode: '{mode}'",
            field="mode", received=mode, valid_values=["semantic", "hybrid", "lexical"],
        )
    if not isinstance(profile_id, str) or not profile_id:
        raise ArchiveValidationError("profile_id is required", field="profile_id")
    variants = query_variants(queries)
    if type(limit) is not int:
        raise ArchiveValidationError("limit must be an integer", field="limit", received=limit)
    limit = max(1, min(limit, 100))
    if record_limit is not None and (type(record_limit) is not int or not 1 <= record_limit <= MAX_RECORD_POOL):
        raise ArchiveValidationError(f"record_limit must be an integer 1..{MAX_RECORD_POOL}", field="record_limit",
                                 received=record_limit)
    _check_filters(kind, domain, project, tags, since, until, machine_tag)
    where, filter_params = _build_filter_clause(
        kind=kind, domain=domain, project=project, tags=tags, machine_tag=machine_tag,
        exclude_forgotten=exclude_forgotten,
    )
    # The provider runs before the read snapshot opens: no read transaction is held during inference.
    vectors, space_id, reason = None, None, None
    if mode != "lexical":
        try:
            vectors, space_id = _query_vectors(conn, provider, profile_id, variants)
        except ArchiveEmbeddingError as error:
            if mode == "semantic":
                raise
            reason = error.data.get("reason") or "embedding_unavailable"
    with read_snapshot(conn):
        where, filter_params = _precise_dates(conn, where, filter_params, since, until)
        return _search_chunks(conn, variants, profile_id, mode, limit, where, filter_params,
                              vectors, space_id, reason, record_limit)


def _query_vectors(conn, provider, profile_id, variants) -> tuple[list[bytes], str]:
    """The query vectors of the provider's verified space, after proving that its input path is the one
    the stored profile was cut for and that the profile has vectors of that space at all (then without
    a model call). Single autocommit reads only; the model runs outside any transaction."""
    if provider is None:
        raise ArchiveEmbeddingError("No embedding provider is configured", reason="embedding_unavailable")
    space, policy = bound_policy(provider)
    space_id = identity_id(space)
    try:
        check_profile(conn, profile_id, space, policy)
    except ArchiveValidationError as error:
        if error.data.get("field") != "profile_id":  # an unknown profile is reported by the snapshot read
            raise ArchiveEmbeddingError("The chunk profile was cut for another input path than the provider's; "
                                    "run reembed for this provider", reason="profile_policy_mismatch",
                                    field=error.data.get("field")) from error
    stored = conn.execute(
        "SELECT 1 FROM chunk_vectors v JOIN embedding_spaces s ON s.space_id = v.space_id AND s.verified = 1 "
        "WHERE v.space_id = ? AND v.profile_id = ? LIMIT 1", (space_id, profile_id)).fetchone()
    if stored is None:
        raise ArchiveEmbeddingError("No stored vectors of this profile share the provider's embedding space",
                                reason="no_compatible_vectors", space_id=space_id)
    return vector_blobs(provider.embed_queries(list(variants)), space, "query", len(variants)), space_id


def _search_chunks(conn, variants, profile_id, mode, limit, where, filter_params, vectors, space_id,
                   reason, record_limit=None) -> dict:
    current = current_chunks(conn, profile_id, where, filter_params)
    indexed = {chunk.memory_id for chunk in current.values()}
    selected = [row[0] for row in conn.execute(f"SELECT m.id FROM memories m WHERE {where}", filter_params)]
    unindexed = sum(1 for memory_id in selected if memory_id not in indexed)
    if unindexed and conn.execute("SELECT 1 FROM chunks WHERE profile_id = ? LIMIT 1",
                                  (profile_id,)).fetchone() is None:
        raise ArchiveStorageError(
            "records exist but none has chunks of this profile: run reembed before searching",
            operation="memory_search", underlying_error="chunks_not_built",
        )
    current_ids = json.dumps(sorted(current))

    vectorless = None
    if vectors is not None:
        vectorless = len(current) - _covered(conn, space_id, current_ids)
        if current and vectorless == len(current):
            if mode == "semantic":
                raise ArchiveEmbeddingError("No current chunk of this search has a vector of the provider's "
                                        "embedding space", reason="no_compatible_vectors", space_id=space_id)
            vectors, reason = None, "no_compatible_vectors"
    if reason is None and vectorless:
        # Ranked by the vectors that exist; the chunks still waiting for theirs are not in that ranking.
        reason = "partial_vector_coverage"

    rankings: list[list[int]] = []
    nearest: dict[int, float] = {}
    for vector in vectors or []:
        hits = _vector_ranking(conn, vector, space_id, current_ids)
        rankings.append([chunk_id for chunk_id, _ in hits])
        for chunk_id, distance in hits:
            nearest[chunk_id] = min(distance, nearest.get(chunk_id, distance))
    if mode != "semantic":
        rankings += [_bm25_ranking(conn, q, current_ids) for q in variants]
    ranked = _fuse(current, rankings, limit, distances=nearest if mode == "semantic" else None,
                   records=record_limit)
    effective = "lexical" if vectors is None else mode
    search = {
        "mode": effective,
        "degraded": reason is not None,
        "reason": reason,
        "index_identity": f"chunk:{profile_id}:{space_id if vectors is not None else 'lexical'}",
        "unindexed_records": unindexed,
        "vectorless_chunks": vectorless,
    }
    if reason is not None:
        search["requested_mode"] = mode
    return {"items": _load_results(conn, ranked, current), "search": search}


def _covered(conn, space_id, current_ids) -> int:
    """How many of the search's current chunks hold a verified vector of this space for their present text."""
    return conn.execute(
        f"""
        SELECT COUNT(*) FROM chunks c WHERE c.id IN ({_CURRENT_IDS}) AND EXISTS (
            SELECT 1 FROM chunk_vectors v JOIN embedding_spaces s ON s.space_id = v.space_id AND s.verified = 1
            WHERE v.space_id = ? AND v.chunk_id = c.id AND v.text_sha256 = c.text_sha256)
        """,
        (current_ids, space_id),
    ).fetchone()[0]


# The current chunk ids of one search, passed once as a JSON array: ranking reads only those.
_CURRENT_IDS = "SELECT value FROM json_each(?)"


def _vector_ranking(conn, qvec, space_id, current_ids) -> list[tuple[int, float]]:
    """Nearest current chunks by L2 distance, filtered before ranking, MAX_CHUNKS_PER_MEMORY per record.

    Exact distances over the filtered current chunks: no candidate window can hide a rare scope, and one
    record with many similar chunks cannot crowd the others out. A vector joins its chunk only while the
    chunk's text hash is the one it was computed from."""
    rows = conn.execute(
        f"""
        WITH scored AS MATERIALIZED (
            SELECT c.id AS chunk_id, c.memory_id AS memory_id,
                   vec_distance_l2(v.embedding, ?) AS distance
            FROM chunk_vectors v
            JOIN chunks c ON c.id = v.chunk_id AND c.text_sha256 = v.text_sha256
            WHERE v.space_id = ? AND c.id IN ({_CURRENT_IDS})
        ), capped AS (
            SELECT chunk_id, distance,
                   ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY distance, chunk_id) AS in_record
            FROM scored
        )
        SELECT chunk_id, distance FROM capped WHERE in_record <= ?
        ORDER BY distance, chunk_id LIMIT ?
        """,
        [qvec, space_id, current_ids, MAX_CHUNKS_PER_MEMORY, HYBRID_CANDIDATES],
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _bm25_ranking(conn, query, current_ids) -> list[int]:
    """Keyword ranking of current chunks; bm25() is negative, more negative is better."""
    rows = conn.execute(
        f"""
        WITH scored AS MATERIALIZED (
            SELECT c.id AS chunk_id, c.memory_id AS memory_id, bm25(chunks_fts) AS score
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.rowid
            WHERE f.text MATCH ? AND c.id IN ({_CURRENT_IDS})
        ), capped AS (
            SELECT chunk_id, score,
                   ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY score, chunk_id) AS in_record
            FROM scored
        )
        SELECT chunk_id FROM capped WHERE in_record <= ? ORDER BY score, chunk_id LIMIT ?
        """,
        [fts5_any_word(query), current_ids, MAX_CHUNKS_PER_MEMORY, HYBRID_CANDIDATES],
    ).fetchall()
    return [row[0] for row in rows]


def _fuse(current, rankings, limit, k=RRF_K, distances=None, records=None) -> list[tuple[int, float | None]]:
    """Reciprocal-rank fusion with equal weights, keyed by chunk, at most MAX_CHUNKS_PER_MEMORY per
    record; equal scores keep chunk order. Equal weights: on the frozen bench a vector weight of 3 lost
    same-language queries. Stops at `limit` chunks, or with `records` at that many distinct records (later
    chunks of an admitted record are still taken up to its cap)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    ranked: list[tuple[int, float | None]] = []
    per_record: dict[str, int] = {}
    for chunk_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        chunk = current.get(chunk_id)
        if chunk is None or per_record.get(chunk.memory_id, 0) >= MAX_CHUNKS_PER_MEMORY:
            continue
        if records is not None and chunk.memory_id not in per_record and len(per_record) == records:
            continue
        per_record[chunk.memory_id] = per_record.get(chunk.memory_id, 0) + 1
        ranked.append((chunk_id, distances.get(chunk_id) if distances else None))
        if records is None and len(ranked) == limit:
            break
    return ranked


def _load_results(conn, ranked, current) -> list[dict]:
    """Result rows for the chosen chunks, in ranking order, bound to the record version and full-text hash
    of the same snapshot; internal row ids stay internal."""
    if not ranked:
        return []
    totals: dict[str, int] = {}
    for chunk in current.values():
        totals[chunk.memory_id] = totals.get(chunk.memory_id, 0) + 1
    placeholders = ",".join("?" * len(ranked))
    rows = conn.execute(
        f"""
        SELECT c.id AS chunk_id, c.idx AS chunk_idx, c.text AS chunk_text,
               c.start_char AS chunk_start, c.end_char AS chunk_end, m.*
        FROM chunks c JOIN memories m ON m.id = c.memory_id
        WHERE c.id IN ({placeholders})
        """,
        [chunk_id for chunk_id, _ in ranked],
    ).fetchall()
    by_chunk = {row["chunk_id"]: row for row in rows}
    results = []
    for chunk_id, distance in ranked:
        row, chunk = by_chunk.get(chunk_id), current[chunk_id]
        if row is None or row["version"] != chunk.version:
            continue
        result = dict(row)
        result["content"] = result.pop("chunk_text")
        del result["chunk_id"]
        result["chunk_total"] = totals[chunk.memory_id]
        result["record_content_sha256"] = chunk.content_sha256
        if distance is not None:
            result["distance"] = distance
        results.append(result)
    return results
