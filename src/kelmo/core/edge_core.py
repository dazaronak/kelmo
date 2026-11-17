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
import collections
import inspect
import json
import math
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
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
# An old outbox is rewritten once (to give pages back in steps from then on) when its predicted rewrite fits this
# bound, on the first pass with nothing pending: a waiter at the outbox waits at most about this long, once.
COMPACT_MAX_SECONDS = 1.0
# A rewrite's cost per MB of live data, the whole rewrite: the upper end measured (9 to 12 ms).
CONVERSION_SECONDS_PER_MB = 0.012
# Where the rewrite is stopped and rolled back when the machine is slower than predicted: about a quarter of the
# rewrite is its copy phase, the only part that can be stopped.
COMPACT_COPY_SECONDS = 0.25
# An old outbox too large for the bound (or whose bounded rewrite was stopped) is rewritten once at idle: no call
# served for this long. It is looked at again the first moment the daemon can have been idle that long.
IDLE_SECONDS = 600.0
# How soon a pass compacts again after it found the outbox held or gave way to a waiter, and after a failure.
BUSY_RETRY_SECONDS = 1.0
COMPACT_RETRY_SECONDS = 3600.0
# A rewrite refused by another process reading the file had copied it first: the next try waits this many times as
# long as that try took (at least BUSY_RETRY_SECONDS), so a reader that stays costs the outbox about 1% of the time.
BUSY_REWRITE_PAUSE_FACTOR = 100.0
# Bound of a hook that is not a SessionStart or prompt hook (those carry their own, shorter deadline).
HOOK_SECONDS = 3.0
# How long `kelmo daemon status` waits for another thread inside the outbox.
STATUS_OUTBOX_SECONDS = 0.5
# Bound of one spooled hook replayed by the deliverer.
SPOOL_HOOK_SECONDS = 10.0

_SERVICE = ContextVar('kelmo_edge_service')


class _RewriteRefused(Exception):
    """A rewrite another connection's lock refused, after `seconds` of work (its copy phase, when a reader held the
    file)."""

    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


class _GaveWay(Exception):
    """An idle rewrite stopped because a caller came for the outbox while it copied."""


