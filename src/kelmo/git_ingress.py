"""Successful Git evidence intake over enrolled local repositories, never a shell."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .context_binding import ContextBindings, canonical_remote, valid_head_ref
from .contracts import OperationReceipt, Origin, Record, SourcePacket
from .errors import KelmoError
from .state import ActionState, canonical_json, event_revision


# Marks a packet that GitIngress.capture built at event occurrence. The mark alone grants
# nothing: only capture() stores the matching observation on the request row, and ingest()
# refuses a packet carrying it when no such row exists.
CAPTURE_PRODUCER = 'kelmo/git-capture'
QUALIFYING_SUBJECT = re.compile(r'^(feat|fix|refactor)(?:\([^)]*\))?!?:\s*.+', re.IGNORECASE)
NO_OP_MESSAGES = ('already up to date', 'already up-to-date', 'everything up-to-date')
PUSH_FLAGS_WITH_VALUES = {'--exec', '--push-option', '--receive-pack', '--repo', '-o'}
MERGE_FLAGS_WITH_VALUES = {'--cleanup', '--file', '--gpg-sign', '--message', '--strategy',
                         '--strategy-option', '-F', '-S', '-m', '-s', '-X'}
# Git's own report lines are the attestation; refs read later may have moved since.
PUSH_DESTINATION = re.compile(r'^To\s+(\S.*?)\s*$')
PUSH_UPDATE = re.compile(r'^\s*\+?\s*([0-9a-f]{7,64})\.{2,3}([0-9a-f]{7,64})\s+(\S+)\s+->\s+(\S+)'
                         r'(?:\s+\(forced update\))?\s*$')
PUSH_NEW_REF = re.compile(r'^\s*\*\s+\[new (?:branch|reference)\]\s+(\S+)\s+->\s+(\S+)\s*$')
MERGE_UPDATE = re.compile(r'^Updating ([0-9a-f]{7,64})\.\.([0-9a-f]{7,64})\s*$')
MERGE_FAST_FORWARD = re.compile(r'^Fast-forward\s*$')


@dataclass(frozen=True)
class GitInvocation:
    operation: str
    repository: str
    args: tuple[str, ...]
    source_ref: str = ''
    configuration: tuple[str, ...] = ()
    # The directory exactly as the command named it, before lexical normalization; Git ran
    # there, so symlinks and `..` resolve against the real filesystem, not the string.
    directory: str = ''


def _tokens(command):
    if not isinstance(command, str) or any(char in command for char in ('\x00', '\n', '\r')):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars='|&;')
        lexer.whitespace_split = True
        lexer.commenters = ''
        return list(lexer)
    except ValueError:
        return []


def _find_operation(tokens, cwd):
    repository, directory = os.path.normpath(cwd), cwd
    if tokens[:1] == ['cd']:
        if len(tokens) < 4 or tokens[2] != '&&':
            return None
        directory = os.path.join(directory, tokens[1])
        repository = os.path.normpath(os.path.join(repository, tokens[1]))
        tokens = tokens[3:]
    if any(token and set(token) <= set('|&;') for token in tokens):
        return None
    if tokens[:1] == ['command']:
        tokens = tokens[1:]
    if not tokens or os.path.basename(tokens[0]) != 'git':
        return None
    index, configuration = 1, []
    while index < len(tokens):
        token = tokens[index]
        if token in ('-C', '-c'):
            if index + 1 >= len(tokens):
                return None
            if token == '-C':
                directory = os.path.join(directory, tokens[index + 1])
                repository = os.path.normpath(os.path.join(repository, tokens[index + 1]))
            else:
                configuration.append(tokens[index + 1])
            index += 2
        elif token.startswith('-C') and len(token) > 2:
            directory = os.path.join(directory, token[2:])
            repository = os.path.normpath(os.path.join(repository, token[2:]))
            index += 1
        elif token.startswith('-'):
            return None
        else:
            return token, tuple(tokens[index + 1:]), repository, tuple(configuration), directory
    return None


def _option_positionals(args, flags_with_values):
    positionals, index, end_of_options = [], 0, False
    while index < len(args):
        token = args[index]
        if end_of_options:
            positionals.append(token)
        elif token == '--':
            end_of_options = True
        elif token in flags_with_values:
            if index + 1 >= len(args):
                return None
            index += 1
        elif any(token.startswith(flag + '=') for flag in flags_with_values if flag.startswith('--')):
            pass
        elif not token.startswith('-'):
            positionals.append(token)
        index += 1
    return positionals


def parse_git_invocation(command, cwd):
    """Interpret supported invocation forms as data; no current-directory fallback."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or '\x00' in cwd:
        return None
    found = _find_operation(_tokens(command), cwd)
    if found is None:
        return None
    operation, args, repository, configuration, directory = found
    if operation == 'push':
        if any(arg in {'--delete', '-d', '--dry-run', '-n', '--all', '--tags', '--mirror', '--prune'}
               or arg.startswith('--delete=') for arg in args):
            return None
        positionals = _option_positionals(args, PUSH_FLAGS_WITH_VALUES)
        if positionals is None or any(value.startswith(':') for value in positionals[1:]):
            return None
        return GitInvocation('push', repository, args, configuration=configuration, directory=directory)
    if operation == 'merge':
        if any(arg in {'--abort', '--continue', '--quit'} for arg in args):
            return None
        positionals = _option_positionals(args, MERGE_FLAGS_WITH_VALUES)
        if positionals is not None and len(positionals) == 1:
            return GitInvocation('merge', repository, args, positionals[0], configuration, directory)
    return None


