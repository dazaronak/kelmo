"""Load harness: N simulated sessions against a synthetic vault, with process count, memory and latency.

    uv run --locked python tools/loadtest.py build --vault /tmp/load/vault --records 10000
    uv run --locked python tools/loadtest.py run --vault /tmp/load/vault --sessions 5,50 --seconds 60 \
        --out measurements/before.json

With `--daemon KELMOD` the same mix runs against the 0.2 shape instead: one `kelmod serve` for the vault (state,
archive and embed cores, HTTP endpoint), each session an MCP client of that endpoint, each hook `kelmod hook`.

`build` makes a vault with a synthetic word-level tokenizer and a synthetic embedding broker (this script's
`broker` command), then archives `--records` mixed Russian and English records through the same store path the
`memory_store` tool uses. `run` starts each session the way a 0.1 client does on a single machine, one
`kelmo broker ... -- kelmo mcp` process tree per session, drives a mix of search, get, store, create and hook
events, samples every process below this one, and writes one JSON document per session count.

Nothing here loads a real model: the numbers measure Kelmo's process and memory shape, not embedding quality.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

DIM = 64
MAX_TOKENS = 512
FDS_ENV = 'KELMO_EMBED_FDS'
PROJECT = 'loadtest'
SCRIPT = Path(__file__).resolve()

EN = ('archive build cache commit daemon deploy editor error fence graph index journal ledger memory model '
      'outbox page publish queue record release search session socket spool store tokenizer vector wiki worker '
      'basalt cedar quarry harbor lantern meadow orchard river summit timber').split()
RU = ('архив сборка кэш коммит демон выкладка редактор ошибка граф индекс журнал память модель очередь '
      'запись релиз поиск сессия сокет хранилище токенизатор вектор вики воркер базальт кедр карьер гавань '
      'фонарь луг сад река вершина лес').split()


# --- synthetic embedding -------------------------------------------------------------------------------------

def tokenizer_bytes():
    """A word-level tokenizer: every word one token plus [CLS] and [SEP], as the test suite's synthetic one."""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, '[CLS]': 1, '[SEP]': 2, '[PAD]': 3}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single='[CLS] $A [SEP]', special_tokens=[('[CLS]', 1), ('[SEP]', 2)])
    return tokenizer.to_str().encode()


def space_for(tokenizer):
    from kelmo.contracts import CONTRACT_VERSION, EmbeddingSpace
    from kelmo.store.memory.embed import InputPolicy, bound_transform, preparation_digest
    identity = 'sha256:' + hashlib.sha256(tokenizer).hexdigest()
    policy = InputPolicy(identity, MAX_TOKENS, '', '', preparation_digest([]))
    return EmbeddingSpace(
        contract_version=CONTRACT_VERSION, artifact_identity='loadtest@sha256:' + '0' * 64,
        tokenizer_identity=identity, query_transform=bound_transform('loadtest:query', '', policy),
        document_transform=bound_transform('loadtest:document', '', policy), pooling='mean', projection='none',
        normalization='l2', dimension=DIM, dtype='float32', byte_order='little')


def vector(text):
    import numpy as np
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], 'little')
    value = np.random.RandomState(seed).randn(DIM)
    return base64.b64encode((value / np.linalg.norm(value)).astype('<f4').tobytes()).decode()


def answer(request, space, space_id):
    if request.get('kind') == 'health':
        return {'id': request['id'], 'ok': True, 'space': space.to_dict(),
                'input': {'max_tokens': MAX_TOKENS, 'query_prefix': '', 'document_prefix': '',
                          'special_tokens': []}}
    return {'id': request['id'], 'space_id': space_id, 'vectors': [vector(t) for t in request['texts']]}


def serve_broker(tokenizer_path, requests, replies):
    from kelmo.store.memory.embed import identity_id
    space = space_for(Path(tokenizer_path).read_bytes())
    space_id = identity_id(space)
    for line in requests:
        replies.write((json.dumps(answer(json.loads(line), space, space_id)) + '\n').encode())
        replies.flush()


def embedding_section(tokenizer_path, space):
    lines = ['[embedding]', 'provider = "broker"', f'fds_env = "{FDS_ENV}"',
             f'tokenizer = {json.dumps(str(tokenizer_path))}', '',
             '[embedding.input]', f'max_tokens = {MAX_TOKENS}', 'query_prefix = ""', 'document_prefix = ""',
             'special_tokens = []', '', '[embedding.space]']
    lines += [f'{key} = {json.dumps(value)}' for key, value in space.to_dict().items()]
    return '\n'.join(lines) + '\n'


