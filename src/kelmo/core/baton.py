"""The baton: the one lock around the state core's read-write journal connection.

The writer thread and job threads share one connection, so exactly one of them holds the baton at a time.
Interactive writes go first: a job step waits while any interactive writer is waiting. A holder may take
it again (re-entrant). Every release checks that no journal transaction is open, so a transaction never
spans two holders; `released()` lets go for a model call and takes it back afterwards.
"""
import threading
from contextlib import contextmanager, nullcontext


class BatonError(AssertionError):
    """The baton's rules were broken: a transaction left open across a release, or the connection used
    without holding the baton."""


class Baton:
    def __init__(self, in_transaction=lambda: False, rollback=lambda: None):
        self._condition = threading.Condition()
        self._holder = None
        self._depth = 0
        self._interactive_waiting = 0
        self._in_transaction = in_transaction
        self._rollback = rollback

    def held(self):
        """True when the calling thread holds the baton."""
        return self._holder == threading.get_ident()

    def _take(self, interactive):
        me = threading.get_ident()
        with self._condition:
            if self._holder == me:
                self._depth += 1
                return
            if interactive:
                self._interactive_waiting += 1
            try:
                while self._holder is not None or (not interactive and self._interactive_waiting):
                    self._condition.wait()
            finally:
                if interactive:
                    self._interactive_waiting -= 1
            self._holder, self._depth = me, 1

    def _give(self):
        with self._condition:
            if self._holder != threading.get_ident():
                raise BatonError('The baton is released by a thread that does not hold it.')
            self._depth -= 1
            if self._depth:
                return
            # Checked while still held: an open transaction is rolled back here, never handed to the next holder.
            open_transaction = self._in_transaction()
            if open_transaction:
                self._rollback()
            self._holder = None
            self._condition.notify_all()
        if open_transaction:
            raise BatonError('A journal transaction was open when the baton was released; it was rolled back.')

    @contextmanager
    def hold(self, *, interactive=True):
        """Holds the baton for the block. `interactive=False` for a job step, which yields to writers."""
        self._take(interactive)
        try:
            yield
        finally:
            self._give()

    @contextmanager
    def released(self):
        """Lets go of the baton for the block (a model call), fully even if taken several times, and takes
        it back at the same depth. The caller must not be inside a journal transaction."""
        if not self.held():
            yield
            return
        if self._in_transaction():
            raise BatonError('A model call started inside a journal transaction.')
        with self._condition:
            depth, self._depth, self._holder = self._depth, 0, None
            self._condition.notify_all()
        try:
            yield
        finally:
            self._take(interactive=False)
            with self._condition:
                self._depth = depth

    def check(self):
        """Raises unless the calling thread holds the baton (the debug guard on the shared connection)."""
        if not self.held():
            raise BatonError('The shared journal connection was used without holding the baton.')


_installed = None


def install(baton):
    """Makes `baton` the one a model call lets go of (the state core installs its own; None removes it)."""
    global _installed
    _installed = baton


def outside_journal():
    """Around work that never touches the shared journal connection (a model call, a wait for another lock, a
    publication's renders and Git commit): the installed baton is released for it when the calling thread holds
    it, so interactive writes go first meanwhile. The journal guard refuses any journal use inside."""
    return _installed.released() if _installed is not None else nullcontext()


def model_call():
    """Around a model call: the installed baton is released for it when the calling thread holds it."""
    return outside_journal()
