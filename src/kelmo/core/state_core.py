"""The state core: the one process that opens the journal, started by kelmod.

    python -m kelmo.core.state_core --vault V [--server-profile P] [--readers 4]

One writer `Service` owns the read-write journal connection; the writer thread runs every write under the
baton. Reader threads each hold a reader `Service` on a read-only connection. A call never keeps a Service of
its own: the origin comes from the frame header and is bound to the call (`Service.requested_by`).

In a vault with a server profile every call that names an enrolled client runs through that client's
`ClientSession` rules (scoped keys, keyless writes refused, origin-filtered status, the fence and the
enrollment read per request). A vault without one has a single origin, `local`, and today's local rules.
"""
import argparse
import asyncio
import contextvars
import inspect
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from ..errors import KelmoError
from .baton import Baton

# Service methods by access. A read runs on a reader's read-only journal; phase 3 proves each entry.
READS = frozenset({
    'page_index', 'page_facts', 'editor_context', 'graph_query', 'graph_stats', 'graph_read',
    'source_path', 'source_packet', 'queued', 'queue', 'orphan_sources', 'trace', 'failed_operations', 'status',
    'archive_record', 'show', 'doctor', 'list', 'search', 'compat_metadata',
})
WRITES = frozenset({
    'capture', 'create', 'register', 'reconcile', 'capture_source', 'propose_archived_note', 'run', 'retry', 'forget',
    'graph_rebuild', 'apply_action',
})
SHARED_ATTRIBUTES = frozenset({'root', 'config', 'bindings', 'context_bindings'})

DEFAULT_READERS = 4
HANDLER_LANE = 32

_FACADE = contextvars.ContextVar('kelmo_state_facade', default=None)


class Storage:
    """The writer Service on its thread, and the reader Services on theirs."""

    def __init__(self, make_writer, make_reader, readers=DEFAULT_READERS):
        self.writer = make_writer()
        self.baton = Baton(in_transaction=self._in_transaction)
        self.writer.journal.guard = self.baton.check
        self._make_reader = make_reader
        self._local = threading.local()
        self._readers = []
        self._readers_lock = threading.Lock()
        self._writer_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='state-writer')
        self._writer_thread = self._writer_pool.submit(threading.get_ident).result()
        self._reader_pool = ThreadPoolExecutor(max_workers=readers, thread_name_prefix='state-reader')

    def _in_transaction(self):
        import sqlite3
        try:
            return self.writer.journal._conn.in_transaction
        except sqlite3.ProgrammingError:  # closed: nothing can be open
            return False

    def _reader(self):
        service = getattr(self._local, 'service', None)
        if service is None:
            service = self._local.service = self._make_reader()
            with self._readers_lock:
                self._readers.append(service)
        return service

    def write(self, function):
        """Runs `function(writer)` on the writer thread under the baton, with the caller's context."""
        def task():
            with self.baton.hold():
                return function(self.writer)
        if threading.get_ident() == self._writer_thread:
            return task()
        return self._writer_pool.submit(contextvars.copy_context().run, task).result()

    def read(self, function):
        """Runs `function(reader)` on a reader thread, with the caller's context."""
        if threading.get_ident() == self._writer_thread:
            return function(self.writer)
        return self._reader_pool.submit(contextvars.copy_context().run, lambda: function(self._reader())).result()

    def counts(self):
        with self._readers_lock:
            return {'writer_services': 1, 'reader_services': len(self._readers)}

    def close(self):
        self._reader_pool.shutdown(wait=True)
        with self._readers_lock:
            readers, self._readers = self._readers, []
        # A reader's connection belongs to its thread, which has ended; closing from here only drops Python refs.
        del readers
        self.write(lambda writer: writer.close())
        self._writer_pool.shutdown(wait=True)


class Routed:
    """The Service surface a local call sees: each method runs on the writer or on a reader."""

    def __init__(self, storage):
        self._storage = storage

    def __getattr__(self, name):
        if name in READS:
            return lambda *args, **kwargs: self._storage.read(lambda service: getattr(service, name)(*args, **kwargs))
        if name in WRITES:
            if not hasattr(self._storage.writer, name):
                raise AttributeError(name)
            return lambda *args, **kwargs: self._storage.write(lambda service: getattr(service, name)(*args, **kwargs))
        if name in SHARED_ATTRIBUTES:
            return getattr(self._storage.writer, name)
        raise AttributeError(name)

    def compat(self, tool, arguments):
        from ..mcp import COMPAT_WRITES, compat
        run = self._storage.write if tool in COMPAT_WRITES else self._storage.read
        return run(lambda service: compat(service, tool, arguments))


