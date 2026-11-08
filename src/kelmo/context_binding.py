"""Controller enrollment for actions and repository context; no accepted state."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, unquote

from .contracts import ActionUpdate, Origin, Record, _page_path, _text
from .errors import KelmoError
from .server import OriginBindings


def canonical_remote(value: str) -> str:
    """Repository identity excludes transport credentials and never contacts a host."""
    _text(value)
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError('Remote identity contains whitespace or control characters.')
    if os.path.isabs(value):
        return 'file://' + os.path.normpath(value)
    if '://' not in value:
        match = re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', value)
        if not match:
            raise ValueError('Remote identity must be an absolute path or repository URL.')
        host, path = match.groups()
        host = host.lower()
    else:
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment:
            raise ValueError('Remote identity cannot contain a query or fragment.')
        if parsed.scheme == 'file':
            if parsed.netloc not in ('', 'localhost') or not parsed.path.startswith('/'):
                raise ValueError('Only local absolute file remotes are supported.')
            return canonical_remote(unquote(parsed.path))
        if parsed.scheme not in ('https', 'http', 'ssh', 'git') or not parsed.hostname:
            raise ValueError('Unsupported repository URL.')
        host = parsed.hostname.lower()
        default_port = {'https': 443, 'http': 80, 'ssh': 22, 'git': 9418}[parsed.scheme]
        if parsed.port is not None and parsed.port != default_port:
            host += ':' + str(parsed.port)
        path = unquote(parsed.path)
    path = path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if (not path or any(part in ('', '.', '..') for part in path.split('/'))
            or any(char.isspace() or ord(char) < 32 for char in path)):
        raise ValueError('Remote repository path must be canonical.')
    return host + '/' + path


def canonical_root(value: str) -> str:
    if (not isinstance(value, str) or not os.path.isabs(value)
            or os.path.normpath(value) != value or '\x00' in value):
        raise ValueError('Controller roots must be canonical absolute paths.')
    return value


def valid_head_ref(value: str) -> bool:
    return (isinstance(value, str) and value.startswith('refs/heads/')
            and bool(value.removeprefix('refs/heads/')) and not value.endswith(('/', '.', '.lock'))
            and '..' not in value and '@{' not in value and '//' not in value
            and not any(c.isspace() or ord(c) < 32 or c in '~^:?*[\\' for c in value)
            and not any(p.startswith('.') or p.endswith('.lock') for p in value.split('/')))


@dataclass(frozen=True)
class ActionBinding(Record):
    """Immutable identity only. Repeated ordinary work may share a subject, never an ID."""
    action_id: str
    subject_kind: str
    subject_id: str
    kind: str
    feature_id: str | None = None
    repository_id: str | None = None
    environment: str | None = None
    work_revision: str | None = None
    remote_ref: str | None = None
    artifact_id: str | None = None

    def __post_init__(self):
        for name in ('action_id', 'subject_kind', 'subject_id', 'kind'):
            _text(getattr(self, name))
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None:
                _text(value)
        if self.subject_kind not in ('feature', 'topic', 'person', 'task'):
            raise ValueError('Unsupported action subject kind.')
        if self.feature_id is not None and (
                self.subject_kind != 'feature' or self.feature_id != self.subject_id):
            raise ValueError('Feature identity must equal the feature subject.')
        if self.kind in ('push', 'merge'):
            if (self.repository_id is None or self.work_revision is None
                    or not valid_head_ref(self.remote_ref)):
                raise ValueError('Git actions require repository, revision and full head ref.')
        if self.work_revision is not None and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', self.work_revision):
            raise ValueError('Git work revision must be an exact object ID.')
        if self.kind == 'deploy':
            if self.environment is None or self.artifact_id is None:
                raise ValueError('Deployments require environment and artifact identity.')
            if (self.repository_id is None) != (self.work_revision is None):
                raise ValueError('Git-derived deployments require repository and revision together.')

    @classmethod
    def from_update(cls, update: ActionUpdate) -> ActionBinding:
        return cls(**{item.name: getattr(update, item.name) for item in fields(cls)})

    def same_identity(self, other: ActionBinding) -> bool:
        left, right = self.to_dict(), other.to_dict()
        # The optional feature_id is redundant with an explicit feature subject.
        if self.subject_kind == 'feature':
            left['feature_id'] = self.subject_id
        if other.subject_kind == 'feature':
            right['feature_id'] = other.subject_id
        return left == right

    def record_fields(self) -> dict:
        return {key: value for key, value in self.to_dict().items() if value is not None}


@dataclass(frozen=True, init=False)
class ContextBindings:
    """Trusted constructor data only. No payload enrollment, disk writes or Git reads."""
    bindings: OriginBindings
    actions: Mapping[str, ActionBinding]
    repository_remotes: Mapping[str, str]
    worktrees: Mapping[str, str]
    feature_pages: Mapping[str, tuple[str, ...]]
    producer_client_ids: frozenset[str]
    author_emails: frozenset[str]

    def __init__(self, bindings: OriginBindings, *, actions: tuple[ActionBinding, ...],
                 repository_remotes: dict[str, str], worktrees: dict[str, str],
                 feature_pages: dict[str, tuple[str, ...]], producer_client_ids: tuple[str, ...],
                 author_emails: tuple[str, ...] = ()):
        if not isinstance(bindings, OriginBindings):
            raise ValueError('Use the existing trusted OriginBindings.')
        if not isinstance(actions, tuple) or not all(isinstance(a, ActionBinding) for a in actions):
            raise ValueError('Actions must be immutable controller bindings.')
        enrolled = {a.action_id: a for a in actions}
        if len(enrolled) != len(actions):
            raise ValueError('Duplicate action ID in controller enrollment.')
        remotes = {key: canonical_remote(value) for key, value in repository_remotes.items()}
        if len(set(remotes.values())) != len(remotes):
            raise ValueError('One canonical remote must have one repository ID.')
        trees = {canonical_root(key): value for key, value in worktrees.items()}
        if any(value not in remotes for value in trees.values()):
            raise ValueError('A worktree refers to a repository without remote enrollment.')
        if any(a.repository_id is not None and a.repository_id not in remotes for a in actions):
            raise ValueError('An action refers to a repository without remote enrollment.')
        pages = {}
        for feature, paths in feature_pages.items():
            _text(feature)
            if not isinstance(paths, tuple) or len(set(paths)) != len(paths):
                raise ValueError('Feature pages must be unique immutable paths.')
            for path in paths:
                _page_path(path)
            pages[feature] = paths
        if not isinstance(producer_client_ids, tuple) or not isinstance(author_emails, tuple):
            raise ValueError('Producer and author enrollment must be immutable.')
        for value in (*producer_client_ids, *author_emails, *remotes.keys()):
            _text(value)
        for key, value in dict(bindings=bindings, actions=MappingProxyType(enrolled),
                               repository_remotes=MappingProxyType(remotes), worktrees=MappingProxyType(trees),
                               feature_pages=MappingProxyType(pages),
                               producer_client_ids=frozenset(producer_client_ids),
                               author_emails=frozenset(value.casefold() for value in author_emails)).items():
            object.__setattr__(self, key, value)

    def require_origin(self, origin: Origin, *, requester=False, producer=False):
        try:
            enrolled = self.bindings.validate(origin)
            if requester:
                self.bindings.require_requester(origin)
            if producer and enrolled.client_id not in self.producer_client_ids:
                raise ValueError('This origin is not an enrolled Git producer.')
            return enrolled
        except ValueError as error:
            raise KelmoError('E_ORIGIN', str(error)) from error

    def require_repository(self, origin: Origin, repository_id: str):
        try:
            return self.bindings.repository(origin, repository_id)
        except (KeyError, ValueError) as error:
            raise KelmoError('E_BINDING', 'Repository is outside trusted origin enrollment.') from error

    def require_action(self, update: ActionUpdate, origin: Origin) -> ActionBinding:
        self.require_origin(origin)
        try:
            proposed = ActionBinding.from_update(update)
        except ValueError as error:
            raise KelmoError('E_INPUT', 'Invalid action identity.') from error
        known = self.actions.get(update.action_id)
        if known is None or not known.same_identity(proposed):
            raise KelmoError('E_BINDING', 'Action identity differs from controller enrollment.')
        if known.repository_id is not None:
            self.require_repository(origin, known.repository_id)
        return known

    def repository_for(self, origin: Origin, cwd: str):
        """Choose an enrolled root before any native inspection; nested Git is rechecked later."""
        self.require_origin(origin, producer=True)
        try:
            path = canonical_root(cwd)
        except ValueError as error:
            raise KelmoError('E_BINDING', 'Git cwd is not a canonical absolute path.') from error
        matches = [(root, repo) for root, repo in self.worktrees.items()
                   if path == root or Path(path).is_relative_to(root)]
        if not matches:
            raise KelmoError('E_BINDING', 'Git cwd is not inside an enrolled worktree.')
        root, repository_id = max(matches, key=lambda item: len(Path(item[0]).parts))
        return self.require_repository(origin, repository_id), root

    def match_git(self, origin: Origin, operation: str, repository_id: str,
                  work_revision: str, remote_ref: str) -> ActionBinding | None:
        self.require_origin(origin, producer=True)
        self.require_repository(origin, repository_id)
        matches = [action for action in self.actions.values()
                   if (action.kind, action.repository_id, action.work_revision, action.remote_ref)
                   == (operation, repository_id, work_revision, remote_ref)]
        return matches[0] if len(matches) == 1 else None

    def pages_for(self, action: ActionBinding) -> tuple[str, ...]:
        if action.subject_kind != 'feature':
            return ()
        return self.feature_pages.get(action.subject_id, ())
