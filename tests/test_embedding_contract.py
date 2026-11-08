"""Embedding contract: space identity, vector admission, token windows and the two providers.

Fakes and a synthetic tokenizer establish the algorithm and the contracts only. Nothing here loads a
real model, so nothing here is real-model acceptance.
"""
import base64
import gc
import hashlib
import inspect
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
from kelmo.store.memory.embed import (NO_ADDITIONS, InputPolicy, TokenWindow, bound_transform, identity_id,
                                  preparation_digest, transforms_bind, vector_blobs)
from kelmo.contracts import CONTRACT_VERSION, ChunkProfile, EmbeddingSpace, VectorBatch

DIM = 8


def make_space(**changes):
    fields = dict(
        contract_version=CONTRACT_VERSION, artifact_identity="synthetic@sha256:" + "0" * 64,
        tokenizer_identity="sha256:" + "1" * 64, query_transform="synthetic:query",
        document_transform="synthetic:document", pooling="mean", projection="none",
        normalization="l2", dimension=DIM, dtype="float32", byte_order="little",
    )
    fields.update(changes)
    return EmbeddingSpace(**fields)


def unit(seed, dim=DIM):
    vector = np.random.RandomState(seed).randn(dim)
    return vector / np.linalg.norm(vector)


# --- identity ---

@pytest.mark.parametrize("field, value", [
    ("artifact_identity", "synthetic@sha256:" + "2" * 64), ("tokenizer_identity", "sha256:" + "3" * 64),
    ("query_transform", "synthetic:query;prefix='query: '"), ("document_transform", "other"),
    ("pooling", "cls"), ("projection", "pca-8"), ("normalization", "none"), ("byte_order", "big"),
])
def test_every_component_of_a_space_changes_its_identity(field, value):
    assert identity_id(make_space(**{field: value})) != identity_id(make_space())


def test_a_matching_model_name_and_dimension_do_not_make_one_space():
    first = make_space(artifact_identity="model-a@sha256:" + "4" * 64)
    second = make_space(artifact_identity="model-a@sha256:" + "4" * 64, tokenizer_identity="sha256:" + "5" * 64)
    assert (first.dimension, first.artifact_identity) == (second.dimension, second.artifact_identity)
    assert identity_id(first) != identity_id(second)
    assert identity_id(make_space()) == identity_id(EmbeddingSpace.from_dict(make_space().to_dict()))


def test_chunk_profile_identity_is_separate_from_the_space():
    profile = ChunkProfile("archive-chunk/2", "blocks", 512, 0, "exact", "sha256:" + "1" * 64)
    assert identity_id(profile) != identity_id(make_space())
    assert identity_id(replace(profile, token_budget=256)) != identity_id(profile)


# --- vector admission ---

def batch(purpose="document", vectors=None, space=None):
    space = space or make_space()
    vectors = vectors if vectors is not None else (tuple(unit(1)), tuple(unit(2)))
    return SimpleNamespace(space=space, purpose=purpose, vectors=vectors)


def test_admitted_vectors_are_float32_little_endian_of_the_space():
    blobs = vector_blobs(batch(), make_space(), "document", 2)
    assert [len(blob) for blob in blobs] == [DIM * 4, DIM * 4]
    assert np.allclose(np.frombuffer(blobs[0], dtype="<f4"), unit(1))


