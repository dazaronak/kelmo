"""Fixtures for the embedding core tests.

The daemon harness is imported by module name, so its directory goes on sys.path here. Every
tokenizer and vault is synthetic and lives under the test's temp directory.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
for directory in (TESTS, TESTS / 'daemon', HERE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


@pytest.fixture(autouse=True)
def local_counting():
    """No test leaves a remote counter installed.

    Every TokenWindow counts with its own tokenizer unless the test installs a counter.
    """
    from kelmo.store.memory.embed import install_remote_counter
    install_remote_counter(None)
    yield
    install_remote_counter(None)
