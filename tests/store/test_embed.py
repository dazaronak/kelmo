"""Tests for embedding module."""
import pytest
from kelmo.store.config import EMBEDDING_DIM
from kelmo.store.errors import ArchiveEmbeddingError


def test_embed_returns_bytes_of_correct_length(mock_embed):
    from kelmo.store.memory.embed import embed
    result = embed("test sentence")
    assert isinstance(result, bytes)
    assert len(result) == EMBEDDING_DIM * 4


def test_embed_empty_string_raises():
    from kelmo.store.memory import embed as embed_mod
    with pytest.raises(ArchiveEmbeddingError):
        embed_mod.embed("")


def test_embed_deterministic(mock_embed):
    from kelmo.store.memory.embed import embed
    a = embed("hello world")
    b = embed("hello world")
    assert a == b


def test_embed_different_inputs_differ(mock_embed):
    from kelmo.store.memory.embed import embed
    a = embed("hello")
    b = embed("goodbye")
    assert a != b