@pytest.mark.parametrize("candidate, purpose, count, reason", [
    (batch(space=make_space(tokenizer_identity="sha256:" + "9" * 64)), "document", 2, "space_mismatch"),
    (batch(purpose="query"), "document", 2, "purpose_mismatch"),
    (batch(), "document", 3, "malformed_vectors"),
    (batch(vectors=(tuple(unit(1))[:-1], tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=((float("nan"),) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=((1e39,) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=(("x",) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    # Values that would coerce into plausible numbers: refused, never parsed.
    (batch(vectors=(("0.5",) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=((True,) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=((None,) * DIM, tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=(np.array(["0.5"] * DIM), tuple(unit(2)))), "document", 2, "malformed_vectors"),
    (batch(vectors=(np.ones(DIM, dtype=bool), tuple(unit(2)))), "document", 2, "malformed_vectors"),
])
def test_malformed_nonfinite_or_foreign_vectors_are_refused(candidate, purpose, count, reason):
    with pytest.raises(ArchiveEmbeddingError) as refused:
        vector_blobs(candidate, make_space(), purpose, count)
    assert refused.value.data["reason"] == reason


def test_numeric_vectors_of_any_number_type_are_admitted():
    integers = batch(vectors=(np.ones(DIM, dtype=np.int32), (1,) * DIM))
    assert [len(blob) for blob in vector_blobs(integers, make_space(), "document", 2)] == [DIM * 4, DIM * 4]


def test_a_float64_space_is_refused_by_the_float32_store():
    space = make_space(dtype="float64")
    with pytest.raises(ArchiveEmbeddingError) as refused:
        vector_blobs(batch(space=space), space, "document", 2)
    assert refused.value.data["reason"] == "unsupported_representation"


def test_the_contract_batch_itself_rejects_nonfinite_values():
    with pytest.raises(ValueError):
        VectorBatch(make_space(), "query", ((float("inf"),) * DIM,))


# --- token windows ---

def synthetic_tokenizer(path, *, truncate=None, pad=None):
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "[PAD]": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)])
    if truncate:
        tokenizer.enable_truncation(max_length=truncate)
    if pad:
        tokenizer.enable_padding(pad_id=3, pad_token="[PAD]", length=pad)
    tokenizer.save(str(path))
    return path


def test_token_window_counts_prefix_and_special_tokens_without_truncation_or_padding(tmp_path):
    pytest.importorskip("tokenizers")
    path = synthetic_tokenizer(tmp_path / "tokenizer.json", truncate=4, pad=32)
    window = TokenWindow.from_file(path, budget=8, prefix="passage: ")
    # "passage", ":", then the words, then [CLS] and [SEP]: never cut at 4, never padded to 32.
    assert window.count("one two three") == 7
    assert window.count("w " * 40) == 44
    assert window.fits("one two three four five") is False
    assert window.fits("one two three four") is True
    assert window.identity == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_token_window_refuses_a_missing_tokenizer(tmp_path):
    with pytest.raises(ArchiveEmbeddingError) as refused:
        TokenWindow.from_file(tmp_path / "absent.json", budget=8)
    assert refused.value.data["reason"] == "tokenizer_missing"


def test_the_record_entry_point_keeps_its_signature():
    from kelmo.store.memory import embed as embed_mod

    assert "cache_dir" in inspect.signature(embed_mod.embed).parameters
    assert callable(embed_mod.get_model) and callable(embed_mod.model_load_ms)


# --- brokered provider ---

class FakeBroker:
    """The launcher's side of the pipes: reads requests, answers with `respond`."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.request_r, request_w = os.pipe()
        answer_r, self.answer_w = os.pipe()
        self.fds = (request_w, answer_r)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        with os.fdopen(self.request_r, "rb") as requests:
            for line in requests:
                request = json.loads(line)
                self.requests.append(request)
                for answer in self.respond(request):
                    os.write(self.answer_w, (answer if isinstance(answer, bytes) else
                                             json.dumps(answer).encode()) + b"\n")

    def close(self):
        for fd in (*self.fds, self.answer_w):
            try:
                os.close(fd)
            except OSError:
                pass


def encoded(text):
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % 2**31
    return base64.b64encode(unit(seed).astype("<f4").tobytes()).decode()


def synthetic_tokenizer_bytes():
    """tokenizer.json bytes of a word-level tokenizer: every word one token, plus [CLS] and [SEP]."""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "[PAD]": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)])
    return tokenizer.to_str().encode()


TOKENIZER = synthetic_tokenizer_bytes()
BROKER_LIMIT = 32


TOKENIZER_ID = "sha256:" + hashlib.sha256(TOKENIZER).hexdigest()


def broker_policy(*, query_prefix="", document_prefix="", special_tokens=(), max_tokens=BROKER_LIMIT):
    return InputPolicy(TOKENIZER_ID, max_tokens, query_prefix, document_prefix,
                       preparation_digest(list(special_tokens)))


def broker_space(policy=None, **changes):
    """A declared space over the synthetic tokenizer whose transforms name the broker's input path."""
    policy = policy or broker_policy()
    return make_space(**{"tokenizer_identity": TOKENIZER_ID,
                         "query_transform": bound_transform("synthetic:query", policy.query_prefix, policy),
                         "document_transform": bound_transform("synthetic:document", policy.document_prefix,
                                                               policy), **changes})


def health_answer(request, space, special_tokens=(), input_changes=None, **changes):
    inputs = {"max_tokens": BROKER_LIMIT, "query_prefix": "", "document_prefix": "",
              "special_tokens": list(special_tokens), **(input_changes or {})}
    return {"id": request["id"], "ok": True, "space": space.to_dict(), "input": inputs, **changes}


def vectors_answer(request, space):
    return {"id": request["id"], "space_id": identity_id(space),
            "vectors": [encoded(t) for t in request["texts"]]}


def well_behaved(space):
    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        return [vectors_answer(request, space)]
    return respond


@pytest.fixture
def broker(tmp_path):
    from kelmo.embeddings import BrokeredProvider
    started = []
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_bytes(TOKENIZER)

    def start(respond=None, space=None, **options):
        space = space or broker_space()
        fake = FakeBroker(respond or well_behaved(space))
        started.append(fake)
        options.setdefault("tokenizer", tokenizer)
        return fake, BrokeredProvider(*fake.fds, space, **options)

    yield start
    for fake in started:
        fake.close()


def test_broker_verifies_the_whole_space_then_sends_queries_and_passages_apart(broker):
    fake, provider = broker()
    assert provider.identity_verified is False
    queries = provider.embed_queries(["what changed", "что изменилось"])
    documents = provider.embed_documents(["a note"])
    assert provider.identity_verified is True
    assert (queries.purpose, documents.purpose) == ("query", "document")
    assert queries.space == provider.describe() == broker_space()
    assert provider.input_policy().max_tokens == BROKER_LIMIT
    assert [(r["kind"], r["texts"]) for r in fake.requests] == [
        ("health", []), ("query", ["what changed", "что изменилось"]), ("passage", ["a note"])]


@pytest.mark.parametrize("reply", ["healthy", "different-space", "unhealthy"])
def test_mcp_warmup_proves_the_real_broker_protocol_before_reporting_ready(broker, tmp_path, monkeypatch, reply):
    from types import SimpleNamespace
    from kelmo.mcp import warm_model

    space = broker_space(pooling="cls") if reply == "different-space" else broker_space()

    def respond(request):
        return [health_answer(request, space, ok=reply != "unhealthy")]

    def wrong_model(*args):
        raise AssertionError("A broker-backed vault must not load the record model")

    monkeypatch.setattr("kelmo.store.memory.embed.get_model", wrong_model)
    fake, provider = broker(respond)
    service = SimpleNamespace(config=SimpleNamespace(cache=tmp_path, embedding={"provider": "broker"}),
                              archive_api=SimpleNamespace(chunk_index=SimpleNamespace(provider=provider)))
    warm = warm_model(service)
    assert warm["loaded"] is (reply == "healthy")
    assert provider.identity_verified is (reply == "healthy")
    assert fake.requests and {row["kind"] for row in fake.requests} == {"health"}


def test_a_broker_that_cannot_report_its_space_is_refused_before_any_vector(broker):
    def respond(request):
        if request["kind"] == "health":
            return [{"id": request["id"], "ok": True, "model": "named-model", "dim": DIM}]
        return [{"id": request["id"], "vectors": [encoded(t) for t in request["texts"]]}]

    fake, provider = broker(respond)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "embedding_identity_unverified"
    assert provider.identity_verified is False
    assert [r["kind"] for r in fake.requests] == ["health"]


def test_a_broker_serving_another_space_is_refused(broker):
    other = broker_space(pooling="cls")
    _, provider = broker(well_behaved(other))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_queries(["anything"])
    assert refused.value.data["reason"] == "space_mismatch"


def test_a_broker_reporting_ok_false_is_not_verified(broker):
    space = broker_space()
    fake, provider = broker(lambda r: [health_answer(r, space, ok=False)] if r["kind"] == "health"
                            else [vectors_answer(r, space)])
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "embedding_unavailable" and provider.identity_verified is False
    assert [r["kind"] for r in fake.requests] == ["health"]


def test_a_broker_whose_model_changes_after_verification_is_refused_per_answer(broker):
    space, other = broker_space(), broker_space(pooling="cls")
    swapped = []

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        return [vectors_answer(request, other if swapped else space)]

    _, provider = broker(respond)
    assert len(provider.embed_documents(["before"]).vectors) == 1
    swapped.append(True)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["after"])
    assert refused.value.data["reason"] == "space_mismatch" and provider.identity_verified is False


def test_a_configured_tokenizer_that_is_not_the_space_tokenizer_is_refused(broker, tmp_path):
    other = tmp_path / "other-tokenizer.json"
    other.write_bytes(TOKENIZER.replace(b"[PAD]", b"[PAX]"))
    _, provider = broker(tokenizer=other)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_an_input_over_the_broker_limit_is_refused_not_truncated(broker):
    fake, provider = broker()
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["word " * BROKER_LIMIT])
    assert refused.value.data["reason"] == "input_too_long"
    assert [r["kind"] for r in fake.requests] == ["health"]


@pytest.mark.parametrize("drift", [dict(query_prefix="query: "), dict(document_prefix="passage: "),
                                   dict(special_tokens=["[SEP]"]), dict(max_tokens=16)])
def test_a_reported_input_path_the_declared_space_does_not_name_is_refused(broker, drift):
    space = broker_space()
    fake, provider = broker(lambda r: [health_answer(r, space, input_changes=drift)] if r["kind"] == "health"
                            else [vectors_answer(r, space)])
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "space_mismatch" and provider.identity_verified is False
    assert [r["kind"] for r in fake.requests] == ["health"]


def test_a_broker_that_changes_its_input_path_after_verification_is_refused(broker):
    space, drifted = broker_space(), broker_space(broker_policy(document_prefix="passage: "))
    changed = []

    def respond(request):
        if request["kind"] == "health":  # still claims the declared space, now with another input path
            return [health_answer(request, space, input_changes={"document_prefix": "passage: "} if changed
                                  else None)]
        return [vectors_answer(request, drifted if changed else space)]

    _, provider = broker(respond)
    provider.embed_documents(["before"])
    changed.append(True)
    for _ in range(2):  # the answer names another space, then the re-verification names another input path
        with pytest.raises(ArchiveEmbeddingError) as refused:
            provider.embed_documents(["after"])
        assert refused.value.data["reason"] == "space_mismatch" and provider.identity_verified is False


def test_a_failed_explicit_verify_leaves_no_earlier_proof_behind(broker):
    space = broker_space()
    changed = []

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space, input_changes={"document_prefix": "passage: "} if changed
                                  else None)]
        return [vectors_answer(request, space)]

    fake, provider = broker(respond)
    assert provider.verify() == space and provider.identity_verified is True
    assert provider.input_policy().document_prefix == ""
    changed.append(True)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.verify()
    assert refused.value.data["reason"] == "space_mismatch"
    assert provider.identity_verified is False
    assert (provider._policy, provider._windows, provider._additions) == (None, None, None)
    sent = len(fake.requests)
    # Nothing continues under the stale proof: every use proves the identity again and is refused.
    for use in (provider.input_policy, provider.token_window, lambda: provider.embed_documents(["a note"])):
        with pytest.raises(ArchiveEmbeddingError):
            use()
    assert [r["kind"] for r in fake.requests[sent:]] == ["health"] * 3


def test_a_broker_window_over_a_replaced_tokenizer_file_is_refused(broker, tmp_path):
    _, provider = broker()
    assert provider.token_window().identity == TOKENIZER_ID
    (tmp_path / "tokenizer.json").write_bytes(TOKENIZER.replace(b"[PAD]", b"[PAX]"))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.token_window()
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_broker_windows_count_the_special_tokens_its_loader_adds(broker):
    space = broker_space(broker_policy(special_tokens=["[SEP]"]))
    _, provider = broker(lambda r: [health_answer(r, space, special_tokens=["[SEP]"])] if r["kind"] == "health"
                         else [vectors_answer(r, space)], space=space)
    window = provider.token_window()
    assert window.preparation == provider.input_policy().preparation == preparation_digest(["[SEP]"])
    assert window.preparation != NO_ADDITIONS
    # [CLS] a [SEP] b [SEP]; the raw tokenizer.json alone splits the literal [SEP] into "[", "SEP", "]".
    assert (window.count("a [SEP] b"), TokenWindow.from_bytes(TOKENIZER, budget=8).count("a [SEP] b")) == (5, 7)


@pytest.mark.parametrize("special_tokens", [None, "[SEP]", [""], ["[SEP]", 3], [{"lstrip": True}],
                                            [{"content": "[SEP]", "strip": True}]])
def test_a_broker_input_policy_without_valid_additions_is_refused(broker, special_tokens):
    space = broker_space()

    def respond(request):
        if request["kind"] != "health":
            return [vectors_answer(request, space)]
        answer = health_answer(request, space)
        if special_tokens is None:
            del answer["input"]["special_tokens"]
        else:
            answer["input"]["special_tokens"] = special_tokens
        return [answer]

    fake, provider = broker(respond)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "input_policy_unproven" and provider.identity_verified is False
    assert [r["kind"] for r in fake.requests] == ["health"]


@pytest.mark.parametrize("line", [
    b'"oops"', b'{"vectors": []}', b'{"id": 7, "vectors": []}', b"not json",
    pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep-nesting"),
])
def test_an_answer_that_names_no_request_is_a_protocol_error_not_a_long_wait(broker, line):
    space = broker_space()
    _, provider = broker(lambda r: [health_answer(r, space)] if r["kind"] == "health" else [line],
                         document_timeout=30.0)
    started = time.monotonic()
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == "protocol_error" and time.monotonic() - started < 5
    assert provider._channel.closed is True and provider.identity_verified is False


def test_an_error_reply_requires_the_identity_to_be_proven_again(broker):
    space, failing = broker_space(), []

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        if failing:
            return [{"id": request["id"], "error": "overloaded"}]
        return [vectors_answer(request, space)]

    fake, provider = broker(respond)
    provider.embed_documents(["first"])
    failing.append(True)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["second"])
    assert refused.value.data["reason"] == "embedding_unavailable" and provider.identity_verified is False
    failing.clear()
    provider.embed_documents(["third"])
    assert [r["kind"] for r in fake.requests] == ["health", "passage", "passage", "health", "passage"]


def decoded(text):
    return np.frombuffer(base64.b64decode(encoded(text)), dtype="<f4")


def test_providers_on_the_same_pipes_share_one_channel_and_never_take_each_others_answers(broker, tmp_path):
    from kelmo.embeddings import BrokeredProvider
    space, held = broker_space(), []

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        if not held and request["texts"] == ["a text"]:
            held.append(request)  # the first provider times out waiting for this one
            return []
        late = [vectors_answer(request_, space) for request_ in held]  # same space, same vector count
        held.clear()
        return late + [vectors_answer(request, space)]

    fake, first = broker(respond, document_timeout=0.2)
    second = BrokeredProvider(*fake.fds, space, tokenizer=tmp_path / "tokenizer.json")
    assert second._channel is first._channel
    with pytest.raises(ArchiveEmbeddingError) as refused:
        first.embed_documents(["a text"])
    assert refused.value.data["reason"] == "timeout"
    assert np.allclose(second.embed_documents(["b text"]).vectors[0], decoded("b text"))
    request_ids = [r["id"] for r in fake.requests]
    assert len(set(request_ids)) == len(request_ids)
    assert all(isinstance(i, str) and len(i) == 32 for i in request_ids), "random ids, not counters"


def test_concurrent_requests_from_two_providers_keep_their_own_vectors(broker, tmp_path):
    from kelmo.embeddings import BrokeredProvider
    fake, first = broker()
    second = BrokeredProvider(*fake.fds, broker_space(), tokenizer=tmp_path / "tokenizer.json")
    mismatched, errors = [], []

    def run(provider, name):
        try:
            for i in range(20):
                text = f"{name} {i}"
                if not np.allclose(provider.embed_documents([text]).vectors[0], decoded(text)):
                    mismatched.append(text)
        except Exception as error:  # noqa: BLE001 - reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run, args=pair) for pair in ((first, "first"), (second, "second"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert (errors, mismatched) == ([], [])
    assert sum(r["kind"] == "passage" for r in fake.requests) == 40


def test_a_pipe_opened_after_another_was_closed_never_reuses_its_channel(tmp_path):
    from kelmo.embeddings import BrokeredProvider
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_bytes(TOKENIZER)
    space = broker_space()
    first = FakeBroker(well_behaved(space))
    old = BrokeredProvider(*first.fds, space, tokenizer=tokenizer)
    assert len(old.embed_documents(["x"]).vectors) == 1
    first.close()  # the numbers are free for the next pipe; the old provider is still alive
    second = FakeBroker(well_behaved(space))
    try:
        new = BrokeredProvider(*second.fds, space, tokenizer=tokenizer)
        assert new._channel is not old._channel
        assert len(new.embed_documents(["y"]).vectors) == 1
        assert [r["kind"] for r in second.requests] == ["health", "passage"]
    finally:
        second.close()


def test_the_channel_never_closes_the_callers_descriptors(broker):
    fake, provider = broker()
    provider.embed_documents(["x"])
    del provider
    gc.collect()
    for fd in fake.fds:
        os.fstat(fd)  # still open: only the channel's own duplicates were closed


def test_the_settings_window_comes_from_the_provider_already_open():
    from kelmo.embeddings import token_window_from_settings
    with pytest.raises(TypeError):
        token_window_from_settings({"provider": "broker", "token_budget": None}, None)


@pytest.mark.parametrize("answer, reason", [
    (lambda r, s: {"id": r["id"], "space_id": s, "vectors": ["not base64!"]}, "malformed_vectors"),
    (lambda r, s: {"id": r["id"], "space_id": s, "vectors": [base64.b64encode(b"\0" * 8).decode()]},
     "malformed_vectors"),
    (lambda r, s: {"id": r["id"], "space_id": s,
                   "vectors": [base64.b64encode(np.full(DIM, np.nan, "<f4").tobytes()).decode()]},
     "malformed_vectors"),
    (lambda r, s: {"id": r["id"], "space_id": s, "vectors": []}, "malformed_vectors"),
    (lambda r, s: {"id": r["id"], "space_id": "sha256:" + "f" * 64, "vectors": [encoded("x")]}, "space_mismatch"),
    (lambda r, s: {"id": r["id"], "vectors": [encoded("x")]}, "space_mismatch"),
    (lambda r, s: {"id": r["id"], "error": "unavailable"}, "embedding_unavailable"),
])
def test_malformed_broker_answers_are_refused(broker, answer, reason):
    space = broker_space()

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        return [answer(request, identity_id(space))]

    _, provider = broker(respond)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["a note"])
    assert refused.value.data["reason"] == reason


