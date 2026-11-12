"""The archive core: the only opener of `archive.db`, started and supervised by kelmod.

Two halves. `main` is what kelmod starts: it builds the confinement (sandbox-exec on macOS, bubblewrap on Linux)
exactly as the state core's archive process is built today, starts the confined worker with kelmod's channel
(descriptor 3) passed through, hands it the startup frame on stdin, and waits for it; its exit code is the
worker's. `serve` runs inside the confinement: it checks the boundary as `archive_worker_main` does, then answers
`archive.*` calls on descriptor 3 with one writer thread and reader threads, each with its own adapter and SQLite
connection. kelmod owns its lifetime, so there is no idle exit.

    python -m kelmo.core.archive_core --vault V [--server-profile P] [--in-process]
"""
import argparse
import json
import os
import shutil
import signal
import sys
import threading
from pathlib import Path

from ..errors import KelmoError

READERS = 4


def _worker_factory(start, *, confined, provider_factory=None):
    from ..archive_adapter import LocalArchiveAdapter
    from ..server import ArchiveWorker, OriginBindings, chunk_index
    bindings = OriginBindings.from_description(start['enrollment'])
    local = threading.local()
    opened = []
    lock = threading.Lock()

    def worker():
        current = getattr(local, 'worker', None)
        if current is None:
            current = ArchiveWorker(None, bindings, start['scratch'], confined=confined, fenced=start['fenced'])
            current.adapter = LocalArchiveAdapter(start['archive'], start['cache'], bindings,
                                                  legacy_receipt_verifier=current.legacy_verified,
                                                  chunk_index=chunk_index(start['embedding'], provider_factory))
            local.worker = current
            with lock:
                opened.append(current)
        return current
    return worker, opened


def serve(start, *, confined, descriptor=3):
    """Answers archive.* on `descriptor` until kelmod closes it or stops the core."""
    from . import operations
    from .corelink import CoreLink
    from ..server import _readable, encode_frame
    if confined:
        readable = [path for path in start['canaries'] if _readable(path)]
        if readable:
            raise KelmoError('E_ISOLATION', 'The archive core can read coordinator data; native confinement is '
                                            'absent, so it refuses to serve.')
    provider_factory = None
    worker = None
    opened = []

    def handler(name):
        def handle(_context, body):
            # The same encoding the archive pipe applies: contract objects become their JSON form.
            reply = json.loads(encode_frame(worker().handle({'method': name, 'args': body if body is not None else {}})))
            if 'error' in reply:
                raise KelmoError(reply['error']['code'], reply['error']['message'])
            return reply['result']
        return handle

    handlers = {}
    for op in operations.table():
        if op.name.startswith('archive.'):
            handlers[op.name] = (handler(op.name[len('archive.'):]), op.lanes[0])
    link = CoreLink.from_descriptor(descriptor, confined=confined, handlers=handlers,
                                    lanes={'write': 1, 'read': READERS})
    try:
        if start.get('embed_core'):
            # Counts go to the embedding core, so this process parses no tokenizer. The counter is in place before
            # hello: kelmod sends no archive call until then, so no window is built with a local tokenizer first.
            import threading
            from kelmo.store.memory.embed import install_remote_counter
            from .embed_client import EmbedChannel, EmbedCounter
            link.hold_hello = True
            serving = threading.Thread(target=link.serve, name='corelink', daemon=True)
            serving.start()
            settings = start.get('embedding') or {}
            broker_command = settings.get('provider') == 'broker' and bool(settings.get('command'))
            counter = EmbedCounter.connect(link, pending_vectors=broker_command)
            install_remote_counter(counter)
            if broker_command:
                from ..contracts import EmbeddingSpace
                from ..embeddings import BrokeredProvider
                from ..store.errors import ArchiveEmbeddingError

                def provider_factory(settings):
                    # An embedding core that did not answer at start is asked again by every call: the channel
                    # reports it as unavailable until it serves.
                    if counter is not None and not counter.provider:
                        raise ArchiveEmbeddingError('The embedding core provider is unavailable',
                                                    reason='embedding_unavailable')
                    return BrokeredProvider(None, None, EmbeddingSpace.from_dict(settings['space']),
                                            tokenizer=settings['tokenizer'], channel=EmbedChannel(link))
                if counter is not None and not counter.provider:
                    print('kelmo archive core: embedding core has no provider; vectors pending',
                          file=sys.stderr, flush=True)
            worker, opened = _worker_factory(start, confined=confined, provider_factory=provider_factory)
            worker()  # check the archive schema before hello
            link.say_hello()
            serving.join()
        else:
            worker, opened = _worker_factory(start, confined=confined)
            worker()
            link.serve()
    finally:
        for item in opened:
            try:
                item.adapter.close()
            except Exception:  # noqa: BLE001 - closing at exit; the process ends either way
                pass
    return 0


