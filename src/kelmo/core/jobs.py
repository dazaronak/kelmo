"""The job module of the state core: the background work the worker daemon and the dream timer did as their
own processes.

Each job runs on a thread of the state core, over the writer Service, holding the baton for one step at a time
(a job step yields to interactive writes) and never across a model call (`daemon.execute` lets it go). At most
one job of a kind runs at a time. Claims a job takes are tracked by token and released when its thread ends for
any reason, so a dead job never holds an operation until its lease runs out. Executor children run in their own
process group, recorded under `<state>/executors/state/` so kelmod can kill what is left after a crash of the core.

Kinds:
  worker     one pass of the queue: operations, one resolution request, session jobs (daemon.tick)
  dream      waiting notes first, then the findings pass (dreaming `run`); `apply` runs only the notes
  backfill   vectors for chunks that have none
  reembed    vectors again for the configured model
  graph      rebuild the wiki graph index
"""
import json
import os
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
BACKFILL_FIRST_SECONDS = 300
BACKFILL_SECONDS = 600
BACKFILL_BATCH = 16
# One provider batch per archive call: the archive has one write lane, and a longer step would hold every
# capture behind it. Between steps the job rests in proportion to the step, as the offline backfill does.
BACKFILL_LIMIT = BACKFILL_BATCH
BACKFILL_REST_FACTOR = 2.0
BACKFILL_REST_MAX_SECONDS = 30.0
APPLY_MIN_INTERVAL_SECONDS = 900
APPLY_NO_PROGRESS_SECONDS = 7200


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
        from .instance import state_dir
        self._state_path = state_dir(core.root) / 'jobs.json'
        self.history = []
        self._state_lock = threading.Lock()
        self.last = {}
        # The last failure of each kind is kept in .state/jobs.json: a watcher that polls every few minutes
        # still sees it after the daemon restarted, and tells one failure from the next by `ended_at`.
        kept = self._read_state().get('last_failure')
        self.last_failure = dict(kept) if isinstance(kept, dict) else {}
        self._sequence = 0
        self._backfill_due = self.clock() + BACKFILL_FIRST_SECONDS
        self._backfill_scheduled = False

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
        if kind == 'dream':
            outcome['action'] = options.get('action', 'run')  # the daily run and an apply share the kind
        try:
            with tracked_claims(claims):
                result = getattr(self, '_' + kind)(options)
            outcome.update(state='finished', result=result)
            return result
        except Exception as error:
            outcome.update(state='failed', error_code=getattr(error, 'code', type(error).__name__))
            raise
        finally:
            try:
                if claims:
                    self._release(claims)
            finally:
                with self._active_lock:
                    self._active.pop(kind, None)
                    outcome['seconds'] = round(time.monotonic() - began, 3)
                    self._sequence += 1
                    outcome.update(seq=self._sequence, ended_at=self.clock())
                    self.history = (self.history + [outcome])[-20:]
                    # The worker pass runs every few seconds and fills `history`; a poller that comes every
                    # few minutes reads the last outcome and the last failure of each kind here instead.
                    summary = {key: value for key, value in outcome.items() if key != 'result'}
                    result_state = outcome.get('result')
                    if isinstance(result_state, dict) and isinstance(result_state.get('state'), str):
                        summary['result_state'] = result_state['state']
                    self.last[kind] = summary
                    if outcome.get('state') == 'failed':
                        self.last_failure[kind] = summary
                if outcome.get('state') == 'failed':
                    try:
                        # Read under the file's lock, so two jobs failing at once cannot write an older copy last.
                        self._write_state(last_failure=self._failures)
                    except OSError:  # a full disk must not turn one failed job into a broken scheduler
                        pass
                lock.release()

    def _failures(self):
        with self._active_lock:
            return dict(self.last_failure)

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
                    timeout=config['timeout_seconds'], should_stop=self._stop.is_set, step=self.step,
                    open_service=self._session_service())

    def _session_service(self):
        """What archives a session note under a server profile: this core's own writer, acting for the enrolled
        client that captured the session, as it does for that client's own writes. No second Service and no
        second archive are opened, and no note is filed under a local identity. A local vault needs none.
        Enrollment is read from the profile file at every note, as for a client's own request: a client removed
        since the daemon started gets no more notes filed for it."""
        profile = getattr(self.core, 'profile', None)
        if profile is None:
            return None
        writer = self.storage.writer
        path = getattr(self.core, 'profile_path', None)

        @contextmanager
        def acting(client_id):
            from ..server import ServerProfile
            try:
                current = ServerProfile.load(path) if path is not None else profile
                current.bindings.origin(client_id)
                origin = profile.bindings.origin(client_id)
            except ValueError as error:
                raise KelmoError('E_ORIGIN', 'The client that captured this session is not enrolled.') from error
            with writer.requested_by(origin):
                yield writer

        return acting

    def _dream(self, options):
        from ..dreaming_cli import run_dream
        scheduled = options.get('scheduled')
        action = options.get('action', 'run')
        if scheduled and self._fenced():
            return {'state': 'fenced'}

        def started():
            if scheduled and action == 'run':
                self._record_dream_day()
            elif scheduled and action == 'apply':
                self._record_apply_start()

        with self.step():
            try:
                return run_dream(self.storage.writer, action, contexts=getattr(self.core.profile, 'contexts', None),
                                 clock=self.clock, on_start=started)
            finally:
                if scheduled and action == 'apply' and getattr(self, '_apply_started', False):
                    self._record_apply_end()

    def _admin(self):
        from ..archive_admin import ArchiveAdmin, operator_grant
        writer = self.storage.writer
        return lambda verb: ArchiveAdmin(writer, operator_grant(writer.config, {verb}))

    def _backfill(self, options):
        admin = self._admin()('backfill')
        batch = options.get('batch', BACKFILL_BATCH)
        result = {'written': 0, 'stale': 0, 'hidden': 0, 'present': 0, 'failed': [],
                  'remaining': None, 'stopped': None}
        cursor = None
        while not self._stop.is_set():
            if options.get('scheduled') and self._fenced():
                result['stopped'] = 'fenced'
                break
            step = admin.backfill(batch=batch, limit=BACKFILL_LIMIT, after=cursor)
            for key in ('written', 'stale', 'hidden', 'present'):
                result[key] += step.get(key, 0)
            result['failed'].extend(step.get('failed', []))
            result.update({key: value for key, value in step.items()
                           if key not in ('written', 'stale', 'hidden', 'present', 'failed')})
            if step.get('stopped') or step.get('skipped') or not step.get('cursor'):
                break
            cursor = step['cursor']
            rest = min(BACKFILL_REST_MAX_SECONDS, (step.get('seconds') or 0) * BACKFILL_REST_FACTOR)
            if rest > 0 and self._stop.wait(rest):
                break
        return result

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
        return self._read_state().get('dream_day')

    def _read_state(self):
        try:
            state = json.loads(self._state_path.read_text(encoding='utf-8'))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _write_state(self, **updates):
        # Job threads and the scheduler all keep their marks in this one file.
        with self._state_lock:
            state = self._read_state()
            state.update({key: value() if callable(value) else value for key, value in updates.items()})
            temporary = self._state_path.with_name(self._state_path.name + '.tmp')
            with temporary.open('w', encoding='utf-8') as file:
                file.write(json.dumps(state))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self._state_path)

    def _record_dream_day(self):
        self._write_state(dream_day=self._today())

    def _fenced(self):
        if getattr(self.core, 'profile_path', None) is not None:
            from ..server import ServerProfile
            try:
                return ServerProfile.load(self.core.profile_path).fenced
            except (KelmoError, OSError):
                # A profile that cannot be read holds scheduled work, as a fenced one does.
                return True
        return bool(getattr(getattr(self.core, 'profile', None), 'fenced', False))

    def _queue_notes(self):
        """Ids of the notes waiting for Dreaming, by state: `archived` waits for its first pass, `parked` was
        refused by one and is retried by every pass. One indexed read: the scheduler asks every few seconds."""
        rows = self.storage.read(lambda reader: reader.journal.conn.execute(
            "SELECT id, state FROM operations WHERE terminal=0 AND intent='create' "
            "AND state IN ('archived', 'parked')").fetchall())
        notes = {'archived': set(), 'parked': set()}
        for row in rows:
            notes[row[1]].add(str(row[0]))
        return notes

    def _dream_hold(self):
        from ..dreaming_cli import status
        state = status(self.core.root)
        if state.get('paused'):
            return 'paused', None
        reset = state.get('next_attempt')
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > self.clock():
            return 'deferred', reset
        return None, None

    def _record_apply_start(self):
        notes = self._queue_notes()
        self._write_state(apply={'started': self.clock(), 'ids': sorted(notes['archived']), 'finished': None})
        self._apply_started = True

    def _record_apply_end(self):
        state = self._read_state().get('apply', {})
        notes = self._queue_notes()
        state['no_progress'] = bool(state.get('ids')) and set(state['ids']) <= notes['archived'] | notes['parked']
        state['finished'] = self.clock()
        self._write_state(apply=state)
        self._apply_started = False

    def _apply_hold(self):
        last = self._read_state().get('apply') or {}
        started, ended = last.get('started'), last.get('finished')
        if not isinstance(started, (int, float)):
            return None, None
        if ended is None:
            notes = self._queue_notes()
            no_progress = bool(last.get('ids')) and set(last['ids']) <= notes['archived'] | notes['parked']
        else:
            no_progress = last.get('no_progress', False)
        if no_progress and self.clock() < started + APPLY_NO_PROGRESS_SECONDS:
            return 'no_progress', started + APPLY_NO_PROGRESS_SECONDS
        # An apply the daemon did not see end (it was stopped or died meanwhile) counts from its start, so a
        # restart never brings the next apply at once.
        since = ended if isinstance(ended, (int, float)) else started
        if self.clock() < since + APPLY_MIN_INTERVAL_SECONDS:
            return 'min_interval', since + APPLY_MIN_INTERVAL_SECONDS
        return None, None

    def _dream_due(self):
        """Dreaming runs once a day from DREAM_HOUR (the day is kept in .state/jobs.json, so a restart does
        not run it twice), and at once when DREAM_WAITING_NOTES notes wait."""
        if self._dream_hold()[0] is not None:
            return None
        if time.localtime(self.clock()).tm_hour >= DREAM_HOUR and self._last_dream_day() != self._today():
            return 'run'
        return 'apply' if self._waiting_notes() >= DREAM_WAITING_NOTES and self._apply_hold()[0] is None else None

    def _waiting_notes(self):
        try:
            return len(self._queue_notes()['archived'])
        except Exception:  # noqa: BLE001 - a schedule probe never stops the scheduler
            return 0

    def _tick(self):
        if self._fenced():
            return
        if self.worker_config is not None:
            self._background('worker', {'scheduled': True})
        try:
            action = self._dream_due()
        except Exception as error:  # noqa: BLE001 - a failed probe must not stop other scheduled jobs
            print(f'kelmo: Dreaming schedule probe failed: {type(error).__name__}', file=sys.stderr, flush=True)
            action = None
        if action is not None:
            self._background('dream', {'action': action, 'scheduled': True})
        if (self.storage.writer.config.embedding is not None and not self._backfill_scheduled
                and self.clock() >= self._backfill_due):
            self._backfill_scheduled = True
            if not self._background('backfill', {'scheduled': True}):
                self._backfill_scheduled = False

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
        attempted = False
        try:
            if options.get('scheduled') and self._fenced():
                return
            attempted = True
            self.run(kind, options)
        except Exception as error:  # noqa: BLE001 - recorded; the scheduler keeps going
            print(f'kelmo: scheduled {kind} job failed: {getattr(error, "code", type(error).__name__)}: {error}',
                  file=sys.stderr, flush=True)
        finally:
            if kind == 'backfill' and options.get('scheduled'):
                if attempted:
                    self._backfill_due = self.clock() + BACKFILL_SECONDS
                self._backfill_scheduled = False

    def start(self):
        self._backfill_due = self.clock() + BACKFILL_FIRST_SECONDS
        self._scheduler = threading.Thread(target=self._loop, name='job-scheduler', daemon=True)
        self._scheduler.start()

    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self._tick()
            except Exception as error:  # noqa: BLE001 - one failed tick must not end scheduling for good
                print(f'kelmo: scheduler tick failed: {type(error).__name__}: {error}', file=sys.stderr, flush=True)

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
            backfill = next((item for item in reversed(self.history) if item['kind'] == 'backfill'), None)
            last, last_failure = dict(self.last), dict(self.last_failure)
        fenced = self._fenced()
        try:
            reason, allowed = ('fenced', None) if fenced else self._dream_hold()
            daily_due = (time.localtime(self.clock()).tm_hour >= DREAM_HOUR
                         and self._last_dream_day() != self._today())
            if reason is None and not daily_due:
                reason, allowed = self._apply_hold()
            if reason is None:
                allowed = self.clock()
        except Exception:  # noqa: BLE001 - status remains available when a schedule probe fails
            reason, allowed = 'unavailable', None
        return {'active': active, 'recent': recent, 'worker_configured': self.worker_config is not None,
                'backfill': backfill, 'last': last, 'last_failure': last_failure, 'schedule': {
                    'dream': {'held': reason, 'next_allowed': allowed},
                    'backfill': {'held': 'fenced' if fenced else None, 'next_allowed': self._backfill_due}}}
