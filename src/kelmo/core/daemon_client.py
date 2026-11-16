"""The CLI's way to the daemon that owns a vault: one kelmo/2 call over `d.sock`.

When no daemon runs for the root, `ensure` starts `kelmod serve --ephemeral`, which ends by itself once no
client has been connected for a short while. The runtime directory is derived exactly as kelmod derives it
(kelmo-runtime/src/ipc.rs): `$TMPDIR/kelmo/<first 4 bytes of sha256(realpath)>` (`$XDG_RUNTIME_DIR` on Linux).
"""
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..errors import KelmoError
from . import wire

START_SECONDS = 15.0
# How long a resend after a cut keeps trying to connect while kelmod rebinds its socket.
RECONNECT_SECONDS = START_SECONDS


def runtime_dir(root):
    real = os.path.realpath(root)
    base = os.environ.get('XDG_RUNTIME_DIR') if sys.platform.startswith('linux') else None
    base = base or os.environ.get('TMPDIR') or '/tmp'
    return Path(base) / 'kelmo' / hashlib.sha256(os.fsencode(real)).hexdigest()[:8]


def socket_path(root):
    return runtime_dir(root) / 'd.sock'


def _trusted(path):
    """True when the socket's directory is a real directory of this user with no access for others: under a
    shared /tmp another user could otherwise stand up a socket that answers in the daemon's place."""
    try:
        info = os.lstat(Path(path).parent)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def kelmod_executable(preferred=None):
    """kelmod beside the kelmo launcher (the wheel installs both in one scripts directory), beside this
    interpreter, else on PATH."""
    for beside in (preferred, Path(sys.executable).parent / 'kelmod'):
        if beside is not None and Path(beside).is_file():
            return str(beside)
    found = shutil.which('kelmod')
    if found is None:
        raise KelmoError('E_RUNTIME', 'kelmod is not installed beside kelmo; reinstall the package.')
    return found


# The first attempt's socket waits this long past the deadline, so kelmod's own E_DEADLINE reply can arrive.
GRACE_SECONDS = 5.0
RECONNECT_PAUSE = 0.05


class _Cut(Exception):
    """kelmod's channel ended under a call: closed, reset, or broken on a write. `sent` says whether the
    call frame's write had been attempted."""

    def __init__(self, sent):
        super().__init__('The daemon closed the connection.')
        self.sent = sent


def _budget(sock, until):
    """Bounds the next blocking socket operation by the absolute `until`, so all of an exchange's waits
    together stay inside it."""
    left = until - time.monotonic()
    if left <= 0:
        raise TimeoutError('timed out')
    sock.settimeout(left)


def _frames(sock, reader, until):
    while True:
        _budget(sock, until)
        data = sock.recv(1048576)
        if not data:
            raise ConnectionResetError('The daemon closed the connection.')
        frames = reader.feed(data)
        if frames:
            return frames


def _resendable(op, key, resend=False):
    """Whether a call cut after it was sent may go once more: kelmod's own replay rule (operations.py), or the
    caller's word that this call changes nothing or carries its own idempotency key (`resend`)."""
    from .operations import REPLAY_KEYED, REPLAY_READ, by_name
    if resend or op == 'daemon.status':  # daemon.status is served by kelmod itself, outside the operation table
        return True
    found = by_name().get(op)
    replay = found.replay if found is not None else None
    return replay == REPLAY_READ or (replay == REPLAY_KEYED and key is not None)


def _connect(target, until, limit=None):
    """A connected socket, each connect bounded by the absolute `until` (a daemon whose listen queue is full
    leaves a connect waiting). Without `limit` one try; with it, a try every RECONNECT_PAUSE until `limit`."""
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _budget(sock, until)
            sock.connect(target)
            return sock
        except OSError as error:
            sock.close()
            now = time.monotonic()
            if now + RECONNECT_PAUSE >= until:
                raise KelmoError('E_DEADLINE', 'The daemon did not answer in time.') from error
            if limit is None or now + RECONNECT_PAUSE >= limit:
                raise KelmoError('E_TRANSPORT', f'No daemon answers at {target}.') from error
            time.sleep(RECONNECT_PAUSE)


