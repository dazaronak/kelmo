"""Retrieval over a temporary archive: chunking, hybrid search, degradation, backfill and reembed.

A bag-of-words provider and a character-counting window stand in for the model and its tokenizer:
they establish chunking, fusion, filtering and the write rechecks, not real-model retrieval quality.
"""
import hashlib
import json
import re
import sqlite3

import numpy as np
import pytest

from kelmo.store.config import CHUNK_MAX_CHARS, MAX_CHUNKS_PER_MEMORY
from kelmo.store.db import init_schema, open_connection
from kelmo.store.errors import (ArchiveConflictError, ArchiveEmbeddingError, ArchiveNotFoundError, ArchiveStorageError,
                            ArchiveValidationError)
from kelmo.store.memory.backfill import backfill, missing_chunks
from kelmo.store.memory.chunk import ChunkBudgetError, expand_window, split, split_to_fit
from kelmo.store.memory.embed import NO_ADDITIONS, InputPolicy, bound_transform, identity_id, preparation_digest
from kelmo.store.memory.reembed import reembed
from kelmo.store.memory.search import memory_search, search_chunks
from kelmo.contracts import CONTRACT_VERSION, EmbeddingSpace, VectorBatch

DIM = 256
QUIET = {"progress": lambda line: None}


def make_policy(**changes):
    """The input path Window counts: the space's tokenizer with nothing added, a 512-token limit,
    "passage: " before documents and nothing before queries."""
    fields = dict(tokenizer_identity="sha256:" + "1" * 64, max_tokens=512, query_prefix="",
                  document_prefix="passage: ", preparation=NO_ADDITIONS)
    fields.update(changes)
    return InputPolicy(**fields)


def make_space(policy=None, **changes):
    """A synthetic space whose transforms name its provider's input path, as every provider's must."""
    policy = policy or make_policy()
    fields = dict(
        contract_version=CONTRACT_VERSION, artifact_identity="synthetic-bow@sha256:" + "0" * 64,
        tokenizer_identity=policy.tokenizer_identity,
        query_transform=bound_transform("synthetic:query", policy.query_prefix, policy),
        document_transform=bound_transform("synthetic:document", policy.document_prefix, policy),
        pooling="sum", projection="word-hash", normalization="l2", dimension=DIM, dtype="float32",
        byte_order="little",
    )
    fields.update(changes)
    return EmbeddingSpace(**fields)


def bag_of_words(text, salt=""):
    """Texts sharing words land close, so ranking is meaningful without a model."""
    vector = np.zeros(DIM)
    for word in re.findall(r"\w+", text.lower()) or [text]:
        seed = int(hashlib.md5((salt + word).encode()).hexdigest(), 16) % 2**31
        vector += np.random.RandomState(seed).randn(DIM)
    return vector / np.linalg.norm(vector)


class FakeProvider:
    def __init__(self, space=None, policy=None):
        self.space = space or make_space(policy)
        self.policy = policy or make_policy(tokenizer_identity=self.space.tokenizer_identity)
        self.calls = []
        self.down = False
        self.during_documents = None
        self.during_queries = None

    def describe(self):
        return self.space

    def input_policy(self):
        return self.policy

    def health(self):
        if self.down:
            raise ArchiveEmbeddingError("synthetic outage", reason="embedding_unavailable")
        return {"ok": True}

    def embed_queries(self, texts):
        return self._embed("query", texts)

    def embed_documents(self, texts):
        return self._embed("document", texts)

    def _embed(self, purpose, texts):
        self.calls.append((purpose, list(texts)))
        if purpose == "query" and self.during_queries is not None:
            self.during_queries(texts)
        if self.down:
            raise ArchiveEmbeddingError("synthetic outage", reason="embedding_unavailable")
        if purpose == "document" and self.during_documents is not None:
            hook, self.during_documents = self.during_documents, None
            hook(texts)
        return VectorBatch(self.space, purpose,
                           tuple(tuple(float(x) for x in bag_of_words(t)) for t in texts))


class Window:
    """About three characters per token, prefix and two special tokens included. It stands for the
    synthetic space's own tokenizer, so its identity is that space's tokenizer identity."""
    budget = 512
    prefix = "passage: "
    identity = "sha256:" + "1" * 64
    preparation = NO_ADDITIONS

    def count(self, text):
        return len(self.prefix + text) // 3 + 2

    def fits(self, text):
        return self.count(text) <= self.budget


class DenseWindow(Window):
    """Two UTF-8 bytes per token: hashes, CJK and emoji overflow a character-sized chunk."""
    def count(self, text):
        return len((self.prefix + text).encode()) // 2 + 2


@pytest.fixture
def conn(tmp_path):
    connection = open_connection(str(tmp_path / "archive.db"))
    init_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def insert(conn, memory_id, content, *, kind="event", domain="dev", project="fixture", tags=("fixture",),
           valid_from="2026-09-08"):
    conn.execute(
        "INSERT INTO memories (id, content, kind, domain, project, tags_json, machine_tag, valid_from, source, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, content, kind, domain, project, json.dumps(list(tags)), "test-node", valid_from,
         "synthetic", "2026-09-08T00:00:00Z", "2026-09-08T00:00:00Z"),
    )
    conn.commit()


def build(conn, provider=None):
    return reembed(conn, Window(), provider=provider, **QUIET)


def ids(result):
    return [row["id"] for row in result["items"]]


RU_SENTENCE = "Мы перенесли черновики в общую папку проекта, потому что копии на ноутбуках расходились между собой. "


def long_note(paragraphs=12):
    parts = ["---\ndate: 2026-09-20\nproject: demo\n---", "# Заголовок заметки"]
    for i in range(paragraphs):
        if i % 4 == 0:
            parts.append(f"## Раздел {i}")
        parts.append(f"Абзац {i}. " + RU_SENTENCE * 4 + "English words stay in the same note. ")
    return "\n\n".join(parts)


def covered(content, chunks):
    marks = set()
    for chunk in chunks:
        marks.update(range(chunk.start, chunk.end))
    return all(i in marks for i, ch in enumerate(content) if not ch.isspace())


# --- chunking ---

def test_chunks_are_exact_slices_that_cover_every_character():
    note = long_note()
    chunks = split_to_fit(note, Window())
    assert len(chunks) > 3 and [c.idx for c in chunks] == list(range(len(chunks)))
    assert all(note[c.start:c.end] == c.text and c.text.strip() == c.text for c in chunks)
    assert all(len(c.text) <= CHUNK_MAX_CHARS for c in chunks)
    assert covered(note, chunks)
    assert any(previous.end > current.start for previous, current in zip(chunks, chunks[1:])), "overlap"
    assert chunks[0].text.startswith("---\ndate: 2026-09-20")
    assert split_to_fit(note, Window()) == chunks


