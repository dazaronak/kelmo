"""The job module of the state core: the background work the worker daemon and the dream timer did as their
own processes.

Each job runs on a thread of the state core, over the writer Service, holding the baton for one step at a time
(a job step yields to interactive writes) and never across a model call (`daemon.execute` lets it go). At most
one job of a kind runs at a time. Claims a job takes are tracked by token and released when its thread ends for
any reason, so a dead job never holds an operation until its lease runs out. Executor children run in their own
process group, recorded under `<state>/executors/state/` so kelmod can kill what is left after a crash of the core.

Kinds:
  worker     one pass of the queue: operations, one resolution request, session jobs (daemon.tick)
  dream      waiting notes first, then the findings pass (dreaming `run`); `apply` runs only the notes, and
             `apply` with `ready` only the projects with `[editor] batch_ready_sources` notes waiting
  backfill   vectors for chunks that have none
  reembed    vectors again for the configured model
  graph      rebuild the wiki graph index
"""
import json
import os
import sys
import threading
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

from .. import stopping
from ..errors import KelmoError, Stopping

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
# A scheduled apply the core's own stop cut resumes this long after the stop, for up to APPLY_QUICK_RESUMES cuts
# in a row; further cuts wait APPLY_MIN_INTERVAL_SECONDS, so a run of restarts cannot restart model calls at will.
APPLY_RESUME_SECONDS = 60
APPLY_QUICK_RESUMES = 2
# A daily Dreaming run that did not complete (it failed, or the core died under it) is tried again after
# DREAM_RETRY_SECONDS, at most DREAM_TRIES times a day; one the core's stop ended costs no try.
DREAM_TRIES = 3
DREAM_RETRY_SECONDS = 900
# Answers of a started daily run that complete the day; answers that give the try back.
DREAM_DAY_STATES = ('finished', 'idle')
DREAM_HELD_STATES = ('deferred', 'paused')
# After a core agrees to a recycle, no job starts for this long: kelmod stops it at once, and if no stop comes
# the core goes on as before.
RECYCLE_GATE_SECONDS = 30.0


