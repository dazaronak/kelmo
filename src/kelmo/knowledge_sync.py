"""Explicit Git synchronization under the same locks used by publication."""
import json
import re
import subprocess
import sys
import uuid
from contextlib import ExitStack

from .config import Config
from .errors import KelmoError
from .install.setup import write
from .publication import coordinator_lock
from .wiki import engine


def synchronize(root, remote, branch, *, apply=False, push=False):
    config = Config.load(root)
    repository = config.knowledge_git_root
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', remote):
        raise KelmoError('E_INPUT', 'Select a configured Git remote name.')

    def git(*args, allow_failure=False):
        try:
            result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True,
                                    text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KelmoError('E_GIT', 'Knowledge Git command failed or exceeded its deadline.') from error
        if result.returncode and not allow_failure:
            raise KelmoError('E_GIT', f'Knowledge Git {args[0]} failed; inspect the repository locally.')
        return result.returncode, result.stdout.strip()

    git('check-ref-format', 'refs/heads/' + branch)
    if git('symbolic-ref', '--quiet', '--short', 'HEAD')[1] != branch:
        raise KelmoError('E_GIT', 'The checked-out knowledge branch differs from --branch.')
    if remote not in git('remote')[1].splitlines():
        raise KelmoError('E_GIT', 'The selected Git remote is not configured.')
    before = git('rev-parse', 'HEAD')[1]
    dirty = bool(git('status', '--porcelain', '--untracked-files=all')[1])
    if not apply:
        return {'applied': False, 'remote': remote, 'branch': branch, 'head': before,
                'dirty': dirty, 'push_requested': push, 'remote_checked': False}
    with ExitStack() as stack:
        stack.enter_context(coordinator_lock(config))
        if not (config.schema_version == 1 and config.wiki == config.root / 'knowledge/wiki'):
            stack.enter_context(engine.wiki_lock(config.wiki, config.vault_id))
        if git('status', '--porcelain', '--untracked-files=all')[1]:
            raise KelmoError('E_GIT', 'Knowledge checkout is dirty; synchronization changed nothing.')
        if git('symbolic-ref', '--quiet', '--short', 'HEAD')[1] != branch:
            raise KelmoError('E_GIT', 'The knowledge branch changed before synchronization.')
        if config.internal('.state/wiki-transaction.json').exists():
            raise KelmoError('E_GIT', 'Finish the pending wiki transaction before synchronizing.')
        before = git('rev-parse', 'HEAD')[1]
        run_id = uuid.uuid4().hex
        backup_ref = 'refs/kelmo/sync/' + run_id
        git('update-ref', backup_ref, before)
        receipt = config.state / 'sync' / (run_id + '.json')
        report = {'remote': remote, 'branch': branch, 'before': before,
                  'backup_ref': backup_ref, 'pushed': False, 'state': 'fetching'}

        def save():
            write(receipt, (json.dumps(report, indent=2) + '\n').encode())

        save()
        git('fetch', '--quiet', '--no-tags', remote, 'refs/heads/' + branch)
        fetched = git('rev-parse', 'FETCH_HEAD')[1]
        report['remote_before'] = fetched
        if git('merge-base', '--is-ancestor', fetched, before, allow_failure=True)[0]:
            if git('merge-base', '--is-ancestor', before, fetched, allow_failure=True)[0]:
                # Check conflicts without putting conflict markers in the live checkout.
                if git('merge-tree', '--write-tree', before, fetched, allow_failure=True)[0]:
                    report['state'] = 'conflict'
                    save()
                    raise KelmoError('E_CONFLICT', f'Knowledge branches conflict; checkout retained at {before}. '
                                    f'Recovery receipt: {receipt}')
            report['state'] = 'merging'
            save()
            git('merge', '--no-edit', fetched)
        after = git('rev-parse', 'HEAD')[1]
        ahead = int(git('rev-list', '--count', fetched + '..' + after)[1])
        paths = git('diff', '--name-only', fetched + '..' + after)[1].splitlines()
        report.update(after=after, ahead=ahead, changed_paths=paths, state='merged')
        save()
        if push and ahead:
            print(f'Knowledge sync: pushing {ahead} commit(s); paths: ' + ', '.join(paths), file=sys.stderr)
            git('push', '--quiet', remote, 'HEAD:refs/heads/' + branch)
            report.update(pushed=True, state='pushed')
            save()
        return dict(report, applied=True, receipt=str(receipt))
