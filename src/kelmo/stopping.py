"""The process's stop, once a core is told to end.

A core that is asked to stop (`admin.stop`, or the end of its channel to kelmod) requests it here. From then on a
model call ends at once (its executor group is killed), background work stops at its next step, and a failure
that the stop itself causes, such as a call to another core that can no longer be answered, is raised as
`Stopping`: the work in flight releases its claims and resumes on the next start, and no attempt is charged.
A process that never stops (a CLI command) never requests it.
"""
import threading

from .errors import Stopping

_REQUESTED = threading.Event()


def request():
    _REQUESTED.set()


def requested():
    return _REQUESTED.is_set()


def check():
    """Raises Stopping once the process's stop was requested."""
    if _REQUESTED.is_set():
        raise Stopping()


def reset():
    """Forgets a requested stop (tests run many cores in one process)."""
    _REQUESTED.clear()
