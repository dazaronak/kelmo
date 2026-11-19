"""Stable errors shared by the CLI and service."""
import sqlite3
import time

# The sleeps of SQLite's own busy handler, in seconds; after the last one, the last again.
BUSY_PAUSES = (0.001, 0.002, 0.005, 0.01, 0.015, 0.02, 0.025, 0.025, 0.025, 0.05, 0.05, 0.1)


def sqlite_busy(error):
    """Whether a SQLite failure is contention (busy or locked), which passes, rather than a fault."""
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return isinstance(error, sqlite3.OperationalError) and ('locked' in str(error) or 'busy' in str(error))


def _passes(error):
    """Whether a lock error goes away once another connection lets go: SQLITE_BUSY does. SQLITE_LOCKED (a conflict
    inside this connection) and a commit refused while this connection's own statements run do not, so waiting for
    them would only spend the deadline; SQLite's busy handler is not called for them either."""
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff == sqlite3.SQLITE_BUSY and 'statements in progress' not in str(error)
    return sqlite_busy(error)


def retry_busy(step, until, *, sleep=time.sleep):
    """`step()`, run again after each lock it meets until the monotonic time `until`, then the last lock error.

    For a connection whose busy timeout is 0. SQLite's own timeout adds up the sleeps it asks for, not the time
    that passed: where timers are coalesced (a background process on macOS, a loaded VM) every short sleep runs
    long, and a 1 s timeout waited more than 2 s. Here the clock decides: the wait ends at the first look at it
    after `until`, one pause past it however long the system lets that pause run. `step` must be safe to run
    again after a lock: a read, a whole transaction that rolls back when it fails, or a COMMIT, which a lock
    leaves open with the writer still holding its claim on the file."""
    tries = 0
    while True:
        try:
            return step()
        except sqlite3.OperationalError as error:
            if not _passes(error):
                raise
            left = until - time.monotonic()
            if left <= 0:
                raise
            sleep(min(BUSY_PAUSES[min(tries, len(BUSY_PAUSES) - 1)], left))
            tries += 1


class KelmoError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class Stopping(BaseException):
    """The worker was asked to stop before its next model call.

    A BaseException, so no failure handler records it: the attempt in flight releases its claim and
    consulting lease on the way out and resumes on the next start, without a charged attempt.
    """


class VaultFailure(KelmoError):
    """A condition of the shared vault under which no note can be published until a person repairs it.

    A prepared publication the recovery cannot finish (every later publication waits behind it), a commit that
    cannot be written, uncommitted edits on the paths every publication rewrites, a working registry that
    differs from the committed generation, a wiki that is over its budget or cannot be rendered as it stands.
    It refuses no note (the alert names the note whose turn met it, never as the cause): every note it meets
    keeps its request key, its request row and its place, spends no attempt, and the apply pass stops and tells
    the vault owner. The code is the one the
    condition always carried (`E_CONFLICT`, `E_IO`, `E_BUDGET`, ...), so callers outside the pass read it
    exactly as before.
    """


# Refusals the vault itself causes, never the note's own content: the generation moved while the model was
# consulted (E_STALE), the outcome store refused the reason of a publication (E_OUTCOME), the controller
# enrollment did not match the pages a proposal would change (E_BINDING), the note's stored request changed
# under the request its attempt had opened (E_IDEMPOTENCY), the commit of a batch was refused as a whole
# (E_UNION). A note refused for one of them is asked again, and its claim is still to come.
VAULT_REFUSALS = ('E_STALE', 'E_OUTCOME', 'E_BINDING', 'E_IDEMPOTENCY', 'E_UNION')


class HeadRefusal(KelmoError):
    """A refusal that belongs to the source a batch led with, and to no other source of it.

    The batch shrank to that source and the budget cannot carry even it, its key already names a different
    request, or its selection names a page that is gone. The source is refused exactly as on the
    single-source path; every other source of the batch spends no attempt and keeps its place in the pass.
    """
