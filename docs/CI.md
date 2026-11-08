# Continuous integration

Pushes and pull requests run one `ubuntu-24.04` / Python 3.13 lane. Release tags, the weekly schedule and a manual run add the full cross-product of `ubuntu-24.04` and `macos-26` with Python 3.12 and 3.13: four source lanes and four fresh installed-launcher lanes. Every lane uses the repository's locked dependencies.

Each source lane checks `uv.lock`, runs Ruff, and selects `src` explicitly for the default source profile. It excludes:

- `tests/test_runtime.py`
- `tests/test_runtime_macos.py`
- `tests/test_linux_runtime.py`
- `tests/test_installed_cli.py`

The first three files describe retained isolation experiments requiring separate host acceptance. Installed-launcher tests run separately; the default launcher uses the caller's OS permissions.

After all source lanes succeed, one `ubuntu-24.04` / Python 3.12 artifacts job builds the wheel and source distribution, checks deployment exclusions and source parity, and uploads `kelmo-dist` as a seven-day CI artifact. Each installed lane depends explicitly on source and artifacts, downloads that exact artifact by ID, verifies its ZIP digest, and installs the same wheel into an empty runtime with locked runtime constraints.

The installed test harness syncs dependencies with `--no-install-project`, selects installed site-packages, and runs from a temporary directory with `--import-mode=importlib --noconftest`. A preflight rejects project-package origins outside the fresh prefix. Fixture launcher copies come from the installation. Tests exercise CLI and stdio MCP graph navigation after a wiki edit and compare the checkout's Python modules, SQLite schema and shared entrypoints with both wheel and installed bytes. The missing-entry test checks incomplete-install refusal; it does not authenticate the interpreter.

All jobs retain a temporary `HOME`, synthetic Git identity, no client/provider credentials, and no model download. Existing action pins, the uv version, and dependency versions are unchanged. The added `actions/download-artifact` action is pinned to v8.0.0's commit and rejects digest mismatches. Artifact retrieval uses the ephemeral workflow token with `actions: read`, including when retrying failed jobs. The workflow does not publish a package or configure a native service, provider, model, or real user profile.

Both matrices use `fail-fast: false`. Clean-machine bootstrap, native launchd/systemd lifecycle, real Claude/Codex acceptance, and broader OS/CPU support remain separate from these component and packaging checks.

## Standalone measurement gate

Standalone source uses `bash run-tests.sh` as its test entry point. The bundled `tools/gate.py` reads
`gate.json`; it needs no parent checkout or Git metadata to start. `--list` lists targets without
running sections or probing host capabilities. `--only 1,2,3` selects syntax and lint, but still performs
the header's prerequisite probes. Unselected sections are not verified by that run.

The shell ownership check skips `_docs`, an untracked folder that no distribution ships.

The launcher uses unbuffered Python output, so completed section results reach
terminal monitors and piped logs immediately. A running section still collects
its child output before reporting its result.

For the bounded, non-database portability regression, select only this file:

```sh
PYTHONPATH=/absolute/path/to/kelmo/src python3.12 -B tests/test_gate_contract.py -v
```

Use the same command with Python 3.13 for the other supported interpreter. The cases retain lane and shell
coverage checks and exercise source copies, synthetic archive extraction, runner/interpreter discovery,
checkout imports ahead of a stale package, explicit five-file model inputs and wrapped counts. They do not
build a release artifact, run database/model/native tests or establish Linux compatibility by running on macOS.

For actual distribution verification, the controller first includes `tools/gate.py` in the candidate source
and builds with the existing locked toolchain. From the selected repository root:

```sh
uv build --no-sources
mkdir -p /path/to/empty-sdist-check
tar -xzf dist/kelmo-0.1.0.tar.gz -C /path/to/empty-sdist-check
GATE_PYTHON=/path/to/python bash /path/to/empty-sdist-check/kelmo-0.1.0/run-tests.sh --list
PYTHONPATH=/path/to/empty-sdist-check/kelmo-0.1.0/src /path/to/python -B /path/to/empty-sdist-check/kelmo-0.1.0/tests/test_gate_contract.py -v
```

Retain the complete archive contents. A wheel installs the runtime guard from `packaging/gate.py`; it does
not install this source measurement gate. Artifact parity and native acceptance remain separate checks.

The controller's selected commands for the unchanged measurement lanes, from this checkout:

