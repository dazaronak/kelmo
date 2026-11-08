"""Retrieval through Service and its real LocalArchiveAdapter.

The configured provider is the real LocalFastEmbedProvider over synthetic pinned files: a word-level
tokenizer, its policy files, config.json and a placeholder graph. Only the fastembed backend is replaced,
by FakeBackend: its registry entry and its TextEmbedding, which returns bag-of-words vectors. These cases
prove wiring, durability, identity and transaction boundaries. They are not model quality and they do
not prove that fastembed loads any name: the real pinned model is test_pinned_model_probe, which the
controller runs separately with KELMO_NATIVE_MODEL_DIR set.
"""
import hashlib
import json
import os
import re
from dataclasses import replace

import numpy as np
import pytest

MODEL = "synthetic/bag-of-words"
DIM = 64
LIMIT = 64
SIZES = dict(target_chars=300, max_chars=400, overlap_chars=40)
PREFIXES = ("query: ", "passage: ")


def bag_of_words(text):
    for prefix in PREFIXES:
        text = text[len(prefix):] if text.startswith(prefix) else text
    vector = np.zeros(DIM)
    for word in re.findall(r"\w+", text.lower()) or [text]:
        seed = int(hashlib.md5(word.encode()).hexdigest(), 16) % 2**31
        vector += np.random.RandomState(seed).randn(DIM)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


class FakeBackend:
    """Stands in for fastembed only, honestly not a model: a registry entry for MODEL and a TextEmbedding
    whose query and passage calls are recorded and answered with bag-of-words vectors."""

    def __init__(self):
        self.calls = []
        self.loads = 0
        self.down = False

    def description(self, model):
        if model != MODEL:
            return None
        return {"model": MODEL, "dim": DIM, "model_file": "model.onnx", "additional_files": []}

    def load(self, model, path):
        if self.down:
            raise RuntimeError("synthetic backend outage while loading")
        self.loads += 1
        return self

    def _embed(self, kind, texts):
        if self.down:
            raise RuntimeError("synthetic backend outage")
        texts = list(texts)
        self.calls.append((kind, texts))
        return [bag_of_words(text) for text in texts]

    def query_embed(self, texts):
        return self._embed("query", texts)

    def passage_embed(self, texts):
        return self._embed("passage", texts)

    def kinds(self):
        return [kind for kind, _ in self.calls]


def tokenizer_bytes():
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "[PAD]": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)])
    return tokenizer.to_str().encode()


def model_files(directory):
    directory.mkdir(parents=True)
    contents = {
        "model.onnx": b"placeholder graph: the fake backend never reads it",
        "config.json": json.dumps({"hidden_size": DIM}).encode(),
        "tokenizer.json": tokenizer_bytes(),
        "tokenizer_config.json": json.dumps({"model_max_length": 128, "max_length": LIMIT}).encode(),
        "special_tokens_map.json": json.dumps({"cls_token": "[CLS]", "sep_token": "[SEP]",
                                               "pad_token": "[PAD]", "unk_token": "[UNK]"}).encode(),
    }
    pins = {}
    for name, data in contents.items():
        (directory / name).write_bytes(data)
        pins[name] = (len(data), hashlib.sha256(data).hexdigest())
    return pins


def local_section(pins, *, document_prefix="passage: "):
    lines = ['[embedding]', 'provider = "local"', f'model = "{MODEL}"', 'model_dir = ".cache/embeddings/custom"',
             'pooling = "mean"', 'projection = "none"', 'normalization = "l2"', f'dimension = {DIM}',
             'query_prefix = "query: "', f'document_prefix = "{document_prefix}"']
    lines += [f"{key} = {value}" for key, value in SIZES.items()]
    lines += ["[embedding.files]"] + [f'"{name}" = [{size}, "{digest}"]' for name, (size, digest) in pins.items()]
    return "\n".join(lines) + "\n"


