"""Shared-mode client: durable outbox, one bounded channel and a hash-checked context cache.

Every write is stored in the local outbox before it is sent and is retried under the same
idempotency key until the server settles it. The server assigns identity from the channel
its trusted launcher opened; no request field can choose an origin, a server path, a
provider command or a writer epoch. The cache is a read aid: a stale copy is labelled as
stale, is never complete and never acts as a writer or as proof of current state.
"""
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import select
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import CLIENT_FILE, ClientProfile
from .errors import KelmoError
from .server import NATIVE_ID, PROTOCOL, PipeReader, encode_frame

# Only an unreachable or foreign server falls back to the labelled cache.
CACHE_FALLBACK = frozenset({'E_TRANSPORT', 'E_AUTHORITY'})
# SessionStart runs under a 20 s hook timeout; everything it does, reconnect included, ends before this.
START_DEADLINE_SECONDS = 15
# The part of that deadline the flush never spends (at most a third of it): the context request, or the
# labelled cache after it.
CONTEXT_RESERVE_SECONDS = 5
BINDING_FILE = 'hot-work-binding.json'
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*')
REPLY_FIELDS = frozenset({'protocol', 'request_id', 'ok', 'result', 'error', 'replayed'})
EVENTS = {'session-stop': 'Stop', 'session-checkpoint': 'PreCompact', 'session-end': 'SessionEnd'}
# A transcript travels in pieces of this many raw bytes, each one bounded frame; a transcript itself has
# no size ceiling, and a capture sends only the bytes after the ones this client already delivered.
PIECE_BYTES = 8 * 1048576
# A transcript read is repeated this many times while the provider is still appending to the file.
CAPTURE_READ_ATTEMPTS = 4
CAPTURE_READ_PAUSE_SECONDS = 0.15
# A lifecycle event that has nothing to capture is recorded under its reason instead of failing the hook: a
# transcript replaced under the read, and one a provider names before writing it (a session that ends
# before its first message).
CAPTURE_SKIPS = {'E_CAPTURE_RETRY': 'transcript-changing', 'E_TRANSCRIPT_ABSENT': 'transcript-absent'}
TRANSCRIPT_OPS = frozenset({'session_capture', 'session_append'})


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def utc_now():
    return datetime.now(UTC).isoformat(timespec='microseconds')


class Unbound(KelmoError):
    """No server verdict on this request: the launcher, the channel, hello, the authority pin, or a reply
    bound to no request. A queued write is never settled by one; it stays pending under its key."""


class Unsent(Unbound):
    """Refused here before a byte was written (a frame over the bound, an operation the connected server
    does not advertise); the channel stays healthy and the entry stays pending."""


def _atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def profile_text(authority_id, command, *, sessions=None, timeout_seconds=20,
                 max_snapshot_bytes=16 * 1048576):
    """The owner-selected shared connection and enrolled local transcript roots."""
    sessions = sessions or {}
    text = ('schema_version = 1\nmode = "shared"\nauthority_id = ' + json.dumps(authority_id)
            + '\ncommand = ' + json.dumps(list(command)) + '\ntimeout_seconds = ' + json.dumps(timeout_seconds)
            + '\n\n[sessions]\nmax_snapshot_bytes = ' + json.dumps(max_snapshot_bytes) + '\n')
    for provider in ('claude', 'codex'):
        if provider in sessions:
            text += provider + ' = ' + json.dumps([str(item) for item in sessions[provider]]) + '\n'
    return text


def prepare_profile(root, authority_id, command, *, sessions=None, timeout_seconds=20,
                    max_snapshot_bytes=16 * 1048576):
    """Write an explicit opt-in client profile. Nothing is registered with any agent or host."""
    root = Path(root).expanduser().resolve()
    if (root / 'kelmo.toml').exists():
        raise KelmoError('E_CONFIG', 'This directory is a local vault; a shared client uses its own directory.')
    text = profile_text(authority_id, command, sessions=sessions, timeout_seconds=timeout_seconds,
                        max_snapshot_bytes=max_snapshot_bytes)
    ClientProfile.parse(root, text.encode('utf-8'))
    path = root / CLIENT_FILE
    if path.exists():
        if path.read_text(encoding='utf-8') != text:
            raise KelmoError('E_CONFIG', 'A different shared-client profile already exists; it is not replaced.')
        return ClientProfile.load(root).as_dict()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write(path, text.encode('utf-8'))
    try:
        return ClientProfile.load(root).as_dict()
    except KelmoError:
        path.unlink()
        raise


COMPACT_MIN_FREE_BYTES = 16 * 1048576
OUTBOX_WAIT_SECONDS = 60
REFUSED_SHOWN = 20


OUTBOX_STALL_SECONDS = 3600


def outbox_stall_warning(summary, clock=None):
    """The warning for an outbox whose oldest undelivered write is older than OUTBOX_STALL_SECONDS, or None."""
    head = summary.get('oldest_pending')
    if not head:
        return None
    try:
        age = (clock or time.time)() - datetime.fromisoformat(head['created_at']).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    if age <= OUTBOX_STALL_SECONDS:
        return None
    return (f"{summary['pending']} write(s) have waited {int(age // 60)} min to reach the server behind "
            f"{head['op']} {head['idempotency_key']} (last refusal {head.get('last_error') or 'none'}); "
            'check the server for a stuck lock or a stopped worker, then run kelmo client flush.')