class _NoOutbox:
    def summary(self):
        return {'pending': 0, 'delivered': 0, 'refused': [], 'skipped': {}}


class SessionClient:
    """`SharedClient`'s surface over the state core itself, for one origin: a tool's call becomes a
    kelmo-client/1 request run under that client's session rules, without a channel or an outbox."""

    def __init__(self, core, origin, root):
        self._core = core
        self._origin = origin
        self.profile = SimpleNamespace(root=root)
        self.outbox = _NoOutbox()

    def deadline(self, _seconds):
        from contextlib import nullcontext
        return nullcontext()

    def _request(self, op, args, key):
        from ..server import PROTOCOL
        frame = {'protocol': PROTOCOL, 'request_id': uuid.uuid4().hex, 'op': op, 'args': args}
        if key is not None:
            frame['idempotency_key'] = key
        reply = self._core.client_request(self._origin, frame)
        if reply.get('ok'):
            return reply.get('result')
        error = KelmoError(reply['error']['code'], reply['error']['message'])
        error.retryable = reply['error']['retryable']
        raise error

    def call(self, op, args=None):
        return self._request(op, args or {}, None)

    def submit(self, op, args, key):
        return self._request(op, args, key)


def _session_service(core, origin, root):
    from ..client import ClientService

    class OriginService(ClientService):
        """A client's Service surface inside the core: no outbox is added, the edge owns that."""

        def status(self):
            return self.client.call('status')

        def doctor(self):
            return self.client.call('doctor')

    return OriginService(SessionClient(core, origin, root))