def _successful(payload):
    response = payload.get('tool_response')
    if not isinstance(response, dict):
        return False
    values = [response[name] for name in ('exit_code', 'exitCode') if name in response]
    return bool(values) and all(type(value) is int and value == 0
                                or type(value) is str and value == '0' for value in values)


def _response_text(payload):
    response = payload.get('tool_response')
    if not isinstance(response, dict):
        return ''
    return '\n'.join(response[key] for key in ('stdout', 'stderr', 'content', 'message')
                     if isinstance(response.get(key), str)).lower()


def _output_lines(payload):
    """The archived command output exactly as Git wrote it: stdout and stderr only."""
    response = payload.get('tool_response')
    if not isinstance(response, dict):
        return []
    return [line.rstrip('\r') for key in ('stdout', 'stderr') if isinstance(response.get(key), str)
            for line in response[key].split('\n')]


def attested_receipt(payload):
    """The part of a tool receipt the producer parser reads, normalized as that parser reads it.

    Command and cwd as `_observe` takes them, the exit status as `_successful` decides it, the
    no-op markers `_response_text` finds, and the `_output_lines` the line patterns can match:
    trailing whitespace dropped (every pattern ends in `\\s*$`) and blank lines omitted. Other
    envelope fields (session, transcript or tool-use identifiers) are not evidence. None when the
    payload has no command the parser could read.
    """
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get('command'), str):
        return None
    text = _response_text(payload)
    return {'command': tool_input['command'], 'cwd': payload.get('cwd') or tool_input.get('cwd'),
            'succeeded': _successful(payload),
            'no_op': [message for message in NO_OP_MESSAGES if message in text],
            'output': [line.rstrip() for line in _output_lines(payload) if line.strip()]}


def _command_toplevel(directory):
    """The Git top-level of the directory the command really ran in, resolved by Git itself."""
    profile_keys = {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM'}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_') or key in profile_keys}
    environment.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
             '-c', 'protocol.allow=never', 'rev-parse', '--show-toplevel'],
            cwd=directory, env=environment, text=True, capture_output=True, check=False, timeout=10)
    except subprocess.TimeoutExpired as error:
        raise KelmoError('E_GIT', 'The command directory could not be inspected in time.') from error
    except OSError as error:
        raise KelmoError('E_BINDING', 'The command directory is not an inspectable Git worktree.') from error
    if result.returncode or not result.stdout.strip():
        raise KelmoError('E_BINDING', 'The command directory is not inside a Git worktree.')
    return Path(result.stdout.strip()).resolve()


