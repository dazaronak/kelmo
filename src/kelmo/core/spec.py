"""The kelmod specification of one root: the cores it starts, their lanes and budgets, the operation table.

`kelmo core-spec --vault V` prints it as JSON for `kelmod serve --spec`. A core starts with a stripped
environment, so every value it needs is named here (core/environment.py lists the `core-spec` variables).
"""
import json
import os
import sys
from pathlib import Path

from . import operations

LOCAL_ORIGIN = {'client_id': 'local', 'machine_tag': None, 'session': None}

# Lane: (in flight to the core, queued in kelmod, order). Round robin per origin, so one busy client cannot
# starve the others; the state core runs writes one at a time under its baton whatever the limit. Hooks
# have their own lane: a session waits on its SessionStart hook, which must not queue behind busy sessions' tools.
STATE_LANES = {
    'handler': (32, 512, 'rr'),
    'hook': (4, 256, 'rr'),
    'read': (4, 512, 'rr'),
    'write': (8, 512, 'rr'),
    'admin': (4, 64, 'fifo'),
}
STATE_BUDGET_MB = (1536, 3072)
ARCHIVE_LANES = {'write': (1, 512, 'fifo'), 'read': (4, 512, 'fifo')}
ARCHIVE_BUDGET_MB = (768, 1536)
EMBED_LANES = {'count': (8, 1024, 'fifo'), 'query': (2, 256, 'fifo'), 'passage': (2, 256, 'fifo')}
EMBED_BUDGET_MB = (450, 900)
PASSED_ENVIRONMENT = ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                      'HOME', 'PATH', 'LANG', 'TMPDIR')


def _lanes(table):
    return {name: {'limit': limit, 'queue': queue, 'order': order} for name, (limit, queue, order) in table.items()}


def _environment(source_root=None):
    passed = {name: os.environ[name] for name in PASSED_ENVIRONMENT if name in os.environ}
    if source_root is not None:
        passed['PYTHONPATH'] = str(source_root)
    return passed


def installed_core_command(launcher):
    """How the installed launcher's runtime starts a core: the same isolated interpreter and entry script the
    launcher itself runs (`-I -S` imports only from the installation), through the hidden `core-run` verb; the
    core's name follows."""
    prefix = Path(launcher).resolve().parent.parent
    return [sys.executable, '-I', '-S', '-X', 'utf8=1', str(prefix / 'share/kelmo/entry.py'), 'core-run', '--']


def embed_inputs(settings):
    """(tokenizer.json path, special-token additions, preparation digest) of the vault's embedding settings,
    exactly the input the archive's TokenWindow is built from; None without embedding settings. Read from
    the pinned and declared files only: no model is loaded and no tokenizer parsed."""
    if settings is None:
        return None
    from ..embeddings import declared_policy, provider_from_settings
    if settings['provider'] == 'broker':
        policy, additions = declared_policy(settings)
        return str(Path(settings['tokenizer']).expanduser().resolve()), additions, policy.preparation
    provider = provider_from_settings(settings)
    provider._read_policy('unused')  # fills provider._special from the pinned policy files
    from kelmo.store.memory.embed import preparation_digest
    tokenizer = (Path(settings['model_dir']).expanduser() / 'tokenizer.json').resolve()
    provider._pinned('tokenizer.json')  # refuses bytes that are not the pinned ones
    return str(tokenizer), provider._special, preparation_digest(provider._special)


def http_token(root):
    """The bearer token of the root's HTTP MCP endpoint: 256 random bits in `.state/http-token` (0600),
    created on first use. Clients registered with the endpoint carry it."""
    return http_token_at(Path(root) / '.state')


def http_token_at(state, value=None):
    """`<state>/http-token`, written whole by link from a private temporary file, so no reader (kelmod
    included) ever sees an empty or partial token. A token file shorter than a token is replaced."""
    import secrets
    path = Path(state) / 'http-token'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The token never reaches a git repository the state directory may sit in.
    ignore = path.parent / '.gitignore'
    if not ignore.exists():
        ignore.write_text('*\n', encoding='utf-8')
    try:
        if len(path.read_bytes().strip()) >= 64:
            return path
        path.unlink()
    except FileNotFoundError:
        pass
    temporary = path.with_name(f'.http-token.{os.getpid()}.{secrets.token_hex(4)}')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write((value or secrets.token_hex(32)) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)  # fails if another writer won: its token stands
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return path


def tool_definitions():
    """The MCP tool definitions as `list_tools` answers them; the HTTP endpoint serves these."""
    import asyncio

    from ..mcp import server
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(server('/nonexistent').list_tools())
    else:
        # Called from inside an event loop: list the tools on a thread of its own.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as pool:
            tools = pool.submit(asyncio.run, server('/nonexistent').list_tools()).result()
    return sorted((tool.to_mcp_tool().model_dump(mode='json', exclude_none=True) for tool in tools),
                  key=lambda tool: tool['name'])


def _command(core_command, executable, name):
    if core_command is not None:
        return [*core_command, name]
    return [executable or sys.executable, '-m', f'kelmo.core.{name}_core']


