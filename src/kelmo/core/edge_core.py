"""The edge core: the one owner of a client root's outbox and caches, started by `kelmod serve` on an edge
machine. It is `SharedClient` behind the daemon: tool calls (`edge.tool`) run the MCP tool functions over
`ClientService`, hooks (`edge.hook`) run `client.hook` with what `kelmod hook` read in the hook's process, and a
deliverer thread carries the outbox to the authority, woken by every hook that queued something.

Reads go to the authority several at a time, over one channel that matches replies by id. Whatever touches the
outbox (a write, a delivery pass, a transcript capture) takes the owner lock inside the client, one caller at a
time, a hook before a tool.

    python -m kelmo.core.edge_core --client ROOT       (descriptor 3 is the channel to kelmod)
"""
import argparse
import inspect
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..errors import KelmoError

# Tool calls in flight in this core; the router queues the rest, round robin between sessions. Reads overlap
# on the channel to the authority, whose own lanes bound what runs there.
HANDLER_LANE = 32
HOOK_LANE = 4
# How long one delivery pass may hold the outbox: a write that needs it waits at most this long.
DELIVER_SECONDS = 5.0
# How often the deliverer tries on its own when nothing woke it (a write queued while the authority was down).
DELIVER_INTERVAL = 30.0
# Bound of a hook that is not a SessionStart or prompt hook (those carry their own, shorter deadline).
HOOK_SECONDS = 3.0
# Bound of one spooled hook replayed by the deliverer.
SPOOL_HOOK_SECONDS = 10.0

_SERVICE = ContextVar('kelmo_edge_service')


class OwnerLock:
    """A reentrant lock shared by urgent and ordinary callers, which take turns while both wait: an urgent
    caller goes first, but never twice in a row past a waiting ordinary one. A hook is urgent (a session waits
    on it); the tool calls of busy sessions would otherwise take the client ahead of it again and again, and a
    stream of hooks must not shut the tools out either."""

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._holder = None
        self._count = 0
        self._waiting = {True: 0, False: 0}
        self._last_urgent = False

    def _turn(self, urgent):
        if self._holder is not None:
            return False
        other = self._waiting[not urgent]
        return not other or (urgent != self._last_urgent)

    def acquire(self, timeout=None, urgent=False):
        me = threading.get_ident()
        end = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if self._holder == me:
                self._count += 1
                return True
            self._waiting[urgent] += 1
            try:
                while not self._turn(urgent):
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._condition.wait(left)
                self._holder, self._count, self._last_urgent = me, 1, urgent
                return True
            finally:
                self._waiting[urgent] -= 1
                # A caller that gave up may have been the one whose turn it was.
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._holder != threading.get_ident():
                raise RuntimeError('release of an owner lock this thread does not hold')
            self._count -= 1
            if not self._count:
                self._holder = None
                self._condition.notify_all()


class _OwnerGate:
    """What `SharedClient` takes around its outbox in the edge core: the owner lock, waited for within the
    calling thread's deadline and urgently for a hook. A caller that cannot get it in time is refused as busy."""

    def __init__(self, core):
        self._core = core

    def __enter__(self):
        from ..client import Busy
        core = self._core
        try:
            seconds = core.client.remaining()
        except KelmoError:  # the deadline has passed: one try, no wait
            seconds = 0.0
        if not core._owner.acquire(timeout=seconds, urgent=getattr(core._urgent, 'value', False)):
            raise Busy('The edge core is busy with another write; retry.')
        return self

    def __exit__(self, *_):
        self._core._owner.release()


