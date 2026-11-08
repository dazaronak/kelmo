#!/bin/sh
# Measurement gate for kelmo: the CLI, the service, the wiki compiler, the graph, sessions, quota,
# removal and the installer, the ten frozen acceptance scenarios, and the three lanes CI keeps out of the suite.
# Self-contained: ROOT is this directory, wherever the checkout is copied.
#
# The sections are data, in gate.json; tools/gate.py is the bundled measurement runner.
# gate.json names each section, its kind and the prerequisites it needs, and the runner probes every need by
# capability and prints what it found in the header: the interpreter and the locked modules it carries, the
# model directory and the installed prefix named by the environment, bubblewrap at the two paths
# src/kelmo/linux_runtime.py looks at, and whether this host creates the unprivileged user namespace
# bubblewrap builds its sandbox from. The shell files are found by their shebang and not by their suffix,
# because the installed launcher here is packaging/kelmo.
#
# Usage: bash run-tests.sh [--only 1,2,3,4,5,6,7,8] [--list] | --adoption [pytest options]
# GATE_PYTHON names the interpreter that runs the runner and the suite. With none set the gate takes this
#   folder's own .venv/bin/python when uv sync has built one, and the PATH python3 otherwise; the suite is the
#   locked environment pyproject.toml and uv.lock fix, so an interpreter that carries none of those modules
#   turns the sections that need them into SKIPs, and the header names which one ran.
# GATE_WITHOUT=<need ids gate.json declares> reports a prerequisite absent whatever the machine has, which is
#   how a SKIP branch is measured instead of trusting that the tool happens to be missing.
# GATE_BASH32 names the bash to try first, on a host that keeps 3.2 somewhere other than /bin/bash.
# KELMO_GATE_MODEL_DIR or KELMO_MODEL_DIR names the directory holding the five pinned model files;
#   KELMO_TEST_PREFIX names the prefix whose bin/kelmo the installed-CLI lane runs (default .venv).
# Every section is bounded: gate.json `timeout` seconds, 300 when it names none. Past the limit the runner
#   kills the group it started for that section, reports the section as a red and goes on; Ctrl-C does the
#   same to the whole gate, so a hung section is a finding instead of a gate that never comes back.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$ROOT/tools/gate.py"
if [ ! -f "$RUNNER" ]; then
    echo "kelmo gate: runner is missing: $RUNNER" >&2
    echo "  Use a complete checkout or extracted source distribution, including tools/gate.py." >&2
    exit 2
fi
PY="${GATE_PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PY="$ROOT/.venv/bin/python"
    else
        PY="python3"
        echo "kelmo gate: no .venv here and no GATE_PYTHON, so the PATH python3 runs the gate." >&2
        echo "  Run \`uv sync\` in this folder for the locked environment the suite is written against." >&2
    fi
fi
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "kelmo gate: $PY is not runnable. This suite is the locked environment pyproject.toml names:" >&2
    echo "  build it with \`uv sync\` in this folder, or name another one with GATE_PYTHON." >&2
    exit 2
fi
# The two lanes this gate loads in-process import kelmo from src/, and the suite's own child starts with
# its cwd inside tests/, so the absolute path has to be in the environment the runner and its children share.
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# The installed-CLI probe asks the question tests/test_installed_cli.py asks, whose default is this .venv.
KELMO_TEST_PREFIX="${KELMO_TEST_PREFIX:-$ROOT/.venv}"
export KELMO_TEST_PREFIX
if [ "${1:-}" = "--adoption" ]; then
    shift
    if [ ! -f "${KELMO_TEST_WHEEL:-}" ] || [ ! -f "${KELMO_TEST_SDIST:-}" ] ||
       [ ! -x "$KELMO_TEST_PREFIX/bin/kelmo" ]; then
        echo "kelmo adoption: select a freshly built wheel, sdist and installed runtime." >&2
        echo "  Set KELMO_TEST_WHEEL, KELMO_TEST_SDIST and KELMO_TEST_PREFIX." >&2
        exit 2
    fi
    while IFS= read -r ADOPTION_CASE; do
        [ -n "$ADOPTION_CASE" ] || continue
        set -- "$@" "$ROOT/$ADOPTION_CASE"
    done < "$ROOT/tests/adoption-checks.txt"
    exec "$PY" -u -m pytest -v -ra --durations=10 "$@"
fi
exec "$PY" -u "$RUNNER" "$ROOT" "$@"
