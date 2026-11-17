#!/usr/bin/env python3
"""Run one pytest selection as several pytest processes side by side and report it as one run.

usage: python tools/pytest_shards.py [the arguments one `python -m pytest` run would take]

The selection is collected once (`--collect-only -q`) and its test ids are dealt round robin over WIDTH pytest
processes, so the slow cases of one file land in different processes instead of making one file the run's tail.
WIDTH is GATE_SHARD_WIDTH when it is set (set it when several runs share the machine), else four, and
never more than the cores or the ids. Every process runs the same options with its own ids, in this process's
group, so a gate that kills the group it started kills every shard.

What is printed is what a gate reads of a pytest run: the whole output of a shard that failed (its tracebacks),
every FAILED / ERROR / SKIPPED summary line of every shard, and last one summary line in pytest's own shape
(`N failed, M passed, K skipped, E errors in Ts`) summing the shards. Before them, as the run goes, it says what it
collected (`collected N cases in K shards`) and a `progress:` line as each shard ends, with the cases done so
far; just before the summary, how many of the collected cases passed. None of these lines has a summary's or a
report line's shape. When GITHUB_STEP_SUMMARY names a file, the result is appended to it as well. A shard that
ends without a summary line is an error that names the shard and its last lines. Exit 0 only when every shard passed and the shards together
report an outcome for every case they were dealt. A collection that ended in an error (a module that does not
import) is not sharded: its output is printed and its exit code returned.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SUMMARY = re.compile(r'^=*\s*(?:\d+ [a-z]+.*|no tests ran) in [\d.]+s\b')
COUNT = re.compile(r'(\d+) (failed|passed|skipped|errors?|xfailed|xpassed|warnings?|deselected)')
REPORT = re.compile(r'^(?:FAILED|ERROR|SUBFAILED|SKIPPED|XFAIL|XPASS)\b')
ORDER = ('failed', 'passed', 'skipped', 'error', 'xfailed', 'xpassed', 'warning', 'deselected')
OUTCOMES = ('failed', 'passed', 'skipped', 'error', 'xfailed', 'xpassed')
# PY_COLORS or FORCE_COLOR make pytest colour its lines; the patterns above read them without the colour codes.
COLOUR = re.compile(r'\x1b\[[0-9;]*m')


def width():
    cores = os.cpu_count() or 1
    try:
        wanted = int(os.environ.get('GATE_SHARD_WIDTH') or 4)
    except ValueError:
        wanted = 4
    return max(1, min(cores, wanted))


VALUED = {'-k', '-m', '-p', '-o', '-c', '-W', '-r', '-n', '--rootdir', '--confcutdir', '--basetemp', '--deselect',
          '--ignore', '--ignore-glob', '--junitxml', '--junit-xml', '--junit-prefix', '--tb', '--maxfail',
          '--durations', '--durations-min', '--log-level', '--log-file', '--log-file-level', '--log-cli-level',
          '--color', '--import-mode', '--capture', '--override-ini', '--timeout', '--cov-report'}


def split(args):
    """The options of a pytest command line apart from the paths it selects, which the ids replace. An option
    that takes its value as the next argument keeps it when VALUED names it; one it does not name loses its
    value, and the shard's pytest then stops on the option with no value (the ids follow `--`, so none of them
    is taken for it), which is a red naming the option rather than a case that silently never ran."""
    options, keep = [], False
    for arg in args:
        if keep or arg.startswith('-'):
            options.append(arg)
            keep = not keep and arg in VALUED
    return options


ROOTDIR = 'pytest-shards-rootdir: '
# Collected ids are relative to pytest's rootdir, which need not be where this runner was started; the collection
# prints the rootdir so every id can be made absolute and a shard finds it from any working directory.
COLLECTOR = ('import sys, pytest\n'
             'class Rootdir:\n'
             '    def pytest_collection_finish(self, session):\n'
             '        print(%r + str(session.config.rootpath), flush=True)\n'
             'raise SystemExit(pytest.main(sys.argv[1:], plugins=[Rootdir()]))\n' % ROOTDIR)


def collect(args):
    # Exactly one -q: with two, --collect-only prints a count per file instead of the test ids.
    quiet = [arg for arg in args if arg not in ('-q', '-qq', '--quiet')]
    done = subprocess.run([sys.executable, '-c', COLLECTOR, '--collect-only', '-q', '-p', 'no:cacheprovider', *quiet],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    lines = COLOUR.sub('', done.stdout).splitlines()
    roots = [line[len(ROOTDIR):] for line in lines if line.startswith(ROOTDIR)]
    done.stdout = ''.join(line + '\n' for line in lines if not line.startswith(ROOTDIR))
    done.stderr = COLOUR.sub('', done.stderr)
    ids = [line.strip() for line in lines if '::' in line and not line.startswith((' ', ROOTDIR))]
    return done, ids, (roots[-1] if roots else None)


def counts(text):
    """The counts of the last pytest summary line in a shard's output."""
    found = {}
    for line in reversed(text.splitlines()):
        if SUMMARY.match(line.strip()):
            for number, word in COUNT.findall(line):
                key = {'errors': 'error', 'warnings': 'warning'}.get(word, word)
                found[key] = found.get(key, 0) + int(number)
            return found
    return None


