#!/usr/bin/env python3
"""Rehearse an embedded consumer upgrade using only a new synthetic store."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def check(condition, message):
    """Fail the rehearsal; unlike assert, this still runs under python -O."""
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-kelmo", type=Path, required=True)
    parser.add_argument("--new-kelmo", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.old_kelmo, args.new_kelmo, args.work_dir):
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            parser.error("Use absolute paths without symlinks.")
    if args.work_dir.exists():
        parser.error("Work directory must be new; no existing store is accepted.")
    for executable in (args.old_kelmo, args.new_kelmo):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            parser.error("Both installed Kelmo executables must exist.")
    args.work_dir.mkdir(parents=True, mode=0o700)
    config = args.work_dir / "gitconfig"
    config.write_text('[user]\nname = Upgrade Fixture\nemail = upgrade@example.invalid\n')
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_SYSTEM=os.devnull)
    old, candidate = args.work_dir / "original", args.work_dir / "candidate"
    started = time.monotonic()
    log = args.work_dir / "rehearsal.log"
    results = []

    def record(label):
        line = f"PASS {time.monotonic() - started:.2f}s [{len(results) + 1}/5] {label}"
        results.append(label)
        print(line, flush=True)
        with log.open("a") as output:
            output.write(line + "\n")

    def cli(executable, root, *argv, payload=None):
        result = subprocess.run([str(executable), "--vault", str(root), *argv, "--json"],
                                input=json.dumps(payload) if payload is not None else None,
                                env=environment, text=True, capture_output=True, timeout=60, check=False)
        try:
            answer = json.loads(result.stdout)
        except ValueError as error:
            raise RuntimeError("Installed command returned no JSON response.") from error
        if result.returncode or not answer.get("ok"):
            raise RuntimeError("Installed command failed: " + str(answer.get("error", {}).get("code")))
        return answer["result"]

    def compat(executable, root, tool, payload):
        return cli(executable, root, "compat", tool, payload=payload)["result"]

    cli(args.old_kelmo, old, "init", "--label", "upgrade-fixture")
    # This is deliberately hand-maintained legacy HOT, preserved across a runtime upgrade.
    rules = old / "knowledge/wiki/rules.md"
    rules.write_text("# Fixture rules\n\nKeep this local instance separate from other authorities.\n")
    with (old / "kelmo.toml").open("a") as output:
        output.write('\n[hot]\npages = ["rules.md"]\nmax_bytes = 48000\n')
    for argv in (["config", "user.name", "Upgrade Fixture"],
                 ["config", "user.email", "upgrade@example.invalid"],
                 ["add", "--", "kelmo.toml", "knowledge/wiki/rules.md"],
                 ["commit", "-qm", "chore: Configure synthetic local HOT"]):
        subprocess.run(["git", "-C", str(old), *argv], env=environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stored = compat(args.old_kelmo, old, "memory_store",
                    {"content": "Synthetic local rule, original.", "kind": "rule", "domain": "dev"})
    compat(args.old_kelmo, old, "memory_update",
           {"id": stored["id"], "content": "Synthetic local rule, corrected.", "expected_version": 1})
    record("Old runtime created a local record and correction")
    before = {name: compat(args.old_kelmo, old, name, {"id": stored["id"], **options})
              for name, options in (("memory_get", {"full": True}), ("memory_history", {}))}
    old_status = cli(args.old_kelmo, old, "status")
    old_hot = cli(args.old_kelmo, old, "hot")
    shutil.copytree(old, candidate)
    record("Stopped synthetic store copied; original retained")
    after = {name: compat(args.new_kelmo, candidate, name, {"id": stored["id"], **options})
             for name, options in (("memory_get", {"full": True}), ("memory_history", {}))}
    check(before == after, "Record identity, correction or exact history changed.")
    new_status = cli(args.new_kelmo, candidate, "status")
    check(old_status["vault_id"] == new_status["vault_id"], "The vault identity changed.")
    check(cli(args.new_kelmo, candidate, "hot")["context"] == old_hot["context"], "The HOT context changed.")
    check((candidate / "knowledge/wiki/rules.md").read_bytes() == rules.read_bytes(), "The wiki rules page changed.")
    record("New runtime preserved exact record/history, authority and HOT")
    compat(args.new_kelmo, candidate, "memory_store",
           {"content": "Synthetic write after upgrade.", "kind": "event", "domain": "dev"})
    check(cli(args.new_kelmo, candidate, "status")["archive_records"] == old_status["archive_records"] + 1,
          "The candidate did not keep the new write.")
    check(cli(args.old_kelmo, old, "status")["archive_records"] == old_status["archive_records"],
          "The write reached the original store.")
    record("Candidate accepts local writes without touching the original")
    report = {"synthetic_only": True, "old_executable": str(args.old_kelmo),
              "new_executable": str(args.new_kelmo), "checks": results,
              "source_content_sha256": hashlib.sha256(rules.read_bytes()).hexdigest(),
              "original": str(old), "candidate": str(candidate),
              "recovery": "Keep candidate writes; the original is not a post-write rollback target."}
    (args.work_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    record("Report saved; no real consumer data or remote route accessed")


if __name__ == "__main__":
    main()
