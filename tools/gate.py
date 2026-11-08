#!/usr/bin/env python3
"""The gate runner behind run-tests.sh.

A folder's gate is still `bash <folder>/run-tests.sh`; that script is a call to this file with the folder as
its argument, and the folder's sections live in `<folder>/gate.json`: each names itself, its kind and the
prerequisites it needs, so what a folder measures is data a person reads instead of shell to reverse-engineer.
One runner because every folder used to copy ok/fail/skip/unmeasured into its own run-tests.sh and read a
unittest suite by parsing the text unittest prints with sed - and a suite that echoes what a nested run said
(which is what every gate pin does when it fails) corrupts that parse while the gate still exits 0. Here the
suite runs through unittest's own API, so its counts, reds and skip reasons come from the result object.

Kinds: `unittest` (test files, or a discover start), `node` (the same two shapes for a folder whose suite is
`node --test`), `command` (an argv that must exit 0), `runner` (a wrapped suite whose own counts line is the
measurement), `syntax` (`bash -n` on the listed files, under bash 3.2 too), `shellcheck` (`shellcheck -s bash -S
warning`).

A `node` section names its test files the way a `unittest` section does — `tests`, or `discover` with a `start` and
a `pattern` (`*.test.mjs` when none is named) — and may `exclude` one of them to the section that knows what it
needs. Both python shapes import the same way: a `tests:` list puts the directory holding each file on `sys.path`
while that file is executed, which is what `discover` does with a start directory that is not a package, so a case
that imports its sibling helper reads the same in either shape. The derivation is the runner's and never node's own discovery, because a file node picked up by itself is a
file the gate cannot claim and would print a green over. The reporter is pinned to `tap` so a terminal and a pipe
print the same numbers, and the measurement is read off node's own summary block (`# tests`, `# pass`, `# fail`,
`# skipped`, `# todo`, `# cancelled`): a suite that collected nothing is a red, a summary whose parts do not add up
to its total is a red, a test cancelled is a red, and a test that skipped itself or is a `todo` is a SKIP naming it
with the reason node printed. The section needs a `node` need in gate.json and among its own, so a machine without
node skips it with the reason instead of running it with nothing.

A `syntax` or `shellcheck` section may name `shebang`, which changes what its `files` list may hold: a part may be
a directory, and stands for every shell file under it. A shell file is one whose first line names a shell, so the
installed launcher `packaging/kelmo` joins the parse and the lint beside `run-tests.sh` and a python file with no
`.py` does not join either. Dot-directories are not walked, because naming a folder is not naming the virtualenv
inside it.

A `syntax` section may also name `parse`, a second list whose parts (files, globs or directories) stand for the
entry points of a folder: every file there an interpreter is asked to run, classified by what it is — a shell
shebang is parsed with `bash -n` (and under bash 3.2 too, exactly as a `files` part is), a python shebang with
`ast.parse`, a `.json` file with `json.load`, and a file that names no interpreter is not an entry point and
joins nothing. A `.py` suffix names python the way a `.json` suffix names JSON, because the driver that runs
those files names the interpreter instead of their first line: a training driver runs its scripts as
`sys.executable /data/<dataset>/train.py`, and a parse section that skipped them printed a green over the
whole driver. A first line naming an interpreter this runner does not parse overrules the suffix. Extensionless
launchers are the shape this exists for: some are bash with no `.sh`,
others are python with no `.py`, and the configs a serving
container mounts are JSON, so a `*.sh` glob measures none of them. A parse section that derives an empty list is
a red: the folder would print a green having parsed nothing. The `files` list is held to the same: a syntax or
shellcheck section whose `files` match nothing in the folder is a red naming that list, asked before the
section's prerequisites so a host without shellcheck reports the same stale claim as one that has it.

A `runner` section runs each entry of its `runners` list and classifies it by what it printed, because a wrapped
runner's exit code is a convention it keeps and its counts line is the measurement it took: a runner that prints
`12 passed, 1 failed` beside exit 0 is a red, one that prints no counts line is a red, one that exits non-zero
while counting no failures is a red, one that wrote a scratch error (`mkdir:` and friends, which leaves its
counts meaningless) is a red, and one that probed its own prerequisite and printed SKIP is a SKIP carrying the
runner's own reason - `0 passed, 0 failed` beside exit 0 is the same hole as a run that measured nothing. The
counts line is found by the `counts` regex (default `N passed, M failed`), which must name the groups `passed`
and `failed`; a group that does not participate in the match counts as zero, so a runner that names only its
failures is classifiable too, and a line carrying neither number is not a counts line.

A wrapped program may also say out loud that this machine or checkout cannot run the measurement, with an exit code
of its own and a reason line: an evaluation may exit 2 when its corpus pins commits the checkout does not hold, or
when the history is too short for the window it measures. A section names that code
with `skip_exit`; when the program exits with it and prints its reason (found by `skip_line`) the section is a SKIP
carrying that line, and when it exits with that code naming no reason it is a red, because a SKIP a reader cannot
check is the same hole as a green that measured nothing. Without the code the missing counts line stays the red it
was: a program that fell over before it counted is not one that said why.

The `counts` regex may also name a third group, `skipped`: the cases the wrapped runner withdrew from itself. A
suite that ran N cases and let 2 skip themselves is not the same measurement as one that ran N + 2, and a counts
line that says `N passed, 2 skipped` beside exit 0 prints ok while two claims stay unmade, so such a section is
a SKIP: the counts line, the runner's own skip lines (found by `skip_line`) and `skip_leaves` say what the run
left unmeasured. A runner that withdrew every case is the same SKIP, which is why a counts line naming only
`skipped` counts as a counts line. What those cases measure is a different sentence from what the whole section
leaves when its prerequisite is absent, so a section may name `skipped_leaves` for them; without it the section's
`skip_leaves` stands. This reading comes before the runner's own SKIP line, because the counts name
how many were withdrawn and the lines only why; a section whose counts name no skips is still read by the SKIP
line above it.

A `runner` or `command` section may name `covers`: the test files, or the directories holding them, that the thing
it runs executes. The runner cannot see inside a suite it wraps, so without this a pytest file is a test file no
section claims - a red - and a folder whose suite is pytest could not use this runner at all. A directory stands
for every test file under it, however deep, because a suite that lives in a subdirectory is the same suite. A
covered file claimed again by a unittest section is the same double claim as any other, and a `covers` path the
folder does not hold is refused before anything runs. Such a section may also name `exclude`, the files inside those
directories that it does not run - the same key a `discover` section uses - so one file is claimed by the section
that knows what it needs from the machine and not by the section that ignores it. An excluded name no `covers`
entry of that section claims is refused: it is a claim about a file the section never had. A file and a claim are
the same thing only when their paths match, because one folder can hold two files of one name in two directories
and a claim that named only the last part would read that pair as one file claimed twice.

What counts as a test file is the folder's own convention, so gate.json may name `test_files`: the globs, relative
to the folder, whose matches must each be claimed by exactly one section. Without it the runner looks where the
python folders put theirs (every `test_*.py` under `tests/`, however deep, and the folder's root); a node folder
keeps them in `test/` named `*.test.mjs`, and a guard that cannot see them lets a suite sit there green. A folder
that names `test_files` and holds nothing matching is a red: the guard would print a green having claimed nothing.

A `unittest` `discover` section claims what its own `start` and `pattern` derive, the way the `node` kind does: a
pattern narrowed below the files under that start leaves the files it never collects unclaimed, because a
section that claimed the whole directory printed a green over a suite it had cut down to a third of its cases. The
walk descends only into a subdirectory that is a python package, because that is what the loader it mirrors does,
so a test file in a plain subdirectory is a file no section runs and the coverage guard's to name.

The same guard runs over the folder's shell files, found by their first line: a `#!/bin/sh` file the folder holds
and no section parses, lints or runs is a red naming it. A `*.sh` glob cannot see it, because the suffix is what
the file kept and not what it is — a launcher rewritten from python into a shell stub joins no glob, and the
folder prints a green having read it in no section. A section that starts a shell file by path (a wrapped runner,
a lint) counts as touching it, and a subdirectory that keeps a `gate.json` of its own is not walked, because that
folder's gate answers for its files. The guard names a second gap beside that one: a shell file whose only claim
comes from a `parse` walk is a red too, because a parse claim says only that bash can read the script. It is the
same hole one step later — a folder whose `*.sh` glob sits beside a `parse: ["."]` half claims the extension-less
launcher through the parse walk, so the folder stays green while what the script does goes unread. A `files` walk
or a section that starts the file closes the gap; a parse claim does not, because run_shellcheck is handed only a
shellcheck section's `files` list.


Claiming a file is half the contract; running a case out of it is the other half. A test file the section collects
that hands over no case - its cases deleted, its `TestCase` base taken away - is a red naming that file, because
the section would otherwise run its siblings, print `Ran 11` and call the folder measured. A file whose cases all
live in a class it imports from a sibling counts for it, because that is what the loader collects from it.

The run's tail is a counts line and then three blocks - `STANDING LISTS TO FIX`, `SKIPPED by this run:` and
`UNMEASURED by this run:` - each a heading with one item to a line. The blocks are what a reader of a gate is
there for, and items joined onto the heading read as one sentence about one thing: the next gap started mid-run-on
and no one could tell where the previous one ended.

A part of a `runner` or `command` section's argv may be `$<need>` - one of the folder's declared needs, which the
runner replaces with the program that need resolved to: the interpreter that probed the modules is the interpreter
the suite has to run on, and a section that named `python3` would run the suite on a different one than the header
printed. The need must be in that section's `needs`, so a machine without it skips the section instead of running
it with nothing, and a need with no program behind it (`module:`, `git:`) is refused where a program is asked for.
The `$` is what keeps the two namespaces apart: a section that runs `pytest` as an argument does not mean the
`pytest` need.

A `command` section or one entry of a `runner` section may name `cwd`: the directory its child starts in, relative
to the gate folder. A part of that argv that looks like a path is then held by that directory and not by the folder,
because the child looks for it where it starts: Go cases that run inside another module with an overlay file
in place of its main.go take that module as the section's cwd and its `-overlay` file is named from there. The claim
(`covers`) still belongs to the gate folder, because what the gate promises to measure is the folder's own file.

A need may carry `costs`: the sentence naming what its absence takes from the sections that still ran. jq is the
case in point - one hook suite is green on a machine without jq because its payload helper falls
back to python3 even when `HOOK_PAYLOAD_PARSER=jq`, so every parser-parity case compares python3 with python3.
The core section is not skipped for that, but the green means less, and the cost is printed under UNMEASURED.

A directory need may carry `holding`: the paths it must hold, because a model directory that lacks the five pinned
files is not the prerequisite the acceptance harness asks for and a prefix whose bin carries no launcher is not an
installed runtime. It may also carry `answers`: a program it holds and the arguments that program must exit 0 on
(answering `"ok": true` when it prints JSON), because a uv virtualenv carries a `bin/kelmo` console script that
only reports E_RUNTIME, so a launcher that is present is not yet an installed runtime. A section may name `env`, names and values handed to every child that section starts; a value
may be `$<need>`, the path the gate resolved, so the prerequisite the header probed is the one the script runs with
(`PYTHONPATH`, `KELMO_MODEL_DIR`) and not a second guess made by the folder's shell. An `any:` need answers
as a directory - for `holding`, for an `env` value and for an argv part - only when every alternative it may answer
with answers that way, because the gate cannot hold paths in, or hand out, a value it did not resolve as a
directory. A reviewer tool's checkout is the shape this exists for: `env-dir:SOME_REPO`, the walk up from the folder,
or the home path it falls back to, each asked to hold the same two files.

A `$<need>` in argv must be one of the section's own needs: a section that cannot find the program it runs measures
nothing and has to say so. A `$<need>` in `env` may name a need the section does not declare, and then the value is
handed only when the machine answered that need - with it absent the variable is left out of the child's environment,
the suite keeps its own default and its own skip line, and the section still measures everything it can. That is the
difference between a prerequisite a section cannot run without and one it borrows: kelmo's source suite is
handed the model directory the header found, while declaring it as that section's need would skip the whole suite on
a host that has no model. An `env` map reaches the suite a `unittest` section runs in the runner's own process the
same way, as does the bash 3.2 the gate probed: a section that names its suite's environment in gate.json and is
handed silence is the same gap as a probe that probed and never handed the answer over.

A section may name cases `bound` to one of its needs: a case that lives in a core file but asks the machine for
something it may not have. While that need is really absent its red prints as BOUND with the case's own error
line and joins UNMEASURED; with the need present the same red is a FAIL, and a bound case that ran and passed
while the need was absent is announced as an excuse that excuses nothing. An unconditional allowlist would
excuse a real failure the day the prerequisite arrived, which is why the probe decides every time.

A prerequisite is a capability and not a path one machine happens to have: `command:<name>` is on PATH,
`module:<name>` is importable by this interpreter, `file:<path>` is a file, `dir:<path>` is a directory (under the
folder, or absolute, or under the running user's home as `~/…`), `walk-dir:<relative path>` is the nearest directory
at or above the folder that holds that relative path, `env-dir:<NAME>[,<NAME>]` is a directory named by the first of
those environment variables the machine sets,
`any:<a>|<b>` is asked of each alternative in order and present when one of them answers, `probe:<argv>` is a
command whose exit 0 is the answer - a capability of the host rather than a binary on PATH, like the unprivileged
user namespace bubblewrap builds its sandbox from - `python` is this interpreter, `git:checkout` is a
folder git itself reports a toplevel for, `git:ancestor-checkout:<name>` is a directory named `<name>` above
that toplevel which git reports as a second checkout of the same repository, and `bash32` is whichever
candidate reports version 3.2 - /bin/bash on macOS, a separately built 3.2 on Linux. `git:checkout` is asked of
git and not read off the tree, because a linked worktree names its git directory in a `.git` *file*: a check
on a directory named `.git` would call such a checkout not one, and a suite whose every case skips itself is a
red here (`no case was collected`), so a folder whose cases need a checkout declares the need instead. The
layout probe is asked of git for the same reason: a plain directory with the expected name satisfies the name and
fails the rule the deployed install is written against, and only git knows that. `walk-dir:` is asked of the folder
because that is the directory every child of the section is started in: some suites walk up from where they
run looking for the target checkout's `node_modules/typescript`, and a gate that asked only the environment
variable would report the checkout absent on a host where the program finds it, which is a false skip.

Every section is bounded: it gets `timeout` seconds from gate.json, or DEFAULT_LIMIT, and past that the runner
kills what it started, reports the section as a red naming the limit, and goes on with the next section. A
folder whose gate hangs reports instead of hanging. The same holds for a person who types Ctrl-C: the runner kills what it started and prints a red for
the gate itself, because a gate that never finished measured nothing.

Environment: `GATE_WITHOUT=<need ids>` reports a prerequisite absent whatever the machine has, so a SKIP branch
is measured instead of trusting that a tool is missing; `GATE_PIN` marks a run already inside a pin section;
`GATE_BASH32` names a bash to try first, and the bash a gate resolved for its `bash32` need is handed to the
suites it runs in its own process under that same name, so a case that asks for 3.2 asks the answer the header
printed and not a second guess.

Usage: python3 gate.py <folder> [--only 1,2] [--list]  -  exit 0 when nothing is red, 1 when something is,
2 for a usage or gate.json error.
"""

