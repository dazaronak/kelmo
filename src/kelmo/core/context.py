"""The context every operation runs under: who asked (origin), until when (deadline), whether the caller
still waits (cancel flag), and which request it is. A core builds one per call from the frame header and
passes it down; nothing about a request is kept on a long-lived object."""
import threading
import time
from dataclasses import dataclass, field

from ..errors import KelmoError


@dataclass(frozen=True)
class Origin:
    client_id: str
    machine_tag: str | None = None
    session: str | None = None

    @classmethod
    def from_header(cls, value):
        if value is None:
            return None
        return cls(value['client_id'], value.get('machine_tag'), value.get('session'))

    def to_header(self):
        return {'client_id': self.client_id, 'machine_tag': self.machine_tag, 'session': self.session}


@dataclass(frozen=True)
class Context:
    request_id: str
    origin: Origin | None
    deadline: float  # time.monotonic() limit
    key: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    @classmethod
    def from_header(cls, header, *, now=None):
        start = time.monotonic() if now is None else now
        return cls(request_id=header['id'], origin=Origin.from_header(header.get('origin')),
                   deadline=start + header['deadline_ms'] / 1000, key=header.get('key'))

    @classmethod
    def local(cls, request_id, seconds, origin=None, key=None):
        return cls(request_id=request_id, origin=origin, deadline=time.monotonic() + seconds, key=key)

    def remaining(self):
        """Seconds left before the deadline, never below zero."""
        return max(0.0, self.deadline - time.monotonic())

    def remaining_ms(self):
        """What the next hop's deadline_ms carries: the time left, rounded down."""
        return int(self.remaining() * 1000)

    def cancel(self):
        self.cancelled.set()

    def check(self):
        """At a safe point: stop with E_CANCELLED if the caller cancelled, E_DEADLINE if time ran out."""
        if self.cancelled.is_set():
            raise KelmoError('E_CANCELLED', 'The caller cancelled this request.')
        if time.monotonic() >= self.deadline:
            raise KelmoError('E_DEADLINE', 'The request deadline passed.')