class _RepositoryReader:
    """Only fixed read commands execute, inside an independently enrolled root."""
    def __init__(self, contexts, origin, cwd):
        repository, root = contexts.repository_for(origin, cwd)
        self.repository_id = repository.repository_id
        self.root = Path(root)
        self.primary = Path(repository.root)
        self.expected_remote = contexts.repository_remotes[self.repository_id]
        for path in (self.root, self.primary):
            if path.resolve() != path or not path.is_dir():
                raise KelmoError('E_BINDING', 'Enrolled repository roots changed or are symlinks.')
        if Path(self.read('rev-parse', '--show-toplevel')) != self.root:
            raise KelmoError('E_BINDING', 'Git root differs from enrolled worktree.')
        if Path(self.read('rev-parse', '--show-toplevel', primary=True)) != self.primary:
            raise KelmoError('E_BINDING', 'Primary Git root differs from repository enrollment.')
        # Git common identity, rather than cwd or source branch, joins worktrees.
        common = self.read('rev-parse', '--path-format=absolute', '--git-common-dir')
        primary_common = self.read('rev-parse', '--path-format=absolute', '--git-common-dir',
                                   primary=True)
        if Path(common).resolve() != Path(primary_common).resolve():
            raise KelmoError('E_BINDING', 'Worktree belongs to a different Git repository.')

    def read(self, *args, optional=False, primary=False):
        root = self.primary if primary else self.root
        if root.resolve() != root:
            raise KelmoError('E_BINDING', 'Enrolled repository root changed during inspection.')
        profile_keys = {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM'}
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('GIT_') or key in profile_keys}
        environment.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
                           GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='', GIT_TERMINAL_PROMPT='0')
        try:
            result = subprocess.run(
                ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                 '-c', 'protocol.allow=never', *args], cwd=root, env=environment,
                text=True, capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KelmoError('E_GIT', 'Enrolled repository inspection failed or timed out.') from error
        if result.returncode and not optional:
            raise KelmoError('E_GIT', 'Enrolled repository inspection failed.')
        return result.stdout.strip() if result.returncode == 0 else ''

    def sha(self, revision):
        value = self.read('rev-parse', '--verify', revision, optional=True)
        return value if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) else ''

    def commit(self, prefix):
        """The one commit an abbreviated receipt name denotes in the immutable object store."""
        value = self.read('rev-parse', '--verify', '--quiet', prefix + '^{commit}', optional=True)
        return value if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) and value.startswith(prefix) else ''

    def reflog(self, ref):
        """(old, new) object IDs of every recorded update of one ref, oldest first; None when absent.

        Read from the reflog file Git itself locates (common directory included). A ref without a
        file-backed reflog (logging disabled, expired, deleted, or another ref backend) has no
        history to offer, and that absence is not evidence of anything.
        """
        location = self.read('rev-parse', '--git-path', 'logs/' + ref, optional=True)
        if not location:
            return None
        path = Path(location) if Path(location).is_absolute() else self.root / location
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            return None
        entries = []
        for line in text.splitlines():
            fields = line.split(' ', 2)
            if len(fields) < 3 or not all(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value)
                                          for value in fields[:2]):
                return None
            entries.append((fields[0], fields[1]))
        return entries

    def remote(self, name=None):
        names = self.read('remote').splitlines()
        if name is not None and name not in names:
            raise KelmoError('E_BINDING', 'Push remote is not a configured enrolled remote name.')
        candidates = [name] if name is not None else names
        matching = []
        for candidate in candidates:
            urls = self.read('remote', 'get-url', '--push', '--all', candidate).splitlines()
            try:
                identities = {canonical_remote(url) for url in urls}
            except ValueError as error:
                raise KelmoError('E_BINDING', 'Push remote cannot be matched to enrollment.') from error
            if identities == {self.expected_remote}:
                matching.append(candidate)
        if not matching:
            raise KelmoError('E_BINDING', 'Actual push destination differs from controller enrollment.')
        chosen = 'origin' if 'origin' in matching else matching[0]
        mirror = self.read('config', '--get', f'remote.{chosen}.mirror', optional=True)
        if mirror.lower() in ('true', 'yes', 'on', '1'):
            raise KelmoError('E_CAPABILITY', 'A mirrored push is not a single enrolled action.')
        return chosen


def _current_branch(reader):
    return reader.read('symbolic-ref', '--quiet', '--short', 'HEAD', optional=True)


def _default_branch(reader, remote):
    symbolic = reader.read('symbolic-ref', '--quiet', '--short',
                           f'refs/remotes/{remote}/HEAD', optional=True)
    if symbolic.startswith(remote + '/'):
        return symbolic[len(remote) + 1:]
    for candidate in ('main', 'master'):
        if reader.sha(f'refs/remotes/{remote}/{candidate}'):
            return candidate
    return ''


