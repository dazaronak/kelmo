"""Split a record's text into overlapping chunks that each fit the embedding window.

Every chunk is an exact slice content[start:end] of the record version it was cut from, so its bytes,
coordinates and source date stay the record's own. The cut follows the frozen archive splitter: paragraph
blocks (a heading starts one; YAML frontmatter and fenced code stay whole), then sentence ends, then
whitespace, then a hard cut, packed toward a character target with a word-aligned overlap taken from
the previous chunk. Each candidate is then checked against the token window, prefix and special tokens
included. One that does not fit loses its overlap first, then is split further under a halved budget,
and finally bisected; a single code point that still does not fit is refused with ChunkBudgetError.
No depth limit hands back an over-budget chunk.
"""
import hashlib
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NamedTuple

from kelmo.store.db import define_function
from kelmo.store.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS, CHUNK_TARGET_CHARS
from kelmo.store.errors import ArchiveConflictError, ArchiveNotFoundError, ArchiveStorageError, ArchiveValidationError

PROFILE_VERSION = "archive-chunk/2"
SEGMENTATION = "markdown-blocks>sentences>words>hard-cut;fit:drop-overlap>halve>bisect;exact-slices"
MAX_NEIGHBORS = 3

_SEPARATOR = "\n\n"
_HEADING_RE = re.compile(r"^#{1,6}\s")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")
_WORD_RE = re.compile(r"\S+\s*")
_SPACE_RE = re.compile(r"\s+")


class ChunkBudgetError(ArchiveValidationError):
    """Text that cannot be cut to fit the token window. Nothing over budget is emitted in its place."""


@dataclass(frozen=True)
class Chunk:
    idx: int
    start: int
    end: int
    text: str