DENSE = {
    "hex": "a3f9c1e07b" * 400,
    "base64": "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" * 150,
    "cjk": "记忆系统将笔记分块并嵌入向量以便检索。" * 200,
    "punctuation": "{[(<!--#%&*+=|~^$@;:,.?/-->)]}" * 130,
    "emoji": "🚀🧠📦🔥✅" * 260,
    "paths": " ".join(f"src/archive/search.py:{i} commit 9f8e7d6c5b4a{i:04x}" for i in range(80)),
}


@pytest.mark.parametrize("kind", sorted(DENSE))
def test_dense_records_fit_the_token_window_with_prefix_and_specials(kind):
    text, window = DENSE[kind], DenseWindow()
    assert not window.fits(text[:CHUNK_MAX_CHARS]), "the fixture must overflow a character-sized chunk"
    chunks = split_to_fit(text, window)
    assert max(window.count(c.text) for c in chunks) <= window.budget
    assert all(text[c.start:c.end] == c.text for c in chunks) and covered(text, chunks)


def test_an_overlap_that_breaks_the_window_is_dropped_not_kept():
    note = long_note(20)
    limit = 1100
    chunks = split(note, fits=lambda text: len(text) <= limit)
    assert all(len(c.text) <= limit for c in chunks) and covered(note, chunks)


def test_text_that_can_never_fit_is_refused_instead_of_emitted():
    with pytest.raises(ChunkBudgetError):
        split("x" * 5000, fits=lambda text: False)
    tiny = Window()
    tiny.budget = 3  # the prefix and special tokens alone exceed it
    with pytest.raises(ChunkBudgetError):
        split_to_fit("short", tiny)


def test_whitespace_gaps_and_crlf_stay_exact():
    content = "first line\r\nsecond line" + "\r\n" * 3000 + "tail " * 400
    chunks = split_to_fit(content, Window())
    assert all(content[c.start:c.end] == c.text and c.text.strip() for c in chunks)
    assert chunks[0].text.startswith("first line\r\nsecond line") and covered(content, chunks)
    assert split("") == [] and split(" \n" * 2000) == []


# --- search over chunks ---

def test_a_second_language_variant_finds_what_the_first_misses(conn):
    insert(conn, "ru", "Собрали релиз Nimbus на стенде, тесты прошли")
    insert(conn, "en", "Release pipeline review compared ten build caches")
    insert(conn, "other", "Router firmware stays stock")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    one = search_chunks(conn, queries=["release pipeline review"], profile_id=profile_id, provider=provider,
                        limit=1)
    assert ids(one) == ["en"] and one["search"]["degraded"] is False
    provider.calls.clear()
    both = search_chunks(conn, queries=["release pipeline review", "собрали релиз на стенде"],
                         profile_id=profile_id, provider=provider, limit=2)
    assert set(ids(both)) == {"ru", "en"}
    assert provider.calls == [("query", ["release pipeline review", "собрали релиз на стенде"])]
    provider.calls.clear()
    search_chunks(conn, queries=["Same words", "same words "], profile_id=profile_id, provider=provider)
    assert provider.calls == [("query", ["Same words"])]


def test_a_record_limit_counts_records_while_the_public_limit_still_counts_chunk_rows(conn):
    from collections import Counter

    from kelmo.store.config import MAX_RECORD_POOL
    paragraph = " ".join(["rotation schedule check passed"] * 26)
    for i in range(30):
        insert(conn, f"r{i:02d}", f"{paragraph}\n\n{paragraph} record {i}")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    assert min(Counter(row[0] for row in conn.execute("SELECT memory_id FROM chunks")).values()) >= 2
    query = dict(queries=["rotation schedule"], profile_id=profile_id, provider=provider)
    assert len(search_chunks(conn, limit=10, **query)["items"]) == 10
    grouped = Counter(ids(search_chunks(conn, limit=10, record_limit=25, **query)))
    assert len(grouped) == 25 and max(grouped.values()) <= MAX_CHUNKS_PER_MEMORY
    assert len(Counter(ids(search_chunks(conn, limit=10, record_limit=MAX_RECORD_POOL, **query)))) == 30
    for wrong in (0, MAX_RECORD_POOL + 1, True, 2.0):
        with pytest.raises(ArchiveValidationError):
            search_chunks(conn, limit=10, record_limit=wrong, **query)


def test_a_negation_word_is_a_search_term_never_an_fts_operator(conn):
    insert(conn, "refused", "The deploy was NOT approved by review")
    insert(conn, "approved", "The deploy was approved by review")
    profile_id = build(conn, FakeProvider())["profile_id"]
    for query in ("NOT approved", 'NOT "approved" OR NEAR(x)', "не одобрен NOT"):
        result = search_chunks(conn, queries=[query], profile_id=profile_id, mode="lexical")
        assert ids(result)[0] == "refused" and "distance" not in result["items"][0]


def test_rare_tags_project_and_dates_filter_before_ranking(conn):
    for i in range(150):
        insert(conn, f"common-{i}", f"lint warning formatter noise variant {i}", tags=("common",))
    for i in range(3):
        insert(conn, f"rare-{i}", f"pottery kiln firing log {i}", domain="life", tags=("ceramics_club",),
               project="pottery", valid_from=f"2026-0{i + 1}-15")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    rare = {f"rare-{i}" for i in range(3)}
    for mode in ("semantic", "hybrid"):
        def found(**filters):
            return set(ids(search_chunks(conn, queries=["lint warning formatter noise"], profile_id=profile_id,
                                         provider=provider, mode=mode, limit=10, **filters)))
        assert found(tags=["ceramics_club"]) == rare
        assert found(domain="life") == rare and found(project="pottery") == rare
        assert found(tags=["ceramics%club"]) == set() and found(tags=["ceramics_club", "absent"]) == set()
        assert found(project="pottery", since="2026-02-01", until="2026-02-28") == {"rare-1"}


def test_one_record_with_many_similar_chunks_does_not_crowd_out_the_rest(conn):
    insert(conn, "dominant", "\n\n".join(f"Run {i}. " + "the widget check passed. " * 40 for i in range(40)))
    for i in range(15):
        insert(conn, f"other-{i}", f"widget check: a separate note number {i} about case {i}")
    provider = FakeProvider()
    summary = build(conn, provider)
    assert summary["chunks"] > 40
    for mode in ("semantic", "hybrid", "lexical"):
        result = search_chunks(conn, queries=["the widget check passed"], profile_id=summary["profile_id"],
                               provider=provider, mode=mode, limit=10)
        found = ids(result)
        assert len(found) == 10 and found.count("dominant") <= MAX_CHUNKS_PER_MEMORY
        dominant = [row for row in result["items"] if row["id"] == "dominant"]
        assert len({row["chunk_idx"] for row in dominant}) == len(dominant)


