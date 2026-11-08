"""Snapshot, backup and recovery commands, and the JSON specification reader they share."""
import json
from pathlib import Path

from .errors import KelmoError


def read_spec(path, allowed=None):
    path = Path(path).expanduser().absolute()
    if (any(p.is_symlink() for p in (path, *path.parents))
            or any(p.name == ".env" or p.name.startswith(".env.") for p in (path, *path.parents))
            or not path.is_file() or path.stat().st_size > 16 * 1048576):
        raise KelmoError("E_INPUT", "Select a regular JSON specification of at most 16 MiB.")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise KelmoError("E_INPUT", "Migration specification must be valid JSON.") from error
    if not isinstance(value, dict) or (allowed is not None and set(value) - set(allowed)):
        raise KelmoError("E_INPUT", "Migration specification contains unsupported fields.")
    return value


def absolute(value):
    if not isinstance(value, (str, Path)) or not Path(value).expanduser().is_absolute():
        raise KelmoError("E_INPUT", "Migration paths must be absolute.")
    path = Path(value).expanduser()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise KelmoError("E_INPUT", "Migration roots cannot contain symlinks.")
    return path


def separate(sources, destinations):
    for destination in destinations:
        for source in sources:
            if source == destination or source in destination.parents or destination in source.parents:
                raise KelmoError("E_INPUT", "Migration source and destination roots must not overlap.")


def summary(manifest):
    return {"snapshot_id": manifest["id"], "sha256": manifest["sha256"],
            "sources": {name: {"kind": row["kind"], "files": len(row.get("files", {})),
                               "archive_versions": len(row.get("archive") or ())}
                        for name, row in manifest["sources"].items()}}


def roots_spec(value):
    if not isinstance(value, dict) or set(value) != {"state_root", "archive_root", "knowledge_git_root"}:
        raise KelmoError("E_INPUT", "Name state_root, archive_root and knowledge_git_root.")
    roots = {name: absolute(path) for name, path in value.items()}
    paths = list(roots.values())
    for index, path in enumerate(paths):
        separate([path], paths[index + 1:])
    return roots


def snapshot_command(root, args):
    from . import snapshot
    action = args.action
    if action == "report":
        from .migration_report import bundle
        return bundle(root, args.destination, args.include, apply=args.apply)
    if action == "create":
        sources = read_spec(args.spec)
        if not sources:
            raise KelmoError("E_INPUT", "Name at least one snapshot source.")
        for name, row in sources.items():
            snapshot._plain_name(name)
            if (not isinstance(row, dict) or set(row) != {"kind", "path"}
                    or row["kind"] not in snapshot.SOURCE_KINDS):
                raise KelmoError("E_INPUT", "Each source needs kind and path.")
            row["path"] = absolute(row["path"])
            if not row["path"].exists():
                raise KelmoError("E_INPUT", "A snapshot source does not exist.")
        destination = absolute(args.destination)
        separate([row["path"] for row in sources.values()], [destination])
        if not args.apply:
            return {"applied": False, "destination": str(destination), "sources": sorted(sources),
                    "requires_stopped_writers": True}
        if not args.writers_stopped:
            raise KelmoError("E_INPUT", "Stop the source writers and pass --writers-stopped.")
        return dict(summary(snapshot.take_snapshot(sources, destination)), applied=True)
    if action in ("verify", "inventory"):
        selected = absolute(args.snapshot)
        if action == "verify":
            return dict(summary(snapshot.verify_snapshot(selected)), verified=True)
        result = snapshot.inventory(selected, queues=args.queue)
        if not args.details:
            result = {key: value for key, value in result.items() if key != "items"}
        return result
    if action == "import":
        spec = read_spec(args.spec, {"archive", "vault", "journal", "roots", "authority_id", "label",
                                   "registry_relative", "wiki_relative", "settings", "state_sources", "state_sessions",
                                   "state_telemetry"})
        if not {"archive", "vault", "roots"} <= set(spec):
            raise KelmoError("E_INPUT", "Import needs archive and vault source names and destination roots.")
        spec["roots"] = roots_spec(spec["roots"])
        for key in ("wiki_relative", "registry_relative"):
            value = spec.get(key)
            if value is not None and (not isinstance(value, str) or not value or Path(value).is_absolute()
                                      or ".." in Path(value).parts):
                raise KelmoError("E_INPUT", key + " must be a relative path inside the knowledge root.")
        selected, destination = absolute(args.snapshot), absolute(root)
        separate([selected], [destination, *spec["roots"].values()])
        separate([destination], list(spec["roots"].values()))
        manifest = snapshot.verify_snapshot(selected)
        if not args.apply:
            return dict(summary(manifest), applied=False, destination=str(destination),
                        roots={key: str(path) for key, path in spec["roots"].items()})
        state = snapshot.import_snapshot(selected, destination, **spec)
        return {"applied": True, "snapshot_id": state["snapshot"], "authority_id": state["authority_id"],
                "steps": sorted(state["steps"])}
    if action == "backup":
        from .config import Config
        if not args.apply:
            config = Config.load(root)
            return {"applied": False, "destination": str(absolute(args.destination)), "keep": args.keep,
                    "sources": sorted(snapshot.vault_sources(config)), "requires_stopped_writers": True,
                    "last_backup": snapshot.last_backup(config)}
        if not args.writers_stopped:
            raise KelmoError("E_INPUT", "Fence the server, stop the workers and pass --writers-stopped.")
        return dict(snapshot.backup(root, absolute(args.destination), keep=args.keep), applied=True)
    if action == "mark-cutover":
        if not args.apply:
            return {"applied": False, "path": str(Path(root) / snapshot.CUTOVER)}
        return {"applied": True, "marked_at": snapshot.mark_cutover(root)["marked_at"]}
    if action == "tail":
        return snapshot.tail(root)
    if action == "rollback-check":
        return snapshot.rollback_check(root)
    if action == "recover":
        from .config import Config
        config = Config.load(root)
        spec = read_spec(args.spec, {"roots"})
        roots = roots_spec(spec.get("roots"))
        selected, destination = absolute(args.snapshot), absolute(args.destination)
        separate([config.root, config.state, config.archive.parent, config.knowledge_git_root],
                 [selected, destination, *roots.values()])
        separate([selected, destination], list(roots.values()))
        separate([selected], [destination])
        if not args.apply:
            return {"applied": False, "snapshot": str(selected), "destination": str(destination),
                    "requires_stopped_writers": True}
        if not args.writers_stopped:
            raise KelmoError("E_INPUT", "Stop the source writers and pass --writers-stopped.")
        recovered = snapshot.forward_recover(root, selected, destination, roots=roots)
        return {"applied": True, "destination": str(destination),
                "tail": {key: len(value) for key, value in recovered["tail"].items()
                         if isinstance(value, list)}}
    raise KelmoError("E_INPUT", "Unknown snapshot action.")

