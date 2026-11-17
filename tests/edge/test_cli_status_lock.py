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


class OneAtATimeChannel:
    """A kelmo-client/1 channel (one request at a time): hello answers at once, `status` waits until released,
    as an authority far away would answer."""

    def __init__(self, authority, entered, release):
        self.authority, self.entered, self.release = authority, entered, release

    def request(self, frame, timeout=None):
        from kelmo.client import PROTOCOL
        if frame['op'] == 'hello':
            result = {'authority_id': self.authority, 'operations': ['hello', 'status']}
        else:
            self.entered.set()
            self.release.wait(10)
            result = {'ok': True}
        return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

    def close(self):
        pass


def test_a_hook_gets_the_outbox_while_status_waits_on_a_one_request_channel(profile):
    """Against an authority that answers kelmo-client/1 the channel carries one request at a time; `kelmo status`
    waiting for its reply holds the channel, not the owner of the outbox."""
    from kelmo.client import SharedClient
    from kelmo.core.edge_core import EdgeCore
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: OneAtATimeChannel(profile.authority_id, entered, release),
                          multiplex=True)
    core = EdgeCore(profile.root, client=client)
    context = SimpleNamespace(remaining=lambda: 30.0, check=lambda: None)
    answers = []
    command = threading.Thread(target=lambda: answers.append(
        core.handle_cli(context, {'argv': ['status', '--vault', str(profile.root)]})), daemon=True)
    try:
        command.start()
        assert entered.wait(5), 'status never reached the authority'
        start = time.monotonic()
        got = core._owner.acquire(timeout=1.0, urgent=True)
        waited = time.monotonic() - start
        if got:
            core._owner.release()
        assert got and waited < 1.0, 'the hook waited for `kelmo status` on a one-request channel'
    finally:
        release.set()
        command.join(10)
        core.close()
    assert answers, 'status gave no answer'


def test_a_hook_with_writes_queued_does_not_outwait_its_deadline_behind_a_slow_hello(profile):
    """A status command connecting to a slow authority holds the connect; a delivery that needs the channel
    meanwhile gives up at its own deadline, entries pending, instead of waiting for that hello."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: SlowHelloChannel(profile.authority_id, entered, release),
                          multiplex=True)
    client.outbox.enqueue('remember', {'content': 'synthetic queued note'}, 'synthetic-key-1')
    reader = threading.Thread(target=client._connect, daemon=True)  # how `kelmo status` opens the channel
    try:
        reader.start()
        assert entered.wait(5), 'the connect never started'
        start = time.monotonic()
        with client.deadline(1.0):
            result = client.flush()
        waited = time.monotonic() - start
        assert waited < 2.0, f'the delivery waited {waited:.1f} s for another caller\'s hello'
        assert result['pending'] == 1, result
    finally:
        release.set()
        reader.join(10)
        client.close()


def test_a_write_on_a_one_request_channel_closed_under_it_stays_pending(profile):
    """A one-request channel closed while a write waited for its turn raises ValueError on the closed pipe; the
    delivery reports it as a transport failure and keeps the write, instead of failing the caller."""
    from kelmo.client import PROTOCOL, SharedClient

    class ClosedChannel:
        def request(self, frame, timeout=None):
            if frame['op'] == 'hello':
                return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True,
                        'result': {'authority_id': profile.authority_id, 'operations': ['hello', 'remember']}}
            raise ValueError('I/O operation on closed file.')

        def close(self):
            pass

    client = SharedClient(profile, channel_factory=ClosedChannel, multiplex=True)
    client.outbox.enqueue('remember', {'content': 'synthetic queued note'}, 'synthetic-key-2')
    try:
        result = client.flush()
    finally:
        client.close()
    assert result['stopped'] == 'E_TRANSPORT' and result['pending'] == 1, result


def test_a_probe_hello_on_its_own_channel_does_not_hold_the_main_channel(profile):
    """Requests take turns per channel: a probe waiting for its own channel's hello leaves the client's channel
    free for a read."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    opened = []

    class Channel:
        def __init__(self):
            self.first = not opened
            opened.append(self)

        def request(self, frame, timeout=None):
            from kelmo.client import PROTOCOL
            if frame['op'] == 'hello' and not self.first:
                entered.set()
                release.wait(10)
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
                      if frame['op'] == 'hello' else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            pass

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        assert client.call('status') == {'ok': True}  # the client's own channel is open
        probe = threading.Thread(target=client.probe, daemon=True)
        probe.start()
        assert entered.wait(5), 'the probe never opened its channel'
        start = time.monotonic()
        answer = client.call('status')
        assert answer == {'ok': True} and time.monotonic() - start < 1.0, 'the read waited for the probe hello'
    finally:
        release.set()
        client.close()


