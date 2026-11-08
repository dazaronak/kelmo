#!/usr/bin/env python3
"""Administrator capabilities over an archive.

ArchiveAdmin is the administrator surface (`kelmo admin <verb>`): count, stats, doctor, backup, dump,
graph and entity-index rebuild, reembed, vector backfill, search-log read and physical purge. Every verb
needs an operator grant issued for this vault that names it; the ordinary CLI/MCP surface never holds one.
The grant separates capabilities inside the process for code that follows it; it is not a security
boundary against hostile code in the same process.
The read verbs (count, stats, doctor, backup, dump, search-log) work from the vault's Config through
read-only connections, so they need no archive writer (search-log first applies an explicitly configured
retention, a separate write). Purge is two operator steps under the purge capability: authorize_purge
stores a named disposition for the exact plan the operator reviewed in the vault's journal, and purge
executes it separately, rechecking plan and writer under the locks; nothing authorizes itself.
"""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kelmo.wiki.engine import sync_directory, sync_file


def _write_snapshot(dst_path: str, source) -> None:
    source_path = Path(source).resolve()
    if Path(dst_path).resolve() == source_path:
        raise OSError('Backup source and destination must differ')
    fd = os.open(dst_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise OSError('Backup destination must not be a hardlink')
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    src = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    try:
        src.execute('BEGIN')
        source_count = src.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise OSError('Backup integrity verification failed')
            if dst.execute('SELECT COUNT(*) FROM memories').fetchone()[0] != source_count:
                raise OSError('Backup snapshot verification failed')
        finally:
            dst.close()
    finally:
        src.close()
    sync_file(Path(dst_path))


CAPABILITIES = frozenset({'count', 'stats', 'doctor', 'backup', 'dump', 'graph', 'entity-index',
                          'reembed', 'backfill', 'search-log',
                          'purge-plan', 'purge'})
# Verbs that read the archive through Config and read-only connections, without an archive writer.
READ_VERBS = frozenset({'count', 'stats', 'doctor', 'backup', 'dump', 'search-log'})
BACKUP_KEEP = 14
BACKUP_NAME = re.compile(r'archive-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.db')
# The backups this tool created in a directory; rotation never touches any other file.
BACKUP_MANIFEST = '.kelmo-backups.json'
DISPOSITION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
REMOVED_BY_PURGE = (
    'the archive row (memories) and every replaced version (memory_versions)',
    'its record and chunk FTS postings (memories_fts, chunks_fts; both indexes optimized afterwards)',
    'its chunks and every vector derived from its text: the record vector (memories_vec), chunk vectors in '
    'every space (chunk_vectors), unverified copies attributed to it, legacy vec0 rows of its own chunks',
    'its archive receipts, hide receipts and source packets (archive_receipts, archive_hides, '
    'archive_sources, archive_version_sources)',
    'its visibility marker and every supersession link that names it',
    'its id in logged search results (rewritten as a purged marker, also for searches logged later)',
    'with secure_delete on from the first delete, the freed pages of the main database file are zeroed',
)
RETAINED_BY_PURGE = (
    'captured source bytes in the vault spool (.state/sources/<sha256>.md) for the content hashes listed',
    'journal operations, events and source metadata (the operations are staged purged)',
    'Git history of the data repository and the wiki',
    'archive backups and dumps made before the purge, and copies outside this vault',
    'older frames in the WAL file until the next checkpoint',
    'pages that earlier ordinary writes freed without secure_delete, until VACUUM rewrites the file',
    'query text of searches logged in full telemetry mode',
    'the purge receipt: content hashes, source ids and operation ids, never bytes',
    'the stored disposition and its settlement receipt in the operation journal',
)
# Dependents a disposition can settle in this baseline, with the one action each accepts.
DISPOSITION_ACTIONS = {'receipt': 'void', 'hide': 'void', 'operation': 'void',
                       'successor': 'unlink', 'predecessor': 'unlink'}
# Dependents no disposition can settle yet: each owner must remove its reference first.
REFUSED_DEPENDENTS = {
    'claim': 'an accepted wiki fact cites this record; retract it through the knowledge publisher first',
    'note': 'the fact registry records this record as an applied note; no note retraction exists yet',
    'application': 'a session application receipt names this source; no receipt retraction exists yet',
    'resolution': 'an unsettled resolution job can still publish facts citing this record',
    'lease': 'an external attempt holds a claim row on a dependent operation, expired or not; '
             'no administrator verb settles it yet',
    'intent': 'an open or prepared StateAPI or editor request depends on this record or its bytes; it could '
              'publish the evidence after a purge, so it must be settled or withdrawn first',
    'narrative': 'a published narrative cites this record as evidence; no narrative retraction exists yet',
    'action': 'an accepted action or its history cites this record as evidence; accepted history is never '
              'rewritten, so the record stays',
}

_ISSUED = weakref.WeakSet()


def _cites(value, archive_id, hashes):
    """Whether a stored intent holds an evidence reference to this record or to one of its versions."""
    if isinstance(value, dict):
        if value.get('archive_id') == archive_id or value.get('content_sha256') in hashes:
            return True
        return any(_cites(item, archive_id, hashes) for item in value.values())
    if isinstance(value, list):
        return any(_cites(item, archive_id, hashes) for item in value)
    return False


class AdminGrant:
    """An operator's grant of named administrator verbs for one vault, issued by operator_grant only.
    A copied or reconstructed grant is not issued and authorizes nothing. This is a capability check
    for cooperating code, not a sandbox: code in the same process can call operator_grant itself."""
    __slots__ = ('principal', 'vault_id', 'capabilities', '__weakref__')

    def __setattr__(self, name, value):
        raise AttributeError('An administrator grant is immutable.')


def operator_grant(config, capabilities):
    """Issue the local operator's grant (same OS trust as the vault owner) for the named verbs."""
    capabilities = frozenset(capabilities)
    if not capabilities or capabilities - CAPABILITIES:
        raise ValueError('Unknown administrator capability: ' + ', '.join(sorted(capabilities - CAPABILITIES)))
    grant = object.__new__(AdminGrant)
    for name, value in (('principal', 'operator'), ('vault_id', config.vault_id), ('capabilities', capabilities)):
        object.__setattr__(grant, name, value)
    _ISSUED.add(grant)
    return grant


@dataclass(frozen=True)
class PurgeDisposition:
    """A coordinated decision naming one record, the exact plan it reviewed and one action per dependent
    (`kind:id` -> action). The journal store (ArchiveAdmin.authorize_purge) produces these; ArchiveAdmin
    rechecks every one against the current plan before it executes."""
    disposition_id: str
    archive_id: str
    plan_sha256: str
    actions: tuple[tuple[str, str], ...]


def _inside(path, root):
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _export_path(config, destination, *, backups=False):
    """The destination as a new path in an existing directory, outside the vault's runtime state.

    The vault's .state, archive, .cache and .git trees hold live databases, WAL files, journals and
    models; an export never lands there, except a backup into archive/backups. An existing path,
    a dangling link included, is refused: exports never replace a file."""
    from .errors import KelmoError
    destination = Path(destination).expanduser()
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise KelmoError('E_INPUT', 'The export directory does not exist.', 2) from error
    if not parent.is_dir():
        raise KelmoError('E_INPUT', 'The export directory is not a directory.', 2)
    target = parent / destination.name
    root = config.root.resolve()
    allowed = (root / 'archive/backups').resolve() if backups else None
    for internal in ('.state', 'archive', '.cache', '.git'):
        if _inside(target, (root / internal).resolve()) and not (allowed and _inside(target.parent, allowed)):
            raise KelmoError('E_INPUT', f'Exports never write inside the vault runtime directory {internal}/.', 2)
    if os.path.lexists(target):
        raise KelmoError('E_EXISTS', 'The export destination already exists; choose a new path.', 2)
    return target


def _publish_snapshot(source, target):
    """A verified snapshot published at target by link, which fails rather than replace a path."""
    from .errors import KelmoError
    fd, temporary = tempfile.mkstemp(prefix='.kelmo-snapshot-', suffix='.tmp', dir=target.parent)
    os.close(fd)
    try:
        _write_snapshot(temporary, source)
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise KelmoError('E_EXISTS', 'The export destination appeared meanwhile; nothing was replaced.',
                            2) from error
        sync_directory(target.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ArchiveAdmin:
    def __init__(self, target, grant, *, disposition_source=None):
        """target: a Service (every verb) or the vault's Config (the read verbs only)."""
        from .config import Config
        from .errors import KelmoError
        self.service = None if isinstance(target, Config) else target
        self.config = target if isinstance(target, Config) else target.config
        if (not isinstance(grant, AdminGrant) or grant not in _ISSUED or grant.principal != 'operator'
                or grant.vault_id != self.config.vault_id):
            raise KelmoError('E_PERMISSION', 'Administrator verbs need an operator grant issued for this vault.')
        self.grant = grant
        # A trusted callable: disposition_id -> PurgeDisposition or None. By default the dispositions the
        # operator stored in this vault's journal with authorize_purge; without a Service, none.
        if disposition_source is None and self.service is not None:
            disposition_source = self.stored_disposition
        self.disposition_source = disposition_source

    def _require(self, capability):
        from .errors import KelmoError
        if capability not in self.grant.capabilities:
            raise KelmoError('E_PERMISSION', f'The operator grant does not include "{capability}".')

    def _archive(self):
        from .archive_adapter import LocalArchiveAdapter
        from .errors import KelmoError
        if self.service is None:
            raise KelmoError('E_STATE', 'This verb changes the archive and needs the vault opened with its writer.')
        archive = self.service.archive_api
        if not isinstance(archive, LocalArchiveAdapter):
            raise KelmoError('E_CAPABILITY', 'Administrator verbs are local-only; a transport archive needs its '
                            'own administrator composition.')
        return archive

    @contextmanager
    def _reader(self):
        """A read-only connection to the archive file: no schema, writer row or repair, ever."""
        from .errors import KelmoError
        path = self.config.archive
        if path.is_symlink() or not path.is_file():
            raise KelmoError('E_STATE', 'The archive database is missing; restore it from a backup '
                            '(kelmo admin doctor reports the state).')
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        except sqlite3.Error as error:
            raise KelmoError('E_STATE', 'The archive database cannot be opened read-only.') from error
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            try:
                import sqlite_vec
                connection.enable_load_extension(True)
                sqlite_vec.load(connection)
                connection.enable_load_extension(False)
            except (ImportError, AttributeError, sqlite3.Error):
                pass  # Vector counts are then reported unknown.
            yield connection
        except sqlite3.DatabaseError as error:
            raise KelmoError('E_STATE', f'The archive database is damaged or unreadable ({type(error).__name__}); '
                            'run kelmo admin doctor.') from error
        finally:
            connection.close()

    def _index(self):
        if self.service is not None:
            return getattr(self.service.archive_api, 'chunk_index', None)
        from .service import chunk_index_for
        return chunk_index_for(self.config)

    def _index_identity(self, index=None):
        if index is None:
            try:
                index = self._index()
            except Exception:  # noqa: BLE001 - doctor reports the failure; other reads say unknown.
                return None, None
        if index is None:
            return None, None
        identity = []
        for read in (index.profile_id, index.declared_space_id):
            try:
                identity.append(read())
            except Exception:  # noqa: BLE001 - An unavailable identity is reported as unknown, not guessed.
                identity.append(None)
        return tuple(identity)

    def _idle(self):
        from .errors import KelmoError
        if self.service.archive.in_transaction:
            raise KelmoError('E_STATE', 'Administrator writes require an idle archive connection.')

    def count(self):
        self._require('count')
        with self._reader() as connection:
            return {'count': connection.execute('SELECT COUNT(*) FROM memories').fetchone()[0]}

    def stats(self):
        from kelmo.store.memory.stats import memory_stats
        self._require('stats')
        profile_id, space_id = self._index_identity()
        with self._reader() as connection:
            result = memory_stats(connection, db_path=str(self.config.archive),
                                  profile_id=profile_id, space_id=space_id)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['archive_records'] = result['total']
            for key, table in (('forgotten_records', 'record_visibility'),
                               ('superseded_records', 'record_supersession')):
                result[key] = (connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                               if table in tables else 0)
        from . import telemetry as server_log
        telemetry = self.config.telemetry
        result['search_log_mode'] = telemetry['search_log'] if telemetry else 'off'
        try:
            result['server_searches_logged'] = server_log.count(self.config)
        except sqlite3.Error:
            result['server_searches_logged'] = None
        return result

    def doctor(self, *, probe_embedding=False):
        """Dependencies, archive coverage and graph freshness: healthy, degraded (WARN) or failed (FAIL).
        Read-only; a missing, damaged or foreign-owned archive is reported, never repaired. The embedding
        service is called only with probe_embedding."""
        from kelmo.store.doctor import FAIL, PASS, WARN, archive_checks, dependency_checks, overall
        from .doctor import _graph_state
        self._require('doctor')
        index = None
        index_error = None
        try:
            index = self._index()
        except Exception as error:  # noqa: BLE001 - A configuration the provider rejects is a failed check.
            index_error = type(error).__name__
        embedding_probe = tokenizer_probe = None
        if index is not None:
            tokenizer_probe = index.window
            if probe_embedding:
                def embedding_probe():
                    if index.provider is None:
                        raise RuntimeError(index.unavailable or 'embedding_unavailable')
                    if index.provider.health().get('ok') is not True:
                        raise RuntimeError('provider reports unhealthy')
                    index.provider.input_policy()
        elif probe_embedding and index_error is None:
            def embedding_probe():
                from kelmo.store.memory.embed import embed
                embed('Synthetic model health check', cache_dir=str(self.config.cache))
        profile_id, space_id = self._index_identity(index) if index is not None else (None, None)
        checks = dependency_checks(embedding_probe=embedding_probe, tokenizer_probe=tokenizer_probe)
        if index_error is not None:
            checks.append({'name': 'embedding_index', 'status': FAIL, 'hint': index_error})
        checks += archive_checks(self.config.archive, profile_id=profile_id, space_id=space_id)
        graph = _graph_state(self.config)
        status = {'current': PASS, 'failed': FAIL}.get(graph['status'], WARN)
        checks.append({'name': 'graph_snapshot', 'status': status,
                       'hint': '' if status == PASS else f"graph is {graph['status']}: kelmo graph rebuild"})
        return {'status': overall(checks), 'checks': checks}

    def _source(self):
        from .errors import KelmoError
        path = self.config.archive
        if path.is_symlink() or not path.is_file():
            raise KelmoError('E_STATE', 'The archive database is missing; there is nothing to copy.')
        return path

    def backup(self, directory=None, keep=BACKUP_KEEP):
        """A consistent, integrity-checked snapshot (sqlite backup API), published under a new name; the
        newest `keep` backups this tool made in that directory are kept. The snapshot holds everything
        the archive holds, search telemetry included."""
        from .errors import KelmoError
        self._require('backup')
        if type(keep) is not int or keep < 1:
            raise KelmoError('E_INPUT', 'Backup retention must be a positive whole number.', 2)
        source = self._source()
        if directory:
            target = Path(directory).expanduser()
            if not os.path.lexists(target):
                # A new backup directory obeys the same placement rule as the files it will hold.
                target = _export_path(self.config, target, backups=True)
                target.mkdir(mode=0o700)
        else:
            target = self.config.internal('archive/backups')
            target.mkdir(mode=0o700, exist_ok=True)
        name = time.strftime('archive-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.db'
        destination = _export_path(self.config, target / name, backups=True)
        try:
            _publish_snapshot(source, destination)
        except sqlite3.Error as error:
            raise KelmoError('E_STATE', 'The archive could not be read for a snapshot.') from error
        manifest = destination.parent / BACKUP_MANIFEST
        made = [entry for entry in self._manifest(manifest) if entry != destination.name] + [destination.name]
        existing = [entry for entry in made if BACKUP_NAME.fullmatch(entry)
                    and (destination.parent / entry).is_file() and not (destination.parent / entry).is_symlink()]
        # Oldest first by modification time: names made in the same second carry a random suffix.
        existing.sort(key=lambda entry: ((destination.parent / entry).stat().st_mtime_ns, entry))
        rotated = existing[:-keep]
        for entry in rotated:
            os.unlink(destination.parent / entry)
        self._write_manifest(manifest, existing[-keep:])
        sync_directory(destination.parent)
        return {'path': str(destination), 'bytes': destination.stat().st_size, 'rotated': rotated}

    @staticmethod
    def _manifest(path):
        if path.is_symlink() or not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []

    @staticmethod
    def _write_manifest(path, names):
        fd, temporary = tempfile.mkstemp(prefix='.kelmo-backups-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(names, stream)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def dump(self, destination):
        """The same consistent snapshot published at one new file (0600); never replaces a path."""
        from .errors import KelmoError
        self._require('dump')
        source = self._source()
        target = _export_path(self.config, destination)
        try:
            _publish_snapshot(source, target)
        except sqlite3.Error as error:
            raise KelmoError('E_STATE', 'The archive could not be read for a snapshot.') from error
        return {'path': str(target), 'bytes': target.stat().st_size}

    def graph(self):
        self._require('graph')
        self._archive()
        return self.service.graph_rebuild()

    def entity_index(self):
        """Rebuild the graph name index from the published entity rows, under the graph lock."""
        from kelmo.store.graph.build import ensure_schema
        from kelmo.store.graph.index import rebuild_entity_index
        self._require('entity-index')
        archive = self._archive()
        self._idle()
        connection = self.service.archive
        with self.config.internal('.state/graph.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                archive.assert_writer()
                ensure_schema(connection)
                return rebuild_entity_index(connection)

    def reembed(self, *, batch=64, force=False):
        """Rebuild chunks and vectors of the configured chunk index (reembed): resumable, records
        changed meanwhile are reported stale. `failed` > 0 means unfinished."""
        from .errors import KelmoError
        self._require('reembed')
        if type(batch) is not int or not 1 <= batch <= 10000:
            raise KelmoError('E_INPUT', 'Batch must be between 1 and 10000.', 2)
        return self._archive().rebuild_chunks(force=bool(force), batch=batch)

    def backfill(self, *, batch=None):
        """Embed missing vectors of the configured profile (backfill); one run at a time."""
        from .errors import KelmoError
        self._require('backfill')
        if batch is not None and (type(batch) is not int or not 1 <= batch <= 10000):
            raise KelmoError('E_INPUT', 'Batch must be between 1 and 10000.', 2)
        archive = self._archive()
        with self.config.internal('.state/backfill.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'skipped': 'another backfill is running'}
            return archive.backfill_vectors(batch=batch)

    def search_log(self, limit=500):
        """The last `limit` (at most 500) logged searches, oldest first. A configured [telemetry]
        retention is applied first, so expired rows are never shown; without one nothing is pruned."""
        from kelmo.store.memory.search_log import READ_LIMIT, enforce_retention, read_recent
        from .errors import KelmoError
        self._require('search-log')
        if type(limit) is not int or not 1 <= limit <= READ_LIMIT:
            raise KelmoError('E_INPUT', f'Search-log limit must be between 1 and {READ_LIMIT}.', 2)
        from . import telemetry
        # The archive's own log, and a server's log beside the journal (kelmo.telemetry).
        sources = [self._source()] + ([telemetry.path(self.config)] if telemetry.path(self.config).exists() else [])
        try:
            for source in sources:
                enforce_retention(source, self.config.telemetry)
        except sqlite3.Error as error:
            raise KelmoError('E_BUSY', 'The configured telemetry retention could not be applied; the log '
                            'is not shown. Retry when the archive is idle.') from error
        try:
            rows = [row for source in sources for row in read_recent(source, limit)]
        except sqlite3.Error as error:
            raise KelmoError('E_STATE', 'The search log could not be read.') from error
        return {'searches': sorted(rows, key=lambda row: row['at'])[-limit:]}

    # --- physical purge ---

    def purge_plan(self, archive_id):
        """Read-only: every dependent of the record, the action a disposition must name for each, the
        dependents no disposition can settle yet, and exactly which copies a purge removes and retains."""
        self._require('purge-plan' if 'purge-plan' in self.grant.capabilities else 'purge')
        return self._plan(archive_id)

    def _accepted_registry(self):
        """The accepted knowledge registry: the committed `knowledge/provenance/registry.json` the readers
        and the publisher serve, never a working-tree edit. A vault with neither a committed nor a
        working registry holds no knowledge dependents. An unfinished wiki transaction, a working
        registry that differs from the committed one, or one with no committed generation refuses: a
        dependency hidden by an uncommitted edit must not let referenced evidence be erased."""
        from .errors import KelmoError
        from .wiki import engine
        root = Path(self.service.root)
        if engine.transaction_path(root).exists() or engine.transaction_path(root).is_symlink():
            raise KelmoError('E_CONFLICT', 'A wiki publication is unfinished; let its recovery complete (memory '
                            'doctor), then plan the purge again.')
        working_path = root / 'knowledge/provenance/registry.json'
        try:
            committed = engine.git_blob(root, 'HEAD:knowledge/provenance/registry.json')
        except (OSError, RuntimeError) as error:
            raise KelmoError('E_STATE', 'The committed knowledge registry cannot be read; purge planning '
                            'needs it to find accepted facts citing the record.') from error
        working = working_path.read_bytes() if working_path.exists() else None
        if committed is None and working is None:
            return {'version': engine.REGISTRY_VERSION, 'facts': {}, 'notes': {}}
        if committed is None:
            raise KelmoError('E_CONFLICT', 'The knowledge registry has no committed generation; publish it '
                            'through the knowledge publisher before planning a purge.')
        if working != committed:
            raise KelmoError('E_CONFLICT', 'The working knowledge registry differs from the committed one; '
                            'publish or restore it before planning a purge.')
        try:
            return engine.validate_registry(json.loads(committed.decode('utf-8')))
        except (UnicodeError, ValueError) as error:
            raise KelmoError('E_CONFLICT', 'The committed knowledge registry is not a supported registry; '
                            'purge planning refuses rather than guess its dependents.') from error

    def _intent_dependents(self, archive_id, hashes):
        """Unsettled StateAPI and editor requests that depend on this record: refused dependents.

        A request is open or prepared while it can still publish. It depends on the record when its
        source operation archived it, or archived these bytes before receiving a record, or when its
        prepared intent carries an evidence reference to the record or one of its versions. Nothing
        is withdrawn or settled here.
        """
        found = []
        rows = self.service.journal.conn.execute(
            "SELECT r.operation_id, i.intent, o.archive_id AS source_archive, o.source_hash "
            'FROM action_requests r '
            'LEFT JOIN action_intents i ON i.operation_id = r.operation_id '
            'LEFT JOIN operations o ON o.id = r.source_operation_id '
            "WHERE r.state IN ('open', 'prepared') ORDER BY r.created_at, r.operation_id")
        for row in rows:
            sourced = row['source_archive'] == archive_id or (
                row['source_archive'] is None and row['source_hash'] in hashes)
            if sourced or (row['intent'] and _cites(json.loads(row['intent']), archive_id, hashes)):
                found.append('intent:' + row['operation_id'])
        return found

    def _plan(self, archive_id):
        archive = self._archive()
        dependents = archive.purge_dependents(archive_id)
        hashes = set(dependents['content_sha256'])
        journal = self.service.journal.conn
        operations = {}
        for row in journal.execute('SELECT id, state, terminal, archive_id, source_hash FROM operations '
                                   'ORDER BY created_at, id'):
            if row['archive_id'] == archive_id or (row['archive_id'] is None and row['source_hash'] in hashes):
                operations[row['id']] = dict(row)
        required = {}
        for kind, values in (('receipt', dependents['receipts']), ('hide', dependents['hides']),
                             ('operation', sorted(operations)), ('successor', dependents['successors']),
                             ('predecessor', [dependents['predecessor']] if dependents['predecessor'] else [])):
            for value in values:
                required[f'{kind}:{value}'] = DISPOSITION_ACTIONS[kind]
        refused = {}
        registry = self._accepted_registry()
        for key, fact in sorted(registry['facts'].items()):
            if fact.get('ark_id') == archive_id:
                refused['claim:' + key] = REFUSED_DEPENDENTS['claim']
        for key, note in sorted(registry['notes'].items()):
            if note.get('ark_id') == archive_id or key in hashes:
                refused['note:' + key] = REFUSED_DEPENDENTS['note']
        for key, receipt in sorted(registry.get('session_applications', {}).items()):
            if isinstance(receipt, dict) and receipt.get('note') in hashes:
                refused['application:' + key] = REFUSED_DEPENDENTS['application']
        for key, narrative in sorted(registry.get('narratives', {}).items()):
            if _cites(narrative, archive_id, hashes):
                refused['narrative:' + key] = REFUSED_DEPENDENTS['narrative']
        for key, action in sorted(registry.get('actions', {}).items()):
            if _cites(action, archive_id, hashes):
                refused['action:' + key] = REFUSED_DEPENDENTS['action']
        for row in journal.execute('SELECT action_id, accepted_event FROM action_history ORDER BY action_id, sequence'):
            if _cites(json.loads(row['accepted_event']), archive_id, hashes):
                refused['action:' + row['action_id']] = REFUSED_DEPENDENTS['action']
        dependent_ids = json.dumps(sorted(operations))
        if journal.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions'").fetchone():
            for row in journal.execute("SELECT id FROM resolutions WHERE state != 'applied' AND operation_id IN "
                                       '(SELECT value FROM json_each(?)) ORDER BY id', (dependent_ids,)):
                refused['resolution:' + row['id']] = REFUSED_DEPENDENTS['resolution']
        # Handover removes only a dead local worker's claim; any other claim row, expired or not, is refused.
        for row in journal.execute('SELECT operation_id FROM claims WHERE local_worker=0 AND operation_id IN '
                                   '(SELECT value FROM json_each(?)) ORDER BY operation_id', (dependent_ids,)):
            refused['lease:' + row['operation_id']] = REFUSED_DEPENDENTS['lease']
        for key in self._intent_dependents(archive_id, hashes):
            refused[key] = REFUSED_DEPENDENTS['intent']
        spool = [f'.state/sources/{value}.md' for value in sorted(hashes)
                 if self.config.internal(f'.state/sources/{value}.md').exists()]
        archive_sha256 = archive.plan_digest(dependents)
        # Two identities, never interchanged. archive_sha256 is the archive-side compare-and-swap value
        # apply_purge rechecks under its own lock. plan_sha256 is what the operator reviews and
        # authorizes: the archive identity together with every required action and every refused
        # dependent, so a dependent that appears after the review (a queued capture of the same bytes
        # included) changes it and is never approved on the operator's behalf.
        review = hashlib.sha256(json.dumps(
            {'archive_id': archive_id, 'archive_sha256': archive_sha256,
             'required': sorted(required.items()), 'refused': sorted(refused)}, sort_keys=True).encode()
        ).hexdigest()
        return {'archive_id': archive_id, 'plan_sha256': review, 'archive_sha256': archive_sha256,
                'required': required, 'refused': refused, 'operations': list(operations.values()),
                'dependents': dependents, 'removes': list(REMOVED_BY_PURGE),
                'retains': list(RETAINED_BY_PURGE), 'retained_spool': spool,
                'identical_content_records': dependents['identical_content_records'],
                'whole_system_erasure': False}

    def authorize_purge(self, archive_id, disposition_id, plan_sha256):
        """Store the operator's named disposition for one record in the journal; nothing is removed.

        plan_sha256 is the hash purge-plan showed the operator. The full current plan is computed again: any
        dependent no disposition can settle refuses, and a hash that is no longer current refuses, so a
        changed plan is never approved on the operator's behalf. The disposition records one action per
        required dependent. The same id with the same record, plan and actions replays; with anything else
        it is refused. Purge executes separately and reads it."""
        from .errors import KelmoError
        self._require('purge')
        if not isinstance(archive_id, str) or not archive_id.strip():
            raise KelmoError('E_INPUT', 'Provide the archive record id the disposition names.', 2)
        if not isinstance(disposition_id, str) or not DISPOSITION_ID.fullmatch(disposition_id):
            raise KelmoError('E_INPUT', 'A disposition id is 1 to 128 letters, digits, dots, colons, underscores '
                            'or hyphens, starting with a letter or digit.', 2)
        if not isinstance(plan_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', plan_sha256):
            raise KelmoError('E_INPUT', 'Provide the 64-character plan_sha256 that kelmo admin purge-plan showed.', 2)
        archive = self._archive()
        journal = self.service.journal
        if self.service.archive.in_transaction or journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge authorization requires idle archive and journal connections.')
        stored = journal.purge_disposition(disposition_id)
        if stored is not None:
            if (stored['vault_id'], stored['operator'], stored['archive_id'], stored['plan_sha256']) != (
                    self.config.vault_id, self.grant.principal, archive_id, plan_sha256):
                raise KelmoError('E_IDEMPOTENCY', f'Disposition {disposition_id} already authorizes a different '
                                'record or plan; choose a new disposition id.')
            return self._authorization(stored, replayed=True)
        # Captures wait while the plan is read and stored, so it describes one moment of the vault.
        with self.config.internal('.state/capture.lock').open('a') as capture:
            fcntl.flock(capture, fcntl.LOCK_EX)
            archive.assert_writer()
            plan = self._plan(archive_id)
            self._refuse_unsettled(plan)
            if plan['plan_sha256'] != plan_sha256:
                raise KelmoError('E_CONFLICT', 'The purge plan changed since it was reviewed; run kelmo admin '
                                'purge-plan again and authorize its current plan_sha256.')
            stored, replayed = journal.authorize_purge(
                disposition_id, vault_id=self.config.vault_id, operator=self.grant.principal,
                archive_id=archive_id, plan_sha256=plan_sha256, actions=sorted(plan['required'].items()),
                check_writer=archive.assert_writer)
        return self._authorization(stored, replayed=replayed)

    @staticmethod
    def _authorization(stored, *, replayed):
        return {'disposition_id': stored['disposition_id'], 'archive_id': stored['archive_id'],
                'plan_sha256': stored['plan_sha256'], 'actions': dict(stored['actions']),
                'operator': stored['operator'], 'state': stored['state'],
                'authorized_at': stored['authorized_at'], 'replayed': replayed, 'removed': False,
                'next_step': f"kelmo admin purge {stored['archive_id']} --disposition {stored['disposition_id']}"}

    def stored_disposition(self, disposition_id):
        """The journal's disposition with this id as a PurgeDisposition, or None (also for another vault's)."""
        stored = self.service.journal.purge_disposition(disposition_id)
        if stored is None or stored['vault_id'] != self.config.vault_id:
            return None
        return PurgeDisposition(stored['disposition_id'], stored['archive_id'], stored['plan_sha256'],
                                tuple(stored['actions']))

    def purge(self, archive_id, disposition_id):
        """ArchiveAdminAPI.purge: the OperationReceipt of a completed physical purge, or a refusal."""
        return self._purge(archive_id, disposition_id)[0]

    def purge_report(self, archive_id, disposition_id):
        receipt, report = self._purge(archive_id, disposition_id)
        return dict(report, receipt=receipt.to_dict())

    def _purge(self, archive_id, disposition_id):
        from .errors import KelmoError
        self._require('purge')
        if not isinstance(archive_id, str) or not archive_id.strip():
            raise KelmoError('E_INPUT', 'Provide the archive record id to purge.', 2)
        if not isinstance(disposition_id, str) or not disposition_id.strip():
            raise KelmoError('E_INPUT', 'Purge needs a named coordinated disposition.', 2)
        archive = self._archive()
        if self.disposition_source is None:
            raise KelmoError('E_CAPABILITY', 'No coordinated purge disposition source is available; physical purge '
                            'is refused and nothing was removed. Use kelmo forget to hide it.')
        journal = self.service.journal
        # Both connections are checked before anything changes, the journal's claim rows included.
        if self.service.archive.in_transaction or journal.conn.in_transaction:
            raise KelmoError('E_STATE', 'Purge requires idle archive and journal connections; the caller\'s '
                            'transaction is kept.')
        config = self.config
        with config.internal('.state/worker.lock').open('a') as worker:
            try:
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise KelmoError('E_BUSY', 'Another worker is processing this vault.') from error
            with config.internal('.state/capture.lock').open('a') as capture:
                fcntl.flock(capture, fcntl.LOCK_EX)
                archive.assert_writer()
                stored = archive.purge_record(archive_id)
                if stored is not None:
                    if stored['disposition_id'] != disposition_id:
                        raise KelmoError('E_IDEMPOTENCY', 'This record was purged under a different disposition.')
                    # Repeat after commit: the archive half is done; the journal half below is completed.
                    receipt, effects, replayed = archive.apply_purge(archive_id, disposition_id,
                                                                     stored['plan_sha256'], ())
                else:
                    receipt, effects, replayed = self._execute(archive, journal, archive_id, disposition_id)
                # Rechecked after any interruption: an external claim row on an operation of this purge
                # is the refused lease dependent, never cleared here. The execution intent keeps new
                # claims out, so only a row older than it can be found.
                leases = journal.external_claims(effects['journal_operations'])
                if leases:
                    raise KelmoError('E_CAPABILITY', 'The archive half of this purge is committed, but external '
                                    'claims hold its operations: ' + ', '.join('lease:' + item for item in leases)
                                    + f" ({REFUSED_DEPENDENTS['lease']}). Repeat the purge once they are gone.")
                # Archive first, then the journal: a crash between them is finished by a repeat.
                for operation_id in effects['journal_operations']:
                    # The worker lock is held: a local claim left behind belongs to a dead worker.
                    journal.handover_local_claim(operation_id)
                    journal.event_once(operation_id, 'purge', check_writer=archive.assert_writer,
                                       archive_id=archive_id, disposition_id=disposition_id)
                    if journal.get(operation_id)['state'] != 'purged':
                        journal.stage(operation_id, 'purged', terminal=True)
                # Last: the stored disposition's state and its receipt, in one journal transaction. None
                # when the disposition came from a source other than this journal.
                settled = journal.settle_purge(disposition_id, archive_id, receipt.to_dict(),
                                               effects['journal_operations'], check_writer=archive.assert_writer)
                # A server's search log lives beside the journal; its rows lose the record's id as the
                # archive's own log rows did. A repeat rewrites nothing more.
                from . import telemetry
                effects = dict(effects, telemetry_search_log_results=telemetry.forget(config, archive_id))
        spool = [f'.state/sources/{value}.md' for value in effects['content_sha256']
                 if config.internal(f'.state/sources/{value}.md').exists()]
        return receipt, {'archive_id': archive_id, 'disposition_id': disposition_id, 'replayed': replayed,
                         'disposition_state': settled['state'] if settled else None,
                         'effects': effects, 'operations_purged': effects['journal_operations'],
                         'removed': list(REMOVED_BY_PURGE), 'retained': list(RETAINED_BY_PURGE),
                         'retained_spool': spool,
                         'identical_content_records': effects['identical_content_records'],
                         'whole_system_erasure': False}

    def _execute(self, archive, journal, archive_id, disposition_id):
        """The first execution of a disposition, under the worker and capture locks held by _purge.

        The full plan and the disposition are checked again. Then, in one journal transaction, the
        external claims of the planned operations are rechecked and an execution intent naming them is
        recorded; Journal.claim refuses those operations while it lasts, so no claim can appear between
        the archive commit and the journal half. If the archive half does not commit, the intent is
        withdrawn. An intent left by a process that died before the archive commit is withdrawn first."""
        from .errors import KelmoError
        stored = journal.purge_disposition(disposition_id)
        intent = journal.purge_intent(disposition_id)
        if (stored is not None and stored['state'] == 'settled') or (intent and intent['state'] == 'finished'):
            # The journal settled this disposition, yet the archive holds the record and no purge
            # receipt: the archive was restored from an earlier backup. The earlier review does not
            # authorize a new deletion.
            raise KelmoError('E_STATE', f'Disposition {disposition_id} was already settled, but the archive holds '
                            'the record again (restored from a backup?). Review purge-plan and authorize a new '
                            'disposition id; a settled disposition is never executed twice.')
        journal.abandon_purge(disposition_id, check_writer=archive.assert_writer)
        plan = self._plan(archive_id)
        self._check_disposition(plan, archive_id, disposition_id)
        operations = [item['id'] for item in plan['operations']]
        journal.begin_purge(disposition_id, archive_id, operations, check_writer=archive.assert_writer)
        try:
            return archive.apply_purge(archive_id, disposition_id, plan['archive_sha256'], operations)
        except BaseException:
            if archive.purge_record(archive_id) is None:
                journal.abandon_purge(disposition_id, check_writer=archive.assert_writer)
            raise

    @staticmethod
    def _refuse_unsettled(plan):
        from .errors import KelmoError
        if plan['refused']:
            raise KelmoError('E_CAPABILITY', 'Purge refused; these dependents have no disposition in this baseline: '
                            + '; '.join(f'{key} ({reason})' for key, reason in plan['refused'].items()))

    def _check_disposition(self, plan, archive_id, disposition_id):
        from .errors import KelmoError
        self._refuse_unsettled(plan)
        disposition = self.disposition_source(disposition_id)
        if disposition is None:
            raise KelmoError('E_CAPABILITY', f'Disposition {disposition_id} is unknown to the disposition source.')
        if (not isinstance(disposition, PurgeDisposition) or disposition.disposition_id != disposition_id
                or disposition.archive_id != archive_id):
            raise KelmoError('E_CAPABILITY', f'Disposition {disposition_id} does not name record {archive_id}.')
        if disposition.plan_sha256 != plan['plan_sha256']:
            raise KelmoError('E_CONFLICT', 'The disposition reviewed a different purge plan; review the current one.')
        actions = dict(disposition.actions)
        if len(actions) != len(disposition.actions):
            raise KelmoError('E_CONFLICT', 'The disposition names a dependent twice.')
        missing = sorted(plan['required'].keys() - actions.keys())
        if missing:
            raise KelmoError('E_CONFLICT', 'Incomplete disposition; no action for: ' + ', '.join(missing))
        extra = sorted(actions.keys() - plan['required'].keys())
        if extra:
            raise KelmoError('E_CONFLICT', 'The disposition names dependents this record does not have: '
                            + ', '.join(extra))
        wrong = sorted(key for key, action in plan['required'].items() if actions[key] != action)
        if wrong:
            raise KelmoError('E_CONFLICT', 'Unsupported disposition action for: ' + ', '.join(wrong))

