"""The hook spool `kelmod hook` writes under `<state>/hook-spool/`, read by the core that owns the root.

A visible `<nanos>-<pid>.json` is a capture no daemon took. A hidden `.inflight-<name>` is a capture whose call
was under way; `kelmod hook` removes it or makes it visible when the call settles, so one older than
`ORPHAN_SECONDS` was left by a hook its provider killed and is taken as well. Half-written `.tmp` files older
than an hour are removed.
"""
import time
from pathlib import Path

ORPHAN_SECONDS = 60.0
TEMPORARY_SECONDS = 3600.0
INFLIGHT = '.inflight-'


def entries(directory):
    """The spool files to ingest, oldest first."""
    directory = Path(directory)
    try:
        listed = list(directory.iterdir())
    except FileNotFoundError:
        return []
    now = time.time()
    found = []
    for path in listed:
        name = path.name
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if name.endswith('.tmp'):
            if age > TEMPORARY_SECONDS:
                path.unlink(missing_ok=True)
        elif name.startswith(INFLIGHT) and name.endswith('.json'):
            if age > ORPHAN_SECONDS:
                found.append((name[len(INFLIGHT):], path))
        elif name.endswith('.json') and not name.startswith('.'):
            found.append((name, path))
    return [path for _, path in sorted(found)]


def reject(path):
    """Keeps a file that will never ingest, out of the way, for inspection."""
    path = Path(path)
    path.rename(path.with_name(path.name.removeprefix(INFLIGHT)).with_suffix('.bad'))