def _channels(profile, first_status):
    """A factory of one-request channels: the first answers `status` with `first_status()`, the later ones with
    {'ok': True}."""
    from kelmo.client import PROTOCOL
    opened = []

    class Channel:
        def __init__(self):
            self.number = len(opened)
            self.closed = False
            opened.append(self)

        def request(self, frame, timeout=None):
            if frame['op'] == 'hello':
                result = {'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
            elif self.number == 0:
                result = first_status()
            else:
                result = {'ok': True}
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            self.closed = True

    return Channel, opened


def test_a_read_whose_one_request_channel_was_closed_under_it_opens_a_new_one(profile):
    """A read that waited for its turn on a channel another caller closed meanwhile answers over a new channel,
    as it did when reads held the client's lock, instead of failing E_TRANSPORT."""
    from kelmo.client import SharedClient

    def closed():
        raise ValueError('I/O operation on closed file.')

    factory, opened = _channels(profile, closed)
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    try:
        assert client.call('status') == {'ok': True}
    finally:
        client.close()
    assert len(opened) == 2 and opened[0].closed, opened


def test_a_deadline_that_passes_while_waiting_for_the_turn_leaves_the_channel_open(profile, monkeypatch):
    """Nothing was written when the deadline is found passed inside the turn: the healthy channel stays."""
    from kelmo.client import SharedClient, StdioChannel, Unbound

    class Pipe(StdioChannel):
        def __init__(self):
            self.timeout, self.grace, self.closed = 1.0, 0, False

        def request(self, frame):
            from kelmo.client import PROTOCOL
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
                      if frame['op'] == 'hello' else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            self.closed = True

    client = SharedClient(profile, channel_factory=Pipe, multiplex=True)
    try:
        assert client.call('status') == {'ok': True}
        channel = client._channel
        real = client.remaining

        def remaining():
            if client._turn_for(channel)._is_owned():
                raise Unbound('E_TRANSPORT', 'The deadline passed before the shared kelmo server answered.')
            return real()

        monkeypatch.setattr(client, 'remaining', remaining)
        with pytest.raises(Unbound):
            client.call('status')
        assert client._channel is channel and not channel.closed, 'a request never written closed the channel'
    finally:
        client.close()


def test_dropping_a_failed_channel_does_not_wait_without_bound_for_its_turn(profile):
    """A request is still on a one-request channel that failed for another caller: the drop returns within a bounded
    wait and the channel is closed when that request ends, never under it (a descriptor closed under a read could be
    reused and the read would take another channel's reply)."""
    from kelmo.client import PROTOCOL, SharedClient
    entered, release = threading.Event(), threading.Event()

    class Channel:
        closed = False

        def request(self, frame, timeout=None):
            if frame['op'] != 'hello' and not entered.is_set():
                entered.set()
                release.wait(10)
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
                      if frame['op'] == 'hello' else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            assert not entered.is_set() or release.is_set(), 'closed under a request in flight'
            self.closed = True

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        channel = client._connect()
        reader = threading.Thread(target=client.call, args=('status',), daemon=True)
        reader.start()
        assert entered.wait(5)
        dropper = threading.Thread(target=client._drop, args=(channel,), daemon=True)
        start = time.monotonic()
        dropper.start()
        dropper.join(3)
        assert not dropper.is_alive() and time.monotonic() - start < 3, 'the drop waited for the other request'
        assert not channel.closed, 'the channel was closed under the request in flight'
        release.set()
        reader.join(10)
        assert channel.closed, 'the channel was not closed once its request ended'
    finally:
        release.set()
        client.close()


def test_two_borrowed_status_commands_at_once_leave_standard_output_alone(monkeypatch):
    """Parsing a borrowed command silences standard output for the parse only; two at once must not leave it
    pointing at one parse's buffer."""
    import sys
    a_in, b_in, a_done = threading.Event(), threading.Event(), threading.Event()
    real = cli.build_parser

    def build_parser(version):
        parser, commands = real(version)
        parse = parser.parse_args

        def parse_args(argv):
            if threading.current_thread().name == 'first':
                a_in.set()
                b_in.wait(1)
            else:
                b_in.set()
                a_done.wait(5)
            return parse(argv)

        parser.parse_args = parse_args
        return parser, commands

    monkeypatch.setattr(cli, 'build_parser', build_parser)
    original = sys.stdout

    def first():
        cli.borrowed_read_only(['status'])
        a_done.set()

    one = threading.Thread(target=first, name='first', daemon=True)
    two = threading.Thread(target=lambda: cli.borrowed_read_only(['status']), name='second', daemon=True)
    one.start()
    assert a_in.wait(5)
    two.start()
    one.join(10)
    two.join(10)
    left, sys.stdout = sys.stdout, original
    assert left is original, 'standard output was left redirected'


def test_status_reports_the_outbox_busy_instead_of_outwaiting_its_deadline(profile):
    """`kelmo status` reads the outbox summary within its deadline: while another caller holds the outbox (a
    flush rewriting it), status answers with the outbox busy instead of waiting past its deadline."""
    from kelmo.client import ClientService, SharedClient
    factory, _opened = _channels(profile, lambda: {'ok': True})
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with client.outbox._serial:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert holding.wait(5)
        start = time.monotonic()
        with client.deadline(1.0):
            status = ClientService(client).status()
        assert time.monotonic() - start < 2.0, 'status waited for the outbox past its deadline'
        assert status['outbox'] == {'busy': True}, status
    finally:
        release.set()
        holder.join(10)
        client.close()


def test_a_read_whose_deadline_passed_before_its_turn_leaves_the_channel_open(profile):
    from kelmo.client import SharedClient, Unbound
    factory, _opened = _channels(profile, lambda: {'ok': True})
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    try:
        channel = client._connect()
        with client.deadline(0.01):
            time.sleep(0.05)
            with pytest.raises(Unbound):
                client._read('status', {}, channel)
        assert client._channel is channel and not channel.closed, 'a request never written dropped the channel'
    finally:
        client.close()


def test_a_read_whose_frame_was_written_before_the_channel_closed_is_not_sent_again(profile):
    """A closed pipe found after the frame went out is not "nothing written": the read is not sent a second time."""
    from kelmo.client import PROTOCOL, SharedClient, StdioChannel, Unbound
    sent = []

    class Pipe(StdioChannel):
        def __init__(self):
            self.timeout, self.grace, self.closed, self.wrote = 1.0, 0, False, False

        def request(self, frame):
            if frame['op'] == 'hello':
                return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True,
                        'result': {'authority_id': profile.authority_id, 'operations': ['hello', 'status']}}
            sent.append(frame['op'])
            self.wrote = True
            raise ValueError('I/O operation on closed file.')

        def close(self):
            self.closed = True

    client = SharedClient(profile, channel_factory=Pipe, multiplex=True)
    try:
        with pytest.raises(Unbound):
            client.call('status')
        assert sent == ['status'], sent
    finally:
        client.close()


