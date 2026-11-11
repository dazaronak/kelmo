"""Fixtures for the edge core and `kelmod hook` tests.

The edge core runs in process against a fake SharedClient that owns a real client profile: a synthetic UUID
authority written by `prepare_profile`, the same call the shared-client fixtures make, with a channel command that
is never opened. Only the `kelmod hook` tests start a process, and they run the real binary.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from kelmo.client import prepare_profile
from kelmo.config import ClientProfile
from kelmo.core.edge_core import EdgeCore

REPO = Path(__file__).resolve().parents[2]
KELMOD = Path(os.environ.get('KELMOD') or REPO / 'rust' / 'target' / 'debug' / 'kelmod')
AUTHORITY = '00000000-0000-4000-8000-00000000e09e'


@pytest.fixture
def kelmod():
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)
    return KELMOD


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / 'client'
    prepare_profile(root, AUTHORITY, ['/bin/false'])
    return ClientProfile.load(root)


class FakeClient:
    """The SharedClient surface that EdgeCore and `hook()` use, recording what would have been sent."""

    def __init__(self, profile):
        self.profile = profile
        self.captured = []  # (provider, action, payload, deliver, internal_job) for each capture_session call
        self.deadlines = []  # seconds passed to deadline(), in order
        self.flushed = threading.Event()
        self.closed = False

    @contextmanager
    def deadline(self, seconds):
        self.deadlines.append(seconds)
        yield

    def flush(self):
        self.flushed.set()
        return {'reached': True, 'stopped': None, 'pending': 0, 'delivered': 0}

    def capture_session(self, provider, action, payload, *, deliver=True, restart=True, internal_job=None):
        self.captured.append((provider, action, dict(payload), deliver, internal_job))
        return {'queued': True, 'accepted': False}

    def close(self):
        self.closed = True


@pytest.fixture
def edge(profile):
    """(core, client, wakes) for an EdgeCore whose first deliverer pass is finished.

    `wake` becomes a recorder: the deliverer would otherwise wake on its own and ingest the spool at the same time
    as the test does.
    """
    client = FakeClient(profile)
    core = EdgeCore(profile.root, client=client)
    assert client.flushed.wait(10), 'the deliverer finished no first pass'
    wakes = []
    core.wake = lambda: wakes.append(True)
    client.deadlines.clear()
    yield core, client, wakes
    core.close()