def test_rows_keep_the_public_shape_and_semantic_distances_are_ordered(conn):
    for memory_id, text in (("abc", "alpha beta gamma"), ("ab", "alpha beta"), ("de", "delta epsilon")):
        insert(conn, memory_id, text)
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    result = search_chunks(conn, queries=["alpha beta gamma"], profile_id=profile_id, provider=provider,
                           mode="semantic", limit=3)
    distances = [row["distance"] for row in result["items"]]
    assert distances == sorted(distances) and result["items"][0]["id"] == "abc"
    row = result["items"][0]
    assert {"id", "content", "kind", "tags_json", "version", "chunk_idx", "chunk_total"} <= set(row)
    assert "rowid" not in row and "chunk_id" not in row
    assert row["content"] == "alpha beta gamma"[row["chunk_start"]:row["chunk_end"]]


def test_equal_scores_keep_a_stable_chunk_order(conn):
    for memory_id in ("first", "second", "third"):
        insert(conn, memory_id, "identical twin text")
    profile_id = build(conn, FakeProvider())["profile_id"]
    runs = [ids(search_chunks(conn, queries=["identical twin"], profile_id=profile_id, mode="lexical"))
            for _ in range(3)]
    assert runs[0] == ["first", "second", "third"] and runs.count(runs[0]) == 3


def test_hidden_records_are_filtered_before_ranking(conn):
    insert(conn, "visible", "shared needle words")
    insert(conn, "hidden", "shared needle words")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    conn.execute("INSERT INTO record_visibility VALUES ('hidden', '2026-10-15T00:00:00Z', 'test')")
    conn.commit()
    for mode in ("semantic", "hybrid", "lexical"):
        result = search_chunks(conn, queries=["needle"], profile_id=profile_id, provider=provider, mode=mode,
                               exclude_forgotten=True)
        assert ids(result) == ["visible"] and result["search"]["unindexed_records"] == 0


# --- degradation ---

def test_without_compatible_vectors_semantic_fails_and_hybrid_says_why(conn):
    insert(conn, "note", "a note about the router")
    profile_id = build(conn)["profile_id"]
    provider = FakeProvider()
    with pytest.raises(ArchiveEmbeddingError) as refused:
        search_chunks(conn, queries=["router"], profile_id=profile_id, provider=provider, mode="semantic")
    assert refused.value.data["reason"] == "no_compatible_vectors"
    result = search_chunks(conn, queries=["router", "роутер"], profile_id=profile_id, provider=provider)
    assert ids(result) == ["note"]
    labels = {key: result["search"][key] for key in ("mode", "degraded", "reason", "requested_mode")}
    assert labels == {"mode": "lexical", "degraded": True, "reason": "no_compatible_vectors",
                      "requested_mode": "hybrid"}
    assert provider.calls == [], "no model call when no vector could match"


def test_a_model_outage_keeps_text_chunks_and_fts_and_labels_hybrid(conn):
    insert(conn, "note", "fallback token survives the outage")
    provider = FakeProvider()
    provider.down = True
    summary = build(conn, provider)
    assert summary["chunks"] == 1 and summary["vectors"] == 0 and summary["vectors_pending"] == 1
    provider.down = False
    backfill(conn, provider, profile_id=summary["profile_id"], **QUIET, sleep=lambda s: None)
    provider.down = True
    result = search_chunks(conn, queries=["fallback token"], profile_id=summary["profile_id"], provider=provider)
    assert ids(result) == ["note"]
    assert (result["search"]["degraded"], result["search"]["reason"]) == (True, "embedding_unavailable")
    with pytest.raises(ArchiveEmbeddingError):
        search_chunks(conn, queries=["fallback token"], profile_id=summary["profile_id"], provider=provider,
                      mode="semantic")


def test_a_changed_model_identity_never_reuses_stored_vectors(conn):
    insert(conn, "note", "a note about the router")
    first = FakeProvider()
    profile_id = build(conn, first)["profile_id"]
    # Same artifact, tokenizer and dimension; only the pooling differs, so the space differs.
    second = FakeProvider(make_space(pooling="cls"))
    assert (second.space.artifact_identity, second.space.dimension) == (first.space.artifact_identity, DIM)
    result = search_chunks(conn, queries=["router"], profile_id=profile_id, provider=second)
    assert result["search"]["reason"] == "no_compatible_vectors"
    with pytest.raises(ArchiveEmbeddingError):
        search_chunks(conn, queries=["router"], profile_id=profile_id, provider=second, mode="semantic")
    assert backfill(conn, second, profile_id=profile_id, **QUIET)["written"] == 1
    spaces = {row[0] for row in conn.execute("SELECT space_id FROM chunk_vectors")}
    assert spaces == {identity_id(first.space), identity_id(second.space)}
    # A space with another tokenizer cannot embed chunks counted for this one.
    third = FakeProvider(make_space(tokenizer_identity="sha256:" + "7" * 64))
    with pytest.raises(ArchiveValidationError):
        backfill(conn, third, profile_id=profile_id, **QUIET)
    assert {row[0] for row in conn.execute("SELECT space_id FROM chunk_vectors")} == spaces


def test_fts_and_storage_failures_are_not_reported_as_degradation(conn):
    insert(conn, "note", "synthetic text")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    conn.execute("DROP TABLE chunks_fts")
    conn.commit()
    provider.down = True
    with pytest.raises(sqlite3.OperationalError):
        search_chunks(conn, queries=["synthetic"], profile_id=profile_id, provider=provider)


def test_records_without_chunks_are_reported_not_silently_missed(conn):
    insert(conn, "legacy", "written before chunks existed")
    with pytest.raises(ArchiveStorageError, match="reembed"):
        search_chunks(conn, queries=["written"], profile_id="sha256:" + "a" * 64, mode="lexical")
    profile_id = build(conn)["profile_id"]
    insert(conn, "late", "stored after the build")
    result = search_chunks(conn, queries=["stored"], profile_id=profile_id, mode="lexical")
    assert result["search"]["unindexed_records"] == 1 and ids(result) == []


@pytest.mark.parametrize("chunk_options", [dict(queries=["a", "b"]), dict(profile_id="sha256:" + "a" * 64),
                                           dict(provider=None)])
def test_the_record_api_has_no_chunk_path_that_could_drop_search_metadata(conn, chunk_options):
    with pytest.raises(TypeError):
        memory_search(conn, query="a", mode="lexical", **chunk_options)
    with pytest.raises(TypeError):
        memory_search(conn, mode="lexical")


# --- backfill and write rechecks ---

