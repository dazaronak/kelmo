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
