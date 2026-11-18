"""A CLI read run by the edge core (`edge.cli`) whose channel to the server is cut, while the server's kelmod is
gone. The read reconnects with a pause between tries, and in the edge core a pause lends the outbox to other
callers and takes it back. When another caller keeps the outbox past the read's deadline, the read fails with a
typed error inside that deadline.

The client is a real `SharedClient` over a real `kelmod attach --protocol 1`, as in `test_attach_cut`, inside a
real `EdgeCore`; the daemon behind the bridge is `scripted_daemon.ScriptedDaemon`.
"""
import sys
import threading
import time
from pathlib import Path

DAEMON_TESTS = Path(__file__).resolve().parents[1] / 'daemon'
if str(DAEMON_TESTS) not in sys.path:
    sys.path.insert(0, str(DAEMON_TESTS))

from scripted_daemon import ANSWER, CLOSE, Connection  # noqa: E402
from test_attach_cut import bridge, requests  # noqa: E402,F401

from kelmo.core.edge_core import EdgeCore  # noqa: E402
from kelmo.errors import KelmoError  # noqa: E402

SLACK = 0.3  # starting the command's thread, a bridge process, joining


class Context:
    def __init__(self, remaining):
        self._remaining = remaining

    def remaining(self):
        return self._remaining

    def check(self):
        """Never cancelled."""


def test_a_cut_read_whose_reconnect_pause_cannot_take_the_outbox_back_fails_inside_its_deadline(bridge):  # noqa: F811 - the fixture
    # The read is answered by nothing: its channel closes after it was sent, and the daemon stays gone while
    # the read reconnects, so every reconnect pauses.
    daemon, client = bridge(Connection(ANSWER, CLOSE, gone=10.0))
    core = EdgeCore(client.profile.root, client=client)
    core.wake = lambda: None  # the deliverer must not take the outbox on its own during the test
    deadline = 2.0
    lent = threading.Event()
    real_pause = core._pause

    def pause(seconds):
        lent.set()
        return real_pause(seconds)

    client.pause = pause
    outcome = []

    def read():
        try:
            outcome.append(core.handle_cli(Context(deadline), {'argv': ['search', '--vault', str(core.root),
                                                                        'synthetic']}))
        except Exception as error:  # noqa: BLE001 - the test asserts on what the call did
            outcome.append(error)

    try:
        began = time.monotonic()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert lent.wait(deadline), 'the read never paused to reconnect'
        assert core._owner.acquire(timeout=5), 'the read never lent the outbox out'
        try:
            thread.join(deadline + 3.0)
            ended = time.monotonic() - began
        finally:
            core._owner.release()
        thread.join(10)
        assert not thread.is_alive(), 'the read is still blocked'
    finally:
        core.close()

    result = outcome[0]
    assert ended < deadline + SLACK, (ended, result)
    assert isinstance(result, KelmoError), result
    assert (result.code, getattr(result, 'retryable', None)) == ('E_BUSY', True), result
    assert [op for op, _, _ in requests(daemon)] == ['search']
