"""Optional custom executor using crew's usage advice and saved invocation results."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .daemon import validate_argv, validate_timeout
from .errors import KelmoError
from .executor import termination_forwarding
from .wiki.engine import atomic_write, durable_directory, sync_directory
from . import quota

TERMINAL = {'completed', 'failed', 'cancelled', 'stopped', 'interrupted'}


def call(crew, *args, timeout=30):
    try:
        result = subprocess.run([crew, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise KelmoError('E_EXECUTOR', f'crew {args[0]} did not finish; inspect crew diagnostics and any saved run receipt.') from error
    if result.returncode:
        raise KelmoError('E_EXECUTOR', f'crew {args[0]} exited with {result.returncode}; inspect crew diagnostics.')
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise KelmoError('E_EXECUTOR', f'crew {args[0]} did not return JSON.') from error
    if not isinstance(value, dict):
        raise KelmoError('E_EXECUTOR', f'crew {args[0]} returned an invalid response.')
    return value


def select_route(advice, routes, model=None):
    """Use crew's eligibility decisions; configuration limits which accounts may receive this job."""
    if not isinstance(routes, list) or not routes:
        raise KelmoError('E_CONFIG', 'Configure at least one allowed crew route.')
    allowed = {}
    for route in routes:
        if (not isinstance(route, dict) or set(route) - {'account', 'provider', 'model', 'effort'}
                or not all(isinstance(route.get(key), str) and route[key].strip()
                           for key in ('account', 'provider', 'model'))
                or route['provider'] not in ('claude', 'codex', 'agy')
                or route.get('effort', 'medium') not in ('low', 'medium', 'high', 'xhigh', 'max')):
            raise KelmoError('E_CONFIG', 'Each route needs account, provider, explicit model and valid effort.')
        if model is None or route['model'] == model:
            allowed[route['account']] = route
    rows = {row['account']: row for row in advice.get('rows', [])
            if isinstance(row, dict) and row.get('account') in allowed}
    eligible = [name for name, row in rows.items() if not row.get('excluded')]
    selected = advice.get('advice')
    if selected not in eligible:
        selected = min(eligible, key=lambda name: rows[name]['windows']['seven_day']['used'], default=None)
    if selected is not None:
        return allowed[selected]
    resets = []
    for row in rows.values():
        exhausted = [window.get('resets_at') for key, window in row.get('windows', {}).items()
                     if 'reserve:' + window.get('label', key) in row.get('excluded', [])]
        if exhausted and all(isinstance(value, (float, int)) for value in exhausted):
            resets.append(max(exhausted))
    if resets and quota.within_bounds(min(resets)):
        raise quota.QuotaWait(min(resets))
    raise KelmoError('E_USAGE', 'No configured route is eligible. Refresh crew usage or select an available model.')