class InProcessBroker:
    """The launcher's side of the broker pipes, served by a thread, for building the vault in this process."""

    def __init__(self, tokenizer_path):
        request_r, request_w = os.pipe()
        answer_r, answer_w = os.pipe()
        self.fds = (request_w, answer_r)
        requests, replies = os.fdopen(request_r, 'rb'), os.fdopen(answer_w, 'wb')
        threading.Thread(target=serve_broker, args=(tokenizer_path, requests, replies), daemon=True).start()


# --- synthetic vault -----------------------------------------------------------------------------------------

def sentence(rng, words):
    return ' '.join(rng.choice(words) for _ in range(rng.randint(6, 40)))


def text(rng, index):
    parts = [sentence(rng, EN), sentence(rng, RU)]
    rng.shuffle(parts)
    return f'Record {index}. ' + ' '.join(parts) + '.'


def build(args):
    vault = Path(args.vault).absolute()
    if vault.exists():
        raise SystemExit(f'{vault} exists; choose a new path.')
    model = vault.parent / 'loadtest-model'
    model.mkdir(parents=True, exist_ok=True)
    tokenizer = model / 'tokenizer.json'
    tokenizer.write_bytes(tokenizer_bytes())
    from kelmo.mcp import compat
    from kelmo.service import Service
    Service.initialize(vault, 'synthetic', project=PROJECT)
    config_path = vault / 'kelmo.toml'
    head = config_path.read_text().split('[embedding]')[0]
    config_path.write_text(head + embedding_section(tokenizer, space_for(tokenizer.read_bytes())))
    broker = InProcessBroker(tokenizer)
    os.environ[FDS_ENV] = ','.join(str(fd) for fd in broker.fds)
    rng = random.Random(args.seed)
    started = time.monotonic()
    with Service(vault) as service:
        for index in range(args.records):
            compat(service, 'memory_store', {
                'content': text(rng, index), 'kind': rng.choice(['decision', 'event', 'quirk', 'rule']),
                'domain': rng.choice(['dev', 'life']), 'project': PROJECT,
                'tags': [rng.choice(EN), 'loadtest']})
            if (index + 1) % 500 == 0:
                print(f'{index + 1} records, {time.monotonic() - started:.0f} s', file=sys.stderr)
    print(json.dumps({'vault': str(vault), 'records': args.records,
                      'seconds': round(time.monotonic() - started, 1)}))


# --- process sampling ----------------------------------------------------------------------------------------

def process_table(commands=True):
    """{pid: (ppid, rss bytes, command)} for every process this user may inspect; with `commands` false the
    command is left empty (on macOS reading every process's argv each second is the costly part).

    Read from the kernel rather than `ps`: `ps` is setuid on macOS, and a sandboxed run may not execute setuid
    programs.
    """
    if sys.platform == 'darwin':
        return _darwin_table(commands)
    table = {}
    page = os.sysconf('SC_PAGE_SIZE')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_bytes().decode('utf-8', 'replace')
            rss_pages = int((entry / 'statm').read_text().split()[1])
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        ppid = int(stat[stat.rindex(')') + 2:].split()[1])
        table[int(entry.name)] = (ppid, rss_pages * page, b' '.join(a for a in argv if a).decode(errors='replace'))
    return table


def _darwin_table(commands=True):
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    count = libproc.proc_listallpids(None, 0)
    pids = (ctypes.c_int * (count * 2))()
    count = libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    bsd = ctypes.create_string_buffer(136)      # struct proc_bsdinfo; pbi_ppid at offset 16
    task = ctypes.create_string_buffer(96)      # struct proc_taskinfo; pti_resident_size at offset 8
    table = {}
    for pid in pids[:count]:
        if pid <= 0:
            continue
        if libproc.proc_pidinfo(pid, 3, 0, bsd, 136) != 136 or libproc.proc_pidinfo(pid, 4, 0, task, 96) != 96:
            continue
        ppid = int.from_bytes(bsd.raw[16:20], 'little')
        rss = int.from_bytes(task.raw[8:16], 'little')
        table[pid] = (ppid, rss, _darwin_command(libc, ctypes, pid) if commands else '')
    return table


def _darwin_command(libc, ctypes, pid):
    """argv of `pid` joined by spaces, from sysctl KERN_PROCARGS2 (argc, exec path, padding, argv...)."""
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return ''
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return ''
    raw = buffer.raw[:size.value]
    argc = int.from_bytes(raw[:4], 'little')
    rest = raw[4:]
    rest = rest[rest.index(b'\0'):].lstrip(b'\0')
    argv = rest.split(b'\0')[:argc]
    return b' '.join(argv).decode(errors='replace')


