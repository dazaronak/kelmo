"""Helpers for the archive core tests.

A kelmo/2 peer on a socketpair, the calls a state core sends to the archive, and the archive
core process started the way kelmod starts it. Every vault is synthetic.
"""
import hashlib
import os
import subprocess
import sys
import time
import uuid
from collections import deque

from daemon_harness import SRC

from kelmo.config import Config
from kelmo.contracts import SourcePacket
from kelmo.core import wire
from kelmo.server import ARCHIVE_PROTOCOL

PROJECT = 'archive-core-check'
OBSERVED_AT = '2026-11-10T09:00:00+00:00'


class Peer:
    """The far end of a kelmo/2 channel; frames that arrive early are kept in order."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = wire.FrameReader()
        self.frames = deque()

    def frame(self, timeout=30):
        """The next frame as (header, body), whichever call it answers."""
        while not self.frames:
            self._receive(timeout)
        return self.frames.popleft()

    def send(self, op, body):
        """Sends one call and returns its id; the answer is read with reply()."""
        call_id = uuid.uuid4().hex
        header = {'v': wire.VERSION, 'kind': 'call', 'id': call_id, 'op': op,
                  'body_type': 'json', 'deadline_ms': 60000}
        self.sock.sendall(wire.encode(header, body))
        return call_id

    def reply(self, call_id, timeout=30):
        """(header, body) of the frame that answers `call_id`."""
        end = time.monotonic() + timeout
        while True:
            for index, (header, body) in enumerate(self.frames):
                if header['id'] == call_id:
                    del self.frames[index]
                    return header, body
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f'no reply to call {call_id}')
            self._receive(left)

    def call(self, op, body, timeout=30):
        return self.reply(self.send(op, body), timeout)

    def _receive(self, timeout):
        self.sock.settimeout(timeout)
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError('the archive core closed the channel')
        self.frames.extend(self.reader.feed(data))


def answered(peer, op, body):
    """The body of a successful answer; a refusal fails the test with its header."""
    header, value = peer.call(op, body)
    assert header['ok'] is True, header
    return value


def source_packet(text, filename='probe.md'):
    """A raw-note source packet, built as the state core builds one."""
    content = text.encode('utf-8')
    return SourcePacket(content, 'raw_note', hashlib.sha256(content).hexdigest(),
                        filename, OBSERVED_AT, kind='event', domain='dev',
                        project=PROJECT, tags=('archive-core',))


def capture_body(root, text, key):
    """The arguments of archive.capture for `text`, under the vault's local origin."""
    origin = Config.load(root).local_bindings().origin('local')
    return {'source': source_packet(text).to_dict(), 'origin': origin.to_dict(),
            'idempotency_key': key}


def in_process_start(root, scratch):
    """The startup an in-process archive core is given: the fields archive_core.main computes."""
    config = Config.load(root)
    return {'protocol': ARCHIVE_PROTOCOL, 'enrollment': config.local_bindings().describe(),
            'archive': str(config.archive.resolve()), 'cache': str(config.cache),
            'scratch': str(scratch), 'canaries': [], 'fenced': False, 'embedding': None,
            'idle_seconds': None}


def spawn_archive_core(root, channel, log_path):
    """`kelmo.core.archive_core --in-process` with the channel on descriptor 3, as kelmod
    hands it over. The descriptor is copied in preexec_fn; close_fds stays off so that
    nothing closes it before exec."""
    argv = [sys.executable, '-m', 'kelmo.core.archive_core', '--vault', str(root),
            '--in-process']
    environment = dict(os.environ, PYTHONPATH=str(SRC))
    with log_path.open('w') as log:
        return subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, close_fds=False,
                                preexec_fn=lambda: os.dup2(channel, 3))


def stop_archive_core(proc, timeout=60):
    """Waits for the core to end after its channel closed; a core that does not end is killed."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
