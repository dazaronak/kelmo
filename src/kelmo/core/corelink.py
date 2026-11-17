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
import errno
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor

from ..errors import KelmoError
from . import wire
from .context import Context

CHANNEL_FD = 3


class BrokenFrame(OSError):
    """A write that failed after part of its frame went out: the stream now holds a frame cut short, and no frame
    may follow it on that channel."""

    def __init__(self, cause):
        code = getattr(cause, 'errno', None)
        message = f'The frame was cut short: {getattr(cause, "strerror", None) or cause}'
        if code is None:
            super().__init__(message)
        else:
            super().__init__(code, message)


_SOCKET = socket.socket  # the class itself, taken at import: a sandboxed core may have socket.socket replaced


def write_frame(sock, data):
    """Writes one whole frame. A socket's `sendall` does not say how much it wrote before an error, so a socket is
    written with `send`; a write that fails after part of the frame went out raises BrokenFrame. Any other channel
    object writes through its own `sendall` (DescriptorChannel raises BrokenFrame itself)."""
    if not isinstance(sock, _SOCKET):
        sock.sendall(data)
        return
    view, sent = memoryview(data), 0
    while sent < len(view):
        try:
            sent += sock.send(view[sent:])
        except OSError as error:
            if sent:
                raise BrokenFrame(error) from error
            raise


class DescriptorChannel:
    """The channel as a plain descriptor, for a core inside the Linux sandbox: its seccomp policy refuses every
    socket syscall (`getsockopt` already, which `socket.socket(fileno=...)` makes), while read, write and poll on
    an inherited descriptor stay allowed. Offers the socket methods CoreLink uses; `shutdown` of the read side wakes
    the reader through a pipe instead of `shutdown(2)`. A write shutdown cannot half-close the descriptor, so it
    wakes the reader CONFINED_DRAIN_SECONDS later: replies already on their way are read meanwhile, then the channel
    ends and is closed, and kelmod sees the end of the stream."""

    def __init__(self, fd):
        self.fd = fd
        os.set_blocking(fd, True)  # sendall's write loop expects a write to wait, not to fail with EAGAIN
        self._wake_read, self._wake_write = os.pipe()
        self._closed = False
        self._write_ended = False  # a write shutdown: replies on their way are read before the wake ends it

    def recv(self, size):
        import select
        poller = select.poll()  # poll, not select: no FD_SETSIZE limit on descriptor numbers
        poller.register(self.fd, select.POLLIN)
        poller.register(self._wake_read, select.POLLIN)
        while True:
            ready = dict(poller.poll())
            if self._wake_read in ready:
                if self._write_ended and self.fd in ready:
                    return os.read(self.fd, size)  # a reply that came with the drain's end is still read
                return b''
            if self.fd in ready:
                return os.read(self.fd, size)  # POLLHUP or POLLERR: read returns b'' or raises OSError

    def sendall(self, data):
        view, sent = memoryview(data), 0
        while sent < len(view):
            try:
                sent += os.write(self.fd, view[sent:])
            except OSError as error:
                if sent:
                    raise BrokenFrame(error) from error
                raise

    def shutdown(self, how):
        if how == socket.SHUT_WR:
            # No half-close without shutdown(2): writes already stop on `_peer_gone`; kelmod's reader would wait for
            # the rest of a cut frame for good, so the whole channel ends after the drain.
            self._write_ended = True
            timer = threading.Timer(CONFINED_DRAIN_SECONDS, self._wake)
            timer.daemon = True
            try:
                timer.start()
            except RuntimeError:  # no thread to be had: the channel ends now rather than never
                self._wake()
            return
        self._wake()

    def _wake(self):
        try:
            os.write(self._wake_write, b'x')
        except OSError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        fds = (self.fd, self._wake_read, self._wake_write)
        self.fd = self._wake_write = -1  # a late write or wake fails with EBADF, never into a reused number
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


# How long a confined channel ended for writing still reads replies on their way before it ends whole.
CONFINED_DRAIN_SECONDS = 1.0
# How long the stop waits for a write in progress before it closes the channel anyway.
CLOSE_WAIT_SECONDS = 5.0

