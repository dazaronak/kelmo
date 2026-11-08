"""The public CLI for humans, coding agents and local automation."""
import io
import json
import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__, timings
from .errors import KelmoError
from .interface import UsageError, build_parser, vault_from_environment

DEFAULT_ROOT = Path.home() / '.local/share/kelmo/default'
EXECUTOR_INPUT_LIMIT = 8 * 1048576


def selected_root(args):
    """The vault this call works on: `--vault`, else the vault named once by KELMO_VAULT, else the default."""
    return args.vault or vault_from_environment() or DEFAULT_ROOT


def _readonly_hook_journal(config):
    """Expose existing journal read methods without opening a writer or running schema setup."""
    from .journal import Journal
    path = config.state / 'journal.sqlite3'
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise KelmoError('E_STATE', 'Pending updates unavailable: operation journal path is unsafe.')
    if not path.exists():
        return None
    if not path.is_file():
        raise KelmoError('E_STATE', 'Pending updates unavailable: operation journal is not a regular file.')
    class Reader:
        get = Journal.get
        source_metadata = Journal.source_metadata
    reader = Reader()
    try:
        reader.conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        reader.conn.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise KelmoError('E_STATE', 'Pending updates unavailable: operation journal could not be opened.') from error
    return reader


def read_source(name):
    if name == '-':
        return sys.stdin.read(1048577), 'stdin.md'
    path = Path(name).expanduser()
    if path.is_symlink() or any(part == '.env' or part.startswith('.env.') for part in path.resolve().parts):
        raise KelmoError('E_INPUT', 'This path cannot be used as a memory source.')
    if not path.exists():
        raise KelmoError('E_INPUT', 'The selected source file does not exist.')
    if not path.is_file():
        raise KelmoError('E_INPUT', 'The selected source must be a regular Markdown file.')
    if path.stat().st_size > 1048576:
        raise KelmoError('E_INPUT', 'Source exceeds the one MiB intake limit.')
    return path.read_text(encoding="utf-8"), path.name


def shared_opener(root, profile_path):
    """None for a local vault; under --server-profile, the opener of shared Services for that server
    (server.shared_service): background work without a client, a client's own work with its id."""
    if profile_path is None:
        return None
    from .server import ServerProfile, shared_service
    path = Path(profile_path)
    ServerProfile.load(path)

    def open_service(client_id):
        # Read again on every open, so a fence set after the worker started holds its next job.
        return shared_service(root, ServerProfile.load(path), client_id)

    def fenced():
        try:
            return ServerProfile.load(path).fenced
        except KelmoError:
            return True  # an unreadable profile writes nothing
    open_service.fenced = fenced
    return open_service


