"""Verified backups and rollback for explicit operator-selected file changes."""
import hashlib
import json
import os
import stat
from pathlib import Path

from ..errors import KelmoError
from .setup import write


KINDS = {"kelmo-mcp-migration", "kelmo-legacy-retirement"}


def sha(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def safe_path(value):
    path = Path(value).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError("E_INPUT", "Migration paths cannot contain symlinks.")
    if any(p.name == ".env" or p.name.startswith(".env.") for p in (path, *path.parents)):
        raise KelmoError("E_INPUT", "Environment files are outside file migration scope.")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise KelmoError("E_INPUT", "Migration files must be regular files with one link.")
    return path


def apply_changes(changed, receipt, *, kind):
    receipt = safe_path(receipt)
    backup_dir = receipt.with_name(receipt.name + ".backups")
    if receipt.exists() or backup_dir.exists() or backup_dir.is_symlink():
        raise KelmoError("E_INPUT", "Use a fresh receipt path; an existing receipt belongs to recovery.")
    if kind not in KINDS:
        raise KelmoError("E_INPUT", "Unknown file migration kind.")
    for row in changed:
        path = safe_path(row["path"])
        if path.read_bytes() != row["before"]:
            raise KelmoError("E_CONFLICT", "A selected file changed before migration.")
    backup_dir.mkdir(parents=True, mode=0o700)
    manifest = {"schema_version": 1, "kind": kind, "files": []}
    for index, row in enumerate(changed):
        path = safe_path(row["path"])
        backup = backup_dir / f"{index}.bak"
        write(backup, row["before"], expected_prior=None)
        manifest["files"].append({"path": str(path), "backup": str(backup),
                                  "before_sha256": sha(row["before"]),
                                  "after_sha256": sha(row["after"]),
                                  "mode": stat.S_IMODE(path.stat().st_mode) & 0o777})
    write(receipt, (json.dumps(manifest, indent=2) + "\n").encode(), expected_prior=None)
    completed = []
    try:
        for row in changed:
            path = safe_path(row["path"])
            if row["after"] is None:
                if path.read_bytes() != row["before"]:
                    raise KelmoError("E_CONFLICT", "A selected file changed before retirement.")
                path.unlink()
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                write(path, row["after"], expected_prior=row["before"])
            completed.append(str(path))
    except (KelmoError, OSError) as error:
        raise KelmoError("E_MIGRATION", f"File migration stopped after {len(completed)} file(s). "
                        f"Rollback receipt: {receipt}") from error
    return {"applied": True, "receipt": str(receipt), "backups": str(backup_dir)}


def rollback(receipt, *, apply=False):
    receipt = safe_path(receipt)
    try:
        manifest = json.loads(receipt.read_bytes())
    except (ValueError, OSError) as error:
        raise KelmoError("E_INPUT", "Cannot read the migration receipt.") from error
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("kind") not in KINDS or not isinstance(manifest.get("files"), list)):
        raise KelmoError("E_INPUT", "Not a supported file migration receipt.")
    rows, seen = [], set()
    for item in manifest["files"]:
        try:
            path, backup = safe_path(item["path"]), safe_path(item["backup"])
            original = backup.read_bytes()
            current = path.read_bytes() if path.exists() else None
            mode = item.get("mode", stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
            if path in seen or sha(original) != item["before_sha256"] or type(mode) is not int or not 0 <= mode <= 0o777:
                raise ValueError("Invalid backup")
            seen.add(path)
            if sha(current) not in {item["before_sha256"], item["after_sha256"]}:
                raise KelmoError("E_CONFLICT", f"File changed after migration: {path}. "
                                "Rollback will not overwrite it.")
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise KelmoError("E_INPUT", "Migration backup or file cannot be verified.") from error
        if current != original:
            rows.append((path, current, original, mode))
    if apply:
        for path, current, original, mode in rows:
            write(path, original, expected_prior=current)
            path.chmod(mode)
    return {"applied": bool(apply and rows), "receipt": str(receipt),
            "profiles": [str(row[0]) for row in rows], "restart_required": bool(rows)}
