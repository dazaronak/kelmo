"""Ask at a terminal which model processes notes, when `kelmo setup` was given no provider."""
import os
import re
import subprocess
import shutil
import sys
from pathlib import Path

from ..errors import KelmoError

# provider, label, account variable, default account directory, files that mark another account directory
PROVIDERS = (
    ('claude', 'Claude Code', 'CLAUDE_CONFIG_DIR', '.claude', ('.claude.json', 'settings.json')),
    ('codex', 'Codex', 'CODEX_HOME', '.codex', ('auth.json', 'config.toml')),
)
SUGGESTED_MODEL = {'claude': 'claude-haiku-4-5'}


def running_executable():
    """The kelmo command beside the running interpreter: a pip, pipx or uv tool install, or install.py's runtime."""
    candidate = Path(sys.executable).parent / 'kelmo'
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise KelmoError('E_CONFIG', 'Cannot find the installed kelmo command; pass --executable.')


def wanted(args):
    """Ask only a person at a terminal who chose nothing: scripts keep the flag contract."""
    return (getattr(args, 'provider', None) is None and getattr(args, 'executor_config', None) is None
            and not getattr(args, 'non_interactive', False) and sys.stdin.isatty() and sys.stderr.isatty())


def accounts(home, default, markers):
    """The default account directory first, then every other `<default>-*` directory that looks like one."""
    found = [None]
    for path in sorted(Path(home).glob(default + '-*')):
        if path.is_dir() and any((path / marker).exists() for marker in markers):
            found.append(path)
    return found


def ask_terminal(prompt):
    """Prompt on stderr and read one line, so stdout stays the command's JSON answer."""
    print(prompt, end='', file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise KelmoError('E_INPUT', 'Setup was interrupted before an answer.')
    return line.rstrip('\n')


def choose(args, *, ask=ask_terminal, out=sys.stderr, which=shutil.which):
    """Fill args.provider, args.model and args.provider_home from answers; leave them unset to skip."""
    home = Path(getattr(args, 'home', None) or Path.home()).expanduser()

    def pick(question, options, default=1):
        for number, label in enumerate(options, 1):
            print(f'  {number}. {label}', file=out)
        while True:
            answer = ask(f'{question} [{default}]: ').strip() or str(default)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            print(f'  Enter a number from 1 to {len(options)}.', file=out)

    installed = [item for item in PROVIDERS if which(item[0])]
    print('\nKelmo\'s worker sends each note to a model that updates your wiki. Those calls are billed\n'
          'to the subscription or API key of the account you choose here.', file=out)
    if not installed:
        print('Neither Claude Code (claude) nor Codex (codex) is on PATH. Install and log in to one, or use\n'
              '--provider custom for your own executor (docs/EXECUTORS.md).', file=out)
    labels = [f'{label} ({name})' for name, label, *_ in installed]
    labels.append('Skip for now: notes are archived and wait until an executor is configured')
    index = pick('Which model should process notes?', labels)
    if index == len(installed):
        return False
    name, label, variable, default, markers = installed[index]
    suggested = SUGGESTED_MODEL.get(name)
    while True:
        model = ask(f'{label} model' + (f' [{suggested}]' if suggested else '') + ': ').strip() or suggested
        if model:
            break
        print('  Enter the model name your account can use.', file=out)
    found = accounts(home, default, markers)
    chosen = None
    if len(found) > 1:
        options = [f'~/{default} (default login)'] + [f'{path} ({variable})' for path in found[1:]]
        chosen = found[pick(f'Which {label} account?', options)]
    args.provider, args.model, args.provider_home = name, model, chosen
    return True


def repository_root(cwd):
    """Return the actual checkout root, without treating its parent as another project."""
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else None


def offer_project(args, *, cwd=None, ask=ask_terminal, out=sys.stderr):
    """Offer repository linking only within an explicitly interactive setup."""
    if getattr(args, 'link', None) is not None:
        return False
    repository = repository_root(cwd or Path.cwd())
    if repository is None:
        return False
    answer = ask(f'Link {repository} to a Kelmo project? [y/N]: ').strip().lower()
    if answer not in ('y', 'yes'):
        return False
    suggested = getattr(args, 'project', 'default')
    if suggested == 'default':
        suggested = re.sub(r'[^a-z0-9-]+', '-', repository.name.lower()).strip('-')[:60]
        if len(suggested) < 3:
            suggested = 'default'
    from ..initialization import validate
    while True:
        project = ask(f'Project name [{suggested}]: ').strip() or suggested
        try:
            validate(project)
            if project == 'mandatory':
                raise KelmoError('E_INPUT', 'mandatory is reserved for global memory.')
        except KelmoError as error:
            print(str(error), file=out)
            continue
        break
    args.project, args.link, args._project_requested = project, repository, True
    return True
