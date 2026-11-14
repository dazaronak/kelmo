"""Stable errors shared by the CLI and service."""
import sqlite3


def sqlite_busy(error):
    """Whether a SQLite failure is contention (busy or locked), which passes, rather than a fault."""
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return isinstance(error, sqlite3.OperationalError) and ('locked' in str(error) or 'busy' in str(error))


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
