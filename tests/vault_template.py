"""Build a test vault once per process and give every case its own copy of it.

A fixture that initializes a store, adopts a registry and commits seed pages costs seconds, nearly all of it git
and SQLite work that comes out the same in every case. copy() runs such a build once per process for each key, at
a private path, and then copies the result to the case's own path: a plain directory copy, tens of milliseconds
for a fixture vault, the same on macOS and on Linux, with no copy-on-write or mount support needed. A copy is a
separate directory, so nothing a case writes reaches the template or another case.

KELMO_TEST_GOLDEN=0 turns the template off and builds at each case's own path, which is how a copy and a
fresh build are shown to pass the same cases.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_BUILT = {}


def enabled():
    return os.environ.get('KELMO_TEST_GOLDEN', '1') != '0'


def copy(target, key, build):
    """Make target the directory build(path) produces there, and return what build returned.

    build runs once per process for each key. The key names everything the built directory depends on: a later
    caller with the same key gets a copy of the first build and never runs its own. The returned value is the
    first build's own object, shared by every caller and never copied, because trusted bindings recognise the
    origins they issued by identity; a case must not change it in place.
    """
    target = Path(target)
    if not enabled():
        return build(target)
    if key not in _BUILT:
        base = Path(tempfile.mkdtemp(prefix='vault-template-'))
        atexit.register(shutil.rmtree, base, True)
        source = base / target.name
        _BUILT[key] = (source, build(source))
    source, value = _BUILT[key]
    shutil.copytree(source, target, symlinks=True)
    return value