def test_backfill_attaches_every_missing_vector_and_rests_between_requests(conn):
    insert(conn, "long", "\n\n".join(f"Paragraph {i} about the embedding service. " * 25 for i in range(5)))
    provider = FakeProvider()
    provider.down = True
    summary = build(conn, provider)
    provider.down = False
    space_id = identity_id(provider.space)
    assert len(missing_chunks(conn, space_id, summary["profile_id"])) == summary["chunks"] > 2
    rests = []
    result = backfill(conn, provider, profile_id=summary["profile_id"], batch=2, sleep=rests.append, **QUIET)
    assert result["written"] == summary["chunks"] and result["stopped"] is None
    assert len(rests) == -(-summary["chunks"] // 2) - 1
    assert all(purpose == "document" and len(texts) <= 2 for purpose, texts in provider.calls[1:])
    assert backfill(conn, provider, profile_id=summary["profile_id"], **QUIET)["missing"] == 0


def test_backfill_stops_at_once_when_the_provider_is_down(conn):
    insert(conn, "note", "\n\n".join(f"Paragraph {i}. " * 80 for i in range(4)))
    provider = FakeProvider()
    provider.down = True
    summary = build(conn, provider)
    provider.calls.clear()
    result = backfill(conn, provider, profile_id=summary["profile_id"], batch=1, sleep=lambda s: None, **QUIET)
    assert result["written"] == 0 and "unavailable" in result["stopped"] and len(provider.calls) == 1


def pending_build(conn, content="first text of the record"):
    insert(conn, "note", content)
    provider = FakeProvider()
    provider.down = True
    profile_id = build(conn, provider)["profile_id"]
    provider.down = False
    return provider, profile_id


def vector_count(conn):
    return conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]


def test_a_vector_finished_after_the_record_changed_is_not_attached(conn, monkeypatch):
    from kelmo.store.memory.update import memory_update
    provider, profile_id = pending_build(conn)
    monkeypatch.setattr("kelmo.store.memory.store.embed",
                        lambda *a, **k: (_ for _ in ()).throw(ArchiveEmbeddingError("offline")))
    provider.during_documents = lambda texts: memory_update(conn, id="note", content="replaced text")
    result = backfill(conn, provider, profile_id=profile_id, **QUIET)
    assert (result["written"], result["stale"]) == (0, 1) and vector_count(conn) == 0
    assert conn.execute("SELECT content FROM memory_versions WHERE memory_id='note'").fetchone()[0] == \
        "first text of the record"


def test_a_metadata_revision_during_embedding_also_waits_for_the_next_run(conn):
    provider, profile_id = pending_build(conn)

    def bump(texts):
        conn.execute("UPDATE memories SET version = version + 1 WHERE id = 'note'")
        conn.commit()

    provider.during_documents = bump
    assert backfill(conn, provider, profile_id=profile_id, **QUIET)["stale"] == 1
    assert backfill(conn, provider, profile_id=profile_id, **QUIET)["written"] == 1


def test_a_vector_finished_after_the_record_was_hidden_is_not_attached(conn):
    provider, profile_id = pending_build(conn)

    def hide(texts):
        conn.execute("INSERT INTO record_visibility VALUES ('note', '2026-10-15T00:00:00Z', NULL)")
        conn.commit()

    provider.during_documents = hide
    result = backfill(conn, provider, profile_id=profile_id, **QUIET)
    assert (result["written"], result["hidden"]) == (0, 1) and vector_count(conn) == 0


def test_a_reused_chunk_id_does_not_inherit_a_vector(conn):
    provider, profile_id = pending_build(conn)
    insert(conn, "other", "another record entirely")
    build(conn)
    chunk_id = conn.execute("SELECT id FROM chunks WHERE memory_id = 'note'").fetchone()[0]

    def reuse(texts):
        # While the note's chunk is embedded, its id is taken over by an exact chunk of another record.
        assert texts == ["first text of the record"]
        donor = conn.execute("SELECT * FROM chunks WHERE memory_id = 'other'").fetchone()
        conn.execute("DELETE FROM chunks WHERE id IN (?, ?)", (chunk_id, donor["id"]))
        conn.execute(
            "INSERT INTO chunks (id, memory_id, idx, text, profile_id, memory_version, content_sha256, "
            "start_char, end_char, text_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chunk_id, "other", 0, donor["text"], donor["profile_id"], donor["memory_version"],
             donor["content_sha256"], donor["start_char"], donor["end_char"], donor["text_sha256"]))
        conn.commit()

    provider.during_documents = reuse
    result = backfill(conn, provider, profile_id=profile_id, batch=1, **QUIET, sleep=lambda s: None)
    assert (result["written"], result["stale"]) == (0, 1) and vector_count(conn) == 0
    again = backfill(conn, provider, profile_id=profile_id, **QUIET)
    stored = conn.execute("SELECT memory_id, text_sha256 FROM chunk_vectors WHERE chunk_id = ?",
                          (chunk_id,)).fetchall()
    assert again["written"] == 1
    assert [tuple(row) for row in stored] == [("other", hashlib.sha256(b"another record entirely").hexdigest())]


def test_reembed_leaves_a_record_changed_while_it_was_cut_for_the_next_run(conn):
    insert(conn, "note", "original words of the note")
    provider = FakeProvider()

    def change(texts):
        conn.execute("UPDATE memories SET content = 'changed words of the note' WHERE id = 'note'")
        conn.commit()

    provider.during_documents = change
    summary = build(conn, provider)
    assert summary["stale_ids"] == ["note"] and summary["chunks"] == 0 and vector_count(conn) == 0
    assert conn.execute("SELECT value FROM archive_meta WHERE key = 'chunks_built_at'").fetchone()[0] == ""
    again = build(conn, provider)
    assert again["memories"] == 1 and again["stale_ids"] == []
    assert conn.execute("SELECT text FROM chunks WHERE memory_id = 'note'").fetchone()[0] == \
        "changed words of the note"


def test_reembed_is_resumable_and_reports_unchunkable_records(conn):
    insert(conn, "good", "a good record")
    insert(conn, "blank", "   \n  ")
    first = build(conn)
    assert first["failed_ids"] == ["blank"] and first["memories"] == 1
    assert build(conn)["memories"] == 0, "current chunks are not cut again"
    assert reembed(conn, Window(), force=True, **QUIET)["memories"] == 1


@pytest.mark.parametrize("operation", ["backfill", "reembed"])
def test_a_caller_transaction_is_refused_and_left_intact(conn, tmp_path, operation):
    insert(conn, "note", "some text")
    profile_id = build(conn)["profile_id"]
    conn.execute("INSERT INTO archive_meta VALUES ('caller', 'partial')")
    assert conn.in_transaction
    with pytest.raises(ArchiveStorageError):
        if operation == "backfill":
            backfill(conn, FakeProvider(), profile_id=profile_id, **QUIET)
        else:
            build(conn, FakeProvider())
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM archive_meta WHERE key = 'caller'").fetchone()[0] == "partial"
    conn.rollback()
    assert conn.execute("SELECT value FROM archive_meta WHERE key = 'caller'").fetchone() is None