def _count(value):
    """A count kept in jobs.json; anything else reads as none."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


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
        self._gate_until = 0.0

    def should_stop(self):
        """True once the jobs or the whole core are stopping: a job ends at its next step."""
        return self._stop.is_set() or stopping.requested()

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
            # Checked under the lock `quiesce` reads `_active` with: a job either shows as running there or
            # does not start.
            if time.monotonic() < self._gate_until or self.should_stop():
                lock.release()
                why = 'is stopping' if self.should_stop() else 'is about to restart'
                error = KelmoError('E_BUSY', f'The state core {why}; run the job again shortly.')
                error.retryable = True
                raise error
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
        except Stopping:
            # The core's stop ended it between steps or cut its model call: nothing is charged, and the work
            # resumes on the next start.
            outcome.update(state='stopped')
            print(f'kelmo: {kind} job stopped for the core\'s stop; it resumes on the next start',
                  file=sys.stderr, flush=True)
            return {'state': 'stopped'}
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
                    timeout=config['timeout_seconds'], should_stop=self.should_stop, step=self.step,
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
        scheduled = options.get('scheduled')
        action = options.get('action', 'run')
        ready = options.get('ready', False)
        if type(ready) is not bool:
            raise KelmoError('E_PROTOCOL', 'The dream job option ready is a boolean.')
        if ready and action != 'apply':
            raise KelmoError('E_INPUT', 'Only a Dreaming apply runs over ready projects; a run drains every project.')
        if scheduled and self._fenced():
            return {'state': 'fenced'}
        with self._dreaming_lock() as free:
            if not free:
                # What the local run answers too; a scheduled job that meets it starts nothing and records
                # no start, so the next tick asks again.
                return {'state': 'busy'}
            return self._dream_run(action, scheduled, ready)

    @contextmanager
    def _dreaming_lock(self):
        """The process lock every Dreaming run takes, yielding whether this job now holds it.

        Inside the daemon one dream job runs at a time anyway. The lock is for a run outside it: a
        `--server-profile` Dreaming call runs in its own process with its own writer (`dreaming_cli.dispatch`
        takes the same file), and two passes over one vault would only meet each other's claims."""
        with self._state_path.with_name('dreaming.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def _dream_run(self, action, scheduled, ready):
        from ..dreaming_cli import run_dream
        # The daily run counts once it really started: a paused or deferred Dreaming takes no day and no try.
        daily = []

        def started():
            if scheduled and action == 'run':
                daily.append(True)
                self._record_dream_try()
            elif scheduled and action == 'apply':
                self._record_apply_start(ready=ready)

        stopped = False
        try:
            # One step per unit (each note, the findings pass), never the whole run. Inside a unit the writer is held
            # only for journal transactions; model calls and the publication run without it, so an interactive write
            # waits for the transaction in progress, not for the unit.
            result = run_dream(self.storage.writer, action, contexts=getattr(self.core.profile, 'contexts', None),
                               clock=self.clock, on_start=started, should_stop=self.should_stop, step=self.step,
                               ready=ready)
        except Stopping:
            stopped = True
            if daily:
                self._record_dream_try(undo=True)
            raise
        finally:
            if scheduled and action == 'apply' and getattr(self, '_apply_started', False):
                self._record_apply_end(stopped=stopped)
        if daily:
            state = result.get('state') if isinstance(result, dict) else None
            if state in DREAM_DAY_STATES:
                self._record_dream_day()
            elif state in DREAM_HELD_STATES:
                # A usage limit or a pause met after the start: Dreaming's own hold brings the run back, and the
                # try is not the run's failure.
                self._record_dream_try(undo=True)
            # Any other answer (a failed findings pass) keeps its try: the run is tried again after
            # DREAM_RETRY_SECONDS, at most DREAM_TRIES times a day.
        return result

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
        while not self.should_stop():
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

    def _record_dream_try(self, undo=False):
        """Counts a start of today's scheduled run, or takes back the one the core's stop ended."""
        def counted():
            tries = self._read_state().get('dream_tries')
            today = self._today()
            count = tries.get('count', 0) if isinstance(tries, dict) and tries.get('day') == today else 0
            if undo:
                return {'day': today, 'count': max(0, count - 1), 'at': self.clock()}
            return {'day': today, 'count': count + 1, 'at': self.clock()}
        self._write_state(dream_tries=counted)

    def _dream_try_allowed(self):
        """Today's scheduled run may start: no try yet, or fewer than DREAM_TRIES and the last one long enough ago."""
        tries = self._read_state().get('dream_tries')
        if not isinstance(tries, dict) or tries.get('day') != self._today():
            return True
        count, at = tries.get('count', 0), tries.get('at', 0)
        if not isinstance(count, int) or not isinstance(at, (int, float)):
            return True
        return count == 0 or count < DREAM_TRIES and self.clock() >= at + DREAM_RETRY_SECONDS

    def _daily_due(self):
        return (time.localtime(self.clock()).tm_hour >= DREAM_HOUR and self._last_dream_day() != self._today()
                and self._dream_try_allowed())

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

    def _record_apply_start(self, ready=False):
        # The notes this pass is given: every first-pass note, or for a ready pass the first-pass notes of the
        # projects it takes. A note of a project the ready pass leaves for the night was never given to it, so it
        # neither marks the pass as stuck nor brings a cut pass back.
        given = ({operation_id for ids in self._ready_projects().values() for operation_id in ids} if ready
                 else self._queue_notes()['archived'])
        previous = self._read_state().get('apply') or {}
        # Cuts in a row: carried while every pass since the last complete one was ended by the core's stop.
        stops = _count(previous.get('stops')) if previous.get('stopped') is True else 0
        self._write_state(apply={'started': self.clock(), 'ids': sorted(given), 'finished': None,
                                 'stops': stops, 'ready': ready})
        self._apply_started = True

    def _record_apply_end(self, stopped=False):
        state = self._read_state().get('apply', {})
        if stopped:
            # A pass the core's stop cut says nothing about whether its notes can progress.
            state.update(stopped=True, no_progress=False, stops=_count(state.get('stops')) + 1)
        else:
            notes = self._queue_notes()
            state['no_progress'] = bool(state.get('ids')) and set(state['ids']) <= notes['archived'] | notes['parked']
            state.update(stopped=False, stops=0)
        state['finished'] = self.clock()
        self._write_state(apply=state)
        self._apply_started = False

    def _apply_hold(self):
        last = self._read_state().get('apply') or {}
        started, ended = last.get('started'), last.get('finished')
        if not isinstance(started, (int, float)):
            return None, None
        if (last.get('stopped') is True and isinstance(ended, (int, float))
                and 0 < _count(last.get('stops')) <= APPLY_QUICK_RESUMES):
            if self.clock() < ended + APPLY_RESUME_SECONDS:
                return 'stopped', ended + APPLY_RESUME_SECONDS
            return None, None
        if ended is None:
            notes = self._queue_notes()
            no_progress = bool(last.get('ids')) and set(last['ids']) <= notes['archived'] | notes['parked']
        else:
            no_progress = last.get('no_progress', False)
        if no_progress and self.clock() < started + APPLY_NO_PROGRESS_SECONDS:
            return 'no_progress', started + APPLY_NO_PROGRESS_SECONDS
        # An apply the daemon did not see end (the core died under it) counts from its start, so a crash never
        # brings the next apply at once; an ended one, stopped or complete, counts from its end.
        since = ended if isinstance(ended, (int, float)) else started
        if self.clock() < since + APPLY_MIN_INTERVAL_SECONDS:
            return 'min_interval', since + APPLY_MIN_INTERVAL_SECONDS
        return None, None

    def _dream_due(self):
        """Dreaming runs once a day from DREAM_HOUR and drains every project; between the daily runs an apply
        starts early. The day is kept in .state/jobs.json once a run completes, so a restart does not run it
        twice; a run that did not complete is tried again (see DREAM_TRIES).

        Without `[editor] batch_apply` the early apply starts when DREAM_WAITING_NOTES notes wait across all
        projects and takes them all. With it, the early apply is the ready pass (see `_ready_apply`): it starts
        once one project has `batch_ready_sources` notes waiting for their first pass and publishes only the
        projects that have, so a small project waits for the night instead of paying a conversation per note.

        A pass the core's stop cut comes back after APPLY_RESUME_SECONDS, or after the usual interval once it
        was cut more than APPLY_QUICK_RESUMES times in a row (see `_apply_hold`). The plain apply
        is then taken up again whatever the count (`_cut_apply_waits`). The ready pass needs no such rule: it
        re-counts a project after every batch and leaves it once fewer than `batch_ready_sources` notes remain,
        so what a cut pass still owed is exactly a project that is still ready, and the same readiness brings
        it back. What it left below the number waits for the night, as it would have after an uncut pass."""
        if self._dream_hold()[0] is not None:
            return None
        if self._daily_due():
            return 'run'
        if self._apply_hold()[0] is not None:
            return None
        if self._ready_apply():
            return 'apply' if self._ready_projects() else None
        if self._cut_apply_waits():
            return 'apply'
        return 'apply' if self._waiting_notes() >= DREAM_WAITING_NOTES else None

    def _ready_apply(self):
        """True when a scheduled apply is the ready pass: `[editor] batch_apply` is on. A ready project is
        published in batches, so with the flag off the scheduler never asks for a ready pass."""
        return self.storage.writer.config.batch_apply is True

    def _ready_projects(self):
        """{project: [operation_id]} of the projects a ready pass would take now, counted as `kelmo dream status`
        and the pass count them: first-pass notes only, published project redirects followed. The registry is
        read only once the notes waiting under their captured project names could make one project ready, so
        the scheduler's tick stays one journal query while too few notes wait. Like `_waiting_notes` it is a
        schedule probe: a failure reads as no ready project (the daily run still drains every note), never
        as an error raised on every tick."""
        from ..creation import waiting_by_project
        from ..wiki import engine
        try:
            ready_at = self.storage.writer.config.batch_ready_sources
            waiting = self.storage.read(lambda reader: waiting_by_project(reader.journal.conn))
            if sum(len(ids) for ids in waiting.values()) < ready_at:
                return {}
            if any(len(ids) < ready_at for ids in waiting.values()):
                # A redirect can join two captured names into one project: only the registry says whether
                # the small ones add up.
                try:
                    registry = engine.load_registry_view(self.core.root)
                except KelmoError:
                    registry = None
                if registry is not None:
                    waiting = self.storage.read(lambda reader: waiting_by_project(reader.journal.conn, registry))
            return {project: ids for project, ids in waiting.items() if len(ids) >= ready_at}
        except Exception:  # noqa: BLE001 - a schedule probe never stops the scheduler
            return {}

    def _cut_apply_waits(self):
        """A scheduled plain apply the core's stop cut is taken up again whatever the count, while a note it was
        given still waits for its first pass. The ready pass comes back through readiness (see `_dream_due`)."""
        last = self._read_state().get('apply') or {}
        if last.get('stopped') is not True or not isinstance(last.get('ids'), list):
            return False
        try:
            return bool(set(last['ids']) & self._queue_notes()['archived'])
        except Exception:  # noqa: BLE001 - a schedule probe never stops the scheduler
            return False

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
            options = {'action': action, 'scheduled': True}
            if action == 'apply' and self._ready_apply():
                options['ready'] = True
            self._background('dream', options)
        if (self.storage.writer.config.embedding is not None and not self._backfill_scheduled
                and self.clock() >= self._backfill_due):
            self._backfill_scheduled = True
            if not self._background('backfill', {'scheduled': True}):
                self._backfill_scheduled = False

    def _background(self, kind, options):
        """Starts a job thread unless one of its kind runs or the core is about to restart; True when started."""
        if self._running[kind].locked() or time.monotonic() < self._gate_until or self.should_stop():
            return False
        thread = threading.Thread(target=self._quietly, args=(kind, options), name=f'job-{kind}', daemon=True)
        thread.start()
        return True

    def _quietly(self, kind, options):
        """A scheduled job: its failure is recorded and written to the core's stderr (kelmod's log), and the
        scheduler keeps going. A failed daily Dreaming run is tried again after DREAM_RETRY_SECONDS, at most
        DREAM_TRIES times a day, so a broken configuration cannot restart it every few seconds; `kelmo dream
        run` starts one by hand."""
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

    def quiesce(self):
        """A recycle request: the kinds of the jobs running now. When none runs, no job starts for
        RECYCLE_GATE_SECONDS, since kelmod is about to stop the core and start it again."""
        with self._active_lock:
            busy = sorted(self._active)
            if not busy:
                self._gate_until = time.monotonic() + RECYCLE_GATE_SECONDS
        return busy

    def request_stop(self):
        """Asks every job to end at its next step, without waiting."""
        self._stop.set()

    def stop(self, timeout=None):
        """Stops scheduling and waits for running jobs. A model call in flight ends once the core's stop is
        requested (`kelmo.stopping`); without it, as in a test, the call runs to its end."""
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
            daily_due = self._daily_due()
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
