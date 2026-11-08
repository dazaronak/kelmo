"""Controller-only enrollment and monotonic archive writer rotation."""
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .config import Config
from .errors import KelmoError
from .install.setup import client_config, write
from .publication import coordinator_lock
from .server import PROFILE_SCHEMA, ServerProfile


def encoded(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def profile(root, path):
    config = Config.load(root)
    path = Path(path).expanduser().absolute()
    prior, value = client_config(path, "json")
    if prior is None:
        raise KelmoError("E_CONFIG", "Initialize the server profile first.")
    parsed = ServerProfile.from_dict(value)
    if parsed.bindings.authority_id != config.vault_id:
        raise KelmoError("E_AUTHORITY", "The profile belongs to another vault.")
    return config, path, prior, value


def archive_writer(config):
    with closing(sqlite3.connect(config.archive.as_uri() + "?mode=ro", uri=True)) as connection:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='archive_writer_state'").fetchone()
        if not present:
            return {"writer_id": "local", "epoch": 1}
        rows = connection.execute("SELECT authority_id,writer_id,epoch FROM archive_writer_state").fetchall()
    if len(rows) != 1 or rows[0][0] != config.vault_id:
        raise KelmoError("E_WRITER", "Archive authority does not match this vault.")
    return {"writer_id": rows[0][1], "epoch": rows[0][2]}


def initialize(root, path, *, apply=False):
    config = Config.load(root)
    path = Path(path).expanduser().absolute()
    prior, existing = client_config(path, "json")
    if prior is not None:
        profile(root, path)
        return {"changed": False, "applied": False, "profile": str(path)}
    value = {"schema": PROFILE_SCHEMA, "authority_id": config.vault_id,
             "writer": archive_writer(config), "fenced": True,
             "archive_process": "confined", "clients": [], "repositories": []}
    ServerProfile.from_dict(value)
    result = write(path, encoded(value), expected_prior=None) if apply else {}
    return dict(result, changed=True, applied=apply, profile=str(path), fenced=True,
                writer=value["writer"], authority_id=config.vault_id)


def enroll(root, path, client_id, machine_tag, *, repository_ids=(), requester=False, apply=False):
    config, path, prior, value = profile(root, path)
    entry = {"client_id": client_id, "machine_tag": machine_tag,
             "repository_ids": list(repository_ids), "requester": requester}
    current = next((row for row in value["clients"] if row["client_id"] == client_id), None)
    if current is not None and current != entry:
        raise KelmoError("E_CONFLICT", "This client already has a different enrollment.")
    if current is None:
        value["clients"].append(entry)
        value['enrollment_revision'] = value.get('enrollment_revision', 0) + 1
    ServerProfile.from_dict(value)
    changed = current is None
    if not apply:
        return dict(changed=changed, applied=False, client_id=client_id,
                    enrollment_revision=value.get('enrollment_revision', 0), restart_required=changed)
    with coordinator_lock(config):
        _, path, prior, value = profile(root, path)
        current = next((row for row in value['clients'] if row['client_id'] == client_id), None)
        if current is not None and current != entry:
            raise KelmoError('E_CONFLICT', 'This client already has a different enrollment.')
        changed = current is None
        if changed:
            value['clients'].append(entry)
            value['enrollment_revision'] = value.get('enrollment_revision', 0) + 1
        ServerProfile.from_dict(value)
        result = write(path, encoded(value), expected_prior=prior) if changed else {}
    return dict(result, changed=changed, applied=changed, client_id=client_id,
                enrollment_revision=value.get('enrollment_revision', 0), restart_required=changed)


def enroll_repository(root, path, repository_id, repository_root, remote, *, project=None, apply=False):
    config, path, prior, value = profile(root, path)
    entry = {"repository_id": repository_id, "root": str(Path(repository_root).expanduser().absolute()),
             "remote": remote}
    if project:
        entry["project"] = project
    entries = value.setdefault("repositories", [])
    current = next((row for row in entries if row["repository_id"] == repository_id), None)
    if current is not None and current != entry:
        raise KelmoError("E_CONFLICT", "This repository already has a different enrollment.")
    if current is None:
        entries.append(entry)
        value['enrollment_revision'] = value.get('enrollment_revision', 0) + 1
    ServerProfile.from_dict(value)
    changed = current is None
    if not apply:
        return dict(changed=changed, applied=False, repository_id=repository_id,
                    enrollment_revision=value.get('enrollment_revision', 0), restart_required=changed)
    if project:
        from .projects import add
        add(root, project)
    with coordinator_lock(config):
        _, path, prior, value = profile(root, path)
        entries = value.setdefault('repositories', [])
        current = next((row for row in entries if row['repository_id'] == repository_id), None)
        if current is not None and current != entry:
            raise KelmoError('E_CONFLICT', 'This repository already has a different enrollment.')
        changed = current is None
        if changed:
            entries.append(entry)
            value['enrollment_revision'] = value.get('enrollment_revision', 0) + 1
        ServerProfile.from_dict(value)
        result = write(path, encoded(value), expected_prior=prior) if changed else {}
    return dict(result, changed=changed, applied=changed, repository_id=repository_id,
                enrollment_revision=value.get('enrollment_revision', 0), restart_required=changed)


def set_fence(root, path, fenced, *, apply=False):
    _, path, prior, value = profile(root, path)
    changed = value.get("fenced", False) != fenced
    value["fenced"] = fenced
    ServerProfile.from_dict(value)
    result = write(path, encoded(value), expected_prior=prior) if changed and apply else {}
    return dict(result, changed=changed, applied=bool(apply and changed), fenced=fenced,
                restart_required=changed, running_processes_changed=False)


def inspect(root, path):
    config, path, _, value = profile(root, path)
    writer = archive_writer(config)
    return {"profile": str(path), "authority_id": config.vault_id, "writer": value["writer"],
            "archive_writer": writer, "writer_matches": writer == value["writer"],
            "fenced": value.get("fenced", False), "clients": len(value["clients"]),
            "repositories": len(value.get("repositories", [])),
            "archive_process": value.get("archive_process", "confined")}


def rotate(root, path, writer_id, expected_epoch, receipt, *, apply=False):
    config, path, prior, value = profile(root, path)
    receipt = Path(receipt).expanduser().absolute()
    saved, intent = client_config(receipt, "json")
    before = copy.deepcopy(value)
    after = copy.deepcopy(value)
    after["writer"] = {"writer_id": writer_id, "epoch": expected_epoch + 1}
    if saved is not None:
        try:
            if (intent["kind"] != "kelmo-writer-rotation/1" or intent["profile"] != str(path)
                    or intent["after"]["writer"] != after["writer"]):
                raise ValueError("Different rotation")
            before, after = intent["before"], intent["after"]
            if value not in (before, after):
                raise ValueError("Profile changed")
        except (KeyError, TypeError, ValueError) as error:
            raise KelmoError("E_CONFLICT", "Rotation receipt does not match this request or profile.") from error
    if before["writer"]["epoch"] != expected_epoch:
        raise KelmoError("E_WRITER", "Expected writer epoch does not match the profile.")
    for candidate in (before, after):
        parsed = ServerProfile.from_dict(candidate)
        if parsed.bindings.authority_id != config.vault_id:
            raise KelmoError("E_AUTHORITY", "Rotation receipt names another authority.")
    current = archive_writer(config)
    if current not in (before["writer"], after["writer"]):
        raise KelmoError("E_WRITER", "Archive writer does not match the rotation's old or new epoch.")
    result = {"applied": False, "before": before["writer"], "after": after["writer"],
              "receipt": str(receipt), "restart_required": True}
    if not apply:
        return result
    with coordinator_lock(config):
        if saved is None:
            intent = {"kind": "kelmo-writer-rotation/1", "profile": str(path),
                      "before": before, "after": after}
            write(receipt, encoded(intent), expected_prior=None)
        # The receipt makes a committed epoch recoverable even if the profile write fails.
        with closing(sqlite3.connect(config.archive.as_uri() + "?mode=rw", uri=True)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS archive_writer_state ("
                                   "authority_id TEXT PRIMARY KEY, writer_id TEXT NOT NULL, "
                                   "epoch INTEGER NOT NULL)")
                owners = connection.execute("SELECT authority_id FROM archive_writer_state").fetchall()
                if not owners and before["writer"] == {"writer_id": "local", "epoch": 1}:
                    connection.execute("INSERT INTO archive_writer_state VALUES (?, ?, ?)",
                                       (config.vault_id, "local", 1))
                elif owners != [(config.vault_id,)]:
                    raise KelmoError("E_AUTHORITY", "Archive belongs to another authority.")
                row = connection.execute(
                    "SELECT writer_id,epoch FROM archive_writer_state WHERE authority_id=?",
                    (config.vault_id,)).fetchone()
                if row not in ((before["writer"]["writer_id"], expected_epoch),
                               (writer_id, expected_epoch + 1)):
                    raise KelmoError("E_WRITER", "Archive writer changed before rotation.")
                connection.execute(
                    "UPDATE archive_writer_state SET writer_id=?,epoch=? WHERE authority_id=?",
                    (writer_id, expected_epoch + 1, config.vault_id))
        update = write(path, encoded(after), expected_prior=prior)
    return dict(result, applied=True, backup=update["backup"])