def test_client_status_reports_the_outbox_busy_instead_of_outwaiting_its_deadline(profile):
    from kelmo.client import SharedClient
    factory, _opened = _channels(profile, lambda: {'ok': True})
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with client.outbox._serial:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert holding.wait(5)
        start = time.monotonic()
        with client.deadline(1.0):
            status = client.local_status()
        assert time.monotonic() - start < 2.0, 'client status waited for the outbox past its deadline'
        assert status['outbox'] == {'busy': True}, status
    finally:
        release.set()
        holder.join(10)
        client.close()


def test_a_probe_closes_its_channel_within_the_deadline(profile):
    from kelmo.client import PROTOCOL, SharedClient
    graces = []

    class Channel:
        grace = 5

        def request(self, frame, timeout=None):
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True,
                    'result': {'authority_id': profile.authority_id, 'operations': ['hello']}}

        def close(self):
            graces.append(self.grace)

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        with client.deadline(1.0):
            client.probe()
        assert graces and graces[0] <= 1.0, graces
    finally:
        client.close()


def test_status_answered_at_the_end_of_its_deadline_still_reports(profile):
    """The server answered within the deadline but the outbox summary comes after it: status reports what it has
    instead of failing."""
    from kelmo.client import ClientService, SharedClient

    def slow():
        time.sleep(0.4)
        return {'ok': True}

    factory, _opened = _channels(profile, slow)
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    try:
        with client.deadline(0.3):
            status = ClientService(client).status()
        assert status['ok'] is True and 'outbox' in status, status
    finally:
        client.close()


def test_a_borrowed_parse_leaves_standard_error_to_other_threads(monkeypatch, capsys):
    """The edge core logs to standard error from other threads; a borrowed parse must not swap it, and a command
    that does not parse is refused without printing."""
    import sys
    seen = []
    real = cli.build_parser

    def build_parser(version):
        parser, commands = real(version)
        parse = parser.parse_args

        def parse_args(argv):
            seen.append(sys.stderr)
            return parse(argv)

        parser.parse_args = parse_args
        return parser, commands

    monkeypatch.setattr(cli, 'build_parser', build_parser)
    original = sys.stderr
    assert cli.borrowed_read_only(['status']) is True
    assert seen == [original], 'standard error was swapped during the parse'
    assert cli.borrowed_read_only(['client', 'no-such-action']) is False
    assert capsys.readouterr().err == ''