def dispatch(args, commands):
    if args.command == 'executor':
        from .executor import resolve
        from .quota import QuotaWait
        # A complete one-MiB source can expand sixfold when JSON-escaped.
        raw = sys.stdin.read(EXECUTOR_INPUT_LIMIT + 1)
        if len(raw.encode()) > EXECUTOR_INPUT_LIMIT:
            raise KelmoError('E_INPUT', 'Executor input exceeds size limit.')
        payload = json.loads(raw)
        # The worker sends a job's conversation beside its payload; it is never model input.
        conversation = payload.pop('conversation', None) if isinstance(payload, dict) else None
        if args.provider_home is not None:
            if args.provider not in ('claude', 'codex'):
                raise KelmoError('E_CONFIG', 'Provider home applies to claude and codex.')
            # The native CLI reads its login and settings from this directory.
            os.environ['CLAUDE_CONFIG_DIR' if args.provider == 'claude' else 'CODEX_HOME'] = str(
                args.provider_home.expanduser().resolve())
        try:
            return resolve(args.provider, args.model, payload,
                           executable=str(args.provider_executable) if args.provider_executable else None,
                           command=json.loads(args.command_json) if args.command_json else None,
                           effort=args.effort, timeout=args.timeout, conversation=conversation)
        except QuotaWait as wait:
            answer = {'outcome': 'quota_wait', 'reset_at': wait.reset_at}
            if wait.reason is not None:
                answer['reason'] = wait.reason
            return answer
    if args.command == 'setup':
        from .install.setup import setup
        args.vault = selected_root(args)
        return setup(args)
    if args.command == 'install':
        from .install.instructions import install_instructions
        return install_instructions(args.instructions, disable_native=args.disable_native_memory)
    if args.command == 'uninstall':
        from .install.removal import uninstall
        return uninstall(args)
    if args.command == 'version':
        return {'version': __version__}
    if args.command == 'help':
        return {'version': __version__, 'commands': sorted(commands.choices),
                    'writer_modes': ['local', 'shared'], 'remote': True, 'installer_parity_verified': False,
                    'output_schema_version': 1, 'shared_client_protocol': 'kelmo-client/1'}
    if args.command == 'completion':
        names = ' '.join(commands.choices)
        script = ('complete -W ' + repr(names) + ' kelmo\n' if args.shell == 'bash' else
                  '#compdef kelmo\n_arguments "1:command:(' + names + ')"\n')
        return {'script': script}
    root = selected_root(args)
    if args.command in ('project', 'projects', 'link', 'unlink'):
        from . import projects
        if args.command == 'project':
            return (projects.add(root, args.name) if args.action == 'add'
                    else projects.rename(root, args.old, args.new))
        if args.command == 'projects':
            return projects.list_projects(root)
        if args.command == 'link':
            return projects.link(root, args.project, args.directory)
        return projects.unlink(root, args.directory, project=args.project)
    if args.command == 'hub':
        from .config import Config, ClientProfile
        from .hub_embed import import_lines, select, write_block
        shared = ClientProfile.selected(root)
        config = ClientProfile.load(root) if shared else Config.load(root)
        current = args.current or (config.state / 'hot' / 'current' if shared else config.wiki.parent / 'hot')
        lines = import_lines(current, args.project, strict=getattr(args, 'strict', False),
                             scope=getattr(args, 'scope', 'both'))
        if args.action == 'import':
            if not args.file:
                return {'text': lines}
            result = write_block(args.file, lines)
            select(config, args.project, 'claude', args.file, current, scope=args.scope, strict=args.strict,
                   hook_file=args.hook_file)
            return result
        full = Path(current) / (args.project + '.full.md')
        if not full.is_file() or full.is_symlink():
            raise KelmoError('E_HOT', f'Combined project view is missing: {full}')
        text = full.read_text(encoding='utf-8').rstrip('\n')
        target = args.file or Path.cwd() / 'AGENTS.md'
        result = write_block(target, text)
        select(config, args.project, 'codex', target, current, hook_file=args.hook_file)
        return result
    if args.command == 'dream':
        from .dreaming_cli import dispatch as dream_dispatch
        return dream_dispatch(root, args)
    if args.command == 'sync':
        from .knowledge_sync import synchronize
        return synchronize(root, args.remote, args.branch, apply=args.apply, push=args.push)
    if args.command == 'snapshot':
        from .migration_cli import snapshot_command
        return snapshot_command(root, args)
    if args.command == 'server':
        from . import server_admin as admin
        if args.action == 'init':
            return admin.initialize(root, args.profile, apply=args.apply)
        if args.action == 'status':
            return admin.inspect(root, args.profile)
        if args.action == 'enroll':
            return admin.enroll(root, args.profile, args.client_id, args.machine_tag,
                                repository_ids=args.repository_id, requester=args.requester, apply=args.apply)
        if args.action == 'repository':
            return admin.enroll_repository(root, args.profile, args.repository_id, args.root,
                                           args.remote, project=args.project, apply=args.apply)
        if args.action == 'fence':
            return admin.set_fence(root, args.profile, args.fenced, apply=args.apply)
        return admin.rotate(root, args.profile, args.writer_id, args.expect_epoch,
                            args.receipt, apply=args.apply)
    if args.command == 'models' and args.action == 'list':
        from .model_selection import catalog
        return catalog(args.query)
    if args.command == 'client' and args.action == 'init':
        from .client import prepare_profile
        try:
            command = json.loads(args.command_json)
            sessions = json.loads(args.sessions_json) if args.sessions_json else None
        except ValueError as error:
            raise KelmoError('E_INPUT', 'Channel command and sessions must be JSON.', 2) from error
        if not isinstance(command, list) or (sessions is not None and not isinstance(sessions, dict)):
            raise KelmoError('E_INPUT', 'Channel command must be a JSON array and sessions a JSON object.', 2)
        return prepare_profile(root, args.authority, command, sessions=sessions, timeout_seconds=args.timeout)
    from .config import ClientProfile
    if ClientProfile.selected(root):
        from .client import dispatch as client_dispatch
        return client_dispatch(root, args, read_source)
    if args.command == 'action':
        raise KelmoError('E_CAPABILITY', 'Action CLI updates require an enrolled shared-client profile. '
                        'Local integrations use ActionState with explicit trusted bindings.')
    if args.command == 'client':
        raise KelmoError('E_NOT_INITIALIZED', 'No shared-client profile is configured here; this is a local vault '
                                             'or an empty directory.')
    if args.command=='sessions':
        if args.action == 'review':
            from .session_review import review
            return review(root, provider=args.provider, session_id=args.session_id,
                          transcript_path=args.transcript)
        from .session_control import configure, inspect
        from .session_worker import retry, tick
        if args.action=='pause':
            return configure(root,{'paused':True})
        if args.action=='resume':
            from .config import Config
            if Config.load(root).sessions['mode']=='off':
                raise KelmoError('E_STATE','Enable session memory through setup before resuming.')
            return configure(root,{'paused':False})
        if args.action=='retry':
            return retry(root,args.job_id)
        if args.action=='run':
            from .install.setup import read_json
            from .daemon import validate_config
            from .config import Config
            selected=(args.executor_config or Config.load(root).sessions.get('executor_config')
                      or Config.load(root).state/'executor.json')
            selected=Path(selected)
            config=validate_config(read_json(selected,required=True))
            return tick(root,config['argv'],timeout=config['timeout_seconds'],max_attempts=config['max_attempts'],
                        override_configured_executor=args.executor_config is not None,
                        open_service=shared_opener(root, args.server_profile))
        result=inspect(root)
        if args.action=='show':
            job=next((j for j in result['jobs'] if j['id']==args.job_id),None)
            if job is None:
                raise KelmoError('E_NOT_FOUND','Session job not found.')
            return job
        return result
    if args.command=='hook' and args.action.startswith('session-'):
        from .session_control import capture_hook
        raw=sys.stdin.read(1048577)
        if len(raw.encode())>1048576:
            raise KelmoError('E_INPUT','Hook payload exceeds the input ceiling.')
        return capture_hook(root,args.provider,args.action,json.loads(raw or '{}'))
    if args.command == 'hot':
        from .config import Config
        from .hot import check_all, render
        config = Config.load(root)
        return check_all(config) if args.check_all else render(config, args.cwd)
    if args.command == 'hook' and args.action in ('start', 'prompt'):
        from types import SimpleNamespace
        from .integration import hook, PENDING_WARNING
        from .config import Config
        raw = sys.stdin.read(1048577)
        if len(raw.encode()) > 1048576:
            raise KelmoError('E_INPUT', 'Hook input exceeds size limit.')
        payload = json.loads(raw or '{}')
        if args.provider and isinstance(payload, dict):
            payload['kelmo_provider'] = args.provider
        import inspect
        kwargs = ({'acknowledge': args._kelmo_acknowledge}
                  if hasattr(args, '_kelmo_acknowledge') and 'acknowledge' in inspect.signature(hook).parameters
                  else {})
        config = Config.load(root)
        pending_warning = ''
        try:
            journal = _readonly_hook_journal(config)
        except (KelmoError, sqlite3.Error, OSError):
            journal, pending_warning = None, PENDING_WARNING
        try:
            return hook(SimpleNamespace(root=root, journal=journal, pending_warning=pending_warning),
                        args.action, payload, **kwargs)
        finally:
            if journal is not None:
                journal.conn.close()
    if args.command == 'config':
        from .config import Config
        config = Config.load(root).as_dict()
        return dict(valid=True, **config) if args.action == 'validate' else config
    if args.command == 'models':
        from .model_store import ModelImportError, import_model
        try:
            if args.action == 'import':
                return import_model(root, args.source_directory)
            from .model_selection import local_profile, status, use
            if args.action == 'status':
                return status(root)
            if args.profile:
                if args.model or args.source_directory:
                    raise KelmoError('E_INPUT', 'Choose a profile or a model and local directory, not both.')
                from .install.setup import read_json
                profile = read_json(args.profile, required=True)
            else:
                if not args.model or not args.source_directory:
                    raise KelmoError('E_INPUT', 'Select MODEL --from DIRECTORY, or --profile FILE.')
                profile = local_profile(args.model, args.source_directory, pooling=args.pooling,
                                        normalization=args.normalization, projection=args.projection,
                                        dimension=args.dimension, query_prefix=args.query_prefix,
                                        document_prefix=args.document_prefix)
            return use(root, profile, dry_run=args.dry_run)
        except ModelImportError as error:
            raise KelmoError('E_MODEL_IMPORT', str(error)) from error
        except OSError:
            raise KelmoError('E_MODEL_IMPORT', 'Model files are missing or unreadable; provide complete verified files with --from.') from None
    if args.command == 'doctor':
        from .doctor import diagnose
        return diagnose(root)
    if args.command == 'admin':
        from .archive_admin import READ_VERBS
        if args.admin_action in READ_VERBS:
            # Read verbs never open the archive writer: they work when the Service cannot.
            from .config import Config
            return administer(args, Config.load(root))
    with timings.stage('engine_import'):
        from .service import Service
    if args.command == 'init':
        interactive = sys.stdin.isatty() and not args.non_interactive and not args.json
        if args.vault is None and interactive:
            root = Path(input(f'Vault directory [{root}]: ').strip() or root)
        label = args.label
        if not label and interactive:
            label = input('Node label: ').strip()
        if not label:
            raise KelmoError('E_INPUT', 'A node label is required; enter one or provide --label.', 2)
        from .initialization import initialize
        return initialize(root, label, wiki_root=args.wiki, project=args.project,
                          git_name=args.git_name, git_email=args.git_email)
    open_service = shared_opener(root, getattr(args, 'server_profile', None))
    with (Service(root) if open_service is None else open_service(None)) as service:
        if args.command == 'graph':
            if args.graph_action == 'stats':
                return service.graph_stats()
            if args.graph_action == 'rebuild':
                return service.graph_rebuild()
            if args.graph_action == 'read':
                return service.graph_read(args.entity_id)
            return service.graph_query(action=args.query_action, entity_id=args.entity_id,
                                       query=args.query, target_id=args.target_id,
                                       radius=args.radius, relation_filter=args.relation_filter,
                                       limit=args.limit, community_id=args.community_id,
                                       exclude_god_nodes=args.exclude_god_nodes,
                                       min_surprise=args.min_surprise, snapshot=args.snapshot,
                                       assignment=args.assignment)
        if args.command == 'admin':
            return administer(args, service)
        if args.command == 'hook':
            from .integration import hook
            raw = sys.stdin.read(1048577)
            if len(raw.encode()) > 1048576:
                raise KelmoError('E_INPUT', 'Hook input exceeds size limit.')
            return hook(service, args.action, json.loads(raw or '{}'))
        if args.command == 'resolution':
            from .resolution import Resolution
            resolver = Resolution(service)
            if args.action == 'list':
                return {'jobs': resolver.list()}
            if args.action == 'claim':
                return {'job': resolver.claim()}
            if args.action == 'reopen':
                return {'job': resolver.reopen(args.job_id)}
            raw = sys.stdin.read(1048577)
            if len(raw.encode()) > 1048576:
                raise KelmoError('E_INPUT', 'Proposal exceeds size limit.')
            return {'operation': resolver.submit(args.job_id, args.token, json.loads(raw))}
        if args.command == 'reconcile':
            return service.reconcile(args.text, args.target)
        if args.command == 'register':
            return service.register(args.targets, kind=args.kind, summary=args.summary, apply=args.apply)
        if args.command == 'tool':
            from .mcp import compat
            raw = sys.stdin.read(EXECUTOR_INPUT_LIMIT + 1)
            if len(raw.encode()) > EXECUTOR_INPUT_LIMIT:
                raise KelmoError('E_INPUT', 'Tool input exceeds size limit.')
            return {'result': compat(service, args.tool, json.loads(raw or '{}'))}
        if args.command == 'pages':
            return service.page_index(args.projects)
        if args.command == 'page':
            return service.page_facts(args.target)
        if args.command == 'context':
            return service.editor_context(args.project, args.budget_tokens, args.selected)
        if args.command == 'create':
            content, filename = read_source(args.file) if args.file else (args.text, 'memory.md')
            from .creation import requested_work
            operation = service.create(content, filename, project=args.project, selected=args.target,
                                       rule_change=args.rule_change, budget_tokens=args.budget_tokens,
                                       idempotency_key=args.idempotency_key, new_work=requested_work(args, content),
                                       pending=args.pending)
            if args.wait:
                try:
                    operation = service.run(operation['id'])
                except KelmoError as error:
                    if error.code not in ('E_BUSY', 'E_CLAIM'):
                        raise
                    return {'operation': service.journal.get(operation['id']), 'processing_deferred': True}
            return {'operation': operation, 'processing_deferred': not operation['terminal']}
        if args.command in {'add', 'promote'}:
            content, filename = read_source(args.file)
            operation = service.capture(content, filename, args.role,
                                        'promote' if args.command == 'promote' else 'archive',
                                        args.idempotency_key, supersedes=args.supersedes)
            if args.wait:
                try:
                    operation = service.run(operation['id'])
                except KelmoError as error:
                    if error.code not in ('E_BUSY', 'E_CLAIM'):
                        raise
                    return {'operation': service.journal.get(operation['id']),
                            'processing_deferred': True}
                if operation['state'] == 'captured':
                    return {'operation': operation, 'processing_scheduled': False,
                            'next_step': 'Captured evidence is not scheduled; an authorized writer must submit it for archival or checked promotion.'}
            return {'operation': operation}
        if args.command == 'status':
            return service.status()
        if args.command in {'trace', 'logs'}:
            trace = service.trace(args.operation_id if args.command == 'trace' else args.operation)
            return trace if args.command == 'trace' else {'events': trace['events']}
        if args.command == 'show':
            return service.show(args.memory_id)
        if args.command == 'forget':
            return service.forget(args.memory_id)
        if args.command == 'list':
            return service.list(args.limit, args.offset)
        if args.command == 'search':
            return service.search(args.query, args.limit, with_metadata=True, project=args.project,
                                  tags=args.tags, since=args.since, until=args.until,
                                  client='memory-cli/' + __version__)
        if args.command == 'worker':
            if args.action == 'daemon':
                from .daemon import run
                return run(service, args.executor_config, once=args.once, open_service=open_service)
            if not 1 <= args.limit <= 100:
                raise KelmoError('E_INPUT', 'Worker limit must be between 1 and 100.', 2)
            ids = [args.operation_id] if args.operation_id else service.queued()[:args.limit]
            operations, deferred = [], []
            for operation_id in ids:
                try:
                    operations.append(service.run(operation_id))
                except KelmoError as error:
                    if error.code not in ('E_BUSY', 'E_CLAIM'):
                        raise
                    operations.append(service.journal.get(operation_id))
                    deferred.append(operation_id)
            result = {'operations': operations}
            if deferred:
                result['deferred_operations'] = deferred
            return result
        if args.command == 'queue':
            if args.action == 'retry':
                return {'operation': service.retry(args.operation_id)}
            return {'operations': service.queue()}
    raise KelmoError('E_USAGE', 'Unknown command.', 2)


