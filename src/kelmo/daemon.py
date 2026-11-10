"""Deterministic queue polling with bounded external command execution."""
import json
from contextlib import nullcontext
import math
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from .errors import KelmoError, Stopping, sqlite_busy
from . import outcomes, quota
from .journal import when_free


def validate_argv(argv, code='E_EXECUTOR', *, optional=False):
    if optional and argv is None:
        return None
    if (not isinstance(argv, list) or not argv or not isinstance(argv[0], str) or not argv[0]
            or not all(isinstance(argument, str) and '\0' not in argument for argument in argv)
            or not Path(argv[0]).is_absolute()):
        raise KelmoError(code, 'Executor must be an absolute executable and nonempty string arguments.')
    return argv


def validate_timeout(timeout, code='E_EXECUTOR'):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise KelmoError(code, 'Executor timeout must be between 0 and 600 seconds.')
    return timeout


def validate_config(config):
    allowed = {'argv', 'timeout_seconds', 'max_attempts', 'poll_seconds'}
    if not isinstance(config, dict) or not set(config) <= allowed:
        raise KelmoError('E_CONFIG', 'Executor configuration must be an object with supported fields.')
    argv = validate_argv(config.get('argv'), 'E_CONFIG', optional=True)
    timeout = config.get('timeout_seconds', 120)
    attempts = config.get('max_attempts', 3)
    interval = config.get('poll_seconds', 2)
    validate_timeout(timeout, 'E_CONFIG')
    if type(attempts) is not int or not 1 <= attempts <= 10:
        raise KelmoError('E_CONFIG', 'Maximum attempts must be between 1 and 10.')
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 0.1 <= interval <= 60):
        raise KelmoError('E_CONFIG', 'Poll interval must be between 0.1 and 60 seconds.')
    return {'argv': argv, 'timeout_seconds': timeout,
            'max_attempts': attempts, 'poll_seconds': interval}


def _exit_reason(return_code, output, errors):
    """What the executor said as it failed: its `--json` refusal envelope on stdout, else the last line of
    its stderr, bounded; with neither, only the exit code."""
    try:
        envelope = json.loads(bytes(output).decode('utf-8'))
        error = envelope.get('error') if isinstance(envelope, dict) else None
        if isinstance(error, dict) and isinstance(error.get('message'), str):
            return f'Executor exited {return_code}: {error.get("code")}: {error["message"]}'[:400]
    except (ValueError, UnicodeError):
        pass
    lines = bytes(errors).decode('utf-8', 'replace').strip().splitlines()
    detail = lines[-1].strip()[:300] if lines else 'no diagnostic output'
    return f'Executor exited {return_code}: {detail}'


def execute(argv, payload, *, timeout=120, raw_output=False, termination_grace=0.2, json_on_failure=False):
    """Run one executor process and return its JSON object (or text with raw_output).

    A non-zero exit is E_EXECUTOR, except that with json_on_failure a JSON object on stdout that says
    `is_error: true` is returned, so the caller can read why the provider failed (a usage limit, a lost login).
    In the state core the baton is let go for the call: no journal transaction may be open across it.
    """
    from .core.baton import model_call
    with model_call():
        return _execute(argv, payload, timeout=timeout, raw_output=raw_output,
                        termination_grace=termination_grace, json_on_failure=json_on_failure)


