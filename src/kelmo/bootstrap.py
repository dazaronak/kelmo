"""Trusted bootstrap, invoked by the shell launcher with Python -I -S."""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import IsolationError, RuntimePaths, command, environment, validate_stdio
from interface import UsageError, build_parser, vault_from_environment
from model_store import FILES, ModelImportError, source_paths
from errors import KelmoError
from config import Config


def system_paths(python):
    if sys.platform == 'linux':
        return [Path('/usr/lib'), Path('/usr/lib64'), Path('/usr/share/git-core'),
                Path('/usr/share/zoneinfo'), python]
    return [Path(sys.base_prefix), Path('/System/Library'), Path('/usr/lib'),
            *(Path('/opt/homebrew/Cellar') / name for name in
              ('openssl@3', 'sqlite', 'mpdecimal', 'xz'))]


def execute(paths, child, env, scratch, *, capture_output=False, model_files=()):
    if sys.platform == 'linux':
        from linux_runtime import execute as linux_execute
        return linux_execute(paths, child, env=env, cwd=scratch,
                             capture_output=capture_output, model_files=model_files)
    return subprocess.run(command(paths, child), env=env, cwd=scratch,
                          close_fds=True, check=False, text=True,
                          capture_output=capture_output)


def normalize_option(argv, name, value):
    result = []
    iterator = iter(argv)
    for arg in iterator:
        if arg == '--':
            result.extend([arg, *iterator])
            break
        if arg == name:
            next(iterator)
            result.extend([name, str(value)])
        elif arg.startswith(name + '='):
            result.append(name + '=' + str(value))
        else:
            result.append(arg)
    return result