def administer(args, target):
    """One operator verb under a grant naming only that verb; target is the Config for read verbs and the
    Service for the others. purge-authorize records a disposition under the purge capability and removes
    nothing; purge executes a disposition stored that way, read from the vault's journal."""
    from .archive_admin import ArchiveAdmin, operator_grant
    verb = args.admin_action
    config = getattr(target, 'config', target)
    admin = ArchiveAdmin(target, operator_grant(config, {'purge' if verb == 'purge-authorize' else verb}))
    if verb == 'dump':
        if args.output is not None:
            return admin.dump(args.output)
        if args.json:
            raise KelmoError('E_INPUT', 'A JSON response cannot carry database bytes; use --output PATH.', 2)
        directory = Path(tempfile.mkdtemp(prefix='kelmo-dump-'))
        try:
            admin.dump(directory / 'archive.db')
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return {'stream': str(directory / 'archive.db')}
    calls = {
        'count': admin.count, 'stats': admin.stats, 'graph': admin.graph,
        'entity-index': admin.entity_index,
        'doctor': lambda: admin.doctor(probe_embedding=args.probe_embedding),
        'backup': lambda: admin.backup(args.directory, args.keep),
        'reembed': lambda: admin.reembed(batch=args.batch, force=args.force),
        'backfill': lambda: admin.backfill(batch=args.batch),
        'search-log': lambda: admin.search_log(args.limit),
        'purge-plan': lambda: admin.purge_plan(args.memory_id),
        'purge-authorize': lambda: admin.authorize_purge(args.memory_id, args.disposition, args.plan_sha256),
        'purge': lambda: admin.purge_report(args.memory_id, args.disposition),
    }
    return calls[verb]()