def descendants(root):
    """(pid, rss bytes, command) of every live process below `root`, from one process-table snapshot."""
    table = process_table(commands=False)
    children = {}
    for pid, (ppid, _rss, _command) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = [], list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        found.append((pid, table[pid][1], command_of(pid)))
        stack.extend(children.get(pid, ()))
    return found


def command_of(pid):
    """The argv of one process joined by spaces, or '' once it is gone."""
    if sys.platform == 'darwin':
        import ctypes
        import ctypes.util
        return _darwin_command(ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True), ctypes, pid)
    try:
        argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        return ''
    return b' '.join(a for a in argv if a).decode(errors='replace')


def role(command):
    """A short name for a process: the Kelmo verb when there is one, else the executable."""
    words = command.split()
    if '--command-json' in words:
        return 'kelmo broker'
    if 'loadtest.py' in command:
        return 'synthetic broker'
    if 'kelmo.core.' in command or 'core-run' in words:
        for core in ('state', 'archive', 'edge'):
            if f'{core}_core' in command or f'core-run -- {core}' in command:
                return f'{core} core'
    if words and Path(words[0]).name == 'kelmod':
        return 'kelmod ' + (words[1] if len(words) > 1 else '')
    for verb in ('broker', 'mcp', 'hook', 'archive-worker', 'serve-client', 'client', 'worker'):
        if verb in words:
            return 'kelmo ' + verb
    if 'sandbox-exec' in command or 'bwrap' in command:
        return 'confinement wrapper'
    return Path(words[0]).name if words else '?'


# struct vnode_fdinfowithpath (proc_fileinfo 24 bytes, vnode_info 152, then the path) for PROC_PIDFDVNODEPATHINFO.
VNODE_PATH_INFO_SIZE, VNODE_PATH_OFFSET = 1200, 176


def open_paths(pid):
    """The paths of the files `pid` has open, or an empty set once it is gone (or may not be inspected)."""
    if sys.platform != 'darwin':
        paths = set()
        try:
            entries = list((Path('/proc') / str(pid) / 'fd').iterdir())
        except OSError:
            return paths
        for entry in entries:
            try:
                paths.add(os.readlink(entry))
            except OSError:
                continue
        return paths
    import ctypes
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    size = libproc.proc_pidinfo(pid, 1, 0, None, 0)  # PROC_PIDLISTFDS: an array of (int32 fd, uint32 type)
    if size <= 0:
        return set()
    listing = ctypes.create_string_buffer(size + 512)
    size = libproc.proc_pidinfo(pid, 1, 0, listing, len(listing))
    info, paths = ctypes.create_string_buffer(VNODE_PATH_INFO_SIZE), set()
    for offset in range(0, max(size, 0) - max(size, 0) % 8, 8):
        fd = int.from_bytes(listing.raw[offset:offset + 4], 'little', signed=True)
        if int.from_bytes(listing.raw[offset + 4:offset + 8], 'little') != 1:  # PROX_FDTYPE_VNODE
            continue
        if libproc.proc_pidfdinfo(pid, fd, 2, info, VNODE_PATH_INFO_SIZE) != VNODE_PATH_INFO_SIZE:
            continue
        raw = info.raw[VNODE_PATH_OFFSET:]
        paths.add(raw[:raw.index(b'\0')].decode(errors='replace'))
    return paths


def check_open_paths():
    """`open_paths` must see a file this process holds open, or the structural check would pass on no evidence."""
    import tempfile
    with tempfile.NamedTemporaryFile(prefix='kelmo-open-') as held:
        seen = open_paths(os.getpid())
        if not any(Path(path).name == Path(held.name).name for path in seen):
            raise RuntimeError(f'open_paths cannot see this process\'s own open file ({held.name})')


def daemon_tree(pid):
    """(pid, role) of kelmod and every process below it."""
    return [(pid, role(command_of(pid)))] + [(child, role(command)) for child, _rss, command in descendants(pid)]


WATCHED = {'journal': ('journal.sqlite3',), 'archive': ('archive.db',), 'outbox': ('outbox',)}


def watched_name(vault, path):
    """Which watched store an open path belongs to (its -wal and -shm files included), or None."""
    if not path.startswith(str(vault)):
        return None
    for name, needles in WATCHED.items():
        if any(needle in Path(path).name or f'/{needle}' in path for needle in needles):
            return name
    return None


