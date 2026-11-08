#!/usr/bin/env python3
"""Install a private Python runtime, then configure the selected user profile."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import venv


def constraint_dependencies(metadata):
    try:
        constraints = metadata['tool']['uv']['constraint-dependencies']
    except (KeyError, TypeError):
        raise ValueError('pyproject.toml must define tool.uv.constraint-dependencies.') from None
    if not isinstance(constraints, list) or not constraints or not all(
            isinstance(value, str) and value.strip() for value in constraints):
        raise ValueError('tool.uv.constraint-dependencies must be a nonempty string list.')
    return constraints


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/kelmo/runtime')
    parser.add_argument('--vault', type=Path, default=Path.home() / '.local/share/kelmo/default')
    parser.add_argument('--wiki', type=Path)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--claude-profile', type=Path)
    parser.add_argument('--codex-profile', type=Path)
    parser.add_argument('--claude-mcp-config', type=Path)
    parser.add_argument('--project', default='default')
    parser.add_argument('--git-name')
    parser.add_argument('--git-email')
    parser.add_argument('--shared-authority')
    parser.add_argument('--shared-command-json')
    parser.add_argument('--model-source', type=Path)
    parser.add_argument('--embedding-profile', type=Path)
    parser.add_argument('--skip-model', action='store_true')
    parser.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    parser.add_argument('--server-profile', type=Path)
    parser.add_argument('--bwrap-source', type=Path)
    parser.add_argument('--bwrap-sha256')
    parser.add_argument('--executor-config', type=Path)
    parser.add_argument('--provider', choices=['claude', 'codex', 'custom'])
    parser.add_argument('--provider-executable', type=Path)
    parser.add_argument('--provider-home', type=Path)
    parser.add_argument('--provider-command-json')
    parser.add_argument('--model')
    parser.add_argument('--client', choices=['claude', 'codex', 'both', 'none'], default='both')
    policy = parser.add_mutually_exclusive_group(required=True)
    policy.add_argument('--disable-native-memory', action='store_true')
    policy.add_argument('--allow-native-memory', action='store_true')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Preview installation without changing files or invoking providers')
    parser.add_argument('--session-memory',choices=['auto','archive-only','off'])
    parser.add_argument('--extractor-provider',choices=['claude','codex'])
    parser.add_argument('--extractor-model')
    args = parser.parse_args()
    if bool(args.extractor_provider)!=bool(args.extractor_model):
        parser.error('Extractor provider/model options must be selected as complete pairs.')
    if args.executor_config and args.provider:
        parser.error('Select a custom executor or provider/model, not both.')
    if sys.platform not in ('linux', 'darwin') or not (3, 12) <= sys.version_info[:2] < (3, 14):
        parser.error('Use Python 3.12 or 3.13 on macOS or Linux.')
    root = Path(__file__).resolve().parent
    try:
        metadata = tomllib.loads((root / 'pyproject.toml').read_text())
        constraints_values = constraint_dependencies(metadata)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        parser.error(str(error))
    prefix = args.prefix.expanduser().absolute()
    if any(p.is_symlink() for p in (prefix, *prefix.parents)):
        parser.error('Runtime prefix cannot contain symlinks.')
    marker = prefix / '.kelmo-runtime'
    if prefix.exists() and not prefix.is_dir():
        parser.error('Runtime prefix must be a directory.')
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        parser.error('Runtime ownership marker must be a regular file.')
    if marker.is_file():
        try:
            info = marker.stat()
            valid_marker = (info.st_nlink == 1 and info.st_size == len(b'kelmo\n')
                            and marker.read_bytes() == b'kelmo\n')
        except OSError:
            parser.error('Runtime ownership marker is unreadable.')
        if not valid_marker:
            parser.error('Runtime ownership marker is not a private kelmo marker.')
    if prefix.exists() and any(prefix.iterdir()) and not marker.is_file():
        parser.error('Refusing to adopt a nonempty unmanaged runtime directory.')
    sys.path.insert(0,str(root/'src'))
    from kelmo.errors import KelmoError
    try:
        from kelmo.install.preview import preview
        from kelmo.install.sandbox import provision
        from kelmo.install.runtime_backup import backup_runtime
        sandbox_plan = provision(prefix, args.bwrap_source, args.bwrap_sha256)
        args.executable = prefix / 'bin/kelmo'
        result = preview(args)
        if args.dry_run:
            result['runtime'] = {'path': str(prefix), 'action': 'upgrade' if marker.is_file() else 'create',
                                 'dependency_installation': 'on_apply',
                                 'backup_before_upgrade': marker.is_file()}
            if sandbox_plan:
                result['runtime']['sandbox'] = sandbox_plan
            print(json.dumps({'schema_version': 1, 'ok': True, 'result': result}, indent=2))
            return 0
        from kelmo.install.providers import arguments as provider_arguments
        provider_arguments(args, args.executable, check_provider=True)
        from kelmo.install.shared import selected as shared_selected, prepare as prepare_shared
        if shared_selected(args):
            prepare_shared(args)
        else:
            from kelmo.install.session_setup import prepare as prepare_sessions
            from kelmo.install.setup import prepare_clients, profile_options
            home=args.home.expanduser().resolve()
            vault=args.vault.expanduser().resolve()
            session_plan=prepare_sessions(args,home,vault)
            prepare_clients(home,prefix/'bin/kelmo',vault,args.client,args.disable_native_memory,
                            session_plan['settings']['mode'], **profile_options(args))
    except (KelmoError,OSError,ValueError) as error:
        parser.error(str(error))
    finally:
        sys.path.pop(0)
    runtime_backup = None
    if marker.is_file():
        try:
            runtime_backup = backup_runtime(prefix)
        except (KelmoError, OSError, ValueError) as error:
            parser.error(str(error))
        print('Verified runtime backup: ' + runtime_backup['path'], file=sys.stderr)
    prefix.mkdir(parents=True, exist_ok=True)
    if not marker.is_file():
        with marker.open('x') as output:
            output.write('kelmo\n')
    for name in ('python', 'python3', 'python%d.%d' % sys.version_info[:2]):
        # A running worker or MCP server still executes the old interpreter, and writing over a running
        # binary fails (ETXTBSY): remove it so the new runtime gets a fresh file, the old one lives on.
        (prefix / 'bin' / name).unlink(missing_ok=True)
    venv.EnvBuilder(with_pip=True).create(prefix)
    fd, log_name = tempfile.mkstemp(prefix='kelmo-install-', suffix='.log')
    with os.fdopen(fd, 'wb') as log, tempfile.TemporaryDirectory() as temporary:
        constraints = Path(temporary) / 'constraints.txt'
        constraints.write_text('\n'.join(constraints_values) + '\n')
        result = subprocess.run([str(prefix / 'bin/python'), '-m', 'pip', 'install',
                                 '--constraint', str(constraints), str(root)], stdout=log, stderr=log)
    if result.returncode:
        print(json.dumps({'schema_version': 1, 'ok': False, 'error': {
            'code': 'E_DEPENDENCIES',
            'message': 'Dependency installation failed; inspect the retained pip log.',
            'stage': 'dependencies', 'log': log_name,
            'runtime_backup': runtime_backup,
        }}))
        return 1
    Path(log_name).unlink(missing_ok=True)
    if sandbox_plan:
        try:
            sandbox = provision(prefix, args.bwrap_source, args.bwrap_sha256, apply=True)
        except (KelmoError, OSError, ValueError) as error:
            parser.error(str(error))
        print('Sandbox launcher: ' + sandbox['path'], file=sys.stderr)
        if sandbox.get('backup'):
            print('Sandbox backup: ' + sandbox['backup'], file=sys.stderr)
    executable = prefix / 'bin/kelmo'
    command = [str(executable), 'setup', '--vault', str(args.vault), '--home', str(args.home),
               '--executable', str(executable), '--client', args.client,
               '--disable-native-memory' if args.disable_native_memory else '--allow-native-memory', '--json']
    if args.wiki:
        command += ['--wiki', str(args.wiki)]
    if args.model_source:
        command += ['--model-source', str(args.model_source)]
    if args.executor_config:
        command += ['--executor-config', str(args.executor_config)]
    if args.provider:
        command += ['--provider', args.provider]
    if args.model:
        command += ['--model', args.model]
    if args.no_start:
        command.append('--no-start')
    if args.skip_model:
        command.append('--skip-model')
    for key in ('session_memory','extractor_provider','extractor_model','shared_authority','shared_command_json',
                'project', 'git_name', 'git_email', 'effort', 'embedding_profile', 'server_profile',
                'claude_profile', 'codex_profile', 'claude_mcp_config',
                'provider_executable', 'provider_command_json', 'provider_home'):
        value=getattr(args,key)
        if value is not None:
            command+=['--'+key.replace('_','-'),str(value)]
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