def test_a_late_answer_to_a_timed_out_request_is_not_taken_for_the_next(broker):
    space, held = broker_space(), []

    def respond(request):
        if request["kind"] == "health":
            return [health_answer(request, space)]
        if not held:
            held.append(request)  # no answer yet: the client times out
            return []
        late = held.pop()
        return [{**vectors_answer(late, space), "vectors": [encoded("late " + t) for t in late["texts"]]},
                vectors_answer(request, space)]

    _, provider = broker(respond, query_timeout=0.2)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_queries(["first"])
    assert refused.value.data["reason"] == "timeout"
    second = provider.embed_queries(["second"])
    expected = np.frombuffer(base64.b64decode(encoded("second")), dtype="<f4")
    assert np.allclose(second.vectors[0], expected)


def test_a_closed_channel_raises_and_stays_closed(broker):
    fake, provider = broker(lambda request: [])
    os.close(fake.answer_w)
    with pytest.raises(ArchiveEmbeddingError, match="channel failed"):
        provider.health()
    with pytest.raises(ArchiveEmbeddingError, match="closed"):
        provider.health()


def test_a_long_list_is_split_into_requests_the_broker_accepts(broker):
    fake, provider = broker()
    texts = [f"chunk {i}" for i in range(provider.MAX_TEXTS + 3)]
    assert len(provider.embed_documents(texts).vectors) == len(texts)
    assert [len(r["texts"]) for r in fake.requests if r["kind"] == "passage"] == [provider.MAX_TEXTS, 3]