class Structure:
    """Every `interval` seconds: the size of kelmod's process tree and which roles hold the watched stores open."""

    def __init__(self, vault, pid, interval=2.0):
        self.vault, self.pid, self.interval = Path(vault).resolve(), pid, interval
        self.counts, self.holders, self.error = [], {}, None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._guarded, daemon=True)

    def sample(self):
        tree = daemon_tree(self.pid)
        self.counts.append(len(tree))
        for pid, name in tree:
            for path in open_paths(pid):
                store = watched_name(self.vault, path)
                if store is not None:
                    by_role = self.holders.setdefault(store, {})
                    by_role[name] = by_role.get(name, 0) + 1

    def _guarded(self):
        try:
            while not self.stopped.wait(self.interval):
                self.sample()
        except Exception as error:  # a run without this evidence proves nothing: reported as an error
            self.error = error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join()


def settled_tree(pid, limit=60.0):
    """The size of kelmod's tree once it holds still for three samples a second apart."""
    end, sizes = time.monotonic() + limit, []
    while time.monotonic() < end:
        sizes.append(len(daemon_tree(pid)))
        if len(sizes) >= 3 and len(set(sizes[-3:])) == 1:
            return sizes[-1]
        time.sleep(1)
    raise RuntimeError(f'kelmod\'s process tree did not settle: {sizes[-5:]}')


class Sampler:
    def __init__(self, interval=1.0):
        self.interval, self.samples, self.stopped = interval, [], threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._guarded, daemon=True)

    def _guarded(self):
        try:
            self._loop()
        except Exception as error:  # a run without samples measures nothing: the caller raises it
            self.error = error

    def _loop(self):
        while not self.stopped.wait(self.interval):
            processes = descendants(os.getpid())
            by_role = {}
            for _, rss, command in processes:
                entry = by_role.setdefault(role(command), {'count': 0, 'rss': 0, 'max_rss': 0})
                entry['count'] += 1
                entry['rss'] += rss
                entry['max_rss'] = max(entry['max_rss'], rss)
            self.samples.append({'t': time.monotonic(), 'count': len(processes),
                                 'rss': sum(rss for _, rss, _ in processes), 'by_role': by_role})

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join()
        if self.error is not None:
            raise RuntimeError(f'process sampling failed: {self.error!r}') from self.error

    def summary(self, since):
        window = [s for s in self.samples if s['t'] >= since] or self.samples
        if not window:
            return {}
        peak = max(window, key=lambda s: s['rss'])
        mb = 1024 * 1024
        return {
            'samples': len(window),
            'process_count_peak': max(s['count'] for s in window),
            'process_count_median': statistics.median(s['count'] for s in window),
            'rss_total_mb_peak': round(peak['rss'] / mb, 1),
            'rss_total_mb_median': round(statistics.median(s['rss'] for s in window) / mb, 1),
            'at_peak_by_role': {name: {'count': v['count'], 'rss_mb': round(v['rss'] / mb, 1),
                                       'max_process_rss_mb': round(v['max_rss'] / mb, 1)}
                                for name, v in sorted(peak['by_role'].items())},
        }


# --- sessions ------------------------------------------------------------------------------------------------

class Results:
    def __init__(self):
        self.latencies, self.errors = {}, {}

    def add(self, op, seconds, error=None):
        self.latencies.setdefault(op, []).append(seconds)
        if error is not None:
            bucket = self.errors.setdefault(op, {})
            bucket[error] = bucket.get(error, 0) + 1

    def summary(self):
        def pct(values, q):
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000, 1)
        return {op: {'count': len(values), 'p50_ms': pct(values, 0.50), 'p95_ms': pct(values, 0.95),
                     'p99_ms': pct(values, 0.99), 'errors': self.errors.get(op, {})}
                for op, values in sorted(self.latencies.items())}


def error_code(error):
    """The refusal code when the message carries one; a tool error over MCP carries only the message, so its
    first words stand in for the code."""
    message = str(error)
    for word in message.replace(':', ' ').split():
        if word.startswith('E_') and word[2:].replace('_', '').isalnum():
            return word
    return f'{type(error).__name__}: {message[:60]}'


def session_command(kelmo, vault):
    broker = json.dumps([sys.executable, str(SCRIPT), 'broker',
                         '--tokenizer', str(Path(vault).parent / 'loadtest-model' / 'tokenizer.json')])
    return kelmo, ['broker', '--command-json', broker, '--fds-env', FDS_ENV, '--', kelmo, '--vault',
                   str(vault), 'mcp']