def _execute(argv, payload, *, timeout, raw_output, termination_grace, json_on_failure):
    validate_argv(argv)
    validate_timeout(timeout)
    if (isinstance(termination_grace, bool) or not isinstance(termination_grace, (int, float))
            or not math.isfinite(termination_grace) or not 0 <= termination_grace <= 1):
        raise KelmoError('E_EXECUTOR', 'Executor termination grace is invalid.')
    with tempfile.TemporaryFile() as source:
        source.write(json.dumps(payload).encode())
        source.seek(0)
        try:
            process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=tempfile.gettempdir(), start_new_session=True, close_fds=True,
                                       env=dict(os.environ,KELMO_INTERNAL_JOB='1'))
        except OSError:
            raise KelmoError('E_EXECUTOR', 'Executor could not be started.') from None
        record = _record_group(process.pid)
        output = bytearray()
        errors = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KelmoError('E_EXECUTOR', 'Executor timed out or exceeded output limit.')
                for key, _ in selector.select(min(0.02, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if len(key.data) + len(chunk) > 1048576:
                        raise KelmoError('E_EXECUTOR', 'Executor timed out or exceeded output limit.')
                    key.data.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KelmoError('E_EXECUTOR', 'Executor timed out or exceeded output limit.')
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise KelmoError('E_EXECUTOR', 'Executor timed out or exceeded output limit.') from None
            if return_code != 0:
                if json_on_failure and not raw_output:
                    try:
                        failed = json.loads(output.decode('utf-8'))
                    except (ValueError, UnicodeError):
                        failed = None
                    if isinstance(failed, dict) and failed.get("is_error") is True:
                        return failed
                raise KelmoError('E_EXECUTOR', _exit_reason(return_code, output, errors))
            if raw_output:
                try:
                    result = output.decode('utf-8')
                except UnicodeError:
                    raise KelmoError('E_EXECUTOR', 'Executor did not return valid UTF-8.') from None
                return result
            try:
                result = json.loads(output.decode('utf-8'))
            except (ValueError, UnicodeError):
                raise KelmoError('E_EXECUTOR', 'Executor did not return valid JSON.') from None
            if not isinstance(result, dict):
                raise KelmoError('E_EXECUTOR', 'Executor must return a JSON object.')
            return result
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            # The invocation owns its process group even after a successful parent exit.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            if termination_grace:
                try:
                    process.wait(timeout=termination_grace)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if record is not None:
                try:
                    record.unlink()
                except OSError:
                    pass


# In the state core: the directory where each executor's process group is recorded while it runs, so that
# kelmod kills what a crashed core left behind before it starts the core again.
executor_groups = None


def process_start(pid):
    """When process `pid` started, as an opaque string kelmod derives the same way (`sys::process_start`), or
    None when it is gone. A start time survives exec, so it is right even before the child has exec'd."""
    if sys.platform.startswith('linux'):
        try:
            stat = Path(f'/proc/{pid}/stat').read_text(encoding='ascii')
        except OSError:
            return None
        return stat.rsplit(')', 1)[1].split()[19]  # field 22, starttime in clock ticks since boot
    if sys.platform == 'darwin':
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo
        if libc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0), info, 136) != 136:
            return None  # PROC_PIDTBSDINFO
        seconds, micros = int.from_bytes(info.raw[120:128], 'little'), int.from_bytes(info.raw[128:136], 'little')
        return f'{seconds}.{micros:06d}'
    return None


def _record_group(pgid):
    """`<pgid>` holding the group leader's start time: kelmod kills a recorded group when its leader is gone
    or is still the same process, never when the id now belongs to a process started since."""
    if executor_groups is None:
        return None
    path = Path(executor_groups) / str(pgid)
    try:
        path.write_text(json.dumps({'pgid': pgid, 'start': process_start(pgid)}), encoding='utf-8')
    except OSError:
        return None
    return path


def fact_keys(service, operation):
    """The facts a failed source named, so its outcome points at what a repair must touch."""
    from .wiki import engine
    try:
        changes = engine.extract_changes(service.source_path(operation).read_text(encoding='utf-8'))
    except (KelmoError, OSError, UnicodeError, ValueError):
        return set()
    return {change['fact_key'] for change in changes or []}


# Failure codes whose cause can pass: one budgeted retry each (worker_retries). E_ARCHIVE_PROCESS and
# E_BUSY are a restricted archive process that died or a contended archive lock in shared mode; an
# E_BUSY retry waits BUSY_RETRY_SECONDS and is never charged.
TRANSIENT_FAILURES = ('E_EMBEDDING', 'E_GIT', 'E_IO', 'E_PROCESSING', 'E_ARCHIVE_PROCESS', 'E_BUSY')
BUSY_RETRY_SECONDS = 60


