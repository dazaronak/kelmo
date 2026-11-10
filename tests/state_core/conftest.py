"""Fixtures for the state core tests. The shared suites and the daemon harness import each other by
module name, so their directories go on sys.path here. Every core a test opens is closed at its end."""
import sys
from pathlib import Path

import pytest

from kelmo.core.state_core import StateCore

TESTS = Path(__file__).resolve().parents[1]
for directory in (TESTS, TESTS / 'daemon'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


@pytest.fixture
def core_factory():
    cores = []

    def make(root, **options):
        core = StateCore(root, **options)
        cores.append(core)
        return core

    yield make
    for core in reversed(cores):
        core.close()