def _push_target(reader, args):
    positionals = _option_positionals(args, PUSH_FLAGS_WITH_VALUES)
    current = _current_branch(reader)
    if positionals is None or not current or len(positionals) > 2:
        return '', '', ''
    source = current
    if len(positionals) == 2:
        remote = reader.remote(positionals[0])
        refspec = positionals[1].lstrip('+')
        source, target = refspec.split(':', 1) if ':' in refspec else (refspec, refspec)
        source = current if source == 'HEAD' else source.removeprefix('refs/heads/')
        target = current if target == 'HEAD' else target.removeprefix('refs/heads/')
    else:
        # Git resolves pushRemote, remote.pushDefault, push.default and upstream
        # together. Guessing a branch name could attest a different destination.
        if reader.read('config', '--get', 'push.default', optional=True) == 'matching':
            raise KelmoError('E_CAPABILITY', 'Matching pushes cannot identify one action from this receipt.')
        native = reader.read('for-each-ref', '--format=%(push:remotename)%09%(push:remoteref)',
                             'refs/heads/' + current, optional=True).split('\t')
        if len(native) != 2 or not all(native) or not valid_head_ref(native[1]):
            raise KelmoError('E_CAPABILITY', 'Git cannot resolve one implicit push destination.')
        remote, target = native[0], native[1].removeprefix('refs/heads/')
        if positionals and positionals[0] != remote:
            raise KelmoError('E_CAPABILITY', 'Explicit remote without a refspec differs from Git push binding.')
        remote = reader.remote(remote)
        if reader.read('config', '--get-all', f'remote.{remote}.push', optional=True):
            raise KelmoError('E_CAPABILITY', 'Configured push refspecs need an explicit single-ref receipt.')
    if not all(valid_head_ref('refs/heads/' + value) for value in (source, target)):
        return '', '', ''
    return remote, source, target


def _owned_commits(reader, old_sha, new_sha, extra_author_emails):
    if not old_sha or not new_sha or old_sha == new_sha:
        return []
    email = reader.read('config', '--get', 'user.email', optional=True)
    allowed = {value.casefold() for value in (email, *extra_author_emails) if value}
    output = reader.read('log', '--reverse', '--format=%H%x1f%ae%x1f%s', f'{old_sha}..{new_sha}')
    commits = []
    for line in output.splitlines():
        parts = line.split('\x1f', 2)
        if len(parts) != 3:
            continue
        sha, author, subject = parts
        match = QUALIFYING_SUBJECT.match(subject)
        if match and author.casefold() in allowed:
            commits.append(dict(sha=sha, type=match[1].lower(), subject=subject, author_email=author))
    return commits


def _event(reader, operation, remote, source, target, default, old_sha, new_sha, base, commits):
    return dict(version=1, operation=operation, repository=str(reader.root), remote=remote,
                target_branch=target, source_branch=source, default_branch=default,
                old_sha=old_sha, new_sha=new_sha,
                state='final' if target in ('main', 'master') else 'provisional',
                lifecycle_id=hashlib.sha256('\x00'.join((str(reader.root), source, base)).encode()).hexdigest()[:20],
                commits=commits)


def _attested_range(reader, old_prefix, new_prefix):
    old_sha, new_sha = reader.commit(old_prefix), reader.commit(new_prefix)
    if not old_sha or not new_sha:
        raise KelmoError('E_EVIDENCE', 'The receipt names revisions this repository cannot resolve uniquely.')
    return old_sha, new_sha


def _push_receipt_range(payload, reader, target):
    """Old/new revisions the archived push output itself reports for the enrolled destination.

    Only a ref-update line (`<old>..<new> <src> -> <dst>`) names both revisions. A new-branch
    line names none, so it cannot attest which work was published and stays unresolved.
    """
    lines = _output_lines(payload)
    destinations = [match[1] for match in map(PUSH_DESTINATION.match, lines) if match]
    try:
        identities = {canonical_remote(value) for value in destinations}
    except ValueError as error:
        raise KelmoError('E_EVIDENCE', 'The receipt destination cannot be matched to enrollment.') from error
    if identities != {reader.expected_remote}:
        raise KelmoError('E_EVIDENCE', 'The receipt does not name the enrolled push destination.')
    names = {target, 'refs/heads/' + target}
    updates = [match for match in map(PUSH_UPDATE.match, lines) if match and match[4] in names]
    if len(updates) == 1:
        return _attested_range(reader, updates[0][1], updates[0][2])
    if not updates and any(match and match[2] in names for match in map(PUSH_NEW_REF.match, lines)):
        raise KelmoError('E_EVIDENCE', 'A new-branch push receipt names no revision; it cannot attest '
                                      'which enrolled work was published.')
    raise KelmoError('E_EVIDENCE', 'The receipt has no single ref-update line for the enrolled destination.')


