"""Preserve an owned runtime before its interpreter or packages are replaced."""
import os
from pathlib import Path
import shutil
import stat
import tempfile

from ..errors import KelmoError
from ..snapshot import _write_json, tree_files


def backup_runtime(prefix):
    prefix = Path(prefix)
    marker = prefix / ".kelmo-runtime"
    if not marker.is_file() or marker.is_symlink() or marker.read_bytes() != b"kelmo\n":
        raise KelmoError("E_RUNTIME_BACKUP", "Only an owned Kelmo runtime can be backed up for upgrade.")
    for directory, names, files in os.walk(prefix):
        for name in names + files:
            mode = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise KelmoError("E_RUNTIME_BACKUP", "Runtime contains a special file; inspect it before upgrade.")
    before = tree_files(prefix, links="record")
    container = Path(tempfile.mkdtemp(prefix=prefix.name + ".backup-", dir=prefix.parent))
    copy = container / "runtime"
    shutil.copytree(prefix, copy, symlinks=True)
    if tree_files(copy, links="record") != before or tree_files(prefix, links="record") != before:
        raise KelmoError("E_RUNTIME_BACKUP", "Runtime changed during backup; upgrade refused. Retained: " + str(container))
    _write_json(container / "manifest.json", {"schema": "kelmo-runtime-backup/1",
                                             "original": str(prefix), "copy": str(copy), "files": before})
    return {"path": str(container), "runtime": str(copy), "files": len(before)}