def test_a_budget_above_the_broker_limit_is_refused(broker):
    _, provider = broker()
    assert provider.token_window().budget == BROKER_LIMIT
    with pytest.raises(ArchiveValidationError):
        provider.token_window(BROKER_LIMIT + 1)


def test_the_broker_channel_is_named_by_configuration_only(monkeypatch, tmp_path):
    from kelmo.embeddings import BrokeredProvider
    monkeypatch.delenv("SYNTHETIC_EMBED_FDS", raising=False)
    with pytest.raises(ArchiveEmbeddingError, match="SYNTHETIC_EMBED_FDS"):
        BrokeredProvider.from_environment(broker_space(), "SYNTHETIC_EMBED_FDS", tokenizer=tmp_path / "t.json")


@pytest.mark.parametrize("value", ["not-descriptors", "1,2,3", "999990,999991"])
def test_a_malformed_channel_variable_is_an_embedding_failure(monkeypatch, tmp_path, value):
    from kelmo.embeddings import BrokeredProvider
    monkeypatch.setenv("SYNTHETIC_EMBED_FDS", value)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        BrokeredProvider.from_environment(broker_space(), "SYNTHETIC_EMBED_FDS", tokenizer=tmp_path / "t.json")
    assert refused.value.data["reason"] == "embedding_unavailable"


# --- the channel over real pipes ---

def test_a_write_cut_short_by_the_deadline_closes_the_channel():
    from kelmo.embeddings import _Channel
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()
    channel = _Channel(request_w, answer_r)
    try:
        started = time.monotonic()
        with pytest.raises(ArchiveEmbeddingError) as refused:
            channel.call("passage", ["x" * 200_000], 0.3)  # nobody reads: the pipe fills mid-request
        assert refused.value.data["reason"] == "timeout" and channel.closed is True
        assert time.monotonic() - started < 2
        with pytest.raises(ArchiveEmbeddingError, match="closed"):
            channel.call("health", [], 0.3)
    finally:
        for fd in (request_r, request_w, answer_r, answer_w):
            os.close(fd)


