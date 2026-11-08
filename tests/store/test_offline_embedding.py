"""An empty model cache must fail without attempting network access."""
import socket

import pytest

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.memory.embed import embed


def test_missing_model_never_attempts_network(tmp_path, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise OSError('Network is forbidden in this test')

    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    with pytest.raises(ArchiveEmbeddingError):
        embed('Synthetic offline check', cache_dir=str(tmp_path / 'empty-model-cache'))
    assert attempts == []