def test_legacy_chunks_and_vectors_are_kept_as_history_and_never_ranked(conn):
    insert(conn, "note", "legacy chunked words")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    chunk_id = conn.execute("INSERT INTO chunks (memory_id, idx, text) VALUES ('note', 0, 'legacy chunked')"
                            ).lastrowid
    conn.execute("INSERT INTO chunks_vec_m3 (rowid, embedding) VALUES (?, ?)",
                 (chunk_id, np.ones(4, dtype="<f4").tobytes()))
    conn.commit()
    with pytest.raises(ArchiveStorageError):
        search_chunks(conn, queries=["legacy"], profile_id=identity_id(make_space()), mode="lexical")
    provider = FakeProvider()
    summary = build(conn, provider)
    assert summary["legacy_vectors_preserved"] == 1 and summary["memories"] == 1
    kept = conn.execute("SELECT v.memory_id, v.embedding, s.verified FROM unverified_vectors v "
                        "JOIN embedding_spaces s ON s.space_id = v.space_id "
                        "WHERE v.space_id = 'legacy:chunks_vec_m3'").fetchall()
    assert [tuple(row) for row in kept] == [("note", np.ones(4, dtype="<f4").tobytes(), 0)]
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors WHERE space_id LIKE 'legacy:%'").fetchone()[0] == 0
    result = search_chunks(conn, queries=["legacy"], profile_id=summary["profile_id"], provider=provider,
                           mode="semantic")
    assert ids(result) == ["note"] and result["search"]["index_identity"].endswith(identity_id(provider.space))
    assert build(conn, provider)["legacy_vectors_preserved"] == 0


# --- vectors of unknown origin ---

def test_text_update_preserves_unknown_legacy_vector_bytes_before_deleting_chunks(conn, monkeypatch):
    from kelmo.store.memory.update import memory_update

    insert(conn, "legacy-update", "old source text")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    conn.execute("CREATE TRIGGER legacy_chunks_cleanup AFTER DELETE ON chunks BEGIN "
                 "DELETE FROM chunks_vec_m3 WHERE rowid=old.id; END")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('legacy-update',0,'old source text')").lastrowid
    vector = np.asarray([1, 2, 3, 4], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, vector))
    conn.commit()
    monkeypatch.setattr("kelmo.store.memory.store.embed", lambda *args, **kwargs: bytes(1536))

    memory_update(conn, id="legacy-update", content="replacement source text", expected_version=1)

    assert conn.execute("SELECT COUNT(*) FROM chunks_vec_m3").fetchone()[0] == 0
    kept = conn.execute("SELECT embedding FROM unverified_vectors v JOIN embedding_spaces s USING(space_id) "
                        "WHERE s.verified=0").fetchall()
    assert vector in [row[0] for row in kept]


def test_a_physical_delete_keeps_unknown_vector_bytes_and_refuses_a_caller_transaction(conn):
    from kelmo.store.memory.delete import memory_delete

    insert(conn, "doomed", "doomed text")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('doomed',0,'doomed text')").lastrowid
    vector = np.asarray([2, 7, 1, 8], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, vector))
    conn.commit()
    assert memory_delete(conn, id="doomed") == {"deleted": True}
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    kept = conn.execute("SELECT memory_id, embedding FROM unverified_vectors").fetchall()
    assert [tuple(row) for row in kept] == [("doomed", vector)]
    insert(conn, "other", "other text")
    conn.execute("INSERT INTO archive_meta VALUES ('caller', 'partial')")
    with pytest.raises(ArchiveStorageError):
        memory_delete(conn, id="other")
    assert conn.in_transaction
    conn.rollback()


def test_reembed_preserves_orphans_before_a_chunk_id_can_be_reused(conn):
    insert(conn, "fresh", "new indexed source")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    vector = np.asarray([5, 6, 7, 8], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (1,?)", (vector,))
    conn.commit()
    result = build(conn, FakeProvider())
    assert result["legacy_vectors_preserved"] == 1
    kept = conn.execute("SELECT memory_id,text_sha256,embedding FROM unverified_vectors").fetchone()
    assert tuple(kept) == (None, None, vector)


def test_orphan_unknown_vectors_are_preserved_without_fabricating_a_source(conn):
    from kelmo.store.db import preserve_legacy_vectors

    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    vector = np.asarray([5, 6, 7, 8], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (99,?)", (vector,))
    conn.commit()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        assert preserve_legacy_vectors(conn) == 1
    kept = conn.execute("SELECT v.memory_id,v.text_sha256,v.embedding FROM unverified_vectors v "
                        "JOIN embedding_spaces s USING(space_id) WHERE s.verified=0").fetchone()
    assert kept is not None and kept[0] is None and kept[1] is None and kept[2] == vector


def test_distinct_unknown_vector_generations_do_not_overwrite_each_other(conn):
    from kelmo.store.db import preserve_legacy_vectors

    insert(conn, "legacy-generations", "source text")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('legacy-generations',0,'source text')").lastrowid
    original = np.asarray([1, 2, 3, 4], dtype="<f4").tobytes()
    replacement = np.asarray([4, 3, 2, 1], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, original))
    conn.commit()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        assert preserve_legacy_vectors(conn) == 1
    with conn:
        conn.execute("UPDATE chunks_vec_m3 SET embedding=? WHERE rowid=?", (replacement, chunk_id))
        assert preserve_legacy_vectors(conn) == 1
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        assert preserve_legacy_vectors(conn) == 0
    kept = conn.execute("SELECT embedding FROM unverified_vectors v JOIN embedding_spaces s USING(space_id) "
                        "WHERE s.verified=0").fetchall()
    assert sorted(row[0] for row in kept) == sorted([original, replacement])


def test_a_reused_chunk_id_never_attributes_an_unknown_vector_to_the_new_text(conn):
    from kelmo.store.db import preserve_legacy_vectors

    insert(conn, "first", "first record text")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('first',0,'legacy cut')").lastrowid
    vector = np.asarray([9, 8, 7, 6], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, vector))
    # Removed without its vector (nothing here clears chunks_vec_m3): the vector is now an orphan.
    conn.execute("DELETE FROM chunks WHERE id=?", (chunk_id,))
    conn.commit()
    provider = FakeProvider()
    assert build(conn, provider)["legacy_vectors_preserved"] == 1
    reused = conn.execute("SELECT memory_id, profile_id FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    assert reused is not None and reused["profile_id"] is not None, "the fixture must reuse the chunk id"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        assert preserve_legacy_vectors(conn) == 0
    kept = conn.execute("SELECT source_rowid, memory_id, idx, text_sha256, embedding FROM unverified_vectors"
                        ).fetchall()
    assert [tuple(row) for row in kept] == [(chunk_id, None, None, None, vector)]
    verified = conn.execute("SELECT v.embedding FROM chunk_vectors v JOIN embedding_spaces s USING(space_id) "
                            "WHERE v.chunk_id=? AND s.verified=1", (chunk_id,)).fetchall()
    assert vector not in [row[0] for row in verified]
    result = search_chunks(conn, queries=["first record"], profile_id=build(conn)["profile_id"],
                           provider=provider, mode="semantic")
    assert result["search"]["index_identity"].endswith(identity_id(provider.space))


def test_a_failed_text_update_rolls_back_its_preservation_with_the_text(conn, monkeypatch):
    from kelmo.store.memory.update import memory_update

    insert(conn, "kept", "original source text")
    insert(conn, "untouched", "another record")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('kept',0,'original source text')"
                            ).lastrowid
    other_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('untouched',0,'another record')"
                            ).lastrowid
    vector = np.asarray([1, 1, 2, 3], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, vector))
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)",
                 (other_id, np.zeros(4, dtype="<f4").tobytes()))
    conn.execute("CREATE TRIGGER fail_chunk_delete BEFORE DELETE ON chunks BEGIN "
                 "SELECT RAISE(ABORT, 'synthetic storage failure'); END")
    conn.commit()
    monkeypatch.setattr("kelmo.store.memory.store.embed", lambda *args, **kwargs: bytes(1536))
    with pytest.raises(ArchiveStorageError):
        memory_update(conn, id="kept", content="replacement source text", expected_version=1)
    assert not conn.in_transaction
    assert tuple(conn.execute("SELECT content, version FROM memories WHERE id='kept'").fetchone()) == \
        ("original source text", 1)
    assert conn.execute("SELECT COUNT(*) FROM unverified_vectors").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_vec_m3").fetchone()[0] == 2

    conn.execute("DROP TRIGGER fail_chunk_delete")
    conn.commit()
    memory_update(conn, id="kept", content="replacement source text", expected_version=1)
    kept = conn.execute("SELECT source_table, source_rowid, memory_id, embedding FROM unverified_vectors "
                        "ORDER BY source_table").fetchall()
    # Bounded to the updated record: the other record's vector is not copied on this write.
    assert [tuple(row) for row in kept] == [("chunks_vec_m3", chunk_id, "kept", vector)]


