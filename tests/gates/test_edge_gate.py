"""The edge gate: 50 MCP sessions and their SessionStart hooks against one edge daemon, with the process count and
resident memory of the edge tree and of the authority tree sampled every second.

Opt-in: it starts two real kelmod processes and runs for KELMO_EDGE_GATE_SECONDS (default 30), so the normal suite
skips it unless KELMO_EDGE_GATE is set.

    KELMO_EDGE_GATE=1 KELMO_EDGE_GATE_SECONDS=60 KELMO_EDGE_GATE_OUT=/tmp/edge-gate.json pytest tests/gates

The gate: every session opens, no memory_search fails, and the edge tree has a median process count of at most 3
and a median total RSS under 110 MB. The edge tree is the edge kelmod plus its descendants, without `hook`
processes. The authority tree is measured and reported only. Hook failures are reported, not asserted.
"""
import asyncio
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

if not os.environ.get('KELMO_EDGE_GATE'):
    pytest.skip('KELMO_EDGE_GATE is not set: the edge gate is an opt-in measurement', allow_module_level=True)

TESTS = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[2]
for directory in (TESTS, TESTS / 'daemon', TESTS / 'state_core', REPO / 'tools'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402
from loadtest import EN, RU, Results, command_of, descendants, error_code, process_table  # noqa: E402
from daemon_harness import KELMOD, SRC, Daemon, wait_for  # noqa: E402
from state_core_support import profile_path  # noqa: E402
from test_action_state import world_factory as world_factory  # noqa: E402,F401
from test_shared_context import server_config, shared as shared  # noqa: E402,F401

from kelmo.client import prepare_profile  # noqa: E402
from kelmo.config import ClientProfile  # noqa: E402
from kelmo.core import spec as core_spec  # noqa: E402

SESSIONS = 50
ENROLLED = 'mac-a'
SECONDS = int(os.environ.get('KELMO_EDGE_GATE_SECONDS', '30'))
# A context hook per session every 30 s, the pace of a prompt hook on a busy session; searches run without pause.
HOOK_EVERY = float(os.environ.get('KELMO_EDGE_GATE_HOOK_EVERY', '30'))
OPEN_LIMIT = 120.0
PROCESS_LIMIT = 3
RSS_LIMIT_MB = 110
MB = 1024 * 1024


def require_kelmod():
    if not KELMOD.is_file():
        pytest.fail(f'kelmod is missing at {KELMOD}: build it with tools/build_kelmod.sh', pytrace=False)


def ready(status):
    return status is not None and bool(status['cores']) and all(core['ready'] for core in status['cores'])


def attach_command(runtime, client_id, protocol):
    return [str(KELMOD), 'attach', '--client-id', client_id, '--protocol', str(protocol),
            '--runtime-dir', str(runtime)]


def terminate(process):
    """SIGTERM, up to a minute for the daemon to drain its cores, then SIGKILL."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def role(command):
    """A short name for a process of a daemon tree: the core module, the kelmod verb, or the executable."""
    words = command.split()
    for word in words:
        if word.startswith('kelmo.core.') or word in ('state', 'archive', 'edge', 'embed'):
            return word.rsplit('.', 1)[-1]
    if words and words[0].endswith('kelmod') and len(words) > 1:
        return 'kelmod ' + words[1]
    return Path(words[0]).name if words else '?'


def tree(pid, include_root):
    """(process count, RSS bytes, {role: RSS bytes}) of the processes below `pid`, and of `pid` itself when
    `include_root`. A `hook` process is left out: a hook is a short-lived command, not part of a daemon's
    footprint."""
    members = [(rss, command) for _, rss, command in descendants(pid)]
    own = process_table(commands=False).get(pid) if include_root else None
    if own is not None:
        members.append((own[1], command_of(pid)))
    kept = [(rss, command) for rss, command in members if 'hook' not in command.split()]
    roles = {}
    for rss, command in kept:
        roles[role(command)] = roles.get(role(command), 0) + rss
    return len(kept), sum(rss for rss, _ in kept), roles


class Sampler:
    """One sample a second of the edge tree and the authority tree: (time, edge, authority), each (count, bytes)."""

    def __init__(self, edge_pid, authority_pid):
        self.pids = {'edge': edge_pid, 'authority': authority_pid}
        self.samples = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stopped.wait(1.0):
            self.samples.append((time.monotonic(), tree(self.pids['edge'], True),
                                 tree(self.pids['authority'], False)))

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join()

    def summary(self, since, until):
        window = [sample for sample in self.samples if since <= sample[0] <= until]
        assert window, 'no process sample fell inside the load window'

        def side(index):
            counts = [sample[index][0] for sample in window]
            megabytes = [sample[index][1] / MB for sample in window]
            last = window[-1][index][2]
            return {'samples': len(window), 'process_count_median': statistics.median(counts),
                    'rss_mb_median': round(statistics.median(megabytes), 1),
                    'rss_mb_max': round(max(megabytes), 1),
                    'rss_mb_by_role_last': {name: round(rss / MB, 1) for name, rss in sorted(last.items())}}

        return {'edge': side(1), 'authority': side(2)}


async def drive(url, token, root, hook_argv, seconds):
    """Opens SESSIONS sessions at once. When every one has opened or failed, each runs a search loop and a hook loop
    for `seconds`. Returns the results, the sessions that opened, and the load window's start and end."""
    results, opened, failed, go, clock = Results(), [], [], asyncio.Event(), {}
    headers = {'Authorization': 'Bearer ' + token}

    async def searches(client, rng, end):
        while time.monotonic() < end:
            arguments = {'query_en': ' '.join(rng.sample(EN, 3)), 'query_ru': ' '.join(rng.sample(RU, 3)),
                         'limit': 5}
            begin = time.monotonic()
            try:
                await client.call_tool('memory_search', arguments)
                results.add('memory_search', time.monotonic() - begin)
            except Exception as error:  # every refusal is counted by its code
                results.add('memory_search', time.monotonic() - begin, error_code(error))

    async def hooks(session_id, end):
        payload = json.dumps({'session_id': session_id, 'cwd': str(root),
                              'hook_event_name': 'SessionStart'}).encode()
        while time.monotonic() < end:
            begin = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                *hook_argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            _, stderr = await process.communicate(payload)
            # A hook reports its failures on stderr and still exits 0, so both signals count as a failure.
            text = stderr.decode(errors='replace')
            if process.returncode or text.strip():
                match = re.search(r'\bE_[A-Z_]+\b', text)
                code = match.group(0) if match else (f'exit {process.returncode}' if process.returncode
                                                     else 'stderr')
                results.add('hook_start', time.monotonic() - begin, code)
            else:
                results.add('hook_start', time.monotonic() - begin)
            await asyncio.sleep(HOOK_EVERY)

    async def session(number):
        started = time.monotonic()
        try:
            async with Client(StreamableHttpTransport(url, headers=headers)) as client:
                opened.append(number)
                results.add('session_start', time.monotonic() - started)
                await go.wait()
                end = clock['end']
                await asyncio.gather(searches(client, random.Random(number), end),
                                     hooks(f'edge-gate-{number:04d}', end))
        except Exception as error:  # every refusal is counted by its code
            if number in opened:
                results.add('session', time.monotonic() - started, error_code(error))
            else:
                failed.append(number)
                results.add('session_start', time.monotonic() - started, error_code(error))

    tasks = [asyncio.create_task(session(number)) for number in range(SESSIONS)]
    limit = time.monotonic() + OPEN_LIMIT
    while len(opened) + len(failed) < SESSIONS and time.monotonic() < limit:
        await asyncio.sleep(0.05)
    since = time.monotonic()
    clock['end'] = since + seconds
    go.set()
    await asyncio.gather(*tasks)
    return results, opened, since, clock['end']


@pytest.fixture
def authority(shared, tmp_path):
    """kelmod serving the shared vault under its server profile, every core ready before the load starts."""
    require_kelmod()
    server_config(shared.world.root, sessions='archive-only')
    runtime = Path(tempfile.mkdtemp(prefix='ga', dir='/tmp'))
    spec = core_spec.build(shared.world.root, server_profile=profile_path(shared), runtime_dir=runtime,
                           source_root=SRC, schedule=False)
    spec_path = tmp_path / 'authority-spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (tmp_path / 'authority.log').open('w')
    process = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    daemon = Daemon(process, spec_path, runtime, tmp_path, log)
    try:
        wait_for(lambda: ready(daemon.status()), timeout=60, what='the authority cores ready')
        yield daemon
    finally:
        terminate(process)
        log.close()
        shutil.rmtree(runtime, ignore_errors=True)


def test_edge_gate_fifty_sessions_on_one_edge(shared, authority, tmp_path):
    root = shared.tmp_path / 'client-edge'
    prepare_profile(root, shared.world.bindings.authority_id, attach_command(authority.runtime, ENROLLED, 1),
                    timeout_seconds=60)
    profile = ClientProfile.load(root)
    runtime = Path(tempfile.mkdtemp(prefix='ge', dir='/tmp'))
    spec_path = tmp_path / 'edge-spec.json'
    spec_path.write_text(json.dumps(core_spec.build_edge(root, runtime_dir=runtime, source_root=SRC,
                                                         http_port=0)))
    log = (tmp_path / 'edge.log').open('w')
    process = subprocess.Popen([str(KELMOD), 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    edge = Daemon(process, spec_path, runtime, tmp_path, log)
    endpoint = runtime / 'endpoint.json'
    sampler = None
    try:
        wait_for(lambda: ready(edge.status()) and endpoint.is_file(), timeout=60,
                 what='the edge core ready and its HTTP endpoint up')
        url = json.loads(endpoint.read_text())['url']
        token = core_spec.http_token_at(profile.state).read_text().strip()
        hook_argv = [str(KELMOD), 'hook', 'start', '--provider', 'claude', '--runtime-dir', str(runtime),
                     '--state', str(profile.state)]
        sampler = Sampler(process.pid, authority.proc.pid)
        sampler.start()
        results, opened, since, until = asyncio.run(drive(url, token, root, hook_argv, SECONDS))
    finally:
        if sampler is not None:
            sampler.stop()
        terminate(process)
        log.close()
        shutil.rmtree(runtime, ignore_errors=True)

    report = {'sessions': SESSIONS, 'sessions_opened': len(opened), 'seconds': SECONDS,
              'operations': results.summary(), **sampler.summary(since, until)}
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if os.environ.get('KELMO_EDGE_GATE_OUT'):
        out = Path(os.environ['KELMO_EDGE_GATE_OUT'])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n')

    search = report['operations'].get('memory_search', {'count': 0, 'errors': {}})
    assert len(opened) == SESSIONS, f'{len(opened)} of {SESSIONS} sessions opened'
    assert search['count'] > 0 and not search['errors'], search
    assert report['edge']['process_count_median'] <= PROCESS_LIMIT, report['edge']
    assert report['edge']['rss_mb_median'] < RSS_LIMIT_MB, report['edge']
