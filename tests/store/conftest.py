"""Shared test fixtures."""
import os
import tempfile
import pytest
from unittest.mock import patch
import numpy as np

from kelmo.store.db import open_connection, init_schema
from kelmo.store.config import EMBEDDING_DIM


@pytest.fixture
def tmp_db():
    """Fresh archive DB in a temp file, schema initialized."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = open_connection(path)
    init_schema(conn)
    yield conn, path
    conn.close()
    os.unlink(path)


@pytest.fixture
def mock_embed():
    """Mock fastembed to avoid downloading 100MB model in tests."""
    def fake_embed(text: str) -> bytes:
        import hashlib
        h = int(hashlib.md5(text.encode()).hexdigest(), 16)
        rng = np.random.RandomState(h % (2**31))
        vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        return vec.tobytes()

    with (
        patch("kelmo.store.memory.embed.embed", side_effect=fake_embed) as m,
        patch("kelmo.store.memory.store.embed", side_effect=fake_embed),
        patch("kelmo.store.memory.search.embed", side_effect=fake_embed),
    ):
        yield m
