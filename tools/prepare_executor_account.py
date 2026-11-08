#!/usr/bin/env python3
"""Prepare fixed cross-account executor files; never install a sudo rule."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--provider-account", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--provider", choices=("claude", "codex", "custom"), required=True)
    parser.add_argument("--provider-executable", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for account in (args.service_account, args.provider_account):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", account) or account == "root":
            parser.error("Select explicit non-root Linux account names.")
    if args.service_account == args.provider_account:
        parser.error("Separate accounts are required only when the provider login has another owner.")
    if args.provider != "custom" and not args.model:
        parser.error("A native provider needs an explicit model.")
    if args.provider == "codex" and args.effort == "max":
        parser.error("Codex supports effort through xhigh.")
    for path in (args.runtime, args.provider_executable, args.output):
        if not path.is_absolute() or ".." in path.parts:
            parser.error("Use canonical absolute paths.")
    if any(p.is_symlink() for p in (args.output, *args.output.parents)):
        parser.error("The local output directory cannot traverse symlinks.")
    if args.output.exists():
        parser.error("Use a new output directory; retain earlier prepared files.")
    executable = args.runtime / "bin/kelmo"
    argv = ["/usr/bin/env", "-u", "OPENAI_API_KEY", "-u", "ANTHROPIC_API_KEY",
            "-u", "CODEX_HOME", "-u", "CLAUDE_CONFIG_DIR", str(executable), "executor",
            "--provider", args.provider, "--provider-executable", str(args.provider_executable),
            "--timeout", "170"]
    if args.model:
        argv += ["--model", args.model]
    if args.effort:
        argv += ["--effort", args.effort]
    wrapper = "#!/bin/sh\nset -eu\n[ \"$#\" -eq 0 ] || exit 64\nexec " + shlex.join(argv) + "\n"
    target = "/usr/local/libexec/kelmo-executor"
    sudoers = f'{args.service_account} ALL=({args.provider_account}) NOPASSWD: {target} ""\n'
    config = {"argv": ["/usr/bin/sudo", "-n", "-H", "-u", args.provider_account, "--", target],
              "timeout_seconds": 180, "max_attempts": 3, "poll_seconds": 2}
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    files = {"kelmo-executor": wrapper,
             "kelmo-executor.sudoers": sudoers,
             "executor.json": json.dumps(config, indent=2) + "\n"}
    for name, text in files.items():
        (args.output / name).write_text(text)
    (args.output / "kelmo-executor").chmod(0o755)
    manifest = {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest() for name in files}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"prepared": str(args.output), "installed": False,
                      "wrapper_target": target, "sudoers_target": "/etc/sudoers.d/kelmo-executor",
                      "next_step": "Review the files, validate with visudo, then install as administrator."}))


if __name__ == "__main__":
    main()
