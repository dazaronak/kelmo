#!/bin/sh
# Runs the ten frozen acceptance scenarios of docs/TARGET-STATE.md Part 2, in order.
# Prints one PASS/FAIL line per scenario and a summary; exits 1 if any scenario failed.
#
#   KELMO_MODEL_DIR=/path/to/model tests/acceptance/run.sh
#
# Optional: KELMO_PYTHON (interpreter with the locked dependencies; default python3),
#           KELMO_ACCEPTANCE_ROOT (where vaults are created), KELMO_ACCEPTANCE_KEEP=1.
set -u
here=$(cd "$(dirname "$0")" && pwd)
python=${KELMO_PYTHON:-python3}
if [ -z "${KELMO_MODEL_DIR:-}" ]; then
  echo "KELMO_MODEL_DIR is not set; it must name the directory with the five pinned model files" >&2
  exit 2
fi
failed=0
passed=0
for script in "$here"/a0[1-9]_*.py "$here"/a10_*.py; do
  if "$python" "$script"; then
    passed=$((passed + 1))
  else
    failed=$((failed + 1))
  fi
done
echo "acceptance: $passed passed, $failed failed"
[ "$failed" -eq 0 ]