async def hook_event(kelmo, vault, session_id, daemon=None):
    payload = json.dumps({'session_id': session_id, 'cwd': str(vault), 'hook_event_name': 'SessionStart'})
    argv = ([daemon['kelmod'], 'hook', 'start', '--provider', 'claude', '--runtime-dir', daemon['runtime'],
             '--state', str(Path(vault) / '.state')] if daemon
            else [kelmo, '--vault', str(vault), 'hook', 'start', '--provider', 'claude'])
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate(payload.encode())  # stderr: refusal codes only
    if process.returncode:
        raise RuntimeError(error_code(stderr.decode()) if b'E_' in stderr else f'exit {process.returncode}')


async def run_session(number, kelmo, vault, deadline, results, ids, ready, logs, daemon=None):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
    rng = random.Random(number)
    session_id = f'loadtest-{number:04d}'
    started = time.monotonic()
    try:
        if daemon:
            transport = StreamableHttpTransport(daemon['url'], headers={'Authorization': 'Bearer ' + daemon['token']})
        else:
            command, argv = session_command(kelmo, vault)
            transport = StdioTransport(command, argv, env=dict(os.environ), keep_alive=False,
                                       log_file=logs / f'session-{number:04d}.log')
        async with Client(transport) as client:
            results.add('session_start', time.monotonic() - started)
            ready.append(number)
            while time.monotonic() < deadline:
                op = rng.choices(('search', 'get', 'store', 'create', 'hook'), (50, 20, 15, 5, 10))[0]
                begin = time.monotonic()
                try:
                    if op == 'search':
                        await client.call_tool('memory_search', {'query_en': ' '.join(rng.sample(EN, 3)),
                                                                 'query_ru': ' '.join(rng.sample(RU, 3)),
                                                                 'limit': 5})
                    elif op == 'get':
                        if not ids:
                            continue
                        await client.call_tool('memory_get', {'id': rng.choice(ids), 'chunk_idx': 0})
                    elif op == 'store':
                        stored = await client.call_tool('memory_store', {
                            'content': text(rng, f'{number}-{begin}'), 'kind': 'event', 'domain': 'dev',
                            'project': PROJECT, 'tags': ['loadtest']})
                        ids.append(stored.data['id'])
                    elif op == 'create':
                        await client.call_tool('memory_create', {
                            'content': f'---\ndate: 2026-11-10\nproject: {PROJECT}\ndomain: dev\nstatus: shipped\n'
                                       f'tags: [loadtest, synthetic, harness]\n---\n\n{text(rng, number)}\n',
                            'project': PROJECT, 'idempotency_key': f'{session_id}-{begin}'})
                    else:
                        await hook_event(kelmo, vault, session_id, daemon)
                    results.add(op, time.monotonic() - begin)
                except Exception as error:  # every refusal is counted by its code
                    results.add(op, time.monotonic() - begin, error_code(error))
    except Exception as error:
        results.add('session_start', time.monotonic() - started, error_code(error))


def seed_ids(vault, count=200):
    import sqlite3
    database = Path(vault) / '.state' / 'archive.db'
    candidates = [database] + sorted(Path(vault).glob('**/archive.db'))
    for path in candidates:
        if path.exists():
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
                try:
                    return [row[0] for row in connection.execute(
                        'SELECT id FROM memories ORDER BY random() LIMIT ?', (count,))]
                except sqlite3.Error:
                    continue
    return []


def start_daemon(kelmod, vault, logs, archive_in_process=False):
    """`kelmod serve` for the vault with its cores and the HTTP endpoint; returns how sessions reach it.
    `archive_in_process` runs the archive core without its confined worker: inside a sandbox that forbids a
    nested one (the test sandbox on macOS) the confined worker cannot start."""
    import tempfile
    sys.path.insert(0, str(SCRIPT.parent.parent / 'src'))
    from kelmo.core.spec import build
    runtime = tempfile.mkdtemp(prefix='kl', dir='/tmp')
    spec = build(vault, runtime_dir=runtime, source_root=SCRIPT.parent.parent / 'src', archive_core=True,
                 archive_in_process=archive_in_process, embed_core=True, kelmod=kelmod, http_port=0)
    spec_path = Path(logs) / 'daemon.spec.json'
    spec_path.write_text(json.dumps(spec))
    log = (Path(logs) / 'kelmod.log').open('wb')
    process = subprocess.Popen([kelmod, 'serve', '--spec', str(spec_path)], stdout=log, stderr=log)
    endpoint = Path(runtime) / 'endpoint.json'
    limit = time.monotonic() + 60
    while not endpoint.exists():
        if process.poll() is not None or time.monotonic() > limit:
            process.kill()
            raise SystemExit(f'kelmod did not start; see {logs}/kelmod.log')
        time.sleep(0.1)
    url = json.loads(endpoint.read_text())['url']
    token = (Path(vault) / '.state' / 'http-token').read_text().strip()
    return {'kelmod': kelmod, 'runtime': runtime, 'url': url, 'token': token, 'process': process}


