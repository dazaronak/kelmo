"""Fixtures for the job tests. The state core and the daemon harness are imported by module name, so
their directories go on sys.path here, as in tests/state_core. Every core a test opens is closed at its
end."""
import sys
from pathlib import Path

import pytest

from kelmo.core.state_core import StateCore

TESTS = Path(__file__).resolve().parents[1]
for directory in (TESTS, TESTS / 'daemon', TESTS / 'state_core'):
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


@pytest.fixture(autouse=True)
def no_executor_group_directory(monkeypatch):
    """A state core points the executor group directory at its own vault. The module value is restored
    after each test, so one vault's directory never leaks into another test."""
    monkeypatch.setattr('kelmo.daemon.executor_groups', None)
