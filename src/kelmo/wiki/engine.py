"""Apply structured wiki events with transactional Git recovery."""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from kelmo.errors import KelmoError
from kelmo.config import Config

from . import hot_views, links, pages, policy
from .directory import has_content

HERE = Path(__file__).resolve().parent
START, END = '<!-- kb -->', '<!-- /kb -->'
FIELDS = {'fact_key', 'target', 'text', 'status', 'expected_revision', 'replaces'}
STATUSES = {'active', 'backlog', 'in-progress', 'shipped', 'retired'}


class WikiProblem(ValueError):
    """A rejected wiki change: the message names the fact, its length, the page and the permitted repair."""

    def __init__(self, message, *, fact_key=None, target=None, length=None, repair=None):
        self.fact_key = fact_key
        self.target = target
        self.length = length
        self.repair = repair
        super().__init__(message)


class Conflict(WikiProblem):
    pass


class OwnershipConflict(Conflict, KelmoError):
    """A write that reaches a field another owner controls; both a wiki conflict and a stable KelmoError."""

    def __init__(self, message):
        Conflict.__init__(self, message)
        self.code = 'E_OWNERSHIP'
        self.exit_code = 1


class DirtyKnowledge(KelmoError, RuntimeError):
    """Uncommitted edits on paths a publication would rewrite; nothing is written or discarded.

    Still a RuntimeError for callers of the earlier untyped refusal.
    """

    def __init__(self, paths):
        self.paths = tuple(sorted(set(paths)))
        KelmoError.__init__(self, 'E_CONFLICT', 'Knowledge paths have uncommitted local changes: '
                           + ', '.join(self.paths) + '. Restore them to the committed bytes '
                           '(git restore --staged --worktree -- PATH) and record any text you want to keep '
                           'through kelmo create, then retry; committing a hand edit invalidates the knowledge '
                           'generation. A prepared action stays pending until then.')


IDENTITY = re.compile(r'[a-z0-9][a-z0-9._-]{2,119}')
V3_FACT = FIELDS | {'id', 'note_id', 'ark_id', 'revision', 'previous_revision', 'owner', 'protected',
                    'mandatory', 'evidence_resolution', 'evidence_refs', 'compatibility'}
V3_NOTE = {'filename', 'ark_id', 'evidence_resolution', 'evidence_refs', 'compatibility'}
V3_PAGE = set(pages.ENTRY_FIELDS) | {'page_id', 'layout', 'aliases', 'tags', 'repositories', 'links',
                                     'revision', 'previous_revision', 'compatibility'}
V3_LAYOUTS = {'person': ('topic', 'you'), 'project-map': ('hub', 'projects'), 'topic': ('topic', 'projects'),
              'feature': ('work', 'projects'), 'legacy': None}
ACTION_STATES = ('pending', 'in-progress', 'completed', 'cancelled', 'unresolved')
ACTION_FIELDS = {'action_id', 'subject_kind', 'subject_id', 'kind', 'state', 'owner', 'accepted_event',
                 'revision', 'previous_revision', 'compatibility'}
ACTION_BINDINGS = ('feature_id', 'repository_id', 'work_revision', 'remote_ref', 'environment', 'artifact_id')
EVENT_FIELDS = {'event_id', 'event_order', 'occurred_at', 'ingested_at', 'transition', 'evidence_ref',
                'evidence_mark', 'correction_of', 'operation_receipt_id'}
NARRATIVE_FIELDS = {'narrative_id', 'page_id', 'text', 'evidence_refs', 'depends_on', 'state', 'revision',
                    'previous_revision', 'compatibility'}


def valid_wiki_target(target):
    if not isinstance(target, str) or not re.fullmatch(
        r'(?:projects/[a-z0-9_-]+/|life/)?[a-z0-9_/-]+\.md', target
    ):
        return False
    path = Path(target)
    return (not path.is_absolute() and '..' not in path.parts and str(path) == target
            and not any(part.startswith('.') for part in path.parts))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory.parent)


def sync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)


def atomic_write(path, text):
    durable_directory(path.parent)
    if path.is_symlink():
        raise ValueError('refusing symlink output')
    fd, temporary = tempfile.mkstemp(prefix='.incoming-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rejected(item, cause, repair, length=None):
    """One sentence naming the fact, its length, the page and the repair that is permitted.

    A value that fails its own shape check is never echoed: the rejected target or fact key can
    carry a private path, and the repair sentence already says what a valid one looks like.
    """
    key = item.get('fact_key') if isinstance(item, dict) else None
    target = item.get('target') if isinstance(item, dict) else None
    key = key if isinstance(key, str) and re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,119}', key) else None
    target = target if valid_wiki_target(target) else None
    subject = ('fact ' + key) if key else 'a wiki fact change'
    if target is not None:
        subject += ' on page ' + target
    if length is not None:
        subject += ' (' + str(length) + ' characters)'
    return WikiProblem(subject + ': ' + cause + '; permitted repair: ' + repair,
                       fact_key=key, target=target, length=length, repair=repair)


def validate_changes(changes):
    if not isinstance(changes, list) or len(changes) > 30:
        raise WikiProblem('wiki-changes must be an array of at most 30 facts; permitted repair: '
                          'split the note into separate notes')
    keys = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != FIELDS:
            raise rejected(item, 'the change must carry exactly ' + ', '.join(sorted(FIELDS)),
                           'submit the change with those six fields and nothing else')
        key, target, text = item['fact_key'], item['target'], item['text']
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,119}', key) or key in keys:
            raise rejected(item, 'the fact key is invalid or repeated',
                           'use 3 to 120 characters of a-z 0-9 . _ - and name each fact once')
        keys.add(key)
        if not valid_wiki_target(target):
            raise rejected(item, 'the wiki target is not a relative Markdown page',
                           'name the page as projects/<project>/<page>.md or <page>.md under the wiki')
        if not isinstance(text, str) or not text.strip() or len(text) > 400 or any(x in text for x in ('\n', '\r', '<!--', '-->', '```')):
            raise rejected(item, 'the fact text must be one plain line of at most 400 characters',
                           'shorten this fact to a single line of at most 400 characters without markup',
                           length=len(text) if isinstance(text, str) else None)
        if item['status'] not in STATUSES:
            raise rejected(item, 'the fact status must be one of ' + ', '.join(sorted(STATUSES)),
                           'choose one of those statuses')
        revision = item['expected_revision']
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)):
            raise rejected(item, 'expected_revision must be a 64-character hex revision or null',
                           'run kelmo reconcile and copy the current revision of this fact')
        if not isinstance(item['replaces'], list) or not all(isinstance(k, str) and re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,119}', k) for k in item['replaces']):
            raise rejected(item, 'replaces must be a list of valid fact keys',
                           'name each replaced fact with a valid fact key')
        if key in item['replaces']:
            raise rejected(item, 'a fact cannot replace itself',
                           'remove ' + key + ' from its own replaces')
    return changes


def extract_changes(content):
    blocks = re.findall(r'^```wiki-changes\s*\n(.*?)^```\s*$', content, re.MULTILINE | re.DOTALL)
    if not blocks:
        return None
    if len(blocks) != 1:
        raise ValueError('exactly one wiki-changes block is required')
    return validate_changes(json.loads(blocks[0]))


def conflicted(item, cause, repair):
    problem = rejected(item, cause, repair)
    return Conflict(str(problem), fact_key=problem.fact_key, target=problem.target,
                    length=problem.length, repair=problem.repair)


def load_registry(vault):
    config = Config.load(vault)
    file = config.checked_path(config.registry)
    if not file.is_file():
        raise KelmoError('E_STATE', 'Bound registry is missing; opening a store cannot initialize an authority.')
    registry = json.loads(file.read_bytes())
    return validate_registry(registry)


REGISTRY_VERSION = 3


class UnsupportedRegistry(KelmoError, ValueError):
    """A registry in a format this release does not read; still a ValueError for validation callers."""

    def __init__(self, version):
        KelmoError.__init__(self, 'E_CAPABILITY', 'The fact registry has an unsupported format (version '
                            + repr(version)[:40] + '); this release reads version ' + str(REGISTRY_VERSION)
                            + ' only. Start an empty vault and bring the old knowledge in through kelmo create.')


def validate_registry(registry):
    version = registry.get('version') if isinstance(registry, dict) else None
    if type(version) is not int or version != REGISTRY_VERSION:
        raise UnsupportedRegistry(version)
    facts, notes = registry.get('facts'), registry.get('notes')
    if not isinstance(facts, dict) or not isinstance(notes, dict):
        raise ValueError('invalid registry maps')  # noqa: TRY004 - Preserve the registry validation contract.
    return _validate_v3(registry)


def initial_registry(authority_id, project=None):
    """The registry a new vault starts with: no knowledge yet, and the map of its first project if named."""
    registry = {'version': REGISTRY_VERSION, 'authority_id': authority_id, 'facts': {}, 'notes': {},
                'pages': {}, 'actions': {}, 'narratives': {}, 'redirects': {}, 'compatibility': {}}
    if project is None:
        return validate_registry(registry)
    target = f'projects/{project}/{project}.md'
    kind, category = V3_LAYOUTS['project-map']
    entry = {'kind': kind, 'category': category, 'parent': '', 'title': project_title(project),
             'summary': '', 'priority': '', 'page_id': project, 'layout': 'project-map', 'aliases': [], 'tags': [],
             'repositories': [], 'links': [], 'previous_revision': None, 'compatibility': {}}
    entry['revision'] = entity_revision(entry)
    registry['pages'][target] = entry
    return validate_registry(registry)


def entity_revision(value):
    """Entity revision: J of the whole object except revision and previous_revision."""
    from .file_contract import entity_revision as contract_revision
    return contract_revision(value)


def _validate_applications(registry):
    notes = registry['notes']
    applications=registry.get('session_applications',{})
    if not isinstance(applications,dict):
        raise ValueError('invalid session application map')
    for identity,receipt in applications.items():
        if (not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{64}',identity)
                or not isinstance(receipt,dict) or set(receipt)!={'note','proposal'}
                or not isinstance(receipt['note'],str) or receipt['note'] not in notes
                or not isinstance(receipt['proposal'],str) or not re.fullmatch('[0-9a-f]{64}',receipt['proposal'])):
            raise ValueError('invalid session application receipt')


def _plain(value, name, *, identity=False):
    if (not isinstance(value, str) or not value.strip() or any(char in value for char in '\r\n')
            or '<!--' in value or '-->' in value or (identity and not IDENTITY.fullmatch(value))):
        raise ValueError('invalid ' + name)


def _strings(value, name, *, identity=False):
    if not isinstance(value, list) or len(set(map(str, value))) != len(value):
        raise ValueError(name + ' must be a list of unique strings')
    for item in value:
        _plain(item, name, identity=identity)


def _evidence(value, name):
    from kelmo.contracts import EvidenceRef
    if not isinstance(value, dict):
        raise ValueError('invalid ' + name)
    try:
        EvidenceRef.from_dict(value)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid ' + name) from error


def _evidence_list(record, name):
    refs, resolution = record.get('evidence_refs'), record.get('evidence_resolution')
    if not isinstance(refs, list) or resolution not in ('exact', 'unresolved'):
        raise ValueError('invalid ' + name + ' evidence')
    for ref in refs:
        _evidence(ref, name + ' evidence reference')
    if (resolution == 'exact') != bool(refs) or len({encoded(ref) for ref in refs}) != len(refs):
        raise ValueError(name + ' evidence resolution differs from its references')


def _utc(value, name):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError(name + ' requires an RFC3339 UTC time')
    datetime.fromisoformat(value.replace('Z', '+00:00'))


