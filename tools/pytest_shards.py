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
(`N failed, M passed, K skipped, E errors in Ts`) summing the shards. A shard that ends without a summary line
is an error that names the shard and its last lines. Exit 0 only when every shard passed and the shards together
report an outcome for every case they were dealt. A collection that ended in an error (a module that does not
import) is not sharded: its output is printed and its exit code returned.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

SUMMARY = re.compile(r'^=*\s*\d+ [a-z]+.*\bin [\d.]+s\b')
COUNT = re.compile(r'(\d+) (failed|passed|skipped|errors?|xfailed|xpassed|warnings?|deselected)')
REPORT = re.compile(r'^(?:FAILED|ERROR|SUBFAILED|SKIPPED|XFAIL|XPASS)\b')
ORDER = ('failed', 'passed', 'skipped', 'error', 'xfailed', 'xpassed', 'warning', 'deselected')


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
    lines = done.stdout.splitlines()
    roots = [line[len(ROOTDIR):] for line in lines if line.startswith(ROOTDIR)]
    done.stdout = ''.join(line + '\n' for line in lines if not line.startswith(ROOTDIR))
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
    with tempfile.TemporaryFile(mode='w+') as out:
        code = subprocess.call([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *options, '--', *ids],
                               stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        out.seek(0)
        return index, code, out.read()


def main(args):
    started = time.time()
    collected, ids, root = collect(args)
    if collected.returncode or not ids:
        # A module that fails to import still lets the others list their ids, and pytest exits 2 (1 with
        # --continue-on-collection-errors): dealing those ids out ran a green suite without that module.
        listed = set(ids)
        sys.stdout.write(''.join(line + '\n' for line in collected.stdout.splitlines() if line.strip() not in listed))
        sys.stdout.write(collected.stderr)
        return collected.returncode or 5
    if root:
        ids = [os.path.join(root, node) for node in ids]
    count = min(width(), len(ids))
    shards = [ids[index::count] for index in range(count)]
    options = split(args)
    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(lambda pair: run_shard(pair[0], pair[1], options), enumerate(shards)))
    total, reports, failed = {}, [], False
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
            continue
        for key, value in found.items():
            total[key] = total.get(key, 0) + value
        reports.extend(line for line in text.splitlines() if REPORT.match(line))
    ran = sum(total.get(key, 0) for key in ('failed', 'passed', 'skipped', 'error', 'xfailed', 'xpassed'))
    if ran < len(ids):
        # Every dealt id is one collected item, and each item ends as one of these outcomes at least once.
        failed = True
        reports.append('ERROR the shards report %d outcome(s) for the %d case(s) they were dealt: %d never ran'
                       % (ran, len(ids), len(ids) - ran))
    for line in reports:
        print(line)
    parts = ['%d %s' % (total.get(key, 0), {'error': 'errors', 'warning': 'warnings'}.get(key, key))
             for key in ORDER if total.get(key) or key == 'passed']
    print('%s in %.2fs (%d shards)' % (', '.join(parts), time.time() - started, count))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