import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

WITHOUT = os.environ.get('GATE_WITHOUT', '')
PIN = 'GATE_PIN' in os.environ
# The host's bash 3.2, by capability: GATE_BASH32 lets a machine point the runner at the bash it means,
# and the two paths are what the two hosts actually ship - macOS has 3.2 as /bin/bash.
BASH32_CANDIDATES = tuple([part for part in [os.environ.get('GATE_BASH32')] if part] +
                          ['/opt/bash-3.2/bin/bash', '/bin/bash'])
KINDS = ('unittest', 'node', 'command', 'runner', 'syntax', 'shellcheck')  # the six kinds RUNNERS below dispatch on
# What a wrapped runner prints when it counted, what it prints when its scratch never got built, what it prints
# when it withdrew its own cases, and what it prints beside a case it counted as red. Each is a knob a folder may
# override in gate.json; these are the shapes plain shell runners use.
COUNTS_PATTERN = r'^(?P<passed>[0-9]+) passed, (?P<failed>[0-9]+) failed$'
# GNU mkdir says "mkdir: cannot create directory 'X': …", BSD mkdir says "mkdir: X: …", and mkdir prints nothing
# on success, so any line one of them starts is the scratch error that makes the runner's counts meaningless.
SCRATCH_PATTERN = r'^mkdir: |^find: |No such file or directory'
SKIP_LINE_PATTERN = r'^  SKIP '
FAIL_LINE_PATTERN = r'^  FAIL'
# What node's own test summary prints, and how a test that did not run says so. The reporter is pinned to `tap`
# because node prints the spec reporter - the same numbers behind a U+2139 - whenever stdout is a terminal, so a
# gate that read whatever the host happened to choose would need a regex per host.
NODE_TEST_PATTERN = '*.test.mjs'
# What a python folder names its test files. It is one constant because the walk that finds them, the claim a
# section makes about them and the guard that asks whether anyone claimed them must all ask the same question.
TEST_FILE_PATTERN = 'test_*.py'
NODE_SUMMARY = re.compile(r'^# (tests|pass|fail|cancelled|skipped|todo) (-?[0-9]+)\b')
NODE_SKIP_LINE = re.compile(r'^\s*ok [0-9]+ - (?P<name>.*?) # (?P<kind>SKIP|TODO)(?: (?P<reason>.*?))?\s*$')
NODE_FAIL_LINE = re.compile(r'^\s*not ok [0-9]+ - (?P<name>.*?)\s*$')
# The seconds a section gets when gate.json names none: a generous bound for one section, which normally
# finishes in seconds.
DEFAULT_LIMIT = 300


class SectionTimeout(KeyboardInterrupt):
    """A section ran past its limit. It is an interrupt and not an Exception because unittest records an
    Exception as one case's error and carries on with the rest of the suite, which would let a hung section
    spend the limit again on every case after it."""


class MissingProgram(Exception):
    """The child could not be started at all: the program a section names is not one this machine can run. It is
    a red about what the gate names, and not a runner that printed the wrong thing, so it must not be classified
    as "no counts line" or as an exit code."""

    def __init__(self, argv, error):
        Exception.__init__(self, '%s: %s' % (' '.join([str(part) for part in argv]), error))
        self.argv = [str(part) for part in argv]
        self.error = error


CHILDREN = []  # the children this runner started and has not finished


def kill_children():
    """Kill every child the runner started, together with the group it gave it, so a stopped section cannot
    leave a tool running behind it. A child a case of a suite started is not one of these: that suite's own
    gate bounds it, if it has one."""
    for proc in list(CHILDREN):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass


def _past_limit(signum, frame):
    kill_children()
    raise SectionTimeout


def _stopped(signum, frame):
    kill_children()
    raise KeyboardInterrupt


def die(message):
    sys.stderr.write('gate.py: %s\n' % message)
    raise SystemExit(2)