def test_schema_initialization_refuses_a_caller_transaction(conn):
    conn.execute("INSERT INTO archive_meta VALUES ('caller', 'partial')")
    with pytest.raises(sqlite3.ProgrammingError):
        init_schema(conn)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT value FROM archive_meta WHERE key = 'caller'").fetchone() is None


# ---

class SilentProvider(FakeProvider):
    """A provider that cannot establish its space at all (a missing model, a broker that is down)."""
    def describe(self):
        raise ArchiveEmbeddingError("synthetic outage before any embedding", reason="embedding_unavailable")


def test_reembed_keeps_text_chunks_and_fts_when_the_provider_cannot_describe_itself(conn):
    insert(conn, "note", "durable lexical words")
    summary = build(conn, SilentProvider())
    assert (summary["memories"], summary["chunks"], summary["vectors"]) == (1, 1, 0)
    assert summary["vectors_unavailable"] == "embedding_unavailable" and summary["space_id"] is None
    result = search_chunks(conn, queries=["durable"], profile_id=summary["profile_id"], provider=SilentProvider())
    assert ids(result) == ["note"] and result["search"]["reason"] == "embedding_unavailable"
    stopped = backfill(conn, SilentProvider(), profile_id=summary["profile_id"], **QUIET)
    assert stopped["missing"] is None and "unavailable" in stopped["stopped"] and vector_count(conn) == 0


class PolicyProvider(FakeProvider):
    """A consistent provider on another input path: its space names the changed policy."""
    def __init__(self, **policy):
        super().__init__(policy=make_policy(**policy))


ADDS_SEP = preparation_digest(["[SEP]"])


@pytest.mark.parametrize("policy", [dict(max_tokens=256), dict(document_prefix=""),
                                    dict(tokenizer_identity="sha256:" + "9" * 64), dict(preparation=ADDS_SEP)])
def test_a_window_that_is_not_the_providers_input_path_is_refused_before_any_write(conn, policy):
    insert(conn, "note", "some text")
    with pytest.raises(ArchiveValidationError):
        build(conn, PolicyProvider(**policy))
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunk_profiles").fetchone()[0] == 0


class NoHealthProvider(FakeProvider):
    """Refuses one text as malformed and has no health check: isolation must follow the reason."""
    health = None

    def __init__(self, bad):
        super().__init__()
        self.bad = bad

    def _embed(self, purpose, texts):
        if self.bad in texts and not self.down:
            self.calls.append((purpose, list(texts)))
            raise ArchiveEmbeddingError("synthetic NaN output", reason="malformed_vectors")
        return super()._embed(purpose, texts)


def test_backfill_skips_one_bad_chunk_and_embeds_the_rest(conn):
    for memory_id in ("a-first", "b-bad", "c-last"):
        insert(conn, memory_id, f"record {memory_id} words")
    provider = NoHealthProvider("record b-bad words")
    provider.down = True
    profile_id = build(conn, provider)["profile_id"]
    provider.down = False
    bad = conn.execute("SELECT id FROM chunks WHERE memory_id = 'b-bad'").fetchone()[0]
    result = backfill(conn, provider, profile_id=profile_id, batch=8, **QUIET)
    assert (result["written"], result["failed"], result["stopped"]) == (2, [bad], None)
    assert backfill(conn, provider, profile_id=profile_id, batch=8, **QUIET)["failed"] == [bad]


class FailingProvider(FakeProvider):
    """Healthy by its own check, yet every document call fails for `reason`."""
    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    def _embed(self, purpose, texts):
        self.calls.append((purpose, list(texts)))
        raise ArchiveEmbeddingError(f"synthetic {self.reason}", reason=self.reason)


@pytest.mark.parametrize("reason", ["space_mismatch", "protocol_error", "timeout", "embedding_unavailable",
                                    "model_missing"])
def test_backfill_stops_on_a_failure_that_is_not_one_inputs(conn, reason):
    for memory_id in ("a", "b", "c"):
        insert(conn, memory_id, f"record {memory_id} words")
    profile_id = pending_build(conn, "record note words")[1]
    provider = FailingProvider(reason)
    assert provider.health() == {"ok": True}
    result = backfill(conn, provider, profile_id=profile_id, batch=8, **QUIET)
    assert (result["written"], result["failed"]) == (0, []) and reason in result["stopped"]
    assert len(provider.calls) == 1, "no per-chunk retry of a provider-wide failure"