def run_shard(index, ids, options):
    # Bytes a case wrote that are not text in the locale must not cost the shard its summary.
    with tempfile.TemporaryFile(mode='w+', errors='replace') as out:
        code = subprocess.call([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *options, '--', *ids],
                               stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        out.seek(0)
        return index, code, COLOUR.sub('', out.read())


def main(args):
    started = time.time()
    collected, ids, root = collect(args)
    if collected.returncode or not ids:
        # A module that fails to import still lets the others list their ids, and pytest exits 2 (1 with
        # --continue-on-collection-errors): dealing those ids out ran a green suite without that module.
        listed = set(ids)
        shown = [line for line in collected.stdout.splitlines() if line.strip() not in listed]
        sys.stdout.write(''.join(line + '\n' for line in shown))
        sys.stdout.write(collected.stderr)
        code = collected.returncode or 5
        errors = [line for line in shown + collected.stderr.splitlines() if line.startswith('ERROR')]
        what = 'no tests collected' if code == 5 else 'collection failed'
        step_summary(errors, '%s (pytest exit %d): no case ran' % (what, code),
                     '%d cases collected in %.2fs' % (len(ids), time.time() - started))
        return code
    if root:
        ids = [os.path.join(root, node) for node in ids]
    count = min(width(), len(ids))
    shards = [ids[index::count] for index in range(count)]
    options = split(args)
    print('collected %d cases in %d shards' % (len(ids), count), flush=True)
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=count) as pool:
        running = {pool.submit(run_shard, index, shard, options): index for index, shard in enumerate(shards)}
        for future in as_completed(running):
            try:
                index, code, text = future.result()
            except Exception as error:  # noqa: BLE001 - one shard the runner could not read is that shard's red
                index, code, text = running[future], -1, 'the runner failed: %s: %s' % (type(error).__name__, error)
            results.append((index, code, text))
            # A shard can end before it ran every case it was dealt, so only the outcomes it reported count.
            found = counts(text) or {}
            done += min(len(shards[index]), sum(found.get(key, 0) for key in OUTCOMES))
            print('progress: shard %d of %d ended with exit %d, %d of %d cases done'
                  % (index + 1, count, code, done, len(ids)), flush=True)
    total, reports, failed, unreported = {}, [], False, 0
    for index, code, text in sorted(results):
        found = counts(text)
        if code != 0:
            failed = True
            sys.stdout.write('--- shard %d of %d (%d cases, exit %d) ---\n' % (index + 1, count, len(shards[index]),
                                                                              code))
            sys.stdout.write(''.join(line + '\n' for line in text.splitlines() if not SUMMARY.match(line.strip())))
        if found is None:
            failed = True
            tail = ' | '.join(text.strip().splitlines()[-3:]) or 'it printed nothing'
            reports.append('ERROR shard %d of %d ended without a pytest summary (exit %d): %s'
                           % (index + 1, count, code, tail))
            total['error'] = total.get('error', 0) + 1
            unreported += 1  # that error stands for the shard, not for one of its cases
            continue
        for key, value in found.items():
            total[key] = total.get(key, 0) + value
        reports.extend(line for line in text.splitlines() if REPORT.match(line))
    ran = sum(total.get(key, 0) for key in OUTCOMES) - unreported
    if ran < len(ids):
        # Every dealt id is one collected item, and each item ends as one of these outcomes at least once.
        failed = True
        reports.append('ERROR the shards report %d outcome(s) for the %d case(s) they were dealt: '
                       '%d never ran or ran unreported'
                       % (ran, len(ids), len(ids) - ran))
    for line in reports:
        print(line)
    parts = ['%d %s' % (total.get(key, 0), {'error': 'errors', 'warning': 'warnings'}.get(key, key))
             for key in ORDER if total.get(key) or key == 'passed']
    passed = '%d of %d collected cases passed' % (total.get('passed', 0), len(ids))
    summary = '%s in %.2fs (%d shards)' % (', '.join(parts), time.time() - started, count)
    sys.stdout.flush()
    step_summary(reports, passed, summary)  # its warning comes before the summary, which stays the last line
    print(passed)
    print(summary)
    return 1 if failed else 0


def step_summary(reports, passed, summary):
    """Appends the result to the file GITHUB_STEP_SUMMARY names, when it names one; a file that cannot be
    written costs the run nothing."""
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if not target:
        return
    lines = ['### pytest', '', summary, '', passed, '']
    lines += ['    ' + line for line in reports]
    try:
        with open(target, 'a', encoding='utf-8') as out:
            out.write('\n' + '\n'.join(lines) + '\n')  # a line of its own after another step's text
    except OSError as error:
        print('step summary not written: %s' % error, file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
