"""A forced-command entry point with one administrator-selected client identity."""
import argparse
import os
from pathlib import Path
import re
import sys


def command(executable, vault, profile, client_id, requested):
    if requested != "kelmo-client":
        raise ValueError("Only the kelmo-client command is allowed.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", client_id):
        raise ValueError("The configured client identity is invalid.")
    paths = [Path(value) for value in (executable, vault, profile)]
    if not all(path.is_absolute() for path in paths):
        raise ValueError("Gate runtime and storage paths must be absolute.")
    if not paths[0].is_file() or not os.access(paths[0], os.X_OK):
        raise ValueError("The configured Kelmo executable is unavailable.")
    return [str(paths[0]), "--vault", str(paths[1]), "serve-client",
            "--profile", str(paths[2]), "--client-id", client_id]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args()
    try:
        argv = command(args.executable, args.vault, args.profile, args.client_id,
                       os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 126
    os.execv(argv[0], argv)


if __name__ == "__main__":
    raise SystemExit(main())