def _push_event(invocation, reader, extra_author_emails, payload):
    remote, source, target = _push_target(reader, invocation.args)
    if not all((remote, source, target)):
        return None
    old_sha, new_sha = _push_receipt_range(payload, reader, target)
    default = _default_branch(reader, remote)
    commits = _owned_commits(reader, old_sha, new_sha, extra_author_emails)
    if not commits:
        return None
    base = old_sha
    if target not in ('main', 'master') and default:
        default_ref = ('refs/heads/' + default if reader.sha('refs/heads/' + default)
                       else f'refs/remotes/{remote}/{default}')
        base = reader.read('merge-base', new_sha, default_ref)
    if not base:
        return None
    return _event(reader, 'push', remote, source, target, default, old_sha, new_sha, base, commits)


def _merge_event(invocation, reader, extra_author_emails, payload):
    target = _current_branch(reader)
    if target not in ('main', 'master'):
        return None
    reader.remote()
    source = invocation.source_ref.removeprefix('refs/heads/')
    if not valid_head_ref('refs/heads/' + source):
        return None
    lines = _output_lines(payload)
    updates = [match for match in map(MERGE_UPDATE.match, lines) if match]
    # Only a fast-forward report names the merged revision; a merge-commit report names none.
    if len(updates) != 1 or not any(MERGE_FAST_FORWARD.match(line) for line in lines):
        raise KelmoError('E_EVIDENCE', 'This merge output names no merged revision; it cannot attest '
                                      'which enrolled work was merged.')
    old_sha, new_sha = _attested_range(reader, updates[0][1], updates[0][2])
    # The output does not name its branch; the checkout must still contain what it reported.
    if reader.read('merge-base', new_sha, 'HEAD', optional=True) != new_sha:
        raise KelmoError('E_EVIDENCE', 'The current branch no longer contains the reported merge revision.')
    commits = _owned_commits(reader, old_sha, new_sha, extra_author_emails)
    if not commits:
        return None
    base = reader.read('merge-base', old_sha, new_sha)
    return _event(reader, 'merge', '', source, target, target, old_sha, new_sha, base, commits) if base else None


def _receipt_agrees(payload, reader, names, old_sha, new_sha, pattern):
    """Where the receipt does report a destination or revisions, they must be the captured ones."""
    lines = _output_lines(payload)
    destinations = [match[1] for match in map(PUSH_DESTINATION.match, lines) if match]
    if destinations:
        try:
            identities = {canonical_remote(value) for value in destinations}
        except ValueError as error:
            raise KelmoError('E_EVIDENCE', 'The receipt destination cannot be matched to enrollment.') from error
        if identities != {reader.expected_remote}:
            raise KelmoError('E_EVIDENCE', 'The receipt does not name the enrolled push destination.')
    for match in map(pattern.match, lines):
        if match and (names is None or match.lastindex >= 4 and match[4] in names):
            if not (old_sha.startswith(match[1]) and new_sha.startswith(match[2])):
                raise KelmoError('E_EVIDENCE', 'The repository state at capture differs from the revisions '
                                              'this receipt reports.')