def run_child(argv, cwd, env=None):
    """Every child of the gate runs with stdin closed: a tool that reads stdin must not inherit a terminal.
    Each one also gets a process group of its own, which is what lets the runner kill the tool and everything
    that tool started when a section runs past its limit: a child left in the runner's own group could only be
    killed together with the runner. A section that names `env` hands the child the values it asked for on top
    of this process's own, which is how a prerequisite the gate probed reaches the script that needs it."""
    try:
        proc = subprocess.Popen([str(part) for part in argv], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env=(dict(os.environ, **env) if env is not None else None))
    except OSError as exc:
        raise MissingProgram(argv, exc)
    CHILDREN.append(proc)
    try:
        stdout, stderr = proc.communicate()
    except SectionTimeout:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait()
        raise
    finally:
        CHILDREN.remove(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


PROGRAM_NEEDS = ('command', 'python', 'bash32', 'file', 'env-program')  # the capabilities that resolve to a program
DIRECTORY_NEEDS = ('dir', 'env-dir', 'walk-dir')  # the capabilities that resolve to a directory to name


def answers_as(spec, kinds):
    """Whether a capability resolves to one of `kinds`. An `any:` need answers as whichever alternative answers,
    so it counts only when every alternative does: a `holding` list or a `$<need>` env value asked of an `any`
    that could answer with a program is a claim about a path the gate never resolved as a directory."""
    kind, _, name = spec.partition(':')
    if kind != 'any':
        return kind in kinds
    parts = [part for part in name.split('|') if part]
    return bool(parts) and all(answers_as(part, kinds) for part in parts)


def child_dir(folder, spec, path='', name=''):
    """The directory a section's child starts in: the gate folder, or the `cwd` the section names relative to it.
    The argv check and the run read this one function, so a path named by a section is judged where the child will
    look for it."""
    cwd = spec.get('cwd', '.')
    if not isinstance(cwd, str) or not cwd:
        die('%s section %s names cwd %r, which must be a directory relative to the folder' % (path, name, cwd))
    return folder if cwd == '.' else folder / cwd


def check_argv(path, section, gate, argv, folder, run_dir=None):
    """One argv list a section runs. Every part is a literal, a path the directory it runs in holds, or `$<need>`:
    one of the folder's declared needs, which the runner replaces with the program that need resolved to."""
    if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
        die('%s section %s has no argv: %r' % (path, section['name'], argv))
    needs = gate.get('needs') or {}
    for part in [part for part in argv if part.startswith('$')]:
        name = part[1:]
        if name not in needs:
            die('%s section %s runs %s, which gate.json does not declare as a need'
                % (path, section['name'], part))
        capability = needs[name].get('capability', '')
        if not answers_as(capability, PROGRAM_NEEDS):
            die('%s section %s runs %s, which is a %s need: no program stands behind it'
                % (path, section['name'], part, capability))
        if name not in section.get('needs', []):
            die('%s section %s runs %s, a need it does not declare as its own: a machine without it would run '
                'the section with nothing' % (path, section['name'], part))
    # A part that looks like a path is one the directory the child starts in has to hold: a wrapped runner named by
    # a path that moved is a red about the gate, and not a runner that printed no counts line. The check follows
    # the child, because a section may name `cwd` and run somewhere else: a path resolved from the gate folder
    # would refuse the file the child can read and accept the one it cannot.
    base = folder if run_dir is None else run_dir
    where = 'this folder' if base == folder else 'the directory it runs in'
    missing = [str(Path(part)) for part in argv[1:] if '/' in part and not part.startswith('-')
               and not (base / part).is_file()]
    if missing:
        die('%s section %s runs %s, which %s does not hold'
            % (path, section['name'], ', '.join(missing), where))


def test_files_under(directory, pattern, folder, packages_only=False):
    """Every file under `directory` matching `pattern`, each named as a path from the gate's folder.
    Dot-directories are not walked, because naming a folder is not naming the virtualenv inside it. With
    `packages_only` the walk descends only into a directory that is a python package, which is what unittest's own
    discovery does: a test file in a plain subdirectory is collected by no `discover` section, and naming it is the
    coverage guard's job and not the section's."""
    names = [path.relative_to(folder).as_posix() for path in sorted(directory.glob(pattern)) if path.is_file()]
    for child in sorted(path for path in directory.iterdir() if path.is_dir() and not path.name.startswith('.')):
        if packages_only and not (child / '__init__.py').is_file():
            continue
        names.extend(test_files_under(child, pattern, folder, packages_only))
    return names


def covered_names(covers, folder):
    """The test files a covers list claims: a directory stands for the test files under it, however deep, and a
    file for itself. A claim is a path from the folder and not a basename, because one folder can hold two files
    of the same name - this project runs `tests/test_doctor.py` beside `tests/store/test_doctor.py` - and a
    claim that named only the last part would read that pair as one file claimed twice."""
    names = []
    for part in covers:
        target = folder / part
        if target.is_dir():
            names.extend(test_files_under(target, TEST_FILE_PATTERN, folder))
        elif target.is_file():
            names.append(target.relative_to(folder).as_posix())
    return names


def load_gate(folder):
    path = folder / 'gate.json'
    if not path.is_file():
        die('%s has no gate.json' % folder)
    try:
        gate = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        die('%s is not readable JSON: %s' % (path, exc))
    absent = [key for key in ('title', 'sections') if key not in gate]
    if absent:
        die('%s names no %s' % (path, ', '.join(absent)))
    test_files = gate.get('test_files')
    if test_files is not None and (not isinstance(test_files, list) or not test_files or
                                   any(not isinstance(part, str) or not part for part in test_files)):
        die('%s names test_files that is not a list of globs: %r' % (path, test_files))
    for need_id, spec in (gate.get('needs') or {}).items():
        if 'costs' in spec and not isinstance(spec['costs'], str):
            die('%s need %s names costs that is not a sentence: %r' % (path, need_id, spec['costs']))
        holding = spec.get('holding')
        if holding is not None:
            if not isinstance(holding, list) or not holding or any(not isinstance(part, str) or not part
                                                                   for part in holding):
                die('%s need %s names holding that is not a list of paths: %r' % (path, need_id, holding))
            if not answers_as(spec.get('capability', ''), DIRECTORY_NEEDS):
                die('%s need %s names holding on a %s need: only a directory need can hold paths'
                    % (path, need_id, spec.get('capability', '(no capability)')))
        answers = spec.get('answers')
        if answers is not None:
            if not isinstance(answers, list) or not answers or any(not isinstance(part, str) or not part
                                                                   for part in answers):
                die('%s need %s names answers that is not a program and its arguments: %r' % (path, need_id, answers))
            if not answers_as(spec.get('capability', ''), DIRECTORY_NEEDS):
                die('%s need %s names answers on a %s need: only a directory need runs a program it holds'
                    % (path, need_id, spec.get('capability', '(no capability)')))
    for section in gate['sections']:
        if 'name' not in section or 'kind' not in section:
            die('%s has a section with no name or kind: %r' % (path, section))
        if section['kind'] not in KINDS:
            die('%s section %s has kind %s, which is not one of %s'
                % (path, section['name'], section['kind'], ', '.join(KINDS)))
        undeclared = [name for name in section.get('needs', []) if name not in (gate.get('needs') or {})]
        if undeclared:
            die('%s section %s needs %s, which gate.json does not declare'
                % (path, section['name'], ', '.join(undeclared)))
        if 'shebang' in section and (not isinstance(section['shebang'], bool) or
                                     section['kind'] not in ('syntax', 'shellcheck')):
            die('%s section %s names shebang: only a syntax or shellcheck section derives its files from the tree'
                % (path, section['name']))
        parse_parts = section.get('parse')
        if parse_parts is not None:
            if section['kind'] != 'syntax':
                die('%s section %s names parse: only a syntax section parses the entry points it names'
                    % (path, section['name']))
            if not isinstance(parse_parts, list) or not parse_parts or \
                    any(not isinstance(part, str) or not part for part in parse_parts):
                die('%s section %s has a parse list that is not a list of paths: %r'
                    % (path, section['name'], parse_parts))
        if section['kind'] == 'syntax' and not section.get('files') and parse_parts is None:
            die('%s section %s is a syntax section naming no files and no parse: it would parse nothing and '
                'print ok' % (path, section['name']))
        env_map = section.get('env', {})
        if not isinstance(env_map, dict) or any(not key or not isinstance(key, str) or not isinstance(value, str)
                                                for key, value in env_map.items()):
            die('%s section %s has an env map that is not names and values: %r'
                % (path, section['name'], env_map))
        for name, value in env_map.items():
            if not value.startswith('$'):
                continue
            need = value[1:]
            if need not in (gate.get('needs') or {}):
                die('%s section %s hands %s the value of %s, which gate.json does not declare as a need'
                    % (path, section['name'], name, value))
            capability = (gate['needs'][need]).get('capability', '')
            if not answers_as(capability, PROGRAM_NEEDS + DIRECTORY_NEEDS):
                die('%s section %s hands %s the value of %s, which is a %s need: nothing behind it resolves to '
                    'a path or a program' % (path, section['name'], name, value, capability))
        for key in ('counts', 'scratch', 'skip_line', 'fail_line'):
            if key in section:
                try:
                    re.compile(section[key])
                except re.error as exc:
                    die('%s section %s names %s %r, which is not a regex: %s'
                        % (path, section['name'], key, section[key], exc))
        if 'counts' in section:
            groups = set(re.compile(section['counts']).groupindex)
            if not {'passed', 'failed'} <= groups:
                die('%s section %s names counts %r, which must name the groups passed and failed'
                    % (path, section['name'], section['counts']))
        if section['kind'] == 'runner':
            runners = section.get('runners')
            if not isinstance(runners, list) or not runners:
                die('%s section %s is a runner section with no runners list' % (path, section['name']))
            for entry in runners:
                argv = entry.get('argv') if isinstance(entry, dict) else None
                check_argv(path, section, gate, argv, folder,
                           child_dir(folder, entry, path, section['name']))
        if section['kind'] == 'command':
            check_argv(path, section, gate, section.get('argv'), folder,
                       child_dir(folder, section, path, section['name']))
        covers = section.get('covers', [])
        if not isinstance(covers, list) or any(not isinstance(part, str) or not part for part in covers):
            die('%s section %s has a covers list that is not a list of paths: %r'
                % (path, section['name'], covers))
        if covers and section['kind'] in ('unittest', 'node'):
            die('%s section %s names covers: a unittest or node section already claims the files it loads, and '
                'covers is for a section that runs no test file of its own' % (path, section['name']))
        for part in covers:
            if not (folder / part).exists():
                die('%s section %s covers %s, which this folder does not hold' % (path, section['name'], part))
        excluded = section.get('exclude', [])
        if not isinstance(excluded, list) or any(not isinstance(name, str) or not name for name in excluded):
            die('%s section %s has an exclude list that is not a list of test file paths: %r'
                % (path, section['name'], excluded))
        if covers and excluded:
            # An exclusion is a claim about a file the section's own covers would have taken. Naming one it never
            # claimed is a leftover from a file that moved, and it leaves the file unclaimed by anyone. The
            # comparison is the path the covers walk produced, so the file beside the one named is not taken for it.
            claimed = set(covered_names(covers, folder))
            left = [name for name in excluded if name not in claimed]
            if left:
                die('%s section %s excludes %s, which none of its covers claims'
                    % (path, section['name'], ', '.join(left)))

        if section['kind'] == 'node':
            if 'node' not in (gate.get('needs') or {}):
                die('%s section %s is a node section and gate.json declares no node need: the runner has no '
                    'interpreter to run it with' % (path, section['name']))
            if 'node' not in section.get('needs', []):
                die('%s section %s is a node section that does not declare node as its own need: a machine '
                    'without node would run it with nothing' % (path, section['name']))
            if not section.get('tests') and 'discover' not in section:
                die('%s section %s is a node section naming no tests and no discover: it would run node --test '
                    'with no file to run' % (path, section['name']))
            capability = gate['needs']['node'].get('capability', '')
            if not answers_as(capability, PROGRAM_NEEDS):
                die('%s section %s runs its node suite with a %s need: no program stands behind it'
                    % (path, section['name'], capability))
        skip_exit = section.get('skip_exit')
        if skip_exit is not None:
            if section['kind'] != 'runner':
                die('%s section %s names skip_exit: only a runner section reads the exit code of a program it '
                    'wraps' % (path, section['name']))
            if isinstance(skip_exit, bool) or not isinstance(skip_exit, int) or skip_exit < 1:
                die('%s section %s names skip_exit %r: an exit code is a whole number of at least 1'
                    % (path, section['name'], skip_exit))
        for item in section.get('bound', []):
            if not isinstance(item, dict) or not all(key in item for key in ('id', 'need', 'reason')):
                die('%s section %s has a bound case without id, need and reason: %r'
                    % (path, section['name'], item))
            if item['need'] not in (gate.get('needs') or {}):
                die('%s section %s binds %s to %s, which gate.json does not declare'
                    % (path, section['name'], item['id'], item['need']))
        limit = section.get('timeout', DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            die('%s section %s names timeout %r: a section limit is a whole number of seconds of at least 1'
                % (path, section['name'], limit))
        exclude = section.get('discover', {}).get('exclude', [])
        if exclude:
            die('%s section %s names exclude inside discover: a section excludes at its own level, so the same '
                'key holds a file out of a loaded suite and out of a covers claim' % (path, section['name']))
    return gate


def git_answer(argv, cwd):
    """One git question, answered or not: git's own exit code decides, so a folder git does not name a
    checkout of is an absent probe and not a crash. Every caller asks `git is on PATH` first."""
    result = run_child(argv, cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_common_dir(folder):
    """The git directory a checkout shares, as one absolute physical path for every kind of checkout. git
    prints `.git` for the canonical checkout and an absolute path for a linked worktree, so the folder it was
    asked of is part of the answer - and a worktree and the checkout it was made from name the same one."""
    reported = git_answer(['git', 'rev-parse', '--git-common-dir'], folder)
    return None if reported is None else str((Path(folder) / reported).resolve())


def named_path(name, folder):
    """A path a capability names: under the folder, absolute, or under the running user's home. The home form is
    what lets a gate name the fallback a program falls back to - some suites end at
    `~/src/example-repo` - without naming a path one machine happens to have."""
    if name.startswith('~'):
        return Path(name).expanduser()
    return folder / name


def folder_holds(path, folder):
    """Whether a program the machine just resolved is a file the folder itself holds. A folder's own stand-in is
    what its gate runs in place of a tool the machine lacks; read back through PATH or through an environment
    variable it inherited, it answers the sections that ask for the real thing with a script that launches
    nothing, which is a green that measures the stand-in. Compared physically, so a file reached
    both through a symlink and directly counts once."""
    resolved = Path(path).resolve()
    root = Path(folder).resolve()
    return root in resolved.parents


def resolve_capability(spec, folder):
    """(present, what the header prints, the value the sections run) for one capability."""
    kind, _, name = spec.partition(':')
    if kind == 'command':
        found = shutil.which(name)
        if found is None:
            return False, 'not on PATH', None
        if folder_holds(found, folder):
            return False, ('%s resolves to %s, a file this folder holds: its own stand-in is not the tool it '
                           'stands in for' % (name, found)), None
        return True, found, found
    if kind == 'module':
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        return found, 'importable here' if found else 'cannot be imported by this interpreter', None
    if kind == 'file':
        path = named_path(name, folder)
        return path.is_file(), str(path) if path.is_file() else '%s is absent' % name, str(path.resolve())
    if kind == 'dir':
        # A directory and not a file: `file:` answers absent for /private/tmp, which is a directory on the
        # host a folder's macOS cases run on.
        path = named_path(name, folder)
        if path.is_dir():
            return True, str(path), str(path)
        return False, ('%s is not a directory' % name) if path.exists() else '%s is absent' % name, None
    if kind == 'walk-dir':
        if not name:
            die('capability %s names no relative path to look for' % spec)
        for candidate in [folder] + list(folder.parents):
            if (candidate / name).exists():
                return True, '%s (holds %s)' % (candidate, name), str(candidate)
        return False, 'no directory at or above %s holds %s' % (folder, name), None
    if kind == 'env-dir':
        # A prerequisite a person names from outside: the model directory and the installed prefix are not
        # paths this folder holds, and a `dir:` resolved against the folder could not ask for them. The first
        # name the machine sets decides, so a gate knob and the product's own variable can be the same probe.
        names = [part.strip() for part in name.split(',') if part.strip()]
        if not names:
            die('capability %s names no environment variable to ask' % spec)
        for variable in names:
            value = os.environ.get(variable)
            if not value:
                continue
            path = Path(value)
            if path.is_dir():
                return True, '%s=%s' % (variable, value), value
            return False, '%s=%s is not a directory' % (variable, value), None
        return False, 'none of %s is set' % ', '.join(names), None
    if kind == 'env-program':
        # The program a person names from outside, where the program's own code looks for it first: a launcher reads
        # TOOL_BIN before it walks PATH. The value has to be an executable file, and one the folder
        # holds is not an answer - a nested gate run inherits the stand-in its parent exported.
        names = [part.strip() for part in name.split(',') if part.strip()]
        if not names:
            die('capability %s names no environment variable to ask' % spec)
        for variable in names:
            value = os.environ.get(variable)
            if not value:
                continue
            path = Path(value)
            if not path.is_file():
                return False, '%s=%s is not a file' % (variable, value), None
            if not os.access(str(path), os.X_OK):
                return False, '%s=%s is not executable' % (variable, value), None
            if folder_holds(path, folder):
                return False, ('%s=%s is a file this folder holds: its own stand-in is not the tool it stands in '
                               'for' % (variable, value)), None
            return True, '%s=%s' % (variable, value), str(path)
        return False, 'none of %s is set' % ', '.join(names), None
    if kind == 'any':
        # One prerequisite the machine may answer in more than one place, asked in the order the code that
        # needs it looks: bubblewrap is /usr/bin/bwrap or the folder's own libexec, and a gate that asked only
        # one of the two would excuse a red the code really raised for the other reason.
        parts = [part for part in name.split('|') if part]
        if len(parts) < 2:
            die('capability %s names no alternatives to ask' % spec)
        answers = []
        for part in parts:
            present, detail, value = resolve_capability(part, folder)
            if present:
                return True, '%s (%s)' % (detail, part), value
            answers.append(detail)
        return False, 'no alternative answered: %s' % '; '.join(answers), None
    if kind == 'probe':
        # A prerequisite that is a capability of the host and not a binary on PATH: bubblewrap builds its
        # sandbox from an unprivileged user namespace, so the binary alone does not say whether installing it
        # here would measure anything. The command's own exit code answers, and its error line is the detail.
        argv = name.split()
        if not argv:
            die('capability %s names no command to run' % spec)
        try:
            result = run_child(argv, folder)
        except MissingProgram as exc:
            return False, '%s: %s' % (exc.argv[0], exc.error), None
        if result.returncode == 0:
            printed = [line for line in (result.stdout or '').splitlines() if line.strip()]
            return True, printed[0].strip() if printed else '%s exits 0' % argv[0], None
        tail = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        return False, '%s exits %d: %s' % (argv[0], result.returncode, tail[-1] if tail else 'no output'), None
    if kind == 'python':
        return True, '%s (python %s)' % (sys.executable, sys.version.split()[0]), sys.executable
    if kind == 'git':
        if name == 'checkout':
            if shutil.which('git') is None:
                return False, 'git is not on PATH', None
            top = git_answer(['git', 'rev-parse', '--show-toplevel'], folder)
            if top:
                return True, '%s (git names this tree a checkout)' % top, top
            return False, 'git names no checkout above %s' % folder, None
        if name.startswith('ancestor-checkout:'):
            wanted = name[len('ancestor-checkout:'):]
            if not wanted:
                die('capability %s names no directory to look for' % spec)
            if shutil.which('git') is None:
                return False, 'git is not on PATH', None
            top = git_answer(['git', 'rev-parse', '--show-toplevel'], folder)
            own = git_common_dir(folder)
            if top is None or own is None:
                return False, 'git names no checkout at %s, so no ancestor of it can be a second one' % folder, None
            # Above the toplevel and not above the folder: the installed layout is a checkout of the same
            # repository, and the case written against it walks the checkout's own parents.
            for parent in Path(top).parents:
                if parent.name != wanted:
                    continue
                # git prints physical paths, so the candidate is compared as one: on macOS /var and /tmp are
                # symlinks into /private, and a layout reached through either is the same checkout.
                if git_answer(['git', 'rev-parse', '--show-toplevel'], parent) != str(parent.resolve()):
                    continue
                if git_common_dir(parent) != own:
                    continue
                return True, '%s (a checkout of the same repository named %s above this one)' % (parent, wanted), str(parent)
            return False, 'no ancestor of %s is a checkout of the same repository named %s' % (top, wanted), None
        die('capability %s is not one this runner knows' % spec)
    if kind == 'bash32':
        for candidate in BASH32_CANDIDATES:
            if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                continue
            result = run_child([candidate, '-c', 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'], folder)
            if result.stdout.strip().startswith('3.2'):
                return True, '%s (bash %s)' % (candidate, result.stdout.strip()), candidate
        return False, 'no bash reporting version 3.2 among %s' % ', '.join(BASH32_CANDIDATES), None
    die('capability %s is not one this runner knows' % spec)


def answered(directory, answers, folder, detail):
    """Whether the program a directory need holds answers its arguments: exit 0, and `"ok": true` when it prints
    JSON. The failing line is quoted, so a console script that reports E_RUNTIME is named as that and not as a
    missing file."""
    program = directory / answers[0]
    try:
        result = run_child([program, *answers[1:]], folder)
    except MissingProgram as exc:
        return False, '%s does not run: %s' % (program, exc)
    first = (result.stdout.strip() or result.stderr.strip()).splitlines()[:1]
    first = first[0] if first else '(no output)'
    if result.returncode != 0:
        return False, '%s %s exits %d: %s' % (program, ' '.join(answers[1:]), result.returncode, first)
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get('ok') is not True:
        return False, '%s %s does not answer ok true: %s' % (program, ' '.join(answers[1:]), first)
    return True, detail


def resolve_needs(gate, folder):
    """Every prerequisite the folder declares, resolved on the machine that runs this gate. A directory need may
    name `holding`: the files it must hold, because the model directory is only a prerequisite when the five
    pinned files are in it and a prefix is only one when the launcher is in its bin."""
    needs = {}
    for need_id, spec in (gate.get('needs') or {}).items():
        present, detail, value = resolve_capability(spec['capability'], folder)
        if present and spec.get('holding'):
            missing = [name for name in spec['holding'] if not (Path(value) / name).is_file()]
            if missing:
                present, detail = False, '%s carries no %s' % (value, ', '.join(missing))
        if present and spec.get('answers'):
            present, detail = answered(Path(value), spec['answers'], folder, detail)
        needs[need_id] = SimpleNamespace(id=need_id, because=spec.get('because', ''), costs=spec.get('costs', ''),
                                         present=present, detail=detail, value=value)
    for need_id in [part.strip() for part in WITHOUT.split(',') if part.strip()]:
        if need_id in needs:
            needs[need_id].present, needs[need_id].detail = False, 'reported absent by GATE_WITHOUT'
    return needs


class Report:
    """The lines a gate has always printed, counted in one place."""

    def __init__(self):
        self.n_pass = self.n_fail = self.n_skip = self.n_known = self.n_bound = 0
        self.skipped_lines = ''
        self.unmeasured = []
        self.standing = ''

    def ok(self, label):
        self.n_pass += 1
        print('  ok   %s' % label)

    def fail(self, label, reason):
        self.n_fail += 1
        print('  FAIL %s\n       %s' % (label, reason or 'see the output above'))

    def bound(self, label, reason, error):
        """A red the folder named as needing a prerequisite this machine does not have. It is not a pass and
        not a regression: it is one case the run could not measure, printed with the case's own error line so
        the excuse can be checked against it."""
        self.n_bound += 1
        print('  BOUND %s\n       %s\n       error: %s' % (label, reason, error or 'see the suite output'))
        self.unmeasured.append('case %s' % label)

    def skip(self, label, reason, leaves):
        self.n_skip += 1
        print('  SKIP %s\n       %s' % (label, reason))
        self.skipped_lines += '%s :: %s\n' % (label, reason)
        self.unmeasured.append(leaves)

    def known(self, label, reason):
        self.n_known += 1
        print('  KNOWN %s\n       %s' % (label, reason))

    def stale(self, label, reason):
        self.standing += '%s :: %s\n' % (label, reason)


def missing_needs(section, needs):
    return [needs[name] for name in section.get('needs', []) if not needs[name].present]


def skip_reason(missing):
    return '; '.join('%s absent: %s - %s' % (need.id, need.detail, need.because) for need in missing)


def shebang_line(path):
    try:
        with path.open('r', encoding='utf-8', errors='replace') as stream:
            return stream.readline().strip()
    except OSError:
        return ''


def shell_shebang(path):
    """Whether a file's first line names a shell: `#!/bin/sh`, `#!/bin/bash -e`, `#!/usr/bin/env bash`. The
    suffix is no use here - the installed launcher is `packaging/kelmo` and the acceptance harness is a
    `#!/bin/sh` file with no extension beside ones that are python with no extension."""
    first = shebang_line(path)
    return first.startswith('#!') and any(token.rsplit('/', 1)[-1].endswith('sh') for token in first[2:].split())


def shell_files(section, folder):
    """The files a shell section runs, derived from the directory when its entry is a glob. A match under a
    subdirectory is named by the path from the folder, because that is the working directory the child gets.

    A section that names `shebang` may name a directory as well as a glob: the directory stands for every shell
    file under it, found by its shebang and not by its suffix, so a launcher added there joins the parse and the
    lint on its own. Dot-directories are not walked: a section that names a folder does not mean to lint the
    virtualenv inside it."""
    by_shebang = bool(section.get('shebang'))
    files = []
    for pattern in section.get('files', []):
        target = folder / pattern
        if by_shebang and target.is_dir():
            found = [path for path in target.rglob('*')
                     if path.is_file() and shell_shebang(path)
                     and not any(part.startswith('.') for part in path.relative_to(target).parts)]
            files.extend(sorted(str(path.relative_to(folder)) for path in found))
        elif any(char in pattern for char in '*?['):
            matches = [path for path in folder.glob(pattern) if path.is_file()]
            if by_shebang:
                matches = [path for path in matches if shell_shebang(path)]
            files.extend(sorted(str(path.relative_to(folder)) for path in matches))
        elif target.is_file():
            if by_shebang and not shell_shebang(target):
                die('section %s names %s, whose first line names no shell' % (section['name'], pattern))
            files.append(pattern)
        else:
            die('section %s names %s, which this folder does not hold' % (section['name'], pattern))
    return files


def entry_kind(path):
    """What an entry point is parsed under: the interpreter its first line names, or python or JSON by its suffix.
    A file that names no interpreter either way is not an entry point - a Dockerfile and a README sit beside the
    launchers here and asking an interpreter to read them would measure nothing. The suffix carries the claim for
    the files a driver runs through `sys.executable` instead of through their own first line: a training
    driver runs its scripts as `sys.executable /data/<dataset>/train.py`, and a parse section that skipped them
    printed a green over the whole driver. A first line naming an interpreter this runner does not parse overrules the
    suffix."""
    first = shebang_line(path)
    if first.startswith('#!'):
        names = [token.rsplit('/', 1)[-1] for token in first[2:].split() if not token.startswith('-')]
        if any(name.endswith('sh') for name in names):
            return 'bash'
        if any(name.startswith('python') for name in names):
            return 'python'
        return None  # its first line names an interpreter this runner does not parse; the suffix cannot overrule it
    if path.suffix == '.json':
        return 'json'
    if path.suffix == '.py':
        return 'python'
    return None


def parse_files(section, folder):
    """The entry points a `parse` list stands for: every file under the named parts that an interpreter is
    asked to run. A directory is walked, dot-directories excluded, for the same reason a shebang walk skips a
    virtualenv. A part that holds no entry point contributes nothing on its own; a section whose whole parse list
    derives nothing is a red in run_syntax, because it would otherwise print a green having parsed nothing."""
    found = []
    for pattern in section.get('parse', []):
        target = folder / pattern
        if target.is_dir():
            candidates = [path for path in target.rglob('*')
                          if path.is_file() and not any(part.startswith('.') for part in path.relative_to(target).parts)]
        elif any(char in pattern for char in '*?['):
            candidates = [path for path in folder.glob(pattern) if path.is_file()]
        elif target.is_file():
            candidates = [target]
        else:
            die('section %s parses %s, which this folder does not hold' % (section['name'], pattern))
        for path in candidates:
            # gate.json is the gate's own data, not a program the folder runs: a `.json` suffix alone does not
            # make it an entry point, and parsing it would claim a file no interpreter is ever asked to run.
            if path.name == 'gate.json':
                continue
            kind = entry_kind(path)
            if kind is None:
                continue
            relative = str(path.relative_to(folder))
            found.append((relative, kind))
    return sorted(set(found))


AST_READ = 'import ast,sys; ast.parse(open(sys.argv[1]).read())'
JSON_READ = 'import json,sys; json.load(open(sys.argv[1]))'


def parse_command(kind):
    """The question one entry point is asked, and the label that says which was asked: `bash -n` for a shell,
    and a read through the interpreter's own parser for python and JSON, because `python -n <file>` is not a
    parse check at all - it is a flag and a file name the interpreter ignores."""
    if kind == 'bash':
        return 'bash -n', [shutil.which('bash'), '-n']
    if kind == 'python':
        return 'parses as python', [sys.executable, '-c', AST_READ]
    return 'parses as json', [sys.executable, '-c', JSON_READ]


def parse_with(report, argv, label, name, folder):
    result = run_child(list(argv) + [name], folder)
    if result.returncode == 0:
        report.ok('%s %s' % (label, name))
    else:
        report.fail('%s %s' % (label, name), (result.stdout + result.stderr).strip())


def empty_shell_claim(section, folder):
    """Why a syntax or shellcheck section names a `files` list the folder answers with nothing, or None when it
    holds a file matching it.

    The `parse` half has its own red inside run_syntax; this is the `files` half, which a folder with no shell
    file in it answers empty -- and an empty list prints no line at all, so the gate counts a green having parsed
    or linted nothing. It is asked before the section's prerequisites, because the claim is about the folder and
    not about the tool: a host that has shellcheck and one that does not must answer the same for one gate.json,
    and a SKIP would hide a stale list behind the missing tool."""
    if section['kind'] not in ('syntax', 'shellcheck') or not section.get('files'):
        return None
    if shell_files(section, folder):
        return None
    did = 'parse' if section['kind'] == 'syntax' else 'lint'
    return ('names %s, which matches no file in this folder: nothing to %s from it, so the section prints no line '
            'and the gate counts a green that measured nothing' % (', '.join(section['files']), did))


def section_runs_nothing(section, folder):
    """Whether a syntax or shellcheck section has any target left: its `files` walk and its `parse` walk together.
    A section whose whole claim is empty is not also a SKIP for a tool the machine lacks -- there is nothing that
    tool would have been asked to do -- while one whose entry points still ran keeps running them under its red."""
    if section['kind'] not in ('syntax', 'shellcheck'):
        return False
    return not shell_files(section, folder) and not parse_files(section, folder)


def run_syntax(section, folder, needs, report):
    bash32 = needs.get('bash32')
    bash = shutil.which('bash')
    if bash is None:
        die('the syntax kind needs bash on PATH and this machine has none')
    entries = [(name, 'bash') for name in shell_files(section, folder)]
    parsed = parse_files(section, folder)
    entries += parsed
    entries = sorted(set(entries))
    if parsed == [] and section.get('parse'):
        report.fail(section['name'], 'no entry point found under %s: the parse list derived an empty list from '
                                     'the folder, so nothing here was parsed' % folder)
    for name, kind in entries:
        if kind == 'bash':
            label, argv = parse_command('bash')
            parse_with(report, argv, label, name, folder)
            if bash32 is not None and bash32.present:
                parse_with(report, [bash32.value, '-n'], 'bash 3.2 -n', name, folder)
            else:
                reason = ('bash 3.2 absent: %s - %s' % (bash32.detail, bash32.because)) if bash32 else \
                    'gate.json declares no bash32 need for a folder with shell in it'
                report.skip('bash 3.2 -n %s' % name, reason,
                            'whether %s parses under the host\'s bash 3.2' % name)
        else:
            label, argv = parse_command(kind)
            parse_with(report, argv, label, name, folder)


def run_shellcheck(section, folder, needs, report):
    known = {item['id']: item['reason'] for item in section.get('known', [])}
    need = needs.get('shellcheck')
    reason = ('shellcheck absent: %s - %s' % (need.detail, need.because)) if need else \
        'gate.json declares no shellcheck need for a folder with shell in it'
    for name in shell_files(section, folder):
        if need is None or not need.present:
            report.skip('shellcheck %s' % name, reason, 'the shellcheck findings on %s' % name)
            continue
        result = run_child([need.value, '-s', 'bash', '-S', 'warning', name], folder)
        finding = known.get(name)
        if result.returncode != 0 and finding:
            report.known('shellcheck %s' % name, finding)
        elif result.returncode != 0:
            report.fail('shellcheck %s' % name, (result.stdout + result.stderr).strip())
        elif finding:
            report.stale('shellcheck %s' % name, finding)
        else:
            report.ok('shellcheck %s' % name)


def resolve_argv(argv, needs):
    """The `$<need>` parts of a section's argv become the program that need resolved to, so the suite runs on
    the interpreter the header printed and not on whatever the folder's shell found on PATH."""
    return [needs[part[1:]].value if part.startswith('$') else part for part in argv]


def resolve_env(section, needs):
    """The `$<need>` values of a section's env map become the path the gate resolved, so the child runs with the
    prerequisite the header probed instead of a second guess made from the folder's shell. A `$<need>` the machine
    did not answer is left out of the map rather than handed as an empty value: the child keeps the default its own
    code reads, which is what a need the section borrows but does not declare means."""
    resolved = {}
    for name, value in (section.get('env') or {}).items():
        if not value.startswith('$'):
            resolved[name] = value
            continue
        need = needs[value[1:]]
        if need.present and need.value:
            resolved[name] = need.value
    return resolved


def run_command(section, folder, needs, report):
    """A failing command's report carries the head of its output as well as the tail: a lint prints its
    findings first and its verdict last, so the tail alone names no file to open, while a build prints its
    error last and the head is the log that led to it."""
    argv = section.get('argv')
    if not argv:
        die('section %s is a command section with no argv' % section['name'])
    argv = resolve_argv(argv, needs)
    label = section.get('label', argv[0])
    try:
        result = run_child(argv, child_dir(folder, section), env=resolve_env(section, needs))
    except MissingProgram as exc:
        report.fail(label, 'names %s, which this machine cannot run: %s' % (exc.argv[0], exc.error))
        return
    if result.returncode == 0:
        report.ok('%s :: exit 0' % label)
    else:
        lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        named = []
        for line in lines[:3] + lines[-3:]:
            if line not in named:
                named.append(line)
        report.fail(label, 'exit %d: %s' % (result.returncode, ' | '.join(named) if named else 'no output'))


def runner_target(entry):
    """What --list prints for one wrapped runner: the file it runs, so a person can see which runner a section
    claims and compare it with the folder's own README."""
    for part in entry['argv']:
        if '/' in part and not part.startswith('-'):
            return str(Path(part))
    return ' '.join(entry['argv'])


def run_runner(section, folder, needs, report):
    """A wrapped runner is classified by what it printed and not by its exit code: the counts line is the
    measurement it took, the exit code is a convention it keeps and can lose while still counting its cases."""
    counts_re = re.compile(section.get('counts', COUNTS_PATTERN))
    scratch_re = re.compile(section.get('scratch', SCRATCH_PATTERN))
    skip_re = re.compile(section.get('skip_line', SKIP_LINE_PATTERN))
    fail_re = re.compile(section.get('fail_line', FAIL_LINE_PATTERN))
    names_skips = 'skipped' in counts_re.groupindex
    for entry in section.get('runners', []):
        raw = entry['argv']
        label = entry.get('label') or ' '.join(raw)
        try:
            result = run_child(resolve_argv(raw, needs), child_dir(folder, entry),
                               env=resolve_env(section, needs))
        except MissingProgram as exc:
            report.fail(label, 'names %s, which this machine cannot run: %s' % (exc.argv[0], exc.error))
            continue
        lines = (result.stdout + result.stderr).splitlines()

        def last_counts():
            for line in reversed(lines):
                match = counts_re.search(line)
                # A line where no number took part is not a counts line: "0 passed, 0 failed" is one, a header
                # that happens to match the shape and names no number is not. A line naming only its skips is
                # one, because the runner withdrew those cases from itself and said so.
                if match and (match.group('passed') or match.group('failed') or
                              (names_skips and match.group('skipped'))):
                    return line.strip(), match
            return None, None

        counts, match = last_counts()
        if counts is None:
            # A program that says out loud it cannot run this measurement is a SKIP, and the section named which
            # exit code means that. Without the code (or without the reason line) the missing counts line stays
            # the red it was: a program that fell over before it counted is not one that said why.
            reason = next((line.strip() for line in lines if skip_re.search(line)), None)
            if section.get('skip_exit') is not None and result.returncode == section['skip_exit'] and reason:
                report.skip(label, 'the runner said this machine cannot run it (exit %d): %s'
                            % (result.returncode, reason),
                            section.get('skip_leaves', section.get('leaves',
                                                                   '%s (the runner could not run it here)' % label)))
            else:
                report.fail(label, 'printed no counts line matching %s: %s'
                            % (counts_re.pattern, ' | '.join([line for line in lines if line.strip()][-3:])))
            continue
        failed = int(match.group('failed') or 0)
        self_skips = int(match.group('skipped') or 0) if names_skips else 0
        scratch = next((line.strip() for line in lines if scratch_re.search(line)), None)
        reds = [line.strip() for line in lines if fail_re.search(line)]
        skipped = next((i for i, line in enumerate(lines) if skip_re.search(line)), None)
        if scratch:
            report.fail(label, '%s, but the runner wrote a scratch error: %s' % (counts, scratch))
        elif failed:
            report.fail(label, '%s (exit %d) :: the runner\'s own counts line reports failures; the gate reads '
                               'the counts, not the exit code: %s'
                        % (counts, result.returncode, ' | '.join(reds[:3]) or 'and no FAIL line beside them'))
        elif self_skips and result.returncode == 0:
            # The suite ran and withdrew cases from itself. The green beside the counts line is real for the
            # cases that ran and false for the ones that did not, so the section is a SKIP that names both.
            # It is read off the counts line before the runner's own skip lines, because the counts say how many
            # and the lines say why: a suite that names its skips is measured, one that only prints a SKIP line
            # is the section below.
            named = [line.strip() for line in lines if skip_re.search(line)][:6]
            report.skip(label, '%s :: %d case%s skipped themselves; the gate reads the counts, not the exit '
                               'code: %s'
                        % (counts, self_skips, '' if self_skips == 1 else 's',
                           ' | '.join(named) or 'no line matched this section\'s skip_line, so the suite named '
                                                'no reason here'),
                        section.get('skipped_leaves', section.get('skip_leaves', section.get('leaves',
                                                                                             '%s (the runner '
                                                                                             'skipped its own cases)'
                                                                                             % label))))
        elif skipped is not None:
            # A runner that probed its own prerequisite prints "0 passed, 0 failed" beside exit 0. That is the
            # section the runner could not run, and the gate says so in the runner's own words.
            reason = lines[skipped + 1].strip() if skipped + 1 < len(lines) else ''
            report.skip(label, 'the runner skipped its own cases: %s' % (reason or 'and named no reason'),
                        section.get('skip_leaves', section.get('leaves',
                                                               '%s (the runner skipped its own cases)' % label)))
        elif not failed and not int(match.group('passed') or 0):
            # The same guard the unittest kind has for `Ran 0 tests`: a file that lost every case, or a
            # pytest-only file handed to unittest, prints a counts line and exit 0 while measuring nothing.
            report.fail(label, '%s (exit %d) :: the runner counted no case: a suite that collected nothing is a '
                              'green that measures nothing' % (counts, result.returncode))
        elif result.returncode != 0:
            report.fail(label, '%s :: the runner exited %d while reporting no failed cases: %s'
                        % (counts, result.returncode, ' | '.join([line for line in lines if line.strip()][-3:])))
        else:
            report.ok('%s :: %s' % (label, counts))


def without_files(suite, excluded):
    """Drop the cases of the files a discover section names in `exclude`, so one section can cover a folder
    and leave one file to the section that knows what that file needs from the machine. The leaf module name
    is what is compared: a suite discovered from a directory that is not a package reports ids like
    `test_vault.VaultTests.test_x`, and one discovered from a package `tests.test_vault.…`."""
    stems = {Path(name).stem for name in excluded}
    kept = unittest.TestSuite()

    def keep(test):
        # A discovered suite nests, so the walk goes down. It is called before the run: a suite that has been
        # run holds None where its cases were, which is also why the ids below are read before the run.
        if isinstance(test, unittest.TestSuite):
            for child in test:
                keep(child)
        elif test.id().rsplit('.', 2)[0].split('.')[-1] not in stems:
            kept.addTest(test)

    keep(suite)
    return kept


def load_tests(section, folder, loader):
    """Load the suite from the files the section names, so a file that resolves to nothing is a red.

    Returns (suite, errors, contributed): `contributed` maps each test file the loader collected to the cases it
    took from it, because a file that imports cleanly and hands over nothing leaves a section that ran its
    siblings green while measuring one file fewer than the folder holds."""
    base = folder / section.get('base', '.')
    if 'discover' in section:
        start = base / section['discover'].get('start', '.')
        # A start directory that is not a package becomes the top level, so its modules keep the plain
        # names its own test files use for their siblings.
        top = start if not (start / '__init__.py').is_file() else base / section['discover'].get('top', '.')
        suite = loader.discover(str(start), pattern=section['discover'].get('pattern', 'test_*.py'),
                                top_level_dir=str(top))
        if loader.errors:
            return suite, list(loader.errors), {}
        return without_files(suite, section.get('exclude', [])), [], collected_from(start, section, loader)
    suite = unittest.TestSuite()
    errors = []
    contributed = {}
    # A `tests:` list imports the way `discover` does: the directory holding the file joins sys.path while its
    # module is executed, so `from fixture import …` resolves to the sibling the same two shapes name. A plain
    # list left that directory off the path, which made a section that named its files one by one fail on the
    # import a discover section of the same folder passed.
    inserted = []
    try:
        for relative in section.get('tests', []):
            path = base / relative
            if not path.is_file():
                errors.append('%s is named by section %s and is not a file' % (relative, section['name']))
                continue
            parent = str(path.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
                inserted.append(parent)
            name = relative[:-3].replace('/', '.')
            spec = importlib.util.spec_from_file_location(name, str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:
                errors.append('%s failed to import: %s' % (relative, exc))
                continue
            held = loader.loadTestsFromModule(module)
            suite.addTests(held)
            contributed[str(path.resolve())] = test_ids(held)
    finally:
        for parent in inserted:
            if parent in sys.path:
                # Drop the copy this section added, which sits below any the module itself inserted, so a test
                # that put its own directory on the path keeps it.
                del sys.path[len(sys.path) - 1 - sys.path[::-1].index(parent)]
    return suite, errors, contributed


def collected_from(start, section, loader):
    """For every test module `discover` really imported from this section's start, the case ids the loader takes
    from it. The modules come from sys.modules, which is what the discovery imported, so a file the pattern never
    collected is not this guard's business - the coverage guard names that miss. A file whose cases were deleted
    sits here with an empty set, and that is the miss this guard names."""
    pattern = section['discover'].get('pattern', 'test_*.py')
    excluded = {Path(name).name for name in section.get('exclude', [])}
    root = start.resolve()
    contributed = {}
    for module in list(sys.modules.values()):
        source = getattr(module, '__file__', None)
        if not source:
            continue
        path = Path(source).resolve()
        if not path.is_file() or not path.is_relative_to(root) or not fnmatch.fnmatchcase(path.name, pattern):
            continue
        if path.name in excluded:
            continue
        try:
            held = test_ids(loader.loadTestsFromModule(module))
        except BaseException:
            continue
        contributed[str(path)] = contributed.get(str(path), set()) | held
    return contributed


def first_message(traceback):
    """The line a red really says: the first traceback line that is not a frame."""
    for line in traceback.splitlines():
        if line.strip() and not line[:1].isspace() and not line.startswith(('Traceback', '----')):
            return line.strip()
    return 'no message line in its traceback'


def test_ids(suite):
    """Every case id the suite holds, walking the nesting a discovered suite carries. Call it before the run:
    unittest leaves None where a case was once the suite has been through it."""
    ids = set()
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(item)
        else:
            ids.add(item.id())
    return ids


def run_unittest(section, folder, needs, report):
    before = report.n_fail  # the report is shared by the sections; only this section's reds count here
    loader = unittest.TestLoader()
    suite, errors, contributed = load_tests(section, folder, loader)
    for error in errors:
        report.fail(section['name'], error)
    for path in sorted(contributed):
        if not contributed[path]:
            report.fail(str(Path(path).relative_to(folder)),
                        'collected by section %s and hands over no case: a file whose cases were deleted, or '
                        'whose TestCase base is gone, leaves the section green on its siblings and measures '
                        'nothing itself' % section['name'])
    # read before the run: unittest empties the suite as it goes through it
    planned = test_ids(suite)
    # A suite that prints while it runs (the knowledge suite reports every vault step) would write its chatter
    # between the gate's own lines, so the run is held and shown only under a red,
    # like the text unittest hands back. It is held in a file and not a StringIO
    # because a case may start a child attached to the stream it finds in sys.stdout/sys.stderr - fastmcp's
    # stdio transport hands its server `stderr` - and a stand-in without a fileno turns that case into a red
    # the folder never sees when it runs the same file under pytest.
    chatter = tempfile.TemporaryFile(mode='w+')
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = chatter
    # The runner probed bash 3.2 for its header and hands that answer to a suite running in this process, the
    # way resolve_env hands it to a child: a status-line suite's whole-line-under-3.2 case reads GATE_BASH32 the
    # way the runner does, and a gate that probed 3.2 and then let the suite guess again printed SKIP over a
    # host that has it.
    bash32 = needs.get('bash32')
    handed = os.environ.get('GATE_BASH32')
    # The same hand-off a child gets through resolve_env: a unittest section names its suite's environment in
    # gate.json, and a suite run in this process would otherwise read none of it and skip on a host that has it.
    # The values are read before anything is set, so a section that names GATE_BASH32 itself restores what the
    # person ran the gate with and not what this runner handed over.
    section_env = resolve_env(section, needs)
    inherited = {name: os.environ.get(name) for name in section_env}
    if bash32 and bash32.present and bash32.value:
        os.environ['GATE_BASH32'] = bash32.value
    os.environ.update(section_env)
    # The rule run_child gives every child the gate starts, carried into the lane that runs its cases in this
    # process: a case that starts a child of its own hands that child this process's fd 0, and on a host where a
    # person typed the gate from a terminal the child reads the terminal - a terminal helper's exec waits
    # there for a keypress - and the section dies at its limit instead of measuring.
    saved_stdin = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    try:
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        sys.stdout, sys.stderr = real_out, real_err
        if handed is None:
            os.environ.pop('GATE_BASH32', None)
        else:
            os.environ['GATE_BASH32'] = handed
        for name, value in inherited.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    chatter.flush()
    chatter.seek(0)
    printed = chatter.read()
    chatter.close()
    ran = result.testsRun
    if ran == 0:
        report.fail(section['name'], 'no case was collected: the files this section names define no test '
                                     'unittest can run, which a green exit does not notice')
        return
    known = {item['id']: item['reason'] for item in section.get('known', [])}
    bound = {item['id']: item for item in section.get('bound', [])}
    label = section.get('label', section['name'])
    ran_ids = set()
    excused = set()
    for test, traceback in result.expectedFailures:
        ran_ids.add(test.id())
        if test.id() in known:
            report.known(test.id(), known[test.id()])
        else:
            report.fail(label, 'an expected failure the gate does not name: %s' % test.id())
    for test, traceback in list(result.failures) + list(result.errors):
        ran_ids.add(test.id())
        item = bound.get(test.id())
        if item and not needs[item['need']].present:
            excused.add(test.id())
            need = needs[item['need']]
            report.bound(test.id(), 'bound to %s, absent here: %s - %s' % (need.id, need.detail, item['reason']),
                         first_message(traceback))
        elif item:
            report.fail(test.id(), 'bound to %s, which this machine has, and it is still red: %s'
                                 % (item['need'], item['reason']))
        else:
            report.fail(test.id(), first_message(traceback))
    for test in result.unexpectedSuccesses:
        report.fail(test.id(), 'UNEXPECTED SUCCESS (a pinned gap was fixed and the gate no longer '
                               'names it as standing)')
    for test_id, reason in known.items():
        if test_id not in ran_ids:
            report.stale(test_id, reason)
    skipped_ids = {test.id() for test, _ in result.skipped}
    for test_id, item in bound.items():
        # An excuse that excuses nothing: the prerequisite is absent, the case ran, and it passed anyway. The
        # list is a claim about the handler, and a handler that learns to read another way leaves it behind.
        if test_id not in planned:
            report.stale(test_id, 'named as bound to %s and no case this section runs carries that id'
                           % item['need'])
        elif not needs[item['need']].present and test_id not in ran_ids and test_id not in skipped_ids:
            report.stale(test_id, 'named as bound to %s, which this machine lacks, yet the case ran and passed: '
                                  'the excuse covers a red that is not there' % item['need'])
    if result.skipped:
        reasons = '; '.join('%s: %s' % (test.id(), reason) for test, reason in result.skipped)
        report.skip(label, '%d of %d cases skipped: %s' % (len(result.skipped), ran, reasons),
                    section.get('skip_leaves', section.get('leaves', 'the cases this section skipped')))
    elif not (report.n_fail - before or errors):
        report.ok('%s :: Ran %d%s%s' % (label, ran,
                                        ', %d KNOWN' % len(result.expectedFailures) if known else '',
                                        ', every red above is BOUND' if excused else ''))
    if printed.strip() and (report.n_fail - before or errors):
        lines = printed.splitlines()
        print('       what this suite printed while it ran (%d of %d lines):' % (min(len(lines), 20), len(lines)))
        for line in lines[-20:]:
            print('       %s' % line)


def node_test_files(section, folder):
    """The test files a node section runs, named relative to the directory its child is started in: the ones the
    section names, or every file under its discover start matching its pattern, less the ones it excludes. The
    derivation is the runner's and never node's own discovery, because a file node picked up by itself is a file
    the gate never claimed and would sit in the folder green.

    Returns (files, missing): a name the section named that the folder does not hold is a red about the gate, and
    not a suite that ran without that file."""
    base = folder / section.get('base', '.')
    if 'discover' in section:
        start = base / section['discover'].get('start', '.')
        pattern = section['discover'].get('pattern', NODE_TEST_PATTERN)
        names = sorted(str(path.relative_to(base)) for path in start.rglob(pattern)
                       if path.is_file() and not any(part.startswith('.') for part in path.relative_to(start).parts))
    else:
        names = list(section.get('tests', []))
    dropped = {Path(name).name for name in section.get('exclude', [])}
    kept = [name for name in names if Path(name).name not in dropped]
    return kept, [name for name in kept if not (base / name).is_file()]


def run_node(section, folder, needs, report):
    """One node suite, run by node's own test runner and read off the summary block it prints. The counts are the
    measurement and the exit code is not: a file whose import blew up is reported by node as one failed test, and
    a suite that collected nothing exits 0 while measuring nothing."""
    label = section.get('label', section['name'])
    files, missing = node_test_files(section, folder)
    for name in missing:
        report.fail(section['name'], '%s is named by section %s and is not a file' % (name, section['name']))
    if not files:
        report.fail(label, 'no test file to run: the files this section names are not in the folder')
        return
    try:
        result = run_child([needs['node'].value, '--test-reporter=tap', '--test'] + files,
                           folder / section.get('cwd', section.get('base', '.')),
                           env=resolve_env(section, needs))
    except MissingProgram as exc:
        report.fail(label, 'names %s, which this machine cannot run: %s' % (exc.argv[0], exc.error))
        return
    lines = (result.stdout + result.stderr).splitlines()

    def tail():
        return ' | '.join([line.strip() for line in lines if line.strip()][-3:])

    counts = {}
    for line in lines:
        found = NODE_SUMMARY.match(line)
        if found:
            counts[found.group(1)] = int(found.group(2))
    if 'tests' not in counts:
        report.fail(label, 'printed no node test summary (# tests …): the reporter is pinned to tap and this run '
                           'said: %s' % tail())
        return
    ran = counts['tests']
    failed = counts.get('fail', 0)
    cancelled = counts.get('cancelled', 0)
    withdrawn = counts.get('skipped', 0) + counts.get('todo', 0)
    parts = sum(counts.get(key, 0) for key in ('pass', 'fail', 'cancelled', 'skipped', 'todo'))
    reds = [line.strip() for line in lines if NODE_FAIL_LINE.match(line)][:3]
    skipped = [line.strip() for line in lines if NODE_SKIP_LINE.match(line)][:6]
    if ran == 0:
        report.fail(label, 'collected no test: node ran %s and counted 0 tests: %s' % (', '.join(files), tail()))
    elif failed:
        report.fail(label, '%d of %d tests failed (exit %d): %s'
                    % (failed, ran, result.returncode, ' | '.join(reds) or tail()))
    elif cancelled:
        report.fail(label, '%d of %d tests were cancelled (exit %d): the run stopped before they finished, which '
                          'is not a green' % (cancelled, ran, result.returncode))
    elif parts != ran:
        report.fail(label, 'its summary does not add up: %d tests, %s — a total the runner cannot account for is '
                           'not a measurement' % (ran, ' '.join('# %s %d' % (key, value)
                                                                 for key, value in sorted(counts.items()))))
    elif withdrawn:
        report.skip(label, '%d of %d tests did not run: %s'
                    % (withdrawn, ran, ' | '.join(skipped) or 'node named no reason beside its counts'),
                    section.get('skipped_leaves', section.get('leaves',
                                                              'the tests %s skipped or left as todo' % label)))
    elif result.returncode != 0:
        report.fail(label, 'counted %d passing tests and exited %d: %s' % (ran, result.returncode, tail()))
    else:
        report.ok('%s :: %d tests, %d pass' % (label, ran, counts.get('pass', 0)))


def test_files_on_disk(gate, folder):
    """The test files the coverage guard has to see: the globs gate.json names, or - when the folder names none -
    where the python folders put theirs, which is the folder's root and every file under its `tests/`, however
    deep. The walk is deep because a suite that lives in a subdirectory is the same suite: a guard that stopped at
    the directory's own level would run nested test files that no section claimed."""
    if gate.get('test_files'):
        return sorted({path.relative_to(folder).as_posix() for pattern in gate['test_files']
                       for path in folder.glob(pattern) if path.is_file()})
    names = [path.relative_to(folder).as_posix() for path in folder.glob(TEST_FILE_PATTERN) if path.is_file()]
    if (folder / 'tests').is_dir():
        names.extend(test_files_under(folder / 'tests', TEST_FILE_PATTERN, folder))
    return sorted(set(names))


def discovered_names(section, folder):
    """The files a unittest discover section collects: under its start, matching its pattern, descending only into
    the subdirectories unittest's own loader descends into. The claim is the derivation and not the directory's
    contents: a pattern narrowed below the folder's test files leaves the rest unclaimed, and a file in a plain
    subdirectory is imported by no discovery, so the section that named the directory never ran it."""
    base = folder / section.get('base', '.')
    start = base / section['discover'].get('start', '.')
    pattern = section['discover'].get('pattern', TEST_FILE_PATTERN)
    return test_files_under(start, pattern, folder, packages_only=True)


def claimed_paths(section, folder):
    """The test files one section claims, as paths from the gate's folder: what a `node` or `unittest` section
    loads, what a `discover` section's own start and pattern derive, and what a `covers` list says a wrapped
    suite runs. `exclude` is how one of them goes to the section that knows what it needs from the machine."""
    base = section.get('base', '.')
    if section['kind'] == 'node':
        names = [name for name in node_test_files(section, folder)[0]]
    elif section['kind'] == 'unittest' and 'discover' in section:
        names = discovered_names(section, folder)
    elif section['kind'] == 'unittest':
        names = list(section.get('tests', []))
    else:
        names = covered_names(section.get('covers', []), folder)
    dropped = {Path(name).name for name in section.get('exclude', [])}
    paths = [name if section['kind'] in ('runner', 'command') else str(Path(base) / name) for name in names]
    return [path for path in paths if Path(path).name not in dropped]


def coverage(gate, folder):
    """A test file the folder holds and no section runs is a red: it sits there green and measures nothing.
    The folder's own root counts beside its `tests/` directory, because a suite can live at the top of a folder
    (some do) and a file added there is the same silent green. A file and a claim are the same
    thing only when the paths match: two files of one name in two directories are two claims, not one file run
    twice."""
    on_disk = test_files_on_disk(gate, folder)
    claimed = []
    for section in gate['sections']:
        claimed.extend(claimed_paths(section, folder))
    return [(path, claimed.count(path)) for path in on_disk if claimed.count(path) != 1]


def shell_files_under(directory, folder):
    """Every shell file under `directory`, found by its first line and named as a path from the gate's folder.
    Dot-directories are not walked, for the reason a `shebang` walk gives them, and neither is a subdirectory that
    keeps a `gate.json` of its own: a nested tool answers for its own files through its own gate, and a parent
    that also claimed them would report a file measured twice as one measured once."""
    # _docs is an untracked local folder that no distribution ships.
    if directory == folder / '_docs':
        return []
    names = [path.relative_to(folder).as_posix() for path in sorted(directory.glob('*'))
             if path.is_file() and shell_shebang(path)]
    for child in sorted(path for path in directory.iterdir()
                        if path.is_dir() and not path.name.startswith('.') and not (path / 'gate.json').is_file()):
        names.extend(shell_files_under(child, folder))
    return names


def shell_claims(section, folder):
    """The shell files one section touches, kept apart by the half that touches them: `files` (the `files` walk of
    a syntax or shellcheck section), `parse` (an entry point the `parse` walk reads) and `run` (the file a
    `command` or `runner` section starts by path). A wrapped runner or a lint a section names is a shell file the
    gate does run, so it is not a file sitting here unmeasured; a path part is judged from the `cwd` the child
    starts in, the way check_argv judges it. The groups stay apart because a parse claim says only that bash can
    read the file: it is not a claim that anything checked what the file does."""
    files = list(shell_files(section, folder))
    parsed = [name for name, kind in parse_files(section, folder) if kind == 'bash']
    run = []
    for spec in [section] + list(section.get('runners', []) or []):
        base = child_dir(folder, spec, path='gate.json', name=section.get('name', ''))
        for part in spec.get('argv', []) or []:
            if '/' not in part or part.startswith('-') or part.startswith('$'):
                continue
            target = (base / part).resolve()
            if target.is_file() and shell_shebang(target) and folder in target.parents:
                run.append(target.relative_to(folder).as_posix())
    return {'files': files, 'parse': parsed, 'run': run}


def shell_coverage(gate, folder):
    """A shell file the folder holds that no section measures, as (path, gap) pairs. gap is 'nothing' when no
    section parses, lints or runs it at all: the suffix is no use there, because a file that was python and
    becomes a `#!/bin/sh` stub keeps its old name, joins no `*.sh` glob, and the folder prints a green having
    never read it - which is how a rename stub sat in a folder whose only ruff section was busy failing to parse
    it as python. gap is 'lint' when its only claim is a parse walk: the gate prints a green having proved only
    that bash can read the script, and what it does - an unquoted path, a variable set in a subshell - is named
    by no section. A `files` walk (of a syntax or a shellcheck section) or a section that starts the file closes
    that gap; a parse claim does not, because run_shellcheck is handed only a shellcheck section's `files` list.
    The two gaps are read off one list: every claim that is not a parse claim measures the file, so a file with
    no such claim is in one of the two gaps and a file with one is in neither."""
    on_disk = shell_files_under(folder, folder)
    claimed, measured = [], []
    for section in gate['sections']:
        claims = shell_claims(section, folder)
        claimed.extend(claims['files'] + claims['parse'] + claims['run'])
        measured.extend(claims['files'] + claims['run'])
    return [(path, 'nothing' if path not in claimed else 'lint') for path in on_disk if path not in measured]


def listed(gate, folder):
    for index, section in enumerate(gate['sections'], 1):
        if section['kind'] in ('syntax', 'shellcheck'):
            # A file named by both halves of a syntax section (the shell `files` walk and the `parse` walk of
            # the folder) is one target and is printed once.
            targets = shell_files(section, folder) + [name for name, _ in parse_files(section, folder)]
        else:
            targets = ([' '.join(section.get('argv', []))] if section['kind'] == 'command'
                       else [runner_target(entry) for entry in section.get('runners', [])]
                       if section['kind'] == 'runner'
                       else node_test_files(section, folder)[0] if section['kind'] == 'node'
                       else [section['discover'].get('start', '.')] if 'discover' in section
                       else section.get('tests', []))
        seen = set()
        for target in targets:
            if target not in seen:
                seen.add(target)
                print('%d %s' % (index, target))


RUNNERS = {'syntax': run_syntax, 'shellcheck': run_shellcheck, 'command': run_command,
           'runner': run_runner, 'unittest': run_unittest, 'node': run_node}


def run_section(section, folder, needs, report, label):
    """One section, with the limit gate.json gives it (or DEFAULT_LIMIT) armed on the runner's own clock."""
    limit = section.get('timeout', DEFAULT_LIMIT)
    started = time.time()
    signal.signal(signal.SIGALRM, _past_limit)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        RUNNERS[section['kind']](section, folder, needs, report)
    except SectionTimeout:
        report.fail(label, 'ran %d s, past its %d s limit, and was stopped with everything it started; '
                           'left unmeasured: %s'
                           % (round(time.time() - started), limit,
                              section.get('leaves', 'what this section measures')))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def main(argv):
    usage = 'usage: python3 gate.py <folder> [--only 1,2] [--list]'
    args = list(argv)
    if not args:
        die(usage)
    folder = Path(args.pop(0)).resolve()
    if not folder.is_dir():
        die('%s is not a directory' % folder)
    only, listing = '', False
    while args:
        flag = args.pop(0)
        if flag == '--only':
            if not args or not args[0]:
                die('--only needs a section list, e.g. --only 1,2')
            only, args = args[0], args[1:]
        elif flag == '--list':
            listing = True
        else:
            die('%s (unknown flag %s)' % (usage, flag))
    gate = load_gate(folder)
    if only:
        selected = {part.strip() for part in only.split(',')}
        available = {str(index) for index in range(1, len(gate['sections']) + 1)}
        if selected - available:
            die('--only must name existing section numbers from 1 to %d' % len(gate['sections']))
    signal.signal(signal.SIGINT, _stopped)
    signal.signal(signal.SIGTERM, _stopped)
    if listing:
        listed(gate, folder)
        return 0

    needs = resolve_needs(gate, folder)
    report = Report()
    print(gate['title'])
    print('  root: %s' % folder)
    # Which interpreter measured the suite matters to the reader: a folder whose cases need a module names the
    # module as a need, and the line below says what carried it.
    interpreter = '%s (python %s)' % (sys.executable, sys.version.split()[0])
    print('  interpreter: %s' % interpreter)
    for need in needs.values():
        # A `python` need resolves to the interpreter already running this runner, which the line above named:
        # a folder that declares its interpreter as a need so a section can run `$interpreter` would otherwise
        # print the same path twice and read as two interpreters in one run. A need that names a program by
        # path, or one this machine lacks, keeps its own line.
        if need.present and need.detail == interpreter:
            continue
        print('  %s: %s' % (need.id, need.detail if need.present else 'absent - %s' % need.detail))
    if WITHOUT:
        print('  forced absent by GATE_WITHOUT: %s' % WITHOUT)

    unclaimed = coverage(gate, folder)
    unnamed = bool(gate.get('test_files')) and not test_files_on_disk(gate, folder)
    shell_gaps = shell_coverage(gate, folder)
    if unclaimed or unnamed or shell_gaps:
        print("== 0. the folder's own files no section measures")
        if unnamed:
            report.fail('its test-file list', 'gate.json names test_files (%s) and the folder holds no file '
                                              'matching them: the guard that a test file no section runs is a red '
                                              'would print a green having claimed nothing'
                        % ', '.join(gate['test_files']))
        for name, times in unclaimed:
            if times:
                report.fail(name, 'claimed by %d sections of this gate: a file run twice is counted twice, and '
                                  'its red is reported twice' % times)
            else:
                report.fail(name, 'no section of this gate runs it: name it in a section or it measures nothing')
        for name, gap in shell_gaps:
            if gap == 'nothing':
                report.fail(name, 'no section of this gate parses, lints or runs it: it is shell by its first line, '
                                  'so name it in a syntax or shellcheck section (a directory with "shebang": true '
                                  'takes every shell file under it) or run it from a section')
            else:
                report.fail(name, 'its only claim is a parse walk: bash -n proved this file can be read and no '
                                  'section checked what it does - name it in a shellcheck section (a directory '
                                  'with "shebang": true takes every shell file under it) or start it from a '
                                  'section')

    stopped = ''
    label = 'its header probes'
    try:
        for index, section in enumerate(gate['sections'], 1):
            if only and str(index) not in [part.strip() for part in only.split(',')]:
                continue
            label = section.get('label', section['name'])
            print('== %d. %s' % (index, section['name']))
            empty = empty_shell_claim(section, folder)
            if empty:
                report.fail(label, empty)
                if section_runs_nothing(section, folder):
                    continue
            missing = missing_needs(section, needs)
            if missing:
                report.skip(label, skip_reason(missing), section.get('leaves', 'what this section measures'))
            elif section.get('pin') and PIN:
                report.skip(label, 'already inside a pin run: the gate does not spawn the pin that runs the gate',
                            section.get('leaves', "this section's own coverage"))
            else:
                if section.get('pin'):
                    os.environ['GATE_PIN'] = '1'
                run_section(section, folder, needs, report, label)
    except KeyboardInterrupt:
        stopped = 'stopped by a signal while it ran %s: the sections after it did not run' % label
    if stopped:
        report.fail('the gate', stopped)

    # A need this machine lacks takes something from the sections that ran anyway: the green they printed means
    # less than it reads as, and the folder names by how much.
    for need in needs.values():
        if not need.present and need.costs:
            report.unmeasured.append(need.costs)
    report.unmeasured.extend(gate.get('unmeasured', []))
    print()
    summary = 'PASS=%d FAIL=%d SKIP=%d KNOWN=%d' % (report.n_pass, report.n_fail, report.n_skip, report.n_known)
    if any(section.get('bound') for section in gate['sections']):
        summary += ' BOUND=%d' % report.n_bound
    print(summary)
    if report.standing:
        print("STANDING LISTS TO FIX (a KNOWN red that went green, or a BOUND case that stayed green with its "
              "prerequisite absent; remove it from the gate's list):\n"
              + '\n'.join('  %s' % line for line in report.standing.splitlines()))
    if report.skipped_lines:
        print('SKIPPED by this run:')
        sys.stdout.write(report.skipped_lines)
    if report.unmeasured:
        print('UNMEASURED by this run:')
        for line in report.unmeasured:
            print('  %s' % line)
    return 0 if report.n_fail == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