def invocation(value, agent_id, invocation_id):
    for agent in value.get('agents', []):
        if agent.get('id') == agent_id:
            for item in agent.get('invocations', []):
                if item.get('id') == invocation_id:
                    return item
    raise KelmoError('E_EXECUTOR', 'crew did not report the allocated invocation.')


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def run(request, config):
    if not isinstance(request, dict) or request.get('protocol') != 'kelmo-executor/1':
        raise KelmoError('E_INPUT', 'Expected a kelmo-executor/1 request.')
    if not isinstance(config, dict) or set(config) - {'crew_executable', 'state_dir', 'timeout_seconds', 'routes'}:
        raise KelmoError('E_CONFIG', 'Unsupported crew adapter configuration.')
    crew = config.get('crew_executable')
    validate_argv([crew], 'E_CONFIG')
    timeout = validate_timeout(config.get('timeout_seconds', 120), 'E_CONFIG')
    directory = config.get('state_dir')
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise KelmoError('E_CONFIG', 'state_dir must be an absolute private directory.')
    identity = hashlib.sha256(json.dumps([request, config], sort_keys=True).encode()).hexdigest()
    work = Path(directory) / identity
    if any(path.is_symlink() for path in (work, *work.parents)):
        raise KelmoError('E_CONFIG', 'The adapter job directory must not traverse a symbolic link.')
    durable_directory(work)
    work.chmod(0o700)
    sync_directory(work)
    import fcntl
    with (work / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise KelmoError('E_BUSY', 'An identical crew adapter request is already running.') from error
        result_path, receipt_path = work / 'result.json', work / 'receipt.json'
        if result_path.exists():
            return json.loads(result_path.read_text(encoding='utf-8'))
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        else:
            if (work / 'launching.json').exists():
                raise KelmoError('E_LAUNCH_UNCERTAIN', 'A previous crew allocation has no receipt. Inspect '
                                'crew runs before retrying; a second invocation was not started.')
            advice = call(crew, 'recommend', '--json')
            route = select_route(advice, config.get('routes'), request.get('model'))
            instructions = (request.get('instructions') or '') + (
                '\nDo NOT spawn agents or subagents; do the work yourself. '
                'Use only the supplied input, without tools or file access. Return one JSON object.')
            atomic_write(work / 'instructions.txt', instructions)
            atomic_write(work / 'input.txt', json.dumps(request.get('input'), ensure_ascii=False))
            agent = dict(route, id='executor', name='Process memory source', permission_mode='read-only',
                         web=False, effort=request.get('effort') or route.get('effort', 'medium'),
                         prompt_file=str(work / 'input.txt'))
            if route['provider'] in ('claude', 'agy'):
                agent.update(policy='isolated', tools=[], system_prompt_file=str(work / 'instructions.txt'))
                if route['provider'] == 'claude':
                    agent['cwd'] = str(work)
            else:
                agent['cwd'] = str(work)
                atomic_write(work / 'input.txt', instructions + '\n\nINPUT\n' +
                             json.dumps(request.get('input'), ensure_ascii=False))
            schema = request.get('output_schema')
            if isinstance(schema, dict):
                schema = dict(schema)
                schema.pop('$schema', None)
                write_json(work / 'schema.json', schema)
                agent['output_schema'] = str(work / 'schema.json')
            manifest = {'version': 1, 'name': 'Kelmo executor', 'max_parallel': 1, 'crew': [agent]}
            write_json(work / 'manifest.json', manifest)
            write_json(work / 'launching.json', {'started_at': time.time()})
            receipt = call(crew, 'start', '--headless', '--file', str(work / 'manifest.json'))
            write_json(receipt_path, receipt)
            (work / 'launching.json').unlink()
            sync_directory(work)
        try:
            session_id = receipt['session_id']
            invocation_id = next(item['invocation_id'] for item in receipt['agents'] if item['id'] == 'executor')
        except (KeyError, StopIteration, TypeError) as error:
            raise KelmoError('E_EXECUTOR', 'crew returned no complete allocation receipt; inspect its run list.') from error
        completed = False
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                status = call(crew, 'status', '--session-id', session_id,
                              timeout=max(0.1, min(30, deadline - time.monotonic())))
                current = invocation(status, 'executor', invocation_id)
                if current.get('state') in TERMINAL:
                    collected = call(crew, 'result', '--session-id', session_id, '--agent', 'executor')
                    current = invocation(collected, 'executor', invocation_id)
                    completed = True
                    if current.get('exit_code') != 0 or current.get('state') != 'completed':
                        receipt_path.replace(work / ('failed-' + invocation_id + '.json'))
                        raise KelmoError('E_EXECUTOR', 'The allocated crew invocation failed; inspect crew result.')
                    answer = current.get('structured_output')
                    if answer is None:
                        try:
                            answer = json.loads(current.get('answer', ''))
                        except (ValueError, TypeError) as error:
                            receipt_path.replace(work / ('failed-' + invocation_id + '.json'))
                            raise KelmoError('E_EXECUTOR', 'The crew invocation returned no JSON answer.') from error
                    if not isinstance(answer, dict):
                        receipt_path.replace(work / ('failed-' + invocation_id + '.json'))
                        raise KelmoError('E_EXECUTOR', 'The crew invocation must return an answer object.')
                    outcome = {'kind': 'proposal', 'result': answer}
                    write_json(result_path, outcome)
                    return outcome
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            raise KelmoError('E_EXECUTOR', 'The crew invocation exceeded the adapter deadline.')
        finally:
            if not completed:
                try:
                    call(crew, 'stop', '--session-id', session_id, '--agent', 'executor', timeout=10)
                except KelmoError:
                    pass  # The durable receipt retains the exact run for inspection or collection.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        raw = sys.stdin.read(8 * 1048576 + 1)
        if len(raw.encode()) > 8 * 1048576:
            raise KelmoError('E_INPUT', 'Executor input exceeds eight MiB.')
        with termination_forwarding():
            result = run(json.loads(raw), json.loads(args.config.read_text(encoding='utf-8')))
    except quota.QuotaWait as wait:
        result = {'kind': 'deferred', 'reset_at': wait.reset_at}
    except Exception as error:
        result = {'kind': 'failure', 'error_code': error.code if isinstance(error, KelmoError) else 'E_EXECUTOR',
                  'reason': str(error) if isinstance(error, KelmoError) else 'The crew adapter could not complete its request.'}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