def _live_push_event(invocation, reader, extra_author_emails, payload):
    """The frozen collector's foreground read, made at event occurrence: Git has just updated the
    remote-tracking ref this push wrote, and its reflog holds the value before it."""
    remote, source, target = _push_target(reader, invocation.args)
    if not all((remote, source, target)):
        return None
    remote_ref = f'refs/remotes/{remote}/{target}'
    new_sha = reader.sha(remote_ref)
    if not new_sha or new_sha != reader.sha('refs/heads/' + source):
        return None
    default = _default_branch(reader, remote)
    # The receipt must itself report this destination: exactly one update or new-ref line.
    # Quiet or ambiguous output is refused, as on the raw path; no proof is inferred from refs.
    names = {target, 'refs/heads/' + target}
    lines = _output_lines(payload)
    if not any(PUSH_DESTINATION.match(line) for line in lines):
        raise KelmoError('E_EVIDENCE', 'The receipt names no push destination; quiet output cannot attest a capture.')
    updates = [match for match in map(PUSH_UPDATE.match, lines) if match and match[4] in names]
    created = [match for match in map(PUSH_NEW_REF.match, lines) if match and match[2] in names]
    if len(updates) + len(created) != 1:
        raise KelmoError('E_EVIDENCE', 'The receipt reports no single update for the enrolled destination.')
    previous = reader.sha(remote_ref + '@{1}')
    if created:
        # Positive creation evidence only: the tracking ref's whole recorded history is the one
        # creation entry (null old value) at the current value. A missing, disabled or expired
        # reflog, or any later entry, proves nothing about which push created it.
        history = reader.reflog(remote_ref)
        if history is None or not history:
            raise KelmoError('E_EVIDENCE', 'A new-branch receipt needs the tracking ref\'s reflog to show '
                                          'its creation; this repository records no such history.')
        if previous or len(history) != 1 or set(history[0][0]) != {'0'} or history[0][1] != new_sha:
            raise KelmoError('E_EVIDENCE', 'A new-branch receipt cannot attest a tracking ref whose history '
                                          'is not exactly its creation at the current value.')
        if not default or target == default:
            return None
        old_sha = reader.read('merge-base', new_sha, f'refs/remotes/{remote}/{default}')
    else:
        if not previous:
            raise KelmoError('E_EVIDENCE', 'The update receipt has no previous tracking value to attest.')
        old_sha = previous
    _receipt_agrees(payload, reader, names, old_sha, new_sha, PUSH_UPDATE)
    commits = _owned_commits(reader, old_sha, new_sha, extra_author_emails)
    if not commits:
        return None
    base = old_sha
    if target not in ('main', 'master') and default:
        default_ref = ('refs/heads/' + default if reader.sha('refs/heads/' + default)
                       else f'refs/remotes/{remote}/{default}')
        base = reader.read('merge-base', new_sha, default_ref)
    if not base:
        return None
    return _event(reader, 'push', remote, source, target, default, old_sha, new_sha, base, commits)


def _live_merge_event(invocation, reader, extra_author_emails, payload):
    """A local merge into main/master read at occurrence; fast-forward or merge commit alike."""
    target = _current_branch(reader)
    if target not in ('main', 'master'):
        return None
    reader.remote()
    source = invocation.source_ref.removeprefix('refs/heads/')
    if not valid_head_ref('refs/heads/' + source):
        return None
    new_sha, old_sha = reader.sha('HEAD'), reader.sha('HEAD@{1}')
    reflog = reader.read('reflog', 'show', '-1', '--format=%gs', 'HEAD')
    if not old_sha or old_sha == new_sha or not reflog.startswith(f'merge {invocation.source_ref}:'):
        return None
    source_sha = reader.sha('refs/heads/' + source)
    # The merged tip: the new head itself, or the second parent of the merge commit.
    if not source_sha or source_sha not in (new_sha, reader.sha('HEAD^2')):
        return None
    _receipt_agrees(payload, reader, None, old_sha, new_sha, MERGE_UPDATE)
    commits = _owned_commits(reader, old_sha, new_sha, extra_author_emails)
    if not commits:
        return None
    base = reader.read('merge-base', old_sha, source_sha)
    if not base:
        return None
    event = _event(reader, 'merge', '', source, target, target, old_sha, new_sha, base, commits)
    event['merged_sha'] = source_sha
    return event


def _captured(source):
    try:
        value = json.loads(source.content)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) and value.get('producer') == CAPTURE_PRODUCER else None


def _stored(envelope):
    """The collector a capture row is written from: the archived envelope, never a new ref read."""
    def collect():
        if envelope.get('refusal') is not None:
            raise KelmoError(envelope['refusal']['code'], envelope['refusal']['message'])
        return GitObservation.from_dict(envelope['observation'])
    return collect


@dataclass(frozen=True)
class GitObservation(Record):
    """Internal journal observation, not another producer input or authority token."""
    disposition: str
    reason: str
    repository_id: str | None = None
    remote_identity: str | None = None
    event_json: str | None = None

    def __post_init__(self):
        if self.disposition not in ('matched', 'ignored', 'unresolved') or not self.reason:
            raise ValueError('Invalid Git observation disposition.')
        if self.disposition in ('matched', 'unresolved'):
            event = self.event()
            if (not self.repository_id or not self.remote_identity or event.get('version') != 1
                    or event.get('operation') not in ('push', 'merge')
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', event.get('new_sha', ''))
                    or not valid_head_ref('refs/heads/' + event.get('target_branch', ''))):
                raise ValueError('Git observation lacks exact repository/revision/ref evidence.')

    def event(self) -> dict:
        value = json.loads(self.event_json) if self.event_json is not None else None
        if not isinstance(value, dict):
            raise ValueError('Git observation requires a stored producer event.')
        return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field in producer receipt.')
        result[key] = value
    return result