def test_a_hook_gets_the_outbox_while_status_waits_on_a_kelmo2_channel(profile):
    """The same for a kelmo/2 channel, which carries several requests at once."""
    from kelmo.client import SharedClient
    from kelmo.core.edge_core import EdgeCore
    entered, release = threading.Event(), threading.Event()

    class Concurrent(OneAtATimeChannel):
        concurrent = True
        alive = True
        stalled = False

    client = SharedClient(profile, channel_factory=lambda: Concurrent(profile.authority_id, entered, release),
                          multiplex=True)
    core = EdgeCore(profile.root, client=client)
    context = SimpleNamespace(remaining=lambda: 30.0, check=lambda: None)
    answers = []
    command = threading.Thread(target=lambda: answers.append(
        core.handle_cli(context, {'argv': ['status', '--vault', str(profile.root)]})), daemon=True)
    try:
        command.start()
        assert entered.wait(5), 'status never reached the authority'
        start = time.monotonic()
        got = core._owner.acquire(timeout=1.0, urgent=True)
        waited = time.monotonic() - start
        if got:
            core._owner.release()
        assert got and waited < 1.0, 'the hook waited for `kelmo status` on a kelmo/2 channel'
    finally:
        release.set()
        command.join(10)
        core.close()
    assert answers, 'status gave no answer'


def test_a_command_that_does_not_parse_is_not_a_status_read():
    """It goes on to the borrowed run, which refuses it under the owner lock with the command's own error."""
    for argv in (['no-such-command'], ['client', 'status', '--no-such-option']):
        try:
            answer = cli.borrowed_read_only(argv)
        except Exception as error:  # noqa: BLE001 - the answer under test, whatever it is
            answer = error
        assert answer is False, (argv, answer)


def test_dropping_a_failed_channel_does_not_wait_past_its_deadline_for_a_caller_still_connecting(profile):
    """Another caller holds the connect for a slow hello: a status whose request failed returns within its own
    deadline, and the failed channel is never handed out again."""
    from kelmo.client import SharedClient
    factory, _opened = _channels(profile, lambda: {'ok': True})
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    connecting, release = threading.Event(), threading.Event()

    def slow_hello():
        with client._connecting:
            connecting.set()
            release.wait(10)

    try:
        failed = client._connect()
        holder = threading.Thread(target=slow_hello, daemon=True)
        holder.start()
        assert connecting.wait(5)
        start = time.monotonic()
        with client.deadline(1.0):
            client._drop(failed)
        assert time.monotonic() - start < 2.0, 'the drop waited past its deadline for the hello of another caller'
        release.set()
        holder.join(10)
        assert client._connect() is not failed, 'the failed channel was handed out again'
        assert failed.closed
    finally:
        release.set()
        client.close()


def test_client_status_reports_the_outbox_busy_while_another_process_holds_the_file(profile):
    import sqlite3
    from kelmo.client import SharedClient
    factory, _opened = _channels(profile, lambda: {'ok': True})
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    other = sqlite3.connect(profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    other.execute('BEGIN EXCLUSIVE')
    release = threading.Timer(6.0, other.rollback)
    release.start()
    try:
        start = time.monotonic()
        with client.deadline(1.0):
            status = client.local_status()
        assert time.monotonic() - start < 2.0, 'client status waited for another process past its deadline'
        assert status['outbox'] == {'busy': True}, status
    finally:
        release.cancel()
        other.rollback()
        other.close()
        client.close()


def test_a_drop_that_marks_the_channel_as_its_request_ends_still_gets_it_closed(profile, monkeypatch):
    """The request on the channel checks the mark just before the drop sets it, then lets the channel go: the channel
    is still closed, by whichever of the two sees the mark with the channel free."""
    from kelmo.client import PROTOCOL, SharedClient
    checked, marked = threading.Event(), threading.Event()

    class Channel:
        closed = False
        _mark = False

        @property
        def close_when_free(self):
            value = self._mark
            if threading.current_thread().name == 'status-reader' and not checked.is_set():  # its own check
                checked.set()
                marked.wait(5)
            return value

        @close_when_free.setter
        def close_when_free(self, value):
            self._mark = value
            marked.set()

        def request(self, frame, timeout=None):
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
                      if frame['op'] == 'hello' else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            self.closed = True

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        channel = client._connect()
        reader = threading.Thread(target=client.call, args=('status',), name='status-reader', daemon=True)
        reader.start()
        assert checked.wait(5)
        client._drop(channel)
        reader.join(10)
        assert channel.closed, 'the marked channel was never closed'
    finally:
        marked.set()
        client.close()


def test_a_channel_closed_when_its_request_ends_waits_no_longer_than_that_request_may(profile, monkeypatch):
    """The drop came from a caller with no deadline; the request that closes the channel bounds the close's grace
    by its own deadline, so it does not wait seconds for the process to exit."""
    from kelmo.client import PROTOCOL, SharedClient
    entered, release, graces = threading.Event(), threading.Event(), []

    class Channel:
        closed = False
        grace = 5

        def request(self, frame, timeout=None):
            if frame['op'] != 'hello' and not entered.is_set():
                entered.set()
                release.wait(10)
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status']}
                      if frame['op'] == 'hello' else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            graces.append(self.grace)
            self.closed = True

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)

    def read():
        with client.deadline(2.0):
            client.call('status')

    try:
        channel = client._connect()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert entered.wait(5)
        client._drop(channel)  # no deadline on this caller
        release.set()
        reader.join(10)
        assert channel.closed and graces and graces[0] <= 2.0, graces
    finally:
        release.set()
        client.close()