class CoreChannel:
    """The transport `ArchiveProcess` uses inside the state core: each archive frame becomes an `archive.*` call
    through kelmod, on the read or write lane the operation table names. Several threads call it at once;
    replies are matched by id in CoreLink."""

    SECONDS = 300.0
    # The route to the archive core failed, not the archive: the core died with the frame in flight, kelmod
    # was stopping or full, or no answer came. As with the pipe to an archive process, the frame's effect is
    # unknown, so the refusal is E_ARCHIVE_PROCESS and the caller's ledger decides whether it runs again
    # (daemon.TRANSIENT_FAILURES); the daemon's own code would leave the operation failed for good.
    ROUTE_FAILURES = frozenset({'E_CORE_RESTARTED', 'E_TRANSPORT', 'E_UNAVAILABLE', 'E_OVERLOADED', 'E_DEADLINE'})

    def __init__(self, link):
        from . import operations
        self.link = link
        self.lanes = {op.name: op.lanes[0] for op in operations.table() if op.name.startswith('archive.')}

    def call(self, frame):
        from .context import Context
        op = 'archive.' + frame['method']
        context = Context.local(os.urandom(8).hex(), self.SECONDS)
        try:
            result = self.link.call(op, frame['args'], context, lane=self.lanes.get(op)).result(self.SECONDS + 5)
        except KelmoError as error:
            if error.code in self.ROUTE_FAILURES:
                return {'error': {'code': 'E_ARCHIVE_PROCESS',
                                  'message': f'The archive core did not complete {op} ({error.code}): {error}'}}
            refusal = {'code': error.code, 'message': str(error)}
            if isinstance(getattr(error, 'retryable', None), bool):
                refusal['retryable'] = error.retryable
            return {'error': refusal}
        except TimeoutError:
            return {'error': {'code': 'E_ARCHIVE_PROCESS',
                              'message': f'The archive core did not answer {op} in time.'}}
        return {'result': result}

    def close(self):
        pass


def proxy(link, bindings, *, fenced=False):
    """The state core's archive API over the archive core."""
    from ..server import ArchiveProcess
    return ArchiveProcess(CoreChannel(link), bindings, {'mode': 'core', 'confined': True}, fenced=fenced)


def _bindings(root, profile_path):
    from ..config import Config
    from ..server import ServerProfile
    config = Config.load(root)
    if profile_path is None:
        return config, config.local_bindings(), False
    profile = ServerProfile.load(profile_path)
    if profile.bindings.authority_id != config.vault_id:
        raise KelmoError('E_ORIGIN', 'The server profile belongs to another vault authority.')
    return config, profile.bindings, profile.fenced


def _startup(config, bindings, fenced, scratch, wire, embed_core=False):
    from ..server import ARCHIVE_PROTOCOL, coordinator_canaries
    return {'protocol': ARCHIVE_PROTOCOL, 'enrollment': bindings.describe(),
            'archive': str(config.archive.resolve()), 'cache': str(config.cache), 'scratch': str(scratch),
            'canaries': coordinator_canaries(config), 'fenced': fenced, 'embedding': wire, 'idle_seconds': None,
            'embed_core': embed_core}


def main(argv=None):
    """Started by kelmod with its channel on descriptor 3."""
    import tempfile
    parser = argparse.ArgumentParser(prog='kelmo-archive-core')
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--server-profile', type=Path)
    parser.add_argument('--in-process', action='store_true', help='No confinement (tests and unsupported hosts '
                                                                  'that a profile explicitly allows)')
    parser.add_argument('--embed-core', action='store_true', help='Token counts come from the embedding core')
    args = parser.parse_args(argv)
    config, bindings, fenced = _bindings(args.vault, args.server_profile)
    from ..server import embedding_launch
    scratch = Path(tempfile.mkdtemp(prefix='kelmo-archive-')).resolve()
    try:
        wire, model_roots, model_files, fds, channel_env = embedding_launch(config.embedding, poison=False)
        start = _startup(config, bindings, fenced, scratch, wire, embed_core=args.embed_core)
        if args.in_process:
            return serve(start, confined=False)
        return _confined(config, start, scratch, model_roots, model_files, fds, channel_env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _confined(config, start, scratch, model_roots, model_files, fds, channel_env):
    from ..runtime import IsolationError, confined_spawn, environment
    from ..server import archive_paths, encode_frame, import_roots
    python = Path(sys.executable).resolve()
    paths = archive_paths(config, python, import_roots(), scratch, model_roots=model_roots,
                          model_files=model_files)
    argv = [str(python), '-I', '-S', '-X', 'utf8=1', str(Path(__file__).resolve().parent.parent / 'archive_worker.py'),
            '--confined', '--core']
    for root in import_roots():
        argv += ['--import-root', str(root)]
    try:
        child = confined_spawn(paths, argv, env=dict(environment(scratch, [python.parent]), **channel_env),
                               cwd=scratch, pass_fds=(3, *fds))
    except IsolationError as error:
        raise KelmoError('E_ISOLATION', str(error)) from error
    os.close(3)  # the child holds the channel now; kelmod sees it close when the child ends

    def forward(number, _frame):
        try:
            child.send_signal(number)
        except OSError:
            pass
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, forward)
    try:
        child.stdin.write(encode_frame(start))
        child.stdin.close()
    except OSError:
        pass
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.kill()
        return child.wait()
    finally:
        if child.stdout is not None:
            child.stdout.close()


if __name__ == '__main__':
    sys.exit(main())
