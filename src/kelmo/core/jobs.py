"""The job module of the state core: the background work the worker daemon and the dream timer did as their
own processes.

Each job runs on a thread of the state core, over the writer Service, holding the baton for one step at a time
(a job step yields to interactive writes) and never across a model call (`daemon.execute` lets it go). At most
one job of a kind runs at a time. Claims a job takes are tracked by token and released when its thread ends for
any reason, so a dead job never holds an operation until its lease runs out. Executor children run in their own
process group, recorded under `<state>/executors/` so kelmod can kill what is left after a crash of the core.

Kinds:
  worker     one pass of the queue: operations, one resolution request, session jobs (daemon.tick)
  dream      waiting notes first, then the findings pass (dreaming `run`); `apply` runs only the notes
  backfill   vectors for chunks that have none
  reembed    vectors again for the configured model
  graph      rebuild the wiki graph index
"""
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..errors import KelmoError

KINDS = ('worker', 'dream', 'backfill', 'reembed', 'graph')
DEFAULT_POLL_SECONDS = 5.0
DREAM_HOUR = 3
DREAM_WAITING_NOTES = 20


class JobBusy(KelmoError):
    def __init__(self, kind):
        super().__init__('E_BUSY', f'A {kind} job is already running.')
        self.retryable = True


class Jobs:
    def __init__(self, core, *, worker_config=None, poll_seconds=DEFAULT_POLL_SECONDS, clock=time.time):
        self.core = core
        self.storage = core.storage
        self.worker_config = Path(worker_config) if worker_config is not None else None
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._running = {kind: threading.Lock() for kind in KINDS}
        self._active = {}
        self._active_lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler = None
        self._state_path = Path(core.root) / '.state' / 'jobs.json'
        self.history = []

    # --- running one job --------------------------------------------------------------------------------

    @contextmanager
    def step(self):
        """One job step: the baton, yielding to interactive writes."""
        with self.storage.baton.hold(interactive=False):
            yield

    def run(self, kind, options=None):
        """Runs one job of `kind` on the calling thread and returns its result; E_BUSY while one runs."""
        from ..journal import tracked_claims
        if kind not in KINDS:
            raise KelmoError('E_INPUT', f'Unknown job kind: {kind}.')
        options = dict(options or {})
        lock = self._running[kind]
        if not lock.acquire(blocking=False):
            raise JobBusy(kind)
        began = time.monotonic()
        with self._active_lock:
            self._active[kind] = {'started_at': self.clock(), 'thread': threading.get_ident()}
        claims = set()
        outcome = {'kind': kind}
        try:
            with tracked_claims(claims):
                result = getattr(self, '_' + kind)(options)
            outcome.update(state='finished', result=result)
            return result
        except Exception as error:
            outcome.update(state='failed', error_code=getattr(error, 'code', type(error).__name__))
            raise
        finally:
            if claims:
                self._release(claims)
            with self._active_lock:
                self._active.pop(kind, None)
            outcome['seconds'] = round(time.monotonic() - began, 3)
            with self._active_lock:
                self.history = (self.history + [outcome])[-20:]
            lock.release()

    def _release(self, tokens):
        """Drops every claim this job still holds, whatever ended it."""
        with self.step():
            connection = self.storage.writer.journal.conn
            with connection:
                connection.executemany('DELETE FROM claims WHERE token=?', [(token,) for token in tokens])

    # --- the kinds ---------------------------------------------------------------------------------------

    def _executor(self):
        from ..daemon import validate_config
        if self.worker_config is None:
            return validate_config({})
        try:
            raw = json.loads(self.worker_config.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError) as error:
            raise KelmoError('E_CONFIG', 'Executor configuration is not readable valid JSON.') from error
        return validate_config(raw)

    def _worker(self, options):
        from ..daemon import tick
        config = self._executor()
        return tick(self.storage.writer, config['argv'], max_attempts=config['max_attempts'],
                    timeout=config['timeout_seconds'], should_stop=self._stop.is_set, step=self.step)

    def _dream(self, options):
        from ..dreaming_cli import run_dream
        with self.step():
            return run_dream(self.storage.writer, options.get('action', 'run'),
                             contexts=getattr(self.core.profile, 'contexts', None))

    def _admin(self):
        from ..archive_admin import ArchiveAdmin, operator_grant
        writer = self.storage.writer
        return lambda verb: ArchiveAdmin(writer, operator_grant(writer.config, {verb}))

    def _backfill(self, options):
        with self.step():
            return self._admin()('backfill').backfill(batch=options.get('batch', 64))

    def _reembed(self, options):
        with self.step():
            return self._admin()('reembed').reembed(batch=options.get('batch', 64),
                                                    force=bool(options.get('force')))

    def _graph(self, options):
        with self.step():
            return self.storage.writer.graph_rebuild()

    # --- schedule ----------------------------------------------------------------------------------------

    def _today(self):
        return time.strftime('%Y-%m-%d', time.localtime(self.clock()))

    def _last_dream_day(self):
        try:
            return json.loads(self._state_path.read_text(encoding='utf-8')).get('dream_day')
        except (OSError, ValueError, AttributeError):
            return None

    def _record_dream_day(self):
        temporary = self._state_path.with_name(self._state_path.name + '.tmp')
        temporary.write_text(json.dumps({'dream_day': self._today()}), encoding='utf-8')
        temporary.replace(self._state_path)

    def _dream_due(self):
        """Dreaming runs once a day from DREAM_HOUR (the day is kept in .state/jobs.json, so a restart does
        not run it twice), and at once when DREAM_WAITING_NOTES notes wait."""
        if time.localtime(self.clock()).tm_hour >= DREAM_HOUR and self._last_dream_day() != self._today():
            return 'run'
        return 'apply' if self._waiting_notes() >= DREAM_WAITING_NOTES else None

    def _waiting_notes(self):
        def count(reader):
            return reader.journal.conn.execute(
                "SELECT COUNT(DISTINCT operation_id) FROM events WHERE stage='awaiting_dreaming' "
                'AND operation_id IN (SELECT id FROM operations WHERE terminal=0)').fetchone()[0]
        try:
            return self.storage.read(count)
        except Exception:  # noqa: BLE001 - a schedule probe never stops the scheduler
            return 0

    def _tick(self):
        if self.worker_config is not None:
            self._background('worker', {})
        action = self._dream_due()
        if action is not None and self._background('dream', {'action': action}) and action == 'run':
            self._record_dream_day()

    def _background(self, kind, options):
        """Starts a job thread unless one of its kind runs; True when started."""
        if self._running[kind].locked():
            return False
        thread = threading.Thread(target=self._quietly, args=(kind, options), name=f'job-{kind}', daemon=True)
        thread.start()
        return True

    def _quietly(self, kind, options):
        """A scheduled job: its failure is recorded and written to the core's stderr (kelmod's log), and the
        scheduler keeps going. A failed daily Dreaming run is not retried until the next day (its day is taken
        when it starts, so a broken configuration cannot restart it every few seconds); `kelmo dream run`
        starts one by hand."""
        try:
            self.run(kind, options)
        except Exception as error:  # noqa: BLE001 - recorded; the scheduler keeps going
            print(f'kelmo: scheduled {kind} job failed: {getattr(error, "code", type(error).__name__)}: {error}',
                  file=sys.stderr, flush=True)

    def start(self):
        self._scheduler = threading.Thread(target=self._loop, name='job-scheduler', daemon=True)
        self._scheduler.start()

    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            self._tick()

    def stop(self, timeout=None):
        """Stops scheduling and waits for running jobs; a running model call is not killed (drain waits)."""
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout)
        limit = None if timeout is None else time.monotonic() + timeout
        for lock in self._running.values():
            remaining = None if limit is None else max(0.0, limit - time.monotonic())
            if lock.acquire(timeout=-1 if remaining is None else remaining):
                lock.release()

    def status(self):
        with self._active_lock:
            active = {kind: {'seconds': round(self.clock() - item['started_at'], 1)}
                      for kind, item in self._active.items()}
            recent = self.history[-5:]
        return {'active': active, 'recent': recent, 'worker_configured': self.worker_config is not None}