class WritesChannel:
    """A kelmo-client/1 channel that offers `capture` too; `status` waits until released."""

    def __init__(self, authority, entered, release):
        self.authority, self.entered, self.release, self.frames = authority, entered, release, []

    def request(self, frame, timeout=None):
        from kelmo.client import PROTOCOL
        self.frames.append(frame)
        if frame['op'] == 'hello':
            result = {'authority_id': self.authority, 'operations': ['hello', 'status', 'capture']}
        elif frame['op'] == 'status':
            self.entered.set()
            self.release.wait(10)
            result = {'ok': True}
        else:
            result = {'operation_id': 'op-1'}
        return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

    def close(self):
        pass


def test_dropping_a_one_request_channel_under_another_request_does_not_wait_for_that_request(profile):
    """A delivery drops the channel under the owner of the outbox; a status request on it closes it when it ends,
    so the drop marks it and returns instead of holding the owner meanwhile."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    reader = threading.Thread(target=client.call, args=('status',), daemon=True)
    try:
        channel = client._connect()
        reader.start()
        assert entered.wait(5)
        start = time.monotonic()
        client._close_channel(channel)
        assert time.monotonic() - start < 0.5, 'the drop waited for the request on the channel'
        assert channel.close_when_free, 'the channel was not marked for its request to close'
    finally:
        release.set()
        reader.join(10)
        client.close()


def test_a_channel_marked_to_close_once_free_is_closed_once(profile):
    """A stdio channel has no `closed` flag: the mark itself is cleared, so a later caller does not close it again."""
    from kelmo.client import SharedClient
    closes = []

    class Channel:
        def request(self, frame, timeout=None):
            raise AssertionError('no request is made here')

        def close(self):
            closes.append(1)

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        channel = Channel()
        channel.close_when_free = True
        client._close_if_marked(channel)
        client._close_if_marked(channel)
        assert closes == [1], closes
    finally:
        client.close()


def test_a_channel_dropped_after_the_connect_handed_it_out_carries_no_request(profile):
    """A caller that could not wait for the connect marks the channel dropped just after `_connect` checked it: the
    request is refused before a byte is written, so a read goes again over a new channel."""
    from kelmo.client import Closed, SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    try:
        channel = client._connect()
        channel.dropped = True
        with pytest.raises(Closed):
            client._send(channel, 'status', {}, None)
        assert [frame['op'] for frame in channel.frames] == ['hello'], channel.frames
    finally:
        release.set()
        client.close()


def test_a_delivery_refused_because_the_channel_was_busy_counts_no_attempt(profile):
    """Nothing reached the server: the entry's attempts and last refusal stay as they were."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    held, done = threading.Event(), threading.Event()

    def hold_the_turn():  # another request in flight on the one-request channel
        with client._turn_for(channel):
            held.set()
            done.wait(10)

    reader = threading.Thread(target=hold_the_turn, daemon=True)
    try:
        channel = client._connect()
        reader.start()
        assert held.wait(5)
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'busy-one')
        with client.deadline(0.3):
            result = client.flush()
        assert result['stopped'] == 'E_BUSY', result
        entry = client.outbox.pending()[0]
        assert (entry['attempts'], entry['last_error']) == (0, None), entry
    finally:
        done.set()
        release.set()
        reader.join(10)
        client.close()


