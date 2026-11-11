"""`kelmo daemon status|stop|uninstall`: the operator's view of the machine daemon of one root.

`stop` asks the running daemon to stop (SIGTERM): it stops taking requests, lets the calls in flight finish within
their drain and exits. `uninstall` is the way back to a 0.1 install: it unloads and removes the kelmod service
(a keep-alive service would otherwise start the daemon again beside a 0.1 process that knows nothing of
`daemon.lock`), stops a daemon still running, and checks that nothing holds the root's `daemon.lock` any more.
Client registrations are removed by `kelmo uninstall`.
"""
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

from ..errors import KelmoError
from . import daemon_client

STOP_SECONDS = 45.0


def _state(root):
    from .instance import state_dir
    return state_dir(root)


OPTIONS = 'kelmod.options.json'


def write_private(path, data):
    """Writes `data` (mode 0600) by rename, without the backups client-configuration writes keep: these files
    are generated, rewritten at every daemon start."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_options(state, options):
    """What `kelmo daemon run` rebuilds the spec from at every start (role, port, runtime directory, worker
    configuration, launcher and kelmod paths)."""
    import json
    write_private(Path(state) / OPTIONS, (json.dumps(options, indent=2, sort_keys=True) + '\n').encode())


def build_spec(root):
    """The spec of the root's service from its saved options, built with the installed package as it is now: an
    upgrade brings its new tool list and paths without running setup again."""
    import json
    from .spec import build, build_edge, installed_core_command
    state = _state(root)
    try:
        options = json.loads((state / OPTIONS).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise KelmoError('E_CONFIG', f'{state / OPTIONS} is missing or unreadable; run kelmo setup.') from error
    command = installed_core_command(options['launcher'])
    if options['role'] == 'edge':
        return build_edge(root, core_command=command, http_port=options['port'], runtime_dir=options['runtime'])
    profile = options.get('server_profile')
    in_process = False
    if profile:
        # A server profile that says `"archive_process": "in-process"` is the operator's word that this host
        # runs the archive without confinement (no sandbox launcher for its platform); the archive core obeys
        # it, as the archive process of 0.1 did.
        from ..server import ServerProfile
        profile = str(Path(profile).expanduser().resolve())
        in_process = ServerProfile.load(profile).archive_process == 'in-process'
    return build(root, core_command=command, server_profile=profile, schedule=True,
                 worker_config=options.get('worker_config'),
                 archive_core=True, archive_in_process=in_process, embed_core=True, kelmod=options['kelmod'],
                 http_port=None if profile else options['port'],
                 runtime_dir=options['runtime'])


def run(root):
    """The service's command: rebuild the spec, then become `kelmod serve --spec`."""
    import json
    from ..install.setup import service_absent, worker_service_file
    leftover = worker_service_file(Path.home())
    if leftover is not None and not _client_root(root):
        raise KelmoError('E_CONFIG', f'The 0.1 worker service ({leftover}) is still installed; run kelmo setup '
                                     'without --no-start, or remove it, before the daemon starts.')
    if not _client_root(root):
        worker = f'gui/{os.getuid()}/org.kelmo.worker' if sys.platform == 'darwin' else 'kelmo.service'
        if not service_absent(worker, sys.platform):
            raise KelmoError('E_CONFIG', 'The legacy worker may still be loaded; run kelmo setup without '
                                        '--no-start to unload it before the daemon starts.')
    spec = build_spec(root)
    if any(core['name'] == 'archive' and '--in-process' in core['argv'] for core in spec['cores']):
        # Said in the service's log at every start, so an unconfined archive is never a silent state.
        print('kelmo: the archive core runs without confinement, as the server profile says '
              '("archive_process": "in-process").', file=sys.stderr, flush=True)
    state = _state(root)
    path = state / 'kelmod.spec.json'
    write_private(path, (json.dumps(spec, indent=2) + '\n').encode())
    options = json.loads((state / OPTIONS).read_text(encoding='utf-8'))
    os.execv(options['kelmod'], [options['kelmod'], 'serve', '--spec', str(path)])


def _client_root(root):
    from ..config import ClientProfile
    return ClientProfile.selected(Path(root).expanduser().resolve())


def status(root):
    result = daemon_client.call(root, 'daemon.status', {}, seconds=5.0)
    if any(core.get('name') == 'state' for core in result.get('cores', [])):
        try:
            result['jobs'] = daemon_client.call(root, 'state.scheduler_status', {}, seconds=5.0)
        except KelmoError as error:
            result['jobs'] = {'unavailable': error.code}
    return result


def _pid(root):
    path = daemon_client.runtime_dir(Path(root).expanduser().resolve()) / 'pid'
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None


def lock_free(root):
    """True when no process holds the root's `daemon.lock`."""
    path = _state(root) / 'daemon.lock'
    if not path.exists():
        return True
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
    return True


def stop(root, seconds=STOP_SECONDS):
    """Sends SIGTERM to the root's daemon and waits until its lock is free."""
    if lock_free(root):
        return {'stopped': False, 'running': False}
    pid = _pid(root)
    if pid is None:
        raise KelmoError('E_RUNTIME', 'A daemon holds the lock but published no pid; stop it by hand.')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        if lock_free(root):
            return {'stopped': True, 'running': False, 'pid': pid}
        time.sleep(0.1)
    raise KelmoError('E_RUNTIME', f'The daemon (pid {pid}) did not stop within {int(seconds)} s.')


def uninstall(root, home, *, apply=False, platform=None):
    """Unloads and removes the kelmod service, stops the daemon, and verifies the lock is free."""
    from ..install.shared import SERVICE_LABEL, SERVICE_UNIT
    from ..install.setup import unload_service
    platform = platform or sys.platform
    home = Path(home).expanduser().resolve()
    if platform == 'darwin':
        service = home / f'Library/LaunchAgents/{SERVICE_LABEL}.plist'
        unload = ['launchctl', 'bootout', f'gui/{os.getuid()}/{SERVICE_LABEL}']
    else:
        service = home / f'.config/systemd/user/{SERVICE_UNIT}'
        unload = ['systemctl', '--user', 'disable', '--now', SERVICE_UNIT]
    result = {'applied': apply, 'service_file': str(service) if service.exists() else None}
    if not apply:
        return dict(result, running=not lock_free(root))
    # A loaded service can outlive its file and restart the daemon after rollback.
    unload_service(unload, platform)
    if service.exists():
        service.unlink()
    result.update(stop(root))
    if not lock_free(root):
        raise KelmoError('E_RUNTIME', 'daemon.lock is still held; do not start a 0.1 install yet.')
    return dict(result, lock_free=True)
