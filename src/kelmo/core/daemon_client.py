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


def _frames(sock, reader):
    while True:
        data = sock.recv(1048576)
        if not data:
            raise KelmoError('E_TRANSPORT', 'The daemon closed the connection.')
        frames = reader.feed(data)
        if frames:
            return frames


def call(root, op, body=None, *, seconds=60.0, key=None, path=None):
    """One call to the daemon of `root`; returns the reply body or raises KelmoError with its verdict."""
    target = str(path or socket_path(root))
    if not _trusted(target):
        raise KelmoError('E_TRANSPORT', f'The daemon directory of {target} is not private to this user.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(seconds + 5)
        try:
            sock.connect(target)
        except OSError as error:
            raise KelmoError('E_TRANSPORT', f'No daemon answers at {target}.') from error
        reader = wire.FrameReader()
        sock.sendall(wire.encode({'v': wire.VERSION, 'kind': 'hello', 'id': uuid.uuid4().hex, 'body_type': 'json',
                                  'protocols': ['kelmo/2'], 'operations': []}))
        _frames(sock, reader)
        header = {'v': wire.VERSION, 'kind': 'call', 'id': uuid.uuid4().hex, 'op': op, 'body_type': 'json',
                  'deadline_ms': int(seconds * 1000)}
        if key is not None:
            header['key'] = key
        sock.sendall(wire.encode(header, body if body is not None else {}))
        try:
            reply, result = _frames(sock, reader)[0]
        except TimeoutError as error:
            raise KelmoError('E_DEADLINE', 'The daemon did not answer in time.') from error
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
    service_spec = (ClientProfile.load(root).state if ClientProfile.selected(root) else root / '.state') / 'kelmod.spec.json'
    if service_spec.exists():
        # The kelmod service owns this root; a temporary daemon without its endpoint would take its lock.
        raise KelmoError('E_RUNTIME', f'The kelmod service of {root} is not running; start it '
                                      '(launchctl kickstart gui/$UID/org.kelmo.daemon, or systemctl --user start '
                                      'kelmod.service) and retry.')
    if ClientProfile.selected(root):
        state = ClientProfile.load(root).state
        spec = build_edge(root, core_command=command)
    else:
        state = root / '.state'
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