class OwnerLock:
    """A reentrant lock shared by urgent and ordinary callers, which take turns while both wait: an urgent
    caller goes first, but never twice in a row past a waiting ordinary one. A hook is urgent (a session waits
    on it); the tool calls of busy sessions would otherwise take the client ahead of it again and again, and a
    stream of hooks must not shut the tools out either. Callers of one kind are served in the order they came: a
    newcomer queues behind a waiter that was woken but has not taken the lock yet."""

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._holder = None
        self._count = 0
        self._waiting = {True: collections.deque(), False: collections.deque()}
        self._ends = {}
        self._last_urgent = False

    def _live(self, urgent, now, me):
        """The waiters of one kind that can still take the lock, the asking caller always among them: another
        whose timeout passed stays queued until its thread gets the condition back, and holds nobody up meanwhile."""
        return [ticket for ticket in self._waiting[urgent]
                if ticket is me or self._ends.get(ticket) is None or self._ends[ticket] > now]

    def _turn(self, urgent, ticket):
        if self._holder is not None:
            return False
        now = time.monotonic()
        mine = self._live(urgent, now, ticket)
        if mine[0] is not ticket:
            return False
        return not self._live(not urgent, now, ticket) or (urgent != self._last_urgent)

    def acquire(self, timeout=None, urgent=False):
        me = threading.get_ident()
        end = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if self._holder == me:
                self._count += 1
                return True
            ticket = object()
            self._waiting[urgent].append(ticket)
            self._ends[ticket] = end
            try:
                while not self._turn(urgent, ticket):
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._condition.wait(left)
                    # Woken after its time ran out: it gave up, and the turn is for those still within theirs.
                    if end is not None and time.monotonic() >= end:
                        return False
                self._holder, self._count, self._last_urgent = me, 1, urgent
                return True
            finally:
                self._waiting[urgent].remove(ticket)
                self._ends.pop(ticket, None)
                # A caller that gave up may have been the one whose turn it was.
                self._condition.notify_all()

    def acquire_if_idle(self):
        """Takes the lock only when nobody holds it and nobody waits for it, without joining the waiters or changing
        whose turn is next: a reader that must never stand in a caller's way. True when taken."""
        with self._condition:
            if self._holder is not None or any(self._waiting.values()):
                return False
            self._holder, self._count = threading.get_ident(), 1
            return True

    def waited_for(self):
        """True while any caller waits for the lock; read without the condition (a background job's peek)."""
        return any(self._waiting.values())

    def held(self):
        """How many times the calling thread holds the lock: 0 when another thread or nobody does."""
        with self._condition:
            return self._count if self._holder == threading.get_ident() else 0

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
        # and the deliverer alike. A read over a channel that carries several requests does not take it.
        self._owner = OwnerLock()
        self._urgent = threading.local()
        self.client._lock = _OwnerGate(self)
        self._acks = {}
        self._acks_lock = threading.Lock()
        self.client.pause = self._pause
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._last = {'at': None, 'result': None}
        self._last_call = time.monotonic()
        self.client.compactor = self  # `kelmo client flush` leaves the compaction to the deliverer
        self._compaction = self._compaction_load()
        self._saved = bool(self._compaction)
        # A bounded rewrite of this old file was stopped for being slow: it waits for idle from then on.
        self._over_limit = self._compaction.get('over_limit') is True
        # Pauses are timed on the monotonic clock in this process, so a wall clock set back does not stretch them;
        # `retry_at` (wall clock) is what survives a restart.
        self._compact_after = time.monotonic() + max(0.0, self._compaction.get('retry_at', 0.0) - time.time())
        self._busy_after = 0.0
        self._wake.set()  # the first pass ingests what was spooled while no daemon ran
        self._deliverer = threading.Thread(target=self._deliver_loop, name='deliverer', daemon=True)
        self._deliverer.start()

    @contextmanager
    def owned(self, seconds, urgent=False):
        """Holds the outbox for one caller across a whole section, waiting at most `seconds` for it."""
        from ..client import Busy
        if not self._owner.acquire(timeout=max(0.0, seconds), urgent=urgent):
            raise Busy('The edge core is busy with another write; retry.')
        try:
            yield self.client
        finally:
            # A pause that could not take the outbox back left this thread holding nothing.
            if self._owner.held():
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
        self._last_call = time.monotonic()
        try:
            with self.client.deadline(max(0.001, left)):
                yield self.client
        finally:
            self._urgent.value = previous
            self._last_call = time.monotonic()

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
        """A wait between polls (`kelmo create --wait`) gives the outbox to other callers meanwhile and takes it
        back before the call's deadline, or fails as busy. A tool call holds nothing across its wait, so it lends
        nothing and takes nothing back."""
        from ..client import Busy
        depth = self._owner.held()
        if not depth:
            time.sleep(seconds)
            return
        for _ in range(depth):
            self._owner.release()
        try:
            time.sleep(max(0.0, min(seconds, self._left())))
        finally:
            left = self._left()
            # With nothing left, even a free outbox is not taken: the next poll would run past the deadline.
            if left <= 0 or not self._owner.acquire(timeout=left, urgent=getattr(self._urgent, 'value', False)):
                raise Busy('The edge core stayed busy past this call\'s deadline while it waited between polls.')
            if self._left() <= 0:
                # Taken back after the deadline (the thread was held up before it asked): give it back.
                self._owner.release()
                raise Busy('The edge core gave the outbox back too late for this call\'s deadline.')
            for _ in range(depth - 1):
                self._owner.acquire()

    def _left(self):
        """Seconds left before the nearest deadline of this thread's call (the request's, or a wait's inside it),
        not capped by the profile timeout as `remaining()` is."""
        left = self.client.time_left()
        if left is not None:
            return left
        try:  # no deadline on this thread: the profile timeout bounds it
            return self.client.remaining()
        except KelmoError:
            return 0.0

    # --- delivery --------------------------------------------------------------------------------------

    def wake(self):
        self._wake.set()

    def _deliver_loop(self):
        while not self._stopping.is_set():
            interval = DELIVER_INTERVAL
            now = time.monotonic()
            for after in (self._compact_after, self._busy_after):
                if 0 < after - now < interval:
                    interval = after - now  # a pause of a second comes back without being woken
            self._wake.wait(interval)
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
                    # An outbox whose pending count another caller keeps from this pass ends it with E_BUSY: that
                    # caller delivers. Once counted, the pass waits for the outbox up to DELIVER_SECONDS.
                    result = self.client.flush(wait_for_holder=False)
            except Exception as error:  # noqa: BLE001 - the next pass retries; the outbox keeps every entry
                result = {'stopped': type(error).__name__, 'message': str(error)[:200]}
            # An outbox another caller held is looked at again a second later, woken or not.
            self._busy_after = time.monotonic() + BUSY_RETRY_SECONDS if result.get('stopped') == 'E_BUSY' else 0.0
            if result.get('pending') == 0 and time.monotonic() >= self._compact_after:
                result = dict(result, compaction=self._compact())
            self._last = {'at': time.time(), 'result': result, 'spool': spool}
            if (result.get('pending') and result.get('stopped') is None) or (
                    spool.get('pending') and spool.get('stopped') is None):
                self._wake.set()  # more queued than one pass carried

    # --- compaction ------------------------------------------------------------------------------------

    def _compact(self):
        """Gives the outbox's free pages back after a pass that left nothing pending. An old file is rewritten once
        (when its predicted rewrite fits COMPACT_MAX_SECONDS, or at idle when it does not, stopping as soon as a
        caller waits for the outbox); a converted one is drained in steps of COMPACT_CHUNK_PAGES, each taken only
        while nobody holds or waits for the outbox, so a waiter waits at most one step. Records and returns the
        outcome."""
        from ..client import COMPACT_CHUNK_PAGES, COMPACT_TRY_SECONDS, _compaction_due
        freed, now = 0, time.time()
        try:
            outbox = self.client.outbox
            while True:
                if self._stopping.is_set() or (freed and self._wake.is_set()):
                    return self._compaction_done('paused', freed, BUSY_RETRY_SECONDS)  # a hook queued: deliver first
                step = self._compaction_step(outbox, COMPACT_TRY_SECONDS, COMPACT_CHUNK_PAGES, _compaction_due,
                                             draining=bool(freed))
                if step is None:
                    return self._compaction_done('busy', freed, BUSY_RETRY_SECONDS)
                state, given = step
                freed += given
                if state == 'too_large':
                    return self._compaction_done(state, freed, self._until_idle())
                if state != 'more':
                    return self._compaction_done(state, freed, COMPACT_RETRY_SECONDS if state == 'stuck' else 0.0)
        except _RewriteRefused as refused:
            return self._compaction_done('busy', freed, min(COMPACT_RETRY_SECONDS, max(
                BUSY_RETRY_SECONDS, BUSY_REWRITE_PAUSE_FACTOR * refused.seconds)))
        except _GaveWay:
            if self._stopping.is_set():
                return self._compaction_done('paused', freed, 0.0)  # rolled back for the stop: no pause to keep
            return self._compaction_done('paused', freed, BUSY_RETRY_SECONDS)  # rolled back; the next idle time
        except sqlite3.OperationalError as error:
            from ..errors import sqlite_busy
            if self._stopping.is_set():
                return self._compaction_done('paused', freed, 0.0)  # the outbox closed under it: no pause to keep
            if sqlite_busy(error):
                return self._compaction_done('busy', freed, BUSY_RETRY_SECONDS)
            if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                # Slower than predicted: rolled back, the file stays as it was. From then on it waits for idle, like
                # a file too large to rewrite while calls come, where the rewrite has no time bound.
                self._over_limit = True
                return self._compaction_done('too_large', freed, self._until_idle())
            return self._compaction_done('error', freed, COMPACT_RETRY_SECONDS, error)
        except Exception as error:  # noqa: BLE001 - an outbox that cannot be compacted still delivers
            if self._stopping.is_set():
                return self._compaction_done('paused', freed, 0.0)
            return self._compaction_done('error', freed, COMPACT_RETRY_SECONDS, error)
        finally:
            self._compaction['at'] = now

    def _compaction_step(self, outbox, try_seconds, chunk, due, *, draining=False):
        """One statement under the outbox, taken only while idle: ('more', bytes) after a drain step that left free
        pages, ('done' | 'nothing' | 'too_large' | 'stuck', bytes) at the end, None when the outbox was held. A
        drain once begun (`draining`) goes on to an empty freelist."""
        if not self._owner.acquire_if_idle():
            return None
        try:
            if not outbox._serial.acquire(timeout=try_seconds):
                return None
            try:
                # Every statement here, reads included, waits for another connection no longer than the try.
                with outbox.waiting(try_seconds):
                    if outbox.pending_count():  # a hook queued since the flush: deliver first
                        return 'nothing', 0
                    pages, free, size, mode = outbox.compaction_state()
                    if mode != 2:
                        live_mb = (pages - free) * size / 1e6
                        fits = live_mb * CONVERSION_SECONDS_PER_MB <= COMPACT_MAX_SECONDS and not self._over_limit
                        idle = time.monotonic() - self._last_call >= IDLE_SECONDS
                        if not fits and not idle:
                            return 'too_large', 0
                        started, gave_way = time.monotonic(), []

                        def a_caller_waits():  # the idle rewrite has no time bound: it gives way to any caller
                            if self._owner.waited_for() or self._stopping.is_set():  # and to a stop
                                gave_way.append(True)
                            return bool(gave_way)

                        try:
                            given = outbox.convert(copy_seconds=COMPACT_COPY_SECONDS if fits else None,
                                                   stop_when=None if fits else a_caller_waits)
                        except sqlite3.OperationalError as error:
                            from ..errors import sqlite_busy
                            if gave_way:
                                raise _GaveWay() from error
                            if sqlite_busy(error):
                                raise _RewriteRefused(time.monotonic() - started) from error
                            raise
                        self._over_limit = False
                        return 'done', given
                    self._over_limit = False  # converted (by a client on its own meanwhile): no rewrite is owed
                    if not free:
                        return ('done' if draining else 'nothing'), 0
                    if not draining and free < chunk and not due(pages, free, size):
                        return 'nothing', 0
                    given, left = outbox.vacuum_step(chunk)
                    if not given:
                        return 'stuck', 0
                    return ('more' if left else 'done'), given * size
            finally:
                outbox._serial.release()
        finally:
            self._owner.release()

    def _until_idle(self):
        """The pause of a rewrite that waits for idle: looked at again the first moment the daemon can have been
        idle long enough, an hour at most."""
        quiet = time.monotonic() - self._last_call
        return min(COMPACT_RETRY_SECONDS, max(0.0, IDLE_SECONDS - quiet))

    def _compaction_done(self, state, freed, pause, error=None):
        record = {'state': state, 'freed': freed}
        if self._over_limit:
            record['over_limit'] = True
        if error is not None:
            record['error'] = f'{type(error).__name__}: {str(error)[:200]}'
        if pause:
            record['retry_at'] = time.time() + pause
        self._compact_after = time.monotonic() + pause
        self._compaction = record
        if state in ('too_large', 'stuck', 'error') or self._over_limit or pause > BUSY_RETRY_SECONDS:
            # A long pause (an hour, a reader's refusal) and a stopped rewrite survive the core's recycles.
            self._compaction_save(record)
        elif self._saved:
            self._compaction_forget()  # done, or nothing to keep: a restart must not wait for the old pause
        return record

    def _compaction_path(self):
        return Path(self.client.profile.state) / 'compaction.json'

    def _compaction_load(self):
        try:
            record = json.loads(self._compaction_path().read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(record, dict):
            return {}
        retry = record.get('retry_at', 0.0)
        if isinstance(retry, bool) or not isinstance(retry, (int, float)):
            return {}
        latest = time.time() + COMPACT_RETRY_SECONDS
        try:
            finite = math.isfinite(retry)
        except OverflowError:  # an integer too large for a float
            finite = False
        if not finite or retry > latest:
            record = dict(record, retry_at=latest)  # saved under a wall clock since set back: an hour at most
        return record

    def _compaction_save(self, record):
        from ..client import _atomic_write
        try:
            _atomic_write(self._compaction_path(), json.dumps(record).encode())  # 0600, like the outbox's other files
        except OSError:
            return  # the pause then lasts this process only
        self._saved = True

    def _compaction_forget(self):
        try:
            self._compaction_path().unlink(missing_ok=True)
        except OSError:
            return
        self._saved = False

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
        from ..cli import borrowed_read_only, run_borrowed_client
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
        # A status command only reads it, and holding it across its wait for the server would keep a hook out.
        held = nullcontext() if borrowed_read_only(argv) else self.owned(context.remaining())
        with held, self.serving(context):
            result = run_borrowed_client(self.client, argv, sources, stdin, root=self.root)
        self.wake()
        return result

    def handlers(self):
        return {'edge.tool': (self.handle_tool, 'handler'), 'edge.hook': (self.handle_hook, 'hook'),
                'edge.cli': (self.handle_cli, 'handler'), 'edge.ack': (self.handle_ack, 'hook')}

    def status(self):
        # admin.status runs on a thread of its own and must answer at once: it reads the outbox only while nobody
        # holds or waits for it, so it never waits for the client and never goes ahead of a queued hook; otherwise
        # it reports the outbox busy.
        if not self._owner.acquire_if_idle():
            outbox = {'busy': True}
        else:
            try:
                # A status command may be inside the outbox past another process's lock: wait for it briefly.
                outbox = self.client.outbox.summary_within(STATUS_OUTBOX_SECONDS)
            except Exception as error:  # noqa: BLE001 - status reports, never fails
                outbox = {'error': str(error)[:200]}
            finally:
                self._owner.release()
        return {'authority_id': self.client.profile.authority_id, 'outbox': outbox, 'last_delivery': self._last,
                'compaction': dict(self._compaction)}

    def trim(self):
        import gc
        gc.collect()

    def close(self):
        from ..client import Busy
        self._stopping.set()
        self._wake.set()
        self._deliverer.join(timeout=DELIVER_SECONDS + 1)
        try:
            with self.client.deadline(2.0):
                self.client.close()
        except (Busy, KelmoError) as error:  # a stop never fails on the outbox; the next start opens it again
            print(f'kelmo edge core: the client did not close cleanly ({error})', file=sys.stderr, flush=True)


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
