"""One orchestration layer over the archive and wiki libraries."""
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from kelmo.store.config import VALID_DOMAINS
from kelmo.store.db import init_schema, open_connection
from kelmo.store.errors import ArchiveEmbeddingError, ArchiveValidationError
from kelmo.store.memory.store import resolve_valid_from

from .config import Config
from .errors import KelmoError, sqlite_busy
from .journal import Journal, now
from .contracts import (CONTRACT_VERSION, INTAKE_BEHAVIOR_VERSION, ISO_DAY,
                        EvidenceRef, Origin, SearchFilters, SourcePacket, iso_day)
from .wiki import engine
from . import fact_checks, outcomes

def chunk_index_for(config):
    """The configured [embedding] index of a vault, or None; builds its provider without inference.
    Shared by Service and the read-only administrator diagnostics, which open no archive writer."""
    settings = config.embedding
    if settings is None:
        return None
    from .archive_adapter import ChunkIndex
    from .embeddings import provider_from_settings
    try:
        return ChunkIndex(settings, provider_from_settings(settings))
    except ArchiveEmbeddingError as error:
        return ChunkIndex(settings, None, unavailable=error.data.get('reason') or 'embedding_unavailable')


ROLES = {'lead', 'collector', 'implementer', 'reviewer', 'unknown'}
_FRONTMATTER_BLOCK = re.compile(r'\A---\n(.*?)\n---\n', re.DOTALL)
# The leading frontmatter block, its body excluded: a field is read from the block alone.
_FRONTMATTER_FIELDS = re.compile(r'\A---\n(.*?)\n---(?:\n|$)', re.DOTALL)
_DECLARED_DOMAIN = re.compile(r'^domain:\s*([A-Za-z0-9_.-]+)\s*$', re.MULTILINE)


def declared_domain(content):
    """The `domain:` a note declares in its leading frontmatter, or None when it declares none.

    One reading shared by the capture that refuses a bad note and the packet that describes an accepted
    one, so a value the archive would reject can never be captured first and fail later."""
    header = _FRONTMATTER_FIELDS.match(content) if isinstance(content, str) else None
    match = _DECLARED_DOMAIN.search(header[1]) if header else None
    return match[1] if match else None


def _note_text(content):
    """A creation source as the editor should read it: one frontmatter block at most, and a body.

    A wrapper that adds frontmatter to text already carrying its own produces two blocks in a row;
    the model then reads the first as the note's body. Two leading blocks merge into one, outer
    fields first, inner fields overriding. A note that is frontmatter alone has nothing to record and
    is refused here, at capture, rather than by the editor at night.
    """
    if not isinstance(content, str):
        return content
    first = _FRONTMATTER_BLOCK.match(content)
    if first is None:
        return content
    rest = content[first.end():].lstrip('\n')
    second = _FRONTMATTER_BLOCK.match(rest)
    if second is not None:
        fields = {}
        for block in (first.group(1), second.group(1)):
            for line in block.splitlines():
                key = line.split(':', 1)[0].strip()
                if key and ':' in line:
                    fields[key] = line
        content = '---\n' + '\n'.join(fields.values()) + '\n---\n\n' + rest[second.end():].lstrip('\n')
        rest = content[_FRONTMATTER_BLOCK.match(content).end():]
    if not rest.strip():
        raise KelmoError('E_INPUT', 'The note carries frontmatter and no text; nothing could be recorded from it.')
    return content
TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}')
DAY = ISO_DAY  # a day only, ASCII digits: times and offsets would compare as text
# Idempotency key namespaces only StateAPI's coordinator may capture under (Service._state_intake).
RESERVED_INTAKE_PREFIXES = ('state-capture/', 'state-intake/')
# How many stopped notes `status` names: enough for a shared client and `memory_status` to see a stretch
# of failures at a glance, small enough to stay one readable page of output.
FAILED_OPERATIONS_REPORT = 20


