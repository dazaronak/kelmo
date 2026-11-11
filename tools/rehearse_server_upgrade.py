#!/usr/bin/env python3
"""Rehearse a server upgrade and the way back between two installed Kelmo runtimes, on a new synthetic vault.

One vault, one server profile, two enrolled clients. The client channel is the real ssh gate module of whichever
runtime the `runtime` link points at, started without ssh. The rehearsal:

  1. old runtime serves; an old client writes and corrects records;
  2. the link moves to the new runtime and its daemon starts (`kelmo daemon run`, the service's own command);
     the old client reads back exactly what it wrote and writes more; a new client writes too;
  3. the daemon stops, the link moves back; the old runtime serves every record, the ones written under the
     new runtime included; the new client still works against the old server.

Nothing is installed, no service manager is called and no existing store is opened.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

STEPS = 9
# Run by each runtime's own interpreter: ROOT TOOL ARGUMENTS-JSON -> the tool's result as JSON.
CLIENT_TOOL = '''
import json, sys
from pathlib import Path
from kelmo.client import ClientService, SharedClient
from kelmo.config import ClientProfile
with SharedClient(ClientProfile.load(Path(sys.argv[1]))) as client:
    print(json.dumps(ClientService(client).compat(sys.argv[2], json.loads(sys.argv[3])), default=str))
'''
# The channel of the new edge core: eight reads at once on one connection. ROOT -> {channel, counts}.
MUX_READS = '''
import json, sys, threading
from pathlib import Path
from kelmo.client import ClientService, SharedClient
from kelmo.config import ClientProfile
with SharedClient(ClientProfile.load(Path(sys.argv[1])), multiplex=True) as client:
    service, counts, failures = ClientService(client), [], []
    def read():
        try:
            listed = service.compat('memory_list', {'limit': 100})
            counts.append(len(listed['items'] if isinstance(listed, dict) else listed))
        except Exception as error:
            failures.append(repr(error))
    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    print(json.dumps({'channel': type(client._channel).__name__, 'counts': counts, 'failures': failures}))
'''


def check(condition, message):
    """Fail the rehearsal; unlike assert, this still runs under python -O."""
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--old-prefix', type=Path, required=True, help='Installed old runtime (has bin/kelmo)')
    parser.add_argument('--new-prefix', type=Path, required=True, help='Installed new runtime (bin/kelmo, bin/kelmod)')
    parser.add_argument('--work-dir', type=Path, required=True, help='New directory for the synthetic store')
    parser.add_argument('--archive-process', choices=['confined', 'in-process'], default='confined',
                        help='How the server profile runs its archive (in-process where confinement cannot nest)')
    args = parser.parse_args()
    for path in (args.old_prefix, args.new_prefix, args.work_dir):
        if not path.is_absolute():
            parser.error('Use absolute paths.')
    if args.work_dir.exists():
        parser.error('Work directory must be new; no existing store is accepted.')
    for prefix in (args.old_prefix, args.new_prefix):
        if not os.access(prefix / 'bin/kelmo', os.X_OK):
            parser.error(f'{prefix} has no bin/kelmo.')
    check(os.access(args.new_prefix / 'bin/kelmod', os.X_OK), 'The new runtime has no bin/kelmod.')
    work = args.work_dir
    work.mkdir(parents=True, mode=0o700)
    work = work.resolve()
    gitconfig = work / 'gitconfig'
    gitconfig.write_text('[user]\nname = Upgrade Fixture\nemail = upgrade@example.invalid\n')
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_AUTHOR_NAME='Upgrade Fixture', GIT_AUTHOR_EMAIL='upgrade@example.invalid',
                       GIT_COMMITTER_NAME='Upgrade Fixture', GIT_COMMITTER_EMAIL='upgrade@example.invalid')
    vault, profile, runtime = work / 'vault', work / 'server.json', work / 'runtime'
    old_kelmo, new_kelmo = args.old_prefix / 'bin/kelmo', args.new_prefix / 'bin/kelmo'
    log = work / 'rehearsal.log'
    started, results = time.monotonic(), []

    def record(label):
        results.append(label)
        line = f'PASS {time.monotonic() - started:.2f}s [{len(results)}/{STEPS}] {label}'
        print(line, flush=True)
        with log.open('a') as output:
            output.write(line + '\n')

    def cli(executable, root, *argv, payload=None, timeout=120):
        result = subprocess.run([str(executable), '--vault', str(root), *argv, '--json'],
                                input=json.dumps(payload) if payload is not None else None,
                                env=environment, text=True, capture_output=True, timeout=timeout, check=False)
        try:
            answer = json.loads(result.stdout)
        except ValueError as error:
            raise RuntimeError(f'{executable} {" ".join(argv)} returned no JSON: {result.stderr[-400:]}') from error
        if result.returncode or not answer.get('ok'):
            raise RuntimeError(f'{executable} {" ".join(argv)} failed: {json.dumps(answer.get("error"))}')
        return answer['result']

    def tool(executable, root, name, payload):
        """One memory tool through that runtime's own shared client, as its MCP server calls it (the 0.1.1
        command line offers no tool call on a client root)."""
        result = subprocess.run([str(executable.parent / 'python'), '-I', '-c', CLIENT_TOOL, str(root), name,
                                 json.dumps(payload)], env=environment, text=True, capture_output=True,
                                timeout=120, check=False)
        if result.returncode:
            raise RuntimeError(f'{name} through {executable} failed: {result.stderr.strip()[-400:]}')
        return json.loads(result.stdout)

    def overlapping_reads(root, channel, records, server):
        """Eight reads at once through the new client's multiplexing channel; `channel` is the kind it must get."""
        result = subprocess.run([str(args.new_prefix / 'bin/python'), '-I', '-c', MUX_READS, str(root)],
                                env=environment, text=True, capture_output=True, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError(f'Overlapping reads against the {server} server failed: {result.stderr.strip()[-400:]}')
        answer = json.loads(result.stdout)
        check(answer['channel'] == channel and answer['counts'] == [records] * 8 and not answer['failures'],
              f'Overlapping reads against the {server} server: expected {channel} and eight answers of {records} '
              f'records, got {json.dumps(answer)}')

    def serve(prefix):
        """Points the gate at one runtime, as replacing an installed runtime in place does."""
        if runtime.is_symlink():
            runtime.unlink()
        runtime.symlink_to(prefix)

    def client(executable, name, client_id):
        """A client root whose channel is the ssh gate of the served runtime, for one enrolled client."""
        root = work / name
        gate = ['/usr/bin/env', 'SSH_ORIGINAL_COMMAND=kelmo-client', str(runtime / 'bin/python'), '-I', '-m',
                'kelmo.ssh_gate', '--executable', str(runtime / 'bin/kelmo'), '--vault', str(vault),
                '--profile', str(profile), '--client-id', client_id]
        cli(executable, root, 'client', 'init', '--authority', vault_id, '--command-json', json.dumps(gate))
        return root

    def probe(executable, root, client_id, message):
        """A fresh channel must reach this vault as this enrolled client."""
        connection = cli(executable, root, 'client', 'status', '--probe')['connection']
        check(connection.get('verified') is True and connection.get('authority_id') == vault_id
              and connection.get('client_id') == client_id, message)
        return connection

    def snapshot(executable, root):
        """Every record a client can list, with its exact content and history."""
        listed = tool(executable, root, 'memory_list', {'limit': 100})
        items = listed['items'] if isinstance(listed, dict) else listed
        return {item['id']: {'get': tool(executable, root, 'memory_get', {'id': item['id'], 'full': True}),
                             'history': tool(executable, root, 'memory_history', {'id': item['id']})}
                for item in items}

    def store(executable, root, text):
        return tool(executable, root, 'memory_store', {'content': text, 'kind': 'event', 'domain': 'dev',
                                                       'project': 'upgrade-fixture'})['id']

    # 1. The old runtime: vault, server profile, two enrolled clients.
    cli(old_kelmo, vault, 'init', '--label', 'upgrade-fixture')
    vault_id = cli(old_kelmo, vault, 'status')['vault_id']
    cli(old_kelmo, vault, 'server', 'init', '--profile', str(profile), '--apply')
    for client_id in ('laptop', 'desk'):
        cli(old_kelmo, vault, 'server', 'enroll', '--profile', str(profile), '--client-id', client_id,
            '--machine-tag', client_id, '--apply')
    if args.archive_process == 'in-process':
        settings = json.loads(profile.read_text())
        settings['archive_process'] = 'in-process'
        profile.write_text(json.dumps(settings, indent=2) + '\n')
    cli(old_kelmo, vault, 'server', 'fence', '--profile', str(profile), '--off', '--apply')
    serve(args.old_prefix)
    laptop = client(old_kelmo, 'client-laptop', 'laptop')
    served = probe(old_kelmo, laptop, 'laptop', 'The old client did not reach the synthetic authority.')
    record(f'Old runtime serves an enrolled old client through the ssh gate (archive {served["archive"]["mode"]})')

    # 2. Records and a correction written by the old client to the old server.
    first = store(old_kelmo, laptop, 'Synthetic record one, original.')
    store(old_kelmo, laptop, 'Synthetic record two.')
    tool(old_kelmo, laptop, 'memory_update', {'id': first, 'content': 'Synthetic record one, corrected.',
                                              'expected_version': 1})
    before = snapshot(old_kelmo, laptop)
    check(len(before) == 2, f'Expected two records before the upgrade, found {len(before)}.')
    record('Old client wrote two records and one correction')

    # 3. The new runtime takes the vault: the service's own command, started here by hand.
    serve(args.new_prefix)
    python = args.new_prefix / 'bin/python'
    daemon_runtime = subprocess.run(
        [str(python), '-I', '-c', 'import sys\nfrom pathlib import Path\nfrom kelmo.core.daemon_client import '
         'runtime_dir\nprint(runtime_dir(Path(sys.argv[1])))', str(vault)],
        env=environment, text=True, capture_output=True, check=True).stdout.strip()
    options = {'role': 'vault', 'port': None, 'runtime': daemon_runtime, 'server_profile': str(profile),
               'worker_config': None, 'launcher': str(new_kelmo), 'kelmod': str(args.new_prefix / 'bin/kelmod')}
    subprocess.run([str(python), '-I', '-c', 'import json, sys\nfrom kelmo.core.daemon_admin import write_options\n'
                    'write_options(sys.argv[1], json.loads(sys.argv[2]))', str(vault / '.state'),
                    json.dumps(options)], env=environment, check=True)
    daemon_log = (work / 'daemon.log').open('w')
    daemon = subprocess.Popen([str(new_kelmo), 'daemon', 'run', '--vault', str(vault)], env=environment,
                              stdout=daemon_log, stderr=daemon_log, start_new_session=True)
    try:
        limit = time.monotonic() + 120
        while True:
            check(daemon.poll() is None, f'The new daemon exited with {daemon.returncode}; see {work / "daemon.log"}.')
            try:
                status = cli(new_kelmo, vault, 'daemon', 'status', timeout=10)
                cores = {core['name']: core['ready'] for core in status.get('cores', [])}
                if cores.get('state') and cores.get('archive'):
                    break
            except RuntimeError:
                pass
            check(time.monotonic() < limit, 'The new daemon did not become ready in 120 s.')
            time.sleep(0.5)
        record(f'New runtime started its daemon on the same vault (cores: {", ".join(sorted(cores))})')

        # 4. The old client against the new server: same records, exactly; then more writes.
        probe(old_kelmo, laptop, 'laptop', 'The old client did not reach the new server.')
        check(snapshot(old_kelmo, laptop) == before, 'A record, its correction or its history changed in the upgrade.')
        store(old_kelmo, laptop, 'Synthetic record three, written by the old client under the new server.')
        tool(old_kelmo, laptop, 'memory_update', {'id': first, 'content': 'Synthetic record one, corrected twice.',
                                                  'expected_version': 2})
        record('Old client reads its records back exactly and writes through the new server')

        # 5. A new client against the new server.
        desk = client(new_kelmo, 'client-desk', 'desk')
        store(new_kelmo, desk, 'Synthetic record four, written by the new client under the new server.')
        under_new = snapshot(old_kelmo, laptop)
        check(len(under_new) == 4, f'Expected four records under the new server, found {len(under_new)}.')
        check(snapshot(new_kelmo, desk) == under_new, 'The old and the new client read different records.')
        overlapping_reads(desk, 'MuxChannel', 4, 'new')
        record('New client writes; both clients read the same four records; eight reads overlap on one channel')

        # 6. The way back starts with the daemon's own stop.
        cli(new_kelmo, vault, 'daemon', 'stop', timeout=90)
        check(daemon.wait(timeout=60) is not None, 'The new daemon did not exit.')
    finally:
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGKILL)
        daemon_log.close()
    leftovers = subprocess.run(['/usr/bin/pgrep', '-f', str(vault)], text=True, capture_output=True).stdout.split()
    check(not leftovers, f'Processes of the new runtime outlived its daemon: {leftovers}')
    record('New daemon stopped cleanly and left no process on the vault')

    # 7. The old runtime serves again: every accepted write is there, the ones made under the new server included.
    serve(args.old_prefix)
    check(snapshot(old_kelmo, laptop) == under_new, 'The old server lost or changed a write made under the new one.')
    store(old_kelmo, laptop, 'Synthetic record five, written after the way back.')
    record('Old runtime serves all four records after the way back and accepts a new write')

    # 8. A new client that was not rolled back still works against the old server.
    probe(new_kelmo, desk, 'desk', 'The new client did not reach the old server.')
    check(len(snapshot(new_kelmo, desk)) == 5, 'The new client does not read five records from the old server.')
    store(new_kelmo, desk, 'Synthetic record six, written by the new client to the old server.')
    check(len(snapshot(old_kelmo, laptop)) == 6, 'The write of the new client did not reach the old server.')
    overlapping_reads(desk, 'StdioChannel', 6, 'old')
    record('New client reads and writes against the old server, one request at a time')

    report = {'synthetic_only': True, 'old_runtime': str(args.old_prefix), 'new_runtime': str(args.new_prefix),
              'archive_process': args.archive_process, 'checks': results, 'records': 6, 'vault': str(vault)}
    (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    record('Report saved; no real store, service manager or remote host was touched')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        print(f'FAIL {error}', file=sys.stderr, flush=True)
        sys.exit(1)
