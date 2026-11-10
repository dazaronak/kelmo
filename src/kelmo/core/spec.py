"""The kelmod specification of one root: the cores it starts, their lanes and budgets, the operation table.

`kelmo core-spec --vault V` prints it as JSON for `kelmod serve --spec`. A core starts with a stripped
environment, so every value it needs is named here (core/environment.py lists the `core-spec` variables).
"""
import os
import sys
from pathlib import Path

from . import operations

LOCAL_ORIGIN = {'client_id': 'local', 'machine_tag': None, 'session': None}

# Lane: (in flight to the core, queued in kelmod, order). Round robin per origin, so one busy client cannot
# starve the others; the state core runs writes one at a time on its writer thread whatever the limit.
STATE_LANES = {
    'handler': (32, 512, 'rr'),
    'read': (4, 512, 'rr'),
    'write': (8, 512, 'rr'),
    'admin': (4, 64, 'fifo'),
}
STATE_BUDGET_MB = (1536, 3072)
PASSED_ENVIRONMENT = ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                      'HOME', 'PATH', 'LANG', 'TMPDIR')


def _lanes(table):
    return {name: {'limit': limit, 'queue': queue, 'order': order} for name, (limit, queue, order) in table.items()}


def _environment(source_root=None):
    passed = {name: os.environ[name] for name in PASSED_ENVIRONMENT if name in os.environ}
    if source_root is not None:
        passed['PYTHONPATH'] = str(source_root)
    return passed


def installed_core_command(launcher):
    """How the installed launcher's runtime starts a core: the same isolated interpreter and entry script the
    launcher itself runs (`-I -S` imports only from the installation), through the hidden `core-run` verb."""
    prefix = Path(launcher).resolve().parent.parent
    return [sys.executable, '-I', '-S', '-X', 'utf8=1', str(prefix / 'share/kelmo/entry.py'), 'core-run', '--']


def build(root, *, server_profile=None, runtime_dir=None, executable=None, readers=4, source_root=None,
          core_command=None):
    """The spec of the daemon that owns `root`. `core_command` is the argv prefix that runs
    `kelmo.core.state_core.main` (default: this interpreter with `-m`); `source_root` puts a source tree on
    the core's PYTHONPATH (tests and a development checkout)."""
    root = Path(root).expanduser().resolve()
    prefix = core_command or [executable or sys.executable, '-m', 'kelmo.core.state_core']
    argv = [*prefix, '--vault', str(root), '--readers', str(readers)]
    if server_profile is not None:
        argv += ['--server-profile', str(Path(server_profile).expanduser().resolve())]
    lanes = dict(STATE_LANES, read=(readers, STATE_LANES['read'][1], STATE_LANES['read'][2]))
    state = {'name': 'state', 'argv': argv, 'env': _environment(source_root), 'lanes': _lanes(lanes),
             'budget': {'soft_mb': STATE_BUDGET_MB[0], 'hard_mb': STATE_BUDGET_MB[1]}}
    served = [op for op in operations.table() if op.name.startswith('state.')]
    spec = {'root': str(root), 'origin': LOCAL_ORIGIN, 'cores': [state],
            'operations': [{'name': op.name, 'lanes': [lane for lane in op.lanes if lane in lanes],
                            'replay': op.replay} for op in served]}
    if runtime_dir is not None:
        spec['runtime_dir'] = str(runtime_dir)
    return spec