def _attempt(sock, op, body, key, until, deadline_ms):
    """One exchange on a connected socket: hello, the call, its reply, all of it by the absolute `until`."""
    sent = False
    with sock:
        try:
            reader = wire.FrameReader()
            _budget(sock, until)
            sock.sendall(wire.encode({'v': wire.VERSION, 'kind': 'hello', 'id': uuid.uuid4().hex,
                                      'body_type': 'json', 'protocols': ['kelmo/2'], 'operations': []}))
            _frames(sock, reader, until)
            header = {'v': wire.VERSION, 'kind': 'call', 'id': uuid.uuid4().hex, 'op': op, 'body_type': 'json',
                      'deadline_ms': deadline_ms}
            if key is not None:
                header['key'] = key
            frame = wire.encode(header, body if body is not None else {})
            _budget(sock, until)
            sent = True
            sock.sendall(frame)
            return _frames(sock, reader, until)[0]
        except TimeoutError as error:
            if not sent:
                # Nothing reached kelmod, so no pass or write can be running for this call.
                failure = KelmoError('E_TRANSPORT', 'The daemon did not answer the hello in time; nothing was sent.')
                failure.retryable = True
                raise failure from error
            raise KelmoError('E_DEADLINE', 'The daemon did not answer in time.') from error
        except (ConnectionResetError, BrokenPipeError) as error:
            raise _Cut(sent) from error


def _unsent(cause):
    """No attempt reached kelmod: nothing runs for this call, and it may be sent again."""
    error = KelmoError('E_TRANSPORT', 'The daemon did not take the connection in time; nothing was sent.')
    error.retryable = True
    return error


def _unknown(cause):
    error = KelmoError('E_TRANSPORT', 'The daemon closed the connection after the call was sent; its effect is '
                                      'unknown.')
    error.retryable = False
    return error


def call(root, op, body=None, *, seconds=60.0, key=None, path=None, resend=False):
    """One call to the daemon of `root`; returns the reply body or raises KelmoError with its verdict.

    A channel cut under the call (kelmod stopping, restarting or crashing) is met by kelmod's own replay
    rule: a call cut before it was sent, a read, or a write under a key goes once more inside the deadline;
    any other write is not sent again and is reported with its effect unknown. `resend=True` is the caller's
    word that the call is safe to send again although its op is not a read: a CLI command that changes
    nothing, or one under its own idempotency key."""
    target = str(path or socket_path(root))
    if not _trusted(target):
        raise KelmoError('E_TRANSPORT', f'The daemon directory of {target} is not private to this user.')
    limit = time.monotonic() + seconds
    try:
        until = limit + GRACE_SECONDS
        try:
            sock = _connect(target, until)
        except KelmoError as error:
            if error.code == 'E_DEADLINE':  # the connect itself ran out of time: nothing was sent
                raise _unsent(error) from error
            raise
        reply, result = _attempt(sock, op, body, key, until, int(seconds * 1000))
    except _Cut as cut:
        if cut.sent and not _resendable(op, key, resend):
            raise _unknown(cut) from cut
        if limit - time.monotonic() <= 0:
            raise KelmoError('E_DEADLINE', 'The daemon did not answer in time.') from cut
        try:
            sock = _connect(target, limit, min(limit, time.monotonic() + RECONNECT_SECONDS))
        except KelmoError as error:
            if error.code == 'E_DEADLINE' and not cut.sent:  # neither attempt reached kelmod
                raise _unsent(error) from error
            raise
        left = limit - time.monotonic()
        if left <= 0:
            sock.close()
            raise KelmoError('E_DEADLINE', 'The daemon did not answer in time.') from cut
        try:
            reply, result = _attempt(sock, op, body, key, limit, int(left * 1000))
        except _Cut as again:
            if again.sent and not _resendable(op, key, resend):
                raise _unknown(again) from again
            error = KelmoError('E_TRANSPORT', 'The daemon closed the connection again.')
            error.retryable = True  # nothing was sent the second time, or the call is safe to send again
            raise error from again
    if reply.get('ok'):
        return result
    error = KelmoError(reply['error']['code'], reply['error']['message'])
    error.retryable = reply['error'].get('retryable', False)
    raise error


