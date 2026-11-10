"""The Python side of a core's channel to kelmod: descriptor 3, kelmo/2 frames, one reader thread.

The reader thread answers heartbeats and cancels itself, so a busy core still answers `admin.ping`. Calls
go to one thread pool per lane, sized by the core; replies are written under a lock and matched by id.
Calls this core makes to other cores go out with their own ids and come back as futures.

    link = CoreLink.from_descriptor(3, handlers={'state.status': (status, 'read')}, lanes={'read': 4})
    link.serve()          # blocks until kelmod closes the channel or sends admin.stop
"""
import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from ..errors import KelmoError
from . import wire
from .context import Context

CHANNEL_FD = 3


class CoreLink:
    def __init__(self, sock, handlers, lanes, *, trim=None, status=None):
        """`handlers`: op -> (function(context, body) -> body, lane). `lanes`: lane -> thread count.
        `trim`: called on admin.trim to drop caches and unload what can be loaded again. `status`: returns
        the core's own fields for admin.status."""
        self.sock = sock
        self.handlers = handlers
        self.trim = trim
        self.status = status
        self.pools = {lane: ThreadPoolExecutor(max_workers=size, thread_name_prefix=f'lane-{lane}')
                      for lane, size in lanes.items()}
        self._write_lock = threading.Lock()
        self._open = {}  # call id -> Context, for cancels
        self._outgoing = {}  # call id -> Future
        self._state = threading.Lock()
        self._stopping = threading.Event()
        self.operations = sorted(handlers)

    @classmethod
    def from_descriptor(cls, fd=CHANNEL_FD, **options):
        return cls(socket.socket(fileno=fd), **options)

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
        except OSError as error:
            with self._state:
                self._outgoing.pop(call_id, None)
            future.set_exception(KelmoError('E_TRANSPORT', f'The channel to kelmod failed: {error}'))
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
        if not self.hold_hello:
            self.say_hello()
        reader = wire.FrameReader()
        try:
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

    def _finish(self):
        self._stopping.set()
        for pool in self.pools.values():
            pool.shutdown(wait=True, cancel_futures=False)
        with self._state:
            pending, self._outgoing = list(self._outgoing.values()), {}
        for future in pending:
            if not future.done():
                future.set_exception(KelmoError('E_TRANSPORT', 'The channel to kelmod closed.'))
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
            self._stopping.set()
            try:
                self.sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        elif op == 'admin.status':
            with self._state:
                open_calls = len(self._open)
            extra = {}
            if self.status is not None:
                try:
                    extra = self.status()
                except Exception as error:  # noqa: BLE001 - a status probe must answer
                    extra = {'status_error': type(error).__name__}
            self._reply(call_id, {**extra, 'pid': os.getpid(), 'open_calls': open_calls})
        else:
            self._reply(call_id, error={'code': 'E_PROTOCOL', 'message': f'{op} is not served here.',
                                        'retryable': False})

    def _trim_then_reply(self, call_id):
        try:
            self.trim()
        finally:
            self._reply(call_id, {'trimmed': True})