def _validate_v3(registry):
    """The registry: one authority, explicit owners, exact revisions."""
    required = {'version', 'authority_id', 'facts', 'notes', 'pages', 'actions', 'narratives',
                'redirects', 'compatibility'}
    if not required <= registry.keys() or set(registry) - required - {'session_applications'}:
        raise ValueError('the registry carries exactly its documented maps')
    _plain(registry['authority_id'], 'authority', identity=True)
    for name in required - {'version', 'authority_id'}:
        if not isinstance(registry[name], dict):
            raise ValueError('invalid ' + name + ' map')
    _validate_applications(registry)
    facts, notes, entries = registry['facts'], registry['notes'], registry['pages']
    for identity, note in notes.items():
        if not re.fullmatch(r'[0-9a-f]{64}', identity) or not isinstance(note, dict) or set(note) != V3_NOTE:
            raise ValueError('invalid note receipt')
        _plain(note['filename'], 'note filename')
        _plain(note['ark_id'], 'note archive identity')
        _evidence_list(note, 'note')
        if not isinstance(note['compatibility'], dict):
            raise ValueError('invalid note compatibility')
    page_ids = {}
    for target, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) - {'feature_id'} != V3_PAGE:
            raise ValueError('page ' + str(target)[:120] + ': page fields differ from the contract')
        projected = {name: entry[name] for name in pages.ENTRY_FIELDS}
        projected['revision'] = pages.entry_revision(projected)
        pages.validate_entry(target, projected)
        _plain(entry['page_id'], 'page identity', identity=True)
        if entry['page_id'] in page_ids:
            raise ValueError('duplicate page identity ' + entry['page_id'])
        page_ids[entry['page_id']] = target
        if entry['layout'] not in V3_LAYOUTS:
            raise ValueError('page ' + target + ': unknown layout')
        expected = V3_LAYOUTS[entry['layout']]
        if expected is not None and (entry['kind'], entry['category']) != expected:
            raise ValueError('page ' + target + ': layout differs from its kind and category')
        if (entry.get('feature_id') != entry['page_id'] if entry['layout'] == 'feature'
                else 'feature_id' in entry):
            raise ValueError('page ' + target + ': a feature ID belongs only to its feature page')
        for name in ('aliases', 'tags'):
            _strings(entry[name], name)
        for name in ('repositories', 'links'):
            _strings(entry[name], name, identity=True)
        if not isinstance(entry['compatibility'], dict):
            raise ValueError('page ' + target + ': invalid compatibility')
        header = entry['compatibility'].get('header')
        if header is not None and (not isinstance(header, dict) or not isinstance(header.get('text'), str)
                                   or header != pages.header_record(header['text'])):
            raise ValueError('page ' + target + ': header adoption evidence must keep its exact original bytes')
        if entry['revision'] != entity_revision(entry):
            raise ValueError('page ' + target + ': entity revision mismatch')
    projection = {target: {name: entry[name] for name in pages.ENTRY_FIELDS} for target, entry in entries.items()}
    for entry in projection.values():
        entry['revision'] = pages.entry_revision(entry)
    pages.validate_pages(projection, {})
    for target, entry in entries.items():
        if set(entry['links']) - page_ids.keys():
            raise ValueError('page ' + target + ': links must name registered page identities')
    seen = set()
    for key, fact in facts.items():
        if not isinstance(fact, dict) or not V3_FACT <= fact.keys() or set(fact) - V3_FACT - {'role', 'index_target'}:
            raise ValueError('invalid stored fact')
        validate_changes([{k: fact[k] for k in FIELDS}])
        if fact['fact_key'] != key or fact['id'] != digest(key)[:12] or fact['id'] in seen:
            raise ValueError('invalid fact identity')
        seen.add(fact['id'])
        if fact['owner'] not in ('rule-policy', 'semantic-editor', 'legacy-import'):
            raise ValueError('fact ' + key + ': unknown owner')
        if type(fact['protected']) is not bool or type(fact['mandatory']) is not bool:
            raise ValueError('fact ' + key + ': protected and mandatory are booleans')
        if fact['protected'] and (fact['owner'] != 'rule-policy' or not fact['mandatory']):
            raise ValueError('fact ' + key + ': a protected rule needs policy ownership and mandatory delivery')
        if fact.get('role') is not None and fact['role'] not in pages.ROLES:
            raise ValueError('fact ' + key + ': invalid role')
        if fact['target'] not in entries or entries[fact['target']]['kind'] == 'prose':
            raise ValueError('fact ' + key + ': a fact needs a registered non-prose page')
        if fact['status'] in pages.OPEN and entries[fact['target']]['kind'] != 'work':
            raise ValueError('fact ' + key + ': open work belongs on a work page')
        if 'index_target' in fact:
            validate_changes([{**{k: fact[k] for k in FIELDS}, 'target': fact['index_target']}])
        _evidence_list(fact, 'fact')
        note = notes.get(fact['note_id'])
        if note is None or note['ark_id'] != fact['ark_id'] or note['evidence_refs'] != fact['evidence_refs']:
            raise ValueError('fact ' + key + ': note receipt and fact evidence differ')
        if not isinstance(fact['compatibility'], dict) or fact['revision'] != entity_revision(fact):
            raise ValueError('fact ' + key + ': entity revision mismatch')
    from kelmo.context_binding import valid_head_ref
    from kelmo.state import action_revision
    for key, action in registry['actions'].items():
        if (not isinstance(action, dict) or not ACTION_FIELDS <= action.keys()
                or set(action) - ACTION_FIELDS - set(ACTION_BINDINGS) or action['action_id'] != key):
            raise ValueError('invalid action ' + str(key)[:120])
        for name in ('action_id', 'subject_id', 'kind'):
            _plain(action[name], 'action ' + name, identity=True)
        for name in ACTION_BINDINGS:
            if name in action:
                _plain(action[name], 'action ' + name)
        if action['subject_kind'] not in ('feature', 'topic', 'person', 'task'):
            raise ValueError('action ' + key + ': unknown subject kind')
        if action['owner'] != 'state-api' or action['state'] not in ACTION_STATES:
            raise ValueError('action ' + key + ': only StateAPI owns a known action state')
        kind = action['kind']
        required_fields = {'push': {'repository_id', 'work_revision', 'remote_ref'},
                           'merge': {'repository_id', 'work_revision', 'remote_ref'},
                           'deploy': {'environment', 'artifact_id'}}.get(kind, set())
        forbidden = {'push': {'environment', 'artifact_id'}, 'merge': {'environment', 'artifact_id'},
                     'deploy': {'remote_ref'}}.get(kind, set(ACTION_BINDINGS) - {'feature_id'})
        if required_fields - action.keys() or forbidden & action.keys():
            raise ValueError('action ' + key + ': kind-specific fields differ')
        if kind == 'deploy' and ('repository_id' in action) != ('work_revision' in action):
            raise ValueError('action ' + key + ': a Git-derived deployment needs repository and revision')
        if 'work_revision' in action and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', action['work_revision']):
            raise ValueError('action ' + key + ': work revision must be an exact object ID')
        if 'remote_ref' in action and not valid_head_ref(action['remote_ref']):
            raise ValueError('action ' + key + ': remote ref must be a full head ref')
        if action['subject_kind'] == 'feature':
            feature = page_ids.get(action['subject_id'])
            if (feature is None or entries[feature]['layout'] != 'feature'
                    or action.get('feature_id', action['subject_id']) != action['subject_id']):
                raise ValueError('action ' + key + ': a feature subject needs its registered feature page')
        elif 'feature_id' in action:
            raise ValueError('action ' + key + ': only a feature subject has a feature ID')
        event = action['accepted_event']
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValueError('action ' + key + ': invalid accepted event')
        for name in ('event_id', 'operation_receipt_id'):
            _plain(event[name], 'event ' + name)
        if type(event['event_order']) is not int or event['event_order'] < 1:
            raise ValueError('action ' + key + ': event order must be a positive integer')
        _utc(event['occurred_at'], 'occurred_at')
        _utc(event['ingested_at'], 'ingested_at')
        _evidence(event['evidence_ref'], 'event evidence reference')
        if (event['transition'] != action['state'] or event['evidence_mark'] not in ('PRODUCER', 'REQUESTER')
                or event['correction_of'] is not None and not isinstance(event['correction_of'], str)):
            raise ValueError('action ' + key + ': accepted event differs from its projection')
        previous = action['previous_revision']
        if previous is not None and not re.fullmatch(r'[0-9a-f]{64}', str(previous)):
            raise ValueError('action ' + key + ': invalid previous revision')
        if not isinstance(action['compatibility'], dict) or action['revision'] != action_revision(action):
            raise ValueError('action ' + key + ': revision mismatch')
    for key, narrative in registry['narratives'].items():
        if (not isinstance(narrative, dict) or set(narrative) != NARRATIVE_FIELDS
                or narrative['narrative_id'] != key or narrative['page_id'] not in page_ids):
            raise ValueError('invalid narrative ' + str(key)[:120])
        _plain(key, 'narrative identity', identity=True)
        if not isinstance(narrative['text'], str) or not narrative['text'].strip() or any(
                marker in narrative['text'] for marker in (START, END, '<!-- narrative -->', '<!-- /narrative -->')):
            raise ValueError('narrative ' + key + ': invalid text')
        if not isinstance(narrative['evidence_refs'], list):
            raise ValueError('narrative ' + key + ': invalid evidence')
        for ref in narrative['evidence_refs']:
            _evidence(ref, 'narrative evidence reference')
        depends = narrative['depends_on']
        if not isinstance(depends, dict) or set(depends) != {'facts', 'actions'} or narrative['state'] not in (
                'current', 'stale'):
            raise ValueError('narrative ' + key + ': invalid dependencies or state')
        for kind in ('facts', 'actions'):
            if not isinstance(depends[kind], dict) or any(
                    not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)
                    for revision in depends[kind].values()):
                raise ValueError('narrative ' + key + ': dependencies map identities to revisions')
            if narrative['state'] == 'current' and any(
                    registry[kind].get(ref, {}).get('revision') != revision
                    for ref, revision in depends[kind].items()):
                raise ValueError('narrative ' + key + ': a changed or missing dependency cannot stay current')
        if not isinstance(narrative['compatibility'], dict) or narrative['revision'] != entity_revision(narrative):
            raise ValueError('narrative ' + key + ': entity revision mismatch')
    for target, page_id in registry['redirects'].items():
        if not valid_wiki_target(target) or target in entries or page_id not in page_ids:
            raise ValueError('invalid redirect')
    return registry


def render_v3(vault, registry, wiki_root):
    """Every registered page for one publication; unmanaged bytes come from the current file."""
    wiki = Path(wiki_root).resolve()
    result = {}
    for target in sorted(registry['pages']):
        path = wiki / target
        if any(item.is_symlink() for item in (path, *path.parents)) or not path.resolve().is_relative_to(wiki):
            raise ValueError('wiki target traverses symlink')
        existing = path.read_bytes().decode('utf-8') if path.exists() else None
        result[path] = pages.render_v3_page(target, existing, registry)
    return result


def render_registry(vault, registry, wiki_root=None):
    """The committed projection readers and doctor compare against."""
    config = Config.load(vault)
    wiki_root = (Path(wiki_root) if wiki_root is not None else config.wiki).resolve()
    return render_v3(vault, registry, wiki_root)


# A Git lock older than this was most likely left by a crashed process; kelmo doctor names it.
STALE_GIT_LOCK_SECONDS = 600
GIT_LOCK = re.compile(rb"Unable to create '([^']+\.lock)': File exists")


def git(vault, *args, binary=False):
    # Untranslated messages, so a held lock is recognized whatever the host's language.
    environment = {key: value for key, value in os.environ.items() if key not in ('LANGUAGE', 'LC_ALL')}
    environment['LC_MESSAGES'] = 'C'
    result = subprocess.run(['git', '-c', 'core.fsync=all', '-c', 'core.fsyncMethod=fsync',
                             '-C', str(vault), *args], capture_output=True, check=False, env=environment)
    if result.returncode:
        if GIT_LOCK.search(result.stderr):
            # Another Git process holds the index or a ref: contention the caller waits out. A lock a
            # crashed process left is named by kelmo doctor (STALE_GIT_LOCK_SECONDS), never failed here.
            raise KelmoError('E_BUSY', f'Git {args[0]} found the repository locked by another Git process; '
                                      'it is retried when that process finishes.')
        if b'cannot lock ref' in result.stderr and b'but expected' in result.stderr:
            # Another writer moved the ref between our read and our update: its turn came first. A ref
            # name conflict also says `cannot lock ref`, but it never passes, so it stays E_GIT.
            raise KelmoError('E_BUSY', f'Git {args[0]} lost a ref update race to another writer; retry.')
        raise KelmoError(
            'E_GIT',
            f'Git {args[0]} failed with exit code {result.returncode}. '
            'Check repository locks, signing, or hooks, then retry.',
        )
    return result.stdout if binary else result.stdout.decode('utf-8').strip()


