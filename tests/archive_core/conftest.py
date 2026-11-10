"""Fixtures for the archive core tests.

The daemon harness and the state core's helpers are imported by module name, so their
directories go on sys.path here. Every vault is synthetic and lives under the test's tmp_path.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
for directory in (TESTS, TESTS / 'daemon', TESTS / 'state_core', HERE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

GIT_IDENTITY = {
    'GIT_AUTHOR_NAME': 'Synthetic Fixture',
    'GIT_COMMITTER_NAME': 'Synthetic Fixture',
    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A new vault with its archive and journal, made by the product's own initialization."""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    from kelmo.service import Service
    root = tmp_path / 'vault'
    Service.initialize(root, 'fixture-node')
    return root


@pytest.fixture
def offline_embedding(monkeypatch):
    """The embedding model cannot be reached, so a captured record keeps its vector pending."""
    from kelmo.store.errors import ArchiveEmbeddingError

    def unavailable(*args, **kwargs):
        raise ArchiveEmbeddingError('Synthetic offline embedding provider')
    monkeypatch.setattr('kelmo.store.memory.store.embed', unavailable)