def _print_admin(verb, result):
    """Plain text for count and backup; other verbs print their JSON on one line."""
    if verb == 'count':
        print(result['count'])
    elif verb == 'backup':
        print(f"{result['path']} {result['bytes']} bytes")
        for name in result['rotated']:
            print(f'rotated out {name}')
    elif verb == 'search-log':
        for record in result['searches']:
            print(json.dumps(record, ensure_ascii=False))
    elif verb == 'doctor':
        for check in result['checks']:
            icon = {'PASS': '+', 'FAIL': 'x', 'SKIP': '-'}.get(check['status'], '!')
            print(f"  [{icon}] {check['name']}: {check['status']}", file=sys.stderr)
            if check['hint'] and check['status'] != 'PASS':
                print(f"      -> {check['hint']}", file=sys.stderr)
    elif verb in ('stats', 'purge-plan', 'purge-authorize', 'purge'):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))


def _stream(path):
    """Write a dump's bytes to stdout, then remove its private temporary directory."""
    path = Path(path)
    try:
        with path.open('rb') as source:
            while chunk := source.read(1 << 20):
                sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def _finish_admin(verb, result, machine):
    """Exit 1 for a failed doctor or unfinished maintenance (failed records)."""
    if 'stream' in result:
        _stream(result['stream'])
        return 0
    failed = result.get('status') == 'failed' or bool(result.get('failed'))
    envelope = {'schema_version': 1, 'ok': not failed, 'result': result}
    if failed:
        envelope['error'] = (
            {'code': 'E_HEALTH', 'message': 'Archive health checks failed; inspect the checks.'} if verb == 'doctor'
            else {'code': 'E_PROCESSING', 'message': 'Maintenance did not finish; inspect the failed and stale '
                                                     'records, then run it again.'})
    if machine:
        print(json.dumps(envelope, ensure_ascii=False))
    else:
        _print_admin(verb, result)
        if failed and verb != 'doctor':
            print(envelope['error']['message'], file=sys.stderr)
    return int(failed)