async def drive(kelmo, vault, sessions, seconds, warmup, daemon=None):
    results, ids, ready = Results(), seed_ids(vault), []
    logs = Path(vault).parent / f'{Path(vault).name}-logs'
    logs.mkdir(exist_ok=True)
    structure, tree = None, {}
    if daemon:
        check_open_paths()
        tree['before'] = settled_tree(daemon['process'].pid)
    with Sampler() as sampler:
        deadline = time.monotonic() + warmup + seconds
        try:
            tasks = [asyncio.create_task(run_session(n, kelmo, vault, deadline, results, ids, ready, logs, daemon))
                     for n in range(sessions)]
            await asyncio.sleep(warmup)
            measured_from = time.monotonic()
            if daemon:
                structure = Structure(vault, daemon['process'].pid).__enter__()
            # A session stuck on a dead daemon must not hold the run forever.
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=seconds + 120)
            if daemon:
                structure.__exit__()
                tree['after'] = len(daemon_tree(daemon['process'].pid))
        finally:
            if structure is not None:
                structure.__exit__()
            if daemon:
                daemon['process'].terminate()
                try:
                    daemon['process'].wait(timeout=60)
                except subprocess.TimeoutExpired:
                    daemon['process'].kill()
                    daemon['process'].wait()
    report = {'sessions': sessions, 'sessions_ready': len(ready), 'seconds': seconds, 'warmup_seconds': warmup,
              'operations': results.summary(), 'processes': sampler.summary(measured_from)}
    if structure is not None:
        if structure.error is not None:
            raise RuntimeError(f'structural sampling failed: {structure.error!r}') from structure.error
        tree.update(during_min=min(structure.counts, default=None), during_max=max(structure.counts, default=None),
                    samples=len(structure.counts))
        report['structure'] = {'daemon_tree': tree, 'open_by_role': structure.holders}
    return report


