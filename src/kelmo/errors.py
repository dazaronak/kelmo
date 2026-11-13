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


class HeadRefusal(KelmoError):
    """A batch shrank to its oldest source and the budget cannot carry even that one.

    The refusal is that source's alone, exactly as on the single-source path: every other source of the
    batch was dropped before the model read it, spends no attempt and keeps its place in the pass.
    """
