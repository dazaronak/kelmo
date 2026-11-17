"""`kelmo status` and `kelmo client status` on an edge machine run as edge.cli; they only read the outbox, so a hook
that arrives while they wait for the authority gets the outbox at once. A command that rewrites the outbox still
holds it for the whole command."""
import threading
import time
from types import SimpleNamespace

import pytest

from kelmo import cli


def slow_command(monkeypatch):
    """Replaces the borrowed CLI run with one that waits until released, as a slow authority would."""
    entered, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        return {'ok': True}

    monkeypatch.setattr(cli, 'run_borrowed_client', slow)
    return entered, release


def hook_waits(core, argv, monkeypatch):
    """Seconds an urgent caller (a hook) waits for the owner lock while edge.cli runs `argv`; None past 1 s."""
    entered, release = slow_command(monkeypatch)
    context = SimpleNamespace(remaining=lambda: 30.0, check=lambda: None)
    command = threading.Thread(target=lambda: core.handle_cli(context, {'argv': argv}), daemon=True)
    command.start()
    try:
        assert entered.wait(5)
        start = time.monotonic()
        got = core._owner.acquire(timeout=1.0, urgent=True)
        waited = time.monotonic() - start
        if got:
            core._owner.release()
        return waited if got else None
    finally:
        release.set()
        command.join(10)


@pytest.mark.parametrize('argv', [['status'], ['client', 'status']], ids=['status', 'client-status'])
def test_a_hook_gets_the_outbox_while_a_status_command_waits(edge, monkeypatch, argv):
    core, _client, _ = edge
    waited = hook_waits(core, argv, monkeypatch)
    assert waited is not None and waited < 1.0, f'the hook waited for `kelmo {" ".join(argv)}`'


def test_a_command_that_rewrites_the_outbox_still_holds_it(edge, monkeypatch):
    core, _client, _ = edge
    assert hook_waits(core, ['client', 'flush'], monkeypatch) is None, 'a flush let the hook in mid-command'


class SlowHelloChannel:
    """A channel whose hello waits until released, as an authority far away would answer."""

    def __init__(self, authority, entered, release):
        self.authority, self.entered, self.release, self.closed = authority, entered, release, False

    def request(self, frame, timeout=None):
        from kelmo.client import PROTOCOL
        self.entered.set()
        self.release.wait(10)
        return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True,
                'result': {'authority_id': self.authority, 'operations': ['hello']}}

    def close(self):
        self.closed = True


def test_a_hook_gets_the_outbox_while_a_status_probe_waits_for_the_authority(profile):
    from kelmo.client import SharedClient
    from kelmo.core.edge_core import EdgeCore
    entered, release = threading.Event(), threading.Event()
    channels = []

    def factory():
        channels.append(SlowHelloChannel(profile.authority_id, entered, release))
        return channels[-1]

    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    context = SimpleNamespace(remaining=lambda: 30.0, check=lambda: None)
    answers = []
    command = threading.Thread(target=lambda: answers.append(
        core.handle_cli(context, {'argv': ['client', 'status', '--probe', '--vault', str(profile.root)]})),
        daemon=True)  # the installed CLI appends the client root it resolved (cli.py through_daemon)
    try:
        command.start()
        assert entered.wait(5), 'the probe never opened a channel'
        start = time.monotonic()
        got = core._owner.acquire(timeout=1.0, urgent=True)
        waited = time.monotonic() - start
        if got:
            core._owner.release()
        assert got and waited < 1.0, 'the hook waited for `kelmo client status --probe`'
    finally:
        release.set()
        command.join(10)
        core.close()
    assert answers and all(channel.closed for channel in channels), (answers, channels)


def test_a_command_that_does_not_parse_is_not_a_status_read():
    """It goes on to the borrowed run, which refuses it under the owner lock with the command's own error."""
    for argv in (['no-such-command'], ['client', 'status', '--no-such-option']):
        try:
            answer = cli.borrowed_read_only(argv)
        except Exception as error:  # noqa: BLE001 - the answer under test, whatever it is
            answer = error
        assert answer is False, (argv, answer)

