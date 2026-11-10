"""Every environment variable read under src/kelmo is listed with its home in kelmo.core.environment."""
import ast
from pathlib import Path

from kelmo.core.environment import DYNAMIC_READS, VARIABLES

SRC = Path(__file__).resolve().parents[2] / 'src' / 'kelmo'
CONSTANTS = {'VAULT_ENV_VAR': 'KELMO_VAULT', 'ENV_VAR': 'KELMO_TIMINGS'}


def _environ(node):
    return isinstance(node, ast.Attribute) and node.attr == 'environ' or isinstance(node, ast.Name) and \
        node.id == 'environ'


def reads():
    """(file, line, name or None for a computed name) of every read of the process environment."""
    found = []
    for path in sorted(SRC.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            target = None
            if isinstance(node, ast.Subscript) and _environ(node.value) and isinstance(node.ctx, ast.Load):
                target = node.slice
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args and (
                    node.func.attr in ('get', 'pop') and _environ(node.func.value) or node.func.attr == 'getenv'):
                target = node.args[0]
            elif isinstance(node, ast.Compare) and any(_environ(item) for item in node.comparators):
                target = node.left
            if target is None:
                continue
            if isinstance(target, ast.Constant):
                name = target.value
            elif isinstance(target, ast.Name) and target.id in CONSTANTS:
                name = CONSTANTS[target.id]
            else:
                name = None
            found.append((path.relative_to(SRC).as_posix(), node.lineno, name))
    return found


def test_every_environment_read_has_a_home():
    found = reads()
    assert found, 'the scan found no environment read, so it checks nothing'
    unlisted = [(file, line, name) for file, line, name in found if name is not None and name not in VARIABLES]
    computed = [(file, line) for file, line, name in found if name is None and file not in DYNAMIC_READS]
    assert not unlisted, f'environment reads without a home in kelmo.core.environment: {unlisted}'
    assert not computed, f'computed environment reads not explained in DYNAMIC_READS: {computed}'


def test_every_home_is_one_of_the_known_kinds():
    assert {home for home, _, _ in VARIABLES.values()} <= {'hook-frame', 'daemon', 'core-spec', 'cli', 'removed'}