# A write that fails with one of these means kelmod's end of the channel is gone; any other error (no buffer space)
# fails that call alone.
PEER_GONE = frozenset({errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN, errno.EBADF, errno.ESHUTDOWN})
# How long the stop waits for a call already writing before it sets the stop flag: kelmod closed the channel, so
# that write ends at once (it lands or fails); the bound is for a peer that keeps reading slowly.
STOP_WRITE_WAIT_SECONDS = 5.0
# How long a call whose write found kelmod gone waits for the reader to reach the channel's end before it asks
# the owner to stop itself: replies kelmod sent before closing reach their calls first.
GONE_READER_WAIT_SECONDS = 1.0


def _fail(future, error):
    """Fails `future` unless the stop already failed it (the reader reached the channel's end first)."""
    try:
        future.set_exception(error)
    except InvalidStateError:
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
        self._write_wait_ran_out = False  # a stop step found a write stuck past its bound; later steps do not wait
        self._open = {}  # call id -> Context, for cancels
        self._outgoing = {}  # call id -> Future
        self._state = threading.Lock()
        self._stopping = threading.Event()
        self._peer_gone = threading.Event()  # a write found kelmod's end gone: no later call is written or registered
        self._draining = threading.Event()  # admin.stop came: new calls are refused, replies still delivered
        self._owner_stop_lock = threading.RLock()  # held while begin_stop runs: no caller passes it before then
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
            self._write_locked(data)

    def _write_locked(self, data):
        """Writes one frame; the caller holds `_write_lock`. A frame cut short (BrokenFrame) or a gone peer ends the
        channel for writing: nothing is written after it, and kelmod sees the channel end instead of a frame that
        goes on."""
        if self._peer_gone.is_set():
            raise OSError(errno.EPIPE, 'The channel to kelmod ended for writing.')
        try:
            write_frame(self.sock, data)
        except OSError as error:
            if isinstance(error, BrokenFrame) or error.errno in PEER_GONE:
                self._peer_gone.set()  # under the write lock: no write after this one
            if isinstance(error, BrokenFrame):
                try:
                    self.sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            raise

    def _reply(self, call_id, body=None, error=None):
        try:
            data = wire.encode(*wire.reply(call_id, body, error=error))
        except (TypeError, ValueError, KelmoError) as failure:
            # A result that cannot be encoded still answers, or the caller would wait for its deadline.
            data = wire.encode(*wire.reply(call_id, None, error={
                'code': 'E_PROCESSING', 'retryable': False, 'message': f'The reply could not be encoded: {failure}'}))
        try:
            with self._write_lock:
                self._write_locked(data)
        except OSError:
            pass

    # --- outgoing calls --------------------------------------------------------------------------------

    def call(self, op, body, context, *, lane=None, key=None):
        """Calls another core through kelmod. Returns a Future of the reply body; a refusal raises KelmoError
        from `result()` with its code and its `retryable` verdict, and a call that could not be sent or answered
        (the channel to kelmod closing or gone) fails with E_TRANSPORT."""
        if self._stopping.is_set() or self._peer_gone.is_set():
            # A call made once the link stops fails at once, whatever its body, and never queues behind an
            # earlier write that kelmod may no longer read.
            return self._refuse_stopped(Future())
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
            data = wire.encode(header, body)
        except Exception as error:  # noqa: BLE001 - nothing was written: the call alone fails, whatever the encoder raised
            with self._state:
                self._outgoing.pop(call_id, None)
            _fail(future, error if isinstance(error, KelmoError)
                  else KelmoError('E_TRANSPORT', f'The call could not be sent: {error}'))
            return future
        try:
            with self._write_lock:
                # Once the link stops, `_finish` has failed (or is about to fail) every call it knows, this one among
                # them if it was registered before; a call written now would wait for a reply that cannot come.
                refused = self._stopping.is_set() or self._peer_gone.is_set()
                if not refused:
                    self._write_locked(data)
        except Exception as error:  # noqa: BLE001 - a failed write fails this call; only a gone peer stops the core
            with self._state:
                self._outgoing.pop(call_id, None)
            if isinstance(error, BrokenFrame) or (isinstance(error, OSError) and error.errno in PEER_GONE):
                # The channel to kelmod is gone, or now carries a frame cut short, though the reader may not have
                # reached its end yet: the reader goes on to the channel's end, so replies kelmod sent before
                # closing still arrive, and the core hears of the stop from it (or after a short bound) before this
                # call fails, as it does for the calls the reader fails.
                self._leave_stop_to_reader()
            _fail(future, KelmoError('E_TRANSPORT', f'The call could not be sent: {error}'))
            return future
        if refused:
            with self._state:
                self._outgoing.pop(call_id, None)
            self._refuse_stopped(future)
        return future

    def _leave_stop_to_reader(self):
        """kelmod is gone for writing: wait briefly for the reader to deliver what kelmod sent before closing and
        reach the channel's end (`_finish` asks the owner to stop), then ask the owner, once, in any case."""
        if not self._stopping.is_set():
            self._stopping.wait(GONE_READER_WAIT_SECONDS)
        self._ask_owner_to_stop()

    def _refuse_stopped(self, future):
        self._leave_stop_to_reader()  # the owner hears of the stop before the call fails
        _fail(future, KelmoError('E_TRANSPORT', 'The channel to kelmod is closing; the call was not sent.'))
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
        """`begin_stop`, once. A caller on another thread that comes while it runs returns only after it has run,
        so its own failure is counted as the stop's too; `begin_stop` does not wait, so neither does that caller for
        long. A call `begin_stop` itself made would return at once (the lock is reentrant)."""
        failure = None
        with self._owner_stop_lock:
            if self._owner_stop_asked:
                return
            self._owner_stop_asked = True
            if self.begin_stop is not None:
                try:
                    self.begin_stop()
                except Exception as error:  # noqa: BLE001 - the stop goes on
                    failure = error
        if failure is not None:  # reported outside the lock: a stuck stderr holds no other caller
            try:
                print(f'kelmo core: asking the core to stop failed ({type(failure).__name__})', file=sys.stderr)
            except (OSError, ValueError):
                pass

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
        self._set_stopping()
        try:
            self.sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _set_stopping(self):
        """Sets the stop flag under the write lock, waiting for it at most STOP_WRITE_WAIT_SECONDS: a call that passed
        its stop check finishes its write first, and every call after it sees the stop. Only a write stuck past
        that bound (kelmod not reading) can end after the flag is up; its future fails all the same, failed by that
        call itself or by `_finish`."""
        taken = self._take_write_lock_for_stop(STOP_WRITE_WAIT_SECONDS)
        try:
            self._stopping.set()
        finally:
            if taken:
                self._write_lock.release()

    def _take_write_lock_for_stop(self, seconds):
        """Takes the write lock for a step of the stop, waiting at most `seconds`. Once one step has waited its bound
        out, the write in progress then is stuck (kelmod not reading), and the stop's later steps only try the lock:
        one stuck write is waited for once, not once per step."""
        if self._write_wait_ran_out:
            return self._write_lock.acquire(blocking=False)
        taken = self._write_lock.acquire(timeout=seconds)
        if not taken:
            self._write_wait_ran_out = True
        return taken

    def _finish(self):
        self._set_stopping()
        # The owner hears of the stop before its open calls fail, so it counts those failures as the stop's.
        self._ask_owner_to_stop()
        # Outgoing calls next: no reply can arrive any more, and a handler waiting on one would hold the pool.
        with self._state:
            pending, self._outgoing = list(self._outgoing.values()), {}
        for future in pending:
            # A call thread may fail its own future meanwhile; the stop goes on.
            _fail(future, KelmoError('E_TRANSPORT', 'The channel to kelmod closed.'))
        for pool in self.pools.values():
            pool.shutdown(wait=True, cancel_futures=False)
        self._wait_for_owner()
        # Closed only once no write is on the descriptor: a number closed under a write could be reused by another
        # open before that write runs. A write still in progress at the bound (kelmod not reading) is cut short by a
        # shutdown on a socket, which keeps the number; a confined channel has no shutdown(2), so its write goes on
        # until kelmod reads or goes. Either way the descriptor is closed by a thread of its own once that write lets
        # go, and this step does not wait for it.
        if not self._take_write_lock_for_stop(CLOSE_WAIT_SECONDS):
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                threading.Thread(target=self._close_when_free, name='kelmo-corelink-close', daemon=True).start()
            except RuntimeError:  # no thread to be had: the descriptor stays open for the process's exit
                pass
            return
        try:
            self.sock.close()
        except OSError:
            pass
        finally:
            self._write_lock.release()

    def _close_when_free(self):
        with self._write_lock:
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