def test_text_update_records_the_text_an_old_record_vector_was_observed_with(conn, monkeypatch):
    from kelmo.store.memory.update import memory_update

    insert(conn, "record", "old record text")
    rowid = conn.execute("SELECT rowid FROM memories WHERE id = 'record'").fetchone()[0]
    old_vector = np.full(384, 0.5, dtype="<f4").tobytes()
    conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (rowid, old_vector))
    conn.commit()
    monkeypatch.setattr("kelmo.store.memory.store.embed",
                        lambda *a, **k: (_ for _ in ()).throw(ArchiveEmbeddingError("offline")))
    memory_update(conn, id="record", content="new record text", expected_version=1)
    kept = conn.execute("SELECT memory_id, text_sha256, embedding FROM unverified_vectors "
                        "WHERE source_table = 'memories_vec'").fetchall()
    assert [tuple(row) for row in kept] == [
        ("record", hashlib.sha256(b"old record text").hexdigest(), old_vector)]


def test_write_chunks_itself_preserves_unknown_vectors_before_deleting(conn):
    from kelmo.store.memory.chunk import write_chunks

    content = "ingested record text"
    insert(conn, "ingested", content)
    conn.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    conn.execute("CREATE TRIGGER legacy_chunks_cleanup AFTER DELETE ON chunks BEGIN "
                 "DELETE FROM chunks_vec_m3 WHERE rowid=old.id; END")
    chunk_id = conn.execute("INSERT INTO chunks(memory_id,idx,text) VALUES ('ingested',0,'legacy window')"
                            ).lastrowid
    vector = np.asarray([3, 1, 4, 1], dtype="<f4").tobytes()
    conn.execute("INSERT INTO chunks_vec_m3(rowid,embedding) VALUES (?,?)", (chunk_id, vector))
    conn.commit()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        write_chunks(conn, "ingested", split_to_fit(content, Window()), profile_id="sha256:" + "c" * 64,
                     memory_version=1, content=content)
    assert conn.execute("SELECT COUNT(*) FROM chunks_vec_m3").fetchone()[0] == 0
    kept = conn.execute("SELECT source_rowid, memory_id, embedding FROM unverified_vectors").fetchall()
    assert [tuple(row) for row in kept] == [(chunk_id, "ingested", vector)]


def test_a_text_change_outside_every_chunk_retires_the_records_chunks(conn):
    content = "first indexed sentence about the harbour."
    insert(conn, "note", content)
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    old = hashlib.sha256(content.encode()).hexdigest()
    # Appended outside the chunk's coordinates: the stored slice still matches, the record does not.
    conn.execute("UPDATE memories SET content = content || ' A later appended clause.' WHERE id = 'note'")
    conn.commit()
    for mode in ("semantic", "hybrid", "lexical"):
        result = search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=provider, mode=mode)
        assert ids(result) == [] and result["search"]["unindexed_records"] == 1
    with pytest.raises(ArchiveConflictError):
        expand_window(conn, memory_id="note", chunk_idx=0, neighbors=0, profile_id=profile_id,
                      content_sha256_expected=old)
    assert build(conn, provider)["memories"] == 1
    assert ids(search_chunks(conn, queries=["harbour"], profile_id=profile_id, mode="lexical")) == ["note"]


def test_a_metadata_revision_keeps_vectors_and_binds_hits_to_the_present_version(conn):
    from kelmo.store.memory.update import memory_update

    content = "metadata revision keeps this text"
    insert(conn, "note", content)
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    vectors_before = vector_count(conn)
    memory_update(conn, id="note", project="moved", expected_version=1)
    result = search_chunks(conn, queries=["metadata revision"], profile_id=profile_id, provider=provider,
                           mode="semantic")
    hit = result["items"][0]
    assert (hit["id"], hit["version"], hit["project"]) == ("note", 2, "moved")
    assert hit["record_content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert hit["chunk_total"] == 1 and vector_count(conn) == vectors_before
    window = expand_window(conn, memory_id="note", chunk_idx=hit["chunk_idx"], neighbors=1,
                           profile_id=profile_id, content_sha256_expected=hit["record_content_sha256"])
    assert window["version"] == 2 and window["chunk_total"] == hit["chunk_total"]
    conn.execute("INSERT INTO record_visibility VALUES ('note', '2026-10-15T00:00:00Z', NULL)")
    conn.commit()
    with pytest.raises(ArchiveNotFoundError):
        expand_window(conn, memory_id="note", chunk_idx=0, neighbors=0, profile_id=profile_id,
                      content_sha256_expected=hit["record_content_sha256"])


def test_malformed_filters_are_refused_not_read_as_something_else(conn):
    insert(conn, "note", "kiln schedule note", tags=("ceramics_club",))
    profile_id = build(conn)["profile_id"]
    for bad in (dict(tags="ceramics_club"), dict(tags=[""]), dict(since="2026-13-01"),
                dict(until="yesterday"), dict(project=""), dict(limit="10"),
                dict(since="٢٠٢٦-01-01"), dict(until="2026-0١-01"),  # non-ASCII digits
                dict(since="2026-01-01\n"), dict(until="2026-01-01x"),  # trailing bytes
                dict(since="2026-02-30")):  # not a calendar day
        with pytest.raises(ArchiveValidationError):
            search_chunks(conn, queries=["note"], profile_id=profile_id, mode="lexical", **bad)


def test_a_query_of_short_words_matches_any_of_them(conn):
    insert(conn, "first", "ab first")
    insert(conn, "second", "cd second")
    profile_id = build(conn)["profile_id"]
    found = ids(search_chunks(conn, queries=["ab cd"], profile_id=profile_id, mode="lexical"))
    assert sorted(found) == ["first", "second"]


# ---

class PreparedWindow(Window):
    """The same raw tokenizer.json, counted as a loader that adds [SEP] as a special token counts it."""
    preparation = ADDS_SEP


def test_windows_with_other_special_token_additions_never_share_a_profile(conn):
    insert(conn, "note", "some words to cut")
    raw = build(conn)
    prepared = reembed(conn, PreparedWindow(), **QUIET)
    assert raw["profile_id"] != prepared["profile_id"] and prepared["memories"] == 1, "re-cut, not skipped"
    provider = PolicyProvider(preparation=ADDS_SEP)
    with pytest.raises(ArchiveValidationError):
        backfill(conn, provider, profile_id=raw["profile_id"], **QUIET)
    assert backfill(conn, provider, profile_id=prepared["profile_id"], **QUIET)["written"] == 1


def test_a_profile_recorded_without_its_additions_is_kept_refused_and_re_cut(conn):
    from dataclasses import replace

    from kelmo.store.memory.chunk import chunk_profile
    from kelmo.store.memory.embed import canonical_json

    insert(conn, "note", "older profile words")
    build(conn)
    legacy = replace(chunk_profile(Window()),
                     text_preparation="exact-codepoint-slice;counted-as=prefix'passage: '+text+special-tokens")
    legacy_id, body = identity_id(legacy), canonical_json(legacy)
    conn.execute("INSERT INTO chunk_profiles (profile_id, profile_json, created_at) VALUES (?, ?, ?)",
                 (legacy_id, body, "2026-10-01T00:00:00Z"))
    conn.execute("UPDATE chunks SET profile_id = ? WHERE memory_id = 'note'", (legacy_id,))
    conn.commit()
    with pytest.raises(ArchiveValidationError):
        backfill(conn, FakeProvider(), profile_id=legacy_id, **QUIET)
    assert vector_count(conn) == 0
    assert build(conn)["memories"] == 1, "chunks of the old profile are not taken as current"
    stored = conn.execute("SELECT profile_json FROM chunk_profiles WHERE profile_id = ?", (legacy_id,)).fetchone()
    assert stored[0] == body, "the old profile is kept as it was, with no identity assigned after the fact"


def test_missing_vectors_are_counted_and_partial_coverage_is_degraded(conn):
    for memory_id, project in (("a", "p"), ("b", "gap"), ("c", "p")):
        insert(conn, memory_id, f"harbour crane {memory_id}", project=project)
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]

    def search(**options):
        return search_chunks(conn, queries=["harbour crane"], profile_id=profile_id, provider=provider, **options)

    full = search(mode="semantic")["search"]
    assert (full["vectorless_chunks"], full["degraded"]) == (0, False)
    conn.execute("DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE memory_id = 'b')")
    conn.commit()
    semantic = search(mode="semantic")
    assert sorted(ids(semantic)) == ["a", "c"]
    assert {key: semantic["search"][key] for key in ("mode", "degraded", "reason", "vectorless_chunks")} == \
        {"mode": "semantic", "degraded": True, "reason": "partial_vector_coverage", "vectorless_chunks": 1}
    hybrid = search()
    assert sorted(ids(hybrid)) == ["a", "b", "c"] and hybrid["search"]["reason"] == "partial_vector_coverage"
    with pytest.raises(ArchiveEmbeddingError) as refused:
        search(mode="semantic", project="gap")
    assert refused.value.data["reason"] == "no_compatible_vectors"
    gap = search(project="gap")
    assert ids(gap) == ["b"] and (gap["search"]["reason"], gap["search"]["vectorless_chunks"]) == \
        ("no_compatible_vectors", 1)
    assert search(mode="lexical")["search"]["vectorless_chunks"] is None
    conn.execute("INSERT INTO record_visibility VALUES ('b', '2026-10-15T00:00:00Z', NULL)")
    conn.commit()
    hidden = search(mode="semantic")["search"]
    assert (hidden["vectorless_chunks"], hidden["degraded"]) == (0, False)