def test_a_late_answer_larger_than_the_pipe_neither_deadlocks_nor_answers_the_next_request():
    from kelmo.embeddings import _Channel
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()

    def serve():
        with os.fdopen(request_r, "rb") as requests, os.fdopen(answer_w, "wb") as answers:
            first = json.loads(requests.readline())
            time.sleep(0.4)
            # Blocks until the client reads it, while the client is still writing a request larger than the pipe.
            answers.write(json.dumps({"id": first["id"], "vectors": ["late" * 75_000]}).encode() + b"\n")
            answers.flush()
            second = json.loads(requests.readline())
            answers.write(json.dumps({"id": second["id"], "length": len(second["texts"][0])}).encode() + b"\n")
            answers.flush()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    channel = _Channel(request_w, answer_r)
    try:
        with pytest.raises(ArchiveEmbeddingError) as refused:
            channel.call("query", ["first"], 0.2)
        assert refused.value.data["reason"] == "timeout" and channel.closed is False
        assert channel.call("passage", ["x" * 300_000], 5.0)["length"] == 300_000
    finally:
        server.join(5)
        os.close(request_w)
        os.close(answer_r)


@pytest.mark.parametrize("line", [
    b"oops", b'"oops"', b'{"vectors": []}', b'{"id": 7, "vectors": []}',
    pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep-nesting"),
])
def test_a_malformed_line_read_while_a_request_is_written_is_a_protocol_error(line):
    """The write-side read path refuses what the ordinary read path refuses, instead of dropping it."""
    from kelmo.embeddings import _Channel
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()

    def answer():
        with os.fdopen(answer_w, "wb") as answers:
            answers.write(line + b"\n")
            answers.flush()

    def serve():
        with os.fdopen(request_r, "rb") as requests:
            requests.read()  # read only after the client gave up, so the request pipe stays full meanwhile

    server = threading.Thread(target=serve, daemon=True)
    responder = threading.Thread(target=answer, daemon=True)
    responder.start()
    channel = _Channel(request_w, answer_r)
    try:
        started = time.monotonic()
        with pytest.raises(ArchiveEmbeddingError) as refused:
            channel.call("passage", ["x" * 300_000], 5.0)
        assert refused.value.data["reason"] == "protocol_error" and channel.closed is True
        assert time.monotonic() - started < 2
    finally:
        os.close(request_w)
        server.start()
        server.join(5)
        os.close(answer_r)
        responder.join(5)


def test_a_well_formed_unknown_answer_read_while_a_request_is_written_is_dropped():
    from kelmo.embeddings import _Channel
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()
    os.write(answer_w, json.dumps({"id": "someone-elses-request", "vectors": []}).encode() + b"\n")

    def serve():
        with os.fdopen(request_r, "rb") as requests, os.fdopen(answer_w, "wb") as answers:
            time.sleep(0.3)  # the client is blocked writing and reads the unknown answer meanwhile
            request = json.loads(requests.readline())
            answers.write(json.dumps({"id": request["id"], "length": len(request["texts"][0])}).encode() + b"\n")
            answers.flush()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    channel = _Channel(request_w, answer_r)
    try:
        assert channel.call("passage", ["x" * 300_000], 5.0)["length"] == 300_000
        assert channel.closed is False and channel.buffer == b""
    finally:
        server.join(5)
        os.close(request_w)
        os.close(answer_r)


def test_empty_texts_are_refused_before_the_channel(broker):
    fake, provider = broker()
    for texts in ([], ["fine", "   "], "not a list"):
        with pytest.raises(ArchiveEmbeddingError):
            provider.embed_documents(texts)
    assert fake.requests == []


# --- local provider ---

LOCAL_LIMIT = 16
SPECIAL_TOKENS = {"cls_token": "[CLS]", "pad_token": "[PAD]", "unk_token": "[UNK]",
                  "sep_token": {"content": "[SEP]", "lstrip": False, "normalized": False, "rstrip": False,
                                "single_word": False},
                  "additional_special_tokens": ["[EXTRA]"]}  # a list: the loader skips it
LOADER_ADDITIONS = ["[CLS]", "[PAD]", "[UNK]", SPECIAL_TOKENS["sep_token"]]


def pin(model_dir, files, name, data):
    (model_dir / name).write_bytes(data)
    files[name] = (len(data), hashlib.sha256(data).hexdigest())


@pytest.fixture
def pinned_model(tmp_path):
    """A provisioned model whose policy files say what fastembed's loader reads: limit min(64, 16)."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    files = {}
    for name, data in {"model.onnx": b"synthetic weights", "tokenizer.json": TOKENIZER,
                       "config.json": json.dumps({"hidden_size": DIM}).encode(),
                       "tokenizer_config.json": json.dumps({"model_max_length": 64, "max_length": LOCAL_LIMIT}).encode(),
                       "special_tokens_map.json": json.dumps(SPECIAL_TOKENS).encode()}.items():
        pin(model_dir, files, name, data)
    return model_dir, files


def fake_registry(**entry):
    """A fake fastembed registry lookup: only "synthetic/model", as a registered entry with these fields.
    It stands in for the backend's listing and proves nothing about which names fastembed really loads."""
    description = {"model": "synthetic/model", "dim": DIM, "model_file": "model.onnx", "additional_files": [],
                   **entry}
    return lambda model: description if model == "synthetic/model" else None


@pytest.fixture(autouse=True)
def registered_synthetic_model(monkeypatch):
    monkeypatch.setattr("kelmo.embeddings.backend_model_description", fake_registry())


def local_provider(model_dir, files, **options):
    from kelmo.embeddings import LocalFastEmbedProvider
    return LocalFastEmbedProvider(model_dir, model="synthetic/model", files=files, pooling="mean",
                                  projection="none", normalization="l2", dimension=DIM, **options)


def test_local_space_is_established_from_the_hashed_pinned_files(pinned_model):
    from kelmo.model_store import artifact_identity
    model_dir, files = pinned_model
    space = local_provider(model_dir, files).describe()
    assert space.artifact_identity == "synthetic/model@" + artifact_identity(files)
    assert space.tokenizer_identity == "sha256:" + files["tokenizer.json"][1]
    assert "query_embed" in space.query_transform and "passage_embed" in space.document_transform
    assert (space.dimension, space.dtype, space.byte_order) == (DIM, "float32", "little")
    assert transforms_bind(space, local_provider(model_dir, files).input_policy())


@pytest.mark.parametrize("registry, reason", [
    (lambda model: None, "model_unsupported"),                            # a name the backend does not register
    (fake_registry(model_file="onnx/model.onnx"), "model_unsupported"),   # the backend would load another graph
    (fake_registry(additional_files=["model.onnx_data"]), "model_unsupported"),  # an unpinned sidecar it reads
    (fake_registry(dim=DIM * 2), "space_mismatch"),                       # another registered dimension
])
def test_a_custom_model_the_backend_would_not_load_as_pinned_is_refused(pinned_model, monkeypatch, registry,
                                                                        reason):
    model_dir, files = pinned_model
    monkeypatch.setattr("kelmo.embeddings.backend_model_description", registry)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()
    assert refused.value.data["reason"] == reason


