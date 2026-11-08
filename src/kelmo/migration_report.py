"""Bundle named deployment evidence into a private, hash-indexed directory."""
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import tempfile

from . import __version__
from .errors import KelmoError
from .migration_cli import absolute
from .wiki.engine import sync_directory


def bundle(root, destination, includes=(), *, apply=False):
    root, destination = absolute(root), absolute(destination)
    if destination.exists():
        raise KelmoError("E_INPUT", "Use a new report destination; existing evidence is retained.")
    sources = [root / name for name in (".import/state.json", ".import/cutover.json")
               if (root / name).exists()]
    sources.extend(map(absolute, includes))
    sources = list(dict.fromkeys(sources))
    if len(sources) > 64:
        raise KelmoError("E_INPUT", "A report accepts at most 64 evidence files.")
    for source in sources:
        absolute(source)
        if (any(p.name == ".env" or p.name.startswith(".env.") for p in (source, *source.parents))
                or not source.is_file() or source.stat().st_size > 16 * 1048576):
            raise KelmoError("E_INPUT", "Select regular evidence files of at most 16 MiB; environment files are excluded.")
        if source == destination or destination in source.parents:
            raise KelmoError("E_INPUT", "Report output cannot contain one of its selected sources.")
    result = {"applied": False, "destination": str(destination), "files": len(sources),
              "consistent_snapshot": False}
    if not apply:
        return result
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".memory-report-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "report"
        stage.mkdir(mode=0o700)
        entries = []
        for index, source in enumerate(sources):
            data = source.read_bytes()
            if len(data) > 16 * 1048576:
                raise KelmoError("E_INPUT", "Evidence grew beyond the report size limit.")
            name = f"{index:03d}-{source.name}"
            _write(stage / name, data)
            entries.append({"source": str(source), "file": name, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"schema": "kelmo-deployment-report/1", "version": __version__,
                    "created_at": datetime.now(UTC).isoformat(), "control_root": str(root),
                    "consistent_snapshot": False, "files": entries}
        _write(stage / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        sync_directory(stage)
        if destination.exists() or destination.is_symlink():
            raise KelmoError("E_CONFLICT", "Report destination appeared during collection.")
        stage.rename(destination)
        sync_directory(destination.parent)
    return dict(result, applied=True, manifest=str(destination / "manifest.json"))


def _write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
