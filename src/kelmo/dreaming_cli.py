"""Bounded operator commands for the existing Dreaming runner."""
import fcntl
import json
import plistlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .config import Config
from .creation import CommandExecutor
from .daemon import validate_config
from .errors import KelmoError
from .migration_cli import read_spec
from .service import Service


def status(root):
    config = Config.load(root)
    with closing(sqlite3.connect((config.state / "journal.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        values = {row["key"]: json.loads(row["value"]) for row in connection.execute(
            "SELECT key,value FROM dream_state")} if "dream_state" in tables else {}
        counts = {row["state"]: row["n"] for row in connection.execute(
            "SELECT state,COUNT(*) AS n FROM dream_findings GROUP BY state")} if "dream_findings" in tables else {}
        recent = connection.execute(
            "SELECT run_id,state,started_at,finished_at FROM dream_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone() if "dream_runs" in tables else None
    return {"settings": values.get("settings"), "paused": values.get("paused", not bool(values.get("settings"))),
            "next_attempt": values.get("next_attempt", 0), "findings": counts,
            "last_run": dict(recent) if recent else None}


def prepare_schedule(root, args):
    from .install.setup import write
    config = Config.load(root)
    current = status(root)
    if not current["settings"]:
        raise KelmoError("E_CONFIG", "Configure Dreaming before preparing its schedule.")
    executable = Path(args.executable).expanduser()
    directory = Path(args.directory).expanduser()
    if not executable.is_absolute() or not directory.is_absolute() or args.interval < 60:
        raise KelmoError("E_INPUT", "Use absolute schedule paths and an interval of at least 60 seconds.")
    argv = [str(executable), "--vault", str(config.root), "dream", "run", "--json"]
    if args.server_profile:
        if not args.client_id:
            raise KelmoError("E_ORIGIN", "A shared schedule needs an enrolled --client-id.")
        argv += ["--server-profile", str(args.server_profile), "--client-id", args.client_id]
    if any(any(character in item for character in ("\n", "\r", "\0")) for item in argv):
        raise KelmoError("E_INPUT", "Schedule arguments cannot contain control characters.")
    if args.platform == "darwin":
        files = {"org.kelmo.dream.plist": plistlib.dumps({
            "Label": "org.kelmo.dream", "ProgramArguments": argv,
            "StartInterval": args.interval, "RunAtLoad": False,
            "StandardOutPath": str(config.state / "dreaming.log"),
            "StandardErrorPath": str(config.state / "dreaming-errors.log")})}
    elif args.platform == "linux":
        command = " ".join('"' + item.replace("\\", "\\\\").replace('"', '\\"')
                           .replace("%", "%%").replace("$", "$$") + '"' for item in argv)
        files = {"kelmo-dream.service": (
            "[Unit]\nDescription=Bounded Kelmo Dreaming\n[Service]\nType=oneshot\nExecStart="
            + command + "\n").encode(),
            "kelmo-dream.timer": (
                "[Unit]\nDescription=Periodic Kelmo Dreaming\n[Timer]\n"
                f"OnBootSec={args.interval}s\nOnUnitActiveSec={args.interval}s\n"
                "Persistent=false\n[Install]\nWantedBy=timers.target\n").encode()}
    else:
        raise KelmoError("E_INPUT", "Schedule platform must be linux or darwin.")
    results = [write(directory / name, data) for name, data in files.items()] if args.apply else []
    return {"applied": args.apply, "files": [str(directory / name) for name in files],
            "backups": [row["backup"] for row in results if row["backup"]],
            "interval_seconds": args.interval, "paused": current["paused"],
            "scheduler_started": False}


def dispatch(root, args):
    configured = None
    if args.action == "configure":
        for name in ("max_sources", "max_pages", "budget_tokens"):
            if getattr(args, name) <= 0:
                raise KelmoError("E_INPUT", "Dreaming ceilings must be positive integers.")
        path = Path(args.executor_config).expanduser().absolute()
        if not validate_config(read_spec(path))["argv"]:
            raise KelmoError("E_CONFIG", "Dreaming requires a configured executor command.")
        configured = {"executor_config": str(path), "max_sources": args.max_sources,
                      "max_pages": args.max_pages, "budget_tokens": args.budget_tokens}
    if args.server_profile:
        from .server import ServerProfile, shared_service
        profile = ServerProfile.load(args.server_profile)
        if profile.bindings.authority_id != Config.load(root).vault_id:
            raise KelmoError("E_AUTHORITY", "The server profile belongs to another vault.")
    elif args.client_id:
        raise KelmoError("E_INPUT", "--client-id requires --server-profile.")
    if args.action == "status":
        return status(root)
    if args.action == "schedule":
        return prepare_schedule(root, args)
    if args.action == "configure" and not args.apply:
        return {"applied": False, "settings": configured, "paused": status(root)["paused"],
                "provider_called": False, "scheduler_started": False}
    if args.server_profile:
        if args.action in ("run", "apply") and not args.client_id:
            raise KelmoError("E_ORIGIN", "Shared Dreaming runs need an enrolled --client-id.")
        service = shared_service(root, profile, args.client_id)
        contexts = profile.contexts
    else:
        service, contexts = Service(root), None
    with service:
        dream = service.dreaming
        settings = dream._state("settings")
        if args.action == "configure":
            settings = configured
            paused = dream._state("paused", True)
            if args.apply:
                service.archive_api.assert_writer()
                dream._set_state("settings", settings)
                dream._set_state("paused", paused)
            return {"applied": args.apply, "settings": settings, "paused": paused,
                    "provider_called": False, "scheduler_started": False}
        if args.action in ("pause", "resume"):
            if args.action == "resume" and not settings:
                raise KelmoError("E_CONFIG", "Configure Dreaming before resuming it.")
            service.archive_api.assert_writer()
            (dream.pause if args.action == "pause" else dream.resume)()
            return {"paused": args.action == "pause"}
        if args.action == "findings":
            return {"findings": dream.findings(args.state)}
        if not settings:
            raise KelmoError("E_CONFIG", "Configure Dreaming before running it.")
        with service.config.internal(".state/dreaming.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"state": "busy"}
            return run_dream(service, args.action, contexts=contexts)


def run_dream(service, action, *, contexts=None):
    """One Dreaming `run` (waiting notes, then the findings pass) or `apply` (the waiting notes only).
    The caller makes sure no other Dreaming run is in progress."""
    dream = service.dreaming
    settings = dream._state("settings")
    if not settings:
        return {"state": "unconfigured"}
    if dream._state("paused", True):
        return {"state": "paused"}
    reset = dream._state("next_attempt", 0)
    if reset > time.time():
        return {"state": "deferred", "reset_at": reset}
    config = validate_config(read_spec(settings["executor_config"]))
    executor = CommandExecutor(config["argv"], config["timeout_seconds"])
    service.archive_api.assert_writer()
    # Waiting notes go first, so the findings pass reads the pages they changed; `apply` alone
    # is the owner's "run the editor now".
    from .creation import apply_pending
    applied = apply_pending(service, executor)
    if action == "apply":
        return {"state": "finished", "apply": applied}
    result = dream.run(executor, origin=service.origin, contexts=contexts,
                       **{name: settings[name] for name in ("max_sources", "max_pages", "budget_tokens")})
    service.archive_api.assert_writer()
    dream._set_state("next_attempt", result.get("reset_at", 0) if result["state"] == "deferred" else 0)
    return dict(result, apply=applied)