@pytest.mark.parametrize("change", [dict(query_prefix="query: "), dict(document_prefix="passage: ")])
def test_a_local_input_path_change_is_another_space(pinned_model, change):
    model_dir, files = pinned_model
    assert identity_id(local_provider(model_dir, files, **change).describe()) != \
        identity_id(local_provider(model_dir, files).describe())


def test_a_changed_or_missing_model_is_refused_without_network(pinned_model, tmp_path, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise OSError("Network is forbidden in this test")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    model_dir, files = pinned_model
    (model_dir / "model.onnx").write_bytes(b"changed weights!!")
    for provider in (local_provider(model_dir, files), local_provider(tmp_path / "absent", files)):
        with pytest.raises(ArchiveEmbeddingError) as refused:
            provider.embed_documents(["a note"])
        assert refused.value.data["reason"] == "model_missing"
    assert attempts == []


class FakeTextEmbedding:
    def __init__(self, output=None):
        self.calls = []
        self.output = output

    def query_embed(self, texts):
        self.calls.append(("query_embed", list(texts)))
        return [self.output if self.output is not None else unit(len(t)) for t in texts]

    def passage_embed(self, texts):
        self.calls.append(("passage_embed", list(texts)))
        return [self.output if self.output is not None else unit(len(t) + 1) for t in texts]


def test_local_queries_and_documents_take_their_own_paths_and_prefixes(pinned_model, monkeypatch):
    model_dir, files = pinned_model
    fake = FakeTextEmbedding()
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding", lambda model, path: fake)
    provider = local_provider(model_dir, files, query_prefix="query: ", document_prefix="passage: ")
    assert provider.embed_queries(["вопрос"]).purpose == "query"
    assert provider.embed_documents(["note"]).purpose == "document"
    assert fake.calls == [("query_embed", ["query: вопрос"]), ("passage_embed", ["passage: note"])]


@pytest.mark.parametrize("output", [np.full(DIM, np.nan), np.ones(DIM + 1),
                                    # DIM values in another shape: refused, not flattened into a vector
                                    np.ones((1, DIM)), np.ones((DIM, 1)), np.ones((2, DIM // 2)),
                                    # values that are not numbers: refused, not parsed
                                    np.array(["0.5"] * DIM), np.ones(DIM, dtype=bool)])
def test_local_nonfinite_or_wrong_dimension_output_is_refused(pinned_model, monkeypatch, output):
    model_dir, files = pinned_model
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding",
                        lambda model, path: FakeTextEmbedding(output))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).embed_documents(["note"])
    assert refused.value.data["reason"] == "malformed_vectors"


def test_weights_replaced_after_describe_are_refused_before_loading(pinned_model, monkeypatch):
    model_dir, files = pinned_model
    loads = []
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding",
                        lambda model, path: loads.append(path) or FakeTextEmbedding())
    provider = local_provider(model_dir, files)
    provider.describe()
    provider.input_policy()
    (model_dir / "model.onnx").write_bytes(b"replaced weights!")  # same size, other bytes
    for call in (lambda: provider.embed_documents(["note"]), provider.health):
        with pytest.raises(ArchiveEmbeddingError) as refused:
            call()
        assert refused.value.data["reason"] == "model_missing"
    assert loads == [], "the replaced weights never reach the loader under the old identity"


def test_local_windows_count_what_the_model_sees_up_to_its_pinned_limit(pinned_model):
    model_dir, files = pinned_model
    provider = local_provider(model_dir, files, document_prefix="passage: ")
    assert provider.input_policy().max_tokens == LOCAL_LIMIT
    documents, queries = provider.token_window(), provider.token_window(purpose="query")
    assert documents.budget == LOCAL_LIMIT and documents.identity == provider.describe().tokenizer_identity
    # A literal [SEP] is one special token, as the loader adds it: [CLS] a [SEP] b [SEP], plus "passage", ":".
    assert (queries.count("a [SEP] b"), documents.count("a [SEP] b")) == (5, 7)
    with pytest.raises(ArchiveValidationError):
        provider.token_window(LOCAL_LIMIT + 1)


def test_a_local_input_over_the_model_limit_is_refused_before_the_model_runs(pinned_model, monkeypatch):
    model_dir, files = pinned_model
    fake = FakeTextEmbedding()
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding", lambda model, path: fake)
    provider = local_provider(model_dir, files)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["w " * (LOCAL_LIMIT - 1)])
    assert refused.value.data["reason"] == "input_too_long" and fake.calls == []
    assert len(provider.embed_documents(["w " * (LOCAL_LIMIT - 2)]).vectors) == 1


@pytest.mark.parametrize("dropped", ["tokenizer_config.json", "special_tokens_map.json", "config.json",
                                     "model.onnx"])
@pytest.mark.parametrize("also_on_disk", [True, False])
def test_a_custom_model_missing_a_pinned_loader_file_is_refused(pinned_model, dropped, also_on_disk):
    model_dir, files = pinned_model
    files = {name: value for name, value in files.items() if name != dropped}
    if not also_on_disk:
        (model_dir / dropped).unlink()
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()
    assert refused.value.data["reason"] == "model_missing"


def test_an_unpinned_weight_or_sidecar_file_is_refused_before_and_after_describe(pinned_model, monkeypatch):
    model_dir, files = pinned_model
    loads = []
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding",
                        lambda model, path: loads.append(path) or FakeTextEmbedding())
    (model_dir / "model.onnx_data").write_bytes(b"external weights")
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()
    assert refused.value.data["reason"] == "model_missing"
    pin(model_dir, files, "model.onnx_data", b"external weights")
    provider = local_provider(model_dir, files)
    provider.describe()
    (model_dir / "model.onnx_data").write_bytes(b"replaced weights")  # the pinned sidecar replaced
    (model_dir / "extra").mkdir()
    (model_dir / "extra" / "weights.bin").write_bytes(b"unlisted")  # and an unlisted file added
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.embed_documents(["note"])
    assert refused.value.data["reason"] == "model_missing" and loads == []


def test_a_custom_model_with_two_graphs_or_a_linked_entry_is_refused(pinned_model, tmp_path):
    model_dir, files = pinned_model
    pin(model_dir, files, "model_quantized.onnx", b"other graph")
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()
    assert refused.value.data["reason"] == "model_missing"
    (model_dir / "model_quantized.onnx").unlink()
    del files["model_quantized.onnx"]
    (model_dir / "linked").symlink_to(tmp_path)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()
    assert refused.value.data["reason"] == "model_missing"


@pytest.mark.parametrize("name, data", [
    ("config.json", json.dumps({"hidden_size": DIM * 2}).encode()),
    ("1_Pooling/config.json", json.dumps({"pooling_mode_cls_token": True,
                                          "pooling_mode_mean_tokens": False}).encode()),
])
def test_a_declared_spec_the_artifact_metadata_contradicts_is_refused(pinned_model, name, data):
    model_dir, files = pinned_model
    (model_dir / "1_Pooling").mkdir()
    pin(model_dir, files, name, data)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).describe()  # declares mean pooling and dimension DIM
    assert refused.value.data["reason"] == "space_mismatch"