def test_daemon_status_answers_while_another_caller_holds_the_outbox_connection(profile):
    """`kelmo daemon status` has five seconds: it does not wait for a thread inside the outbox (a status command
    reading it past another process's lock)."""
    from kelmo.client import SharedClient
    from kelmo.core.edge_core import EdgeCore
    client = SharedClient(profile, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    held, release = threading.Event(), threading.Event()

    def hold():
        with client.outbox._serial:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert held.wait(5)
        start = time.monotonic()
        status = core.status()
        assert time.monotonic() - start < 2.0, 'daemon status waited for the outbox connection'
        assert status['outbox'] == {'busy': True}, status
    finally:
        release.set()
        holder.join(10)
        core.close()



def test_a_hook_gets_the_outbox_while_status_waits_and_a_delivery_needs_the_channel(profile):
    """kelmo-client/1 carries one request at a time. While `kelmo status` waits for its reply, a delivery takes the
    outbox and needs the channel; status reads over a channel of its own, so the delivery is not held behind it and
    a hook gets the outbox within a second."""
    from kelmo.client import SharedClient
    from kelmo.core.edge_core import EdgeCore
    entered, release = threading.Event(), threading.Event()
    channels = []

    def factory():
        channels.append(WritesChannel(profile.authority_id, entered, release))
        return channels[-1]

    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    context = SimpleNamespace(remaining=lambda: 30.0, check=lambda: None)
    status = threading.Thread(target=lambda: core.handle_cli(context, {'argv': ['status', '--vault',
                                                                               str(profile.root)]}), daemon=True)

    def deliver():
        with core.client.deadline(5.0):
            try:
                core.client.flush()
            except Exception:  # noqa: BLE001 - only the hook's wait is measured here
                pass

    delivery = threading.Thread(target=deliver, daemon=True)
    try:
        status.start()
        assert entered.wait(5), 'status never reached the authority'
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'pending-one')
        delivery.start()
        time.sleep(0.3)  # the delivery takes the outbox and asks for a channel
        start = time.monotonic()
        got = core._owner.acquire(timeout=1.0, urgent=True)
        waited = time.monotonic() - start
        if got:
            core._owner.release()
        assert got and waited < 1.0, f'the hook waited {waited:.2f} s behind a delivery held by status'
    finally:
        release.set()
        status.join(10)
        delivery.join(10)
        core.close()


def test_a_status_read_on_a_cold_channel_holds_no_connect_a_delivery_waits_for(profile):
    """No channel is open yet and the authority answers a hello slowly: `kelmo status` connects on a channel of its
    own, so a delivery's connect does not wait for that hello."""
    from kelmo.client import PROTOCOL, SharedClient
    entered, release, opened = threading.Event(), threading.Event(), []

    class Channel:
        def __init__(self, slow):
            self.slow, self.frames = slow, []

        def request(self, frame, timeout=None):
            self.frames.append(frame['op'])
            if frame['op'] == 'hello' and self.slow:
                entered.set()
                release.wait(10)
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'status', 'capture']}
                      if frame['op'] == 'hello' else {'operation_id': 'op-1'} if frame['op'] == 'capture'
                      else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            pass

    def factory():
        opened.append(Channel(slow=not opened))
        return opened[-1]

    from kelmo.client import ClientService
    client = SharedClient(profile, channel_factory=factory, multiplex=True)
    reader = threading.Thread(target=lambda: ClientService(client).status(), daemon=True)
    try:
        reader.start()
        assert entered.wait(5), 'status never connected'
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'cold-one')
        started = time.monotonic()
        with client.deadline(3.0):
            result = client.flush()
        assert time.monotonic() - started < 1.0, 'the delivery waited for the status hello'
        assert result['delivered'] == 1, result
    finally:
        release.set()
        reader.join(10)
        client.close()


def test_a_direct_close_clears_the_mark_so_the_channel_is_closed_once(profile):
    from kelmo.client import SharedClient
    closes = []

    class Channel:
        def request(self, frame, timeout=None):
            raise AssertionError('no request is made here')

        def close(self):
            closes.append(1)

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    try:
        channel = Channel()
        channel.close_when_free = True  # marked earlier by a drop that could not get the turn
        client._close_channel(channel)
        client._close_if_marked(channel)
        assert closes == [1], closes
    finally:
        client.close()


def test_a_mark_seen_before_another_caller_closed_the_channel_does_not_close_it_again(profile):
    from kelmo.client import SharedClient
    closes = []
    client = SharedClient(profile, multiplex=True)

    class Channel:
        _mark = True
        inner = False

        @property
        def close_when_free(self):
            value = self._mark
            if value and not Channel.inner:
                Channel.inner = True
                client._close_if_marked(self)  # another caller closes it right after this one read the mark
            return value

        @close_when_free.setter
        def close_when_free(self, value):
            self._mark = value

        def close(self):
            closes.append(1)

    try:
        client._close_if_marked(Channel())
        assert closes == [1], closes
    finally:
        client.close()