class Outbox:
    """The durable local queue; an entry exists before any byte is sent."""

    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A note is queued even while a compaction holds the file: waiting a minute beats refusing it.
        self.conn = sqlite3.connect(directory / 'outbox.sqlite3', timeout=OUTBOX_WAIT_SECONDS,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA synchronous=FULL')
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS entries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
                op TEXT NOT NULL, args TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL,
                settled_at TEXT, outcome TEXT)''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS transcripts (
                provider TEXT NOT NULL, native_id TEXT NOT NULL, file_identity TEXT NOT NULL,
                size INTEGER NOT NULL, generation INTEGER NOT NULL, PRIMARY KEY (provider, native_id))''')
            # The bytes of the current generation already queued for the server, so the next capture sends
            # only what follows them. An older row has none and starts its next capture from the beginning.
            columns = {row[1] for row in self.conn.execute('PRAGMA table_info(transcripts)')}
            for column, definition in (('sent_bytes', 'INTEGER NOT NULL DEFAULT 0'),
                                       ('sent_sha256', "TEXT NOT NULL DEFAULT ''"),
                                       ('restarts', 'INTEGER NOT NULL DEFAULT 0')):
                if column not in columns:
                    self.conn.execute(f'ALTER TABLE transcripts ADD COLUMN {column} {definition}')
            # Session events this client deliberately did not capture; a record, never a delivery.
            self.conn.execute('''CREATE TABLE IF NOT EXISTS skips (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, event TEXT NOT NULL,
                native_id TEXT, reason TEXT NOT NULL, at TEXT NOT NULL)''')
            self._shed_transcript_bytes()

    def _shed_transcript_bytes(self, seq=None):
        """A settled transcript entry keeps its receipt, not its bytes: the outbox once held every delivered
        transcript whole. Called inside the open transaction."""
        where = 'seq=?' if seq is not None else "state != 'pending' AND op IN ('session_capture', 'session_append')"
        rows = self.conn.execute(f'SELECT seq, args FROM entries WHERE {where} AND args LIKE \'%"content":"_%\'',
                                 (seq,) if seq is not None else ()).fetchall()
        for row in rows:
            args = json.loads(row['args'])
            if args.get('content'):
                self.conn.execute('UPDATE entries SET args=? WHERE seq=?',
                                  (canonical(dict(args, content='')), row['seq']))

    def compact(self, min_free_bytes=COMPACT_MIN_FREE_BYTES):
        """Rewrite the file when more than half of it, and at least `min_free_bytes`, is free pages.

        Settled transcript entries drop their bytes but SQLite keeps the pages, so the file only grows.
        A busy file is left for the next flush.
        Returns the bytes given back, 0 when nothing was due."""
        pages, free, size = (self.conn.execute(f'PRAGMA {name}').fetchone()[0]
                             for name in ('page_count', 'freelist_count', 'page_size'))
        if free * size < min_free_bytes or free * 2 < pages:
            return 0
        try:
            self.conn.execute('VACUUM')
        except sqlite3.OperationalError:
            return 0
        return (pages - self.conn.execute('PRAGMA page_count').fetchone()[0]) * size

    def record_skip(self, provider, event, native_id, reason):
        with self.conn:
            self.conn.execute('INSERT INTO skips (provider,event,native_id,reason,at) VALUES (?,?,?,?,?)',
                              (provider, event, native_id, reason, utc_now()))

    def close(self):
        self.conn.close()

    def enqueue(self, op, args, key):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            self._insert(op, args, key)
        return self.get(key)

    def _insert(self, op, args, key):
        """One entry under its key inside the open transaction; the same request again is the same entry."""
        fingerprint = digest(canonical({'op': op, 'args': args}))
        row = self.conn.execute('SELECT fingerprint FROM entries WHERE idempotency_key=?', (key,)).fetchone()
        if row is not None:
            if row['fingerprint'] != fingerprint:
                raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different queued request.')
            return
        self.conn.execute('INSERT INTO entries (idempotency_key,op,args,fingerprint,state,created_at) '
                          "VALUES (?,?,?,?, 'pending', ?)", (key, op, canonical(args), fingerprint, utc_now()))

    def get(self, key):
        row = self.conn.execute('SELECT * FROM entries WHERE idempotency_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def pending(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM entries WHERE state='pending' ORDER BY seq")]

    def attempt(self, key):
        with self.conn:
            self.conn.execute('UPDATE entries SET attempts=attempts+1 WHERE idempotency_key=?', (key,))

    def note(self, key, code):
        with self.conn:
            self.conn.execute('UPDATE entries SET last_error=? WHERE idempotency_key=?', (code, key))

    def settle(self, key, state, outcome):
        with self.conn:
            self.conn.execute("UPDATE entries SET state=?, outcome=?, settled_at=? "
                              "WHERE idempotency_key=? AND state='pending'",
                              (state, canonical(outcome), utc_now(), key))
            row = self.conn.execute('SELECT seq, op FROM entries WHERE idempotency_key=?', (key,)).fetchone()
            if row is not None and row['op'] in TRANSCRIPT_OPS:
                self._shed_transcript_bytes(row['seq'])

    def resend(self, codes, *, apply=False):
        """Queue refused notes again, unchanged, once a server fix or a registration makes them acceptable.

        Only `create` requests are resent: their text is whole in the entry. The server keeps a refusal
        under its key, so each goes out under the key plus `#resend<n>`; the refused entry becomes
        `resent` and names its successor. Without `apply` nothing changes. The refusals are read under the
        write lock, so two runs at once never resend one note twice.

        A note is sent once: a refusal whose text another queued or delivered note already carries is not
        sent again but names that note (`duplicate`), and of several refusals with one text only the newest
        goes out; the older ones name it too."""
        def text(row):
            return json.loads(row['args']).get('content', '').strip()

        items = []
        with self.conn:
            if apply:
                self.conn.execute('BEGIN IMMEDIATE')
            rows = [row for row in self.conn.execute(
                "SELECT * FROM entries WHERE state='refused' AND op='create' ORDER BY seq")
                    if json.loads(row['outcome'] or '{}').get('code') in codes]
            carried = {text(row): row['idempotency_key'] for row in self.conn.execute(
                "SELECT idempotency_key, args FROM entries WHERE op='create' AND state IN ('pending', 'delivered') "
                'ORDER BY seq')}
            newest = {text(row): row['seq'] for row in rows}
            for row in sorted(rows, key=lambda row: row['seq'] != newest[text(row)]):
                args, outcome = json.loads(row['args']), json.loads(row['outcome'])
                duplicate = text(row) in carried
                if duplicate:
                    key = carried[text(row)]
                else:
                    base, number = row['idempotency_key'].split('#resend', 1)[0], 1
                    while self.get(f'{base}#resend{number}') is not None:
                        number += 1
                    key = carried[text(row)] = f'{base}#resend{number}'
                items.append((row['seq'], {'idempotency_key': row['idempotency_key'], 'resend_as': key,
                                           'code': outcome['code'], 'project': args.get('project'),
                                           'selected': args.get('selected', []),
                                           **({'duplicate': True} if duplicate else {})}))
                if apply:
                    if not duplicate:
                        self._insert('create', args, key)
                    self.conn.execute("UPDATE entries SET state='resent', outcome=? WHERE seq=?",
                                      (canonical(dict(outcome, resent_as=key)), row['seq']))
        return [item for _, item in sorted(items, key=lambda pair: pair[0])]

    def summary(self):
        counts = {row['state']: row['n'] for row in self.conn.execute(
            'SELECT state, COUNT(*) AS n FROM entries GROUP BY state')}
        refused = [{'idempotency_key': row['idempotency_key'], 'op': row['op'], 'at': row['settled_at'],
                    **json.loads(row['outcome'])}
                   for row in self.conn.execute("SELECT * FROM entries WHERE state='refused' ORDER BY seq DESC "
                                                'LIMIT ?', (REFUSED_SHOWN,))][::-1]
        skipped = {row['reason']: row['n'] for row in self.conn.execute(
            'SELECT reason, COUNT(*) AS n FROM skips GROUP BY reason')}
        result = {'pending': counts.get('pending', 0), 'delivered': counts.get('delivered', 0),
                  'refused': refused, 'skipped': skipped}
        if counts.get('pending'):
            # The head of the queue: every later write waits behind it, so its age is the delay of all.
            head = self.conn.execute("SELECT idempotency_key, op, attempts, last_error, created_at FROM entries "
                                     "WHERE state='pending' ORDER BY seq LIMIT 1").fetchone()
            result['oldest_pending'] = dict(head)
        if counts.get('resent'):
            result['resent'] = counts['resent']
        if counts.get('refused', 0) > len(refused):
            # Refusals are final and only accumulate; the newest stay readable, the rest are counted.
            result['refused_total'] = counts['refused']
            result['refused_by_code'] = {row['code']: row['n'] for row in self.conn.execute(
                "SELECT json_extract(outcome, '$.code') AS code, COUNT(*) AS n FROM entries "
                "WHERE state='refused' GROUP BY code")}
        return result

    def queue_transcript(self, provider, native_id, identity, raw, pieces):
        """Queue one capture of `raw` as the pieces `pieces(generation, offset, restarts)` returns, and
        record that the whole of `raw` is now sent, in one transaction.

        The generation is the client's own count of replacements: a new file, a shorter file or a file that
        no longer starts with the bytes already sent is a new generation, sent from its beginning. The same
        file grown past the sent bytes continues its generation from them.
        """
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute('SELECT * FROM transcripts WHERE provider=? AND native_id=?',
                                    (provider, native_id)).fetchone()
            generation, offset, restarts = 1, 0, 0
            if row is not None:
                sent, restarts = row['sent_bytes'], row['restarts']
                continued = (row['file_identity'] == identity and len(raw) >= max(row['size'], sent)
                             and (sent == 0 or digest(raw[:sent]) == row['sent_sha256']))
                generation = row['generation'] + (not continued)
                offset = sent if continued else 0
            queued = pieces(generation, offset, restarts)
            for key, op, args in queued:
                self._insert(op, args, key)
            self.conn.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?,?,?)',
                              (provider, native_id, identity, len(raw), generation, len(raw), digest(raw), restarts))
        return generation, [key for key, _, _ in queued]

    def reset_transcript(self, provider, native_id):
        """Forget the sent position: the server no longer holds those bytes, so the next capture of this
        session starts from the beginning of the transcript. The restart count enters the next request keys,
        so the server applies the resent bytes instead of replaying the earlier answers for them."""
        with self.conn:
            self.conn.execute("UPDATE transcripts SET sent_bytes=0, sent_sha256='', restarts=restarts+1 "
                              "WHERE provider=? AND native_id=?", (provider, native_id))


class StdioChannel:
    """The configured channel command; its stdin/stdout carry bounded frames, nothing else."""

    def __init__(self, command, timeout):
        # The client sets `timeout` before each request, so one deadline bounds every reply.
        self.timeout = timeout
        self.grace = 5
        try:
            self.process = subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as error:
            raise KelmoError('E_TRANSPORT', 'The configured shared-memory channel could not be started.') from error
        self.reader = PipeReader(self.process.stdout)
        # Writes are paced by select under the request deadline, so a server that stops reading cannot
        # block a caller past it.
        os.set_blocking(self.process.stdin.fileno(), False)

    def request(self, frame):
        # A frame too large to encode is refused here (E_LIMIT) before a byte is written, so the
        # healthy channel stays open.
        data = encode_frame(frame)
        deadline = time.monotonic() + self.timeout
        try:
            self._write(data, deadline)
            return self.reader.frame(max(0.0, deadline - time.monotonic()))
        except OSError as error:
            self.grace = 0
            self.close()
            raise KelmoError('E_TRANSPORT', 'The shared-memory channel closed.') from error
        except KelmoError as error:
            # A channel that missed its deadline or broke framing is ended now, never waited for.
            self.grace = 0
            self.close()
            raise KelmoError('E_TRANSPORT', str(error)) from error

    def _write(self, data, deadline):
        """The whole frame within the deadline, or E_TRANSPORT; the caller then closes the channel, so a
        frame cut short is never followed by another on the same stream."""
        descriptor = self.process.stdin.fileno()
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [descriptor], [], left)[1]:
                raise KelmoError('E_TRANSPORT', 'The channel did not accept the request before its deadline.')
            try:
                view = view[os.write(descriptor, view):]
            except BlockingIOError:
                continue

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


def _git(cwd, *args, deadline=None):
    timeout = 5
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise KelmoError('E_BINDING', 'The repository identity could not be read before the deadline.')
    try:
        result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        if deadline is not None:
            raise KelmoError('E_BINDING', 'The repository identity could not be read before the deadline.') from error
        return None
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def read_work_binding(gitdir, common, branch):
    """The worktree's bound work page: (work, project, status). Only a bound page is sent."""
    path = Path(gitdir) / BINDING_FILE
    if not path.is_file() or path.is_symlink():
        return None, None, 'unbound'
    try:
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw)
    except (OSError, ValueError):
        return None, None, 'invalid-binding'
    if not isinstance(data, dict):
        return None, None, 'invalid-binding'
    if data.get('version') == 1:
        return None, None, 'legacy-binding'
    identity = (data.get('gitdir') == str(Path(gitdir).resolve())
                and data.get('common_dir') == str(Path(common).resolve()))
    if (data.get('version') != 2 or not identity or data.get('state') not in ('bound', 'pending')
            or not isinstance(data.get('work'), str) or not SLUG.fullmatch(data['work'])):
        return None, None, 'invalid-binding'
    if branch is None or data.get('branch') != branch:
        return None, None, 'stale-branch'
    project = data.get('project_id') if isinstance(data.get('project_id'), str) else None
    if data['state'] == 'pending':
        return None, project, 'pending'
    return data['work'], project, 'bound'


def local_binding(cwd, *, deadline=None):
    """Resolve the current repository/worktree locally; only canonical identities leave the machine.
    Under a deadline, a Git read that cannot finish in time refuses (E_BINDING) instead of guessing."""
    from .context_binding import canonical_remote
    unbound = {'repository': None, 'branch': None, 'work': None, 'work_project': None,
               'binding_status': None}
    if cwd is None:
        return dict(unbound, repository_status='global')
    if not isinstance(cwd, (str, Path)) or not Path(cwd).is_absolute():
        raise KelmoError('E_INPUT', 'HOT cwd must be an absolute path.')
    path = Path(cwd)

    def git(*args):
        return _git(path, *args, deadline=deadline)
    if git('rev-parse', '--show-toplevel') is None:
        return dict(unbound, repository_status='not-a-repository')
    common = git('rev-parse', '--path-format=absolute', '--git-common-dir')
    gitdir = git('rev-parse', '--absolute-git-dir')
    branch = git('symbolic-ref', '-q', '--short', 'HEAD')
    remote = (git('config', '--get', 'branch.' + branch + '.remote') if branch else None) or 'origin'
    url = git('remote', 'get-url', remote)
    result = dict(unbound, branch=branch, repository_status='repository')
    if url is None:
        result['repository_status'] = 'no-remote'
    else:
        try:
            result['repository'] = canonical_remote(url)
        except ValueError:
            result['repository_status'] = 'unsupported-remote'
    all_remotes = git('config', '--get-regexp', r'^remote\..*\.url$')
    identities = set()
    for row in (all_remotes or '').splitlines():
        parts = row.split(None, 1)
        if len(parts) == 2:
            try:
                identities.add(canonical_remote(parts[1]))
            except ValueError:
                continue
    if len(identities) > 1 or (identities and result['repository'] is None):
        result['repositories'] = sorted(identities)
    if gitdir and common:
        work, project, status = read_work_binding(gitdir, common, branch)
        result.update(work=work, work_project=project, binding_status=status)
    return result


def verify_context(result):
    """Split a delivered context into hash-checked page blocks, or refuse it."""
    from .contracts import Generation
    try:
        text = result['context']
        if digest(text) != result['context_sha256']:
            raise ValueError('context hash')
        Generation.from_dict(result['generation'])
        header, _, rest = text.partition('\n\n')
        data, position, blocks = rest.encode('utf-8'), 0, []
        for index, page in enumerate(result['pages']):
            if index:
                if data[position:position + 2] != b'\n\n':
                    raise ValueError('separator')
                position += 2
            prefix = ('## ' + page['path'] + '\n\n').encode('utf-8')
            if data[position:position + len(prefix)] != prefix:
                raise ValueError('block')
            position += len(prefix)
            body = data[position:position + page['bytes']]
            position += page['bytes']
            if len(body) != page['bytes'] or digest(body) != page['sha256']:
                raise ValueError('page hash')
            blocks.append({'path': page['path'], 'sha256': page['sha256'], 'text': body.decode('utf-8')})
        if position != len(data):
            raise ValueError('trailing bytes')
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise KelmoError('E_CACHE', 'Delivered context failed its page hash check; nothing was cached.') from error
    return header, blocks


CACHE_SCHEMA = 2


def _sealed(record, key, authority_id):
    """The Generation of a cache record whose binding and integrity hold, else ValueError.

    The whole delivered context is rebuilt from the header and page blocks and must hash to the
    delivered context_sha256, so no page can be dropped, added or reordered; the header must name the
    record's generation and its mandatory pages, as hot.context names them; and those pages must lead, in order, as the
    server renders them. A record of an older schema carries none of this and is refused."""
    from .contracts import Generation
    generation = Generation.from_dict(record['generation'])
    if (record.get('schema') != CACHE_SCHEMA or record.get('key') != key
            or record.get('authority_id') != authority_id or generation.authority_id != authority_id):
        raise ValueError('binding')
    pages, mandatory, header = record['pages'], record['mandatory'], record['header']
    for page in pages:
        if digest(page['text']) != page['sha256']:
            raise ValueError('page hash')
    context = '\n\n'.join([header, *('## ' + page['path'] + '\n\n' + page['text'] for page in pages)])
    if digest(context) != record['context_sha256']:
        raise ValueError('context hash')
    if not header.startswith(f'Kelmo knowledge generation {generation.publication_id} '
                             f'(manifest {generation.manifest_sha256[:12]})'):
        raise ValueError('generation')
    if (not isinstance(mandatory, list) or not all(isinstance(path, str) for path in mandatory)
            or [page['path'] for page in pages[:len(mandatory)]] != mandatory
            or not header.endswith('; mandatory pages: ' + (', '.join(mandatory) or 'none') + '.')):
        raise ValueError('mandatory')
    return generation


class SharedClient:
    """One client root: its outbox, its cache and at most one open channel."""

    def __init__(self, profile, channel_factory=None):
        if not isinstance(profile, ClientProfile):
            raise KelmoError('E_CONFIG', 'A shared client requires an explicit client profile.')
        self.profile = profile
        self.outbox = Outbox(profile.state)
        self._factory = channel_factory or (lambda: StdioChannel(profile.command, profile.timeout_seconds))
        self._channel = None
        self._lock = threading.RLock()
        # Per thread: the edge core runs calls on several threads, each under its own deadline.
        self._local = threading.local()
        self.server = None

    def pause(self, seconds):
        """How a wait sleeps between polls; the edge core replaces it to lend its owner lock out meanwhile."""
        time.sleep(seconds)

    def _thread_state(self):
        # Created on first use as well: a subclass that skips __init__ still gets per-thread deadlines.
        local = self.__dict__.get('_local')
        if local is None:
            local = self.__dict__.setdefault('_local', threading.local())
        return local

    @property
    def _deadline(self):
        return getattr(self._thread_state(), 'deadline', None)

    @_deadline.setter
    def _deadline(self, value):
        self._thread_state().deadline = value

    @contextmanager
    def deadline(self, seconds):
        """Bound everything inside, connecting included, by one monotonic deadline."""
        previous = self._deadline
        limit = time.monotonic() + seconds
        self._deadline = limit if previous is None else min(previous, limit)
        try:
            yield
        finally:
            self._deadline = previous

    def remaining(self):
        """Seconds left before the active deadline (the profile timeout without one); refused at zero."""
        if self._deadline is None:
            return self.profile.timeout_seconds
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise Unbound('E_TRANSPORT', 'The deadline passed before the shared kelmo server answered.')
        return min(self.profile.timeout_seconds, left)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._lock:
            self._drop()
            self.outbox.close()

    def _drop(self):
        if self._channel is not None:
            channel, self._channel = self._channel, None
            if isinstance(channel, StdioChannel) and self._deadline is not None:
                channel.grace = max(0, min(channel.grace, self._deadline - time.monotonic()))
            channel.close()

    def _send(self, channel, op, args, key):
        """One request: (True, result) or (False, error) for a reply bound to it; Unbound otherwise.

        Only a reply naming this request ID carries a server verdict, and its refusal must say whether it
        is retryable: that is the server's decision, never derived here from the request."""
        request_id = uuid.uuid4().hex
        frame = {'protocol': PROTOCOL, 'request_id': request_id, 'op': op, 'args': args}
        if key is not None:
            frame['idempotency_key'] = key
        if op != 'hello':
            # Version skew: an operation the connected server does not advertise is never delivered.
            offered = self.server.get('operations') if isinstance(self.server, dict) else None
            if not isinstance(offered, list) or op not in offered:
                raise Unsent('E_CAPABILITY', f'The connected shared kelmo server does not offer `{op}`; '
                                             'nothing was sent and a queued write stays pending.')
        try:
            encode_frame(frame)
        except KelmoError as error:
            raise Unsent(error.code, str(error)) from error
        if isinstance(channel, StdioChannel):
            channel.timeout = self.remaining()
        try:
            reply = channel.request(frame)
        except Unbound:
            raise
        except KelmoError as error:
            raise Unbound(error.code, str(error)) from error
        if (not isinstance(reply, dict) or set(reply) - REPLY_FIELDS or reply.get('protocol') != PROTOCOL
                or type(reply.get('ok')) is not bool):
            raise Unbound('E_TRANSPORT', 'The shared-memory reply is outside the protocol.')
        error = reply.get('error') if isinstance(reply.get('error'), dict) else {}
        if reply.get('request_id') != request_id:
            # A startup or framing refusal answers no request; it is reported, never applied to one.
            code = error.get('code') if reply.get('request_id') is None and reply['ok'] is False else None
            raise Unbound(str(code or 'E_TRANSPORT'),
                          str(error.get('message') or 'The reply does not answer this request.'))
        if reply['ok']:
            return True, reply.get('result')
        if not isinstance(error.get('code'), str) or type(error.get('retryable')) is not bool:
            raise Unbound('E_TRANSPORT', 'The refusal names no server code or retryability.')
        return False, {'code': error['code'], 'message': str(error.get('message', '')),
                       'retryable': error['retryable']}

    def _connect(self):
        if self._channel is None:
            self.remaining()
            try:
                channel = self._factory()
            except Unbound:
                raise
            except KelmoError as error:
                raise Unbound(error.code, str(error)) from error
            except OSError as error:
                raise Unbound('E_TRANSPORT', 'The configured shared-memory channel could not be started.') from error
            try:
                ok, hello = self._send(channel, 'hello', {}, None)
                if not ok:
                    raise Unbound(hello['code'], hello['message'])
                if not isinstance(hello, dict) or hello.get('authority_id') != self.profile.authority_id:
                    raise Unbound('E_AUTHORITY', 'The channel reached a different memory authority than '
                                                 'this client is pinned to; nothing was sent.')
            except BaseException:
                if isinstance(channel, StdioChannel):
                    channel.grace = 0
                channel.close()
                raise
            self._channel, self.server = channel, hello
        return self._channel

    def call(self, op, args=None):
        """A read: no outbox, and a transport failure is reported, never answered from the cache."""
        with self._lock:
            try:
                ok, value = self._send(self._connect(), op, args or {}, None)
            except Unbound as error:
                if not isinstance(error, Unsent):  # a local refusal leaves the channel healthy
                    self._drop()
                raise
            if ok:
                return value
            raise KelmoError(value['code'], value['message'])

    def submit(self, op, args, key):
        """Persist first, then deliver everything queued in order; report this entry's state."""
        # The frame this entry will be sent as, checked before it is queued: an entry that can never fit
        # one bounded frame would block every write behind it.
        try:
            encode_frame({'protocol': PROTOCOL, 'request_id': uuid.uuid4().hex, 'op': op, 'args': args,
                          'idempotency_key': key})
        except KelmoError as error:
            raise KelmoError('E_INPUT', 'This request does not fit one bounded frame; it was not queued.') from error
        with self._lock:
            self.outbox.enqueue(op, args, key)
            self.flush()
            return self._entry_result(self.outbox.get(key))

    @contextmanager
    def _flush_lock(self):
        """One flusher per client root across processes, waited for within the active deadline."""
        with (self.profile.state / 'flush.lock').open('a') as lock:
            limit = time.monotonic() + self.remaining()
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= limit:
                        yield False
                        return
                    time.sleep(0.05)
            yield True

    def flush(self):
        """Deliver pending entries in order. A bound refusal the server calls final settles its entry; a
        retryable one, or no verdict at all (launcher, channel, hello, a reply bound to no request), leaves
        it and everything after it pending and stops. `reached` says whether a channel was open."""
        with self._lock:
            try:
                with self._flush_lock() as acquired:
                    if acquired:
                        return self._deliver()
                    stopped = 'E_BUSY'
            except Unbound as error:  # the deadline passed before the lock was even tried
                stopped = error.code
            return {'delivered': 0, 'refused': [], 'pending': len(self.outbox.pending()),
                    'stopped': stopped, 'reached': None}

    def _deliver(self):
        delivered, refused, stopped, reached = 0, [], None, None
        for entry in self.outbox.pending():
            key = entry['idempotency_key']
            try:
                channel = self._connect()
                reached = True
                self.outbox.attempt(key)
                ok, value = self._send(channel, entry['op'], json.loads(entry['args']), key)
            except Unbound as error:
                if not isinstance(error, Unsent):  # a local refusal leaves the channel healthy
                    self._drop()
                self.outbox.note(key, error.code)
                stopped, reached = error.code, bool(reached)
                break
            if ok:
                self.outbox.settle(key, 'delivered', value)
                delivered += 1
                continue
            if value['retryable']:
                self.outbox.note(key, value['code'])
                stopped = value['code']
                break
            outcome = {'code': value['code'], 'message': value['message']}
            self.outbox.settle(key, 'refused', outcome)
            if entry['op'] == 'session_append' and value['code'] == 'E_CAPTURE_BASE':
                # The server does not hold the bytes this piece followed: the next capture starts over.
                args = json.loads(entry['args'])
                self.outbox.reset_transcript(args['provider'], args['native_id'])
            refused.append(dict(outcome, idempotency_key=key, op=entry['op']))
        return {'delivered': delivered, 'refused': refused, 'pending': len(self.outbox.pending()),
                'stopped': stopped, 'reached': reached}

    def _entry_result(self, entry):
        if entry['state'] == 'delivered':
            return json.loads(entry['outcome'])
        if entry['state'] == 'refused':
            outcome = json.loads(entry['outcome'])
            raise KelmoError(outcome['code'], outcome['message'])
        return {'queued': True, 'accepted': False, 'op': entry['op'], 'idempotency_key': entry['idempotency_key'],
                'pending_reason': entry['last_error'],
                'next_step': 'Stored in the local outbox; it is delivered under the same key on the next '
                             'successful contact and is not accepted until then.'}

    def local_status(self):
        return {'profile': self.profile.as_dict(), 'outbox': self.outbox.summary()}

    def probe(self):
        """Verify a fresh channel's identity without flushing any queued write."""
        began = time.monotonic()
        with self._lock:
            self._drop()
            self._connect()
            hello = self.server
            return {'verified': True, 'authority_id': hello['authority_id'],
                    'client_id': hello.get('client_id'), 'machine_tag': hello.get('machine_tag'),
                    'fenced': hello.get('fenced'), 'writer_id': hello.get('writer_id'),
                    'writer_epoch': hello.get('writer_epoch'),
                    'archive': hello.get('archive'),
                    'elapsed_ms': round((time.monotonic() - began) * 1000, 1),
                    'writes_flushed': False}

    # Context delivery

    def _cache_path(self, key):
        return self.profile.state / 'cache' / ('context-' + key + '.json')

    def context(self, cwd=None, *, unreachable=None, binding=None):
        """Current context from the server, or the labelled stale cache when there is no server verdict.

        `unreachable` is a failure the caller already met in this deadline; the cache is read without
        another connection attempt. `binding` is the caller's local_binding(cwd) when it resolved it first."""
        if binding is None:
            binding = local_binding(cwd, deadline=self._deadline)
        args = {name: binding[name] for name in ('repository', 'work', 'work_project', 'branch', 'binding_status')}
        if binding.get('repositories'):
            args['repositories'] = binding['repositories']
        key_parts = {name: binding[name] for name in ('repository', 'work', 'work_project')}
        if binding.get('repositories'):
            key_parts['repositories'] = binding['repositories']
        key = digest(canonical(key_parts))
        if unreachable is not None:
            return self._stale(key, binding, unreachable)
        try:
            result = self.call('context', args)
        except KelmoError as error:
            if error.code not in CACHE_FALLBACK and not isinstance(error, Unbound):
                raise
            return self._stale(key, binding, error)
        result = dict(result, local_binding=binding)
        if result.get('configured') is False:
            return result
        header, blocks = verify_context(result)
        hub_set = result.get('hub_set')
        if hub_set is not None:
            generation = result['generation']
            if (hub_set.get('generation') != generation['publication_id']
                    or hub_set.get('manifest_sha256') != generation['manifest_sha256']
                    or generation['authority_id'] != self.profile.authority_id):
                raise KelmoError('E_CACHE', 'HOT set and context name different generations.')
        record = {'schema': CACHE_SCHEMA, 'key': key, 'authority_id': self.profile.authority_id,
                  'generation': result['generation'], 'header': header, 'pages': blocks,
                  'context_sha256': result['context_sha256'],
                  'mandatory': result['mandatory'], 'project': result['project'],
                  'binding_status': result.get('binding_status'), 'work_status': result.get('work_status'),
                  'fetched_at': utc_now()}
        if hub_set is not None:
            record.update(hub_generation=hub_set['generation'], hub_revision=hub_set['enrollment_revision'],
                          hub_work=result.get('hub_work', ''), hub_work_path=result.get('hub_work_path'),
                          pending=result.get('pending', []))
            record['hub_extra_sha256'] = digest(canonical([record['hub_generation'], record['hub_revision'],
                                                           record['hub_work'], record['hub_work_path'], record['pending']]))
        try:
            _sealed(record, key, self.profile.authority_id)
        except (KeyError, TypeError, ValueError) as failure:
            raise KelmoError('E_CACHE', 'Delivered context does not carry its mandatory pages first; nothing '
                                       'was cached.') from failure
        if hub_set is not None:
            from .hub_delivery import install_set
            directory = install_set(self.profile.state / 'hot', hub_set)
            record['hub_set_name'] = directory.name
            result['hub_directory'] = str(directory)
        _atomic_write(self._cache_path(key), canonical(record).encode('utf-8'))
        return result

    def _stale(self, key, binding, error):
        path = self._cache_path(key)
        reason = ('the connected shared kelmo server does not advertise the required capability'
                  if error.code == 'E_CAPABILITY' else 'the shared kelmo server could not be reached')
        if not path.is_file():
            raise KelmoError(error.code, reason[0].upper() + reason[1:] + '; this repository has no '
                            'cached generation, so nothing was delivered.')
        try:
            record = json.loads(path.read_bytes())
            generation = _sealed(record, key, self.profile.authority_id)
        except (OSError, KeyError, TypeError, ValueError, UnicodeError) as failure:
            raise KelmoError('E_CACHE', 'The cached generation failed its hash or binding check; '
                                       'nothing was delivered.') from failure
        mandatory = record['mandatory']
        header = (f'Kelmo HOT STALE CACHE ({error.code}): {reason}. '
                  f'This is generation {generation.publication_id} (manifest '
                  f'{generation.manifest_sha256[:12]}) cached at {record["fetched_at"]}; it is not current '
                  'state and not proof of it. Writes stay queued in the local outbox and are not accepted '
                  'yet. Mandatory person pages: ' + (', '.join(mandatory) or 'none') + '.')
        context = '\n\n'.join([header, *('## ' + page['path'] + '\n\n' + page['text'] for page in record['pages'])])
        result = {'configured': True, 'project': record['project'], 'context': context,
                'pages': [{'path': page['path'], 'sha256': page['sha256'],
                           'bytes': len(page['text'].encode('utf-8'))} for page in record['pages']],
                'bytes': len(context.encode('utf-8')), 'generation': record['generation'],
                'freshness': 'stale', 'complete': False, 'mandatory': mandatory,
                'cache': {'fetched_at': record['fetched_at'], 'reason': error.code},
                'binding_status': record.get('binding_status'), 'work_status': record.get('work_status'),
                'local_binding': binding}
        if 'hub_generation' not in record and os.path.lexists(self.profile.state / 'hot/current'):
            raise KelmoError('E_CACHE', 'Legacy cached context predates the known HOT enrollment; reconnect first.')
        if 'hub_generation' in record:
            from .hub_delivery import read_set, _set_metadata
            if record.get('hub_extra_sha256') != digest(canonical([record['hub_generation'], record['hub_revision'],
                                                                    record['hub_work'], record['hub_work_path'],
                                                                    record['pending']])):
                raise KelmoError('E_CACHE', 'Cached work and pending state failed its hash check.')
            _, current = _set_metadata(self.profile.state / 'hot', 'current')
            if current['enrollment_revision'] != record['hub_revision']:
                raise KelmoError('E_CACHE', 'Cached context predates the latest enrollment revision.')
            snapshot = read_set(self.profile.state / 'hot', record.get('hub_set_name', 'current'))
            if (snapshot['generation'] != record['hub_generation']
                    or snapshot['enrollment_revision'] != record['hub_revision']
                    or snapshot['manifest_sha256'] != generation.manifest_sha256):
                raise KelmoError('E_CACHE', 'Cached binding and HOT set name different generations.')
            result.update(hub_set=snapshot, hub_work=record['hub_work'], hub_work_path=record['hub_work_path'],
                          pending=record['pending'],
                          hub_directory=str(self.profile.state / 'hot' / record.get('hub_set_name', 'current')),
                          hub_work_pointer=str(path) + ' (hub_work field; STALE CACHE)')
        return result

    # Transcript capture

    def capture_session(self, provider, action, payload, *, deliver=True, restart=True, internal_job=None):
        """Queue the transcript named by a native lifecycle event, in pieces from the last delivered
        position, and deliver the queue unless the caller hands delivery to a detached flusher.

        A transcript has no size ceiling here. Returns the server result of the final piece when it was
        delivered, or the queued state (`queued`, `pieces`, `generation`). `internal_job` is the hook
        process's own guard when the caller is a daemon core, whose environment cannot show it.
        """
        if internal_job is None:
            internal_job = os.environ.get('KELMO_INTERNAL_JOB') == '1'
        if internal_job:
            # The recursion guard: a transcript of Kelmo's own executor job is never captured. The skip
            # is recorded (outbox `skips`, `client status`), so a guard in the wrong environment shows.
            native = payload.get('session_id') if isinstance(payload, dict) else None
            try:
                self.outbox.record_skip(provider if isinstance(provider, str) else None, str(action)[:40],
                                        native[:120] if isinstance(native, str) else None, 'internal-job')
            except sqlite3.Error:
                pass  # the guard still holds when the record cannot be written
            return {'captured': False, 'skipped': 'internal-job'}
        from .session_transcripts import enrolled_transcript, parse
        if provider not in ('claude', 'codex') or action not in EVENTS:
            raise KelmoError('E_INPUT', 'Unsupported provider or session lifecycle event.')
        if not isinstance(payload, dict) or not isinstance(payload.get('transcript_path'), str):
            raise KelmoError('E_TRANSCRIPT', 'Native event lacks a transcript path; session capture was not accepted.')
        source = enrolled_transcript(payload['transcript_path'], self.profile.sessions.get(provider, ()),
                                     'Native transcript is outside enrolled provider locations.')
        native = payload.get('session_id') or payload.get('thread_id')
        if not isinstance(native, str) or not NATIVE_ID.fullmatch(native):
            raise KelmoError('E_INPUT', 'Native session identity is missing or invalid.')
        raw, info = _stable_read(source)
        if parse(provider, raw)['headless']:
            # A headless run (a helper, a scripted call) reports to the session that started it; its own
            # transcript never leaves this machine. Recorded like the guard above.
            try:
                self.outbox.record_skip(provider, str(action)[:40], native[:120], 'headless')
            except sqlite3.Error:
                pass
            return {'captured': False, 'skipped': 'headless'}
        identity, event = f'{info.st_dev}:{info.st_ino}', EVENTS[action]

        def pieces(generation, offset, restarts):
            """Every piece after `offset`, each naming the bytes it follows; an event without new bytes
            still sends one empty final piece so the server records it."""
            running, queued, position = hashlib.sha256(raw[:offset]), [], offset
            while True:
                end = min(position + PIECE_BYTES, len(raw))
                piece, base = raw[position:end], running.hexdigest()
                running.update(piece)
                args = {'provider': provider, 'native_id': native, 'event': event, 'file_identity': identity,
                        'client_generation': generation, 'offset': position, 'base_sha256': base,
                        'content': base64.b64encode(piece).decode('ascii'), 'size': len(piece),
                        'sha256': running.hexdigest(), 'final': end == len(raw)}
                # Refused before anything is queued: an entry that can never fit one frame would block the
                # outbox. A piece is bounded by PIECE_BYTES, so only a shrunken frame limit refuses here.
                try:
                    encode_frame({'protocol': PROTOCOL, 'request_id': 'x' * 32, 'op': 'session_append',
                                  'args': args, 'idempotency_key': 'x' * 64})
                except KelmoError as error:
                    raise KelmoError('E_INPUT', 'A transcript piece does not fit one bounded frame; nothing '
                                               'was queued.') from error
                # Equal bytes in a later generation, or after a restart, are a new capture; a retry of the
                # same capture keeps the same keys and fingerprints.
                key = digest(canonical(['session', provider, native, identity, generation, restarts, event,
                                        position, args['sha256']]))
                queued.append((key, 'session_append', args))
                position = end
                if args['final']:
                    return queued

        with self._lock:
            generation, keys = self.outbox.queue_transcript(provider, native, identity, raw, pieces)
            if not deliver:
                return {'queued': True, 'accepted': False, 'pieces': len(keys), 'generation': generation}
            self.flush()
            try:
                return self._entry_result(self.outbox.get(keys[-1]))
            except KelmoError as error:
                if error.code != 'E_CAPTURE_BASE' or not restart:
                    raise
        # The server does not hold the bytes this capture followed; the delivery reset the sent position,
        # so the whole transcript goes once more from its start.
        return self.capture_session(provider, action, payload, deliver=True, restart=False)


class ClientService:
    """The Service-shaped surface the CLI and MCP adapters call in shared mode."""

    def __init__(self, client):
        self.client = client
        self.root = client.profile.root

    def pause(self, seconds):
        # The edge core's client lends its owner lock out while it waits; a session client has nothing to lend.
        getattr(self.client, 'pause', time.sleep)(seconds)

    def capture(self, content, filename, role='unknown', intent='archive', idempotency_key=None, supersedes=None):
        """Service.capture's shape: the accepted operation's fields, or the explicit queued result while
        the server has not accepted it (queued true, accepted false, the pending reason)."""
        captured, _ = self._capture(content, filename, role, intent, idempotency_key, supersedes)
        return captured['operation'] if 'operation' in captured else captured

    def create(self, content, filename='memory.md', *, project, selected=(), rule_change=False,
               budget_tokens=None, idempotency_key=None, wait=False, wait_timeout=300, new_work=None,
               pending=None):
        if wait and (not math.isfinite(wait_timeout) or wait_timeout < 0):
            raise KelmoError('E_INPUT', 'Wait timeout must be a finite, nonnegative number of seconds.')
        args = {'content': content, 'filename': filename, 'project': project, 'selected': list(selected),
                'rule_change': rule_change}
        if budget_tokens is not None:
            args['budget_tokens'] = budget_tokens
        if new_work is not None:
            args['new_work'] = new_work
        if pending is not None:
            args['pending'] = pending
        key = idempotency_key or digest(canonical(['create', args]))
        result = self.client.submit('create', args, key)
        return self.wait(result, wait_timeout) if wait and 'operation' in result else result

    def add(self, content, filename, role='unknown', intent='archive', idempotency_key=None, supersedes=None,
            *, wait=False, wait_timeout=300):
        """Capture durably, then optionally observe the server worker without queuing another write."""
        if wait and (not math.isfinite(wait_timeout) or wait_timeout < 0):
            raise KelmoError('E_INPUT', 'Wait timeout must be a finite, nonnegative number of seconds.')
        captured, _ = self._capture(content, filename, role, intent, idempotency_key, supersedes)
        if not wait or 'operation' not in captured:
            return captured
        return self.wait(captured, wait_timeout)

    def wait(self, captured, timeout=300):
        """Observe accepted work; a deadline or channel failure cannot undo its capture."""
        result = captured
        limit = time.monotonic() + timeout
        with self.client.deadline(max(timeout, 0.001)):
            while True:
                operation = result['operation']
                # Failed on a held lock is a turn in the queue: the worker requeues it, so keep watching.
                queued = operation['state'] == 'failed' and operation.get('error_code') == 'E_BUSY'
                if (operation['terminal'] and not queued) or operation['state'] == 'captured':
                    if operation['state'] == 'captured':
                        result = dict(result, processing_scheduled=False,
                                      next_step='Captured evidence is not scheduled; an authorized writer '
                                                'must submit it for archival or checked promotion.')
                    return result
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    if queued:
                        return dict(result, accepted=True, processing_deferred=True, wait_timed_out=True,
                                    waiting_turn=True,
                                    next_step=f'Queued behind another writer; the worker retries it by itself. '
                                              f'kelmo trace {operation["id"]} shows when it lands.')
                    return dict(result, accepted=True, processing_deferred=True, wait_timed_out=True,
                                next_step=f'Work remains on the server. Inspect kelmo trace {operation["id"]}.')
                try:
                    trace = self.trace(operation['id'])
                except Unbound as error:
                    return dict(result, accepted=True, processing_deferred=True,
                                wait_error={'code': error.code, 'message': str(error)},
                                next_step=f'Capture was accepted. Inspect kelmo trace {operation["id"]} '
                                          'after reconnecting; do not recapture it.')
                result = {key: value for key, value in trace.items() if key != 'events'}
                following = result['operation']
                if ((not following['terminal'] or (following['state'] == 'failed'
                                                   and following.get('error_code') == 'E_BUSY'))
                        and following['state'] != 'captured'):
                    self.client.pause(max(0, min(0.25, limit - time.monotonic())))

    def _capture(self, content, filename, role, intent, idempotency_key, supersedes):
        if intent not in ('archive', 'promote'):
            raise KelmoError('E_INPUT', 'Unknown operation intent.')
        if not isinstance(content, str):
            raise KelmoError('E_INPUT', 'Source must contain text within the one MiB intake limit.')
        # The same identity a local capture without a key derives from its input.
        key = idempotency_key or digest(canonical(['capture', digest(content), filename, role, intent, supersedes]))
        return self.client.submit('capture', {'content': content, 'filename': filename, 'role': role,
                                              'promote': intent == 'promote', 'supersedes': supersedes}, key), key

    def search(self, query, limit=10, *, with_metadata=False, project=None, tags=None, since=None, until=None,
               client=None):
        # MCP display metadata does not select the shared channel's authenticated client identity.
        result = self.client.call('search', {'query': query, 'limit': limit, 'project': project,
                                             'tags': list(tags) if tags else None, 'since': since, 'until': until})
        return result if with_metadata else result['items']

    def reconcile(self, text, target):
        return self.client.call('reconcile', {'text': text, 'target': target})

    def register(self, targets, *, kind='topic', summary='', apply=False, idempotency_key=None):
        args = {'targets': list(targets), 'kind': kind, 'summary': summary}
        if not apply:
            return self.client.call('register_preview', args)
        return self.client.submit('register', args, idempotency_key or digest(canonical(['register', args])))

    def page_index(self, projects=None):
        return self.client.call('pages', {'projects': projects})

    def page_facts(self, target):
        return self.client.call('page', {'target': target})

    def editor_context(self, project, budget_tokens, selected=()):
        return self.client.call('editor_context', {'project': project, 'budget_tokens': budget_tokens,
                                                  'selected': list(selected)})

    def status(self):
        return dict(self.client.call('status'), outbox=self.client.outbox.summary())

    def trace(self, operation_id):
        return self.client.call('trace', {'operation_id': operation_id})

    def show(self, memory_id):
        return self.client.call('show', {'memory_id': memory_id})

    def doctor(self):
        """The server vault's health checks, with this client's undelivered writes beside them. A write
        that has waited over OUTBOX_STALL_SECONDS holds every later one back (the outbox delivers in order),
        so it is named: a server lock that never frees would otherwise stall this client in silence."""
        report = self.client.call('doctor')
        outbox = self.client.outbox.summary()
        warning = outbox_stall_warning(outbox)
        if warning:
            report = dict(report, warnings=[*report.get('warnings', []), warning])
        return dict(report, outbox=outbox)

    def list(self, limit=20, offset=0):
        return self.client.call('list', {'limit': limit, 'offset': offset})

    def forget(self, memory_id, *, reason=None):
        return self.client.submit('forget', {'memory_id': memory_id, 'reason': reason}, uuid.uuid4().hex)

    def retry(self, operation_id):
        """`queue retry`: one repair request, queued and delivered like any write, scoped to this client."""
        return self.client.submit('retry', {'operation_id': operation_id}, uuid.uuid4().hex)

    def apply_action(self, update, idempotency_key):
        from .contracts import ActionUpdate
        try:
            checked = ActionUpdate.from_dict(update).to_dict()
        except (TypeError, ValueError) as error:
            raise KelmoError('E_INPUT', 'Provide a complete ActionUpdate with an exact evidence reference.') from error
        return self.client.submit('state_apply', {'update': checked}, idempotency_key)

    def graph_query(self, **kwargs):
        return self.client.call('graph_query', {'params': kwargs})

    def graph_stats(self):
        return self.client.call('graph_stats')

    def graph_rebuild(self):
        return self.client.call('graph_rebuild')

    def graph_read(self, entity_id):
        return self.client.call('graph_read', {'entity_id': entity_id})

    def compat(self, tool, arguments):
        from .mcp import COMPAT_WRITES
        if not isinstance(arguments, dict):
            raise KelmoError('E_INPUT', 'Compatibility arguments must be an object.')
        if tool in COMPAT_WRITES:
            # A write keeps its observation time and identity across every retry.
            arguments = dict(arguments)
            arguments.setdefault('observed_at', utc_now())
            return self.client.submit('compat', {'tool': tool, 'arguments': arguments}, uuid.uuid4().hex)
        return self.client.call('compat', {'tool': tool, 'arguments': arguments})

    def sessions(self):
        return self.client.call('sessions_inspect')


def _stable_read(source):
    """The transcript's bytes and the stat they belong to. A provider appends to its transcript while a
    lifecycle hook runs, so a read that sees the file change is repeated a few times; when it keeps
    growing, the bytes up to the first observed size are the capture (append-only JSONL, the next event
    carries the rest), and only a file that shrank during the read is refused (the check reads the opened descriptor)."""
    for attempt in range(CAPTURE_READ_ATTEMPTS):
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise KelmoError('E_INPUT', 'Transcript is not a regular file.')
            with os.fdopen(descriptor, 'rb') as stream:
                raw = stream.read(info.st_size + 1)
                after = os.fstat(stream.fileno())
        except FileNotFoundError as error:
            raise KelmoError('E_TRANSCRIPT_ABSENT', 'The provider has not written this transcript; '
                                                   'nothing to capture.') from error
        except OSError as error:
            raise KelmoError('E_INPUT', 'Transcript could not be opened for capture.') from error
        if len(raw) == info.st_size and (after.st_size, after.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
            return raw, info
        if attempt + 1 < CAPTURE_READ_ATTEMPTS:
            time.sleep(CAPTURE_READ_PAUSE_SECONDS)
    if len(raw) >= info.st_size and after.st_size >= info.st_size and after.st_ino == info.st_ino:
        return raw[:info.st_size], info
    raise KelmoError('E_CAPTURE_RETRY', 'Transcript changed while reading; retry capture.')


def hook(client, action, payload, provider=None, *, flusher=None, acknowledge=None, internal_job=None):
    """Native lifecycle events against the shared authority. `flusher` is an argv started detached, or a
    callable that wakes a long-lived deliverer; either way the hook itself never waits for delivery."""
    if not isinstance(payload, dict):
        raise KelmoError('E_INPUT', 'Hook payload must be a JSON object.')
    if action in ('start', 'prompt'):
        try:
            # One deadline under the hook timeout covers the binding, the flush, at most one connection and
            # the context.
            with client.deadline(START_DEADLINE_SECONDS):
                # The binding first: it names the exact cache entry, so a flush that uses up its share of
                # the deadline still leaves the labelled cache for this repository and work.
                binding = local_binding(payload.get('cwd'), deadline=client._deadline)
                # Queued writes go next, so a reconnect does not deliver context older than our own writes;
                # the flush stops a reserve early, whatever holds it (the flush lock, a slow write or a
                # reply that never comes).
                unreachable = None
                reserve = min(CONTEXT_RESERVE_SECONDS, START_DEADLINE_SECONDS / 3)
                share = client._deadline - time.monotonic() - reserve
                try:
                    if share <= 0:
                        flushed = {'reached': None, 'stopped': 'E_TRANSPORT'}
                    else:
                        with client.deadline(share):
                            flushed = client.flush()
                except sqlite3.Error:
                    # The local outbox cannot be read, so our queued writes were not delivered: the
                    # labelled cache, never a current context that may predate them.
                    flushed = {'reached': False, 'stopped': 'E_STATE'}
                if flushed['stopped'] is not None and (flushed['reached'] is not True or client._channel is None):
                    # No open channel after the flush stopped (unreachable, lock held elsewhere, deadline,
                    # a dropped channel): the cache, not a second attempt.
                    unreachable = Unbound(flushed['stopped'], 'The shared kelmo server could not be reached '
                                                              'in time, or the local outbox could not be read.')
                delivered = client.context(payload.get('cwd'), unreachable=unreachable, binding=binding)
                if 'hub_set' in delivered:
                    from .hub_delivery import prepare
                    stale = delivered.get('freshness') != 'current'
                    work_status = delivered.get('work_status')
                    binding_status = binding.get('binding_status')
                    notice = ''
                    if work_status and work_status != 'bound':
                        notice = 'Bound work was not delivered: ' + work_status + '.'
                    elif binding_status in ('pending', 'invalid-binding', 'stale-branch'):
                        notice = 'Work binding is ' + binding_status + '; no work page was delivered.'
                    context = prepare(client.profile, payload, delivered['hub_set'], delivered.get('project'),
                                      delivered.get('hub_work', ''), delivered.get('pending', []),
                                      provider=provider or 'claude',
                                      current=delivered.get('hub_directory', client.profile.state / 'hot' / 'current'),
                                      stale=stale, acknowledge=acknowledge, work_path=delivered.get('hub_work_path'),
                                      notice=notice, aliases=delivered.get('project_aliases'),
                                      work_pointer=delivered.get('hub_work_pointer'), action=action)
                else:
                    from .hub_delivery import legacy_delivery
                    context = legacy_delivery(client.profile.state, delivered, delivered.get('pending', []),
                                              provider=provider or 'claude', action=action)
        except (KelmoError, OSError, ValueError, sqlite3.Error) as error:
            code = (error.code if isinstance(error, KelmoError)
                    else 'E_STATE' if isinstance(error, sqlite3.Error) else 'E_HOT')
            state = 'incomplete' if code == 'E_BUDGET' else 'unavailable'
            context = f'Kelmo HOT {state} ({code}). No selected pages were delivered.'
            if code in ('E_BUDGET', 'E_GENERATION', 'E_BINDING', 'E_CACHE', 'E_AUTHORITY', 'E_TRANSPORT'):
                context += ' ' + str(error)
        return {'hookSpecificOutput': {'hookEventName': 'SessionStart' if action == 'start' else 'UserPromptSubmit',
                                      'additionalContext': context}}
    if action == 'capture':
        message = payload.get('last_assistant_message')
        if isinstance(message, str) and message.strip():
            if len(message.encode()) > 1048576:
                raise KelmoError('E_INPUT', 'Hook evidence exceeds the intake limit; submit a scoped note.')
            ClientService(client).capture(message, 'session-evidence.md', role='collector',
                                          idempotency_key=digest(str(payload.get('session_id', '')) + message))
        return {}
    if action in EVENTS:
        # The hook runs under a provider's short hook timeout: it queues the transcript locally and
        # returns; a detached flusher carries the queue to the server, outliving the hook process.
        try:
            client.capture_session(provider, action, payload, deliver=flusher is None,
                                   internal_job=internal_job)
        except KelmoError as error:
            if error.code not in CAPTURE_SKIPS:
                raise
            # Nothing to deliver from this event; the next event captures the file as it then is.
            # Recorded, so a repeated skip shows in `client status` instead of as a hook failure.
            native = payload.get('session_id') or payload.get('thread_id')
            try:
                client.outbox.record_skip(provider, str(action)[:40],
                                          native[:120] if isinstance(native, str) else None,
                                          CAPTURE_SKIPS[error.code])
            except sqlite3.Error:
                pass
            return {}
        if callable(flusher):
            flusher()
        elif flusher is not None:
            detach(flusher)
        return {}
    raise KelmoError('E_INPUT', 'Unsupported hook action.')


def detach(argv):
    """Start `argv` in its own session with no inherited streams; its outcome is the outbox's, not ours."""
    try:
        subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        pass  # the entries stay pending; the next hook or SessionStart flush delivers them


def dispatch(root, args, read_source, client=None):
    """CLI commands a shared client serves; everything else runs on the server and is refused here. Inside the
    edge core `client` is the core's own SharedClient, the one owner of the outbox."""
    import sys
    from contextlib import nullcontext
    command = args.command
    with (nullcontext(client) if client is not None else SharedClient(ClientProfile.load(root))) as client:
        service = ClientService(client)
        if command == 'client':
            if args.action == 'flush':
                # The detached flusher, outside any hook deadline, also gives back the space settled
                # transcript pieces left behind.
                return dict(client.flush(), compacted=client.outbox.compact())
            if args.action == 'resend':
                items = client.outbox.resend(tuple(args.code or ('E_INPUT',)), apply=args.apply)
                result = {'applied': args.apply, 'resent': items}
                return dict(result, flushed=client.flush()) if args.apply and items else result
            result = client.local_status()
            if getattr(args, 'probe', False):
                result['connection'] = client.probe()
            return result
        if command == 'hook':
            raw = sys.stdin.read(1048577)
            if len(raw.encode()) > 1048576:
                raise KelmoError('E_INPUT', 'Hook input exceeds size limit.')
            launcher = getattr(args, 'launcher', None)
            flusher = [str(launcher), 'client', 'flush', '--vault', str(root)] if launcher else None
            return hook(client, args.action, json.loads(raw or '{}'), args.provider, flusher=flusher,
                        acknowledge=getattr(args, '_kelmo_acknowledge', None))
        if command == 'hot':
            if args.check_all:
                raise KelmoError('E_CAPABILITY', 'Selection checks run on the server.')
            return client.context(str(args.cwd) if args.cwd else None)
        if command in ('add', 'promote'):
            content, filename = read_source(args.file)
            return service.add(content, filename, args.role, 'promote' if command == 'promote' else 'archive',
                               args.idempotency_key, supersedes=args.supersedes, wait=args.wait,
                               wait_timeout=args.wait_timeout)
        if command == 'create':
            content, filename = read_source(args.file) if args.file else (args.text, 'memory.md')
            from .creation import requested_work
            return service.create(content, filename, project=args.project, selected=args.target,
                                  rule_change=args.rule_change, budget_tokens=args.budget_tokens,
                                  idempotency_key=args.idempotency_key, wait=args.wait,
                                  wait_timeout=args.wait_timeout, new_work=requested_work(args, content),
                                  pending=args.pending)
        if command == 'status':
            return service.status()
        if command == 'reconcile':
            return service.reconcile(args.text, args.target)
        if command == 'register':
            return service.register(args.targets, kind=args.kind, summary=args.summary, apply=args.apply,
                                    idempotency_key=args.idempotency_key)
        if command == 'pages':
            return service.page_index(args.projects)
        if command == 'page':
            return service.page_facts(args.target)
        if command == 'context':
            return service.editor_context(args.project, args.budget_tokens, args.selected)
        if command == 'action':
            raw, _ = read_source(args.file)
            return service.apply_action(json.loads(raw), args.idempotency_key)
        if command == 'search':
            return service.search(args.query, args.limit, with_metadata=True, project=args.project,
                                  tags=args.tags, since=args.since, until=args.until)
        if command == 'list':
            return service.list(args.limit, args.offset)
        if command == 'show':
            return service.show(args.memory_id)
        if command in ('trace', 'logs'):
            trace = service.trace(args.operation_id if command == 'trace' else args.operation)
            return trace if command == 'trace' else {'events': trace['events']}
        if command == 'forget':
            return service.forget(args.memory_id)
        if command == 'queue' and args.action == 'retry':
            # The repair a failed operation names: the server requeues it for this client's origin only.
            return service.retry(args.operation_id)
        if command == 'compat':
            raw = sys.stdin.read(8 * 1048576 + 1)
            if len(raw.encode()) > 8 * 1048576:
                raise KelmoError('E_INPUT', 'Compatibility input exceeds size limit.')
            return {'result': service.compat(args.tool, json.loads(raw or '{}'))}
        if command == 'graph':
            if args.graph_action == 'stats':
                return service.graph_stats()
            if args.graph_action == 'rebuild':
                return service.graph_rebuild()
            if args.graph_action == 'read':
                return service.graph_read(args.entity_id)
            return service.graph_query(action=args.query_action, entity_id=args.entity_id, query=args.query,
                                       target_id=args.target_id, radius=args.radius,
                                       relation_filter=args.relation_filter, limit=args.limit)
        if command == 'sessions' and args.action in ('list', 'show'):
            result = service.sessions()
            if args.action == 'show':
                job = next((item for item in result['jobs'] if item['id'] == args.job_id), None)
                if job is None:
                    raise KelmoError('E_NOT_FOUND', 'Session job not found.')
                return job
            return result
    raise KelmoError('E_CAPABILITY', '`kelmo ' + command + '` runs on the server; the shared client does not '
                                    'provide it.')