@pytest.mark.parametrize("config, limit", [
    ({"max_length": 16}, 16), ({"model_max_length": 40}, 40),
    ({"model_max_length": 1000000000000000019884624838656}, None),  # the "no limit" sentinel
    ({"model_max_length": 512.0}, None), ({"max_length": 0}, None), ({"do_lower_case": True}, None),
])
def test_the_input_limit_is_the_loaders_own_and_unusable_values_are_refused(pinned_model, config, limit):
    model_dir, files = pinned_model
    pin(model_dir, files, "tokenizer_config.json", json.dumps(config).encode())
    provider = local_provider(model_dir, files)
    if limit is not None:
        assert provider.input_policy().max_tokens == limit
        return
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.input_policy()
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_the_local_preparation_is_the_loaders_additions_in_order(pinned_model):
    model_dir, files = pinned_model
    provider = local_provider(model_dir, files)
    assert provider.input_policy().preparation == preparation_digest(LOADER_ADDITIONS)
    assert provider.token_window().preparation == provider.input_policy().preparation != NO_ADDITIONS


@pytest.mark.parametrize("value", [{"content": "[SEP]", "bogus": True}, {"content": ""}, ""])
def test_a_malformed_special_tokens_map_has_no_provable_input_policy(pinned_model, value):
    model_dir, files = pinned_model
    pin(model_dir, files, "special_tokens_map.json", json.dumps({**SPECIAL_TOKENS, "sep_token": value}).encode())
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).input_policy()
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_a_policy_file_linked_out_of_the_model_directory_is_refused(pinned_model, tmp_path):
    model_dir, files = pinned_model
    outside = tmp_path / "outside.json"
    outside.write_bytes((model_dir / "tokenizer_config.json").read_bytes())
    (model_dir / "tokenizer_config.json").unlink()
    (model_dir / "tokenizer_config.json").symlink_to(outside)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).input_policy()
    assert refused.value.data["reason"] == "model_missing"


def test_local_health_proves_the_policy_and_loads_the_model(pinned_model, monkeypatch):
    model_dir, files = pinned_model
    monkeypatch.setattr("kelmo.embeddings.load_text_embedding", lambda model, path: FakeTextEmbedding())
    assert local_provider(model_dir, files).health() == {"ok": True}

    def broken(model, path):
        raise RuntimeError("onnx runtime missing")

    monkeypatch.setattr("kelmo.embeddings.load_text_embedding", broken)
    with pytest.raises(ArchiveEmbeddingError) as refused:
        local_provider(model_dir, files).health()
    assert refused.value.data["reason"] == "embedding_unavailable"


# --- configuration ---

def vault(tmp_path, embedding=""):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "kelmo.toml").write_text(
        f'schema_version = 1\nwriter = "local"\nnode_label = "test"\nvault_id = "{uuid.uuid4()}"\n' + embedding)
    return root


def test_no_embedding_section_keeps_the_existing_configuration(tmp_path):
    from kelmo.config import Config
    config = Config.load(vault(tmp_path))
    assert config.embedding is None and "embedding" not in config.as_dict()


def test_local_embedding_settings_are_explicit(tmp_path):
    from kelmo.config import Config
    from kelmo.model_store import FILES, MODEL, PINNED_SPEC
    config = Config.load(vault(tmp_path, f'[embedding]\nprovider = "local"\nmodel = "{MODEL}"\n'
                                         'model_dir = ".cache/embeddings/model"\ntoken_budget = 256\n'))
    settings = config.embedding
    assert settings["model_dir"] == str(config.root / ".cache/embeddings/model")
    assert settings["files"] == FILES and settings["dimension"] == PINNED_SPEC["dimension"]
    assert settings["tokenizer"] == str(config.root / ".cache/embeddings/model/tokenizer.json")
    assert config.as_dict()["embedding"]["provider"] == "local"


@pytest.mark.parametrize("section", [
    '[embedding]\nprovider = "local"\nmodel = "other/model"\nmodel_dir = "/m"\ntoken_budget = 256\n',
    '[embedding]\nprovider = "local"\nmodel = "x"\nmodel_dir = "/m"\n',
    '[embedding]\nprovider = "remote"\ntoken_budget = 256\n',
    '[embedding]\nprovider = "broker"\nfds_env = "X"\ntokenizer = "/t.json"\ntoken_budget = 512\n'
    '[embedding.space]\ndimension = 8\n',
    '[embedding]\nprovider = "local"\nmodel = "x"\nmodel_dir = "/m"\ntoken_budget = 256\nhost = "h"\n',
])
def test_incomplete_or_unknown_embedding_settings_are_refused(tmp_path, section):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as refused:
        Config.load(vault(tmp_path, section))
    assert refused.value.code == "E_CONFIG"


def space_toml(space):
    return "[embedding.space]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in space.to_dict().items())


def input_toml(max_tokens=BROKER_LIMIT, query_prefix="", document_prefix="", special_tokens=()):
    return ("[embedding.input]\n" f"max_tokens = {max_tokens}\nquery_prefix = {json.dumps(query_prefix)}\n"
            f"document_prefix = {json.dumps(document_prefix)}\nspecial_tokens = {json.dumps(list(special_tokens))}\n")


def broker_section(extra="", space=None, declared=None):
    return ('[embedding]\nprovider = "broker"\nfds_env = "SYNTHETIC_EMBED_FDS"\ntokenizer = "/t.json"\n'
            + extra + (input_toml() if declared is None else declared) + space_toml(space or broker_space()) + "\n")


def test_broker_settings_carry_the_whole_declared_space_and_input(tmp_path, monkeypatch):
    from kelmo.config import Config
    from kelmo.embeddings import provider_from_settings
    settings = Config.load(vault(tmp_path, broker_section("token_budget = 16\n"))).embedding
    assert settings["space"] == broker_space().to_dict() and settings["token_budget"] == 16
    assert settings["input"] == {"max_tokens": BROKER_LIMIT, "query_prefix": "", "document_prefix": "",
                                 "special_tokens": []}
    assert (settings["query_prefix"], settings["document_prefix"]) == (None, None)
    monkeypatch.delenv("SYNTHETIC_EMBED_FDS", raising=False)
    with pytest.raises(ArchiveEmbeddingError):
        provider_from_settings(settings)


@pytest.mark.parametrize("declared", [
    "",                                                  # no declared input: chunks could not be cut offline
    input_toml(document_prefix="passage: "),             # not the path the declared transforms name
    input_toml(special_tokens=["[SEP]"]),
    input_toml(max_tokens=BROKER_LIMIT * 2),
    "[embedding.input]\nmax_tokens = 32\nquery_prefix = \"\"\ndocument_prefix = \"\"\n",  # incomplete
])
def test_a_declared_broker_input_the_space_does_not_name_is_refused(tmp_path, declared):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as refused:
        Config.load(vault(tmp_path, broker_section(declared=declared)))
    assert refused.value.code == "E_CONFIG"