```sh
# Source suite, including database tests; without a model its model cases can skip.
GATE_PYTHON=/path/to/python KELMO_GATE_MODEL_DIR=/path/to/model-dir bash run-tests.sh --only 4
# All ten frozen model-backed acceptance scenarios.
GATE_PYTHON=/path/to/python KELMO_GATE_MODEL_DIR=/path/to/model-dir bash run-tests.sh --only 5
# Experimental runtime/native lanes, on their supported host: 6 the runtime builder and the Linux kernel
# boundary, 8 the macOS kernel boundary (a Mac only).
GATE_PYTHON=/path/to/python bash run-tests.sh --only 6,8
# Fresh installed runtime; pass the model explicitly for the hiding case. Without KELMO_TEST_PREFIX the
# lane probes .venv/bin/kelmo, which is a console script answering E_RUNTIME, and reports a SKIP that says so.
GATE_PYTHON=/path/to/python KELMO_GATE_MODEL_DIR=/path/to/model-dir KELMO_TEST_PREFIX=/path/to/runtime bash run-tests.sh --only 7
# Existing artifact parity cases, with freshly built artifacts and a fresh selected runtime.
PYTHONPATH=/absolute/path/to/kelmo/src KELMO_TEST_WHEEL=/absolute/path/to/kelmo/dist/kelmo-0.1.0-py3-none-any.whl KELMO_TEST_SDIST=/absolute/path/to/kelmo/dist/kelmo-0.1.0.tar.gz KELMO_TEST_PREFIX=/path/to/runtime /path/to/python -m pytest tests/test_distribution.py -q -ra
```

Run supported host checks on macOS and Linux with Python 3.12 and 3.13. `KELMO_GATE_MODEL_DIR` wins over
`KELMO_MODEL_DIR`; neither variable has a deployment fallback. A selected directory must hold the
five pinned files. Missing prerequisites and suite self-skips remain SKIP, with reasons and uncovered work;
an exit code of zero with SKIPs is not complete acceptance.

## Bounded adoption checks

`run-tests.sh --adoption` reuses the checks for installation, provider selection,
capture/publication, shared transport, recovery and packaged runtime parity.
Build the current source and install its wheel into a disposable prefix first.
Set `KELMO_TEST_WHEEL`, `KELMO_TEST_SDIST` and
`KELMO_TEST_PREFIX` to those exact artifacts and runtime, then run the
command once. Missing artifact prerequisites fail before tests start.

This selection does not replace target-host, native-client or release-matrix
acceptance. Existing native confinement checks remain opt-in and use synthetic data.

## The gate in detail

```bash
bash run-tests.sh --list                          # list targets without probing or running them
bash run-tests.sh --only 1,2,3                     # syntax and lint
KELMO_GATE_MODEL_DIR=/path/to/model-dir bash run-tests.sh --only 4,5
```

`bash run-tests.sh` is the test entry point in a standalone checkout or extracted source distribution.
It calls the bundled `tools/gate.py` with this folder as its argument, and the sections are
data in `gate.json` — 1 syntax (`bash -n`, and under bash 3.2 too, over every file here whose first line names a
shell: `run-tests.sh`, `tests/acceptance/run.sh` and the two launchers in `packaging/`), 2 `ruff check .`,
3 `shellcheck` over the same files, 4 the source suite (the pytest command `docs/TARGET-STATE.md` 1.4 names, with
the four lane files sections 6, 7 and 8 measure ignored and `PYTHONPATH` absolute because
some tests start their child inside `tests/`), 5 the acceptance harness
(`tests/acceptance/run.sh`, the ten frozen scenarios of `docs/TARGET-STATE.md` Part 2), 6 the two lanes CI keeps
out of the source suite, 7 the installed CLI lane, 8 the macOS kernel boundary (`tests/test_runtime_macos.py`, the
seatbelt a confined child runs under), which every host but a Mac reports absent. No parent checkout or Git metadata is needed to start the
gate. Keep `tools/gate.py`, `gate.json`, `src/`, `tests/` and `packaging/` with `run-tests.sh` when copying source.
`packaging/gate.py` is a separate runtime request guard, not this measurement runner. The gate is distributed
with source, not as an installed wheel command. Running without `--only` selects all eight sections, including
database, model and native runtime checks; `--list` only validates configuration and lists targets.
The header prints what the machine answered for every
prerequisite `gate.json` declares: the interpreter and the locked modules it carries, the model directory — the
first set variable among `KELMO_GATE_MODEL_DIR` and `KELMO_MODEL_DIR`, asked to hold the five pinned files.
There is no deployment-directory fallback: an absent or incomplete explicit directory leaves model acceptance
unmeasured. The header also names the installed prefix, bubblewrap at the
two paths `src/kelmo/linux_runtime.py` looks
at, and whether this host creates the unprivileged user namespace bubblewrap builds its sandbox from. The
interpreter is picked the same neutral way — `GATE_PYTHON` when it is set, this folder's own `.venv/bin/python`
when `uv sync` has built one, and the `python3` on PATH otherwise, with a line on stderr saying so. Sections 4 and 5
measure their own prerequisites: run with `GATE_PYTHON=python3` on a host whose PATH interpreter carries none of
the locked modules, section 4 is a SKIP naming `fastmcp`, `fastembed`, `networkx` and `markdown-it-py` as absent
rather than a suite that dies at import, and section 5 is a SKIP naming those same modules rather than the
acceptance harness printing `1 passed, 9 failed` for want of an embedding model. A section
whose prerequisite is absent is a SKIP with the reason and what it leaves uncovered; a suite that let cases skip
themselves is a SKIP too, carrying its counts line and the skip reasons, because a passed count beside a skipped
count is not the measurement the collected total makes. `GATE_WITHOUT=modeldir,bwrap,prefix,shellcheck,ruff`
reports a prerequisite absent
whatever the machine has. `tests/test_gate_contract.py` keeps this folder's own contract — the lanes the suite
ignores are the lanes `ci.yml` ignores and the sections run, and every shell file here is in reach of both shell
checks — plus standalone listing, copied and extracted source, interpreter discovery, checkout imports ahead
of a stale wheel, explicit model inputs and synthetic wrapped PASS/FAIL/SKIP behavior. These bounded cases
execute no database, embeddings or native runtime tests. The sections above give their selected command and
for the build and host checks still needed. The bubblewrap red is earned: it stands only while the failure in
`tests/test_linux_runtime.py` is the `IsolationError` that names the tool. That lane is measured on the Linux
hosts the runtime is built for, not on any host that has the binary: bwrap builds the sandbox from an unprivileged
user namespace, so the gate asks the host whether it creates one (`unshare -U -r true`) and prints what it found —
a host that refuses `unshare(CLONE_NEWUSER)` is not one missing tool away from the measurement. That lane is also
the only place this folder measures that another process's arguments are unreadable: `macos_policy()` allowlists
`sysctl-read` by name and names no `kern.procargs2` rule, and a macOS run of this gate read
`SYNTHETIC_ONLY=test-value` out of another process's arguments through the confined launcher, so the claim is not
made on macOS and `gate.json` names the gap under `UNMEASURED by this run:`.