def test_a_channel_dropped_while_a_delivery_waited_for_its_turn_carries_no_request(profile):
    """The delivery checked the channel, then waited for the turn; a drop marked the channel meanwhile. Nothing is
    written on it, and the entry counts no attempt and keeps no transport refusal."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    try:
        channel = client._connect()
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'dropped-one')
        turn = client._turn_for(channel)
        turn.acquire()
        results = []
        delivery = threading.Thread(target=lambda: results.append(client.flush()), daemon=True)
        delivery.start()
        time.sleep(0.3)  # the delivery waits for the turn
        channel.dropped = True
        turn.release()
        delivery.join(10)
        assert [frame['op'] for frame in channel.frames] == ['hello'], channel.frames
        entry = client.outbox.get('dropped-one')
        assert (entry['attempts'], entry['last_error']) == (0, None), entry
    finally:
        release.set()
        client.close()


def test_a_bounded_summary_counts_its_wait_for_the_outbox_against_its_bound(profile):
    """Half the bound went to waiting for another thread in the outbox; another process then holds the file: the
    summary answers busy within its bound, not after the bound again."""
    import sqlite3
    from kelmo.client import SharedClient
    client = SharedClient(profile, multiplex=True)
    other = sqlite3.connect(profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    held = threading.Event()

    def hold():
        with client.outbox._serial:
            held.set()
            other.execute('BEGIN EXCLUSIVE')
            time.sleep(0.6)

    holder = threading.Thread(target=hold, daemon=True)
    try:
        holder.start()
        assert held.wait(5)
        started = time.monotonic()
        summary = client.outbox.summary_within(1.0)
        took = time.monotonic() - started
        assert summary == {'busy': True}, summary
        assert took < 1.3, f'the summary took {took:.2f} s for a bound of 1 s'
    finally:
        holder.join(10)
        other.rollback()
        other.close()
        client.close()



def test_a_read_on_a_one_request_channel_keeps_the_owner_so_an_urgent_hook_goes_before_a_delivery(profile):
    """A tool read (`trace`) waits for the authority on a channel that carries one request at a time. It keeps the
    owner of the outbox while it waits, as before this change, so a hook queued as urgent takes the owner when the
    read ends, ahead of a delivery that waits for it; the hook is not held behind that delivery's own send."""
    from kelmo.client import PROTOCOL, SharedClient
    from kelmo.core.edge_core import EdgeCore
    entered, release = threading.Event(), threading.Event()

    class Channel:
        def request(self, frame, timeout=None):
            if frame['op'] == 'trace':
                entered.set()
                release.wait(10)
            if frame['op'] == 'capture':
                time.sleep(2.0)
            result = ({'authority_id': profile.authority_id, 'operations': ['hello', 'trace', 'capture']}
                      if frame['op'] == 'hello' else {'operation_id': 'op-1'} if frame['op'] == 'capture'
                      else {'ok': True})
            return {'protocol': PROTOCOL, 'request_id': frame['request_id'], 'ok': True, 'result': result}

        def close(self):
            pass

    client = SharedClient(profile, channel_factory=Channel, multiplex=True)
    core = EdgeCore(profile.root, client=client)
    reader = threading.Thread(target=lambda: client.call('trace', {'operation_id': 'op-0'}), daemon=True)

    def deliver():
        with client.deadline(8.0):
            try:
                client.flush()
            except Exception:  # noqa: BLE001 - only the hook's wait is measured here
                pass

    delivery = threading.Thread(target=deliver, daemon=True)
    try:
        reader.start()
        assert entered.wait(5), 'the read never reached the authority'
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'behind-the-read')
        delivery.start()
        time.sleep(0.3)  # the delivery asks for the outbox (or, without it, for the channel)
        threading.Timer(0.5, release.set).start()
        started = time.monotonic()
        got = core._owner.acquire(timeout=2.5, urgent=True)
        waited = time.monotonic() - started
        if got:
            core._owner.release()
        assert got and waited < 1.5, f'the urgent hook waited {waited:.2f} s behind a delivery started after the read'
    finally:
        release.set()
        reader.join(10)
        delivery.join(10)
        core.close()