class StateCore:
    def __init__(self, root, *, profile_path=None, profile=None, readers=DEFAULT_READERS, worker_config=None,
                 schedule=False, archive_link=None):
        """`profile_path` names the server profile, reread on every call; `profile` is an already loaded one
        (tests), read once."""
        from ..config import Config
        from ..server import KeysInFlight, ServerProfile, open_archive
        from ..service import Service
        self.root = Path(root).expanduser().resolve()
        self.profile_path = Path(profile_path) if profile_path is not None else None
        if profile is None and self.profile_path is not None:
            profile = ServerProfile.load(self.profile_path)
        self.profile = profile
        self.keys_in_flight = KeysInFlight()
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        if self.profile is None and archive_link is None:
            def make_writer():
                return Service(self.root, shared_journal=True)

            def make_reader():
                return Service(self.root, read_only=True)
        elif self.profile is None:
            # A local vault whose archive is the supervised archive core.
            from .archive_core import proxy
            config = Config.load(self.root)
            bindings = config.local_bindings()
            self.archive = proxy(archive_link, bindings)

            def make_writer():
                service = Service(self.root, archive_api=self.archive, bindings=bindings, shared_journal=True)
                self.archive.legacy_receipt_verifier = service._legacy_receipt_allowed
                return service

            def make_reader():
                return Service(self.root, archive_api=self.archive, bindings=bindings, read_only=True)
        else:
            profile = self.profile
            config = Config.load(self.root)
            if config.vault_id != profile.bindings.authority_id:
                raise KelmoError('E_ORIGIN', 'The server profile belongs to another vault authority.')
            if archive_link is not None:
                from .archive_core import proxy
                self.archive = proxy(archive_link, profile.bindings, fenced=profile.fenced)
            else:
                self.archive = open_archive(config, profile.bindings, profile.archive_process,
                                            fenced=profile.fenced)

            def make_writer():
                service = Service(self.root, archive_api=self.archive, bindings=profile.bindings, background=True,
                                  context_bindings=profile.contexts, shared_journal=True)
                self.archive.legacy_receipt_verifier = service._legacy_receipt_allowed
                return service

            def make_reader():
                return Service(self.root, archive_api=self.archive, bindings=profile.bindings, background=True,
                               context_bindings=profile.contexts, read_only=True)
        self.storage = Storage(make_writer, make_reader, readers)
        from .baton import install
        from .jobs import Jobs
        install(self.storage.baton)
        self.jobs = Jobs(self, worker_config=worker_config)
        from .. import daemon as executors
        groups = self.root / '.state' / 'executors'
        groups.mkdir(mode=0o700, exist_ok=True)
        executors.executor_groups = groups
        if schedule:
            self.jobs.start()
        self._tools = None

    # --- origins and sessions --------------------------------------------------------------------------

    def _enrolled(self, origin):
        """The client session for an origin of a vault with a server profile; None for a local origin."""
        from ..server import ClientSession, ServerProfile
        if self.profile is None:
            if origin is not None and origin.client_id != 'local':
                raise KelmoError('E_ORIGIN', 'This vault has no server profile; only local calls are served.')
            return None
        if origin is None:
            return None
        try:
            current = ServerProfile.load(self.profile_path) if self.profile_path is not None else self.profile
            current.bindings.origin(origin.client_id)
        except ValueError as error:
            with self._sessions_lock:
                self._sessions.pop(origin.client_id, None)
            raise KelmoError('E_ORIGIN', 'The calling client is not enrolled.') from error
        with self._sessions_lock:
            session = self._sessions.get(origin.client_id)
            if session is None:
                session = ClientSession(self.root, self.profile, origin.client_id, service=self.storage.writer,
                                        profile_path=self.profile_path, keys_in_flight=self.keys_in_flight)
                self._sessions[origin.client_id] = session
            return session

    def client_request(self, origin, frame):
        """One kelmo-client/1 request, as a client sends it, under the calling client's session rules. A
        write runs on the writer thread, a read on a reader; the reply is the kelmo-client/1 reply."""
        from ..mcp import COMPAT_WRITES
        from ..server import OPERATIONS, PROTOCOL, validate_request
        from .operations import JOURNAL_WRITING_READS
        session = self._enrolled(origin)
        if session is None:
            raise KelmoError('E_ORIGIN', 'Client requests need an enrolled client of the server profile.')
        try:
            op, args, _ = validate_request(frame)
        except KelmoError:
            return session.handle(frame)  # answers the refusal in the protocol's shape
        mutating = OPERATIONS[op][0] or op in JOURNAL_WRITING_READS
        if op == 'compat':
            mutating = args.get('tool') in COMPAT_WRITES

        def run(service):
            with session.using(service):
                return session.handle(frame)
        reply = self.storage.write(run) if mutating else self.storage.read(run)
        return dict(reply, protocol=PROTOCOL)

    # --- handlers --------------------------------------------------------------------------------------

    def handle_client_request(self, context, body):
        if not isinstance(body, dict) or not isinstance(body.get('request'), dict):
            raise KelmoError('E_PROTOCOL', 'state.client_request carries {request}.')
        return self.client_request(context.origin, body['request'])

    def operation_handler(self, op):
        """`state.<op>`: one client operation with its arguments as the body and the key in the header."""
        from ..server import PROTOCOL

        def handle(context, body):
            frame = {'protocol': PROTOCOL, 'request_id': context.request_id[:64], 'op': op,
                     'args': body if body is not None else {}}
            if context.key is not None:
                frame['idempotency_key'] = context.key
            reply = self.client_request(context.origin, frame)
            if reply.get('ok'):
                return reply.get('result')
            error = KelmoError(reply['error']['code'], reply['error']['message'])
            error.retryable = reply['error']['retryable']
            raise error
        return handle

    def tools(self):
        if self._tools is None:
            from .. import mcp
            server = mcp.server(self.root, service=_FACADE.get)
            self._tools = {tool.name: tool.fn for tool in asyncio.run(server.list_tools())}
        return self._tools

    def facade(self, origin):
        session = self._enrolled(origin)
        if session is None:
            return Routed(self.storage)
        return _session_service(self, origin, self.root)

    def handle_tool(self, context, body):
        if not isinstance(body, dict) or not isinstance(body.get('tool'), str):
            raise KelmoError('E_PROTOCOL', 'state.tool carries {tool, arguments}.')
        arguments = body.get('arguments') or {}
        if not isinstance(arguments, dict):
            raise KelmoError('E_PROTOCOL', 'Tool arguments must be an object.')
        function = self.tools().get(body['tool'])
        if function is None:
            raise KelmoError('E_INPUT', 'Unknown tool.')
        try:
            inspect.signature(function).bind(**arguments)
        except TypeError as error:
            raise KelmoError('E_INPUT', f'Tool arguments do not match {body["tool"]}: {error}') from error
        token = _FACADE.set(self.facade(context.origin))
        try:
            return json.loads(json.dumps(function(**arguments), default=str))
        finally:
            _FACADE.reset(token)

    def handle_cli(self, context, body):
        """One installed-CLI call, run on the writer Service. Only a local caller may send it."""
        from ..cli import run_borrowed
        if self._enrolled(context.origin) is not None:
            raise KelmoError('E_FORBIDDEN', 'CLI calls are served to local callers only.')
        argv = body.get('argv') if isinstance(body, dict) else None
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise KelmoError('E_PROTOCOL', 'state.cli carries {argv: [strings]}.')
        sources = body.get('sources') or {}
        stdin = body.get('stdin')
        if (not isinstance(sources, dict) or (stdin is not None and not isinstance(stdin, str))
                or not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair)
                           for pair in sources.values())):
            raise KelmoError('E_PROTOCOL', 'state.cli sources are {name: [content, filename]}, stdin a string.')
        # On this handler thread, holding the baton: a `--wait` that reaches a model call lets go of the baton
        # for it, and the writer thread keeps serving everyone else meanwhile.
        with self.storage.baton.hold():
            result = run_borrowed(self.storage.writer, argv, {name: tuple(pair) for name, pair in sources.items()},
                                  stdin)
        return json.loads(json.dumps(result, default=str))

    def handle_job_run(self, context, body):
        """Runs one job now (`kelmo worker run`, `kelmo dream apply`); E_BUSY while one of its kind runs."""
        if self._enrolled(context.origin) is not None:
            raise KelmoError('E_FORBIDDEN', 'Jobs are started by local callers only.')
        if not isinstance(body, dict) or not isinstance(body.get('job'), str):
            raise KelmoError('E_PROTOCOL', 'state.job_run carries {job, options}.')
        result = self.jobs.run(body['job'], body.get('options') or {})
        return json.loads(json.dumps(result, default=str))

    def handlers(self):
        from ..server import OPERATIONS
        table = {'state.client_request': (self.handle_client_request, 'handler'),
                 'state.tool': (self.handle_tool, 'handler'),
                 'state.cli': (self.handle_cli, 'handler'),
                 'state.job_run': (self.handle_job_run, 'admin')}
        from .operations import JOURNAL_WRITING_READS
        for op, (mutating, _spec) in OPERATIONS.items():
            mutating = True if op in JOURNAL_WRITING_READS else mutating
            lane = 'handler' if mutating is None else ('write' if mutating else 'read')
            table['state.' + op] = (self.operation_handler(op), lane)
        return table

    def status(self):
        return dict(self.storage.counts(), keys_in_flight=len(self.keys_in_flight),
                    sessions=len(self._sessions), profile=self.profile is not None, jobs=self.jobs.status())

    def trim(self):
        import gc
        gc.collect()

    def close(self):
        from .baton import install
        self.jobs.stop()
        with self._sessions_lock:
            self._sessions.clear()
        self.storage.close()
        install(None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kelmo-state-core')
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--server-profile', type=Path)
    parser.add_argument('--readers', type=int, default=DEFAULT_READERS)
    parser.add_argument('--worker-config', type=Path, help='Executor configuration of the worker job')
    parser.add_argument('--schedule', action='store_true', help='Run the worker and Dreaming on their schedule')
    parser.add_argument('--archive-core', action='store_true', help='The archive is the supervised archive core')
    args = parser.parse_args(argv)
    from .corelink import CoreLink
    # The channel exists before the core: the archive proxy calls through it while the core starts.
    link = CoreLink.from_descriptor(handlers={}, lanes={'handler': HANDLER_LANE, 'read': args.readers, 'write': 8,
                                                        'admin': 4})
    serving = threading.Thread(target=link.serve, name='corelink', daemon=True) if args.archive_core else None
    if serving is not None:
        link.hold_hello = True
        serving.start()
    core = StateCore(args.vault, profile_path=args.server_profile, readers=args.readers,
                     worker_config=args.worker_config, schedule=args.schedule,
                     archive_link=link if args.archive_core else None)
    link.handlers, link.trim, link.status = core.handlers(), core.trim, core.status
    link.operations = sorted(link.handlers)
    try:
        if serving is None:
            link.serve()
        else:
            link.say_hello()
            serving.join()
    finally:
        core.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
