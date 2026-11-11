"""Read-only installation plan; no providers, model downloads or storage initialization."""
from pathlib import Path
import re
import shlex
import shutil
import sys

from ..config import Config
from ..errors import KelmoError
from ..wiki.directory import has_content
from .instructions import normalize_binding, render_instructions
from .removal import _read
from .session_setup import prepare as prepare_sessions
from .setup import local_daemon, model_plan, prepare_clients, profile_options, read_json, service_definition


def _change(path, prior, desired):
    return {'path': str(path), 'action': 'create' if prior is None else
            'unchanged' if prior == desired else 'update',
            'backup_on_apply': prior is not None and prior != desired}


def _wiki(args, vault):
    selected = getattr(args, 'wiki', None)
    if selected is not None:
        selected = Path(selected).expanduser().absolute()
        if any(p.is_symlink() for p in (selected, *selected.parents)):
            raise KelmoError('E_CONFIG', 'Selected wiki path cannot contain symlinks.')
        selected = selected.resolve()
        if selected == vault or selected.is_relative_to(vault) or vault.is_relative_to(selected):
            raise KelmoError('E_CONFIG', 'An explicit wiki root must be separate from the data root.')
        if selected.exists() and not selected.is_dir():
            raise KelmoError('E_INPUT', 'Wiki must be a directory.')
    if (vault / 'kelmo.toml').exists():
        config = Config.load(vault)
        if selected is not None and selected not in (config.wiki, config.knowledge_git_root):
            raise KelmoError('E_CONFIG', 'The initialized vault uses a different wiki directory.')
        return config.wiki, 'retain', config.hot is not None
    if vault.exists() and (not vault.is_dir() or any(vault.iterdir())):
        raise KelmoError('E_ROOT_NOT_EMPTY', 'Destination is not empty; initialization will not adopt existing data.')
    if selected is not None and has_content(selected):
        raise KelmoError('E_INPUT', 'Select an empty wiki directory; a new vault does not take over '
                         'existing pages.')
    # A selected directory is the knowledge repository; its pages live under knowledge/wiki.
    wiki = vault / 'knowledge/wiki' if selected is None else selected / 'knowledge/wiki'
    return wiki, 'initialize', False


def preview(args, *, linked=False):
    from .shared import selected as shared_selected, setup as shared_setup
    if shared_selected(args):
        from copy import copy
        preview_args = copy(args)
        preview_args.dry_run = True
        return shared_setup(preview_args)
    if sys.platform not in ('darwin', 'linux'):
        raise KelmoError('E_CONFIG', 'Installation supports macOS and Linux.')
    from ..initialization import validate
    validate(getattr(args, 'project', 'default'), getattr(args, 'git_name', None),
             getattr(args, 'git_email', None))
    from .providers import arguments as provider_arguments
    provider_arguments(args, args.executable)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', getattr(args, 'label', 'local')):
        raise KelmoError('E_INPUT', 'Node label must use letters, digits, underscores or hyphens.')
    home = Path(args.home).expanduser().resolve()
    vault = Path(normalize_binding(args.vault)).resolve()
    executable = Path(normalize_binding(args.executable))
    wiki, wiki_action, hot = _wiki(args, vault)
    model_action = model_plan(args, vault)
    server_profile = getattr(args, 'server_profile', None)
    if server_profile:
        from ..server import ServerProfile
        profile = ServerProfile.load(server_profile)
        if profile.bindings.authority_id != Config.load(vault).vault_id:
            raise KelmoError('E_AUTHORITY', 'The server profile belongs to another vault.')
        if profile.fenced and not args.no_start:
            raise KelmoError('E_FENCED', 'The server is fenced; prepare with --no-start before cutover.')
    custom = Path(args.executor_config).expanduser().resolve() if args.executor_config else None
    if custom is not None:
        from ..daemon import validate_config
        validate_config(read_json(custom, required=True))
    sessions = prepare_sessions(args, home, vault, check_provider=False)
    daemon = local_daemon(args, executable, vault, server_profile)
    plan = prepare_clients(home, executable, vault, args.client,
                           args.disable_native_memory, sessions['settings']['mode'],
                           edge=daemon if not server_profile else None, **profile_options(args))
    changes = []
    binding = shlex.join([str(executable), '--vault', str(vault)])
    for name, item in plan['prepared'].items():
        instructions = item['root'] / ('CLAUDE.md' if name == 'claude' else 'AGENTS.md')
        prior = _read(instructions)
        desired = render_instructions(prior or b'', disable_native=args.disable_native_memory,
                                      command=binding)
        changes.append(_change(instructions, prior, desired))
        for path, prior, desired in (
                (item['path'], item['prior'], item['config_data']),
                (item['hook_path'], item['hook_prior'], item['hook_data']),
                (item['ownership_path'], item['ownership_prior'], item['ownership_data'])):
            if path is not None:
                changes.append(_change(path, prior, desired))
    state = Config.load(vault).state if (vault / 'kelmo.toml').is_file() else vault / '.state'
    executor_config = state / 'executor.json' if args.provider else custom
    if daemon is not None:
        from .shared import service_definition as daemon_service_definition
        service, data = daemon_service_definition(home, executable, vault, sys.platform)
    else:
        service, data = service_definition(home, executable, vault, sys.platform, executor_config, server_profile)
    changes.append(_change(service, _read(service), data))
    providers = set(filter(None, (args.provider, getattr(args, 'extractor_provider', None))))
    warnings = []
    for provider in sorted(providers):
        if provider != 'custom' and shutil.which(provider) is None:
            warnings.append(f'{provider} CLI is not on PATH; install and authenticate it before applying setup.')
    add_project = 'Link a repository with kelmo link --project NAME DIR; kelmo project add NAME creates the project first.'
    if not hot and not linked:
        warnings.append('No repository is linked to a project yet; a session receives no project pages until one is.')
    if not args.provider and custom is None:
        warnings.append('No resolution executor selected; background fact resolution will remain pending.')
    return {'dry_run': True, 'complete': False, 'clients': plan['clients'],
            'executable': str(executable), 'vault': str(vault),
            'wiki': {'path': str(wiki), 'action': wiki_action, 'existing_content_preserved': True},
            'files': changes, 'model': {'action': model_action, 'verification': 'on_apply'},
            'session_memory_mode': sessions['settings']['mode'],
            'service': {'path': str(service), 'action': 'prepare' if args.no_start else 'activate'},
            'provider_calls': 'on_apply' if providers or custom else 'none_selected',
            'warnings': warnings,
            'deferred_checks': ['model_integrity', 'archive_and_wiki_health', 'wiki_ownership',
                                'executor_readiness', 'native_hook_trust_and_delivery', 'service_activation'],
            'client_restart_required_on_apply': True,
            'native_memory_prohibited': bool(args.disable_native_memory),
            'native_memory_disabled': False,
            'next_steps': ['Apply the same command without --dry-run after reviewing this plan.',
                           'Restart selected clients and approve native hook trust if requested.',
                           *([] if hot or linked else [add_project])]}