Section 7 hands its child the model directory the header resolved (`KELMO_MODEL_DIR: "$modeldir"`), because
the installed hiding case (`test_installed_forget_hides_the_record_from_every_reader`) archives through the
installed launcher and then reads the record back through that launcher's own hybrid `search`, `list`, `show`,
`trace`, `status` and `doctor`: a hybrid answer needs the pinned model, so the case skips itself naming
`KELMO_MODEL_DIR` on a host that holds none — which is every CI lane, since `docs/CI.md` downloads no model.

Two files measure an interface rather than one implementation. `tests/test_timings.py` runs the CLI in a child
process and reads the stage line back off stderr: the flag before or after the command name, the environment switch,
no line when nobody asked, no `model_load_ms` for a call that loaded no model — the number belongs to the call
that paid for it, so a process already warm when the call began names none — and `--vault` still winning over
`KELMO_VAULT`.
`tests/test_mcp_cli_parity.py` asks the MCP server and the CLI the same question of one fixture
vault — an unfiltered search, five filtered shapes, and a search after a write made through the server — and requires
the records, their order and `search` to match, including the degraded lexical answer; its model-backed case, which
checks that a server loads the model at start and names no load per call, skips naming `KELMO_MODEL_DIR` (or
`KELMO_GATE_MODEL_DIR`) when the host names no directory holding the five pinned files, and runs when one is named.
`tests/test_gate.py` keeps `packaging/gate.py` refusing `--timings` and clearing it from the child's environment, so
a remote request cannot change the shape of the answer the gate returns.
`tests/test_cli.py`, `tests/test_user_launcher.py` and `tests/test_bootstrap_prompt.py` answer `kelmo init`'s vault
question at a real tty — the first two through `python -m kelmo.cli` and through the installed launcher, the
third by running `src/kelmo/bootstrap.py` the way `packaging/kelmo-isolated` does, which is the only way that
file's prompt branch is reachable in a test on a host without bubblewrap — and each names the vault
`KELMO_VAULT` set rather than the default directory.

Name the two artifacts a build of this folder produces — `KELMO_TEST_WHEEL` and `KELMO_TEST_SDIST`, the
`dist/kelmo-*.whl` and `dist/kelmo-*.tar.gz` a build leaves here —
and the two parity cases in `tests/test_distribution.py` stop skipping themselves, and the source suite section
runs them with the rest.

That prefix is an ordinary install of this folder into a virtualenv of your own, built outside the checkout.
With network access the plain install is enough; offline, point `--find-links` at a wheelhouse that
carries kelmo's locked closure plus its build backend, hatchling. Name the prefix to the gate with
`KELMO_TEST_PREFIX`:

```bash
python3 -m venv /path/to/runtime
/path/to/runtime/bin/pip install .                                            # with network
/path/to/runtime/bin/pip install --no-index --find-links /path/to/wheelhouse .  # offline alternative
KELMO_TEST_PREFIX=/path/to/runtime KELMO_GATE_MODEL_DIR=/path/to/model-dir bash run-tests.sh --only 7
```

The lane measures the prefix and not the checkout: the installed launcher starts Python with `-I -S`, so a change
under `src` reaches it only when the install line is repeated.
