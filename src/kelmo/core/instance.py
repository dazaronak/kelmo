"""`<state>/daemon.lock`: the one lock that says who owns a vault.

`kelmod serve` holds it for its whole life. An offline tool that must have the vault to itself (snapshot,
import, rotate-writer, the archive operator verbs that write) takes the same lock for its run and refuses
while a daemon holds it, so nothing opens the journal beside a running daemon.
"""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from ..errors import KelmoError


def state_dir(root):
    """Where the daemon of `root` keeps its own files (the lock, its spec and options, job state, spools): the
    vault's state directory, which a vault with bound storage keeps outside the vault directory, or a shared
    client's state directory. A root that is not initialized yet has `<root>/.state`, where `init` puts it."""
    from ..config import ClientProfile, Config
    root = Path(root).expanduser().resolve()
    if ClientProfile.selected(root):
        return ClientProfile.load(root).state
    try:
        return Config.load(root).state
    except KelmoError as error:
        if error.code != 'E_NOT_INITIALIZED':
            raise
        return root / '.state'


def lock_path(root):
    return state_dir(root) / 'daemon.lock'


@contextmanager
def offline(root, what):
    """Holds daemon.lock for the block. A root without a state directory has no daemon and nothing to lock
    (an import creates it), so the block runs as it is."""
    path = lock_path(root)
    if not path.parent.is_dir():
        yield
        return
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise KelmoError('E_BUSY', f'A kelmo daemon owns this vault; stop it before {what}.') from error
        yield
    finally:
        os.close(descriptor)