def main(argv=None, *, executable='kelmo'):
    """Run one call, then write its stage times when it asked for them (`--timings` or KELMO_TIMINGS)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    timings.start(reporting='--timings' in argv or timings.enabled_by_environment())
    try:
        return call(argv, executable=executable)
    finally:
        timings.report()


def call(argv, *, executable='kelmo'):
    machine = '--json' in argv
    try:
        parser, commands = build_parser(__version__)
        help_output = io.StringIO()
        try:
            with redirect_stdout(help_output), timings.stage('parse'):
                args = parser.parse_args(argv)
        except SystemExit as error:
            if error.code:
                raise UsageError('Invalid command arguments.') from error
            result = {'version': __version__} if '--version' in argv else {'help': help_output.getvalue()}
            if machine:
                print(json.dumps({'schema_version': 1, 'ok': True, 'result': result}))
            else:
                print(help_output.getvalue(), end='')
            return 0
        machine = args.json
        acknowledgements = []
        args._kelmo_acknowledge = acknowledgements.append
        args.launcher = executable  # how a hook starts its detached flusher
        timings.set_command(args.command)
        if getattr(args, 'timings', False):
            timings.enable()
        if args.command == 'broker':
            from .broker_launcher import run
            timings.disarm()
            child = args.child[1:] if args.child[:1] == ['--'] else args.child
            return run(json.loads(args.command_json), child, fds_env=args.fds_env)
        if args.command == 'mcp':
            timings.disarm()
            if args.reconnect:
                from .mcp_proxy import serve
                serve(selected_root(args), executable=executable)
            else:
                from .mcp import serve
                serve(selected_root(args))
            return 0
        if args.command == 'serve-client':
            # stdout carries protocol frames only; refusals are frames, never an envelope.
            from .server import protocol_streams, serve_client
            timings.disarm()
            stdin, stdout = protocol_streams()
            return serve_client(selected_root(args), args.profile, args.client_id, stdin, stdout)
        with redirect_stdout(sys.stderr), timings.stage('operation'):
            result = dispatch(args, commands)
        if args.command == 'admin':
            return _finish_admin(args.admin_action, result, machine)
        if args.command in ('hook', 'executor'):
            acknowledgement = result.pop('_kelmo_ack', None)
            print(json.dumps(result))
            sys.stdout.flush()
            for acknowledgement in ([acknowledgement] if acknowledgement is not None else []) + acknowledgements:
                # The output is already with the client; a failed delivery record only means the
                # next turn delivers again, never a failed hook.
                try:
                    acknowledgement()
                except OSError as error:
                    print('kelmo: delivery record not written: ' + str(error), file=sys.stderr)
            return 0
        operation = result.get('operation') if args.command in {'add', 'promote', 'create', 'queue', 'resolution'} else None
        processing = (args.command in {'add', 'promote', 'create'}
                      or args.command == 'worker' and args.action == 'run')
        def broken(item):
            # Failed on a held lock is queued, not broken: the worker requeues it without a charge.
            return item['state'] == 'failed' and item.get('error_code') != 'E_BUSY'
        failed = processing and ((operation and broken(operation)) or any(
            broken(item) for item in result.get('operations', []) if isinstance(item, dict)))
        review_needed = (args.command == 'resolution' and args.action == 'submit'
                         and operation and operation['state'] == 'needs_review')
        failed = bool(failed or review_needed or result.get('healthy') is False or result.get('failed_attempts'))
        if args.command == 'dream' and result.get('state') == 'failed':
            failed = True
        if args.command=='sessions' and args.action=='run':
            # A job waiting behind another writer or a quota reset spent nothing and is not a failure.
            waits=(None,'E_BUSY','E_CLAIM','E_QUOTA')
            failed=bool(failed or result.get('error_code') not in waits
                        or any(j.get('error_code') not in waits for j in result.get('jobs',[])))
        envelope = {'schema_version': 1, 'ok': not failed, 'result': result}
        if operation:
            envelope['operation'] = operation
        if failed and args.command == 'doctor':
            envelope['error'] = {'code': 'E_HEALTH',
                                 'message': 'Memory health checks failed; inspect the checks and warnings.'}
        elif failed and args.command == 'dream':
            envelope['error'] = {'code': result.get('code', 'E_PROCESSING'),
                                 'message': result.get('reason', 'Dreaming pass failed.')}
        elif result.get('failed_attempts'):
            envelope['error'] = {'code': result['failed_attempts'][0]['error_code'],
                                 'message': 'Resolution attempts failed; inspect failed_attempts and the queue.'}
        elif failed and args.command=='sessions':
            code = (result.get('error_code') if result.get('error_code') not in waits else None) or next(
                job['error_code'] for job in result.get('jobs',[]) if job.get('error_code') not in waits)
            envelope['error'] = {'code': code,
                                 'message': 'Session processing failed; inspect sessions before retrying.'}
        elif review_needed:
            envelope['error'] = {
                'code': operation['error_code'],
                'message': 'Resolution needs review; inspect the operation before reopening the job.',
                'resolution_id': args.job_id,
                'reopen': shlex.join([str(executable), 'resolution', 'reopen', args.job_id, '--vault',
                                      str(selected_root(args).expanduser().resolve())]),
            }
        elif failed:
            envelope['error'] = {'code': operation['error_code'] if operation else 'E_PROCESSING',
                                 'message': 'Processing or validation failed; inspect status and trace.'}
            if envelope['error']['code'] == 'E_BUDGET':
                envelope['error']['message'] = 'Wiki budget exceeded; run kelmo doctor and inspect budget issues before retrying.'
            failed_operations = [operation] if operation else result.get('operations', [])
            # The repair runs where the operation lives: under the same server profile (its writer and
            # restricted archive) when this run used one; a shared client's own `queue retry` reaches its server.
            scope = (['--server-profile', str(args.server_profile)]
                     if getattr(args, 'server_profile', None) is not None else [])
            envelope['error']['retry'] = [
                {'operation_id': item['id'],
                 'command': shlex.join([str(executable), 'queue', 'retry', item['id'], '--vault',
                                        str(selected_root(args).expanduser().resolve()), *scope]),
                 'requires_cause_resolved': True}
                for item in failed_operations if broken(item)]
        if machine:
            print(json.dumps(envelope, ensure_ascii=False))
        elif args.command == 'hub' and args.action == 'import' and not args.file:
            print(result['text'])
        elif args.command == 'completion':
            print(result['script'], end='')
        elif operation:
            print(f"{operation['id']}  {operation['state']}")
            if failed:
                print(envelope['error']['message'], file=sys.stderr)
            elif result.get('next_step'):
                print(result['next_step'], file=sys.stderr)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(failed)
    except Exception as error:  # noqa: BLE001 - The CLI boundary must not print private tracebacks.
        code = error.code if isinstance(error, KelmoError) else 'E_USAGE' if isinstance(error, UsageError) else 'E_INPUT' if isinstance(error, ValueError) else 'E_IO' if isinstance(error, OSError) else 'E_INTERNAL'
        message = str(error) if isinstance(error, (KelmoError, UsageError)) else 'Input or local storage could not be processed.'
        if isinstance(error, sqlite3.Error):
            from .server import _busy
            busy = _busy(error)
            code = 'E_BUSY' if busy else 'E_STATE'
            message = ('Database is busy; retry after the current writer finishes.' if busy else
                       'Database state could not be read; run kelmo doctor before retrying.')
        envelope = {'schema_version': 1, 'ok': False, 'error': {'code': code, 'message': message}}
        if machine:
            print(json.dumps(envelope))
        else:
            print(f'{code}: {message}', file=sys.stderr)
        return error.exit_code if isinstance(error, KelmoError) else 2 if isinstance(error, UsageError) else 1


if __name__ == '__main__':
    raise SystemExit(main())