def _answers(path):
    if not _trusted(path):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def _check_profile(root, path, server_profile):
    """Refuses a running daemon that serves another server profile than the caller needs: its answers
    would read as an enrollment problem instead of naming the mismatch."""
    if server_profile is None:
        return
    wanted = str(Path(server_profile).expanduser().resolve())
    meta = call(root, 'daemon.status', {}, seconds=5.0, path=path).get('meta') or {}
    if meta.get('server_profile') != wanted:
        running = meta.get('server_profile') or 'no server profile'
        raise KelmoError('E_RUNTIME', f'The daemon of {root} runs with {running}, not {wanted}; '
                                      'stop it (kelmo daemon stop) and connect again.')


# The longest wait a call may ask for: the wire refuses a deadline past a day.
LONGEST_CALL_SECONDS = 86400.0


def service_owned(root):
    """Whether an installed kelmod service owns `root`. Its daemon outlives its callers; a daemon `ensure`
    starts ends once none is connected, and the work it was doing is stopped with it."""
    from .instance import state_dir
    return (state_dir(Path(root).expanduser().resolve()) / 'kelmod.spec.json').exists()


def ensure(root, *, kelmod=None, launcher=None, server_profile=None):
    """The socket of the daemon that owns `root`, starting an ephemeral one when none answers. `launcher`
    is the installed `kelmo` script, whose runtime then starts the cores; `server_profile` lets the started
    daemon serve the profile's enrolled clients. A client root (a shared client) gets the edge daemon."""
    from ..config import ClientProfile
    from .spec import build, build_edge, installed_core_command
    root = Path(root).expanduser().resolve()
    path = socket_path(root)
    if _answers(path):
        _check_profile(root, path, server_profile)
        return path
    command = installed_core_command(launcher) if launcher is not None else None
    from .instance import state_dir
    state = state_dir(root)
    service_spec = state / 'kelmod.spec.json'
    if service_spec.exists():
        # The kelmod service owns this root; a temporary daemon without its endpoint would take its lock.
        raise KelmoError('E_RUNTIME', f'The kelmod service of {root} is not running; start it '
                                      '(launchctl kickstart gui/$UID/org.kelmo.daemon, or systemctl --user start '
                                      'kelmod.service) and retry.')
    if ClientProfile.selected(root):
        spec = build_edge(root, core_command=command)
    else:
        spec = build(root, core_command=command, server_profile=server_profile)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec_path = state / 'daemon.spec.json'
    temporary = spec_path.with_name(spec_path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(spec), encoding='utf-8')
    os.replace(temporary, spec_path)
    log_path = state / 'kelmod.log'
    start = log_path.stat().st_size if log_path.exists() else 0
    with log_path.open('ab') as log:
        process = subprocess.Popen([kelmod_executable(kelmod), 'serve', '--spec', str(spec_path), '--ephemeral'],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                   close_fds=True)
    limit = time.monotonic() + START_SECONDS
    while time.monotonic() < limit:
        if _answers(path):
            _check_profile(root, path, server_profile)
            return path
        code = process.poll()
        # 75: another daemon took the root first; its socket answers in a moment.
        if code is not None and code != 75:
            with log_path.open('rb') as log:
                log.seek(start)
                reason = log.read(2000).decode('utf-8', 'replace').strip()
            raise KelmoError('E_RUNTIME', f'The daemon for {root} exited with {code}: {reason or "no output"}')
        time.sleep(0.05)
    raise KelmoError('E_RUNTIME', f'The daemon for {root} did not start; see {log_path}.')