class GitIngress:
    def __init__(self, service, contexts: ContextBindings, state_api: ActionState):
        if (not isinstance(state_api, ActionState) or state_api.service is not service
                or state_api.contexts is not contexts):
            raise KelmoError('E_ORIGIN', 'Git intake must use this Service\'s existing action owner.')
        self.service, self.contexts, self.state_api = service, contexts, state_api

    def ingest(self, source: SourcePacket, origin: Origin, idempotency_key: str) -> OperationReceipt:
        self.contexts.require_origin(origin, producer=True)
        if not isinstance(source, SourcePacket) or source.source_kind != 'git_event':
            raise KelmoError('E_INPUT', 'Git intake requires the original git_event SourcePacket.')
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise KelmoError('E_INPUT', 'Git intake requires an idempotency key.')
        self.service.archive_api.assert_writer()
        coordinator = self.state_api.coordinator()
        if _captured(source) is not None:
            archived, row = coordinator.captured_git(origin, idempotency_key)
            if archived is None and row is None:
                # No capture provenance under this key. Nothing is archived or recorded, so this
                # refusal cannot pre-empt a lawful capture that later uses the same key.
                raise KelmoError('E_EVIDENCE', 'A captured Git event is producer evidence only when capture() '
                                              'stored it at occurrence; a submitted snapshot is not.')
            if archived is not None and row is None:
                # A capture crashed after archiving its packet; finish its row from that packet.
                if archived != source:
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different capture.')
                coordinator.capture_git(archived, origin, idempotency_key, _stored(_captured(archived)))
        receipt = coordinator.ingest_git(
            source, origin, idempotency_key, lambda: self._collect(source, origin),
            lambda frame, observation: self._decide(frame, observation, source, origin),
        )
        if not isinstance(receipt, OperationReceipt):
            raise KelmoError('E_RECEIPT', 'Git coordinator returned no durable operation receipt.')
        if receipt.outcome not in ('published', 'unchanged', 'ignored', 'unresolved'):
            raise KelmoError('E_RECEIPT', 'Git coordinator returned an unsupported intake outcome.')
        if receipt.outcome in ('published', 'unchanged') and receipt.generation is None:
            raise KelmoError('E_PUBLICATION', 'Git completion has no committed publication generation.')
        return receipt

    def capture(self, payload: dict, origin: Origin, idempotency_key: str, *,
                source_id: str, observed_at: str) -> SourcePacket:
        """Foreground producer capture at event occurrence; admission happens later.

        Reads the repository the moment the tool reports success and archives one packet holding that immutable event, its disposition and the original
        tool receipt. The observation is stored on the request row, so ingest() with this packet and
        key consumes it instead of reading refs again. Returns the archived packet for that ingest().

        A retry under the same origin and key never observes again: it returns the archived packet
        when payload, source_id and observed_at are the same, replays a recorded refusal, finishes a
        row a crash left missing after the archive capture, and refuses any other input.
        """
        from .publication import TRANSIENT
        self.contexts.require_origin(origin, producer=True)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise KelmoError('E_INPUT', 'Git capture requires an idempotency key.')
        self.service.archive_api.assert_writer()
        coordinator = self.state_api.coordinator()
        archived, row = coordinator.captured_git(origin, idempotency_key)
        if archived is None and row is not None:
            raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a Git intake that was '
                                             'not a foreground capture.')
        if archived is not None:
            envelope = _captured(archived)
            try:
                same = (envelope is not None and archived.source_id == source_id
                        and archived.filename == source_id + '.md' and archived.observed_at == observed_at
                        and envelope.get('tool_receipt') == json.loads(json.dumps(payload)))
            except (TypeError, ValueError):
                same = False
            if not same:
                raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies a different capture.')
            coordinator.capture_git(archived, origin, idempotency_key, _stored(envelope))
            return archived
        try:
            observation, refusal = self._observe(payload, origin, live=True).to_dict(), None
        except KelmoError as error:
            if error.code in TRANSIENT:
                raise
            observation, refusal = None, {'code': error.code, 'message': str(error)}
        envelope = {'version': 1, 'producer': CAPTURE_PRODUCER, 'tool_receipt': payload,
                    'observation': observation, 'refusal': refusal}
        try:
            source = SourcePacket((json.dumps(envelope, indent=2, sort_keys=True) + '\n').encode(),
                                  'git_event', source_id, source_id + '.md', observed_at)
        except (TypeError, ValueError) as error:
            raise KelmoError('E_INPUT', 'The Git receipt cannot be archived as one source packet.') from error
        coordinator.capture_git(source, origin, idempotency_key, _stored(envelope))
        return source

    def _decide(self, frame, observation, source, origin):
        envelope = _captured(source)
        if envelope is not None:
            # Admission consumes the archived event; it may only confirm its immutable objects.
            if envelope.get('observation') != observation.to_dict():
                raise KelmoError('E_RECEIPT', 'The request observation differs from the archived capture.')
            if observation.disposition in ('matched', 'unresolved'):
                event = observation.event()
                reader = _RepositoryReader(self.contexts, origin, event['repository'])
                for value in {event['old_sha'], event['new_sha'], event_revision(event)}:
                    if reader.commit(value) != value:
                        raise KelmoError('E_EVIDENCE', 'A captured revision is absent from the enrolled repository.')
        return self.state_api.producer_decision(frame, observation, source, origin)

    def _collect(self, source, origin):
        try:
            payload = json.loads(source.content, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeDecodeError) as error:
            raise KelmoError('E_INPUT', 'Git evidence is not an unambiguous JSON receipt.') from error
        if isinstance(payload, dict) and payload.get('producer') == CAPTURE_PRODUCER:
            raise KelmoError('E_EVIDENCE', 'A captured Git event is producer evidence only when capture() '
                                          'stored it at occurrence; a submitted snapshot is not.')
        return self._observe(payload, origin, live=False)

    def _observe(self, payload, origin, *, live):
        """Strict receipt-only proof for a delayed raw receipt; the live read only at occurrence."""
        if not isinstance(payload, dict) or not _successful(payload):
            raise KelmoError('E_EVIDENCE', 'Missing, failed or ambiguous exit status cannot prove Git success.')
        tool_input = payload.get('tool_input')
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get('command'), str):
            raise KelmoError('E_INPUT', 'Git receipt lacks its original command data.')
        cwd = payload.get('cwd') or tool_input.get('cwd')
        invocation = parse_git_invocation(tool_input['command'], cwd)
        if invocation is None:
            return GitObservation('ignored', 'No supported single Git effect in this receipt.')
        # Command-level overrides could change the real destination. They are not
        # replayed as native inspection arguments or silently applied to origin.
        if invocation.configuration or any(arg.split('=', 1)[0] in ('--exec', '--receive-pack', '--repo')
                                           for arg in invocation.args):
            raise KelmoError('E_CAPABILITY', 'Git routing overrides need a controller-bound collector.')
        repository, root = self.contexts.repository_for(origin, invocation.repository)
        # The enrolled root is chosen from the normalized path; Git must agree on the real one,
        # so a nested repository, a symlink or `..` cannot borrow an enrolled worktree's refs.
        if _command_toplevel(invocation.directory or invocation.repository) != Path(root):
            raise KelmoError('E_BINDING', 'The command ran in a Git worktree other than its enrolled root.')
        if any(message in _response_text(payload) for message in NO_OP_MESSAGES):
            return GitObservation('ignored', 'Producer suppresses already-up-to-date operations.')
        reader = _RepositoryReader(self.contexts, origin, invocation.repository)
        if invocation.operation == 'push':
            collect = _live_push_event if live else _push_event
        else:
            collect = _live_merge_event if live else _merge_event
        event = collect(invocation, reader, self.contexts.author_emails, payload)
        if event is None:
            return GitObservation('ignored', 'Producer found no qualifying newly published commits.')
        binding = self.contexts.match_git(origin, event['operation'], repository.repository_id,
                                         event_revision(event), 'refs/heads/' + event['target_branch'])
        return GitObservation('matched' if binding else 'unresolved',
                              'Enrolled explicit Git completion.' if binding else 'No unique enrolled action.',
                              repository.repository_id, reader.expected_remote, canonical_json(event))