def test_a_bounded_summary_holds_its_bound_across_all_its_queries(profile):
    """Another process takes the outbox file between the summary's queries, each time for half a second: the
    summary reads under one transaction, so the file cannot be taken between its queries and it answers within
    its bound."""
    import sqlite3
    from kelmo.client import SharedClient
    client = SharedClient(profile, multiplex=True)
    client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'counted')
    other = sqlite3.connect(profile.state / 'outbox.sqlite3', timeout=0, check_same_thread=False)
    seen = []

    def between(statement):
        if not statement.lstrip().upper().startswith('SELECT'):
            return
        seen.append(statement)
        if len(seen) < 2:
            return
        try:
            other.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            return  # the summary's own read lock keeps the file
        threading.Timer(0.5, other.rollback).start()

    client.outbox.conn.set_trace_callback(between)
    try:
        started = time.monotonic()
        summary = client.outbox.summary_within(1.0)
        took = time.monotonic() - started
        assert took < 1.3, f'the summary took {took:.2f} s for a bound of 1 s ({summary})'
    finally:
        client.outbox.conn.set_trace_callback(None)
        time.sleep(0.6)
        other.close()
        client.close()


def test_a_delivery_refused_here_before_it_was_sent_counts_no_attempt(profile, monkeypatch):
    """The deadline passed after the channel was open and before the frame went out: nothing reached the server,
    so the entry counts no attempt."""
    from kelmo.client import SharedClient, Unsent
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    try:
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'unsent-one')

        real = client._send

        def refused(channel, op, *args, **kwargs):
            if op != 'capture':
                return real(channel, op, *args, **kwargs)  # the hello goes through: the channel is open
            raise Unsent('E_TRANSPORT', 'The deadline passed before the request was sent.')

        monkeypatch.setattr(client, '_send', refused)
        client.flush()
        entry = client.outbox.get('unsent-one')
        assert entry['attempts'] == 0, entry
    finally:
        release.set()
        client.close()


def test_a_delivery_whose_deadline_passes_before_its_kelmo2_request_counts_no_attempt(profile):
    """The deadline runs out between counting the attempt and sending on a kelmo/2 channel: nothing reached the
    server, so the entry's attempts and last refusal stay as they were."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()

    class Concurrent(WritesChannel):
        concurrent = True
        alive = True
        stalled = False

    client = SharedClient(profile, channel_factory=lambda: Concurrent(profile.authority_id, entered, release),
                          multiplex=True)
    real = client.outbox.attempt

    def slow_attempt(key, *rest):
        result = real(key, *rest)
        time.sleep(0.5)  # past the pass's deadline before the request is made
        return result

    client.outbox.attempt = slow_attempt
    try:
        client._connect()
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'late-one')
        with client.deadline(0.3):
            client.flush()
        entry = client.outbox.pending()[0]
        assert (entry['attempts'], entry['last_error']) == (0, None), entry
    finally:
        release.set()
        client.close()


def test_a_delivery_whose_deadline_passes_before_its_turn_on_a_one_request_channel_keeps_its_refusal(profile):
    """The deadline runs out between counting the attempt and taking a one-request channel's turn: nothing reached
    the server, so the entry's attempts and last refusal stay as they were."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    real = client.outbox.attempt

    def slow_attempt(key, *rest):
        result = real(key, *rest)
        time.sleep(0.5)  # past the pass's deadline before the request takes the channel's turn
        return result

    client.outbox.attempt = slow_attempt
    try:
        client._connect()
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'late-turn')
        client.outbox.note('late-turn', 'E_EARLIER')
        with client.deadline(0.3):
            client.flush()
        entry = client.outbox.pending()[0]
        assert (entry['attempts'], entry['last_error']) == (0, 'E_EARLIER'), entry
    finally:
        release.set()
        client.close()


def test_a_delivery_whose_deadline_passes_before_its_channel_is_connected_keeps_its_refusal(profile):
    """The deadline runs out after the flush began and before the channel's hello: nothing reached the server,
    so the entry's last refusal stays as it was."""
    from kelmo.client import SharedClient
    entered, release = threading.Event(), threading.Event()
    client = SharedClient(profile, channel_factory=lambda: WritesChannel(profile.authority_id, entered, release),
                          multiplex=True)
    real = client._hello_channel

    def slow_hello():
        time.sleep(0.5)  # past the pass's deadline before the channel is started
        return real()

    client._hello_channel = slow_hello
    try:
        client.outbox.enqueue('capture', {'content': 'note', 'intent': 'archive'}, 'late-connect')
        client.outbox.note('late-connect', 'E_EARLIER')
        with client.deadline(0.3):
            client.flush()
        entry = client.outbox.pending()[0]
        assert (entry['attempts'], entry['last_error']) == (0, 'E_EARLIER'), entry
    finally:
        release.set()
        client.close()