def _retries(connection):
    with connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS worker_retries (
            operation_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, eligible REAL NOT NULL
        )''')


def charge_retry(service, operation, max_attempts=3):
    """Charge one retry of a failed operation whose cause is transient, from the daemon's own budget.

    Returns (state, attempts): 'requeued' (charged and queued again), 'waiting' (its backoff has not
    elapsed, or another worker holds the lock; nothing charged), 'exhausted' (recorded unresolved) or
    'ineligible' (not a transient failure). max_attempts counts total executions: the recorded failure is
    attempt one."""
    if operation['state'] != 'failed' or operation['error_code'] not in TRANSIENT_FAILURES:
        return 'ineligible', None
    connection = service.journal.conn
    _retries(connection)
    with connection:
        connection.execute('INSERT OR IGNORE INTO worker_retries VALUES (?,1,?)',
                           (operation['id'], time.time() + 2))
    retry = connection.execute('SELECT * FROM worker_retries WHERE operation_id=?', (operation['id'],)).fetchone()
    # A lock another writer held is a turn in the queue, not a failed attempt: it never spends the budget.
    busy = operation['error_code'] == 'E_BUSY'
    if retry['attempts'] >= max_attempts and not busy:
        outcomes.record(connection, operation['id'], 'unresolved',
                        outcomes.exhaustion_reason(operation['error_code'], retry['attempts'],
                                                   fact_keys(service, operation)))
        return 'exhausted', retry['attempts']
    if retry['eligible'] > time.time():
        return 'waiting', retry['attempts']
    charged_attempts = retry['attempts'] + (0 if busy else 1)
    charged_eligible = time.time() + (BUSY_RETRY_SECONDS if busy else 2 ** charged_attempts)
    with connection:
        charged = connection.execute('''UPDATE worker_retries SET attempts=?,eligible=?
            WHERE operation_id=? AND attempts=? AND eligible=?''',
            (charged_attempts, charged_eligible, operation['id'],
             retry['attempts'], retry['eligible'])).rowcount
    if charged != 1:
        return 'waiting', retry['attempts']
    try:
        service.retry(operation['id'], reset_budget=False)
    except (KelmoError, sqlite3.Error) as error:
        # A held lock, a busy database or another attempt's claim is contention, not a failure: refund and wait.
        benign = (sqlite_busy(error) if isinstance(error, sqlite3.Error) else
                  error.code in ('E_BUSY', 'E_CLAIM') or
                  (error.code == 'E_STATE'
                   and service.journal.get(operation['id'])['state'] != 'failed'))
        if benign:
            def refund():
                with connection:
                    connection.execute('''UPDATE worker_retries SET attempts=?,eligible=?
                        WHERE operation_id=? AND attempts=? AND eligible=?''',
                        (retry['attempts'], retry['eligible'], operation['id'],
                         charged_attempts, charged_eligible))
            # The refund must land even while the journal is busy, or a wait would cost an attempt.
            when_free(refund)
            return 'waiting', retry['attempts']
        raise
    return 'requeued', charged_attempts


def resume_transient(service, operation, max_attempts=3):
    """A failed operation whose cause was transient, resumed under the same budget the daemon uses, for a
    caller (a shared write retried under its key) that would otherwise wait forever on a terminal row.

    Returns the operation after one charged run, or the operation unchanged when its failure is not
    transient. Raises a retryable E_BUSY while its backoff has not elapsed and a final E_ATTEMPTS once the
    budget is spent; no semantic or model retry is added beyond that budget."""
    from .server import _verdict
    state, attempts = charge_retry(service, operation, max_attempts)
    if state == 'ineligible':
        return operation
    if state == 'requeued':
        return service.run(operation['id'])
    if state == 'exhausted':
        raise _verdict(KelmoError('E_ATTEMPTS', f'Operation {operation["id"]} failed {attempts} times with '
                                               f'{operation["error_code"]}; its retry budget is spent.'), False)
    raise _verdict(KelmoError('E_BUSY', f'Operation {operation["id"]} failed with {operation["error_code"]}; '
                                       'its next retry is not yet eligible.'), True)


def runnable_operations(service, argv):
    missing_executor = None
    for operation_id in service.queued():
        operation = service.journal.get(operation_id)
        if operation['intent'] == 'create':
            if service.editor.deferred_until(operation_id) is not None:
                continue
            if operation['state'] == 'archived':
                # Archived notes wait for the Dreaming pass in nightly mode, and for an executor otherwise.
                if service.config.editor_mode == 'nightly':
                    continue
                if argv is None:
                    if missing_executor is None:
                        from .creation import configured_executor
                        missing_executor = configured_executor(service) is None
                    if missing_executor:
                        continue
        yield operation_id


def tick(service, argv=None, *, max_attempts=3, timeout=120, open_service=None, should_stop=None, step=None):
    """One pass over operations, one resolution request and the session jobs.

    `should_stop` is read before every operation and before every editor model call: once it is true
    the pass ends there, and an operation in flight releases its claim and lease and resumes later.
    Operations another attempt holds are listed under `skipped`. `step` (the state core's job step) is
    entered around each unit of work that uses the journal; a model call inside one lets the baton go.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10 or not 0 < timeout <= 600:
        raise KelmoError('E_INPUT', 'Invalid retry or timeout configuration.')
    validate_argv(argv, optional=True)
    stopping = should_stop or (lambda: False)
    step = step or nullcontext
    failed_attempts, skipped = [], []
    with step():
        _retries(service.journal.conn)
        queued = service.queue()
    for operation in queued:
        try:
            with step():
                state, attempts = charge_retry(service, operation, max_attempts)
        except sqlite3.Error as error:
            if not sqlite_busy(error):
                raise
            skipped.append({'operation_id': operation['id'], 'error_code': 'E_BUSY'})
            continue
        if state == 'exhausted':
            failed_attempts.append({'operation_id': operation['id'], 'error_code': 'E_ATTEMPTS',
                                    'attempts': attempts, 'exhausted': True})
    processed = 0
    with step():
        # A list, not the generator: its body reads the journal and must run inside the step.
        runnable = list(runnable_operations(service, argv))
    for operation_id in runnable:
        if processed >= 20 or stopping():
            break
        try:
            with step():
                if service.journal.get(operation_id)['intent'] == 'create':
                    from .creation import CommandExecutor
                    service.run(operation_id, executor=CommandExecutor(argv, timeout, stopping) if argv else None)
                else:
                    service.run(operation_id)
            processed += 1
        except KelmoError as error:
            if error.code not in ('E_BUSY', 'E_CLAIM'):
                raise
            skipped.append({'operation_id': operation_id, 'error_code': error.code})
        except sqlite3.Error as error:
            if not sqlite_busy(error):
                raise
            skipped.append({'operation_id': operation_id, 'error_code': 'E_BUSY'})
        except Stopping:
            break
    if stopping():
        result = {'operations': processed, 'resolutions': 0, 'stopped': True}
        for name, items in (('skipped', skipped), ('failed_attempts', failed_attempts)):
            if items:
                result[name] = items
        return result
    with step():
        return _resolve_and_sessions(service, argv, timeout, max_attempts, open_service, stopping,
                                     processed, skipped, failed_attempts)


def _resolve_and_sessions(service, argv, timeout, max_attempts, open_service, stopping, processed, skipped,
                          failed_attempts):
    from .resolution import Resolution
    queue = Resolution(service)
    request = queue.claim(lease_seconds=int(timeout) + 60) if argv else None
    quota_waits = []
    if request:
        try:
            reply = request['response'] if request['response'] is not None else quota.checked(
                execute(argv, request['payload'], timeout=timeout))
            queue.submit(request['id'], request['token'], reply)
        except Exception as error:  # noqa: BLE001 - One failed request must not stop the worker.
            from .core.baton import BatonError
            if isinstance(error, BatonError):
                raise
            code = (error.code if isinstance(error, KelmoError) else 'E_IO' if isinstance(error, OSError)
                    else 'E_BUSY' if isinstance(error, sqlite3.Error) and sqlite_busy(error) else 'E_PROCESSING')
            if code == 'E_BUSY':
                queue.release_busy(request['id'], request['token'])
            elif isinstance(error, quota.QuotaWait):
                if queue.quota_wait(request['id'], request['token'], error.reset_at):
                    quota_waits.append({'resolution_id': request['id'],
                                        'operation_id': request['operation_id'],
                                        'reset_at': error.reset_at})
            else:
                detail = str(error) if isinstance(error, KelmoError) else None
                exhausted = queue.fail(request['id'], request['token'], code, max_attempts, detail)
                if exhausted is not None:
                    failed_attempts.append({
                        'resolution_id': request['id'],
                        'operation_id': request['operation_id'],
                        'error_code': code,
                        'exhausted': exhausted,
                    })
    result = {'operations': processed, 'resolutions': int(request is not None)}
    if quota_waits:
        result['quota_waits'] = quota_waits
    if skipped:
        result['skipped'] = skipped
    from .session_worker import tick as session_tick
    from .config import Config
    try:
        if not stopping() and Config.load(service.root).sessions['mode']!='off':
            result['sessions']=session_tick(service.root,argv,timeout=timeout,max_attempts=max_attempts,
                                            open_service=open_service)
            # A quota wait is reported with the waits: it spent no attempt.
            for j in result['sessions'].get('jobs',[]):
                if j.get('error_code')=='E_QUOTA':
                    result.setdefault('quota_waits',[]).append({'session_job_id':j['id'],
                                                                'reset_at':j.get('quota_reset_at')})
                elif j.get('error_code') in ('E_BUSY','E_CLAIM'):
                    # Waiting behind another writer spent no attempt either.
                    result.setdefault('busy_waits',[]).append({'session_job_id':j['id']})
                elif j.get('error_code'):
                    failed_attempts.append({'session_job_id':j['id'],'error_code':j['error_code']})
    except (KelmoError, sqlite3.Error) as error:
        if isinstance(error, sqlite3.Error) and not sqlite_busy(error):
            raise
        code = error.code if isinstance(error, KelmoError) else 'E_BUSY'
        result['sessions']={'jobs':[],'error_code':code}
        if code!='E_BUSY':
            failed_attempts.append({'error_code':code,'stage':'session_processing'})
    if failed_attempts:
        result['failed_attempts'] = failed_attempts
    return result


def run(service, config_path, *, once=False, open_service=None):
    try:
        raw = json.loads(Path(config_path).read_text(encoding='utf-8')) if config_path else {}
    except (OSError, ValueError, UnicodeError) as error:
        raise KelmoError('E_CONFIG', 'Executor configuration is not readable valid JSON.') from error
    config = validate_config(raw)
    argv = config['argv']
    interval = config['poll_seconds']
    stopped = threading.Event()

    def stop(*_):
        stopped.set()

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    # Every executor answer the worker checks passes the login watch: a lost login alerts its owner.
    from .alerts import LoginWatch
    if service is not None:
        quota.observer = LoginWatch.for_service(service).saw
    reported = set()
    # A shared server's opener reads its profile's fence; a fence set while the worker runs (a backup
    # window) pauses every pass until it is lifted, without stopping the process.
    fenced = getattr(open_service, 'fenced', None)
    paused = False
    try:
        while not stopped.is_set():
            if fenced is not None and fenced():
                if once:
                    return {'operations': 0, 'resolutions': 0, 'fenced': True}
                if not paused:
                    print('kelmo worker: the server profile is fenced; waiting', file=sys.stderr, flush=True)
                paused = True
                stopped.wait(interval)
                continue
            if paused:
                print('kelmo worker: the fence is lifted; resuming', file=sys.stderr, flush=True)
            paused = False
            try:
                result = tick(service, argv, max_attempts=config['max_attempts'],
                              timeout=config['timeout_seconds'], open_service=open_service,
                              should_stop=stopped.is_set)
            except sqlite3.Error as error:
                # A database another process holds past its wait is contention: the worker stays up and
                # takes the next pass, so no queued note is stranded behind a dead process.
                if not sqlite_busy(error):
                    raise
                if once:
                    return {'operations': 0, 'resolutions': 0, 'busy': True}
                print('kelmo worker: the journal is busy; retrying on the next pass', file=sys.stderr, flush=True)
                stopped.wait(interval)
                continue
            if once:
                return result
            held = {item['operation_id'] for item in result.get('skipped', ())}
            if held - reported:
                # A claim left by a dead worker blocks its operation until the lease expires; say so.
                print('kelmo worker: skipped operations held by another attempt: '
                      + ', '.join(sorted(held)), file=sys.stderr, flush=True)
            reported = held
            stopped.wait(interval)
        return {'stopped': True}
    finally:
        quota.observer = None
        for number, handler in previous.items():
            signal.signal(number, handler)
