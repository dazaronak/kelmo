"""Shared stdlib-only argument contract for bootstrap and application."""
import argparse
import os
from pathlib import Path


class UsageError(Exception):
    pass


class ExplicitProject(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace._project_requested = True


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs['allow_abbrev'] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError(f'Invalid arguments for {self.prog}; use --help for this command.')


VAULT_ENV_VAR = 'KELMO_VAULT'


def vault_from_environment():
    """The vault selected once for this account by environment, so a client configuration can name the vault
    without appending `--vault` to every call. `--vault` still wins; None when the variable is unset."""
    value = os.environ.get(VAULT_ENV_VAR)
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def common(parser, *, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument('--vault', type=Path, default=default, help='Select a vault directory')
    parser.add_argument('--json', action='store_true', default=False if root else argparse.SUPPRESS,
                        help='Emit a versioned machine-readable response')
    parser.add_argument('--non-interactive', action='store_true', default=False if root else argparse.SUPPRESS,
                        help='Fail with missing input instead of prompting')
    parser.add_argument('--timings', action='store_true', default=False if root else argparse.SUPPRESS,
                        help='Write this call stage times as one JSON line on stderr, before or after the '
                             'command name (see also KELMO_TIMINGS)')


def server_profile(parser):
    parser.add_argument('--server-profile', type=Path,
                        help='Absolute server enrollment profile: run as that shared server\'s writer, '
                             'with its restricted archive; omit for a local vault')


def build_parser(version):
    parser = Parser(prog='kelmo', description='One interface for wiki, archive and operation history.')
    common(parser, root=True)
    parser.add_argument('--version', action='version', version=version)
    commands = parser.add_subparsers(dest='command', required=True)
    def command(name, help):
        child = commands.add_parser(name, help=help, description=help)
        common(child)
        return child
    project = command('project', 'Create or rename a project hub in the selected vault.')
    project_actions = project.add_subparsers(dest='action', required=True)
    for name in ('add', 'rename'):
        action = project_actions.add_parser(name)
        common(action)
        action.add_argument('name' if name == 'add' else 'old')
        if name == 'rename':
            action.add_argument('new')
    command('projects', 'List project hubs, repository links and open work.')
    link = command('link', 'Link a Git repository and its worktrees to a project.')
    link.add_argument('--project', required=True)
    link.add_argument('directory', nargs='?', type=Path, default=Path.cwd())
    unlink = command('unlink', 'Detach a Git repository from a project; the hub remains.')
    unlink.add_argument('--project')
    unlink.add_argument('directory', nargs='?', type=Path, default=Path.cwd())
    hub = command('hub', 'Print or install opt-in project hub instructions.')
    hub_actions = hub.add_subparsers(dest='action', required=True)
    for name in ('import', 'embed'):
        action = hub_actions.add_parser(name)
        common(action)
        action.add_argument('--project', required=True)
        action.add_argument('--file', type=Path, help='Write only the Kelmo-owned block in this file; otherwise print')
        action.add_argument('--hook-file', type=Path, help='Selected client settings/hooks JSON, for doctor ordering checks')
        if name == 'import':
            action.add_argument('--strict', action='store_true', help='Import the combined mandatory and project view')
            action.add_argument('--scope', choices=['global', 'project', 'both'], default='both',
                                help='Choose mandatory, project, or both import lines')
            action.add_argument('--current', type=Path, help='Directory containing the generated views')
        else:
            action.add_argument('--codex', action='store_true', required=True)
            action.add_argument('--current', type=Path, help='Directory containing the generated views')
    init = command('init', 'Initialize an empty local vault without changing an existing installation.')
    init.add_argument('--label', help='Stable name of this node')
    init.add_argument('--project', default='default', help='First project for a new current-schema vault')
    init.add_argument('--git-name', help='Repository-local Git author name for a new vault')
    init.add_argument('--git-email', help='Repository-local Git author email for a new vault')
    init.add_argument('--wiki', type=Path, help='Separate knowledge Git repository (pages under knowledge/wiki, registry under knowledge/provenance); omit for the default vault layout')
    snapshot = command('snapshot', 'Copy, inspect, import and recover a stopped deployment.')
    snapshot_actions = snapshot.add_subparsers(dest='action', required=True)
    for name in ('create', 'verify', 'inventory', 'import', 'mark-cutover', 'tail', 'rollback-check', 'recover', 'report',
                 'backup'):
        action = snapshot_actions.add_parser(name)
        common(action)
        if name in ('create', 'import', 'recover'):
            action.add_argument('--spec', type=Path, required=True)
        if name in ('verify', 'inventory', 'import', 'recover'):
            action.add_argument('--snapshot', type=Path, required=True)
        if name in ('create', 'recover', 'report', 'backup'):
            action.add_argument('--destination', type=Path, required=True)
        if name in ('create', 'recover', 'backup'):
            action.add_argument('--writers-stopped', action='store_true')
        if name in ('create', 'import', 'mark-cutover', 'recover', 'report', 'backup'):
            action.add_argument('--apply', action='store_true')
        if name == 'backup':
            action.add_argument('--keep', type=int, default=7, help='Backups this tool keeps in the destination')
        if name == 'report':
            action.add_argument('--include', type=Path, action='append', default=[])
        if name == 'inventory':
            action.add_argument('--queue', action='append', default=[])
            action.add_argument('--details', action='store_true')
    sync = command('sync', 'Preview or synchronize the knowledge Git repository under publication locks.')
    sync.add_argument('--remote', default='origin')
    sync.add_argument('--branch', required=True)
    sync.add_argument('--apply', action='store_true')
    sync.add_argument('--push', action='store_true', help='Also push the synchronized branch; never force')
    dream = command('dream', 'Configure, inspect or run a bounded Dreaming pass; no scheduler is started. '
                             '`apply` publishes every waiting note through the editor now; `run` does that '
                             'first, then the findings pass.')
    dream_actions = dream.add_subparsers(dest='action', required=True)
    for name in ('configure', 'status', 'pause', 'resume', 'apply', 'run', 'findings', 'schedule'):
        action = dream_actions.add_parser(name)
        common(action)
        server_profile(action)
        action.add_argument('--client-id', help='Enrolled origin for a shared Dreaming run')
        if name == 'configure':
            action.add_argument('--executor-config', type=Path, required=True)
            action.add_argument('--max-sources', type=int, default=10)
            action.add_argument('--max-pages', type=int, default=12)
            action.add_argument('--budget-tokens', type=int, default=60000)
            action.add_argument('--apply', action='store_true')
        if name == 'findings':
            action.add_argument('--state', choices=['open', 'published', 'refused', 'stale'])
        if name == 'schedule':
            action.add_argument('--executable', type=Path, required=True)
            action.add_argument('--directory', type=Path, required=True)
            action.add_argument('--platform', choices=['linux', 'darwin'], required=True)
            action.add_argument('--interval', type=int, default=604800,
                                help='Seconds between passes; a ceilinged pass, not a continuously running agent')
            action.add_argument('--apply', action='store_true')
    mcp = command('mcp', 'Serve memory tools over stdio.')
    mcp.add_argument('--reconnect', action='store_true',
                     help='Keep local stdio alive and restart a failed MCP backend without replaying tools')
    broker = command('broker', 'Run one command with an owner-selected stdio embedding broker.')
    broker.add_argument('--command-json', required=True, help='Embedding broker executable and arguments as JSON')
    broker.add_argument('--fds-env', default='KELMO_EMBED_FDS')
    broker.add_argument('child', nargs=argparse.REMAINDER, help='After --, the absolute command to run')
    serve = command('serve-client', 'Serve one enrolled shared client over stdio; run only by its trusted launcher.')
    serve.add_argument('--profile', type=Path, required=True, help='Absolute server enrollment profile')
    serve.add_argument('--client-id', required=True, help='Enrolled client this channel belongs to')
    server = command('server', 'Prepare enrollment and writer control; mutations preview unless --apply.')
    server_actions = server.add_subparsers(dest='action', required=True)
    for name in ('init', 'status', 'enroll', 'repository', 'fence', 'rotate-writer'):
        action = server_actions.add_parser(name)
        common(action)
        action.add_argument('--profile', type=Path, required=True)
        if name != 'status':
            action.add_argument('--apply', action='store_true')
        if name == 'enroll':
            action.add_argument('--client-id', required=True)
            action.add_argument('--machine-tag', required=True)
            action.add_argument('--repository-id', action='append', default=[])
            action.add_argument('--requester', action='store_true',
                                help='Explicitly grant requester-attestation authority to this client')
        if name == 'repository':
            action.add_argument('--repository-id', required=True)
            action.add_argument('--root', type=Path, required=True)
            action.add_argument('--remote', required=True)
            action.add_argument('--project')
        if name == 'fence':
            state = action.add_mutually_exclusive_group(required=True)
            state.add_argument('--on', dest='fenced', action='store_true')
            state.add_argument('--off', dest='fenced', action='store_false')
        if name == 'rotate-writer':
            action.add_argument('--writer-id', required=True)
            action.add_argument('--expect-epoch', type=int, required=True)
            action.add_argument('--receipt', type=Path, required=True)
    client = command('client', 'Prepare or inspect an opt-in shared-client directory.')
    client_actions = client.add_subparsers(dest='action', required=True)
    client_init = client_actions.add_parser('init', help='Write a client profile; nothing is registered')
    common(client_init)
    client_init.add_argument('--authority', required=True, help='Vault ID the client must reach')
    client_init.add_argument('--command-json', required=True,
                             help='JSON array: absolute channel executable and its arguments')
    client_init.add_argument('--sessions-json', help='JSON object of enrolled transcript roots per provider')
    client_init.add_argument('--timeout', type=float, default=20)
    for name in ('status', 'flush', 'resend'):
        action = client_actions.add_parser(name)
        common(action)
        if name == 'status':
            action.add_argument('--probe', action='store_true',
                                help='Open the configured channel and verify its authority without flushing writes')
        if name == 'resend':
            action.add_argument('--code', action='append', default=[], metavar='CODE',
                                help='Refusal code to resend (repeatable; default E_INPUT)')
            action.add_argument('--apply', action='store_true',
                                help='Queue them again under new keys and deliver; without it only list them')
    tool = command('tool', 'Call one archive tool handler by name (the handlers behind the MCP tools) '
                           'with a JSON object on stdin.')
    tool.add_argument('tool')
    graph = command('graph', 'Navigate explicit relationships in the selected wiki.')
    graph_actions = graph.add_subparsers(dest='graph_action', required=True)
    for name in ('stats', 'rebuild'):
        common(graph_actions.add_parser(name))
    graph_read = graph_actions.add_parser('read', help='Read a current page by its graph ID or unique alias')
    common(graph_read)
    graph_read.add_argument('entity_id')
    graph_query = graph_actions.add_parser('query', help='Refresh changed wiki structure and query it')
    common(graph_query)
    graph_query.add_argument('query_action', choices=['search', 'neighbors', 'impact', 'path', 'details',
                                                      'communities', 'god_nodes', 'surprises'])
    graph_query.add_argument('--id', dest='entity_id', help='Wiki-relative page ID without .md, or a unique alias')
    graph_query.add_argument('--query', help='Page search text')
    graph_query.add_argument('--target', dest='target_id', help='Destination page for a path query')
    graph_query.add_argument('--radius', type=int, default=1, help='Neighbor radius, at most 2')
    graph_query.add_argument('--relation', dest='relation_filter', action='append',
                             help='Restrict traversed relations; repeat for additional types')
    graph_query.add_argument('--limit', type=int, default=50, help='Result ceiling, at most 200')
    graph_query.add_argument('--community', dest='community_id', type=int,
                             help='communities/surprises: only this derived group')
    graph_query.add_argument('--exclude-god-nodes', action='store_true',
                             help='neighbors/impact/path/details: skip edges touching derived hubs')
    graph_query.add_argument('--min-surprise', type=float, default=0.0,
                             help='surprises: lowest structural score, 0 to 1')
    graph_query.add_argument('--snapshot', help='Refuse unless this source_digest is still the current snapshot')
    graph_query.add_argument('--assignment',
                             help='Refuse unless this analytics assignment_sha256 is still the current one')
    admin = command('admin', 'Operator-only archive administration; never exposed through MCP.')
    admin_actions = admin.add_subparsers(dest='admin_action', required=True)
    for name, text in (('count', 'Number of archive rows, hidden included'),
                       ('stats', 'Counts, size, index identity and pending vectors'),
                       ('graph', 'Rebuild the selected wiki graph and its derived topology'),
                       ('entity-index', 'Rebuild the graph name index from published entities')):
        common(admin_actions.add_parser(name, help=text))
    admin_doctor = admin_actions.add_parser('doctor', help='Dependency, coverage and graph health')
    common(admin_doctor)
    admin_doctor.add_argument('--probe-embedding', action='store_true',
                              help='Also call the configured embedding provider')
    admin_backup = admin_actions.add_parser('backup', help='Consistent snapshot beside the archive, rotated')
    common(admin_backup)
    admin_backup.add_argument('--dir', dest='directory', type=Path, help='Backup directory (default archive/backups)')
    admin_backup.add_argument('--keep', type=int, default=14, help='Newest snapshots kept')
    admin_dump = admin_actions.add_parser('dump', help='Consistent snapshot to a file, or raw bytes on stdout')
    common(admin_dump)
    admin_dump.add_argument('--output', type=Path, help='Destination file; omit to stream the database to stdout')
    admin_reembed = admin_actions.add_parser('reembed', help='Rebuild chunks and vectors of the chunk index')
    common(admin_reembed)
    admin_reembed.add_argument('--batch', type=int, default=64)
    admin_reembed.add_argument('--force', action='store_true')
    admin_backfill = admin_actions.add_parser('backfill', help='Embed missing chunk vectors; one run at a time')
    common(admin_backfill)
    admin_backfill.add_argument('--batch', type=int)
    admin_log = admin_actions.add_parser('search-log', help='Last logged searches, oldest first')
    common(admin_log)
    admin_log.add_argument('--limit', type=int, default=500, help='At most 500')
    admin_plan = admin_actions.add_parser('purge-plan', help='Preview what a purge would remove, retain and need')
    common(admin_plan)
    admin_plan.add_argument('memory_id')
    admin_authorize = admin_actions.add_parser(
        'purge-authorize', help='Record a named disposition for the exact plan purge-plan showed; removes nothing')
    common(admin_authorize)
    admin_authorize.add_argument('memory_id')
    admin_authorize.add_argument('--disposition', required=True, help='New disposition ID to record')
    admin_authorize.add_argument('--plan-sha256', dest='plan_sha256', required=True,
                                 help='The plan_sha256 kelmo admin purge-plan printed for this record')
    admin_purge =admin_actions.add_parser('purge', help='Physically remove one record under a named disposition')
    common(admin_purge)
    admin_purge.add_argument('memory_id')
    admin_purge.add_argument('--disposition', required=True,
                             help='Disposition ID recorded earlier with purge-authorize')
    hot = command('hot', 'Preview explicitly configured global and project wiki context.')
    hot.add_argument('--cwd', type=Path, help='Absolute project or worktree directory; omit for globals')
    hot.add_argument('--check-all', action='store_true', help='Validate every selection without page contents')
    hook = command('hook', 'Handle a native agent lifecycle event.')
    hook.add_argument('action', choices=['start', 'prompt', 'capture','session-stop','session-checkpoint','session-end'])
    hook.add_argument('--provider', choices=['claude','codex'])
    sessions = command('sessions','Inspect and control automatic session memory.')
    actions=sessions.add_subparsers(dest='action',required=True)
    for name in ('list','pause','resume'):
        common(actions.add_parser(name))
    for name in ('show','retry'):
        child=actions.add_parser(name)
        common(child)
        child.add_argument('job_id')
    review = actions.add_parser('review', help='Review captured session bytes and pending work before clearing')
    common(review)
    review.add_argument('--provider', choices=['claude', 'codex'])
    review.add_argument('--session-id')
    review.add_argument('--transcript', type=Path, help='Current enrolled transcript to compare with its snapshot')
    run=actions.add_parser('run')
    common(run)
    run.add_argument('--executor-config',type=Path)
    server_profile(run)
    setup = command('setup', 'Provision a vault, model, agent profiles and background service.')
    setup.add_argument('--label', default='local')
    setup.add_argument('--project', default='default', action=ExplicitProject)
    setup.add_argument('--link', type=Path, help='Link this Git repository to the selected project')
    setup.add_argument('--git-name')
    setup.add_argument('--git-email')
    setup.add_argument('--wiki', type=Path, help='Separate, empty knowledge Git repository (pages under knowledge/wiki)')
    setup.add_argument('--home', type=Path, default=Path.home())
    setup.add_argument('--claude-profile', type=Path, help='Explicit Claude profile directory')
    setup.add_argument('--codex-profile', type=Path, help='Explicit Codex profile directory')
    setup.add_argument('--claude-mcp-config', type=Path, help='Override the selected Claude MCP config path')
    setup.add_argument('--shared-authority', help='Initialize a client of this existing server authority')
    setup.add_argument('--shared-command-json', help='Shared server channel as an absolute JSON argument vector')
    setup.add_argument('--executable', type=Path,
                       help='The kelmo command the worker and clients run (default: this one)')
    setup.add_argument('--model-source', type=Path, help='Use local pinned model files instead of fetching them')
    setup.add_argument('--embedding-profile', type=Path, help='Select a complete local or broker JSON profile')
    setup.add_argument('--skip-model', action='store_true', help='Skip model provisioning; lexical search remains available')
    setup.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    server_profile(setup)
    setup.add_argument('--client', choices=['claude', 'codex', 'both', 'none'], default='both')
    policy = setup.add_mutually_exclusive_group(required=True)
    policy.add_argument('--disable-native-memory', action='store_true')
    policy.add_argument('--allow-native-memory', action='store_true')
    setup.add_argument('--executor-config', type=Path)
    setup.add_argument('--provider', choices=['claude', 'codex', 'custom'])
    setup.add_argument('--provider-executable', type=Path)
    setup.add_argument('--provider-home', type=Path, help='Account directory of the provider CLI: CLAUDE_CONFIG_DIR for claude, CODEX_HOME for codex')
    setup.add_argument('--provider-command-json')
    setup.add_argument('--model')
    setup.add_argument('--no-start', action='store_true')
    setup.add_argument('--dry-run', action='store_true', help='Preview without writes, downloads, provider calls or service activation')
    setup.add_argument('--session-memory',choices=['auto','archive-only','off'])
    setup.add_argument('--extractor-provider',choices=['claude','codex'])
    setup.add_argument('--extractor-model')
    uninstall = command('uninstall', 'Remove exact-owned client integrations; retain worker and data.')
    uninstall.add_argument('--home', type=Path, default=Path.home())
    uninstall.add_argument('--claude-profile', type=Path)
    uninstall.add_argument('--codex-profile', type=Path)
    uninstall.add_argument('--claude-mcp-config', type=Path)
    uninstall.add_argument('--executable', type=Path, required=True)
    uninstall.add_argument('--client', choices=['claude', 'codex', 'both'], default='both')
    uninstall.add_argument('--apply', action='store_true', help='Apply removal; otherwise preview only')
    executor = command('executor', 'Run a native or custom provider adapter with JSON stdin/stdout.')
    executor.add_argument('--provider', choices=['claude', 'codex', 'custom'], required=True)
    executor.add_argument('--model', help='Explicit native model; optional for a custom router')
    executor.add_argument('--command-json', help='Custom adapter argv as JSON, starting with an absolute executable')
    executor.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    executor.add_argument('--timeout', type=float, default=120)
    executor.add_argument('--provider-executable', type=Path, help='Pinned provider CLI selected during setup')
    executor.add_argument('--provider-home', type=Path, help='Account directory of the provider CLI: CLAUDE_CONFIG_DIR for claude, CODEX_HOME for codex')
    install = command('install', 'Install memory guidance into a selected instruction file.')
    install.add_argument('--instructions', type=Path, required=True, help='Explicit CLAUDE.md or AGENTS.md path')
    policy = install.add_mutually_exclusive_group(required=True)
    policy.add_argument('--disable-native-memory', action='store_true', help='Recommended: prohibit native memory in agent instructions')
    policy.add_argument('--allow-native-memory', action='store_true', help='Omit the native-memory prohibition')
    reconcile = command('reconcile', 'Find current fact candidates and persist a search receipt.')
    reconcile.add_argument('--text', required=True, help='Exact proposed fact text')
    reconcile.add_argument('--target', required=True, help='Proposed wiki page')
    action = command('action', 'Apply an explicit source-backed update through an enrolled shared client.')
    action_steps = action.add_subparsers(dest='action', required=True)
    action_apply = action_steps.add_parser('apply')
    common(action_apply)
    action_apply.add_argument('--file', required=True, help='ActionUpdate JSON, or - for stdin')
    action_apply.add_argument('--idempotency-key', required=True, help='Stable key for this exact update')
    pages = command('pages', 'List the registered pages of the store: one row per page, by project.')
    pages.add_argument('--project', action='append', dest='projects', metavar='PROJECT',
                       help='Only this project; repeat to ask for several')
    page = command('page', 'Read one page exactly: its entry, derived status and every fact on it.')
    page.add_argument('target')
    context = command('context', 'Build the bounded page index, selected facts and open-work digest of one project.')
    context.add_argument('--project', required=True, help='The project this call is about')
    context.add_argument('--budget-tokens', type=int, required=True,
                        help='Ceiling for the whole view, in the same estimated tokens the wiki budget uses')
    context.add_argument('--select', action='append', dest='selected', default=[], metavar='TARGET',
                        help='Give this page its complete facts; repeat, order honoured')
    resolutions = command('resolution', 'Claim and submit durable agent resolution jobs.')
    actions = resolutions.add_subparsers(dest='action', required=True)
    for action in ('list', 'claim'):
        common(actions.add_parser(action))
    submit = actions.add_parser('submit', help='Read a JSON proposal from stdin')
    common(submit)
    submit.add_argument('job_id')
    submit.add_argument('--token', required=True)
    reopen = actions.add_parser('reopen', help='Reopen a reviewed job using current facts and its original evidence')
    common(reopen)
    reopen.add_argument('job_id')
    for name, help in [('add', 'Capture evidence; unknown/collector sources stay local.'),
                       ('promote', 'Queue accepted revision-checked facts from a declared lead.')]:
        child = command(name, help)
        child.add_argument('--file', required=True, help='Markdown source file, or - for stdin; basename must start with an ASCII letter/digit and contain only letters, digits, dots, underscores or hyphens, ending in .md')
        child.add_argument('--role', choices=['lead', 'collector', 'implementer', 'reviewer', 'unknown'], default='unknown')
        child.add_argument('--idempotency-key', help='Reuse the same operation when retrying a request')
        child.add_argument('--wait', action='store_true',
                           help='Wait for completion; shared clients observe the server worker')
        child.add_argument('--wait-timeout', type=float, default=300, metavar='SECONDS',
                           help='Shared-client wait deadline (default: 300); timeout leaves work queued')
        child.add_argument('--supersedes', metavar='MEMORY_ID',
                           help='Archive record this source replaces; it is forgotten once this one is archived. '
                                'The record must be the current one of its chain: an already replaced or forgotten '
                                'record is refused with the current one named')
    create = command('create', 'Capture plain text and queue its scoped wiki update.')
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', help='The original note text')
    source.add_argument('--file', help='UTF-8 Markdown source, or - for stdin')
    create.add_argument('--project', required=True, help='Project that owns the note')
    create.add_argument('--target', action='append', default=[], help='Registered page; repeat to select several')
    create.add_argument('--rule-change', action='store_true', help='Explicit rule change from an enrolled requester')
    create.add_argument('--new-work', metavar='SLUG',
                        help='Open a new work page projects/PROJECT/SLUG.md for this note\'s open work; '
                             'it is created only with the facts that land on it')
    create.add_argument('--title', help='Title of the new work page (one line, at most 120 characters)')
    create.add_argument('--summary', help='Summary of the new work page (one line, at most 200 characters; '
                                          'default: the title)')
    create.add_argument('--priority', choices=['H1', 'H2', 'H3'],
                        help='Priority of the new work page, shown on its hub row; default: the priority '
                             'line of the note frontmatter, else none')
    create.add_argument('--pending', metavar='LINE',
                        help='One line (at most 240 characters) that agents see at startup while this note '
                             'waits for the editor; it disappears once the note is applied')
    create.add_argument('--budget-tokens', type=int, default=None,
                        help="Maximum editor context budget; default: the vault's [editor] budget_tokens")
    create.add_argument('--idempotency-key', help='Reuse this key when retrying the same input')
    create.add_argument('--wait', action='store_true', help='Wait for the scoped update')
    create.add_argument('--wait-timeout', type=float, default=300, metavar='SECONDS',
                        help='Shared-client wait deadline (default: 300)')
    register = command('register', 'Register existing unregistered wiki files as pages, so notes can target them.')
    register.add_argument('targets', nargs='+', metavar='PAGE', help='Wiki path, e.g. projects/demo/setup-notes.md')
    register.add_argument('--kind', choices=['topic', 'work'], default='topic',
                          help='topic (default) holds settled facts; work also holds open work and needs --summary')
    register.add_argument('--summary', default='', help='One-line page summary (at most 200 characters)')
    register.add_argument('--apply', action='store_true', help='Publish; without it only the entries are shown')
    register.add_argument('--idempotency-key', help='Shared clients: reuse this key when retrying the same request')
    command('status', 'Show achieved stages and pending operations.')
    command('doctor', 'Check local archive and wiki consistency; not deployment parity.')
    trace = command('trace', 'Show the stage history of one operation.')
    trace.add_argument('operation_id')
    logs = command('logs', 'Read metadata-only processing events.')
    logs.add_argument('--operation', required=True)
    show = command('show', 'Read one archived record by ID.')
    show.add_argument('memory_id')
    forget = command('forget', 'Hide one archived record from search and list; its bytes stay on disk.')
    forget.add_argument('memory_id')
    listing = command('list', 'List archived records.')
    listing.add_argument('--limit', type=int, default=20, help='Page size, at most 500')
    listing.add_argument('--offset', type=int, default=0, help='Records to skip; page with the total it returns')
    search = command('search', 'Search the archive: hybrid vector and lexical retrieval.')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=10)
    search.add_argument('--project', help='Only records whose frontmatter project is this name')
    search.add_argument('--tag', action='append', dest='tags', metavar='TAG',
                        help='Only records carrying this tag; repeat to require several')
    search.add_argument('--since', help='Only records dated on or after this day (YYYY-MM-DD; no time or offset)')
    search.add_argument('--until', help='Only records dated on or before this day (YYYY-MM-DD), whole day included')
    worker = command('worker', 'Run the single local writer.')
    worker_actions = worker.add_subparsers(dest='action', required=True)
    daemon = worker_actions.add_parser('daemon', help='Poll work and optionally invoke a configured executor')
    common(daemon)
    daemon.add_argument('--executor-config', type=Path)
    daemon.add_argument('--once', action='store_true')
    server_profile(daemon)
    run = worker_actions.add_parser('run', help='Process queued operations or one operation ID')
    common(run)
    run.add_argument('operation_id', nargs='?')
    run.add_argument('--limit', type=int, default=20)
    server_profile(run)
    queue = command('queue', 'Inspect or retry durable operations.')
    queue_actions = queue.add_subparsers(dest='action', required=True)
    common(queue_actions.add_parser('list'))
    retry = queue_actions.add_parser('retry')
    common(retry)
    retry.add_argument('operation_id')
    server_profile(retry)
    config = command('config', 'Inspect the shared vault configuration.')
    config_actions = config.add_subparsers(dest='action', required=True)
    for action in ('show', 'validate'):
        common(config_actions.add_parser(action))
    models = command('models', 'Provision a verified local embedding model before runtime.')
    model_actions = models.add_subparsers(dest='action', required=True)
    model_import = model_actions.add_parser('import', help='Import the pinned model from local files')
    common(model_import)
    model_import.add_argument('--from', dest='source_directory', type=Path, required=True)
    listing = model_actions.add_parser('list', help='List the installed backend model catalog without downloads')
    common(listing)
    listing.add_argument('--filter', dest='query', help='Match a model name or description')
    common(model_actions.add_parser('status', help='Show the selected embedding profile'))
    selection = model_actions.add_parser('use', help='Select a verified model or a complete embedding profile')
    common(selection)
    selection.add_argument('model', nargs='?', help='Exact name from models list')
    selection.add_argument('--from', dest='source_directory', type=Path, help='Provisioned local model directory')
    selection.add_argument('--profile', type=Path, help='JSON object containing the embedding configuration')
    selection.add_argument('--pooling', choices=['mean', 'cls', 'max'])
    selection.add_argument('--normalization', choices=['l2', 'none'])
    selection.add_argument('--projection', default='none')
    selection.add_argument('--dimension', type=int)
    selection.add_argument('--query-prefix', default='')
    selection.add_argument('--document-prefix', default='')
    selection.add_argument('--dry-run', action='store_true', help='Verify and preview without changing configuration')
    core_run = commands.add_parser('core-run', help=argparse.SUPPRESS)
    core_run.add_argument('core_args', nargs=argparse.REMAINDER)
    core_spec = command('core-spec', 'Print the kelmod specification of the selected vault as JSON.')
    server_profile(core_spec)
    core_spec.add_argument('--runtime-dir', type=Path, help='Socket directory (default under TMPDIR)')
    core_spec.add_argument('--readers', type=int, default=4, help='Reader threads of the state core')
    core_spec.add_argument('--worker-config', type=Path, help='Executor configuration of the worker job')
    core_spec.add_argument('--no-schedule', action='store_true', help='Run jobs only on request')
    command('help', 'Discover the installed command contract, including JSON output.')
    command('version', 'Show the installed package version.')
    completion = command('completion', 'Generate shell completion for public commands.')
    completion.add_argument('shell', choices=['bash', 'zsh'])
    return parser, commands