class _Budget(NamedTuple):
    target: int
    maximum: int
    overlap: int

    # Every chunk after the first may carry an overlap and its separator, so a body leaves room for them.
    @property
    def body_max(self) -> int:
        return max(1, self.maximum - self.overlap - len(_SEPARATOR))

    @property
    def body_target(self) -> int:
        return max(1, self.target - self.overlap - len(_SEPARATOR))

    def halved(self) -> "_Budget":
        return _Budget(max(1, self.target // 2), max(1, self.maximum // 2), self.overlap // 2)


Span = tuple[int, int]


def split(content: str, fits: Callable[[str], bool] | None = None, *, target: int = CHUNK_TARGET_CHARS,
          maximum: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[Chunk]:
    """The chunks of content in order; none for blank content. Short content is a single chunk.

    `fits` tells whether a chunk's text is within the token window. Without it the budget is in
    characters only, which holds for prose and nothing else."""
    if not isinstance(content, str):
        raise ArchiveValidationError("content must be text", field="content")
    whole = _trim(content, 0, len(content))
    if whole is None:
        return []
    budget = _Budget(target, maximum, overlap)
    if whole[1] - whole[0] <= budget.maximum:
        candidates = [(whole, whole)]
    else:
        candidates = _split_by_budget(content, whole, budget)
    spans: list[Span] = []
    for span, body in candidates:
        if fits is None or fits(content[span[0]:span[1]]):
            spans.append(span)
        elif span != body and fits(content[body[0]:body[1]]):
            spans.append(body)
        else:
            spans.extend(_fit_tokens(content, body, budget.halved(), fits))
    return [Chunk(idx, start, end, content[start:end]) for idx, (start, end) in enumerate(spans)]


def split_to_fit(content: str, window, **budget) -> list[Chunk]:
    """split() checked against a TokenWindow: every chunk fits window.budget with prefix and specials."""
    return split(content, window.fits, **budget)


def _trim(content: str, start: int, end: int) -> Span | None:
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _split_by_budget(content: str, span: Span, budget: _Budget) -> list[tuple[Span, Span]]:
    """(chunk span, body span) pairs; the chunk span adds the overlap taken from the previous body."""
    pieces = [piece for block in _blocks(content, span) for piece in _fit(content, block, budget)]
    result: list[tuple[Span, Span]] = []
    previous = None
    for body in _pack(content, pieces, budget.body_target):
        start = body[0]
        if previous is not None:
            start = _overlap_start(content, previous, body, budget.overlap)
            if body[1] - start > budget.maximum:
                start = body[0]
        result.append(((start, body[1]), body))
        previous = body
    return result


def _blocks(content: str, span: Span) -> list[Span]:
    """Paragraph-level blocks: blank lines separate them, a heading starts one,
    YAML frontmatter and fenced code stay whole."""
    lo, hi = span
    lines: list[Span] = []
    position = lo
    while position < hi:
        newline = content.find("\n", position, hi)
        end = hi if newline == -1 else newline
        lines.append((position, end))
        position = end + 1

    blocks: list[Span] = []
    current: list[int] | None = None
    fence: str | None = None

    def flush():
        nonlocal current
        if current is not None:
            trimmed = _trim(content, current[0], current[1])
            if trimmed:
                blocks.append(trimmed)
        current = None

    def add(line: Span):
        nonlocal current
        current = [line[0], line[1]] if current is None else [current[0], line[1]]

    first = 0
    if lines and content[lines[0][0]:lines[0][1]].strip() == "---":
        for end in range(1, len(lines)):
            if content[lines[end][0]:lines[end][1]].strip() == "---":
                frontmatter = _trim(content, lines[0][0], lines[end][1])
                if frontmatter:
                    blocks.append(frontmatter)
                first = end + 1
                break

    for line in lines[first:]:
        text = content[line[0]:line[1]]
        marker = _FENCE_RE.match(text)
        if fence:
            add(line)
            if marker and marker.group(1) == fence:
                fence = None
                flush()
        elif marker:
            flush()
            fence = marker.group(1)
            add(line)
        elif not text.strip():
            flush()
        elif _HEADING_RE.match(text):
            flush()
            add(line)
        else:
            add(line)
    flush()
    return blocks


def _fit(content: str, block: Span, budget: _Budget) -> list[Span]:
    """A block as pieces no longer than a chunk body: whole when it fits, else cut on
    sentence ends, then on whitespace, then hard at the limit."""
    start, end = block
    if end - start <= budget.body_max:
        return [block]
    limit = budget.body_target
    units: list[Span] = []
    for sentence in _cut_after(content, block, _SENTENCE_END_RE):
        if sentence[1] - sentence[0] <= limit:
            units.append(sentence)
            continue
        for word in _WORD_RE.finditer(content, sentence[0], sentence[1]):
            units.extend((i, min(i + limit, word.end())) for i in range(word.start(), word.end(), limit))
    return _pack(content, units, limit)


def _cut_after(content: str, span: Span, boundary: re.Pattern) -> list[Span]:
    """Cut a span after each boundary match; the pieces cover the span."""
    parts, start = [], span[0]
    for match in boundary.finditer(content, span[0], span[1]):
        parts.append((start, match.end()))
        start = match.end()
    if start < span[1]:
        parts.append((start, span[1]))
    return parts


def _pack(content: str, units: list[Span], budget: int) -> list[Span]:
    """Greedily join consecutive units into spans within the budget, measured on the original text
    between them. A unit over the budget becomes a span of its own."""
    groups: list[Span] = []
    current: Span | None = None
    for unit in units:
        if current is None:
            current = unit
            continue
        joined = _trim(content, current[0], unit[1])
        if joined is not None and joined[1] - joined[0] > budget:
            groups.append(current)
            current = unit
        else:
            current = (current[0], unit[1])
    if current is not None:
        groups.append(current)
    return [trimmed for trimmed in (_trim(content, *group) for group in groups) if trimmed]


def _overlap_start(content: str, previous: Span, body: Span, overlap: int) -> int:
    """Where a chunk starts when it repeats the end of the previous body from a word boundary.
    No overlap when that end holds no whitespace: a fragment of a word helps no search."""
    if overlap <= 0 or previous[1] - previous[0] <= overlap:
        return body[0]
    boundary = _SPACE_RE.search(content, previous[1] - overlap, previous[1])
    if boundary is None or boundary.end() >= previous[1]:
        return body[0]
    return boundary.end()


def _fit_tokens(content: str, span: Span, budget: _Budget, fits) -> list[Span]:
    """Pieces of a span that each fit the window. Every step strictly shortens the span, so this ends;
    a single code point that does not fit is refused."""
    start, end = span
    if fits(content[start:end]):
        return [span]
    if end - start <= 1:
        raise ChunkBudgetError(
            "A single character does not fit the token window; no chunk was emitted",
            field="content", start=start, end=end,
        )
    while budget.maximum > 1 and end - start <= budget.maximum:
        budget = budget.halved()
    parts = [body for _, body in _split_by_budget(content, span, budget)]
    if len(parts) < 2:
        parts = _bisect(content, span)
    result: list[Span] = []
    for part in parts:
        result.extend(_fit_tokens(content, part, budget, fits))
    return result


def _bisect(content: str, span: Span) -> list[Span]:
    """Two pieces, cut at the whitespace nearest the middle or at the middle itself."""
    start, end = span
    middle = (start + end) // 2
    cut = None
    for distance in range(0, (end - start) // 2 + 1):
        for position in (middle - distance, middle + distance):
            if start < position < end and content[position].isspace():
                cut = position
                break
        if cut is not None:
            break
    if cut is None:
        cut = middle
    return [piece for piece in (_trim(content, start, cut), _trim(content, cut, end)) if piece]


# --- profile and storage ---

def chunk_profile(window, *, target: int = CHUNK_TARGET_CHARS, maximum: int = CHUNK_MAX_CHARS,
                  overlap: int = CHUNK_OVERLAP_CHARS):
    """The ChunkProfile this splitter cuts under with a given TokenWindow.

    The overlap is measured in characters and named in `segmentation`; the profile's token
    `overlap` is 0 because no token count of the overlap is guaranteed. The window's special-token
    preparation is part of `text_preparation`, so windows that count differently never share a profile."""
    from kelmo.contracts import ChunkProfile

    return ChunkProfile(
        version=PROFILE_VERSION,
        segmentation=f"{SEGMENTATION};target_chars={target};max_chars={maximum};overlap_chars={overlap}",
        token_budget=window.budget,
        overlap=0,
        text_preparation=text_preparation(window.prefix, getattr(window, "preparation", None)),
        tokenizer_identity=window.identity,
    )


def text_preparation(document_prefix: str, preparation: str) -> str:
    """How a stored chunk reaches the model: its exact slice, after the document prefix, counted with the
    tokenizer's own special tokens plus the loader's additions named by their preparation digest.

    A profile recorded before the digest was part of this string never equals a current one: its chunks
    are re-cut by reembed and it is refused for new vectors, and it is never given a digest after the fact."""
    if not isinstance(preparation, str) or not re.fullmatch("sha256:[0-9a-f]{64}", preparation):
        raise ArchiveValidationError("The counting window names no special-token preparation", field="special_tokens")
    return f"exact-codepoint-slice;counted-as=prefix{document_prefix!r}+text+special-tokens;added={preparation}"


def check_profile(conn, profile_id: str, space, policy) -> None:
    """Refuse vectors for a profile whose chunks were not cut for this provider's actual input path:
    another tokenizer, other special-token additions, another document prefix or a budget above the
    model's input limit."""
    import json

    row = conn.execute("SELECT profile_json FROM chunk_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    if row is None:
        raise ArchiveValidationError("Unknown chunk profile", field="profile_id", received=profile_id)
    profile = json.loads(row[0])
    if profile.get("tokenizer_identity") != space.tokenizer_identity:
        raise ArchiveValidationError("The chunks were counted with another tokenizer than the model's",
                                 field="tokenizer", profile=profile.get("tokenizer_identity"),
                                 space=space.tokenizer_identity)
    if profile.get("text_preparation") != text_preparation(policy.document_prefix, policy.preparation):
        raise ArchiveValidationError("The chunks were counted with another document prefix or other special-token "
                                 "additions than the model's", field="text_preparation")
    if not isinstance(profile.get("token_budget"), int) or profile["token_budget"] > policy.max_tokens:
        raise ArchiveValidationError("The chunk budget exceeds the model's input limit", field="token_budget")


def register_profile(conn, profile) -> str:
    """Record a chunk profile inside the caller's write transaction and return its id."""
    from kelmo.store.db import now_iso
    from kelmo.store.memory.embed import canonical_json, identity_id

    if not conn.in_transaction:
        raise ArchiveStorageError("Recording a chunk profile requires a write transaction",
                              operation="register_profile")
    profile_id, body = identity_id(profile), canonical_json(profile)
    conn.execute("INSERT OR IGNORE INTO chunk_profiles (profile_id, profile_json, created_at) VALUES (?, ?, ?)",
                 (profile_id, body, now_iso()))
    stored = conn.execute("SELECT profile_json FROM chunk_profiles WHERE profile_id = ?",
                          (profile_id,)).fetchone()
    if stored[0] != body:
        raise ArchiveStorageError("Stored chunk profile differs from its identity", operation="register_profile")
    return profile_id


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def write_chunks(conn, memory_id: str, chunks: list[Chunk], *, profile_id: str, memory_version: int,
                 content: str) -> list[int]:
    """Replace a record's chunks with these, under the caller's write transaction. Returns chunk ids.

    The record's own text is never touched; old chunks are derived data and their FTS postings and
    verified vectors leave with them through the triggers. Vectors with no recorded space that are keyed
    by the old chunk ids are first copied, byte for byte, into unverified_vectors in this transaction, so
    no writer of chunks can drop them."""
    from kelmo.store.db import preserve_legacy_vectors
    from kelmo.store.memory.embed import text_sha256

    if not conn.in_transaction:
        raise ArchiveStorageError("Writing chunks requires a write transaction", operation="write_chunks")
    digest = content_sha256(content)
    for chunk in chunks:
        if content[chunk.start:chunk.end] != chunk.text:
            raise ArchiveValidationError("A chunk is not an exact slice of its record", field="chunks",
                                     chunk_idx=chunk.idx)
    replaced = [row[0] for row in conn.execute("SELECT id FROM chunks WHERE memory_id = ?", (memory_id,))]
    preserve_legacy_vectors(conn, chunk_ids=replaced)
    conn.execute("DELETE FROM chunks WHERE memory_id = ?", (memory_id,))
    ids = []
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO chunks (memory_id, idx, text, profile_id, memory_version, content_sha256, "
            "start_char, end_char, text_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, chunk.idx, chunk.text, profile_id, memory_version, digest,
             chunk.start, chunk.end, text_sha256(chunk.text)),
        )
        ids.append(cursor.lastrowid)
    return ids


def _sql_sha256(text):
    if text is None:
        return None
    return hashlib.sha256(text.encode() if isinstance(text, str) else bytes(text)).hexdigest()


def register_functions(conn) -> None:
    """archive_sha256(text) for the current-chunk query; registering again replaces the same function."""
    define_function(conn, "archive_sha256", 1, _sql_sha256)


@dataclass(frozen=True)
class CurrentChunk:
    chunk_id: int
    memory_id: str
    idx: int
    version: int
    content_sha256: str


# A chunk is current for a profile while the whole present text of its record still hashes to the text it
# was cut from and its own text is unchanged: a change anywhere in the record, inside the chunk or not,
# retires every chunk of it. Each filtered record is hashed once per query. Metadata-only revisions keep
# the text and so keep their chunks, which then answer for the record's present version.
_CURRENT_SQL = """
    WITH records AS MATERIALIZED (
        SELECT m.id AS memory_id, m.version AS version, archive_sha256(m.content) AS content_sha256
        FROM memories m WHERE {where}
    )
    SELECT c.id, c.memory_id, c.idx, r.version, r.content_sha256
    FROM chunks c JOIN records r ON r.memory_id = c.memory_id
    WHERE c.profile_id = ? AND c.content_sha256 = r.content_sha256
      AND c.start_char IS NOT NULL AND c.end_char IS NOT NULL AND c.text_sha256 = archive_sha256(c.text)
    ORDER BY c.id
"""


def current_chunks(conn, profile_id: str, where: str = "1=1", params=()) -> dict[int, CurrentChunk]:
    """The current chunks of the profile among the records `where` selects (alias m), by chunk id.
    Run it inside the read snapshot the caller answers from: versions and hashes are that snapshot's."""
    register_functions(conn)
    rows = conn.execute(_CURRENT_SQL.format(where=where), [*params, profile_id]).fetchall()
    return {row[0]: CurrentChunk(*tuple(row)) for row in rows}


def chunk_set_is_current(content: str, chunk_rows, profile_id: str) -> bool:
    """Whether a record's stored chunks are exactly the current set for this text and profile."""
    from kelmo.store.memory.embed import text_sha256

    digest = content_sha256(content)
    return bool(chunk_rows) and all(
        row["profile_id"] == profile_id and row["content_sha256"] == digest and row["start_char"] is not None
        and content[row["start_char"]:row["end_char"]] == row["text"]
        and row["text_sha256"] == text_sha256(row["text"])
        for row in chunk_rows
    )


def expand_window(conn, *, memory_id: str, chunk_idx: int, neighbors: int, profile_id: str,
                  content_sha256_expected: str, include_hidden: bool = False) -> dict:
    """Chunks chunk_idx-neighbors .. chunk_idx+neighbors (at most MAX_NEIGHBORS each way) of the record
    version the locator names, counted and read from the same current set search ranks. A changed record
    or a missing chunk is refused rather than answered with other text; a hidden record is refused unless
    the caller holds the right to read it (include_hidden)."""
    with read_snapshot(conn):
        return _expand(conn, memory_id, chunk_idx, neighbors, profile_id, content_sha256_expected, include_hidden)


@contextmanager
def read_snapshot(conn):
    """One read transaction for several statements; a caller's open transaction is used as it is."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def _expand(conn, memory_id, chunk_idx, neighbors, profile_id, content_sha256_expected, include_hidden):
    if type(chunk_idx) is not int or type(neighbors) is not int:
        raise ArchiveValidationError("chunk_idx and neighbors must be integers", field="chunk_idx")
    row = conn.execute("SELECT id, version, content FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise ArchiveNotFoundError(f"Memory not found: '{memory_id}'", id=memory_id)
    hidden = conn.execute("SELECT 1 FROM record_visibility WHERE id = ?", (memory_id,)).fetchone()
    if hidden is not None and not include_hidden:
        raise ArchiveNotFoundError(f"Memory is hidden: '{memory_id}'", id=memory_id)
    if content_sha256(row["content"]) != content_sha256_expected:
        raise ArchiveConflictError("The record changed since this chunk was found; read its exact version",
                               id=memory_id)
    neighbors = max(0, min(neighbors, MAX_NEIGHBORS))
    current = current_chunks(conn, profile_id, "m.id = ?", (memory_id,))
    ids = [chunk_id for chunk_id, chunk in current.items()
           if chunk_idx - neighbors <= chunk.idx <= chunk_idx + neighbors]
    rows = conn.execute(
        f"SELECT idx, text, start_char, end_char FROM chunks WHERE id IN ({','.join('?' * len(ids))}) "
        f"ORDER BY idx", ids,
    ).fetchall() if ids else []
    if not any(chunk["idx"] == chunk_idx for chunk in rows):
        raise ArchiveValidationError("chunk_idx does not identify a current chunk", field="chunk_idx",
                                 received=chunk_idx, chunk_total=len(current))
    return {
        "id": memory_id, "version": row["version"], "content_sha256": content_sha256_expected,
        "chunk_total": len(current),
        "chunks": [{"chunk_idx": c["idx"], "text": c["text"], "start": c["start_char"], "end": c["end_char"]}
                   for c in rows],
    }