def test_a_budget_above_the_declared_broker_limit_is_refused(tmp_path):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError):
        Config.load(vault(tmp_path, broker_section(f"token_budget = {BROKER_LIMIT + 1}\n")))


def test_the_declared_broker_window_is_built_without_any_channel(tmp_path):
    from kelmo.config import Config
    from kelmo.embeddings import declared_window
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_bytes(TOKENIZER)
    section = broker_section().replace('tokenizer = "/t.json"', f"tokenizer = {json.dumps(str(tokenizer))}")
    settings = Config.load(vault(tmp_path, section)).embedding
    window = declared_window(settings)
    assert (window.identity, window.preparation, window.budget) == (TOKENIZER_ID, NO_ADDITIONS, BROKER_LIMIT)
    tokenizer.write_bytes(TOKENIZER.replace(b"[PAD]", b"[PAX]"))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        declared_window(settings)
    assert refused.value.data["reason"] == "input_policy_unproven"


def test_the_token_budget_defaults_to_the_model_limit(tmp_path):
    from kelmo.config import Config
    from kelmo.model_store import MODEL
    config = Config.load(vault(tmp_path, f'[embedding]\nprovider = "local"\nmodel = "{MODEL}"\nmodel_dir = "/m"\n'))
    assert config.embedding["token_budget"] is None


@pytest.mark.parametrize("section", [
    lambda model: f'[embedding]\nprovider = "local"\nmodel = "{model}"\nmodel_dir = "/m"\ntokenizer = "/other.json"\n',
    lambda model: f'[embedding]\nprovider = "local"\nmodel = "{model}"\nmodel_dir = "/m"\ndimension = 8\n',
    lambda model: f'[embedding]\nprovider = "local"\nmodel = "{model}"\nmodel_dir = "/m"\npooling = "cls"\n',
    lambda model: broker_section('document_prefix = "passage: "\n'),
    lambda model: broker_section(space=broker_space(dtype="float64")),
    lambda model: broker_section(space=broker_space(byte_order="big")),
    lambda model: broker_section(space=make_space()),  # transforms that name no input path
])
def test_settings_that_would_misstate_the_input_path_or_space_are_refused(tmp_path, section):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.model_store import MODEL
    with pytest.raises(KelmoError) as refused:
        Config.load(vault(tmp_path, section(MODEL)))
    assert refused.value.code == "E_CONFIG"


def pinned_table_section(spec):
    """The pinned record model named with an explicit [embedding.files] table equal to its own."""
    from kelmo.model_store import FILES, MODEL
    pins = "".join(f'"{name}" = [{size}, "{checksum}"]\n' for name, (size, checksum) in FILES.items())
    return (f'[embedding]\nprovider = "local"\nmodel = "{MODEL}"\nmodel_dir = "/m"\n' + spec
            + "[embedding.files]\n" + pins)


@pytest.mark.parametrize("spec", [
    "",
    'pooling = "mean"\nprojection = "none"\nnormalization = "l2"\ndimension = 384\n',
])
def test_an_explicit_pin_table_equal_to_the_pinned_model_keeps_its_fixed_spec(tmp_path, spec):
    from kelmo.config import Config
    from kelmo.model_store import FILES, PINNED_SPEC
    settings = Config.load(vault(tmp_path, pinned_table_section(spec))).embedding
    assert settings["files"] == FILES
    assert {key: settings[key] for key in PINNED_SPEC} == PINNED_SPEC


@pytest.mark.parametrize("spec", [
    'pooling = "cls"\nprojection = "none"\nnormalization = "l2"\ndimension = 384\n',
    'pooling = "mean"\nprojection = "linear"\nnormalization = "l2"\ndimension = 384\n',
    'pooling = "mean"\nprojection = "none"\nnormalization = "none"\ndimension = 384\n',
    'pooling = "mean"\nprojection = "none"\nnormalization = "l2"\ndimension = 8\n',
    'pooling = "cls"\n',
])
def test_an_explicit_pin_table_equal_to_the_pinned_model_refuses_a_false_spec(tmp_path, spec):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as refused:
        Config.load(vault(tmp_path, pinned_table_section(spec)))
    assert refused.value.code == "E_CONFIG" and "pinned model fixes" in str(refused.value)


@pytest.mark.parametrize("change", [{"pooling": "cls"}, {"projection": "linear"}, {"normalization": "none"},
                                    {"dimension": 8}, {"dimension": 384.0}])
def test_a_provider_built_directly_refuses_a_false_spec_for_the_pinned_bytes(tmp_path, change):
    from kelmo.embeddings import LocalFastEmbedProvider
    from kelmo.model_store import FILES, MODEL, PINNED_SPEC
    with pytest.raises(ValueError, match="pinned model fixes"):
        LocalFastEmbedProvider(tmp_path, model=MODEL, files=FILES, **{**PINNED_SPEC, **change})
    assert LocalFastEmbedProvider(tmp_path, model=MODEL, files=FILES, **PINNED_SPEC).pinned_record_model is True


CUSTOM_FILES = ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                "model.onnx")


def custom_section(names=CUSTOM_FILES, checksum="a" * 64):
    pins = "".join(f'"{name}" = [10, "{checksum}"]\n' for name in names)
    return ('[embedding]\nprovider = "local"\nmodel = "other/model"\nmodel_dir = "/m"\npooling = "mean"\n'
            'projection = "none"\nnormalization = "l2"\ndimension = 8\n[embedding.files]\n' + pins)


def test_a_custom_model_pinning_every_loader_file_loads(tmp_path):
    from kelmo.config import Config
    names = (*CUSTOM_FILES, "model.onnx_data", "1_Pooling/config.json")
    assert set(Config.load(vault(tmp_path, custom_section(names))).embedding["files"]) == set(names)


@pytest.mark.parametrize("section", [
    custom_section(checksum="A" * 64),                                    # checksums are lowercase hex
    custom_section(("tokenizer.json",)),                                  # the tokenizer alone pins nothing
    custom_section(CUSTOM_FILES[:-1]),                                    # no weights
    custom_section(CUSTOM_FILES[1:]),                                     # no config.json
    custom_section((*CUSTOM_FILES, "onnx/model_quantized.onnx")),         # which graph loads is unproven
    custom_section((*CUSTOM_FILES, "../outside.bin")),                    # outside the model directory
])
def test_a_custom_pin_table_that_cannot_cover_the_loader_is_refused(tmp_path, section):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    with pytest.raises(KelmoError) as refused:
        Config.load(vault(tmp_path, section))
    assert refused.value.code == "E_CONFIG"