def set_embedding(root, section):
    path = root / "kelmo.toml"
    text = path.read_text().split("[embedding]")[0]
    path.write_text(text + section)


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr("kelmo.embeddings.backend_model_description", fake.description)
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding", fake.load)

    def record_model(*args, **kwargs):
        raise AssertionError("a configured vault must never fall back to the built-in record model")

    monkeypatch.setattr("kelmo.store.memory.store.embed", record_model)
    monkeypatch.setattr("kelmo.store.memory.search.embed", record_model)
    return fake


def git_identity(monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Synthetic Fixture")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "fixture@example.invalid")


@pytest.fixture
def pins(tmp_path, monkeypatch, backend):
    """A configured vault: its root is tmp_path/vault, the returned value is the model pin table."""
    from kelmo.service import Service
    git_identity(monkeypatch)
    Service.initialize(tmp_path / "vault", "fixture-node")
    table = model_files(tmp_path / "vault/.cache/embeddings/custom")
    set_embedding(tmp_path / "vault", local_section(table))
    return table


@pytest.fixture
def root(tmp_path, pins):
    return tmp_path / "vault"


@pytest.fixture
def service(root):
    from kelmo.service import Service
    with Service(root) as opened:
        yield opened


def archive(service, content, filename, **packet):
    from kelmo.contracts import SourcePacket
    if packet:
        source = SourcePacket(content.encode(), "raw_note", hashlib.sha256(content.encode()).hexdigest(), filename,
                              "2026-10-15T10:00:00+00:00", **packet)
        operation = service.capture_source(source, service.origin, "key-" + filename)
    else:
        operation = service.capture(content, filename, "implementer")
    done = service.run(operation["id"])
    assert done["state"] == "archived", done
    return done["archive_id"]


def chunk_rows(service, archive_id):
    return service.archive.execute("SELECT id, idx, text, profile_id FROM chunks WHERE memory_id=? ORDER BY idx",
                                   (archive_id,)).fetchall()


def vector_count(service, archive_id=None):
    sql = "SELECT COUNT(*) FROM chunk_vectors" + (" WHERE memory_id=?" if archive_id else "")
    return service.archive.execute(sql, (archive_id,) if archive_id else ()).fetchone()[0]


RU_LONG = "\n\n".join(
    f"Абзац {i}. Мы пересадили рассаду томатов в теплицу, потому что на подоконнике ей не хватало "
    f"света, и проверили полив грядки номер {i} после первой недели." for i in range(12))


# --- intake ---

def test_configured_capture_cuts_fitted_chunks_and_embeds_with_the_configured_provider(service, backend):
    archive_id = archive(service, RU_LONG, "2026-10-01_greenhouse.md")
    index = service.archive_api.chunk_index
    rows = chunk_rows(service, archive_id)
    assert len(rows) > 2 and {row["profile_id"] for row in rows} == {index.profile_id()}
    window = index.window()
    assert all(window.fits(row["text"]) and RU_LONG.find(row["text"]) >= 0 for row in rows)
    assert "target_chars=300;max_chars=400;overlap_chars=40" in index.profile().segmentation
    assert vector_count(service, archive_id) == len(rows)
    passages = [texts for kind, texts in backend.calls if kind == "passage"]
    assert backend.kinds() == ["passage"] and all(text.startswith("passage: ") for text in passages[0])
    assert service.archive_api.get(archive_id).vectors_pending is False


def test_configured_search_ranks_with_the_query_path_and_keeps_the_record_shape(service, backend):
    first = archive(service, "Pipeline review compared ten build caches", "2026-10-01_pipeline.md")
    archive(service, "Router firmware stays stock", "2026-10-01_router.md")
    backend.calls.clear()
    result = service.search("pipeline build caches", with_metadata=True, limit=1)
    assert result["search"] == {"mode": "hybrid", "degraded": False, "reason": None}
    item = result["items"][0]
    assert item["id"] == first and item["content"] == "Pipeline review compared ten build caches"
    assert {"id", "content", "kind", "tags_json", "version"} <= set(item) and "chunk_idx" not in item
    assert backend.calls == [("query", ["query: pipeline build caches"])]


def test_variants_hits_carry_exact_evidence_locators_and_expand(service):
    from kelmo.contracts import SearchFilters
    english = archive(service, "Rolled back the cache release, the hotfix is merged", "2026-10-01_en.md")
    russian = archive(service, RU_LONG, "2026-10-01_ru.md")
    result = service.archive_api.search(["cache release hotfix", "полив грядки в теплице"],
                                        SearchFilters(), 5)
    assert {hit.evidence.archive_id for hit in result.hits} == {english, russian}
    assert result.index_identity.startswith("chunk:") and result.degraded is False
    hit = next(hit for hit in result.hits if hit.evidence.archive_id == russian)
    assert hit.evidence == service.archive_api.get(russian).evidence
    locator = hit.chunks[0]
    assert locator.index_generation == service.archive_api.chunk_index.generation()
    window = service.archive_api.expand(locator, 1)
    assert hit.content in window.chunks and len(window.chunks) <= 3
    assert all(text in RU_LONG for text in window.chunks)


def test_rare_filters_and_precise_dates_select_before_ranking(service):
    for i in range(20):
        archive(service, f"formatter lint warning noise variant {i}", f"2026-03-0{i % 9 + 1}_common-{i}.md",
                project="common", valid_from="2026-03-01")
    rare = archive(service, "schema migration rollback schedule", "2026-02-15_rare.md", project="migrations",
                   valid_from="2026-02-15")
    found = service.search("formatter lint warning noise", project="migrations", limit=5)
    assert [row["id"] for row in found] == [rare]

    def migrations(**dates):
        return [row["id"] for row in service.search("schema migration", project="migrations", **dates)]

    assert migrations(until="2026-02-15") == [rare] and migrations(since="2026-02-16") == []
    # created_at is the fallback date and is compared as a UTC instant, never as text.
    service.archive.execute("UPDATE memories SET valid_from=NULL, created_at='2026-04-17T01:00:00+02:00' "
                            "WHERE id=?", (rare,))
    service.archive.commit()
    assert migrations(since="2026-04-17") == [] and migrations(until="2026-04-16") == [rare]


def test_hidden_records_are_excluded_before_the_limit_and_refuse_expansion(service):
    from kelmo.contracts import SearchFilters
    from kelmo.errors import KelmoError
    best = archive(service, "needle haystack needle haystack", "2026-10-01_best.md")
    second = archive(service, "needle in a field", "2026-10-01_second.md")
    locator = service.archive_api.search(["needle haystack"], SearchFilters(), 1).hits[0].chunks[0]
    assert locator.archive_id == best
    service.forget(best)
    assert [row["id"] for row in service.search("needle haystack", limit=1)] == [second]
    with pytest.raises(KelmoError) as refused:
        service.archive_api.expand(locator, 1)
    assert refused.value.code == "E_FORGOTTEN"


def test_a_record_limit_above_half_the_chunk_clamp_returns_that_many_records(service, monkeypatch):
    """Sixty records of two matching chunks each, one constant vector for every text and a query word no
    record holds: only the vector ranking decides, so a chunk-counted clamp would stop at fifty records."""
    import sys
    from kelmo.contracts import SearchFilters
    direction = np.zeros(DIM, dtype=np.float32)
    direction[0] = 1
    monkeypatch.setattr(sys.modules[__name__], "bag_of_words", lambda text: direction.copy())
    paragraph = "pipeline cache runtime validation " * 7
    identifiers = []
    for number in range(60):
        identifier = archive(service, f"{paragraph}\n\n{paragraph} item {number}", f"2026-10-01_record-{number}.md")
        assert len(chunk_rows(service, identifier)) >= 2
        identifiers.append(identifier)
    result = service.search("absentlexicaltoken", limit=60, with_metadata=True)
    assert result["search"] == {"mode": "hybrid", "degraded": False, "reason": None}
    assert len(result["items"]) == 60 and {row["id"] for row in result["items"]} == set(identifiers)
    # Diversity and bounded work stay: each record carries at most two chunks, and a smaller limit is exact.
    hits = service.archive_api.search(["absentlexicaltoken"], SearchFilters(), 60).hits
    assert len(hits) == 60 and all(1 <= len(hit.chunks) <= 2 for hit in hits)
    assert len(service.search("absentlexicaltoken", limit=7)) == 7


def test_a_record_hidden_after_the_chunk_snapshot_is_not_returned(service, monkeypatch):
    from kelmo.store.memory import search
    from kelmo.contracts import SearchFilters
    hidden = archive(service, "pipeline cache pipeline cache rebuilt", "2026-10-01_hidden-after-rank.md")
    kept = archive(service, "pipeline cache warmed before the release window", "2026-10-01_kept.md")
    ranked = search.search_chunks

    def hide_after_the_snapshot(*args, **kwargs):
        result = ranked(*args, **kwargs)
        # The snapshot ranked the record first while it was visible; the hide lands before the re-read.
        assert result["items"][0]["id"] == hidden
        service.forget(hidden, reason="Synthetic hide after ranking")
        return result

    monkeypatch.setattr(search, "search_chunks", hide_after_the_snapshot)
    result = service.search("pipeline cache", limit=1, with_metadata=True)
    # The hidden record is dropped at the re-read and the bounded pool supplies the next visible one.
    assert [row["id"] for row in result["items"]] == [kept]
    monkeypatch.setattr(search, "search_chunks", ranked)
    assert [hit.evidence.archive_id
            for hit in service.archive_api.search(["pipeline cache"], SearchFilters(), 5).hits] == [kept]


# --- outage, restart and coverage ---

def test_an_outage_keeps_text_chunks_and_fts_then_backfill_after_restart(root, backend):
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    backend.down = True
    with Service(root) as down:
        archive_id = archive(down, "fallback token survives the outage", "2026-10-01_outage.md")
        assert len(chunk_rows(down, archive_id)) == 1 and vector_count(down) == 0
        assert down.archive_api.get(archive_id).vectors_pending is True
        hit = down.search("fallback token", with_metadata=True)
        assert [row["id"] for row in hit["items"]] == [archive_id]
        assert hit["search"]["degraded"] is True and hit["search"]["mode"] == "lexical"
        with pytest.raises(KelmoError) as refused:
            down.archive_api.search_records("fallback token", _filters(), 5, "semantic")
        assert refused.value.code == "E_EMBEDDING"
    backend.down = False
    with Service(root) as restarted:
        summary = restarted.archive_api.backfill_vectors(sleep=lambda seconds: None)
        assert summary["written"] == 1 and summary["stopped"] is None
        assert restarted.archive_api.get(archive_id).vectors_pending is False
        semantic = restarted.archive_api.search_records("fallback token", _filters(), 5, "semantic")
        assert semantic["search"] == {"mode": "semantic", "degraded": False, "reason": None}
        assert [row["id"] for row in semantic["items"]] == [archive_id] and "distance" in semantic["items"][0]


def _filters(**values):
    from kelmo.contracts import SearchFilters
    return SearchFilters(**values)


def test_partial_coverage_and_zero_hits_are_labelled_never_complete(root, backend):
    from kelmo.service import Service
    with Service(root) as service:
        covered = archive(service, "harbour crane alpha", "2026-10-01_alpha.md")
    backend.down = True
    with Service(root) as service:
        archive(service, "harbour crane beta", "2026-10-01_beta.md")
    backend.down = False
    with Service(root) as service:
        semantic = service.archive_api.search_records("harbour crane", _filters(), 5, "semantic")
        assert [row["id"] for row in semantic["items"]] == [covered]
        assert semantic["search"] == {"mode": "semantic", "degraded": True, "reason": "partial_vector_coverage"}
        hybrid = service.search("harbour crane", with_metadata=True)
        assert len(hybrid["items"]) == 2 and hybrid["search"]["reason"] == "partial_vector_coverage"
        none = service.archive_api.search_records("harbour crane", _filters(project="absent"), 5, "semantic")
        assert none["items"] == [] and none["search"]["mode"] == "semantic"


def test_require_vectors_refuses_during_an_outage_and_writes_nothing(root, backend):
    from kelmo.contracts import SourcePacket
    from kelmo.service import Service
    backend.down = True
    with Service(root) as service:
        content = b"replacement that needs vectors"
        packet = SourcePacket(content, "raw_note", hashlib.sha256(content).hexdigest(), "2026-10-01_strict.md",
                              "2026-10-15T10:00:00+00:00", require_vectors=True)
        operation = service.capture_source(packet, service.origin, "strict")
        failed = service.run(operation["id"])
        assert failed["state"] == "failed" and failed["error_code"] == "E_EMBEDDING"
        assert service.archive.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


# --- updates ---

def test_a_text_update_replaces_the_generation_and_keeps_history_and_unknown_vectors(service, backend):
    from kelmo.errors import KelmoError
    archive_id = archive(service, "first indexed sentence about the harbour", "2026-10-01_update.md")
    old_chunk = chunk_rows(service, archive_id)[0]["id"]
    service.archive.execute("CREATE VIRTUAL TABLE chunks_vec_m3 USING vec0(embedding float[4])")
    legacy = np.asarray([1, 2, 3, 4], dtype="<f4").tobytes()
    service.archive.execute("INSERT INTO chunks_vec_m3(rowid, embedding) VALUES (?, ?)", (old_chunk, legacy))
    service.archive.commit()
    from kelmo.contracts import SearchFilters
    locator = service.archive_api.search(["harbour"], SearchFilters(), 1).hits[0].chunks[0]
    current = service.archive_api.get(archive_id)
    ref = service.archive_api.update(archive_id, current.evidence.version,
                                     replace(current.source, content=b"second text about the lighthouse"),
                                     service.origin)
    assert ref.version == 2 and [row["text"] for row in chunk_rows(service, archive_id)] == \
        ["second text about the lighthouse"]
    assert service.archive_api.get(archive_id, 1).source.content == b"first indexed sentence about the harbour"
    lexical = service.archive_api.search_records
    assert [row["id"] for row in lexical("lighthouse", _filters(), 5, "lexical")["items"]] == [archive_id]
    assert lexical("harbour", _filters(), 5, "lexical")["items"] == []
    kept = service.archive.execute("SELECT embedding FROM unverified_vectors").fetchall()
    assert legacy in [row[0] for row in kept]
    assert service.archive_api.get(archive_id).vectors_pending is False
    with pytest.raises(KelmoError) as stale:
        service.archive_api.expand(locator, 0)
    assert stale.value.code == "E_CONFLICT"


def test_metadata_updates_keep_vectors_and_bind_evidence_to_the_present_version(service, backend):
    from kelmo.contracts import SearchFilters
    from kelmo.errors import KelmoError
    archive_id = archive(service, "metadata revision keeps this text", "2026-10-01_meta.md")
    before = service.archive_api.search(["metadata revision"], SearchFilters(), 1).hits[0]
    vectors, passages = vector_count(service), backend.kinds().count("passage")
    current = service.archive_api.get(archive_id)
    moved = replace(current.source, project="moved")
    ref = service.archive_api.update(archive_id, 1, moved, service.origin)
    assert ref.version == 2 and vector_count(service) == vectors and backend.kinds().count("passage") == passages
    hit = service.archive_api.search(["metadata revision"], SearchFilters(project="moved"), 1).hits[0]
    assert hit.evidence == ref and hit.chunks[0].version == 2
    assert service.archive_api.expand(hit.chunks[0], 0).chunks == ("metadata revision keeps this text",)
    with pytest.raises(KelmoError) as stale:
        service.archive_api.expand(before.chunks[0], 0)  # same text, replaced version
    assert stale.value.code == "E_CONFLICT"


def test_a_no_op_update_and_a_replayed_capture_write_nothing_twice(service, backend):
    archive_id = archive(service, "idempotent words", "2026-10-01_same.md")
    counts = lambda: (len(chunk_rows(service, archive_id)), vector_count(service),  # noqa: E731
                      service.archive.execute("SELECT COUNT(*) FROM archive_receipts").fetchone()[0],
                      backend.kinds().count("passage"))
    before = counts()
    current = service.archive_api.get(archive_id)
    assert service.archive_api.update(archive_id, 1, current.source, service.origin) == current.evidence
    again = service.capture("idempotent words", "2026-10-01_same.md", "implementer")
    assert service.run(again["id"])["archive_id"] == archive_id
    assert counts() == before


# --- identity, profile and restart ---

def test_a_restart_with_the_same_settings_reuses_vectors_without_re_embedding(root, backend):
    from kelmo.service import Service
    with Service(root) as service:
        archive_id = archive(service, "compatible reuse after restart", "2026-10-01_reuse.md")
    backend.calls.clear()
    with Service(root) as service:
        assert service.archive_api.rebuild_chunks()["memories"] == 0
        found = service.archive_api.search_records("compatible reuse", _filters(), 5, "semantic")
        assert [row["id"] for row in found["items"]] == [archive_id]
    assert backend.kinds() == ["query"]


def test_another_input_path_is_another_profile_until_an_explicit_rebuild(root, pins, backend):
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    with Service(root) as service:
        archive_id = archive(service, "profile change words", "2026-10-01_profile.md")
        old_profile = service.archive_api.chunk_index.profile_id()
    set_embedding(root, local_section(pins, document_prefix="document: "))
    with Service(root) as service:
        index = service.archive_api.chunk_index
        assert index.profile_id() != old_profile
        lexical = service.search("profile change", with_metadata=True)
        assert [row["id"] for row in lexical["items"]] == [archive_id]
        assert lexical["search"] == {"mode": "lexical", "degraded": True, "reason": "chunks_not_built"}
        with pytest.raises(KelmoError) as refused:
            service.archive_api.search_records("profile change", _filters(), 5, "semantic")
        assert refused.value.code == "E_EMBEDDING"
        rebuilt = service.archive_api.rebuild_chunks()
        assert rebuilt["memories"] == 1 and rebuilt["vectors"] == 1
        assert {row["profile_id"] for row in chunk_rows(service, archive_id)} == {index.profile_id()}
        semantic = service.archive_api.search_records("profile change", _filters(), 5, "semantic")
        assert [row["id"] for row in semantic["items"]] == [archive_id] and not semantic["search"]["degraded"]


def test_a_locator_of_another_generation_is_refused(service):
    from kelmo.contracts import ChunkLocator
    from kelmo.errors import KelmoError
    archive_id = archive(service, "generation words", "2026-10-01_generation.md")
    ref = service.archive_api.get(archive_id).evidence
    for generation in ("core-record-index/1", "chunk:sha256:" + "0" * 64):
        with pytest.raises(KelmoError) as refused:
            service.archive_api.expand(ChunkLocator(archive_id, ref.version, ref.content_sha256, generation, 0), 0)
        assert refused.value.code == "E_CAPABILITY"


def test_a_cold_broker_keeps_text_and_chunks_from_the_declared_window(tmp_path, monkeypatch):
    from kelmo.store.memory.embed import InputPolicy, bound_transform, identity_id, preparation_digest
    from kelmo.contracts import CONTRACT_VERSION, EmbeddingSpace
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    git_identity(monkeypatch)
    monkeypatch.delenv("SYNTHETIC_COLD_BROKER_FDS", raising=False)
    root = tmp_path / "vault"
    Service.initialize(root, "fixture-node")
    tokenizer = root / ".cache/broker-tokenizer.json"
    tokenizer.parent.mkdir(parents=True, exist_ok=True)
    tokenizer.write_bytes(tokenizer_bytes())
    identity = "sha256:" + hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    policy = InputPolicy(identity, LIMIT, "query: ", "passage: ", preparation_digest([]))
    space = EmbeddingSpace(CONTRACT_VERSION, "broker-model@sha256:" + "0" * 64, identity,
                           bound_transform("broker:query", policy.query_prefix, policy),
                           bound_transform("broker:passage", policy.document_prefix, policy),
                           "mean", "none", "l2", DIM, "float32", "little")
    lines = ['[embedding]', 'provider = "broker"', 'fds_env = "SYNTHETIC_COLD_BROKER_FDS"',
             'tokenizer = ".cache/broker-tokenizer.json"', "[embedding.input]", f"max_tokens = {LIMIT}",
             'query_prefix = "query: "', 'document_prefix = "passage: "', "special_tokens = []",
             "[embedding.space]"] + [f"{key} = {json.dumps(value)}" for key, value in space.to_dict().items()]
    set_embedding(root, "\n".join(lines) + "\n")
    with Service(root) as service:
        index = service.archive_api.chunk_index
        assert index.provider is None and index.unavailable == "embedding_unavailable"
        archive_id = archive(service, "cold broker keeps durable text", "2026-10-01_cold.md")
        assert len(chunk_rows(service, archive_id)) == 1 and vector_count(service) == 0
        assert service.archive_api.get(archive_id).vectors_pending is True
        assert service.archive.execute("SELECT COUNT(*) FROM embedding_spaces WHERE space_id=?",
                                       (identity_id(space),)).fetchone()[0] == 0, "no space is claimed verified"
        found = service.search("durable text", with_metadata=True)
        assert [row["id"] for row in found["items"]] == [archive_id]
        assert found["search"] == {"mode": "lexical", "degraded": True, "reason": "embedding_unavailable"}
        with pytest.raises(KelmoError):
            service.archive_api.search_records("durable text", _filters(), 5, "semantic")
        assert "unavailable" in service.archive_api.backfill_vectors()["stopped"]


def test_an_unconfigured_vault_keeps_the_record_api(tmp_path, monkeypatch):
    from kelmo.store.errors import ArchiveEmbeddingError
    from kelmo.service import Service
    git_identity(monkeypatch)

    def absent(*args, **kwargs):
        raise ArchiveEmbeddingError("synthetic unavailable model")

    monkeypatch.setattr("kelmo.store.memory.store.embed", absent)
    monkeypatch.setattr("kelmo.store.memory.search.embed", absent)
    root = tmp_path / "vault"
    Service.initialize(root, "fixture-node")
    with Service(root) as service:
        assert service.archive_api.chunk_index is None
        archive_id = archive(service, "record lane words", "2026-10-01_record.md")
        assert service.archive.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        found = service.search("record lane", with_metadata=True)
        assert found["search"] == {"mode": "lexical", "degraded": True, "reason": "embedding_unavailable"}
        assert [row["id"] for row in found["items"]] == [archive_id]


# --- the real pinned model, run separately by the controller ---

@pytest.mark.skipif(not os.environ.get("KELMO_NATIVE_MODEL_DIR"),
                    reason="native probe: set KELMO_NATIVE_MODEL_DIR to a provisioned pinned MiniLM directory")
def test_pinned_model_probe(tmp_path, monkeypatch):
    """Real pinned all-MiniLM-L6-v2 through Service: capture, chunk, embed, search, expand. Mechanics only:
    an English model, so this says nothing about Russian retrieval quality."""
    from kelmo.contracts import SearchFilters
    from kelmo.model_store import MODEL as PINNED
    from kelmo.service import Service
    git_identity(monkeypatch)
    root = tmp_path / "vault"
    Service.initialize(root, "fixture-node")
    model_dir = os.environ["KELMO_NATIVE_MODEL_DIR"]
    set_embedding(root, f'[embedding]\nprovider = "local"\nmodel = "{PINNED}"\nmodel_dir = {json.dumps(model_dir)}\n')
    with Service(root) as service:
        from kelmo.mcp import warm_model
        warmed = warm_model(service)
        assert warmed['loaded'] is True and warmed['provider'] == 'local'
        build = archive(service, "The nightly build failed because the integration tests timed out.",
                        "2026-10-01_build.md")
        archive(service, "The documentation site switched to a darker colour palette.", "2026-10-01_docs.md")
        assert service.archive_api.get(build).vectors_pending is False
        result = service.archive_api.search(["continuous integration job broke on slow tests"], SearchFilters(),
                                            1, "semantic")
        assert result.hits[0].evidence.archive_id == build and result.degraded is False
        window = service.archive_api.expand(result.hits[0].chunks[0], 1)
        assert window.chunks and all(service.archive_api.chunk_index.window().fits(text) for text in window.chunks)


@pytest.mark.parametrize("require_vectors", [False, True])
def test_unicode_whitespace_has_its_own_index_reason(service, require_vectors):
    from kelmo.store.errors import ArchiveEmbeddingError

    index = service.archive_api.chunk_index
    if require_vectors:
        with pytest.raises(ArchiveEmbeddingError) as error:
            index.prepare("\u00a0\u3000", require_vectors=True)
        assert error.value.data["reason"] == "empty_input"
    else:
        prepared = index.prepare("\u00a0\u3000", require_vectors=False)
        assert prepared.text == "\u00a0\u3000" and not prepared.chunks
        assert prepared.reason == "empty_input"


@pytest.mark.parametrize("transport", ["local", "worker"])
def test_compat_chunk_read_binds_the_configured_index_generation(root, backend, transport):
    """The old memory_get(chunk_idx) in a configured vault: the locator names the generation the archive
    serves now, read in process or through the archive worker's frames, never the record index."""
    from kelmo.config import Config
    from kelmo.mcp import compat
    from kelmo.server import open_archive
    from kelmo.service import Service
    with Service(root) as local:
        archive_id = archive(local, RU_LONG, "2026-10-01_compat-get.md")
        generation = local.archive_api.index_generation()
        texts = [row["text"] for row in chunk_rows(local, archive_id)]
    assert generation.startswith("chunk:") and len(texts) > 2
    if transport == "local":
        opened = Service(root)
    else:
        config = Config.load(root)
        bindings = config.local_bindings()
        opened = Service(root, archive_api=open_archive(config, bindings, "in-process"), bindings=bindings)
    with opened as service:
        assert service.archive_api.index_generation() == generation
        read = compat(service, "memory_get", {"id": archive_id, "chunk_idx": 1, "neighbors": 1})
    assert read["id"] == archive_id and "content" not in read
    assert [chunk["chunk_idx"] for chunk in read["chunks"]] == [0, 1, 2]
    assert [chunk["text"] for chunk in read["chunks"]] == texts[:3]


def test_compat_search_limit_counts_chunk_rows_and_semantic_follows_the_query_vectors(service, backend):
    from kelmo.mcp import compat
    archive(service, RU_LONG, "2026-10-01_compat-search.md")
    query = {"query_en": "greenhouse bed watering", "query_ru": "полив грядки в теплице"}
    one = compat(service, "memory_search", dict(query, limit=1))
    assert len(one) == 1 and one[0]["semantic"] is True
    assert len(compat(service, "memory_search", dict(query, limit=5))) <= 2, "at most two chunks of one record"
    backend.down = True
    degraded = compat(service, "memory_search", dict(query, limit=5))
    assert degraded and all(row["semantic"] is False for row in degraded)


def test_current_locator_with_invalid_chunk_index_is_an_input_error(service):
    from kelmo.contracts import SearchFilters
    from kelmo.errors import KelmoError

    archive(service, "The pipeline cache passed the migration check.", "invalid-chunk.md")
    locator = service.archive_api.search(["pipeline cache"], SearchFilters(), 1).hits[0].chunks[0]
    with pytest.raises(KelmoError) as error:
        service.archive_api.expand(replace(locator, chunk_index=999), 1)
    assert error.value.code == "E_INPUT"
