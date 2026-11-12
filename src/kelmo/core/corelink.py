"""The Python side of a core's channel to kelmod: descriptor 3, kelmo/2 frames, one reader thread.

The reader thread answers heartbeats and cancels itself and runs no core code, so a busy core still answers
`admin.ping`. Calls go to one thread pool per lane, sized by the core; replies are written under a lock and
matched by id. Calls this core makes to other cores go out with their own ids and come back as futures.

A stop is a drain. On `admin.stop` the channel stays open: the core's owner is asked to end its background
work (`begin_stop`), new calls are refused as retryable, and the reader goes on delivering the replies to the
core's own calls until the running handlers and the owner's work have ended (`wait_stopped`); then the channel
closes. When kelmod closes the channel instead (its router is lost), no reply can come any more: the owner is
asked to stop first, so the failures of its open calls count as the stop's, then those calls fail.

    link = CoreLink.from_descriptor(3, handlers={'state.status': (status, 'read')}, lanes={'read': 4})
    link.serve()          # blocks until the core has drained after admin.stop, or kelmod closed the channel
"""
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from ..errors import KelmoError
from . import wire
from .context import Context

CHANNEL_FD = 3


class DescriptorChannel:
    """The channel as a plain descriptor, for a core inside the Linux sandbox: its seccomp policy refuses every
    socket syscall (`getsockopt` already, which `socket.socket(fileno=...)` makes), while read, write and poll on
    an inherited descriptor stay allowed. Offers the socket methods CoreLink uses; `shutdown` wakes the reader
    through a pipe instead of `shutdown(2)`."""

    def __init__(self, fd):
        self.fd = fd
        os.set_blocking(fd, True)  # sendall's write loop expects a write to wait, not to fail with EAGAIN
        self._wake_read, self._wake_write = os.pipe()
        self._closed = False

    def recv(self, size):
        import select
        poller = select.poll()  # poll, not select: no FD_SETSIZE limit on descriptor numbers
        poller.register(self.fd, select.POLLIN)
        poller.register(self._wake_read, select.POLLIN)
        while True:
            ready = dict(poller.poll())
            if self._wake_read in ready:
                return b''
            if self.fd in ready:
                return os.read(self.fd, size)  # POLLHUP or POLLERR: read returns b'' or raises OSError

    def sendall(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def shutdown(self, _how):
        try:
            os.write(self._wake_write, b'x')
        except OSError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        fds, self.fd = (self.fd, self._wake_read, self._wake_write), -1  # a late write fails with EBADF
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


class CoreLink:
    def __init__(self, sock, handlers, lanes, *, trim=None, status=None, recycle=None, begin_stop=None,
                 wait_stopped=None):
        """`handlers`: op -> (function(context, body) -> body, lane). `lanes`: lane -> thread count.
        `trim`: called on admin.trim to drop caches and unload what can be loaded again. `status`: returns
        the core's own fields for admin.status. `recycle`: called on admin.recycle, returns the names of the
        background work running now; none is the core's consent to be stopped and started again, and the
        owner then starts no new work for a while. Without it a core with no call in flight agrees.
        `begin_stop`: asks the owner's background work to end, without waiting. `wait_stopped`: waits until
        it has ended."""
        self.sock = sock
        self.handlers = handlers
        self.trim = trim
        self.status = status
        self.recycle = recycle
        self.begin_stop = begin_stop
        self.wait_stopped = wait_stopped
        self.pools = {lane: ThreadPoolExecutor(max_workers=size, thread_name_prefix=f'lane-{lane}')
                      for lane, size in lanes.items()}
        self._write_lock = threading.Lock()
        self._open = {}  # call id -> Context, for cancels
        self._outgoing = {}  # call id -> Future
        self._state = threading.Lock()
        self._stopping = threading.Event()
        self._draining = threading.Event()  # admin.stop came: new calls are refused, replies still delivered
        self._owner_stop_lock = threading.Lock()
        self._owner_stop_asked = False
        self.operations = sorted(handlers)

    @classmethod
    def from_descriptor(cls, fd=CHANNEL_FD, *, confined=False, **options):
        """`confined`: the core runs inside the sandbox, where socket syscalls are refused (DescriptorChannel)."""
        return cls(DescriptorChannel(fd) if confined else socket.socket(fileno=fd), **options)

    # --- writing ---------------------------------------------------------------------------------------

    def send(self, header, body=None):
        data = wire.encode(header, body)
        with self._write_lock:
            self.sock.sendall(data)

    def _reply(self, call_id, body=None, error=None):
        try:
            data = wire.encode(*wire.reply(call_id, body, error=error))
        except (TypeError, ValueError, KelmoError) as failure:
            # A result that cannot be encoded still answers, or the caller would wait for its deadline.
            data = wire.encode(*wire.reply(call_id, None, error={
                'code': 'E_PROCESSING', 'retryable': False, 'message': f'The reply could not be encoded: {failure}'}))
        try:
            with self._write_lock:
                self.sock.sendall(data)
        except OSError:
            pass

    # --- outgoing calls --------------------------------------------------------------------------------

    def call(self, op, body, context, *, lane=None, key=None):
        """Calls another core through kelmod. Returns a Future of the reply body; a refusal raises KelmoError
        from `result()` with its code and its `retryable` verdict."""
        call_id = uuid.uuid4().hex
        header = {'v': wire.VERSION, 'kind': 'call', 'id': call_id, 'op': op, 'body_type': 'json',
                  'deadline_ms': max(1, context.remaining_ms())}
        if lane:
            header['lane'] = lane
        if key:
            header['key'] = key
        if context.origin is not None:
            header['origin'] = context.origin.to_header()
        future = Future()
        with self._state:
            self._outgoing[call_id] = future
        try:
            self.send(header, body)
        except (OSError, ValueError, TypeError, KelmoError) as error:
            with self._state:
                self._outgoing.pop(call_id, None)
            future.set_exception(error if isinstance(error, KelmoError)
                                 else KelmoError('E_TRANSPORT', f'The call could not be sent: {error}'))
        return future

    # --- serving ---------------------------------------------------------------------------------------

    # True while the core is still starting: serve() reads replies to the core's own calls but does not say
    # hello, so kelmod sends it no calls until say_hello().
    hold_hello = False

    def say_hello(self):
        self.send({'v': wire.VERSION, 'kind': 'hello', 'id': uuid.uuid4().hex, 'body_type': 'json',
                   'protocols': ['kelmo/2'], 'operations': self.operations})

    def serve(self):
        import faulthandler
        import signal
        # `kill -USR1 <core pid>` writes every thread's stack to the core's stderr (kelmod's log).
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        reader = wire.FrameReader()
        try:
            if not self.hold_hello:
                try:
                    self.say_hello()
                except OSError:
                    return  # kelmod closed the channel first; the stop still runs through _finish
            while not self._stopping.is_set():
                try:
                    data = self.sock.recv(1048576)
                except OSError:
                    break
                if not data:
                    break
                for header, body in reader.feed(data):
                    self._dispatch(header, body)
        finally:
            self._finish()

    def _ask_owner_to_stop(self):
        """`begin_stop`, once."""
        with self._owner_stop_lock:
            if self._owner_stop_asked:
                return
            self._owner_stop_asked = True
        if self.begin_stop is not None:
            try:
                self.begin_stop()
            except Exception as error:  # noqa: BLE001 - the stop goes on
                print(f'kelmo core: asking the core to stop failed ({type(error).__name__})', file=sys.stderr)

    def _wait_for_owner(self):
        if self.wait_stopped is not None:
            try:
                self.wait_stopped()
            except Exception as error:  # noqa: BLE001 - the stop goes on
                print(f'kelmo core: waiting for the core to stop failed ({type(error).__name__})', file=sys.stderr)

    def _drain(self):
        """After admin.stop: the owner stops its work and the running handlers finish while their calls to other
        cores still get replies; then the reader is woken to close the channel."""
        self._ask_owner_to_stop()
        for pool in self.pools.values():
            pool.shutdown(wait=True, cancel_futures=False)
        self._wait_for_owner()
        self._stopping.set()
        try:
            self.sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _finish(self):
        self._stopping.set()
        # The owner hears of the stop before its open calls fail, so it counts those failures as the stop's.
        self._ask_owner_to_stop()
        # Outgoing calls next: no reply can arrive any more, and a handler waiting on one would hold the pool.
        with self._state:
            pending, self._outgoing = list(self._outgoing.values()), {}
        for future in pending:
            if not future.done():
                future.set_exception(KelmoError('E_TRANSPORT', 'The channel to kelmod closed.'))
        for pool in self.pools.values():
            pool.shutdown(wait=True, cancel_futures=False)
        self._wait_for_owner()
        try:
            self.sock.close()
        except OSError:
            pass

    def _dispatch(self, header, body):
        kind = header['kind']
        if kind == 'reply':
            with self._state:
                future = self._outgoing.pop(header['id'], None)
            if future is not None and not future.done():
                if header['ok']:
                    future.set_result(body)
                else:
                    error = KelmoError(header['error']['code'], header['error']['message'])
                    error.retryable = header['error']['retryable']
                    future.set_exception(error)
            return
        if kind == 'cancel':
            with self._state:
                context = self._open.get(header['id'])
            if context is not None:
                context.cancel()
            return
        if kind != 'call':
            return
        op = header['op']
        if op.startswith('admin.'):
            self._admin(op, header)
            return
        if self._draining.is_set():
            self._reply(header['id'], error={'code': 'E_UNAVAILABLE', 'message': 'The core is stopping.',
                                             'retryable': True})
            return
        handler = self.handlers.get(op)
        if handler is None:
            self._reply(header['id'], error={'code': 'E_PROTOCOL', 'message': f'{op} is not served here.',
                                             'retryable': False})
            return
        function, default_lane = handler
        pool = self.pools.get(header.get('lane', default_lane)) or self.pools[default_lane]
        context = Context.from_header(header)
        with self._state:
            self._open[header['id']] = context
        pool.submit(self._run, function, context, body)

    def _run(self, function, context, body):
        try:
            context.check()
            result = function(context, body)
        except KelmoError as error:
            retryable = getattr(error, 'retryable', None)
            if not isinstance(retryable, bool):
                from ..server import RETRYABLE
                retryable = error.code in RETRYABLE
            self._reply(context.request_id, error={'code': error.code, 'message': str(error),
                                                   'retryable': retryable})
        except Exception as error:  # a handler bug must still answer, or the call waits for its deadline
            self._reply(context.request_id, error={'code': 'E_PROCESSING', 'message': type(error).__name__,
                                                   'retryable': False})
        # Stopping (a BaseException) gets no reply on purpose: the core is ending, and kelmod answers the open call
        # when it exits, replaying a read or a keyed write once the core is back.
        else:
            self._reply(context.request_id, result)
        finally:
            with self._state:
                self._open.pop(context.request_id, None)

    def _admin(self, op, header):
        call_id = header['id']
        if op == 'admin.ping':
            self._reply(call_id, {'pid': os.getpid(), 'time': time.time()})
        elif op == 'admin.trim':
            if self.trim is not None:
                threading.Thread(target=self._trim_then_reply, args=(call_id,), daemon=True).start()
            else:
                self._reply(call_id, {'trimmed': False})
        elif op == 'admin.stop':
            self._reply(call_id, {'stopping': True})
            if not self._draining.is_set():
                self._draining.set()
                threading.Thread(target=self._drain, name='corelink-drain', daemon=True).start()
        elif op == 'admin.status':
            # The core's own fields may wait on a lock; the reader thread must stay free for heartbeats.
            threading.Thread(target=self._status_then_reply, args=(call_id,), daemon=True).start()
        elif op == 'admin.recycle':
            threading.Thread(target=self._recycle_then_reply, args=(call_id,), daemon=True).start()
        else:
            self._reply(call_id, error={'code': 'E_PROTOCOL', 'message': f'{op} is not served here.',
                                        'retryable': False})

    def _trim_then_reply(self, call_id):
        try:
            self.trim()
        finally:
            self._reply(call_id, {'trimmed': True})

    def _status_then_reply(self, call_id):
        with self._state:
            open_calls = len(self._open)
        extra = {}
        if self.status is not None:
            try:
                extra = self.status()
            except Exception as error:  # noqa: BLE001 - a status probe must answer
                extra = {'status_error': type(error).__name__}
        self._reply(call_id, {**extra, 'pid': os.getpid(), 'open_calls': open_calls})

    def _recycle_then_reply(self, call_id):
        """Consent to a recycle: only a core with no call open and no background work running agrees."""
        if self._draining.is_set():
            self._reply(call_id, {'recycle': False, 'busy': ['stopping']})
            return
        with self._state:
            busy = ['calls'] if self._open else []
        if not busy and self.recycle is not None:
            try:
                busy = list(self.recycle())
            except Exception as error:  # noqa: BLE001 - a failed probe is a decline, never a restart
                busy = [f'probe failed: {type(error).__name__}']
        self._reply(call_id, {'recycle': not busy, 'busy': busy} if busy else {'recycle': True})