class Service:
    @staticmethod
    def initialize(root, label, wiki_root=None, *, git_identity=None, project=None):
        root = Path(root).expanduser().resolve()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', label):
            raise KelmoError('E_INPUT', 'Node label must use letters, digits, underscores or hyphens.')
        if (root / 'kelmo.toml').is_file():
            with Service(root) as service:
                if wiki_root is not None and Path(wiki_root).expanduser().resolve() not in (
                        service.config.wiki, service.config.knowledge_git_root):
                    raise KelmoError('E_CONFIG', 'The initialized vault uses a different wiki directory.')
                if service.config.wiki != service.root / 'knowledge/wiki':
                    engine.activate_wiki(service.config.wiki, service.config.vault_id)
                result = dict(initialized=True, existing=True, **service.config.as_dict())
                if label != service.config.label:
                    result['warnings'] = ['Existing node label retained; initialization does not rename a vault.']
                return result
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise KelmoError('E_ROOT_NOT_EMPTY', 'Destination is not empty; initialization will not adopt existing data.')
        vault_id = str(uuid.uuid4())
        knowledge_setting = None
        if wiki_root is not None:
            # A selected wiki is a separate knowledge Git repository: pages under knowledge/wiki, the
            # registry under knowledge/provenance (the schema-2 layout); state and archive stay here.
            selected = Path(wiki_root).expanduser()
            relative_selection = not selected.is_absolute()
            enrolled = engine.configure_wiki(root, selected.absolute(), vault_id=vault_id)
            wiki_root = Path(enrolled['wiki_root'])
            knowledge_root = Path(enrolled['knowledge_root'])
            knowledge_setting = os.path.relpath(knowledge_root, root) if relative_selection else str(knowledge_root)
        root.parent.mkdir(parents=True, exist_ok=True)
        stage_parent = Path(os.environ.get('KELMO_INIT_PARENT', str(root.parent)))
        stage = Path(tempfile.mkdtemp(prefix='.memory-init-', dir=stage_parent))
        try:
            (stage / '.state').mkdir()
            if wiki_root is None:
                (stage / 'knowledge/wiki').mkdir(parents=True)
                config = 'schema_version = 1\nwriter = "local"\nvault_id = ' + json.dumps(vault_id)
                config += '\nnode_label = ' + json.dumps(label) + '\n'
            else:
                config = 'schema_version = 2\nwriter = "local"\nvault_id = ' + json.dumps(vault_id)
                config += '\nnode_label = ' + json.dumps(label) + '\n\n[storage]\n'
                config += 'state_root = ".state"\narchive_root = "archive"\n'
                config += 'knowledge_git_root = ' + json.dumps(knowledge_setting) + '\n'
                config += 'registry_relative = "knowledge/provenance/registry.json"\nwiki_relative = "knowledge/wiki"\n'
            engine.atomic_write(stage / 'kelmo.toml', config)
            if git_identity is not None and (
                    set(git_identity) != {'name', 'email'}
                    or any(not isinstance(value, str) or not value.strip()
                           or '\n' in value or '\0' in value for value in git_identity.values())):
                raise KelmoError('E_GIT_IDENTITY', 'Supply a nonempty Git name and email.')
            # The vault's own repository holds its configuration; a bound knowledge repository is the
            # one selected above, and the identity checks run against whichever publishes.
            repository = stage if wiki_root is None else knowledge_root
            if wiki_root is None:
                engine.atomic_write(stage / '.gitignore', '.state/\narchive/\n.cache/\nknowledge/raw/\n')
                engine.git(stage, 'init', '-q', '-b', 'master')
            if git_identity is not None:
                for field, value in git_identity.items():
                    engine.git(repository, 'config', 'user.' + field, value)
            for key, field in [('GIT_AUTHOR_NAME', 'user.name'), ('GIT_AUTHOR_EMAIL', 'user.email')]:
                if git_identity is None and os.environ.get(key):
                    engine.git(repository, 'config', field, os.environ[key])
            for identity in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
                checked = subprocess.run(['git', '-C', str(repository), '-c', 'user.useConfigOnly=true',
                                          'var', identity],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if checked.returncode:
                    raise KelmoError('E_GIT_IDENTITY', 'Configure Git author and committer identity first.')
            if wiki_root is None:
                engine.git(stage, 'add', '--', 'kelmo.toml', '.gitignore')
                engine.git(stage, 'commit', '-q', '-m', 'chore: Initialize memory vault')
            engine.initialize_registry(stage, project, 'chore: Initialize fact registry')
            with closing(open_connection(str(stage / 'archive/archive.db'))) as connection:
                init_schema(connection)
            Journal(stage / '.state/journal.sqlite3', create=True).close()
            if root.exists() and any(root.iterdir()):
                raise KelmoError('E_ROOT_NOT_EMPTY', 'Destination changed during initialization.')
            stage.replace(root)
            engine.sync_directory(root)
            engine.sync_directory(root.parent)
            if wiki_root is not None:
                engine.activate_wiki(wiki_root, vault_id)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return dict(initialized=True, existing=False, **Config.load(root).as_dict())

    def __init__(self, root, *, archive_api=None, bindings=None, origin=None,
                 resolve_context_binding=None, background=False, context_bindings=None):
        """`background`: shared background work (a worker or daemon under a server profile). It has no
        capture origin of its own, so a capture or forget without an explicit issued origin is refused;
        queued operations keep the origins recorded when they were captured."""
        self.config = Config.load(root)
        self.root = self.config.root
        for relative in ('.state/journal.sqlite3', 'archive/archive.db', 'knowledge/wiki', '.cache/embeddings'):
            self.config.internal(relative)
        if archive_api is None and not self.config.archive.is_file():
            raise KelmoError('E_STATE', 'Archive database is missing; run kelmo doctor.')
        if archive_api is not None and bindings is None:
            raise KelmoError('E_ORIGIN', 'An injected archive requires explicit trusted controller bindings.')
        self.bindings = bindings if bindings is not None else self.config.local_bindings()
        if self.bindings.authority_id != self.config.vault_id:
            raise KelmoError('E_ORIGIN', 'Controller bindings belong to another vault authority.')
        from .context_binding import ContextBindings
        if context_bindings is not None:
            if not isinstance(context_bindings, ContextBindings) or context_bindings.bindings is not self.bindings:
                raise KelmoError('E_BINDING', 'Page enrollment must use this service\'s trusted origin bindings.')
        elif bindings is None:
            # A local vault's owner is its controller: an empty enrollment lets the editor create topic
            # pages, while feature pages and enrolled subjects still need a server's real enrollment.
            context_bindings = ContextBindings(self.bindings, actions=(), repository_remotes={}, worktrees={},
                                               feature_pages={}, producer_client_ids=())
        self.context_bindings = context_bindings
        if background and (origin is not None or archive_api is None):
            raise KelmoError('E_ORIGIN', 'Background work has no capture origin and needs the trusted archive.')
        try:
            if background:
                self.origin = None
            else:
                self.origin = origin if origin is not None else self.bindings.origin('local')
                self.bindings.validate(self.origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', str(error)) from error
        if resolve_context_binding is not None and not callable(resolve_context_binding):
            raise KelmoError('E_BINDING', 'A trusted context resolver must be callable.')
        self._resolve_context_binding = resolve_context_binding
        self.journal = Journal(self.config.state / 'journal.sqlite3')
        try:
            fact_checks.schema(self.journal.conn)
            outcomes.schema(self.journal.conn)
            if archive_api is None:
                from .archive_adapter import LocalArchiveAdapter
                self.archive_api = LocalArchiveAdapter(self.config.archive, self.config.cache, self.bindings,
                                                       connection_factory=open_connection,
                                                       legacy_receipt_verifier=self._legacy_receipt_allowed,
                                                       chunk_index=self._chunk_index())
            else:
                from .contracts import ArchiveCoordinatorAPI
                if not isinstance(archive_api, ArchiveCoordinatorAPI):
                    raise KelmoError('E_CAPABILITY', 'Archive adapter lacks required coordinator/compatibility operations.')
                self.archive_api = archive_api
            self.journal.writer_check = self.archive_api.assert_writer
        except BaseException:
            self.journal.close()
            raise

    def _chunk_index(self):
        """The configured [embedding] index: its declared settings, chunk policy and the one provider built
        from them for this Service, or None for a vault without [embedding] (the accepted record API).

        Building the provider performs no inference. A broker whose channel cannot be opened leaves the
        provider None: text, chunks and FTS stay durable and the vectors stay pending, with no other model
        taking its place."""
        return chunk_index_for(self.config)

    @property
    def knowledge(self):
        """Immutable reads; enrollment can only be supplied by the trusted constructor."""
        from .wiki.pages import KnowledgeReader
        reader = getattr(self, '_knowledge_reader', None)
        if reader is None:
            reader = KnowledgeReader(self.config, resolve_binding=self._resolve_context_binding)
            self._knowledge_reader = reader
        return reader

    @property
    def knowledge_api(self):
        """KnowledgeAPI over the same reader; publish accepts only editor-prepared proposals."""
        from .editor import SemanticKnowledgeService
        api = getattr(self, '_knowledge_api', None)
        if api is None:
            api = self._knowledge_api = SemanticKnowledgeService(self)
        return api

    @property
    def dreaming(self):
        """Bounded source-backed Dreaming; it publishes only through the editor."""
        from .dreaming import Dreaming
        dreaming = getattr(self, '_dreaming', None)
        if dreaming is None:
            dreaming = self._dreaming = Dreaming(self)
        return dreaming

    @property
    def editor(self):
        """Scoped source-backed editing over this journal, worker lock and the single publisher."""
        from .editor import Editor
        editor = getattr(self, '_editor', None)
        if editor is None:
            editor = self._editor = Editor(self)
        return editor

    @property
    def action_coordinator(self):
        """The internal StateAPI integration over this journal, archive and the single publisher."""
        from .publication import StateCoordinator
        coordinator = getattr(self, '_action_coordinator', None)
        if coordinator is None:
            coordinator = self._action_coordinator = StateCoordinator(self)
        return coordinator

    @property
    def archive(self):
        """Local graph/diagnostic compatibility only; never open storage for transport."""
        from .archive_adapter import LocalArchiveAdapter
        if not isinstance(self.archive_api, LocalArchiveAdapter):
            raise KelmoError('E_CAPABILITY', 'Direct archive access is local-only; transport adapters need a snapshot-aware graph capability.')
        return self.archive_api.connection

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Release this Service's two database connections. A long-lived process opens one per worker thread and
        closes each when its thread is done with the vault."""
        try:
            self.archive_api.close()
        finally:
            self.journal.close()

    @staticmethod
    def capture_request_key(idempotency_key):
        """The journal request key a capture with this provided idempotency key is recorded under."""
        return engine.digest('provided:' + idempotency_key)

    @staticmethod
    def _refuse_reserved(idempotency_key):
        if isinstance(idempotency_key, str) and idempotency_key.startswith(RESERVED_INTAKE_PREFIXES):
            raise KelmoError('E_INPUT', 'This idempotency key namespace is reserved for StateAPI intake.')

    def capture(self, content, filename, role='unknown', intent='archive', idempotency_key=None, supersedes=None):
        self._refuse_reserved(idempotency_key)
        return self._capture(content, filename, role, intent, idempotency_key, supersedes)

    def create(self, content, filename='memory.md', *, project, selected=(), rule_change=False,
               budget_tokens=None, idempotency_key=None, new_work=None, pending=None):
        """Capture original text and queue a scoped editor request without hand-written fact blocks.

        A missing budget is the vault's [editor] budget_tokens, so clients and the MCP tool need not know
        how large this vault's mandatory pages are. `new_work` ({slug, title, summary?}) opens one work
        page for the source's open work, created in the same publication as its facts. `pending` is the
        one line agents see at startup while the note waits for the editor."""
        from .creation import request
        self._refuse_reserved(idempotency_key)
        content = _note_text(content)
        # The declared domain is read exactly as the archive packet reads it, and refused here: a note the
        # archive would reject must not become an operation that fails in the archive phase with no reason.
        domain = declared_domain(content)
        if domain is not None and domain not in VALID_DOMAINS:
            raise KelmoError('E_INPUT', 'Source declares domain ' + repr(domain) + '; allowed values: '
                           + ', '.join(sorted(VALID_DOMAINS)) + '.')
        if budget_tokens is None:
            budget_tokens = self.config.editor_budget
        previous = None
        if isinstance(idempotency_key, str) and idempotency_key:
            prior = self.journal.operation_for_request_key(self.capture_request_key(idempotency_key))
            if prior is not None:
                saved = self.journal.source_metadata(prior)
                previous = (saved or {}).get('metadata', {}).get('editor_request')
        selection = request(self, project, selected, rule_change, budget_tokens, new_work, pending,
                            previous=previous)
        return self._capture(content, filename, 'lead', 'create', idempotency_key, None,
                             editor_request=selection)

    def register(self, targets, *, kind='topic', summary='', apply=False):
        """Register existing unregistered wiki files as legacy pages, deterministically and without a model.

        Without `apply` this only reports the entries. With it, the worker lock is taken and every
        prepared intent finishes first, so registration never moves the baseline of one; a target
        already registered is reported and skipped. A file must be committed and unedited.
        """
        from .publication import coordinator_lock, recover_publications
        if (not isinstance(targets, (list, tuple)) or not targets or len(targets) > 100
                or not all(isinstance(target, str) for target in targets)
                or not isinstance(summary, str) or type(apply) is not bool):
            raise KelmoError('E_INPUT', 'Register 1-100 wiki paths with an optional one-line summary.')
        if kind == 'work' and not summary.strip():
            raise KelmoError('E_INPUT', 'A work page needs a summary; pass --summary.')

        def plan():
            registry = engine.load_registry(self.root)
            already, entries = [], {}
            for name in dict.fromkeys(target.strip().lstrip('/') for target in targets):
                target = name if name.endswith('.md') else name + '.md'
                if target in registry['pages']:
                    already.append(target)
                    continue
                entry = engine.registration_entry(self.config.wiki, registry, target, kind=kind, summary=summary)
                twin = next((other for other, page in entries.items() if page['page_id'] == entry['page_id']), None)
                if twin:
                    raise KelmoError('E_INPUT', f'{twin} and {target} would share the page ID {entry["page_id"]}; '
                                               'register one of them.')
                entries[target] = entry
            return registry, already, entries

        if not apply:
            _, already, entries = plan()
            return {'applied': False, 'registered': [], 'already_registered': already, 'entries': entries}
        self.archive_api.assert_writer()
        with coordinator_lock(self.config):
            recover_publications(self)
            registry, already, entries = plan()
            engine.require_clean_baseline(self.root, registry)
            published = bool(entries) and engine.register_pages(self.root, entries)
        return {'applied': published, 'registered': sorted(entries), 'already_registered': already,
                'entries': entries}

    def capture_source(self, source, origin, idempotency_key, *, compat=None):
        """Capture an unchanged typed source from an issued controller principal.

        `compat` keeps caller metadata the typed packet has no field for (an old raw_note_path)
        verbatim beside the source. It is opaque text: nothing here opens or resolves it.
        """
        self._refuse_reserved(idempotency_key)
        return self._typed_capture(source, origin, idempotency_key, compat=compat)

    def _state_intake(self, source, origin, idempotency_key):
        """StateAPI's own intake capture; the only path into the reserved key namespaces."""
        if not isinstance(idempotency_key, str) or not idempotency_key.startswith(RESERVED_INTAKE_PREFIXES):
            raise KelmoError('E_INPUT', 'StateAPI intake requires its reserved idempotency key namespace.')
        return self._typed_capture(source, origin, idempotency_key)

    def _typed_capture(self, source, origin, idempotency_key, compat=None):
        try:
            self.bindings.validate(origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'Source origin is not an issued controller principal.') from error
        try:
            if not isinstance(source, SourcePacket):
                raise ValueError('Use a SourcePacket.')
            source = SourcePacket.from_dict(source.to_dict())
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError('A typed source requires an idempotency key.')
            if compat is not None and (not isinstance(compat, dict) or not compat or set(compat) - {'raw_note_path'}
                                       or not all(isinstance(value, str) and value and len(value) <= 4096
                                                  and '\x00' not in value for value in compat.values())):
                raise ValueError('Compatibility metadata must be short opaque text.')
        except (ValueError, TypeError) as error:
            raise KelmoError('E_INPUT', 'Invalid typed source or idempotency key.') from error
        return self._capture(source.content.decode('utf-8'), source.filename, 'implementer',
                             'archive', idempotency_key, None, source_packet=source, origin=origin,
                             compat=dict(compat) if compat else None)

    def compat_metadata(self, archive_id):
        """Verbatim compatibility metadata recorded by the first capture that owns this record, if any."""
        for row in self.journal.conn.execute('SELECT id FROM operations WHERE archive_id=? ORDER BY created_at, id',
                                             (archive_id,)):
            saved = self.journal.source_metadata(row['id'])
            if saved and saved['metadata'].get('compat'):
                return dict(saved['metadata']['compat'])
        return None

    def _capture(self, content, filename, role, intent, idempotency_key, supersedes,
                 *, source_packet=None, origin=None, compat=None, editor_request=None):
        if self.journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Source capture requires an idle journal connection.')
        self.archive_api.assert_writer()
        capture_origin = self.origin if origin is None else origin
        try:
            self.bindings.validate(capture_origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'Source origin is not an issued controller principal.') from error
        self.config.internal('.state/sources')
        if role not in ROLES or intent not in {'archive', 'promote', 'create'}:
            raise KelmoError('E_INPUT', 'Unknown role or operation intent.')
        if intent == 'create' and (role != 'lead' or editor_request is None):
            raise KelmoError('E_INPUT', 'Use kelmo create to select the editor scope.')
        if intent == 'promote' and role != 'lead':
            raise KelmoError('E_PERMISSION', 'Only a declared lead may request fact promotion.')
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 1048576:
            raise KelmoError('E_INPUT', 'Source must contain text within the one MiB intake limit.')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,239}\.md', filename):
            raise KelmoError('E_INPUT', 'Source filename must be a simple Markdown filename.')
        if intent == 'promote':
            try:
                changes = engine.extract_changes(content)
            except ValueError as error:
                raise KelmoError('E_INPUT', 'Invalid wiki changes: ' + str(error)) from error
            if changes is None:
                raise KelmoError('E_INPUT', 'Promotion requires a valid wiki-changes block.')
        if intent == 'promote':
            fact_checks.validate(self.journal.conn, engine.load_registry(self.root), content)
        source_hash = engine.digest(content)
        if supersedes is not None:
            if not isinstance(supersedes, str) or not supersedes.strip():
                raise KelmoError('E_INPUT', 'Provide the archive record id this source supersedes.', 2)
            if intent == 'promote':
                raise KelmoError('E_INPUT', 'Supersedes applies to archive captures; fact promotion uses replaces.', 2)
        # A replacement is a different request than the same bytes stored plainly; plain keys stay as they were.
        identity = [source_hash, filename, role, intent] + ([supersedes] if supersedes else [])
        if editor_request is not None:
            identity.append(editor_request)
        fingerprint = engine.digest(json.dumps(identity))
        request_key = self.capture_request_key(idempotency_key) if idempotency_key else fingerprint
        with self.config.internal('.state/capture.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prior = self.journal.conn.execute('SELECT id FROM operations WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                value = self.journal.get(prior['id'])
                if (value['source_hash'], value['filename'], value['role'], value['intent'], value['supersedes']) != (source_hash, filename, role, intent, supersedes):
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies different input.')
                recorded = self.journal.origin_record(value['id'])
                if recorded is not None and recorded != capture_origin.to_dict():
                    raise KelmoError('E_IDEMPOTENCY', 'The request belongs to a different trusted origin.')
                saved_source = self.journal.source_metadata(value['id'])
                saved_request = saved_source['metadata'].get('editor_request') if saved_source else None
                compared_request = editor_request
                if saved_request is not None and editor_request is not None:
                    from .creation import current_request
                    registry = engine.load_registry(self.root)
                    saved_request = current_request(registry, saved_request)
                    compared_request = current_request(registry, editor_request)
                if saved_request != compared_request:
                    raise KelmoError('E_IDEMPOTENCY', 'The request already selects a different editor scope.')
                typed_capture = saved_source and saved_source['metadata'].get('capture_kind') == 'typed'
                if bool(typed_capture) != (source_packet is not None):
                    raise KelmoError('E_IDEMPOTENCY', 'The request uses a different source capture contract.')
                if source_packet is not None and self.source_packet(value['id']) != source_packet:
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies different source metadata.')
                if (saved_source['metadata'].get('compat') if saved_source else None) != compat:
                    raise KelmoError('E_IDEMPOTENCY', 'The idempotency key already identifies different source metadata.')
                return value
            if supersedes is not None:
                self._refuse_hidden_target(supersedes)
                replaced = self.archive_record(supersedes)
                if replaced['raw_note_path'] == source_hash + '-' + filename:
                    raise KelmoError('E_INPUT', 'This source is the record it would supersede; nothing would change.', 2)
            created_at = now()
            original = source_packet
            if original is None:
                # Validated before the spool write, so a refused capture leaves no orphan source.
                try:
                    original = self._legacy_source_packet(
                        {'source_hash': source_hash, 'filename': filename, 'created_at': created_at,
                         'role': role, 'supersedes': supersedes}, content)
                except (ArchiveValidationError, ValueError) as error:
                    raise KelmoError('E_INPUT', 'Source metadata derived from this capture is invalid: '
                                    + str(error)) from error
            if editor_request is not None:
                original = replace(original, project=editor_request['project'])
            relative = Path('.state/sources') / (source_hash + '.md')
            path = self.config.internal(relative)
            if path.exists():
                if path.is_symlink() or path.read_bytes().decode('utf-8') != content:
                    raise KelmoError('E_SOURCE', 'Existing immutable source no longer matches its hash.')
            else:
                engine.atomic_write(path, content)
            engine.sync_file(path)
            operation_id = str(uuid.uuid4())
            metadata = original.to_dict()
            del metadata['content']
            state = 'captured' if role in {'collector', 'unknown'} else 'queued'
            with self.journal.conn:
                self.journal.conn.execute('''INSERT INTO operations
                    (id,request_key,source_hash,source_path,filename,role,intent,state,created_at,supersedes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', (operation_id, request_key, source_hash,
                    relative.as_posix(), filename, role, intent, state, created_at, supersedes))
                self.journal.bind_origin(operation_id, capture_origin)
                recorded = {'packet': metadata, 'capture_kind': 'typed' if source_packet else 'legacy'}
                if compat:
                    recorded['compat'] = compat
                if editor_request is not None:
                    recorded['editor_request'] = editor_request
                self.journal.bind_source(operation_id, recorded, self._packet_digest(original))
                details = {'role': role, 'intent': intent}
                if supersedes:
                    details['supersedes'] = supersedes
                self.journal.event(operation_id, 'captured', **details)
                if state == 'queued':
                    self.journal.event(operation_id, 'queued')
            return self.journal.get(operation_id)

    def _read_registry(self):
        """Current-store reads use committed knowledge, never an unpublished working edit."""
        reader = self.knowledge
        if reader.adopted():
            return reader.registry(reader.generation())
        raise KelmoError('E_GENERATION', 'Publish or recover the initial knowledge generation before reading it.')

    def reconcile(self, text, target):
        return fact_checks.search(self.journal.conn, self._read_registry()['facts'], text, target)

    def page_index(self, projects=None):
        """The discovery view of the page model. `version` is named so an empty list is never read as a store
        that has no pages."""
        from .wiki import pages
        registry = self._read_registry()
        try:
            return {'version': registry['version'], 'projects': pages.page_index(registry, projects)}
        except ValueError as error:
            raise self._page_refusal(error) from error

    def page_facts(self, target):
        """Exact read of one page: entry, derived status and every fact on it, revisions included.

        `page` is null when the store has no page model, and a page with no entry is answered with
        `entry: null` and its facts: a missing entry is a fact about the page, not a reason to return nothing.
        """
        from .wiki import pages
        registry = self._read_registry()
        try:
            return {'version': registry['version'], 'page': pages.page_facts(registry, target)}
        except ValueError as error:
            raise self._page_refusal(error) from error

    def editor_context(self, project, budget_tokens, selected=()):
        """The bounded view of one project for one editor call; what does not fit is named, never dropped."""
        from .wiki.context import editor_context
        try:
            return editor_context(self._read_registry(), project, budget_tokens, selected)
        except ValueError as error:
            raise self._page_refusal(error) from error

    @staticmethod
    def _page_refusal(error):
        """A page-model refusal reaches the CLI as E_INPUT carrying its own repair.

        The wiki half already names the bad argument and the permitted repair; the CLI's ValueError path
        replaces a ValueError's sentence with 'Input or local storage could not be processed.', so without
        this the caller of `kelmo context --budget-tokens 0` is told nothing about what it got wrong.
        """
        return KelmoError('E_INPUT', str(error), 2)

    def _snapshot_graph(self, operation, params=None):
        """A restricted archive builds its graph only from an allowlisted committed snapshot."""
        if not getattr(self.archive_api, 'snapshot_graph', False):
            return None
        from .server import snapshot_graph
        return snapshot_graph(self, operation, params)

    def graph_query(self, **kwargs):
        from .graph import Graph
        shared = self._snapshot_graph('query', kwargs)
        return shared if shared is not None else Graph(self).query(**kwargs)

    def graph_stats(self):
        from .graph import Graph
        shared = self._snapshot_graph('stats')
        return shared if shared is not None else Graph(self).stats()

    def graph_rebuild(self):
        from .graph import Graph
        shared = self._snapshot_graph('rebuild')
        return shared if shared is not None else Graph(self).rebuild()

    def graph_read(self, entity_id):
        from .graph import Graph
        shared = self._snapshot_graph('read', {'entity_id': entity_id})
        return shared if shared is not None else Graph(self).read(entity_id)

    def propose_archived_note(self, operation_id, changes, checks, application_id):
        """Apply a session's checked candidate facts to the wiki, from its archived whole note.

        The registry has one semantic writer, the scoped editor: the saved proposal answers the
        editor as declared changes (no model call), and the editor's own receipts gate, ownership guard,
        baseline check and publication apply. The worker lock is released before the editor runs, because
        the editor takes that same lock.
        """
        from .resolution import worker_lock
        with worker_lock(self):
            operation = self.journal.get(operation_id)
            if operation['durable_stage'] != 'archived' or not operation['archive_id']:
                raise KelmoError('E_PERMISSION', 'A completed archive receipt is required before wiki changes.')
            source = self.source_path(operation).read_bytes().decode('utf-8')
            archived = self.archive_record(operation['archive_id'], operation_id=operation_id)
            if engine.digest(source) != operation['source_hash'] or archived['content'] != source:
                raise KelmoError('E_SOURCE', 'Archived note no longer matches its source.')
            engine.recover_transaction(self.root)
            registry = engine.load_registry(self.root)
            proposal_hash = engine.digest(engine.encoded({'changes':changes,'checks':checks}))
            from . import promotion
            try:
                engine.validate_changes(changes)
                body = ('```wiki-changes\n' + json.dumps(changes) + '\n```\n```fact-checks\n'
                        + json.dumps(checks) + '\n```\n')
                request = promotion.declared_request(registry, body, None)
            except (ValueError, engine.Conflict, KelmoError) as error:
                raise KelmoError('E_PROPOSAL', 'Session proposal is invalid or stale: ' + str(error)) from error
        return self._publish_session_proposal(operation_id, request, application_id, proposal_hash)

    def _publish_session_proposal(self, operation_id, request, application_id, proposal_hash):
        """One editor request per session application: the key is the application identity and the
        fingerprint carries the proposal digest, so a different proposal under the same identity is
        refused (E_IDEMPOTENCY) and a crash after publication replays the finished receipt without a
        second publication. Automatic session facts never carry rule-change authority."""
        from . import promotion
        from .publication import StateCoordinator
        origin = self._operation_origin(operation_id)
        row = self.journal.action_request(StateCoordinator._request_key('semantic', origin, application_id))
        replayed = row is not None and row['state'] == 'finished'
        receipt = self.editor.edit(operation_id, origin=origin, idempotency_key=application_id,
                                   project=request['project'], selected=request['selected'],
                                   executor=promotion.DeclaredChanges(request['proposal']),
                                   budget_tokens=promotion.BUDGET_TOKENS, rule_change=False,
                                   contexts=self.context_bindings, proposal_digest=proposal_hash)
        return {'applied': receipt.outcome == 'published', 'replayed': replayed}

    def source_path(self, operation):
        """Resolve durable source identity only inside the currently selected vault."""
        source_hash = operation['source_hash']
        if not isinstance(source_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', source_hash):
            raise KelmoError('E_SOURCE', 'Invalid captured source identity.')
        relative = Path('.state/sources') / (source_hash + '.md')
        recorded = Path(operation['source_path'])
        # Older journals stored absolute locations; only their canonical suffix is portable.
        valid = (recorded.parts[-3:] == relative.parts if recorded.is_absolute()
                 else recorded == relative)
        if not valid or '..' in recorded.parts:
            raise KelmoError('E_SOURCE', 'Source path does not match its immutable identity.')
        try:
            return self.config.internal(relative)
        except KelmoError as error:
            raise KelmoError('E_SOURCE', 'Source path is unsafe.') from error

    @staticmethod
    def _packet_digest(source):
        return engine.digest(json.dumps(source.to_dict(), sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False))

    def source_packet(self, operation_id):
        """Recover the packet owned by this operation, including its original metadata."""
        operation = self.journal.get(operation_id)
        content = self.source_path(operation).read_bytes()
        if engine.digest(content.decode('utf-8')) != operation['source_hash']:
            raise KelmoError('E_SOURCE', 'Captured source hash no longer matches.')
        stored = self.journal.source_metadata(operation_id)
        if stored is None:
            try:
                return self._legacy_source_packet(operation, content.decode('utf-8'))
            except (ArchiveValidationError, ValueError) as error:
                # A journal from before typed metadata: its source is kept, never re-described.
                raise KelmoError('E_CAPABILITY', 'Legacy operation ' + operation_id + ' has no recorded source '
                                'metadata and its filename cannot describe a valid source (' + str(error) + '). '
                                'Its captured source is retained; repair requires capturing it again under '
                                'a valid filename.') from error
        try:
            packet = SourcePacket.from_dict(dict(stored['metadata']['packet'],
                                                content=base64.b64encode(content).decode('ascii')))
            if (packet.filename != operation['filename']
                    or self._packet_digest(packet) != stored['packet_sha256']):
                raise ValueError('Source metadata identity changed.')
        except (ValueError, TypeError, KeyError) as error:
            raise KelmoError('E_SOURCE', 'Original source metadata no longer matches its recorded identity.') from error
        return packet

    def _legacy_source_packet(self, operation, content):
        header = re.match(r'\A---\n(.*?)\n---(?:\n|$)', content, re.DOTALL)
        def field(name, fallback):
            match = re.search(r'^' + name + r':\s*([A-Za-z0-9_.-]+)\s*$', header[1], re.MULTILINE) if header else None
            return match[1] if match else fallback
        listed = re.search(r'^tags:\s*\[([^\]\n]*)\]\s*$', header[1], re.MULTILINE) if header else None
        declared = [item.strip().strip('\'"') for item in listed[1].split(',')] if listed else []
        tags = [self.config.label, 'role:' + operation['role']]
        # role: and the node label are set by the service; a source cannot claim them.
        tags += [tag for tag in declared if TAG.fullmatch(tag) and tag not in tags
                 and not tag.startswith('role:') and tag != self.config.label]
        return SourcePacket(content.encode('utf-8'), 'raw_note', operation['source_hash'],
                            operation['filename'], operation['created_at'], kind='event',
                            domain=declared_domain(content) or 'dev', project=field('project', 'default'),
                            tags=tuple(tags), valid_from=resolve_valid_from(None, 'raw_note', operation['filename']),
                            require_vectors=bool(operation.get('supersedes')))

    def _archive(self, operation, content):
        source = self.source_packet(operation['id'])
        if source.content != content.encode('utf-8'):
            raise KelmoError('E_SOURCE', 'Archive input differs from the captured source.')
        origin = self._operation_origin(operation['id'])
        ref = self.archive_api.capture(source, origin, operation['id'])
        version = self.archive_api.verify_receipt(ref, operation['id'], origin)
        return {'id': ref.archive_id, 'created_at': version.stored_at, 'valid_from': version.source.valid_from,
                'evidence_ref': ref.to_dict(), 'vectors_pending': version.vectors_pending}

    def _operation_origin(self, operation_id):
        recorded = self.journal.origin_record(operation_id)
        if recorded is None:
            # Old local journals had no origin row. Never import a payload claim here.
            try:
                return self.bindings.origin('local')
            except ValueError as error:
                raise KelmoError('E_ORIGIN', 'This operation has no recorded origin, and these bindings enroll '
                                            'no local client to own it.') from error
        try:
            audit = Origin.from_dict(recorded)
            trusted = self.bindings.origin(audit.client_id, audit.session_id)
            if audit != trusted:
                raise ValueError('Stored origin differs from current enrollment.')
            return trusted
        except ValueError as error:
            raise KelmoError('E_ORIGIN', str(error)) from error

    def _legacy_receipt_allowed(self, ref, operation_id, origin):
        """Internal upgrade admission, checked against existing durable coordinator state."""
        operation = self.journal.get(operation_id)
        if operation['durable_stage'] not in ('archived', 'applied') or operation['archive_id'] != ref.archive_id:
            return False
        content = self.source_path(operation).read_bytes().decode('utf-8')
        return (ref.version == 1 and ref.source_id == operation['source_hash']
                and ref.content_sha256 == operation['source_hash'] == engine.digest(content)
                and origin == self._operation_origin(operation_id))

    def _stage_failure(self, operation_id, code, error, phase, claim_token):
        """Stage a failed attempt and record why it stopped, so a trace explains itself.

        The archive phases fail before any outcome exists, so without a reason here the only trace left
        is an error code and a phase name in the event log, and the session that wrote the note has
        already moved on. A failure of the outcome store itself cannot be written into it."""
        outcome = None if code == 'E_OUTCOME' else ('unresolved', outcomes.repair_reason(code, error))
        self.journal.stage(operation_id, 'failed', terminal=True, error_code=code,
                           error_type=type(error).__name__, phase=phase, claim_token=claim_token,
                           outcome=outcome)

    def run(self, operation_id, *, executor=None):
        with self.config.internal('.state/worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'Another worker is processing this vault.') from error
            self.archive_api.assert_writer()
            operation = self.journal.get(operation_id)
            if operation['terminal'] or operation['state'] in ('captured', 'needs_resolution', 'needs_review'):
                return operation
            claim_token = self.journal.claim(operation_id, local_worker=True)
            if claim_token is None:
                raise KelmoError('E_BUSY', 'This job is claimed by another attempt.')
            phase = 'source'
            editor_request = None
            try:
                path = self.source_path(operation)
                content = path.read_bytes().decode('utf-8')
                if engine.digest(content) != operation['source_hash']:
                    raise KelmoError('E_SOURCE', 'Captured source hash no longer matches.')
                phase = 'archive'
                if operation['archive_id']:
                    receipt = self.archive_record(operation['archive_id'], operation_id=operation_id)
                    if receipt['content'] != content:
                        raise KelmoError('E_SOURCE', 'Archive receipt no longer matches the captured source.')
                else:
                    saved = self.archive_api.capture_receipt(operation_id, self._operation_origin(operation_id))
                    if operation.get('supersedes') and saved is None:
                        # Checked before the archive too: a refused correction must not become a visible record.
                        phase = 'supersede'
                        self._refuse_hidden_target(operation['supersedes'])
                        phase = 'archive'
                    receipt = self._archive(operation, content)
                if not receipt.get('evidence_ref'):
                    raise KelmoError('E_RECEIPT', 'Archive capture did not return an exact evidence reference.')
                if operation.get('supersedes'):
                    phase = 'supersede'
                    self._supersede(operation_id, receipt['id'], operation['supersedes'])
                if operation['state'] != 'archived' or operation['intent'] == 'archive':
                    self.journal.stage(operation_id, 'archived', archive_id=receipt['id'],
                                       terminal=operation['intent'] == 'archive', claim_token=claim_token,
                                       evidence_ref=receipt['evidence_ref'],
                                       vectors_pending=receipt.get('vectors_pending'),
                                       outcome=('archive-only', 'captured as evidence; no fact promotion was requested')
                                       if operation['intent'] == 'archive' else None)
                if operation['intent'] == 'promote':
                    phase = 'wiki_prepare'
                    if operation['role'] != 'lead':
                        raise KelmoError('E_PERMISSION', 'Child evidence cannot publish facts.')
                    phase = 'wiki_recovery'
                    engine.recover_transaction(self.root)
                    phase = 'wiki_prepare'
                    registry = engine.load_registry(self.root)
                    fact_checks.validate(self.journal.conn, registry, content)
                    # The registry is written by the scoped editor alone; the declared facts
                    # reach it after this lock, which the editor takes itself.
                    from . import promotion
                    phase = 'editor'
                    editor_request = promotion.declared_request(
                        registry, content, self.source_packet(operation_id).project)
                elif operation['intent'] == 'create':
                    editor_request = self.journal.source_metadata(operation_id)['metadata']['editor_request']
            except Exception as error:  # noqa: BLE001 - Preserve failed operations across backend faults.
                if isinstance(error, KelmoError) and error.code == 'E_CLAIM':
                    raise
                if isinstance(error, KelmoError):
                    code = error.code
                elif isinstance(error, ArchiveEmbeddingError):
                    code = 'E_EMBEDDING'
                elif isinstance(error, ArchiveValidationError):
                    code = 'E_INPUT'
                elif isinstance(error, engine.Conflict):
                    code = 'E_CONFLICT'
                elif sqlite_busy(error):
                    # Another writer holds the database: the operation waits its turn, never charged.
                    code = 'E_BUSY'
                else:
                    code = 'E_PROCESSING'
                if code in ('E_FACT_CHECK', 'E_CONFLICT') and phase == 'wiki_prepare':
                    from .resolution import Resolution
                    try:
                        Resolution(self).create(self.journal.get(operation_id), content)
                    except (KelmoError, FileNotFoundError) as resolution_error:
                        resolution_code = ('E_SOURCE' if isinstance(resolution_error, FileNotFoundError)
                                           else resolution_error.code)
                        if resolution_code == 'E_CLAIM':
                            raise
                        # The first failure stays in the outcome; the stage names why no resolution exists.
                        unresolved = None if resolution_code == 'E_SOURCE' else (
                            'unresolved', outcomes.repair_reason(
                                resolution_code, f'resolution not created: {resolution_error} '
                                                 f'(after {code}: {error})'))
                        self.journal.stage(operation_id, 'failed', terminal=True,
                                           error_code=resolution_code,
                                           error_type=type(resolution_error).__name__,
                                           phase='resolution_create', claim_token=claim_token,
                                           outcome=unresolved)
                        return self.journal.get(operation_id)
                    self.journal.stage(operation_id, 'needs_resolution', error_code=code, claim_token=claim_token)
                    outcomes.record(self.journal.conn, operation_id, 'unresolved', outcomes.repair_reason(code, error))
                    return self.journal.get(operation_id)
                self._stage_failure(operation_id, code, error, phase, claim_token)
            finally:
                self.journal.release(operation_id, claim_token)
            if editor_request is None:
                return self.journal.get(operation_id)
        if operation['intent'] == 'create':
            from .creation import publish
            return publish(self, operation_id, editor_request, executor)
        return self._promote_through_editor(operation_id, editor_request)

    def _promote_through_editor(self, operation_id, request):
        """Publish one archived promote note through the scoped editor, outside the worker lock.

        The editor takes the worker lock itself, so this runs after `run` released it. The attempt
        holds an external claim, which keeps a concurrent worker off the operation, while the editor's
        request row serializes identical attempts. A refusal fails the operation under the editor's own
        code in phase `editor`; `retry` re-queues it. The next attempt resumes the recorded editor
        request while it is open, prepared or finished (an environment failure after preparation, a
        crash, a recovery that already published it) and opens a new request only after a refusal.
        """
        from . import promotion
        from .publication import StateCoordinator
        claim_token = self.journal.claim(operation_id, lease_seconds=3600)
        if claim_token is None:
            raise KelmoError('E_BUSY', 'This job is claimed by another attempt.')
        try:
            operation = self.journal.get(operation_id)
            origin = self._operation_origin(operation_id)
            key = promotion.request_key(
                operation_id, self.trace(operation_id)['events'],
                existing=lambda key: self.journal.action_request(StateCoordinator._request_key('semantic', origin, key)))
            try:
                self.editor.edit(operation_id, origin=origin, idempotency_key=key,
                                 project=request['project'], selected=request['selected'],
                                 executor=promotion.DeclaredChanges(request['proposal']),
                                 budget_tokens=promotion.BUDGET_TOKENS, rule_change=request['rule_change'],
                                 contexts=self.context_bindings)
            except Exception as error:  # noqa: BLE001 - Preserve failed operations across backend faults.
                if isinstance(error, KelmoError) and error.code in ('E_BUSY', 'E_CLAIM'):
                    raise
                code = error.code if isinstance(error, KelmoError) else 'E_PROCESSING'
                # A refusal raised before the editor opened its request row has no recorded reason yet;
                # a failed outcome store cannot hold one, so that failure is staged without it.
                self._stage_failure(operation_id, code, error, 'editor', claim_token)
                return self.journal.get(operation_id)
            self.journal.stage(operation_id, 'applied', terminal=True, archive_id=operation['archive_id'],
                               claim_token=claim_token)
            return self.journal.get(operation_id)
        finally:
            self.journal.release(operation_id, claim_token)

    def _refuse_hidden_target(self, superseded_id):
        """A correction replaces the visible record of its topic. A target already forgotten, by an earlier
        correction or by hand, would leave two visible heads; the refusal names the current one."""
        self.archive_api.refuse_hidden_target(superseded_id)

    def _supersede(self, operation_id, record_id, superseded_id):
        """Link the archived replacement and forget the record it replaces; repeating it changes nothing."""
        if self.journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal event reconciliation requires an idle connection.')
        origin = self._operation_origin(operation_id)
        self.archive_api.supersede(record_id, superseded_id, origin)
        self._forget(superseded_id, origin, reason='superseded by ' + record_id,
                     idempotency_key='supersede/' + operation_id)
        self.journal.event_once(operation_id, 'superseded', check_writer=self.archive_api.assert_writer,
                                record_id=record_id, superseded_id=superseded_id)

    def _settled(self, operation):
        return outcomes.promotion_outcome(engine.load_registry(self.root)['facts'], operation['source_hash'])

    def retry(self, operation_id, *, reset_budget=True):
        if self.journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal retry requires an idle connection.')
        self.archive_api.assert_writer()
        with self.config.internal('.state/worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'Another worker is processing this vault.') from error
            operation = self.journal.get(operation_id)
            if operation['state'] != 'failed':
                raise KelmoError('E_STATE', 'Only a failed operation can be retried.')
            self.journal.stage(operation_id, 'queued')
            retries = reset_budget and self.journal.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_retries'").fetchone()
            # A source whose editor request already finished is settled: its facts are published and the
            # retry only records that. Any other source is unsettled again until the attempt settles it.
            published = any(row['state'] == 'finished' for row in self.journal.semantic_requests(operation_id))
            with self.journal.conn:
                if not published:
                    outcomes.clear(self.journal.conn, operation_id)
                if reset_budget:
                    # The event is the boundary of the note's editor attempt budget: `kelmo queue retry`
                    # renews its parked attempts and its free uncharged refusals, and the editor counts
                    # from here. The daemon's charged requeue passes reset_budget=False, so its own retry
                    # budget stays and no attempt budget is renewed by it.
                    self.journal.event(operation_id, 'retry_budget_reset')
                if retries:
                    self.journal.conn.execute('DELETE FROM worker_retries WHERE operation_id=?', (operation_id,))
            return self.journal.get(operation_id)

    def queued(self):
        return [row['id'] for row in self.journal.conn.execute(
            "SELECT id FROM operations WHERE state IN ('queued','archived') AND terminal=0 ORDER BY created_at")]

    def queue(self):
        operations = [self.journal.get(row['id']) for row in self.journal.conn.execute(
            "SELECT id FROM operations WHERE terminal=0 OR state='failed' ORDER BY created_at")]
        if self.journal.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions'").fetchone():
            resolutions = {row['operation_id']: row for row in self.journal.conn.execute(
                "SELECT id,operation_id,state FROM resolutions WHERE state!='applied'")}
            for operation in operations:
                resolution = resolutions.get(operation['id'])
                if resolution:
                    operation['resolution_id'] = resolution['id']
                    operation['resolution_state'] = resolution['state']
        return operations

    def orphan_sources(self):
        known = {row[0] for row in self.journal.conn.execute('SELECT source_hash FROM operations')}
        return sum(path.stem not in known for path in self.config.internal('.state/sources').glob('*.md'))

    def trace(self, operation_id):
        return self.journal.trace(operation_id)

    def failed_operations(self, limit=FAILED_OPERATIONS_REPORT):
        """The notes that stopped failing, newest first: what a shared client and `memory_status` need.

        Only `kelmo queue list` showed them before, so a note that failed for days was invisible to the
        agent that wrote it. The reason is the outcome the failure recorded; an older vault without one
        reports null and the code still names the kind of stop."""
        rows = self.journal.conn.execute(
            'SELECT id,filename,error_code,created_at FROM operations WHERE state=? '
            'ORDER BY created_at DESC,id DESC LIMIT ?', ('failed', limit)).fetchall()
        if not rows:
            return []
        has_outcomes = self.journal.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_outcomes'").fetchone() is not None
        report = []
        for row in rows:
            reason = None
            if has_outcomes:
                outcome = self.journal.conn.execute('SELECT reason FROM source_outcomes WHERE operation_id=?',
                                                    (row['id'],)).fetchone()
                reason = outcome['reason'] if outcome is not None else None
            report.append({'id': row['id'], 'filename': row['filename'], 'error_code': row['error_code'],
                           'reason': reason, 'created_at': row['created_at']})
        return report

    def status(self):
        registry = engine.load_registry(self.root)
        counts = {row['state']: row['n'] for row in self.journal.conn.execute(
            'SELECT state,COUNT(*) AS n FROM operations GROUP BY state')}
        settled = outcomes.summary(self.journal.conn)
        pending = self.journal.conn.execute(
            """SELECT COUNT(*) FROM operations WHERE terminal=0
               AND state NOT IN ('captured','needs_resolution','needs_review')"""
        ).fetchone()[0]
        summary = {'total': sum(counts.values()), 'pending': pending,
                   'captured': counts.get('captured', 0), 'applied': settled['applied'],
                   'archive_only': settled['archive-only'], 'failed': counts.get('failed', 0),
                   'needs_review': counts.get('needs_resolution', 0) + counts.get('needs_review', 0)}
        return {'vault_id': self.config.vault_id, 'operations': counts,
                'outcomes': settled, 'summary': summary, 'facts': len(registry['facts']),
                'failed_operations': self.failed_operations(),
                    **self.archive_api.counts(),
                    'archive_contract_version': CONTRACT_VERSION, 'archive_intake_behavior': INTAKE_BEHAVIOR_VERSION,
                    'writer': 'local', 'remote_configured': False, 'deployment_parity_verified': False}

    def archive_record(self, memory_id, *, operation_id=None):
        """Read an exact owning receipt, or the visible head without an operation."""
        if operation_id is None:
            return self.archive_api.public_record(memory_id)
        row = self.journal.conn.execute('SELECT id FROM operations WHERE archive_id=? AND id=?',
                                        (memory_id, operation_id)).fetchone()
        if row is None:
            raise KelmoError('E_RECEIPT', 'This operation does not own the archive receipt.')
        operation = self.journal.get(row['id'])
        source = self.source_path(operation).read_bytes().decode('utf-8')
        if engine.digest(source) != operation['source_hash']:
            raise KelmoError('E_SOURCE', 'Owning source no longer matches its immutable identity.')
        exact = self.journal.evidence_record(operation['id'])
        origin = self._operation_origin(operation['id'])
        if exact is not None:
            ref = EvidenceRef.from_dict(exact)
        else:
            ref = self.archive_api.capture_receipt(operation['id'], origin)
            if ref is None:
                ref = EvidenceRef(memory_id, 1, operation['source_hash'], operation['source_hash'])
                self.archive_api.bind_legacy_receipt(ref, operation['id'], origin)
        if (ref.archive_id, ref.content_sha256) != (memory_id, operation['source_hash']):
            raise KelmoError('E_RECEIPT', 'Owning receipt differs from the archived operation source.')
        record = self.archive_api.receipt_record(ref, operation['id'], origin)
        return dict(record, evidence_ref=ref.to_dict())

    def show(self, memory_id):
        return self.archive_api.public_record(memory_id)

    def doctor(self):
        """This vault's health checks, as `kelmo doctor` reports them."""
        from .doctor import diagnose
        return diagnose(self.root)

    def forget(self, memory_id, *, reason=None):
        """Hide one archived record from search and list. No byte is removed, ever."""
        return self._forget(memory_id, self.origin, reason=reason)

    def _forget(self, memory_id, origin, *, reason=None, idempotency_key=None):
        if self.journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Journal event reconciliation requires an idle connection.')
        # Each CLI call has a fresh identity; archive idempotency remains available to transports.
        receipt = self.archive_api.hide(memory_id, origin, idempotency_key or str(uuid.uuid4()), reason=reason)
        hidden = receipt.outcome == 'hidden'
        stamp = self.archive_api.hidden_at(memory_id)
        if stamp is None:
            raise KelmoError('E_RECEIPT', 'Archive hide has no durable visibility marker.')
        operations = [row['id'] for row in self.journal.conn.execute(
            'SELECT id FROM operations WHERE archive_id=? ORDER BY created_at', (memory_id,))]
        for operation_id in operations:
            self.journal.event_once(operation_id, 'forgotten', check_writer=self.archive_api.assert_writer,
                                    record_id=memory_id, forgotten_at=stamp)
        return {'id': memory_id, 'forgotten': hidden, 'forgotten_at': stamp, 'operations': operations}

    def list(self, limit=20, offset=0):
        """Visible records, newest first, one page at a time; `total` counts them all, so a caller pages by offset."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise KelmoError('E_INPUT', 'List limit must be between 1 and 500.', 2)
        if type(offset) is not int or offset < 0:
            raise KelmoError('E_INPUT', 'List offset must be a whole number of records, 0 or more.', 2)
        return self.archive_api.list_records(limit, offset)

    def search(self, query, limit=10, *, with_metadata=False, project=None, tags=None, since=None, until=None,
               client=None):
        if not isinstance(query, str) or not query.strip():
            raise KelmoError('E_INPUT', 'Provide a nonempty search query.', 2)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise KelmoError('E_INPUT', 'Search limit must be between 1 and 100.', 2)
        filters = self._filters(project, tags, since, until)
        scoped = SearchFilters.from_dict(filters)
        began = time.monotonic()
        result = self.archive_api.search_records(query, scoped, limit)
        self._log_search([query], 'hybrid', limit, filters, result, (time.monotonic() - began) * 1000, client)
        return result if with_metadata else result['items']

    def _log_search(self, variants, requested_mode, limit, filters, result, ms, client):
        """Opt-in telemetry ([telemetry] search_log): its own connection and transaction, never this
        Service's, and never a reason for a successful search to fail."""
        settings = self.config.telemetry
        if not settings or settings['search_log'] == 'off':
            return
        try:
            from kelmo.store.memory import search_log
            from . import telemetry
            from .archive_adapter import LocalArchiveAdapter
            target = self.config.archive
            if not isinstance(self.archive_api, LocalArchiveAdapter):
                # The confined child stays the only writer of the archive; this process logs beside it,
                # naming the enrolled client the search came from.
                target = telemetry.ensure(self.config)
                origin = getattr(self, 'origin', None)
                client = getattr(origin, 'client_id', None) or client
            metadata = result['search']
            search_log.record(target, settings, variants=variants, mode=metadata['mode'],
                              limit=limit, filters=filters, semantic=metadata['mode'] != 'lexical',
                              results=result['items'], ms=ms, client=client, machine_tag=self.config.label,
                              retrieval=dict(self._retrieval_identity(), requested_mode=requested_mode,
                                             degraded=metadata['degraded'], reason=metadata['reason']))
        except Exception:  # noqa: BLE001 - Telemetry must not change the search outcome.
            return

    def _retrieval_identity(self):
        """Index, space, profile, generation and model behind a ranking; unknown parts are None."""
        from kelmo.store.config import EMBEDDING_MODEL
        index = getattr(self.archive_api, 'chunk_index', None)
        if index is None:
            return {'contract': CONTRACT_VERSION, 'index_identity': 'core-record-index/1',
                    'generation': 'core-record-index/1', 'model': EMBEDDING_MODEL,
                    'space_id': None, 'profile_id': None}
        identity = {'contract': CONTRACT_VERSION, 'provider': index.settings['provider'],
                    'model': index.settings.get('model') or index.settings.get('space', {}).get('artifact_identity')}
        for key, read in (('profile_id', index.profile_id), ('space_id', index.declared_space_id),
                          ('generation', index.generation)):
            try:
                identity[key] = read()
            except Exception:  # noqa: BLE001 - An identity that cannot be established is not guessed.
                identity[key] = None
        identity['index_identity'] = identity['generation']
        return identity

    @staticmethod
    def _filters(project, tags, since, until):
        """Exact scope for a search: every given filter must hold, and none widens the result."""
        filters = {}
        if project is not None:
            if not isinstance(project, str) or not TAG.fullmatch(project):
                raise KelmoError('E_INPUT', 'Project filter must be one simple name.', 2)
            filters['project'] = project
        if tags and not isinstance(tags, str):
            tags = list(tags)
        if tags:
            if isinstance(tags, str) or not all(isinstance(tag, str) and TAG.fullmatch(tag) for tag in tags):
                raise KelmoError('E_INPUT', 'Tag filters must be simple names.', 2)
            filters['tags'] = list(tags)
        for name, value in (('since', since), ('until', until)):
            if value is not None:
                try:
                    filters[name] = iso_day(value)
                except ValueError as error:
                    raise KelmoError('E_INPUT', name.capitalize() + ' must be an ISO date such as 2026-10-10.', 2) from error
        return filters