# ---

def test_an_old_profile_under_a_new_document_input_path_is_refused_on_search(conn):
    insert(conn, "note", "harbour crane words")
    profile_id = build(conn, FakeProvider())["profile_id"]
    new = PolicyProvider(document_prefix="")  # same model, documents now embedded without "passage: "
    with pytest.raises(ArchiveEmbeddingError) as refused:
        search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=new, mode="semantic")
    assert refused.value.data["reason"] == "profile_policy_mismatch"
    result = search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=new)
    assert ids(result) == ["note"] and result["search"]["reason"] == "profile_policy_mismatch"
    assert new.calls == [], "no query is embedded for a profile cut for another input path"


def test_a_query_only_input_path_change_never_ranks_against_the_old_vectors(conn):
    insert(conn, "note", "harbour crane words")
    old = FakeProvider()
    profile_id = build(conn, old)["profile_id"]
    drifted = PolicyProvider(query_prefix="query: ")  # the document path, and so the profile, still match
    assert identity_id(drifted.space) != identity_id(old.space)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=drifted, mode="semantic")
    assert refused.value.data["reason"] == "no_compatible_vectors"
    result = search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=drifted)
    assert ids(result) == ["note"] and result["search"]["reason"] == "no_compatible_vectors"
    assert drifted.calls == []


def test_a_provider_whose_space_does_not_name_its_input_path_is_refused(conn):
    insert(conn, "note", "harbour crane words")
    profile_id = build(conn, FakeProvider())["profile_id"]
    unbound = FakeProvider(make_space(query_transform="synthetic:query"))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=unbound, mode="semantic")
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_query_vectors_are_resolved_before_the_read_snapshot_opens(conn):
    insert(conn, "note", "harbour crane words")
    provider = FakeProvider()
    profile_id = build(conn, provider)["profile_id"]
    seen = []
    provider.during_queries = lambda texts: seen.append(conn.in_transaction)
    result = search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=provider, mode="semantic")
    assert ids(result) == ["note"] and seen == [False]
    provider.down = True
    result = search_chunks(conn, queries=["harbour"], profile_id=profile_id, provider=provider)
    assert result["search"]["reason"] == "embedding_unavailable" and ids(result) == ["note"]
    assert seen == [False, False] and not conn.in_transaction


# --- exact chunk expansion ---

def test_expansion_is_bounded_exact_and_refuses_a_changed_record(conn):
    content = "\n\n".join(f"Section {i}. " + "words of the section. " * 60 for i in range(8))
    insert(conn, "note", content)
    profile_id = build(conn)["profile_id"]
    digest = hashlib.sha256(content.encode()).hexdigest()
    total = conn.execute("SELECT COUNT(*) FROM chunks WHERE memory_id = 'note'").fetchone()[0]
    window = expand_window(conn, memory_id="note", chunk_idx=2, neighbors=50, profile_id=profile_id,
                           content_sha256_expected=digest)
    assert [c["chunk_idx"] for c in window["chunks"]] == list(range(0, min(total, 6)))
    assert all(content[c["start"]:c["end"]] == c["text"] for c in window["chunks"])
    with pytest.raises(ArchiveValidationError):
        expand_window(conn, memory_id="note", chunk_idx=total, neighbors=0, profile_id=profile_id,
                      content_sha256_expected=digest)
    conn.execute("UPDATE memories SET content = content || ' appended' WHERE id = 'note'")
    conn.commit()
    with pytest.raises(ArchiveConflictError):
        expand_window(conn, memory_id="note", chunk_idx=2, neighbors=1, profile_id=profile_id,
                      content_sha256_expected=digest)
