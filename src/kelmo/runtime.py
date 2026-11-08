"""Standard-library-only construction of the offline process boundary."""
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


class IsolationError(Exception):
    def __init__(self, message, code='E_ISOLATION'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimePaths:
    read_only: tuple[Path, ...]
    writable: tuple[Path, ...]
    executables: tuple[Path, ...]
    read_exact: tuple[Path, ...] = ()
    sync_dirs: tuple[Path, ...] = ()

    def validate(self):
        paths = (*self.read_only, *self.writable, *self.executables, *self.read_exact, *self.sync_dirs)
        if any(not p.is_absolute() or p != p.resolve() for p in paths):
            raise IsolationError('Runtime paths must be absolute and resolved.')
        if any(p == Path('/') for p in (*self.read_only, *self.writable)):
            raise IsolationError('The host filesystem cannot be a runtime root.')
        for writable in self.writable:
            for protected in (*self.read_only, *self.executables):
                if protected.is_relative_to(writable) or writable.is_relative_to(protected):
                    raise IsolationError('Writable data cannot overlap protected runtime paths.')


def macos_policy(paths):
    """Adapt the strict policy with no network or host IPC exceptions."""
    paths.validate()
    rules = ['(version 1)', '(deny default)', '(deny network*)', '(allow process-fork)',
             '(allow file-read* (literal "/"))']
    sysctls = ('hw.ncpu', 'hw.activecpu', 'hw.logicalcpu', 'hw.logicalcpu_max',
               'hw.physicalcpu', 'hw.physicalcpu_max', 'hw.memsize', 'hw.pagesize',
               'hw.pagesize_compat', 'hw.cpufamily', 'hw.cpusubfamily', 'hw.cputype',
               'hw.cpusubtype', 'hw.machine', 'hw.cachelinesize', 'hw.l1dcachesize',
               'hw.l1icachesize', 'hw.l2cachesize', 'hw.l3cachesize', 'kern.osrelease',
               'kern.osversion', 'kern.ostype', 'kern.osproductversion', 'kern.hostname',
               'kern.version', 'kern.argmax',
               'kern.maxfilesperproc')
    for name in sysctls:
        rules.append('(allow sysctl-read (sysctl-name ' + json.dumps(name) + '))')
    rules.append('(allow sysctl-read (sysctl-name-prefix "hw.optional."))')
    ancestors = {Path('/')}
    for path in (*paths.read_only, *paths.writable, *paths.executables, *paths.read_exact, *paths.sync_dirs):
        ancestors.update(path.parents)
    # Homebrew loader paths traverse opt symlinks before reaching the granted Cellar files. For each
    # granted Cellar path, the formula's literal opt alias (and its ancestors) gets file-read-metadata
    # only, when the alias target lies inside the granted path or the granted path lies inside the
    # target (a granted subtree of a versioned keg, such as the base interpreter's prefix). This adds
    # no content read: file-read* stays granted only on the Cellar subpaths themselves.
    for path in paths.read_only:
        if 'Cellar' not in path.parts:
            continue
        index = path.parts.index('Cellar')
        if index + 1 >= len(path.parts):
            continue
        alias = Path(*path.parts[:index]) / 'opt' / path.parts[index + 1]
        if alias.is_symlink():
            target = alias.resolve()
            if target.is_relative_to(path) or path.is_relative_to(target):
                ancestors.add(alias)
                ancestors.update(alias.parents)
    for path in sorted(ancestors):
        rules.append('(allow file-read-metadata (literal ' + json.dumps(str(path)) + '))')
    for path in paths.executables:
        rules.append('(allow process-exec (literal ' + json.dumps(str(path)) + '))')
    for path in paths.read_only:
        rules.append('(allow file-read* (subpath ' + json.dumps(str(path)) + '))')
    for path in paths.read_exact:
        rules.append('(allow file-read* (literal ' + json.dumps(str(path)) + '))')
    for path in paths.sync_dirs:
        rules.append('(allow file-read* file-write-data (literal ' + json.dumps(str(path)) + '))')
    for path in paths.writable:
        rules.append('(allow file-read* file-write* (subpath ' + json.dumps(str(path)) + '))')
    for path in ('/dev/null', '/dev/random', '/dev/urandom'):
        rules.append('(allow file-read* (literal ' + json.dumps(path) + '))')
    rules.append('(allow file-write* (literal "/dev/null"))')
    return '\n'.join(rules) + '\n'


def command(paths, argv, *, platform=None):
    """Return a native launcher command; unsupported isolation fails closed."""
    platform = sys.platform if platform is None else platform
    paths.validate()
    if not argv or Path(argv[0]).resolve() not in paths.executables:
        raise IsolationError('Executable is outside the runtime allowlist.')
    if platform != 'darwin':
        raise IsolationError('This platform has no verified runtime isolation adapter yet.')
    sandbox = Path('/usr/bin/sandbox-exec')
    if not sandbox.is_file():
        raise IsolationError('Required OS sandbox is unavailable.')
    return [str(sandbox), '-p', macos_policy(paths), *map(str, argv)]


def system_paths(python):
    """Interpreter and platform library roots a confined Python child needs read-only."""
    import sysconfig
    stdlib = [Path(value) for key, value in sysconfig.get_paths().items()
              if key in ('stdlib', 'platstdlib') and value]
    if sys.platform == 'linux':
        roots = [Path('/usr/lib'), Path('/usr/lib64'), Path('/usr/share/zoneinfo'), *stdlib, Path(python)]
    else:
        roots = [Path(sys.base_prefix), Path('/System/Library'), Path('/usr/lib'), *stdlib,
                 *(Path('/opt/homebrew/Cellar') / name for name in ('openssl@3', 'sqlite', 'mpdecimal', 'xz'))]
    return [path.resolve() for path in dict.fromkeys(roots) if path.exists()]


def confined_spawn(paths, argv, *, env, cwd, platform=None, pass_fds=()):
    """Start a long-lived child with stdin/stdout pipes inside the native OS boundary.

    There is no unconfined fallback: an unsupported platform or a missing sandbox raises.
    `pass_fds` hands already-open channel descriptors (an embedding broker's pipes) to
    the child under the same numbers; the policy grants no path or network for them.
    """
    import subprocess
    platform = sys.platform if platform is None else platform
    if any(type(fd) is not int or fd < 3 for fd in pass_fds):
        raise IsolationError('Passed channel descriptors must be open descriptors above stdio.')
    if platform == 'linux':
        if __package__:
            from .linux_runtime import spawn
        else:
            from linux_runtime import spawn
        return spawn(paths, argv, env=env, cwd=cwd, pass_fds=tuple(pass_fds))
    launch = command(paths, argv, platform=platform)
    return subprocess.Popen(launch, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, close_fds=True, pass_fds=tuple(pass_fds))


def environment(scratch, executable_dirs):
    """Construct the runtime environment without inheriting host credentials."""
    scratch = str(scratch)
    return {'HOME': scratch, 'TMPDIR': scratch,
            'PATH': os.pathsep.join(map(str, executable_dirs)),
            'LANG': 'en_US.UTF-8', 'HF_HUB_OFFLINE': '1',
            'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def validate_stdio():
    for fd in (0, 1, 2):
        if stat.S_ISSOCK(os.fstat(fd).st_mode):
            raise IsolationError('Runtime stdio must not carry an inherited socket.')
