"""Linux namespaces and seccomp, established using only the standard library."""
import os
from pathlib import Path
import platform
import struct
import subprocess
import tempfile

if __package__:
    from .runtime import IsolationError
else:
    from runtime import IsolationError


def seccomp_filter(machine=None):
    machine = platform.machine() if machine is None else machine
    if machine != 'x86_64':
        raise IsolationError('The Linux syscall policy currently supports x86_64 only.')
    # Reject other ABIs before interpreting syscall numbers.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0),
                    (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    # No sockets, cross-process inspection, namespace changes, mount APIs,
    # kernel extensions or alternate asynchronous syscall submission.
    blocked = (41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55,
               101, 155, 161, 165, 166, 167, 168, 169, 172, 173, 175, 176,
               246, 248, 249, 250, 272, 288, 298, 300, 303, 304, 308,
               310, 311, 313, 321, 323, 425, 426, 427, 428, 429, 430,
               431, 432, 433, 438, 440, 442)
    for number in blocked:
        instructions.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x50001)])
    # libc falls back to clone when clone3 is unavailable. Threads are needed
    # by the embedding runtime; namespace flags on clone are forbidden.
    instructions.extend([(0x15, 0, 1, 435), (0x06, 0, 0, 0x50026),
                         (0x15, 0, 3, 56), (0x20, 0, 0, 16),
                         (0x45, 0, 1, 0x7E020000), (0x06, 0, 0, 0x50001),
                         (0x06, 0, 0, 0x7FFF0000)])
    return b''.join(struct.pack('=HBBI', *item) for item in instructions)


def launcher():
    candidates = (Path('/usr/bin/bwrap'),
                  Path(__file__).resolve().parents[2] / 'libexec/bwrap')
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
                raise IsolationError('The Linux sandbox executable has unsafe ownership or permissions.')
            if not os.access(path, os.X_OK):
                raise IsolationError('The Linux sandbox executable is not executable.')
            return path
    raise IsolationError('Install bubblewrap before starting the Linux runtime.')


def _arguments(paths, argv, env, cwd, policy, identity, model_files):
    paths.validate()
    if not argv or Path(argv[0]).resolve() not in paths.executables:
        raise IsolationError('Executable is outside the runtime allowlist.')
    policy.write(seccomp_filter())
    policy.flush()
    policy.seek(0)
    # Some native libraries crash while probing an absent machine ID.
    # Supply a synthetic identity without exposing host identifiers.
    identity.write(b'00000000000000000000000000000000\n')
    identity.flush()
    identity.seek(0)
    args = [str(launcher()), '--unshare-all', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--clearenv', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--ro-bind-data', str(identity.fileno()), '/etc/machine-id']
    # Mount only selected roots. Exact directory grants create empty
    # ancestors, never expose a parent's contents through a bind mount.
    for path in sorted(set((*paths.read_exact, *paths.sync_dirs))):
        if path.is_dir():
            args.extend(['--dir', str(path)])
    for path in sorted(set((*paths.read_only, *paths.executables, *paths.read_exact))):
        if path in paths.read_exact and path.is_dir():
            continue
        if path.exists():
            args.extend(['--ro-bind', str(path), str(path)])
    if model_files:
        args.extend(['--dir', '/kelmo-model-input'])
        for name, path in model_files:
            if (Path(name).name != name or path not in paths.read_only
                    or not path.is_file()):
                raise IsolationError('Model mount is outside the selected input files.')
            args.extend(['--ro-bind', str(path), '/kelmo-model-input/' + name])
    # Debian uses these symlinks for the ELF loader and shell-free Git.
    for name in ('lib', 'lib64', 'bin', 'sbin'):
        path = Path('/') / name
        if path.is_symlink():
            args.extend(['--symlink', os.readlink(path), str(path)])
    for path in paths.writable:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.extend(['--bind', str(path), str(path)])
    for key, value in env.items():
        args.extend(['--setenv', key, value])
    args.extend(['--chdir', str(cwd), '--seccomp', str(policy.fileno()), '--', *map(str, argv)])
    return args


def execute(paths, argv, *, env, cwd, capture_output=False, model_files=()):
    with tempfile.TemporaryFile() as policy, tempfile.TemporaryFile() as identity:
        args = _arguments(paths, argv, env, cwd, policy, identity, model_files)
        return subprocess.run(args, env={'PATH': '/usr/bin:/bin'}, cwd='/',
                              close_fds=True, pass_fds=(policy.fileno(), identity.fileno()),
                              capture_output=capture_output, text=True, check=False)


def spawn(paths, argv, *, env, cwd, pass_fds=()):
    """The same boundary for a long-lived child speaking frames over stdin/stdout pipes.

    bubblewrap reads the policy and identity descriptors while it starts, so the
    parent's copies can close once the child holds its own. `pass_fds` are channel
    descriptors the launcher already opened (the embedding broker's pipes); they keep
    their numbers in the child, which reaches nothing else through them.
    """
    with tempfile.TemporaryFile() as policy, tempfile.TemporaryFile() as identity:
        args = _arguments(paths, argv, env, cwd, policy, identity, ())
        return subprocess.Popen(args, env={'PATH': '/usr/bin:/bin'}, cwd='/', close_fds=True,
                                pass_fds=(policy.fileno(), identity.fileno(), *pass_fds),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
