"""Read a provider quota answer as a wait that keeps the failure budget."""
import math
import time
from datetime import datetime, timezone

from .errors import KelmoError

OUTCOME = 'quota_wait'
MAX_WAIT = 86400
# Why a provider waits: its usage limit, or a login that must be renewed by hand.
REASONS = ('limit', 'login')
# Called with every executor answer the worker checks: the QuotaWait, or None for an answer. The worker
# daemon sets it (alerts.LoginWatch) for its lifetime; an observer failure never changes the answer.
observer = None


def clock(reset_at):
    return datetime.fromtimestamp(reset_at, timezone.utc).isoformat(timespec='seconds')


class QuotaWait(KelmoError):
    """The provider is rate limited: the request waits until its reset time and keeps its attempts."""

    def __init__(self, reset_at, reason=None):
        cause = 'The provider login is lost; tried again at ' if reason == 'login' else 'The provider is rate limited until '
        super().__init__('E_QUOTA', cause + clock(reset_at)
                         + '; the request stays pending and no attempt is charged.')
        self.reset_at = reset_at
        self.reason = reason


def _epoch(reset_at):
    return not isinstance(reset_at, bool) and isinstance(reset_at, (int, float)) and math.isfinite(reset_at)


def within_bounds(reset_at):
    """True for a UTC epoch second inside the accepted provider-quota window; checked before any
    conversion, so millisecond or far-future values never reach `clock` or persistence."""
    if not _epoch(reset_at):
        return False
    now = time.time()
    return now - 60 <= reset_at <= now + MAX_WAIT


def read(answer):
    """The reset time of a quota answer, or None when the answer is not one."""
    if not isinstance(answer, dict) or answer.get('outcome') != OUTCOME:
        return None
    if set(answer) - {'reason'} != {'outcome', 'reset_at'} or answer.get('reason', 'limit') not in REASONS:
        raise KelmoError('E_EXECUTOR', 'A quota_wait answer carries outcome, reset_at and an optional reason only.')
    reset_at = answer['reset_at']
    if not _epoch(reset_at):
        raise KelmoError('E_EXECUTOR', 'quota_wait reset_at must be a UTC epoch second.')
    if not within_bounds(reset_at):
        raise KelmoError('E_EXECUTOR', 'quota_wait reset_at must fall within the next 24 hours.')
    return reset_at


def checked(answer):
    """Hand back an executor answer, or raise QuotaWait when it declares the provider rate limited."""
    reset_at = read(answer)
    wait = QuotaWait(reset_at, answer.get('reason')) if reset_at is not None else None
    if observer is not None:
        try:
            observer(wait)
        except Exception:  # noqa: BLE001 - Watching an answer must not change it.
            pass
    if wait is not None:
        raise wait
    return answer