class EdgeCore:
    def __init__(self, root, client=None):
        from ..client import SharedClient
        from ..config import ClientProfile
        self.root = Path(root).expanduser().resolve()
        self.client = client or SharedClient(ClientProfile.load(self.root), multiplex=True)
        self._tools = None
        # The outbox is one connection and a delivery is one ordered pass: one caller at a time owns it, handlers
        # and the deliverer alike. Reads do not take it.
        self._owner = OwnerLock()
        self._urgent = threading.local()
        self.client._lock = _OwnerGate(self)
        self._depth = threading.local()
        self._acks = {}
        self._acks_lock = threading.Lock()
        self.client.pause = self._pause
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._last = {'at': None, 'result': None}
        self._wake.set()  # the first pass ingests what was spooled while no daemon ran
        self._deliverer = threading.Thread(target=self._deliver_loop, name='deliverer', daemon=True)
        self._deliverer.start()

    @contextmanager
    def owned(self, seconds, urgent=False):
        """Holds the outbox for one caller across a whole section, waiting at most `seconds` for it."""
        from ..client import Busy
        if not self._owner.acquire(timeout=max(0.0, seconds), urgent=urgent):
            raise Busy('The edge core is busy with another write; retry.')
        self._depth.count = getattr(self._depth, 'count', 0) + 1
        try:
            yield self.client
        finally:
            self._depth.count -= 1
            self._owner.release()

    @contextmanager
    def serving(self, context, cap=None, urgent=False):
        """Bounds one call by what is left of its deadline (at most `cap` seconds) and says whether its writes go
        ahead of waiting tools. The owner lock is taken inside the client, only where the call touches the
        outbox; its reads overlap with every other call's."""
        context.check()
        left = context.remaining() if cap is None else min(cap, context.remaining())
        previous = getattr(self._urgent, 'value', False)
        self._urgent.value = urgent
        try:
            with self.client.deadline(max(0.001, left)):
                yield self.client
        finally:
            self._urgent.value = previous

    # Acknowledgements of HOT deliveries, run when `kelmod hook` reports that the session printed the context.
    ACK_SECONDS = 60.0

    def _hold_acks(self, acknowledgements):
        import uuid
        now = time.monotonic()
        with self._acks_lock:
            for token in [t for t, (at, _) in self._acks.items() if now - at > self.ACK_SECONDS]:
                del self._acks[token]
            token = uuid.uuid4().hex
            self._acks[token] = (now, acknowledgements)
        return token

    def handle_ack(self, context, body):
        token = body.get('token') if isinstance(body, dict) else None
        with self._acks_lock:
            held = self._acks.pop(token, None) if isinstance(token, str) else None
        if held is None:
            return {'acknowledged': False}
        with self.serving(context), self.client.files:
            for acknowledgement in held[1]:
                try:
                    acknowledgement()
                except OSError:
                    pass
        return {'acknowledged': True}

    def _pause(self, seconds):
        """A wait between polls (`memory_create --wait`) gives the client to other callers meanwhile."""
        depth = getattr(self._depth, 'count', 0)
        for _ in range(depth):
            self._owner.release()
        try:
            time.sleep(seconds)
        finally:
            for _ in range(depth):
                self._owner.acquire()

    # --- delivery --------------------------------------------------------------------------------------

    def wake(self):
        self._wake.set()

    def _deliver_loop(self):
        while not self._stopping.is_set():
            self._wake.wait(DELIVER_INTERVAL)
            self._wake.clear()
            if self._stopping.is_set():
                return
            # Each spooled hook takes the outbox on its own, so a waiting session hook gets its turn in between.
            try:
                spool = self.ingest_spool()
            except Exception as error:  # noqa: BLE001 - an unreadable spool must not end delivery for good
                spool = {'ingested': 0, 'pending': None, 'stopped': type(error).__name__}
            try:
                with self.client.deadline(DELIVER_SECONDS):
                    result = self.client.flush()
            except Exception as error:  # noqa: BLE001 - the next pass retries; the outbox keeps every entry
                result = {'stopped': type(error).__name__, 'message': str(error)[:200]}
            self._last = {'at': time.time(), 'result': result, 'spool': spool}
            if (result.get('pending') and result.get('stopped') is None) or (
                    spool.get('pending') and spool.get('stopped') is None):
                self._wake.set()  # more queued than one pass carried

    # --- handlers --------------------------------------------------------------------------------------

    def tools(self):
        if self._tools is None:
            from .. import mcp
            self._tools = mcp.server(self.root, service=_SERVICE.get, registry={})
        return self._tools

    def handle_tool(self, context, body):
        import json

        from ..client import ClientService
        if not isinstance(body, dict) or not isinstance(body.get('tool'), str):
            raise KelmoError('E_PROTOCOL', 'edge.tool carries {tool, arguments}.')
        arguments = body.get('arguments') or {}
        if not isinstance(arguments, dict):
            raise KelmoError('E_PROTOCOL', 'Tool arguments must be an object.')
        function = self.tools().get(body['tool'])
        if function is None:
            raise KelmoError('E_INPUT', 'Unknown tool.')
        try:
            inspect.signature(function).bind(**arguments)
        except TypeError as error:
            raise KelmoError('E_INPUT', f'Tool arguments do not match {body["tool"]}: {error}') from error
        token = _SERVICE.set(ClientService(self.client))
        try:
            with self.serving(context):
                return json.loads(json.dumps(function(**arguments), default=str))
        finally:
            _SERVICE.reset(token)

    def handle_hook(self, context, body):
        # SessionStart and prompt hooks bound themselves inside `hook()` (START_DEADLINE_SECONDS); the
        # capture hooks only queue, and get the short bound.
        action = body.get('action') if isinstance(body, dict) else None
        cap = None if action in ('start', 'prompt') else HOOK_SECONDS
        with self.serving(context, cap, urgent=True):
            result, acknowledgements = self._run_hook(body)
        self.wake()
        if acknowledgements:
            # Recorded only once `kelmod hook` has printed the context (edge.ack): a lost reply delivers again.
            result = dict(result, _kelmo_ack=self._hold_acks(acknowledgements))
        return result

    def _run_hook(self, body):
        """Runs one hook on the owned client; returns its result and the deliveries it waits to record."""
        from ..client import hook
        if not isinstance(body, dict) or not isinstance(body.get('action'), str):
            raise KelmoError('E_PROTOCOL', 'edge.hook carries {action, payload, context}.')
        payload = body.get('payload')
        payload = {} if payload is None else payload
        frame = body.get('context') or {}
        if not isinstance(frame, dict):
            raise KelmoError('E_PROTOCOL', 'edge.hook context must be an object.')
        provider = frame.get('provider') if isinstance(frame.get('provider'), str) else None
        if isinstance(payload, dict) and isinstance(frame.get('cwd'), str) and not payload.get('cwd'):
            payload['cwd'] = frame['cwd']
        internal_job = frame.get('internal_job') is True
        acknowledgements = []
        result = hook(self.client, body['action'], payload, provider, flusher=self.wake,
                      acknowledge=acknowledgements.append, internal_job=internal_job)
        return result, acknowledgements

    # --- spool ---------------------------------------------------------------------------------------

    def spool_directory(self):
        return Path(self.client.profile.state) / 'hook-spool'

    def ingest_spool(self):
        """Runs every hook `kelmod hook` spooled while no daemon answered, oldest first, and removes each file
        once its hook ran. A file that is not a hook frame is renamed `*.bad` and kept for inspection; a hook
        that fails for any other reason stays in the spool and the next pass retries it."""
        import json
        from . import spool
        names = spool.entries(self.spool_directory())
        ingested = 0
        # One pass holds the client for about one delivery bound; the rest waits for the next pass.
        stop_at = time.monotonic() + DELIVER_SECONDS
        for index, path in enumerate(names):
            if self._stopping.is_set() or time.monotonic() > stop_at:
                return {'ingested': ingested, 'pending': len(names) - index}
            try:
                body = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(body, dict) or not isinstance(body.get('action'), str):
                    raise ValueError('not a hook frame')
                if body['action'] in ('start', 'prompt'):
                    raise ValueError('context hooks are never replayed')
            except (OSError, ValueError, UnicodeDecodeError):
                spool.reject(path)
                continue
            try:
                with self.client.deadline(SPOOL_HOOK_SECONDS):
                    self._run_hook(body)
            except KelmoError as error:
                # Only a refusal worth retrying (busy, unreachable) keeps the file; any other would repeat for
                # ever and hold back every later capture.
                if not getattr(error, 'retryable', False) and error.code not in ('E_TRANSPORT', 'E_BUSY'):
                    spool.reject(path)
                    continue
                return {'ingested': ingested, 'pending': len(names) - index, 'stopped': error.code}
            except Exception as error:  # noqa: BLE001 - kept in the spool, retried on the next pass
                return {'ingested': ingested, 'pending': len(names) - index, 'stopped': type(error).__name__}
            path.unlink(missing_ok=True)
            ingested += 1
        return {'ingested': ingested, 'pending': 0}

    def handle_cli(self, context, body):
        """One installed-CLI call on this client root, over the core's own SharedClient."""
        from ..cli import run_borrowed_client
        argv = body.get('argv') if isinstance(body, dict) else None
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise KelmoError('E_PROTOCOL', 'edge.cli carries {argv: [strings]}.')
        sources = body.get('sources') or {}
        stdin = body.get('stdin')
        if (not isinstance(sources, dict) or (stdin is not None and not isinstance(stdin, str))
                or not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair)
                           for pair in sources.values())):
            raise KelmoError('E_PROTOCOL', 'edge.cli sources are {name: [content, filename]}, stdin a string.')
        # A CLI command may read and rewrite the outbox in several steps (resend, flush): it holds it throughout.
        with self.owned(context.remaining()), self.serving(context):
            result = run_borrowed_client(self.client, argv, sources, stdin, root=self.root)
        self.wake()
        return result

    def handlers(self):
        return {'edge.tool': (self.handle_tool, 'handler'), 'edge.hook': (self.handle_hook, 'hook'),
                'edge.cli': (self.handle_cli, 'handler'), 'edge.ack': (self.handle_ack, 'hook')}

    def status(self):
        try:
            # Status runs on the frame reader: it never waits for the client, it reports it busy.
            with self.owned(0):
                outbox = self.client.outbox.summary()
        except Exception as error:  # noqa: BLE001 - status reports, never fails
            outbox = {'error': str(error)[:200]}
        return {'authority_id': self.client.profile.authority_id, 'outbox': outbox, 'last_delivery': self._last}

    def trim(self):
        import gc
        gc.collect()

    def close(self):
        self._stopping.set()
        self._wake.set()
        self._deliverer.join(timeout=DELIVER_SECONDS + 1)
        self.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kelmo-edge-core')
    parser.add_argument('--client', required=True, type=Path)
    args = parser.parse_args(argv)
    from .corelink import CoreLink
    core = EdgeCore(args.client)
    link = CoreLink.from_descriptor(handlers=core.handlers(), lanes={'handler': HANDLER_LANE, 'hook': HOOK_LANE},
                                    trim=core.trim, status=core.status)
    try:
        link.serve()
    finally:
        core.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