def main():
    argv = sys.argv[1:]
    try:
        if not sys.flags.isolated or not sys.flags.no_site:
            raise IsolationError('The runtime bootstrap requires isolated Python without site startup.')
        validate_stdio()
        if sys.platform not in ('darwin', 'linux'):
            raise IsolationError('This platform has no verified runtime isolation adapter yet.')
        parser, _ = build_parser('')
        try:
            with redirect_stdout(io.StringIO()):
                selected = parser.parse_args(argv)
        except SystemExit as error:
            if error.code:
                raise IsolationError('Invalid command arguments.', 'E_USAGE') from error
            selected = argparse.Namespace(vault=None, command=None)
        main_command = selected.command
        default = vault_from_environment() or Path.home() / '.local/share/kelmo/default'
        if (main_command == 'init' and sys.stdin.isatty()
                and not selected.json and not selected.non_interactive):
            if selected.vault is None:
                selected.vault = Path(input(f'Vault directory [{default}]: ').strip() or default)
            if not selected.label:
                selected.label = input('Node label: ').strip()
                argv.extend(['--label', selected.label])
        root = (selected.vault or default).expanduser().resolve()
        argv = normalize_option(argv, '--vault', root)
        prefix = Path(__file__).resolve().parents[2]
        python = Path(sys.executable).resolve()
        if not python.is_file():
            raise IsolationError('The trusted Python interpreter is missing.')
        if root.is_relative_to(prefix) or prefix.is_relative_to(root):
            raise IsolationError('Vault and installed runtime must not overlap.')
        if main_command == 'models':
            return provision(prefix, python, root, selected)
        if sys.platform == 'linux':
            git = Path('/usr/bin/git').resolve()
        else:
            git_result = subprocess.run(['/usr/bin/xcrun', '--find', 'git'],
                                        env={'PATH': '/usr/bin:/bin'}, capture_output=True,
                                        text=True, timeout=10, check=False)
            if git_result.returncode:
                raise IsolationError('A trusted local Git executable is required.')
            git = Path(git_result.stdout.strip()).resolve()
        if not git.is_file():
            raise IsolationError('The local Git executable is unavailable.')
        system_roots = system_paths(python)
        if sys.platform == 'darwin':
            system_roots.append(git.parent.parent)
        read_only = [prefix, *(p.resolve() for p in system_roots if p.exists())]
        executable = python.resolve()
        executables = [executable, git]
        app = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
        if app.is_file():
            executables.append(app.resolve())
        source_arg = getattr(selected, 'file', None)
        if source_arg and source_arg != '-':
            source = Path(source_arg).expanduser()
            if source.is_symlink() or any(p == '.env' or p.startswith('.env.') for p in source.resolve().parts):
                raise IsolationError('This source path is not permitted.')
            source = source.resolve()
            if not source.is_file() or source.suffix != '.md':
                raise IsolationError('Source must be a regular Markdown file.')
            data_roots = [root / part for part in ('.state', 'archive', 'knowledge', '.git')]
            if not any(source.is_relative_to(data) for data in data_roots):
                read_only.append(source)
            argv = normalize_option(argv, '--file', source)
        with tempfile.TemporaryDirectory(prefix='kelmo-runtime-') as temporary:
            scratch = Path(temporary).resolve()
            env = environment(scratch, [git.parent, python.parent])
            for key, field in [('GIT_AUTHOR_NAME', 'user.name'), ('GIT_AUTHOR_EMAIL', 'user.email')]:
                value = subprocess.run([str(git), 'config', '--global', '--no-includes', '--get', field],
                                       env={'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin'},
                                       cwd='/', capture_output=True, text=True, timeout=10, check=False)
                if value.returncode == 0:
                    env[key] = value.stdout.strip()
                    env[key.replace('AUTHOR', 'COMMITTER')] = value.stdout.strip()
            if main_command == 'init' and not (root / 'kelmo.toml').is_file() and not all(
                    env.get(key) for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL')):
                raise IsolationError('Configure Git user.name and user.email before writing wiki commits.', 'E_GIT_IDENTITY')
            writable = [scratch, *(root / part for part in ('.state', 'archive', 'knowledge', '.git'))]
            vault_read_only = [root / 'kelmo.toml', root / '.cache/embeddings']
            initialized = (root / 'kelmo.toml').is_file()
            if main_command == 'init' and not initialized:
                if root.exists() and (not root.is_dir() or any(root.iterdir())):
                    raise IsolationError('Initialization requires an empty destination.', 'E_ROOT_NOT_EMPTY')
                root.parent.mkdir(parents=True, exist_ok=True)
                if sys.platform == 'linux':
                    return initialize_linux(read_only, executables, python, prefix,
                                            root, argv, env, scratch)
                with tempfile.TemporaryDirectory(prefix='.kelmo-staging-', dir=root.parent) as staging:
                    env['KELMO_INIT_PARENT'] = staging
                    init_paths = RuntimePaths(tuple(read_only),
                                              (scratch, root, Path(staging).resolve()), tuple(executables),
                                              sync_dirs=(root.parent,))
                    return run(init_paths, python, prefix, root, argv, env, scratch)
            paths = RuntimePaths(tuple([*read_only, *vault_read_only]), tuple(writable), tuple(executables),
                                 (root, root / '.gitignore'))
            return run(paths, python, prefix, root, argv, env, scratch)
    except (IsolationError, KelmoError, ModelImportError, UsageError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, IsolationError) else 'Runtime isolation could not be established.'
        code = error.code if isinstance(error, (IsolationError, KelmoError)) else 'E_MODEL_IMPORT' if isinstance(error, ModelImportError) else 'E_USAGE' if isinstance(error, UsageError) else 'E_IO' if isinstance(error, OSError) else 'E_ISOLATION'
        if isinstance(error, (KelmoError, UsageError, ModelImportError)):
            message = str(error)
        elif code == 'E_IO':
            message = 'A required local file could not be accessed.'
        if '--json' in argv:
            print(json.dumps({'schema_version': 1, 'ok': False,
                              'error': {'code': code, 'message': message}}))
        else:
            print(code + ': ' + message, file=sys.stderr)
        return 2 if code == 'E_USAGE' else 1


def run(paths, python, prefix, root, argv, env, scratch):
    child = [str(python), '-I', '-S', str(prefix / 'share/kelmo/entry.py'),
             '--vault', str(root), *argv]
    result = execute(paths, child, env, scratch)
    return result.returncode


def initialize_linux(read_only, executables, python, prefix, root, argv, env, scratch):
    # Namespace mounts cannot atomically rename their own mountpoint. Build
    # under an isolated sibling and publish from the trusted parent process.
    with tempfile.TemporaryDirectory(prefix='.kelmo-staging-', dir=root.parent) as directory:
        staging = Path(directory).resolve()
        staged_root = staging / 'vault'
        paths = RuntimePaths(tuple(read_only), (scratch, staging), tuple(executables))
        child = [str(python), '-I', '-S', str(prefix / 'share/kelmo/entry.py'),
                 *normalize_option(argv, '--vault', staged_root)]
        if '--vault' not in child and not any(a.startswith('--vault=') for a in child):
            child[4:4] = ['--vault', str(staged_root)]
        result = execute(paths, child, env, scratch, capture_output=True)
        if result.returncode == 0:
            Config.load(staged_root)
            staged_root.replace(root)
            descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        print(result.stdout.replace(str(staged_root), str(root)), end='')
        if result.stderr:
            print(result.stderr, file=sys.stderr, end='')
        return result.returncode


def provision(prefix, python, root, selected):
    source = selected.source_directory.expanduser().resolve()
    try:
        files, directories = source_paths(source)
    except OSError as error:
        raise ModelImportError('Model source paths could not be inspected.') from error
    system_roots = system_paths(python)
    read_only = [prefix, *(path.resolve() for path in system_roots if path.exists())]
    read_only.extend(path for path in files if not path.is_relative_to(root / '.cache'))
    model_files = ()
    if sys.platform == 'linux':
        if any(path.is_relative_to(root / '.cache') for path in files):
            raise ModelImportError('Import from a model source outside the destination cache.')
        model_files = tuple(zip(FILES, files, strict=True))
    executables = [python.resolve()]
    app = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    if app.is_file():
        executables.append(app.resolve())
    with tempfile.TemporaryDirectory(prefix='kelmo-provision-') as temporary:
        scratch = Path(temporary).resolve()
        paths = RuntimePaths(tuple(read_only), (scratch, root / '.cache'), tuple(executables),
                             tuple([*directories, root / 'kelmo.toml']), (root,))
        child = [str(python), '-I', '-S', str(prefix / 'share/kelmo/model_store.py'),
                 '--vault', str(root), '--source',
                 '/kelmo-model-input' if model_files else str(source)]
        if selected.json:
            child.append('--json')
        result = execute(paths, child, environment(scratch, [python.parent]), scratch,
                         model_files=model_files)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