def git_blob(repository, specification):
    result = subprocess.run(
        ['git', '-C', str(repository), 'show', specification], capture_output=True, check=False
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 128:
        return None
    raise RuntimeError('Git object inspection failed')


def _wiki_metadata(wiki):
    location = Path(git(wiki, 'rev-parse', '--git-dir'))
    return (wiki / location).resolve() if not location.is_absolute() else location.resolve()


def _owner_path(wiki):
    return _wiki_metadata(wiki) / 'kelmo-owner.json'


def _owner_error():
    return KelmoError(
        'E_WIKI_OWNER',
        'Wiki ownership is missing or invalid; rerun kelmo init --vault <data-root>.',
    )


def _read_owner(wiki):
    owner = _owner_path(wiki)
    if owner.is_symlink():
        raise _owner_error()
    try:
        claim = json.loads(owner.read_text(encoding='utf-8'))
        uuid.UUID(claim['vault_id'])
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise _owner_error() from error
    if claim != {'version': 1, 'vault_id': claim['vault_id']}:
        raise _owner_error()
    return claim


def configure_wiki(data_root, knowledge_root, vault_id=None):
    """Select the knowledge Git repository of a vault: its pages live under knowledge/wiki and its registry
    under knowledge/provenance, the layout a schema-2 vault binds. A directory without a repository gets
    one; a new vault needs an empty one."""
    try:
        uuid.UUID(vault_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise KelmoError('E_CONFIG', 'A valid vault ID is required to enroll a wiki.') from error
    data_root = Path(data_root).expanduser().resolve()
    config = Config.load(data_root) if (data_root / 'kelmo.toml').is_file() else None
    bound = config is not None and config.schema_version == 2
    knowledge = Path(knowledge_root).expanduser()
    if not knowledge.is_absolute():
        knowledge = data_root / knowledge
    if any(path.is_symlink() for path in (knowledge, *knowledge.parents)):
        raise KelmoError(
            'E_CONFIG',
            f'Selected wiki path {knowledge} uses a symlink. Select a real non-symlink path.',
        )
    knowledge = knowledge.resolve()
    if bound and knowledge == config.wiki:
        # A bound vault may be named by its pages directory as well as by its repository.
        knowledge = config.knowledge_git_root
    if bound and (knowledge != config.knowledge_git_root or vault_id != config.vault_id):
        raise KelmoError('E_CONFIG', 'Wiki enrollment differs from the selected storage binding.')
    if knowledge == data_root or knowledge.is_relative_to(data_root) or data_root.is_relative_to(knowledge):
        raise KelmoError('E_CONFIG', 'An explicit wiki root must be separate from the data root.')
    if knowledge.exists() and not knowledge.is_dir():
        raise KelmoError('E_CONFIG', 'Wiki root must be a directory.')
    if (knowledge / '.git').is_symlink():
        raise KelmoError('E_CONFIG', 'Wiki Git metadata cannot be a symlink.')
    existing = has_content(knowledge)
    if existing and not bound:
        raise KelmoError('E_WIKI_ENROLL', 'Select an empty wiki directory; a new vault does not take over '
                         'existing pages.')
    wiki = knowledge / (config.wiki_relative if bound else 'knowledge/wiki')
    if bound:
        if repository_root(config.knowledge_git_root) != config.knowledge_git_root:
            raise KelmoError('E_CONFIG', 'Knowledge Git root must match the existing repository.')
        if not wiki.is_dir() or repository_root(wiki) != config.knowledge_git_root:
            raise KelmoError('E_CONFIG', 'Bound wiki must belong to the existing knowledge Git root.')
        load_registry(data_root)
        owner = _owner_path(wiki)
        if (owner.exists() or owner.is_symlink()) and _read_owner(wiki).get('vault_id') != vault_id:
            raise _owner_error()
        return {'wiki_root': str(wiki), 'knowledge_root': str(knowledge), 'existing': existing}
    durable_directory(knowledge)
    probe = subprocess.run(
        ['git', '-C', str(knowledge), 'rev-parse', '--show-toplevel'], capture_output=True, check=False
    )
    if not probe.returncode and Path(probe.stdout.decode().strip()).resolve() != knowledge:
        raise KelmoError('E_CONFIG', 'Wiki root must have its own Git repository.')
    if probe.returncode:
        name, email = os.environ.get('GIT_AUTHOR_NAME'), os.environ.get('GIT_AUTHOR_EMAIL')
        if not name or not email:
            identity = []
            for field in ('user.name', 'user.email'):
                result = subprocess.run(
                    ['git', 'config', '--get', field], cwd='/',
                    capture_output=True, text=True, check=False,
                )
                if result.returncode or not result.stdout.strip():
                    raise KelmoError('E_GIT_IDENTITY', 'Configure Git user.name and user.email first.')
                identity.append(result.stdout.strip())
            name, email = identity
        git(knowledge, 'init', '-q', '-b', 'master')
        git(knowledge, 'config', 'user.name', name)
        git(knowledge, 'config', 'user.email', email)
        git(knowledge, 'commit', '--allow-empty', '-q', '-m', 'chore: Initialize wiki storage')
    if wiki.exists() and not wiki.is_dir():
        raise KelmoError('E_CONFIG', 'knowledge/wiki inside the selected repository must be a directory.')
    wiki.mkdir(parents=True, exist_ok=True)
    owner = _owner_path(wiki)
    if owner.exists() or owner.is_symlink():
        claim = _read_owner(wiki)
        if claim.get('vault_id') != vault_id:
            raise _owner_error()
    return {'wiki_root': str(wiki), 'knowledge_root': str(knowledge), 'existing': existing}


def activate_wiki(wiki_root, vault_id):
    wiki = Path(wiki_root).expanduser().resolve()
    owner = _owner_path(wiki)
    content = encoded({'version': 1, 'vault_id': vault_id})
    descriptor, temporary = tempfile.mkstemp(prefix='.kelmo-owner-', dir=owner.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, owner, follow_symlinks=False)
        except FileExistsError:
            claim = _read_owner(wiki)
            if claim != {'version': 1, 'vault_id': vault_id}:
                raise _owner_error()
            return
        sync_directory(owner.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wiki_owner_valid(vault, wiki_root=None):
    from kelmo.config import Config
    try:
        config = Config.load(vault)
        wiki = (Path(wiki_root) if wiki_root is not None else config.wiki).resolve()
        if config.schema_version == 1 and wiki == (config.root / 'knowledge/wiki').resolve():
            return True
        return _read_owner(wiki) == {'version': 1, 'vault_id': config.vault_id}
    except (KelmoError, OSError, RuntimeError, ValueError):
        return False


@contextmanager
def wiki_lock(wiki_root, vault_id):
    wiki = Path(wiki_root).resolve()
    owner = _read_owner(wiki)
    if owner.get('vault_id') != vault_id:
        raise _owner_error()
    lock_path = _wiki_metadata(wiki) / 'kelmo.lock'
    if lock_path.is_symlink():
        raise _owner_error()
    with lock_path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def wiki_committed(vault, registry=None, wiki_root=None):
    vault = Path(vault).resolve()
    config = Config.load(vault)
    wiki_root = (Path(wiki_root) if wiki_root is not None else config.wiki).resolve()
    registry = load_registry(vault) if registry is None else validate_registry(registry)
    rendered = render_registry(vault, registry, wiki_root)
    rendered[config.registry] = encoded(registry)
    try:
        for path, expected in rendered.items():
            repository = repository_root(config.knowledge_git_root if path == config.registry
                                         else wiki_root)
            relative = str(path.relative_to(repository))
            if not path.exists() or path.read_bytes() != expected.encode('utf-8'):
                return False
            if git(repository, 'show', 'HEAD:' + relative, binary=True) != expected.encode('utf-8'):
                return False
        if repository_root(wiki_root) == config.knowledge_git_root:
            generation_path = config.registry.with_name('generation.json')
            if generation_path.exists():
                relative = generation_path.relative_to(config.knowledge_git_root).as_posix()
                raw = generation_path.read_bytes()
                if git_blob(config.knowledge_git_root, 'HEAD:' + relative) != raw:
                    return False
                manifest = json.loads(raw)
                if manifest['managed_digest'] != pages.managed_digest(config, 'HEAD'):
                    return False
                pages.KnowledgeReader(config).generation()
    except (KelmoError, OSError, RuntimeError, UnicodeError, ValueError, KeyError):
        return False
    return True


def transaction_path(vault):
    return Config.load(vault).internal('.state/wiki-transaction.json')


def clear_transaction(vault):
    marker = transaction_path(vault)
    recorded = hashlib.sha256(marker.read_bytes()).hexdigest()
    marker.unlink()
    sync_directory(marker.parent)
    _clear_pending(vault, recorded)


def _pending_path(vault):
    """Locator of an unfinished publication in the knowledge Git metadata, beside the wiki owner claim.

    The marker itself lives in the configured state root. A changed state root would hide it, so the
    repository that the publication touches records where its marker is until recovery clears it.
    """
    return _wiki_metadata(Config.load(vault).knowledge_git_root) / 'kelmo-pending.json'


def _record_pending(vault, marker, text):
    pointer = _pending_path(vault)
    if pointer.is_symlink():
        raise Conflict('Pending publication locator cannot be a symlink')
    atomic_write(pointer, encoded({'version': 1, 'marker': str(marker),
                                   'marker_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}))


def _clear_pending(vault, marker_sha256):
    try:
        pointer = _pending_path(vault)
    except KelmoError:
        return
    if pointer.is_symlink() or not pointer.is_file():
        return
    try:
        recorded = json.loads(pointer.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if isinstance(recorded, dict) and recorded.get('marker_sha256') == marker_sha256:
        pointer.unlink()
        sync_directory(pointer.parent)


def _refuse_foreign_pending(vault, marker):
    """With no marker in this state root, a locator naming another one is a stranded publication."""
    try:
        pointer = _pending_path(vault)
    except KelmoError as error:
        if error.code == 'E_GIT':
            return
        raise
    if pointer.is_symlink():
        raise Conflict('Pending publication locator cannot be a symlink')
    if not pointer.exists():
        return
    try:
        recorded = json.loads(pointer.read_text(encoding='utf-8'))
        location = recorded['marker']
        if recorded.get('version') != 1 or not isinstance(location, str):
            raise ValueError('Invalid pending publication locator')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Conflict('Pending publication locator is invalid; preserved for reconciliation') from error
    if location == str(marker):
        # This root's own marker is already gone; only the locator's unlink was interrupted.
        pointer.unlink()
        sync_directory(pointer.parent)
        return
    raise Conflict('An unfinished publication marker is recorded under another state root: ' + location
                   + '; recover it with its original storage binding before publishing')


def repository_root(path):
    return Path(git(path, 'rev-parse', '--show-toplevel')).resolve()


def _binding(config):
    """Absolute schema-2 bindings; legacy relative locations retain relocation semantics."""
    def location(path):
        if config.schema_version == 2:
            return str(path)
        return '.' if path == config.root else (
            path.relative_to(config.root).as_posix() if path.is_relative_to(config.root) else str(path))
    return {'authority': config.vault_id, 'schema_version': config.schema_version,
            'config_sha256': hashlib.sha256((config.root / 'kelmo.toml').read_bytes()).hexdigest(),
            'roots': {name: location(path) for name, path in (
                ('config', config.root), ('state', config.state), ('archive', config.archive.parent),
                ('knowledge', config.knowledge_git_root), ('registry', config.registry), ('wiki', config.wiki))}}


def _transaction_entries(transaction, vault):
    if transaction.get('version') != 2:
        raise ValueError
    config = Config.load(vault)
    wiki = config.wiki
    repositories = {'data': repository_root(config.knowledge_git_root)}
    wiki_repository = repository_root(wiki)
    if wiki_repository != repositories['data']:
        repositories['wiki'] = wiki_repository
    if (repositories['data'] != config.knowledge_git_root
            or config.schema_version == 2 and wiki_repository != config.knowledge_git_root
            or config.schema_version == 1 and 'wiki' in repositories and wiki_repository != wiki):
        raise ValueError
    paths = transaction['paths']
    heads = transaction['base_heads']
    title = transaction['title']
    if not isinstance(title, str) or not title:
        raise ValueError
    if not isinstance(paths, dict) or not isinstance(heads, dict):
        raise ValueError
    if set(heads) != set(repositories):
        raise ValueError
    entries = []
    for identity, state in paths.items():
        repository_name = state['repository']
        repository = repositories[repository_name]
        relative = state['path']
        if identity != repository_name + ':' + relative and transaction.get('version') == 2:
            raise ValueError
        parts = Path(relative).parts
        candidate = repository / relative
        permitted = (repository_name == 'data' and (
            candidate in (config.registry, config.registry.with_name('generation.json'))
            or candidate.parent == wiki.parent / 'hot' and _hot_view_path('hot/' + candidate.name)))
        if candidate.is_relative_to(wiki):
            permitted = permitted or valid_wiki_target(candidate.relative_to(wiki).as_posix())
        if Path(relative).is_absolute() or '..' in parts or not permitted:
            raise ValueError
        path = repository
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError
        before, after = state['before'], state['after']
        if (before is not None and not isinstance(before, str)) or (after is not None and not isinstance(after, str)):
            raise ValueError
        entries.append((repository_name, repository, relative, path,
                        before.encode('utf-8') if before is not None else None,
                        after.encode('utf-8') if after is not None else None))
    for name, head in heads.items():
        if name not in repositories or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
            raise ValueError
    return repositories, heads, title, entries


def _transaction_base(repository, relative, before, after, base_head):
    """Reject index states that recovery did not inherit or produce."""
    records = git(repository, 'ls-files', '--stage', '-z', '--', relative,
                  binary=True).split(b'\0')
    records = [record.split(b'\t', 1)[0].split() for record in records if record]
    if len(records) > 1 or any(len(record) != 3 or record[2] != b'0' for record in records):
        raise Conflict('Unfinished wiki transaction has unmerged index entries')
    indexed = git_blob(repository, ':' + relative)
    index_mode = records[0][0] if records else None
    if bool(records) != (indexed is not None):
        raise Conflict('Unfinished wiki transaction index is unavailable')
    base = git_blob(repository, base_head + ':' + relative)
    if base not in (None, before):
        raise Conflict('Unfinished wiki transaction baseline conflicts with Git')
    tree = git(repository, 'ls-tree', base_head, '--', relative, binary=True)
    base_mode = tree.split(b' ', 1)[0] if tree else None
    if (base is None) != (base_mode is None):
        raise Conflict('Unfinished wiki transaction baseline is unavailable')
    after_modes = {b'100644'}
    if base_mode in (b'100644', b'100755'):
        after_modes.add(base_mode)
    if (indexed, index_mode) != (base, base_mode) and not (
            indexed == after and (index_mode in after_modes if after is not None else index_mode is None)):
        raise Conflict('Unfinished wiki transaction conflicts with staged edits')
    return base


def _hot_view_path(relative):
    """A generated project view (wiki/hot_views.py) beside the wiki: `…hot/<project>.md`, nothing deeper."""
    return re.fullmatch(r'(?:knowledge/)?hot/[a-z0-9_-]{1,80}(?:\.full)?\.md', relative) is not None


def _recover_v1(vault, transaction):
    if Config.load(vault).schema_version != 1:
        raise Conflict('Legacy transaction cannot be recovered under a different storage binding')
    if (not isinstance(transaction.get('paths'), dict)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', transaction.get('base_head', ''))):
        raise ValueError
    entries = []
    for relative, state in transaction['paths'].items():
        parts = Path(relative).parts
        wiki_prefix = 'knowledge/wiki/'
        wiki_target = relative[len(wiki_prefix):] if relative.startswith(wiki_prefix) else None
        if (Path(relative).is_absolute() or '..' in parts
                or not (relative in ('knowledge/provenance/registry.json',
                                     'knowledge/provenance/generation.json')
                        or _hot_view_path(relative)
                        or wiki_target is not None and valid_wiki_target(wiki_target))):
            raise ValueError
        path = vault
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError
        before, after = state['before'], state['after']
        if (before is not None and not isinstance(before, str)) or (after is not None and not isinstance(after, str)):
            raise ValueError
        current = path.read_bytes() if path.exists() else None
        old = before.encode('utf-8') if before is not None else None
        new = after.encode('utf-8') if after is not None else None
        if current not in (old, new):
            raise Conflict('Unfinished wiki transaction conflicts with local edits')
        try:
            _transaction_base(vault, relative, old, new, transaction['base_head'])
        except (KelmoError, RuntimeError) as error:
            raise Conflict('Git index is unavailable during transaction recovery') from error
        entries.append((relative, path, old, new, current))
    if git(vault, 'rev-parse', 'HEAD') != transaction['base_head']:
        try:
            committed = all(current == new and git_blob(vault, 'HEAD:' + relative) == new
                            for relative, _, _, new, current in entries)
        except KelmoError as error:
            raise Conflict('Git history changed during an unfinished wiki transaction') from error
        if not committed:
            raise Conflict('Git history changed during an unfinished wiki transaction')
        if 'knowledge/provenance/generation.json' in transaction['paths']:
            pages.KnowledgeReader(Config.load(vault)).generation()
        clear_transaction(vault)
        return
    for relative, path, old, _, current in entries:
        if current == old:
            continue
        if old is None:
            path.unlink()
            sync_directory(path.parent)
        else:
            atomic_write(path, old.decode('utf-8'))
    if entries:
        git(vault, 'reset', '-q', 'HEAD', '--', *(entry[0] for entry in entries))
    clear_transaction(vault)


def recover_transaction(vault):
    vault = Path(vault).resolve()
    marker = transaction_path(vault)
    if marker.is_symlink():
        raise Conflict('Wiki transaction marker cannot be a symlink')
    if not marker.exists():
        _refuse_foreign_pending(vault, marker)
        return
    from kelmo.config import Config
    config = Config.load(vault)
    if config.schema_version == 1 and config.wiki == (vault / 'knowledge/wiki').resolve():
        return _recover_transaction(vault)
    with wiki_lock(config.wiki, config.vault_id):
        return _recover_transaction(vault)


def _recover_transaction(vault):
    marker = transaction_path(vault)
    if marker.is_symlink():
        raise Conflict('Wiki transaction marker cannot be a symlink')
    if not marker.exists():
        _refuse_foreign_pending(vault, marker)
        return
    config = Config.load(vault)
    try:
        if repository_root(config.knowledge_git_root) != config.knowledge_git_root:
            raise Conflict('Knowledge root must match its bound repository root')
    except (KelmoError, RuntimeError) as error:
        raise Conflict('Data root repository is unavailable during transaction recovery') from error
    try:
        transaction = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(transaction, dict):
            raise ValueError('Invalid transaction object')
        if (transaction.get('binding') is not None and transaction['binding'] != _binding(config)
                or config.schema_version == 2 and transaction.get('binding') is None):
            raise Conflict('Unfinished wiki transaction storage binding changed; marker preserved')
        if transaction.get('version') == 1:
            _recover_v1(vault, transaction)
            return
        repositories, heads, title, entries = _transaction_entries(transaction, vault)
        current_entries = []
        for name, repository, relative, path, old, new in entries:
            current = path.read_bytes() if path.exists() else None
            if current not in (old, new):
                raise Conflict('Unfinished wiki transaction conflicts with local edits')
            current_entries.append((name, repository, relative, path, old, new, current))
    except (ValueError, KeyError, TypeError) as error:
        if isinstance(error, Conflict):
            raise
        raise Conflict('Invalid unfinished wiki transaction; preserved for reconciliation') from error
    committed = set()
    try:
        for name, repository in repositories.items():
            relevant = [entry for entry in current_entries if entry[0] == name]
            base_states = [_transaction_base(repository, entry[2], entry[4], entry[5],
                                             heads[name]) for entry in relevant]
            if git(repository, 'rev-parse', 'HEAD') == heads[name]:
                continue
            head_states = [git_blob(repository, 'HEAD:' + entry[2]) for entry in relevant]
            if relevant and all(entry[6] == entry[5] and head == entry[5]
                                for entry, head in zip(relevant, head_states)):
                committed.add(name)
            elif not all(head == base for head, base in zip(head_states, base_states)):
                raise Conflict('Git history changed during an unfinished wiki transaction')
    except (KelmoError, RuntimeError) as error:
        raise Conflict('Git history changed during an unfinished wiki transaction') from error
    if committed and title is None:
        clear_transaction(vault)
        return
    if committed:
        if any(entry[3] == config.registry.with_name('generation.json') and entry[0] in committed
               for entry in current_entries):
            pages.KnowledgeReader(config).generation()
        for name, repository in repositories.items():
            if name in committed:
                continue
            relevant = [entry for entry in current_entries if entry[0] == name]
            if not relevant:
                continue
            for _, _, _, path, _, new, current in relevant:
                if current != new:
                    if new is None:
                        path.unlink(missing_ok=True)
                        sync_directory(path.parent)
                    else:
                        atomic_write(path, new.decode('utf-8'))
            paths = [entry[2] for entry in relevant]
            git(repository, 'add', '--', *paths)
            git(repository, 'commit', '--only', '--no-verify', '-m', title, '--', *paths)
        clear_transaction(vault)
        return
    for _, repository, relative, path, old, _, current in current_entries:
        if current != old:
            if old is None:
                path.unlink()
                sync_directory(path.parent)
            else:
                atomic_write(path, old.decode('utf-8'))
    for name, repository in repositories.items():
        paths = [entry[2] for entry in current_entries if entry[0] == name]
        if paths:
            git(repository, 'reset', '-q', 'HEAD', '--', *paths)
    clear_transaction(vault)






def _prepared_intent(vault, operation_id, *, semantic=False):
    from kelmo.journal import prepared_action_intent
    intent = prepared_action_intent(Config.load(vault).internal('.state/journal.sqlite3'), operation_id,
                                    semantic=semantic)
    if intent is None:
        raise OwnershipConflict('No prepared ' + ('editor' if semantic else 'StateAPI')
                                + ' journal intent owns this publication')
    return intent


def person_targets(registry):
    """Pages whose every fact is delivered whole as mandatory person context."""
    return {target for target, entry in registry.get('pages', {}).items()
            if entry['layout'] == 'person' or entry['category'] == 'you'} | {'person.md'}


class BindingConflict(OwnershipConflict):
    """A write that would change which pages show an action outside its controller enrollment."""

    def __init__(self, message):
        super().__init__(message)
        self.code = 'E_BINDING'


class SemanticRefusal(Conflict, KelmoError):
    """A semantic intent whose own projection is invalid: settled as a typed refusal, never left prepared."""

    def __init__(self, message, code='E_PROPOSAL'):
        Conflict.__init__(self, message)
        self.code = code
        self.exit_code = 1


def _shown(registry, subject, kind):
    return set(pages.action_targets(registry, {'subject_id': subject, 'subject_kind': kind}))


def _evidence_resolved_only(before, after):
    """True when a fact changed only from unresolved legacy evidence to the exact reference of its note."""
    fixed = ('evidence_resolution', 'evidence_refs', 'revision', 'previous_revision')
    return (before['evidence_resolution'] == 'unresolved' and after['evidence_resolution'] == 'exact'
            and len(after['evidence_refs']) == 1 and before['note_id'] == after['note_id']
            and {k: v for k, v in before.items() if k not in fixed} == {k: v for k, v in after.items() if k not in fixed})


def ownership_guard(previous, result, *, action_id=None, rule_change=False, enrollment=None, subjects=None):
    """The one ownership check of every registry write, whoever proposes it.

    Accepted actions, and the panels rendered from them, change only through the prepared StateAPI
    intent named by `action_id`; a fresh revision gives no other writer that right. The pages that
    show a feature page, an accepted action subject or an enrolled feature change only to exactly
    the controller enrollment supplied in `enrollment`, so a page or link edit cannot make StateAPI
    feature admission inconsistent. Protected rules, person facts, person page entries and the
    protected/mandatory markers change only under an attributable rule-change source; resolving a
    fact's unresolved legacy evidence to the exact reference of its own note is not such a change.
    Every fact on a person page stays in mandatory delivery, and no writer removes a fact:
    retiring one is a status change its owner makes. `subjects` names every enrolled action subject
    with its kind, accepted or not yet materialized, so no write exposes one outside its enrollment.
    """
    keys = previous['actions'].keys() | result['actions'].keys()
    if any(previous['actions'].get(key) != result['actions'].get(key) for key in keys - {action_id}):
        raise OwnershipConflict('Ordinary wiki publication cannot modify accepted actions; only a prepared '
                                'StateAPI intent changes them and their generated panels')
    enrolled = dict(subjects or {})
    subjects = {entry['page_id']: 'feature' for entry in previous['pages'].values() if entry['layout'] == 'feature'}
    subjects.update({action['subject_id']: action['subject_kind'] for action in previous['actions'].values()})
    subjects.update(enrolled)
    subjects.update({feature: 'feature' for feature in (enrollment or {})})
    for subject, kind in sorted(subjects.items()):
        shown = _shown(result, subject, kind)
        if shown != _shown(previous, subject, kind) and not (
                enrollment and subject in enrollment and shown == set(enrollment[subject])):
            raise BindingConflict('Pages showing ' + subject + ' would differ from its controller enrollment; '
                                  'permitted repair: update the feature enrollment first and pass it to the '
                                  'editor, or drop the link')
    before_person, after_person = person_targets(previous), person_targets(result)
    if not rule_change:
        for target in before_person | after_person:
            if previous['pages'].get(target) != result['pages'].get(target):
                raise OwnershipConflict('page ' + target + ': a person page changes only through an attributable '
                                        'rule-change source')
    for key in sorted(previous['facts'].keys() | result['facts'].keys()):
        before, after = previous['facts'].get(key), result['facts'].get(key)
        if before == after:
            continue
        if after is None:
            raise OwnershipConflict('fact ' + key + ': a publication never removes a fact; retire it instead')
        if not rule_change and not (before is not None and _evidence_resolved_only(before, after)):
            if before is not None and (before['protected'] or before['mandatory'] or before['target'] in before_person):
                raise OwnershipConflict('fact ' + key + ': protected rules and person facts keep their text, status, '
                                        'conditions and mandatory delivery; only an attributable rule-change '
                                        'source can change them')
            if (after['protected'] or after['owner'] == 'rule-policy'
                    or after['mandatory'] != (after['target'] in after_person)):
                raise OwnershipConflict('fact ' + key + ': rule protection and mandatory delivery are granted only '
                                        'by an attributable rule-change source')
        if after['target'] in after_person and not after['mandatory']:
            raise OwnershipConflict('fact ' + key + ': every person fact stays in mandatory delivery')
    person_pages = {registry['pages'][target]['page_id'] for registry in (previous, result)
                    for target in before_person | after_person if target in registry['pages']}
    for key in sorted(previous['narratives'].keys() | result['narratives'].keys()):
        before, after = previous['narratives'].get(key), result['narratives'].get(key)
        if before == after:
            continue
        if after is None:
            raise OwnershipConflict('narrative ' + key + ': a publication never removes a narrative')
        # Prose delivered with the person rules is rule text too; a stale label alone is publisher-derived.
        if not rule_change and {page['page_id'] for page in (before, after) if page} & person_pages and (
                before is None or any(before[name] != after[name] for name in NARRATIVE_CONTENT)):
            raise OwnershipConflict('narrative ' + key + ': prose on a person page changes only through an '
                                    'attributable rule-change source')
    return result


SEMANTIC_PAGE_FIELDS = ('title', 'summary', 'priority')
NARRATIVE_CONTENT = ('page_id', 'text', 'evidence_refs', 'depends_on')


def _content(record):
    return {key: value for key, value in record.items() if key not in ('revision', 'previous_revision')}


def semantic_noop(registry, kind, change):
    """True when a proposed fact or page update restates exactly what the registry already holds."""
    if kind == 'pages':
        entry = registry['pages'].get(change['target'])
        return (entry is not None and all(entry[name] == change[name]
                                          for name in SEMANTIC_PAGE_FIELDS if name in change)
                and set(change.get('add_links', ())) <= set(entry.get('links', ())))
    prior = registry['facts'].get(change['fact_key'])
    return (prior is not None and all(prior[name] == change[name] for name in ('target', 'text', 'status'))
            and ('role' not in change or prior.get('role') == change['role']))


def resolvable_note(registry, reference):
    """True when this archive-verified reference resolves an unresolved legacy note of the same bytes.

    The note hash and archive ID must both match; a different source restating a claim never does.
    """
    note = registry['notes'].get(reference['content_sha256'])
    return (note is not None and note['evidence_resolution'] == 'unresolved'
            and note['ark_id'] == reference['archive_id'])


def _resolve_note(registry, reference):
    note = registry['notes'][reference['content_sha256']]
    note.update(evidence_resolution='exact', evidence_refs=[reference])
    for fact in registry['facts'].values():
        if fact['note_id'] == reference['content_sha256'] and fact['evidence_resolution'] == 'unresolved':
            fact.update(evidence_resolution='exact', evidence_refs=[reference], previous_revision=fact['revision'])
            fact['revision'] = entity_revision(fact)


def apply_semantic_intent(registry, intent):
    """The only registry transform an editor proposal makes, recomputed from its prepared journal intent.

    Evidence is the one archived source the intent cites, never a claim in the proposal. Owners,
    protection and mandatory delivery are derived here; a narrative whose fact or action dependency
    changed becomes stale in the same publication.
    """
    from kelmo.contracts import EvidenceRef
    rule_change = intent.get('rule_change')
    if rule_change is not None and rule_change.get('source_operation_id') != intent['source_operation_id']:
        raise OwnershipConflict('A rule change is attributable only to the source the intent cites')
    reference = EvidenceRef.from_dict(intent['evidence_ref']).to_dict()
    delta = intent['delta']
    result = copy.deepcopy(registry)
    person = person_targets(result)
    note_id = reference['content_sha256']
    if resolvable_note(result, reference):
        _resolve_note(result, reference)
    note = result['notes'].setdefault(note_id, {
        'filename': intent['filename'], 'ark_id': reference['archive_id'], 'evidence_resolution': 'exact',
        'evidence_refs': [reference], 'compatibility': {}})
    if (note['ark_id'], note['evidence_refs']) != (reference['archive_id'], [reference]):
        raise Conflict('The source is already recorded with different archive evidence; permitted repair: '
                       'edit from the source that note already cites')
    page_targets, affected_pages = set(), set()
    for change in delta['pages']:
        target = change['target']
        if target in page_targets:
            raise Conflict('page ' + target + ': a page may be proposed only once')
        page_targets.add(target)
        if change['expected_revision'] is None:
            _create_page(result, change, intent)
            affected_pages.add(change['parent'])
            continue
        entry = registry['pages'].get(target)
        if entry is None or entry['revision'] != change['expected_revision']:
            raise Conflict('page ' + target + ': the expected page revision is not the stored one')
        if semantic_noop(registry, 'pages', change):
            continue
        updated = copy.deepcopy(result['pages'][target])
        updated.update({name: change[name] for name in SEMANTIC_PAGE_FIELDS if name in change})
        # Links are only ever appended here; the ownership guard still checks which pages show a feature.
        updated['links'] += [link for link in change.get('add_links', ()) if link not in updated['links']]
        result['pages'][target] = updated
        affected_pages.add(target)
    # All edits in one publication share the last accepted predecessor; an untouched page keeps its own.
    for target in affected_pages & registry['pages'].keys():
        entry = result['pages'][target]
        if _content(entry) == _content(registry['pages'][target]):
            continue
        entry['previous_revision'] = registry['pages'][target]['revision']
        entry['revision'] = entity_revision(entry)
    for change in delta['facts']:
        key = change['fact_key']
        # The expected revision is the one the model read, before this intent resolved its note;
        # the published predecessor stays that single original revision.
        original, prior = registry['facts'].get(key), result['facts'].get(key)
        stored = original['revision'] if original else None
        if stored != change['expected_revision']:
            raise conflicted(change, 'the expected revision ' + (change['expected_revision'] or 'none')[:12]
                             + ' does not match the stored revision ' + (stored or 'none')[:12],
                             'read the page again and propose against its current revision')
        if semantic_noop(result, 'facts', change):
            # Restating a claim never rebinds its owner, evidence or predecessor to this source.
            continue
        # Under an attributable rule change a new person fact is a protected rule; nothing else grants it.
        new_rule = rule_change is not None and prior is None and change['target'] in person
        fact = {'fact_key': key, 'target': change['target'], 'text': change['text'], 'status': change['status'],
                'expected_revision': change['expected_revision'], 'replaces': [], 'id': digest(key)[:12],
                'note_id': note_id, 'ark_id': reference['archive_id'],
                'owner': 'rule-policy' if new_rule or prior and prior['owner'] == 'rule-policy' else 'semantic-editor',
                'protected': new_rule or bool(prior and prior['protected']),
                'mandatory': bool(prior and prior['mandatory']) or change['target'] in person,
                'evidence_resolution': 'exact', 'evidence_refs': [reference],
                'previous_revision': original['revision'] if original else None,
                'compatibility': copy.deepcopy(prior['compatibility']) if prior else {}}
        role = change['role'] if 'role' in change else (prior or {}).get('role')
        if role is not None:
            fact['role'] = role
        if prior and 'index_target' in prior and prior['target'] == change['target']:
            fact['index_target'] = prior['index_target']
        validate_changes([{name: fact[name] for name in FIELDS}])
        fact['revision'] = entity_revision(fact)
        result['facts'][key] = fact
    for change in delta['narratives']:
        key = change['narrative_id']
        prior = result['narratives'].get(key)
        entry = result['pages'].get(change['target'])
        if entry is None or (prior is not None and prior['page_id'] != entry['page_id']):
            raise Conflict('narrative ' + key + ': a narrative stays on one registered page')
        if (prior['revision'] if prior else None) != change['expected_revision']:
            raise Conflict('narrative ' + key + ': the expected revision is not the stored one')
        if prior is None and key in identity_namespace(result):
            raise SemanticRefusal('narrative ' + key + ': the identity already names a page, narrative or action')
        depends = {}
        for kind in ('facts', 'actions'):
            missing = [name for name in change['depends_on'][kind] if name not in result[kind]]
            if missing:
                raise Conflict('narrative ' + key + ': it depends on unknown ' + kind + ' ' + ', '.join(missing[:5]))
            depends[kind] = {name: result[kind][name]['revision'] for name in change['depends_on'][kind]}
        if (prior is not None and prior['state'] == 'current' and prior['text'] == change['text']
                and prior['depends_on'] == depends):
            continue
        refs = list(prior['evidence_refs']) if prior else []
        if reference not in refs:
            refs.append(reference)
        narrative = {'narrative_id': key, 'page_id': entry['page_id'], 'text': change['text'],
                     'evidence_refs': refs, 'depends_on': depends, 'state': 'current',
                     'previous_revision': prior['revision'] if prior else None,
                     'compatibility': copy.deepcopy(prior['compatibility']) if prior else {}}
        narrative['revision'] = entity_revision(narrative)
        result['narratives'][key] = narrative
    return _stale_narratives(result)


CREATED_LAYOUTS = ('topic', 'feature')


def _refuse_claimed_files(wiki, previous, result):
    """A page that a publication newly registers must have no file yet, tracked or not."""
    for target in sorted(result['pages'].keys() - previous['pages'].keys()):
        if os.path.lexists(Path(wiki) / target):
            raise OwnershipConflict('page ' + target + ': a created page cannot claim an existing file')


def creation_problem(page_id, target, layout, features, subjects):
    """Refuse a new page identity that controller enrollment does not authorize.

    Page creation needs the controller's enrollment. A page may take an enrolled feature's
    identity only as that feature's feature page at an enrolled target; a feature page needs an
    enrolled feature; an enrolled topic, person or task subject never gets a page this way, since
    publisher-assigned identities for those are not decided.
    """
    if features is None or subjects is None:
        raise BindingConflict('page ' + target + ': creating a page needs the controller enrollment; permitted '
                              'repair: pass the trusted ContextBindings to the editor')
    kind = subjects.get(page_id, 'feature' if page_id in features else None)
    if kind is not None and kind != 'feature':
        raise BindingConflict('page ' + target + ': ' + page_id + ' is an enrolled ' + kind + ' subject; a created '
                              'page cannot take its identity')
    if (kind == 'feature') != (layout == 'feature') or (
            kind == 'feature' and target not in features.get(page_id, ())):
        raise BindingConflict('page ' + target + ': a feature page takes exactly an enrolled feature identity at '
                              'one of its enrolled pages; permitted repair: enroll the feature and its pages first')


def _create_page(registry, change, intent):
    """Register one linked detail page under the existing page contract, and link it from its parent.

    The target stays inside the request project, hangs from a page the editor sent in full, and
    takes a new stable page ID; repositories, aliases and tags are never proposed. Whether its
    file is absent is checked against the working tree by semantic_projection.
    """
    target, parent, page_id = change['target'], change['parent'], change['page_id']
    if target in registry['pages'] or target in registry['redirects']:
        raise Conflict('page ' + target + ': a created page cannot reuse a registered target')
    if not valid_wiki_target(target) or pages.project_of(target) != intent.get('project'):
        raise OwnershipConflict('A created page stays inside the project of its editor request')
    if (parent not in intent.get('scope', ()) or parent not in registry['pages']
            or parent in person_targets(registry)):
        raise OwnershipConflict('A created page hangs from a non-person page the editor sent in full')
    if change['layout'] not in CREATED_LAYOUTS:
        raise OwnershipConflict('An editor creates only topic or feature detail pages')
    ids = {entry['page_id']: name for name, entry in registry['pages'].items()}
    if not isinstance(page_id, str) or not IDENTITY.fullmatch(page_id) or page_id in identity_namespace(registry):
        raise SemanticRefusal('page ' + target + ': the page ID is invalid or already names a page, narrative '
                              'or action')
    creation_problem(page_id, target, change['layout'], intent.get('feature_enrollment'),
                     intent.get('enrolled_subjects'))
    links = list(change.get('links', []))
    if any(ids.get(link) not in intent.get('scope', ()) for link in links):
        raise OwnershipConflict('A created page links only to pages the editor sent in full')
    # Navigation is recorded in `links` both ways, as adoption records it: the parent lists the
    # child below, and the child links back to its parent.
    parent_id = registry['pages'][parent]['page_id']
    if parent_id not in links:
        links.append(parent_id)
    kind, category = V3_LAYOUTS[change['layout']]
    entry = {'kind': kind, 'category': category, 'parent': parent, 'title': change['title'],
             'summary': change['summary'], 'priority': change.get('priority', ''), 'page_id': page_id,
             'layout': change['layout'], 'aliases': [], 'tags': [], 'repositories': [], 'links': links,
             'previous_revision': None, 'compatibility': {}}
    if change['layout'] == 'feature':
        entry['feature_id'] = page_id
    entry['revision'] = entity_revision(entry)
    registry['pages'][target] = entry
    holder = copy.deepcopy(registry['pages'][parent])
    if page_id not in holder['links']:
        holder['links'].append(page_id)
        holder['previous_revision'] = holder['revision']
        holder['revision'] = entity_revision(holder)
        registry['pages'][parent] = holder


REGISTERED_KINDS = ('topic', 'work')


def registration_entry(wiki, registry, target, *, kind='topic', summary=''):
    """The page entry explicit registration gives one existing, unregistered wiki file.

    The title is the file's own (its frontmatter title, else its first H1, else its name) and the
    page ID is its path, as adoption named pages. The legacy layout keeps every byte and the
    hand-written header; publication only adds the managed kb region. Nothing links to or from the
    page yet, and a person page is never registered this way.
    """
    from kelmo.store.graph.parser import parse_text
    if not isinstance(target, str) or not valid_wiki_target(target):
        raise KelmoError('E_INPUT', 'Name the page as its wiki path, for example projects/<project>/<page>.md.')
    if target in registry['pages'] or target in registry['redirects']:
        raise KelmoError('E_INPUT', 'Page ' + target + ' is already registered; select it with --target.')
    if target in person_targets(registry):
        raise KelmoError('E_INPUT', 'Page ' + target + ' is a person page; it changes only through an attributable '
                                   'rule-change source.')
    if kind not in REGISTERED_KINDS:
        raise KelmoError('E_INPUT', 'A registered page is a topic or a work page.')
    root = Path(wiki).resolve()
    path = root / target
    if (any(item.is_symlink() for item in (path, *path.parents)) or not path.resolve().is_relative_to(root)
            or not path.is_file()):
        raise KelmoError('E_NOT_FOUND', 'No wiki file exists at ' + target + '; open a new page with --new-work.')
    parsed = parse_text(path.read_bytes())
    if parsed['error']:
        raise KelmoError('E_INPUT', 'Page ' + target + ' has malformed frontmatter (' + str(parsed['error'])
                        + '); repair it by hand, then register it.')
    page_id = target[:-len('.md')].replace('/', '-')
    if not IDENTITY.fullmatch(page_id) or page_id in identity_namespace(registry):
        raise KelmoError('E_INPUT', 'Page ID ' + page_id + ' is invalid or already names a page, narrative or action.')
    title = ' '.join((parsed['title'] or Path(target).stem.replace('-', ' ')).split())
    category = 'areas' if target.startswith('life/') else 'projects' if pages.project_of(target) else 'topics'
    entry = {'kind': kind, 'category': category, 'parent': '', 'title': title, 'summary': summary.strip(),
             'priority': '', 'page_id': page_id, 'layout': 'legacy', 'aliases': [], 'tags': [],
             'repositories': [], 'links': [], 'previous_revision': None, 'compatibility': {}}
    entry['revision'] = entity_revision(entry)
    projected = {name: entry[name] for name in pages.ENTRY_FIELDS}
    projected['revision'] = pages.entry_revision(projected)
    try:
        pages.validate_entry(target, projected)
    except ValueError as error:
        raise KelmoError('E_INPUT', str(error)) from error
    return entry


def apply_registration(registry, entries):
    """The only registry transform explicit registration makes: new legacy page entries, nothing else."""
    result = copy.deepcopy(registry)
    for target, entry in sorted(entries.items()):
        if (target in result['pages'] or target in result['redirects']
                or entry['page_id'] in identity_namespace(result)):
            raise Conflict('page ' + target + ': registration cannot reuse a registered target or page ID')
        if (entry['layout'] != 'legacy' or entry['links'] or entry['parent'] or entry['previous_revision'] is not None
                or entry['category'] == 'you' or entry['revision'] != entity_revision(entry)):
            raise OwnershipConflict('page ' + target + ': registration adds only an unlinked legacy page entry')
        result['pages'][target] = copy.deepcopy(entry)
    return result


def project_title(project):
    """The title a project map gets from its name alone: `demo-a` is `Demo A`."""
    return project.replace('-', ' ').title()


def apply_project_map(registry, project, *, target=None):
    """Add one new map while preserving every pre-existing registry record."""
    if not isinstance(project, str) or not pages.PROJECT.fullmatch(project) or project == 'mandatory':
        raise KelmoError('E_INPUT', 'Invalid or reserved project name.')
    hot_views.validate_output_names(registry, [project])
    target = target or f'projects/{project}/{project}.md'
    if target != f'projects/{project}/{project}.md':
        if project in identity_namespace(registry):
            raise Conflict('Project identity is already in use')
        raise KelmoError('E_INPUT', 'Project map target does not match its name.')
    maps = [path for path, entry in registry['pages'].items()
            if pages.project_of(path) == project and entry.get('layout') == 'project-map']
    if len(maps) == 1:
        return copy.deepcopy(registry)
    if len(maps) > 1:
        raise Conflict('Project has multiple maps')
    existing = registry['pages'].get(target)
    if existing and existing['layout'] == 'project-map' and existing['page_id'] == project:
        return copy.deepcopy(registry)
    if target in registry['pages'] or target in registry['redirects']:
        raise Conflict('Project map target is already registered or redirected')
    if project in identity_namespace(registry):
        raise Conflict('Project identity is already in use')
    result = copy.deepcopy(registry)
    result['pages'][target] = initial_registry(registry['authority_id'], project)['pages'][target]
    return result


def apply_project_rename(registry, old, new):
    """Move project paths while retaining page identities and redirecting every old target."""
    if any(not isinstance(name, str) or not pages.PROJECT.fullmatch(name) or name == 'mandatory'
           for name in (old, new)) or old == new:
        raise KelmoError('E_INPUT', 'Invalid project rename.')
    hot_views.validate_output_names(registry, [new])
    old_map = pages.project_entry(registry, old)
    if not old_map or registry['pages'][old_map].get('layout') != 'project-map':
        raise Conflict('Project has no registered map')
    if any(pages.project_of(target) == new for target in registry['pages']) or new in identity_namespace(registry):
        raise Conflict('Destination project or identity is already in use')
    old_prefix = pages.project_directory(old) + '/'
    new_prefix = pages.project_directory(new) + '/'
    moves = {}
    for target, entry in registry['pages'].items():
        if pages.project_of(target) != old:
            continue
        if entry.get('repositories') or any(action['subject_id'] == entry['page_id']
                                            for action in registry['actions'].values()):
            raise Conflict('Project includes externally enrolled pages; update their enrollment before renaming')
        suffix = target[len(old_prefix):]
        if target == old_map:
            suffix = new + '.md'
        dest = new_prefix + suffix
        if dest in registry['pages'] or dest in registry['redirects'] or dest in moves.values():
            raise Conflict('Destination project path is already registered or redirected')
        moves[target] = dest
    result = copy.deepcopy(registry)
    result['pages'] = {}
    for target, entry in registry['pages'].items():
        entry = copy.deepcopy(entry)
        changed = False
        parent = moves.get(entry['parent'], entry['parent'])
        if parent != entry['parent']:
            entry['parent'] = parent
            changed = True
        # A map still titled from its old name follows the rename; a hand-set title stays.
        if target == old_map and entry['title'] == project_title(old):
            entry['title'] = project_title(new)
            changed = True
        if changed:
            entry['previous_revision'] = entry['revision']
            entry['revision'] = entity_revision(entry)
        result['pages'][moves.get(target, target)] = entry
    for fact in result['facts'].values():
        changed = False
        for key in ('target', 'index_target'):
            if fact.get(key) in moves:
                fact[key] = moves[fact[key]]
                changed = True
        if changed:
            fact['previous_revision'] = fact['revision']
            fact['revision'] = entity_revision(fact)
    for source, dest in moves.items():
        result['redirects'][source] = registry['pages'][source]['page_id']
    _stale_narratives(result)
    validate_registry(result)
    return result, moves


def semantic_projection(vault, registry, intent):
    """The registry and exact page bytes one editor intent produces at the next publication.

    Changes name every page whose rendering differs because of the intent, stale labels included,
    with its current bytes as the baseline. Unmanaged bytes come from the current file unchanged.
    A created page needs an absent file: an existing, even unregistered, page is never claimed.
    """
    config = Config.load(vault)
    result = validate_registry(apply_semantic_intent(registry, intent))
    wiki = config.wiki.resolve()
    _refuse_claimed_files(wiki, registry, result)
    for target in sorted(result['pages'].keys() & registry['pages'].keys()):
        title = registry['pages'][target]['title']
        path = wiki / target
        if result['pages'][target]['title'] != title and (
                not path.is_file() or pages.owned_heading(path.read_bytes().decode('utf-8')) != title):
            raise OwnershipConflict('page ' + target + ': its H1 is hand-written, so a new title would disagree '
                                    'with the published heading; permitted repair: keep the title, or edit the '
                                    'H1 by hand in the page and propose the title that matches it')
    before = render_v3(vault, registry, wiki)
    after = render_v3(vault, result, wiki)
    # The managed-link audit the commit runs, on this projection: an invalid proposal is refused
    # here, before an intent is prepared, and again at publication, never left prepared.
    overlays = dict(after)
    overlays[config.registry] = encoded(result)
    before_links = links.audit_managed(vault, registry, wiki_root=wiki)
    after_links = links.audit_managed(vault, result, wiki, overlays)
    for category in ('broken', 'ambiguous'):
        added = sorted(set(after_links[category]) - set(before_links[category]))
        if added:
            raise SemanticRefusal('The proposal introduces ' + category + ' wiki links: '
                                  + ', '.join(source + ' -> ' + str(link) for source, link in added[:5])
                                  + '; permitted repair: link only registered pages through page links')
    changes = {}
    for path, text in sorted(after.items()):
        if before.get(path) != text:
            changes[path.relative_to(wiki).as_posix()] = (path.read_bytes() if path.exists() else b'',
                                                          text.encode('utf-8'))
    return result, changes


def publication_paths(vault, registry):
    """Every working file the next publication of `registry` reads or may write: each registered page and
    fact page (its unmanaged bytes come from the working file), the registry and the generation manifest."""
    config = Config.load(vault)
    wiki = config.wiki.resolve()
    targets = set(registry['pages']) | {fact['target'] for fact in registry['facts'].values()}
    targets |= {fact['index_target'] for fact in registry['facts'].values() if 'index_target' in fact}
    return [wiki / target for target in sorted(targets)] + [
        config.checked_path(config.registry), config.checked_path(config.registry.with_name('generation.json'))]






def identity_namespace(registry):
    """The shared stable-identity namespace: page, narrative and action IDs."""
    return ({entry['page_id'] for entry in registry['pages'].values()}
            | set(registry['narratives']) | set(registry['actions']))


def _stale_narratives(registry):
    """A narrative whose fact or action dependency changed is stale in the same publication."""
    for narrative in registry['narratives'].values():
        if narrative['state'] != 'current':
            continue
        if any(registry[kind].get(ref, {}).get('revision') != revision
               for kind in ('facts', 'actions') for ref, revision in narrative['depends_on'][kind].items()):
            narrative['state'] = 'stale'
            narrative['previous_revision'] = narrative['revision']
            narrative['revision'] = entity_revision(narrative)
    return registry


def apply_action_intent(registry, intent):
    """The only registry transform an accepted action makes: its record and the narratives it stales."""
    action_id, record = intent['action_id'], intent['record']
    if not isinstance(record, dict) or record.get('action_id') != action_id:
        raise OwnershipConflict('Prepared action intent names a different action')
    if registry['actions'].get(action_id, {}).get('revision') != intent['expected_revision']:
        raise OwnershipConflict('Prepared action intent no longer matches the accepted revision')
    result = copy.deepcopy(registry)
    result['actions'][action_id] = copy.deepcopy(record)
    return _stale_narratives(result)


def _preserve_v3(previous, result, vault, action_operation, semantic_operation=None, registration=None, project_map=None):
    if sum(value is not None for value in (action_operation, semantic_operation, registration, project_map)) > 1:
        raise OwnershipConflict('One publication carries one prepared journal intent')
    if project_map is not None:
        expected = (apply_project_rename(previous, *project_map)[0] if isinstance(project_map, tuple)
                    else apply_project_map(previous, project_map))
        if result != expected:
            raise OwnershipConflict('Project publication differs from its map transform')
        if isinstance(project_map, tuple):
            # The exact transform changes protected facts only by moving their target and
            # revision. The guard still checks every action, mandatory fact and subject.
            subjects = {entry['page_id']: 'feature' for entry in previous['pages'].values()
                        if entry['layout'] == 'feature'}
            subjects.update({action['subject_id']: action['subject_kind']
                             for action in previous['actions'].values()})
            enrollment = {subject: _shown(result, subject, kind) for subject, kind in subjects.items()}
            return ownership_guard(previous, result, rule_change=True, enrollment=enrollment)
        return ownership_guard(previous, result)
    if registration is not None:
        if result != apply_registration(previous, registration):
            raise OwnershipConflict('Registration publication differs from its explicit page entries')
        return ownership_guard(previous, result)
    if action_operation is not None:
        intent = _prepared_intent(vault, action_operation)
        if result != apply_action_intent(previous, intent):
            raise OwnershipConflict('Action publication differs from its exact prepared journal intent')
        return ownership_guard(previous, result, action_id=intent['action_id'])
    if semantic_operation is not None:
        intent = _prepared_intent(vault, semantic_operation, semantic=True)
        if result != apply_semantic_intent(previous, intent):
            raise OwnershipConflict('Semantic publication differs from its exact prepared journal intent')
        return ownership_guard(previous, result, rule_change=intent.get('rule_change') is not None,
                               enrollment=intent.get('feature_enrollment'),
                               subjects=intent.get('enrolled_subjects'))
    ownership_guard(previous, result)
    if result != previous:
        raise KelmoError('E_CAPABILITY', 'The registry is written only by StateAPI intents, prepared editor '
                        'intents and page registration; direct registry writes are refused.')
    return result


def _preserve_metadata(previous, proposed, *, vault=None, action_operation=None, semantic_operation=None,
                       registration=None, project_map=None):
    return _preserve_v3(previous, copy.deepcopy(proposed), vault, action_operation, semantic_operation,
                        registration, project_map)


def _projected_tree(repository, base, changes):
    """Use a private index based on HEAD, never the caller's staged files."""
    with tempfile.TemporaryDirectory(prefix='kelmo-tree-') as directory:
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        environment['GIT_INDEX_FILE'] = str(Path(directory) / 'index')

        def command(*args, content=None, binary=False):
            result = subprocess.run(['git', '--literal-pathspecs', '-C', str(repository), *args], env=environment,
                                    input=content, capture_output=True, check=False)
            if result.returncode:
                raise KelmoError('E_GIT', 'Projected knowledge tree could not be built.')
            return result.stdout if binary else result.stdout.decode('ascii').strip()

        command('read-tree', base)
        relatives = [path.relative_to(repository).as_posix() for path in changes]
        entries = command('ls-tree', '-z', base, '--', *relatives, binary=True)
        modes = {}
        for entry in entries.split(b'\0'):
            if entry:
                header, name = entry.split(b'\t', 1)
                modes[name] = header.split(b' ', 1)[0].decode('ascii')
        blobs, rows = [], []
        removed = []
        for index, (path, text) in enumerate(changes.items()):
            relative = path.relative_to(repository).as_posix()
            mode = modes.get(relative.encode('utf-8'), '100644')
            if mode not in ('100644', '100755'):
                raise Conflict('Managed knowledge baseline is not a regular file')
            if text is None:
                removed.append(relative)
                continue
            blob = Path(directory) / f'blob-{index}'
            blob.write_bytes(text.encode('utf-8'))
            blobs.append(str(blob))
            rows.append((mode, relative))
        if rows:
            objects = command('hash-object', '-w', '--no-filters', '--', *blobs).splitlines()
            if len(objects) != len(rows):
                raise KelmoError('E_GIT', 'Projected knowledge objects could not be verified.')
            updates = [f'{mode} {object_id}\t{relative}\0'.encode('utf-8')
                       for (mode, relative), object_id in zip(rows, objects, strict=True)]
            command('update-index', '-z', '--index-info', content=b''.join(updates))
        if removed:
            command('update-index', '--force-remove', '--', *removed)
        return command('write-tree')


def _generation(config, registry, previous, changes, base):
    root, _, _, relative = pages.generation_paths(config)
    raw = git_blob(root, base + ':' + relative)
    if raw is None:
        generation = {'schema': 1, 'sequence': 0, 'parent_sequence': 0,
                      'work_id': '', 'kind': 'ingest', 'project': '', 'sources': [],
                      'registry_version': registry['version'], 'created_at': '',
                      'publisher': '', 'proposal_digest': '', 'managed_digest': '',
                      'resolves': [], 'forwards': [], 'counts': {}}
    else:
        generation = json.loads(raw)
        # Do not extend a manifest that does not describe the exact committed tree.
        pages.KnowledgeReader(config).generation()
    generation = copy.deepcopy(generation)
    sequence = generation['sequence']
    if type(sequence) is not int or sequence < 0:
        raise Conflict('Invalid committed publication sequence')
    proposal_digest = digest(encoded(registry))
    generation.update(schema=1, sequence=sequence + 1, parent_sequence=sequence,
                      registry_version=registry['version'], created_at=datetime.now(UTC).isoformat(),
                      publisher='kelmo:' + config.label,
                      work_id='core-' + proposal_digest, proposal_digest=proposal_digest,
                      counts={'facts': len(registry['facts']), 'pages': len(registry.get('pages', {})),
                              'notes': len(registry['notes'])})
    new_sources = sorted(set(registry['notes']) - set(previous['notes']))
    if raw is None or new_sources:
        generation['sources'] = new_sources or sorted(registry['notes'])
    content_tree = _projected_tree(root, base, changes)
    generation['managed_digest'] = pages.managed_digest(config, content_tree)
    return encoded(generation), content_tree


def _budget_error(budget):
    detail = '; '.join(budget.get('violations', ())) or 'configured limit exceeded'
    return KelmoError('E_BUDGET', 'Wiki budget exceeded: ' + detail + '. Published facts and accepted actions '
                     'are not retired by hand. A named hub page is limited to '
                     f'{policy.HUB_LINE_LIMIT} lines and {policy.HUB_BYTE_LIMIT} bytes: shorten or move its '
                     'hand-written text; a named HOT chain is limited by the vault\'s configured token limit. '
                     'Nothing was prepared or published; the same request can be retried after the repair.')


def check_budget(vault, registry):
    """Refuse a projection the configured budget would refuse at publication, before any intent.

    Renders exactly what the publisher would render from the current working pages. A page the
    renderer itself refuses (ValueError) is left to the publisher's existing typed refusal.
    """
    config = Config.load(vault)
    try:
        rendered = render_v3(vault, registry, config.wiki)
        budget = policy.check(vault, rendered, wiki_root=config.wiki)
    except OSError as error:
        raise KelmoError('E_IO', 'Knowledge pages could not be read for the budget check; retry.') from error
    if not budget['ok']:
        raise _budget_error(budget)


def require_clean_baseline(vault, registry, *, previous=None):
    """Refuse, with the paths, while the registry, manifest or a registered page has local edits.

    A publication renders every registered page from its working copy, so a local edit there would enter
    the commit unreviewed even on a page no fact changes. The refusal is DirtyKnowledge
    (typed E_CONFLICT); nothing is read from them and nothing is discarded.
    """
    config = Config.load(vault)
    root = config.knowledge_git_root
    if previous is not None:
        _refuse_claimed_files(config.wiki, previous, registry)
    paths = publication_paths(vault, registry)
    relative = sorted({path.relative_to(root).as_posix() for path in paths if path.is_relative_to(root)})
    # Binary output: git() strips text, which would cut the leading status column of the first row.
    status = git(root, 'status', '--porcelain', '--untracked-files=all', '--', *relative, binary=True)
    dirty = [line[3:].split(' -> ')[-1] for line in status.decode('utf-8').splitlines() if line.strip()]
    if dirty:
        raise DirtyKnowledge(dirty)


def _published_mandatory(config):
    reader = pages.KnowledgeReader(config)
    if not reader.adopted():
        return {}
    return {config.wiki / page.path: page.content.decode('utf-8')
            for page in reader.mandatory(reader.generation())}


def validate_project_outputs(config, registry, names):
    mandatory = [path.relative_to(config.wiki).as_posix() for path in _published_mandatory(config)]
    hot_views.validate_output_names(registry, names, mandatory=mandatory)


def _hot_sources(config, rendered):
    """Keep explicitly selected unregistered pages pinned to their committed bytes."""
    sources = _published_mandatory(config)
    sources.update(rendered)
    selection = config.hot or {}
    targets = list(selection.get('pages', []))
    for project in selection.get('projects', {}).values():
        targets.extend(project.get('pages', []))
    reader = None
    for target in dict.fromkeys(targets):
        path = config.wiki / target
        if path in sources:
            continue
        if reader is None:
            reader = pages.KnowledgeReader(config)
            generation = reader.generation()
        selected = reader.page(target, generation)
        if selected.path != target:
            raise KelmoError('E_HOT', 'HOT selection must name an exact published page: ' + target)
        sources[path] = selected.content.decode('utf-8')
    return sources


def _commit_registry(vault, registry, title, wiki_root=None, *, _wiki_locked=False, _initialize=False,
                     _action_operation=None, _semantic_operation=None, _registration=None, _project_map=None,
                     _force=False):
    recover = _recover_transaction if _wiki_locked else recover_transaction
    recover(vault)
    config = Config.load(vault)
    binding = _binding(config)
    if (config.schema_version == 2 or config.wiki != vault / 'knowledge/wiki') and not _initialize:
        if _read_owner(config.wiki).get('vault_id') != config.vault_id:
            raise _owner_error()
    data_repository = repository_root(config.knowledge_git_root)
    if data_repository != config.knowledge_git_root:
        raise Conflict('Knowledge root must match its bound repository root')
    validate_registry(registry)
    if _initialize:
        maps = [entry['page_id'] for entry in registry['pages'].values()]
        if (config.registry.exists() or len(maps) > 1
                or registry != initial_registry(config.vault_id, maps[0] if maps else None)):
            raise Conflict('Explicit empty-store initialization cannot replace existing knowledge')
        previous = initial_registry(config.vault_id)
    else:
        previous = load_registry(vault)
        registry = _preserve_metadata(previous, registry, vault=vault, action_operation=_action_operation,
                                      semantic_operation=_semantic_operation, registration=_registration,
                                      project_map=_project_map)
        validate_registry(registry)
    wiki_root = (Path(wiki_root) if wiki_root is not None else config.wiki).resolve()
    if wiki_root != config.wiki:
        raise Conflict('Publication wiki differs from its bound configuration')
    if registry == previous and not _initialize and not _force:
        return
    # Page frontmatter names the publication, so the generation must live in the data repository.
    if repository_root(wiki_root) != data_repository:
        raise KelmoError('E_CAPABILITY', 'The registry publishes only with its wiki inside the knowledge Git root.')
    if _registration is None:
        # Rechecked here, immediately before writing, so a file that appeared is never claimed.
        # Registration claims exactly the committed files it names; an untracked or edited one
        # is refused below as uncommitted knowledge.
        _refuse_claimed_files(wiki_root, previous, registry)
    rendered = render_v3(vault, registry, wiki_root)
    if isinstance(_project_map, tuple):
        _, moves = apply_project_rename(previous, *_project_map)
        for source, destination in moves.items():
            source_path, destination_path = wiki_root / source, wiki_root / destination
            if any(part.is_symlink() for part in (source_path, *source_path.parents)) or not source_path.is_file():
                raise OwnershipConflict('Original project page is missing or unsafe: ' + source)
            rendered[destination_path] = pages.render_v3_page(
                destination, source_path.read_text(encoding='utf-8'), registry)
            rendered[source_path] = None
    budget = policy.check(vault, rendered, wiki_root=wiki_root)
    if not budget['ok']:
        raise _budget_error(budget)
    rendered[config.registry] = encoded(registry)
    before_links = links.audit_managed(vault, previous, wiki_root=wiki_root)
    after_links = links.audit_managed(vault, registry, wiki_root, rendered)
    for category in ('broken', 'ambiguous'):
        introduced = sorted(set(after_links[category]) - set(before_links[category]))
        if introduced:
            # Name the links and any page whose metadata blocks alias lookup, so the repair is findable.
            detail = ', '.join(f'{source} -> [[{target}]]' for source, target in introduced[:8])
            if after_links['metadata_errors']:
                detail += '; malformed metadata on: ' + ', '.join(after_links['metadata_errors'][:5])
            raise ValueError(f'change introduces invalid wiki links ({category}: {detail})')
    repositories = {'data': data_repository}
    wiki_repository = repository_root(wiki_root)
    if (config.schema_version == 2 and wiki_repository != data_repository
            or config.schema_version == 1 and wiki_repository not in (data_repository, wiki_root)):
        raise Conflict('Wiki repository differs from its bound storage layout')
    wiki_name = 'data' if wiki_repository == repositories['data'] else 'wiki'
    repositories[wiki_name] = wiki_repository
    if wiki_name == 'data' and config.hot and config.hot.get('views'):
        hot_root = wiki_root.parent / 'hot'
        base = git(data_repository, 'rev-parse', 'HEAD')
        generation_file = config.registry.with_name('generation.json').relative_to(data_repository).as_posix()
        raw_generation = git_blob(data_repository, base + ':' + generation_file)
        sequence = json.loads(raw_generation)['sequence'] + 1 if raw_generation else 1
        generated = hot_views.render_all(registry, _hot_sources(config, rendered), wiki_root, hot_root,
                                         config.hot['views']['max_bytes'], generation=sequence, selection=config.hot)
        legacy = hot_views.render_all(previous, _published_mandatory(config), wiki_root, hot_root,
                                     config.hot['views']['max_bytes'])
        prior_names = {hot_root / 'mandatory.md'}
        for name in config.hot['projects']:
            prior_names.update((hot_root / (name + '.md'), hot_root / (name + '.full.md')))
        for target in hot_views.project_maps(previous):
            name = pages.project_of(target)
            prior_names.update((hot_root / (name + '.md'), hot_root / (name + '.full.md')))
        for path in set(generated) | set(legacy) | prior_names:
            if not os.path.lexists(path):
                continue
            if path.is_symlink() or not path.is_file():
                raise OwnershipConflict('Generated HOT path is not a regular file: ' + str(path))
            content = path.read_text(encoding='utf-8')
            owned = (path in (prior_names | set(legacy)) and re.match(r'\A<!-- kelmo generation [0-9]+ -->\n', content)
                     or path in legacy and content == legacy[path]
                     # An unmodified committed view stays recoverable from the base commit, whoever wrote its header.
                     or git_blob(data_repository, base + ':' + path.relative_to(data_repository).as_posix())
                     == content.encode('utf-8'))
            if not owned:
                raise OwnershipConflict('Generated HOT path is not owned by Kelmo: ' + str(path))
            if path not in generated:
                rendered[path] = None
        rendered.update(generated)
    grouped = {}
    transaction_paths = {}
    managed_before = {fact['target'] for fact in previous['facts'].values()}
    managed_before |= {fact['index_target'] for fact in previous['facts'].values()
                       if 'index_target' in fact}
    changed_pages = {}
    dirty = []
    for path, text in rendered.items():
        name = 'data' if path == config.registry else wiki_name
        repository = repositories[name]
        relative = str(path.relative_to(repository))
        status = git(repository, 'status', '--porcelain', '--', relative)
        wiki_relative = str(path.relative_to(wiki_root)) if path.is_relative_to(wiki_root) else None
        first_adoption = (name == 'wiki' and status.startswith('?? ')
                          and START not in (path.read_bytes().decode('utf-8') if path.exists() else '')
                          and wiki_relative not in managed_before)
        if status and not first_adoption:
            dirty.append(relative)
            continue
        if text is None and not path.exists():
            continue
        if text is not None and path.exists() and path.read_bytes() == text.encode('utf-8'):
            continue
        changed_pages[path] = text
        grouped.setdefault(name, []).append(relative)
        transaction_paths[name + ':' + relative] = {
            'repository': name,
            'path': relative,
            'before': path.read_bytes().decode('utf-8') if path.exists() else None,
            'after': text,
        }
    if dirty:
        raise DirtyKnowledge(dirty)
    if not changed_pages:
        return
    heads = {name: git(path, 'rev-parse', 'HEAD') for name, path in repositories.items()}
    if wiki_name == 'data':
        generation_path = config.checked_path(config.registry.with_name('generation.json'))
        relative = generation_path.relative_to(data_repository).as_posix()
        if git(data_repository, 'status', '--porcelain', '--', relative):
            raise DirtyKnowledge([relative])
        text, content_tree = _generation(config, registry, previous, changed_pages, heads['data'])
        changed_pages[generation_path] = text
        grouped.setdefault('data', []).append(relative)
        transaction_paths['data:' + relative] = {
            'repository': 'data', 'path': relative,
            'before': generation_path.read_bytes().decode('utf-8') if generation_path.exists() else None,
            'after': text}
        # Validate the same immutable artifacts readers will consume before recording or committing them.
        projected_tree = _projected_tree(data_repository, content_tree, {generation_path: text})
        pages.KnowledgeReader(config)._load(projected_tree)
    if config.schema_version == 1 and (wiki_name == 'data' or 'wiki' not in grouped):
        legacy_paths = {entry['path']: {'before': entry['before'], 'after': entry['after']}
                        for entry in transaction_paths.values()}
        transaction = {'version': 1, 'base_head': heads['data'],
                       'paths': legacy_paths}
    else:
        transaction = {
            'version': 2,
            'title': title,
            'base_heads': heads,
            'paths': transaction_paths,
        }
    if _binding(Config.load(vault)) != binding:
        raise Conflict('Storage binding changed before knowledge publication')
    transaction['binding'] = binding
    marker_text = encoded(transaction)
    _record_pending(vault, transaction_path(vault), marker_text)
    atomic_write(transaction_path(vault), marker_text)
    try:
        for path, text in changed_pages.items():
            if text is None:
                path.unlink()
                sync_directory(path.parent)
            else:
                atomic_write(path, text)
        order = [name for name in ('wiki', 'data') if name in grouped]
        for name in order:
            paths = grouped[name]
            if git(repositories[name], 'rev-parse', 'HEAD') != heads[name]:
                raise Conflict('Git history changed before knowledge publication')
            git(repositories[name], 'add', '--', *paths)
            for entry in transaction_paths.values():
                if entry['repository'] == name and git_blob(repositories[name], ':' + entry['path']) != (entry['after'].encode('utf-8') if entry['after'] is not None else None):
                    raise Conflict('Git index changed managed publication bytes')
            git(repositories[name], 'commit', '--only', '--no-verify', '-m', title, '--', *paths)
            for entry in transaction_paths.values():
                if entry['repository'] == name and git_blob(repositories[name], 'HEAD:' + entry['path']) != (entry['after'].encode('utf-8') if entry['after'] is not None else None):
                    raise Conflict('Committed knowledge differs from the prepared publication')
        if config.registry.with_name('generation.json') in changed_pages:
            pages.KnowledgeReader(config).generation()
    except BaseException as error:
        try:
            if wiki_name == 'data' or 'wiki' not in grouped:
                _recover_transaction(vault)
            else:
                recover(vault)
        except Exception as recovery_error:
            raise recovery_error from error
        raise
    clear_transaction(vault)


def initialize_registry(vault, project, title):
    """Commit the first registry of an empty store: its first project map and nothing else."""
    vault = Path(vault).resolve()
    config = Config.load(vault)
    registry = initial_registry(config.vault_id, project)
    if config.schema_version == 1 and config.wiki == (vault / 'knowledge/wiki').resolve():
        return _commit_registry(vault, registry, title, config.wiki, _initialize=True)
    owner = _owner_path(config.wiki)
    marker = transaction_path(vault)
    if not owner.exists() and not owner.is_symlink() and not marker.exists() and not marker.is_symlink():
        return _commit_registry(vault, registry, title, config.wiki, _initialize=True)
    with wiki_lock(config.wiki, config.vault_id):
        return _commit_registry(vault, registry, title, config.wiki, _wiki_locked=True, _initialize=True)


def _with_publication_lock(vault, publish):
    config = Config.load(vault)
    if config.schema_version == 1 and config.wiki == (vault / 'knowledge/wiki').resolve():
        return publish(False)
    with wiki_lock(config.wiki, config.vault_id):
        return publish(True)


def publish_action(vault, operation_id):
    """Publish one prepared StateAPI journal intent with its pages and generation; False if already committed."""
    vault = Path(vault).resolve()

    def publish(locked):
        (_recover_transaction if locked else recover_transaction)(vault)
        intent = _prepared_intent(vault, operation_id)
        registry = load_registry(vault)
        if registry['actions'].get(intent['action_id']) == intent['record']:
            return False
        _commit_registry(vault, apply_action_intent(registry, intent),
                         'chore(state): Accept action ' + intent['action_id'], Config.load(vault).wiki,
                         _wiki_locked=locked, _action_operation=operation_id)
        return True
    return _with_publication_lock(vault, publish)


def publish_semantic(vault, operation_id):
    """Publish one prepared editor journal intent; False if its registry is already committed.

    The registry and page bytes are recomputed from the intent and must equal the proposal it
    recorded, baselines included, before the common commit path runs its ownership check.
    """
    vault = Path(vault).resolve()

    def publish(locked):
        (_recover_transaction if locked else recover_transaction)(vault)
        intent = _prepared_intent(vault, operation_id, semantic=True)
        registry = load_registry(vault)
        state = digest(encoded(registry))
        if state == intent['result_digest']:
            return False
        if state != intent['baseline_digest']:
            raise Conflict('Prepared editor intent is stale: its baseline registry changed before publication')
        result, changes = semantic_projection(vault, registry, intent)
        recorded = {change['path']: (change['baseline_sha256'], base64.b64decode(change['content']))
                    for change in intent['proposal']['changes']}
        wiki = Config.load(vault).wiki
        for path, (baseline, _) in recorded.items():
            current = (wiki / path).read_bytes() if (wiki / path).is_file() else b''
            if hashlib.sha256(current).hexdigest() != baseline:
                # The registry and generation are unchanged, so only working-tree edits differ: retryable.
                raise KelmoError('E_CONFLICT', 'Page ' + path + ' differs from the committed bytes the editor '
                                'read; restore the committed bytes, then retry the same request.')
        projected = {path: (hashlib.sha256(before).hexdigest(), after) for path, (before, after) in changes.items()}
        if digest(encoded(result)) != intent['result_digest'] or recorded != projected:
            raise OwnershipConflict('The editor proposal differs from the publisher projection of its prepared intent')
        _commit_registry(vault, result, 'chore(knowledge): Apply editor proposal ' + operation_id[:8],
                         Config.load(vault).wiki, _wiki_locked=locked, _semantic_operation=operation_id)
        return True
    return _with_publication_lock(vault, publish)


def register_pages(vault, entries):
    """Publish explicit registration entries (registration_entry); False when all are already committed.

    The caller holds the worker lock and has finished every prepared intent, so no intent is
    published against a baseline this registration moved.
    """
    vault = Path(vault).resolve()

    def register(locked):
        (_recover_transaction if locked else recover_transaction)(vault)
        registry = load_registry(vault)
        if all(registry['pages'].get(target) == entry for target, entry in entries.items()):
            return False
        names = sorted(entries)
        title = ('chore(knowledge): Register ' + names[0] if len(names) == 1
                 else f'chore(knowledge): Register {len(names)} existing pages')
        _commit_registry(vault, apply_registration(registry, entries), title, Config.load(vault).wiki,
                         _wiki_locked=locked, _registration=entries)
        return True
    return _with_publication_lock(vault, register)


def publish_project_map(vault, project):
    """Publish the map transform; caller holds the coordinator lock and recovered intents."""
    vault = Path(vault).resolve()

    def publish(locked):
        (_recover_transaction if locked else recover_transaction)(vault)
        previous = load_registry(vault)
        result = apply_project_map(previous, project)
        if result == previous:
            config = Config.load(vault)
            root = config.wiki.parent / 'hot'
            names = ['mandatory.md'] + [pages.project_of(target) + suffix
                    for target in hot_views.project_maps(previous) for suffix in ('.md', '.full.md')]
            if not config.hot or not config.hot.get('views'):
                return False
            if all((root / name).is_file() for name in names):
                reader = pages.KnowledgeReader(config)
                sequence = reader.generation().publication_id
                sources = _hot_sources(config, render_v3(vault, previous, config.wiki))
                expected = hot_views.render_all(previous, sources, config.wiki, root,
                                               config.hot['views']['max_bytes'], generation=sequence,
                                               selection=config.hot)
                if all(path.is_file() and path.read_text(encoding='utf-8') == content for path, content in expected.items()):
                    return False
        _commit_registry(vault, result, 'chore(knowledge): Open project ' + project,
                         Config.load(vault).wiki, _wiki_locked=locked, _project_map=project,
                         _force=result == previous)
        return True
    return _with_publication_lock(vault, publish)


def publish_project_rename(vault, old, new):
    """Publish one checked project relocation under the coordinator lock."""
    vault = Path(vault).resolve()

    def publish(locked):
        (_recover_transaction if locked else recover_transaction)(vault)
        previous = load_registry(vault)
        result, _ = apply_project_rename(previous, old, new)
        require_clean_baseline(vault, result, previous=previous)
        _commit_registry(vault, result, 'chore(knowledge): Rename project ' + old + ' to ' + new,
                         Config.load(vault).wiki, _wiki_locked=locked, _project_map=(old, new))
        return True
    return _with_publication_lock(vault, publish)