def build(root, *, server_profile=None, runtime_dir=None, executable=None, readers=4, source_root=None,
          core_command=None, schedule=False, worker_config=None, archive_core=False, archive_in_process=False,
          embed_core=False, kelmod=None, http_port=None):
    """The spec of the daemon that owns `root`. `core_command` is the argv prefix that runs a core by name
    (default: this interpreter with `-m kelmo.core.<name>_core`); `source_root` puts a source tree on the cores'
    PYTHONPATH (tests and a development checkout). `archive_core` starts the archive as its own supervised core
    (confined unless `archive_in_process`). `http_port` serves the HTTP MCP endpoint on 127.0.0.1 at that port
    (0 picks a free one, published in the runtime directory's endpoint.json)."""
    if http_port is not None and server_profile is not None:
        # The endpoint speaks for the local origin, which a server profile serves only when it enrolls it.
        from ..errors import KelmoError
        raise KelmoError('E_CONFIG', 'A server daemon serves remote clients over ssh, not the HTTP endpoint.')
    root = Path(root).expanduser().resolve()
    argv = [*_command(core_command, executable, 'state'), '--vault', str(root), '--readers', str(readers)]
    if server_profile is not None:
        argv += ['--server-profile', str(Path(server_profile).expanduser().resolve())]
    if schedule:
        argv.append('--schedule')
    if worker_config is not None:
        argv += ['--worker-config', str(Path(worker_config).expanduser().resolve())]
    if archive_core:
        argv.append('--archive-core')
    lanes = dict(STATE_LANES, read=(readers, STATE_LANES['read'][1], STATE_LANES['read'][2]))
    state = {'name': 'state', 'argv': argv, 'env': _environment(source_root), 'lanes': _lanes(lanes),
             'budget': {'soft_mb': STATE_BUDGET_MB[0], 'hard_mb': STATE_BUDGET_MB[1]}}
    cores, served = [state], [(op, lanes) for op in operations.table() if op.name.startswith('state.')]
    if archive_core:
        archive_argv = [*_command(core_command, executable, 'archive'), '--vault', str(root)]
        if server_profile is not None:
            archive_argv += ['--server-profile', str(Path(server_profile).expanduser().resolve())]
        if archive_in_process:
            archive_argv.append('--in-process')
        inputs = None
        if embed_core:
            from ..config import Config
            inputs = embed_inputs(Config.load(root).embedding)
        if inputs is not None:
            archive_argv.append('--embed-core')
            tokenizer, additions, preparation = inputs
            cores.append({'name': 'embed', 'env': {}, 'lanes': _lanes(EMBED_LANES),
                          'argv': [str(kelmod or _kelmod()), 'embed', '--tokenizer', tokenizer,
                                   '--additions', json.dumps(additions), '--preparation', preparation],
                          'budget': {'soft_mb': EMBED_BUDGET_MB[0], 'hard_mb': EMBED_BUDGET_MB[1]}})
            served += [(op, EMBED_LANES) for op in operations.table() if op.name.startswith('embed.')]
        cores.append({'name': 'archive', 'argv': archive_argv, 'env': _environment(source_root),
                      'lanes': _lanes(ARCHIVE_LANES),
                      'budget': {'soft_mb': ARCHIVE_BUDGET_MB[0], 'hard_mb': ARCHIVE_BUDGET_MB[1]}})
        served += [(op, ARCHIVE_LANES) for op in operations.table() if op.name.startswith('archive.')]
    spec = {'root': str(root), 'origin': LOCAL_ORIGIN, 'cores': cores,
            'meta': {'role': 'vault', 'server_profile': None if server_profile is None
                     else str(Path(server_profile).expanduser().resolve())},
            'operations': [{'name': op.name, 'lanes': [lane for lane in op.lanes if lane in table],
                            'replay': op.replay} for op, table in served]}
    if runtime_dir is not None:
        spec['runtime_dir'] = str(runtime_dir)
    if http_port is not None:
        spec['http'] = {'port': int(http_port), 'token_file': str(http_token(root)), 'tools': tool_definitions(),
                        'tool_op': 'state.tool'}
    return spec



EDGE_LANES = {'handler': (32, 512, 'rr'), 'hook': (4, 256, 'rr')}
# Soft: the steady edge core. Hard: room for one transcript snapshot of a long session (the outbox holds it in
# 8 MiB pieces) without a restart loop; the per-piece streaming that would let it shrink is not built yet.
EDGE_BUDGET_MB = (150, 512)


def build_edge(root, *, runtime_dir=None, executable=None, source_root=None, core_command=None, http_port=None):
    """The spec of the daemon on an edge machine: one edge core that owns the client root's outbox and caches
    and reaches the authority through the profile's channel. Sessions and hooks call `edge.*`."""
    from ..config import ClientProfile
    root = Path(root).expanduser().resolve()
    profile = ClientProfile.load(root)
    argv = [*_command(core_command, executable, 'edge'), '--client', str(root)]
    edge = {'name': 'edge', 'argv': argv, 'env': _environment(source_root), 'lanes': _lanes(EDGE_LANES),
            'budget': {'soft_mb': EDGE_BUDGET_MB[0], 'hard_mb': EDGE_BUDGET_MB[1]}}
    served = [op for op in operations.table() if op.name.startswith('edge.')]
    spec = {'root': str(root), 'state_dir': str(profile.state), 'origin': LOCAL_ORIGIN, 'cores': [edge],
            'meta': {'role': 'edge', 'server_profile': None},
            'operations': [{'name': op.name, 'lanes': [lane for lane in op.lanes if lane in EDGE_LANES],
                            'replay': op.replay} for op in served]}
    if runtime_dir is not None:
        spec['runtime_dir'] = str(runtime_dir)
    if http_port is not None:
        spec['http'] = {'port': int(http_port), 'token_file': str(http_token_at(profile.state)),
                        'tools': tool_definitions(), 'tool_op': 'edge.tool'}
    return spec


def _kelmod():
    """The kelmod binary beside this interpreter (the wheel installs both in one scripts directory)."""
    from .daemon_client import kelmod_executable
    return kelmod_executable()