def run(args):
    vault = Path(args.vault).absolute()
    kelmo = args.kelmo or shutil.which('kelmo')
    if not kelmo and not args.daemon:
        raise SystemExit('Name the installed kelmo launcher with --kelmo.')
    reports = []
    for sessions in (int(n) for n in args.sessions.split(',')):
        copy = vault.parent / f'{vault.name}-run-{sessions}'
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(vault, copy, symlinks=True)
        daemon = None
        if args.daemon:
            logs = copy.parent / f'{copy.name}-logs'
            logs.mkdir(exist_ok=True)
            daemon = start_daemon(args.daemon, copy, logs, args.archive_in_process)
        report = asyncio.run(drive(kelmo, copy, sessions, args.seconds, args.warmup, daemon))
        report.update(kelmo=kelmo, mode='daemon' if args.daemon else '0.1', vault=str(copy), platform=sys.platform,
                      archive_in_process=bool(args.daemon and args.archive_in_process),
                      started=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        print(json.dumps({k: report[k] for k in ('sessions', 'sessions_ready')} |
                         {'rss_total_mb_median': report['processes'].get('rss_total_mb_median'),
                          'process_count_median': report['processes'].get('process_count_median')}),
              file=sys.stderr)
        reports.append(report)
        if not args.keep:
            shutil.rmtree(copy, ignore_errors=True)
    document = json.dumps({'reports': reports}, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(document + '\n')
    print(document)


# --- drain under load ----------------------------------------------------------------------------------------

def is_refusal(code):
    return code.startswith('E_')


async def drain_session(number, daemon, stop, writes, results):
    """Writes (store, create) and reads (search) until the daemon is gone. Every write is recorded with its
    marker, when it started relative to the stop signal, and how it ended."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    rng = random.Random(1000 + number)
    transport = StreamableHttpTransport(daemon['url'], headers={'Authorization': 'Bearer ' + daemon['token']})
    sequence = 0
    try:
        async with Client(transport) as client:
            while True:
                # A create with `wait` stays in flight for seconds, so the SIGTERM always meets writes mid-call.
                op = rng.choices(('store', 'create', 'create_wait', 'search'), (50, 15, 10, 25))[0]
                sequence += 1
                marker = f'drain-{number:03d}-{sequence:05d}'
                begin = time.monotonic()
                entry = {'marker': marker, 'op': op, 'begin': begin}
                try:
                    if op == 'store':
                        stored = await client.call_tool('memory_store', {
                            'content': text(rng, marker), 'kind': 'event', 'domain': 'dev',
                            'project': PROJECT, 'tags': ['loadtest', 'drain']})
                        entry.update(ok=True, id=stored.data['id'])
                    elif op in ('create', 'create_wait'):
                        await client.call_tool('memory_create', {
                            'content': f'---\ndate: 2026-11-11\nproject: {PROJECT}\ndomain: dev\nstatus: shipped\n'
                                       f'tags: [loadtest, synthetic, drain]\n---\n\n{text(rng, marker)}\n',
                            'project': PROJECT, 'idempotency_key': marker,
                            'wait': 3 if op == 'create_wait' else 0})
                        entry.update(ok=True)
                    else:
                        await client.call_tool('memory_search', {'query_en': ' '.join(rng.sample(EN, 3)),
                                                                 'query_ru': ' '.join(rng.sample(RU, 3)),
                                                                 'limit': 5})
                        entry.update(ok=True)
                    results.add(op, time.monotonic() - begin)
                except Exception as error:  # every failure is classified below; none is retried here
                    code = error_code(error)
                    entry.update(ok=False, error=code)
                    results.add(op, time.monotonic() - begin, code)
                entry['end'] = time.monotonic()
                if op != 'search':
                    writes.append(entry)
                if not entry['ok'] and stop['exited']:
                    return
                if not entry['ok'] and stop['at'] is not None:
                    await asyncio.sleep(0.2)  # the daemon is stopping: refusals come at once, so pace them
    except Exception as error:  # the session's own transport ended: after the daemon exited this is expected
        results.add('session', 0.0, error_code(error))


def archived(vault, writes):
    """Which writes left a trace, by the marker each one carries: a store in the archive, a create in the
    archive or in its captured source (a create is captured into the journal first and archived later).
    Read only after kelmod exited, so nothing writes while this reads."""
    import re
    import sqlite3
    database = Path(vault) / '.state' / 'archive.db'
    candidates = [database] + sorted(Path(vault).glob('**/archive.db'))
    archive = next((path for path in candidates if path.exists()), None)
    journal = Path(vault) / '.state' / 'journal.sqlite3'
    found, sources = set(), 0
    pattern = re.compile(r'Record (drain-\d{3}-\d{5})\.')
    if archive is not None:
        with sqlite3.connect(f'file:{archive}?mode=ro', uri=True) as connection:
            for (content,) in connection.execute("SELECT content FROM memories WHERE content LIKE '%Record drain-%'"):
                found.update(pattern.findall(content))
    in_archive = len(found)
    if journal.exists():
        with sqlite3.connect(f'file:{journal}?mode=ro', uri=True) as connection:
            for (path,) in connection.execute("SELECT source_path FROM operations WHERE intent = 'create'"):
                source = Path(path) if path.startswith('/') else Path(vault) / path
                try:
                    found.update(pattern.findall(source.read_text()))
                    sources += 1
                except OSError:
                    continue
    for entry in writes:
        entry['persisted'] = entry['marker'] in found
    return {'archive': str(archive), 'markers_in_archive': in_archive, 'create_sources_read': sources}


async def drain_drive(vault, daemon, sessions, stop_after):
    results, writes = Results(), []
    stop = {'at': None, 'exited': False}
    tasks = [asyncio.create_task(drain_session(n, daemon, stop, writes, results)) for n in range(sessions)]
    await asyncio.sleep(stop_after)
    process = daemon['process']
    stop['at'] = time.monotonic()
    process.terminate()
    while process.poll() is None and time.monotonic() - stop['at'] < 60:
        await asyncio.sleep(0.05)
    stopped_in = time.monotonic() - stop['at']
    if process.poll() is None:
        process.kill()
        process.wait()
    stop['exited'] = True
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=60)
    return results, writes, process.returncode, stop['at'], stopped_in


def drain(args):
    """SIGTERM to kelmod in the middle of a write load; afterwards every acknowledged write must be found."""
    vault = Path(args.vault).absolute()
    copy = vault.parent / f'{vault.name}-drain'
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(vault, copy, symlinks=True)
    logs = copy.parent / f'{copy.name}-logs'
    logs.mkdir(exist_ok=True)
    daemon = start_daemon(args.daemon, copy, logs, args.archive_in_process)
    results, writes, exit_code, stop_at, stopped_in = asyncio.run(
        drain_drive(copy, daemon, args.sessions, args.stop_after))
    trace = archived(copy, writes)
    acknowledged = [w for w in writes if w['ok']]
    lost = [w for w in acknowledged if not w['persisted']]
    refused = [w for w in writes if not w['ok'] and is_refusal(w['error'])]
    broken = [w for w in writes if not w['ok'] and not is_refusal(w['error'])]
    # In flight at the SIGTERM: begun before it, ended after it. Each must end with its reply or a refusal.
    in_flight = [w for w in writes if w['begin'] < stop_at <= w['end']]
    unanswered = [w for w in broken if w['begin'] < stop_at]

    def counts(entries):
        return {code: sum(1 for w in entries if w['error'] == code) for code in sorted({w['error'] for w in entries})}

    report = {
        'sessions': args.sessions, 'stop_after_seconds': args.stop_after,
        'archive_in_process': bool(args.archive_in_process), 'platform': sys.platform,
        'kelmod_exit_code': exit_code, 'stop_seconds': round(stopped_in, 2),
        'writes': len(writes), 'acknowledged': len(acknowledged),
        'in_flight_at_stop': len(in_flight),
        'in_flight_acknowledged': sum(1 for w in in_flight if w['ok']),
        'in_flight_refused': counts([w for w in in_flight if not w['ok'] and is_refusal(w['error'])]),
        'lost': [f'{w["op"]} {w["marker"]}' for w in lost],
        'unanswered_before_stop': [f'{w["op"]} {w["marker"]} {w["error"]}' for w in unanswered],
        'refused': counts(refused),
        'refused_but_persisted': [f'{w["op"]} {w["marker"]}' for w in refused if w['persisted']],
        'transport_failures_after_stop': counts([w for w in broken if w['begin'] >= stop_at]),
        'transport_failures_persisted': [f'{w["op"]} {w["marker"]}' for w in broken if w['persisted']],
        'operations': results.summary(), 'trace': trace,
        'started': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    report['passed'] = not lost and not unanswered and exit_code == 0 and stopped_in < 40
    document = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(document + '\n')
    print(document)
    if not args.keep:
        shutil.rmtree(copy, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    b = commands.add_parser('build', help='make a synthetic vault')
    b.add_argument('--vault', required=True)
    b.add_argument('--records', type=int, default=10000)
    b.add_argument('--seed', type=int, default=1)
    r = commands.add_parser('run', help='drive N sessions against copies of a built vault')
    r.add_argument('--vault', required=True)
    r.add_argument('--sessions', default='5,50')
    r.add_argument('--seconds', type=int, default=60)
    r.add_argument('--warmup', type=int, default=15)
    r.add_argument('--kelmo', help='the installed kelmo launcher (default: kelmo on PATH)')
    r.add_argument('--out')
    r.add_argument('--keep', action='store_true', help='keep the per-run vault copies')
    r.add_argument('--daemon', metavar='KELMOD', help='measure the 0.2 shape: one kelmod serve for the vault')
    r.add_argument('--archive-in-process', action='store_true',
                   help='with --daemon: no confined archive worker (needed inside a sandbox that forbids nesting)')
    d = commands.add_parser('drain', help='stop kelmod in the middle of a write load and check every write')
    d.add_argument('--vault', required=True)
    d.add_argument('--daemon', metavar='KELMOD', required=True)
    d.add_argument('--sessions', type=int, default=20)
    d.add_argument('--stop-after', type=float, default=20.0, help='seconds of load before the SIGTERM')
    d.add_argument('--out')
    d.add_argument('--keep', action='store_true', help='keep the vault copy')
    d.add_argument('--archive-in-process', action='store_true',
                   help='no confined archive worker (needed inside a sandbox that forbids nesting)')
    k = commands.add_parser('broker', help='the synthetic embedding broker (stdio), used by sessions')
    k.add_argument('--tokenizer', required=True)
    args = parser.parse_args(argv)
    if args.command == 'build':
        build(args)
    elif args.command == 'run':
        run(args)
    elif args.command == 'drain':
        drain(args)
    else:
        serve_broker(args.tokenizer, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == '__main__':
    main()
