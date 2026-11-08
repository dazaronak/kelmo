"""Stopped-copy snapshot, census, inventory, resumable import and post-cutover recovery.

A snapshot copies named sources while their writers are stopped: SQLite databases through the backup
API, Git working trees and plain directories byte for byte (Git hooks and config included). Each
source is fingerprinted before and after its copy; any difference means a writer was still running,
and the snapshot is refused rather than labelled consistent. Separate SQLite backups and Git refs are
never assumed to form one atomic state: the barrier is the double read, not the order of copies.

Import places a snapshot into a schema-2 vault with independent roots, opens it through Service (the
only schema upgrade path) and then proves, by IDs and hashes, that every archive record version, the
journal, the vault Git history and every copied file arrived unchanged. No model is called. Every
step is recorded before the next, so an interrupted import resumes and repeats nothing it verified.

After cutover the new vault accepts writes. A rollback to the pre-cutover snapshot would lose them,
so it is refused while a tail exists; recovery goes forward, from a fresh snapshot of the current
vault, which carries that tail.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import KelmoError

SOURCE_KINDS = ('sqlite', 'git', 'tree')
MANIFEST = 'manifest.json'
IMPORT_STATE = '.import/state.json'
CUTOVER = '.import/cutover.json'
# Keys the import itself writes; carried settings may not set them.
RESERVED_SETTINGS = frozenset({'schema_version', 'writer', 'vault_id', 'node_label', 'wiki_root', 'storage'})


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _plain_name(name):
    if not isinstance(name, str) or not name or not name.replace('-', '').replace('_', '').isalnum():
        raise KelmoError('E_INPUT', f'Source name {name!r} must be plain letters, digits, - and _.')
    return name


def _git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    if result.returncode:
        raise KelmoError('E_GIT', f'git {args[0]} failed in {path}: {result.stderr.strip()[:300]}')
    return result.stdout.strip()


# Fingerprints and census

def tree_files(root, *, links='refuse'):
    """Every regular file under root as {relative: 'sha256:mode'}; a symlink is never followed.

    A Git source carries symlinks as tracked objects, so `links='record'` keeps each one as
    'link:<sha256 of its target text>' and the copy recreates the link itself. A plain tree refuses them.
    The permission bits are part of the identity: a Git hook that lost its executable bit no longer runs.
    """
    root = Path(root)
    files = {}
    for directory, names, entries in os.walk(root):
        names.sort()
        for name in sorted(entries) + [name for name in names if (Path(directory) / name).is_symlink()]:
            path = Path(directory) / name
            if path.is_symlink():
                if links != 'record':
                    raise KelmoError('E_SNAPSHOT', f'{path} is a symlink; a snapshot copies regular files only.')
                files[path.relative_to(root).as_posix()] = 'link:' + _sha(os.readlink(path).encode('utf-8'))
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise KelmoError('E_SNAPSHOT', f'{path} is not a regular file; a snapshot copies regular files only.')
            mode = stat.S_IMODE(info.st_mode)
            files[path.relative_to(root).as_posix()] = f'{_file_sha(path)}:{mode:o}'
    return files


def _links(kind):
    return 'record' if kind == 'git' else 'refuse'


@contextmanager
def _source_database(path):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            yield connection
    except (sqlite3.Error, OSError) as error:
        raise KelmoError('E_SNAPSHOT', 'Cannot read the snapshot database or its WAL/SHM files. '
                        'Check integrity and file/directory access under the snapshot account, or use '
                        'a consistent SQLite backup database exported by the owning account with writers stopped. '
                        'Do not copy only the main database while WAL writes are pending.') from error


def sqlite_digest(path):
    """The logical content of a database: a checkpoint rewrites its files but not this."""
    digest = hashlib.sha256()
    with _source_database(path) as conn:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.Error):
            pass  # a database without vector tables dumps without the extension
        for line in conn.iterdump():
            digest.update(line.encode('utf-8') + b'\n')
    return digest.hexdigest()


def archive_census(path):
    """Archive records by identity: every current and replaced version with its content hash."""
    with _source_database(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'memories' not in tables:
            return None
        rows = [(row[0], row[1], _sha(row[2].encode('utf-8')))
                for row in conn.execute('SELECT id, version, content FROM memories ORDER BY id')]
        if 'memory_versions' in tables:
            rows += [(row[0], row[1], _sha(row[2].encode('utf-8'))) for row in conn.execute(
                'SELECT memory_id, version, content FROM memory_versions ORDER BY memory_id, version')]
    return sorted(set(rows))


def journal_census(path):
    """Operation identities and content digests for durable session state."""
    with _source_database(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        census = {'operations': [list(row) for row in conn.execute(
            'SELECT id, state, terminal, archive_id, source_hash FROM operations ORDER BY id')]}
        if 'action_requests' in tables:
            census['action_requests'] = [list(row) for row in conn.execute(
                'SELECT operation_id, kind, state FROM action_requests ORDER BY operation_id, kind')]
        if 'session_jobs' in tables:
            census['session_jobs'] = [list(row) for row in conn.execute(
                'SELECT id, state, snapshot_hash FROM session_jobs ORDER BY id')]
        census['session_state'] = {}
        for table in ('sessions', 'session_snapshots', 'session_jobs', 'session_processing',
                      'session_capture_origins', 'session_snapshot_storage'):
            if table in tables:
                rows = sorted(json.dumps(list(row), ensure_ascii=False, separators=(',', ':'))
                              for row in conn.execute(f'SELECT * FROM "{table}"'))
                if rows:
                    census['session_state'][table] = _sha('\n'.join(rows).encode('utf-8'))
    return census


def _fingerprint(kind, path):
    path = Path(path)
    if kind == 'sqlite':
        if not path.is_file():
            raise KelmoError('E_SNAPSHOT', f'{path} is not a database file.')
        return {'content': sqlite_digest(path)}
    if kind == 'git':
        status = _git(path, 'status', '--porcelain', '--untracked-files=all')
        if status:
            raise KelmoError('E_SNAPSHOT', f'{path} has uncommitted changes; a stopped writer leaves a clean tree.')
        return {'head': _git(path, 'rev-parse', 'HEAD'), 'refs': _git(path, 'show-ref'),
                'files': tree_files(path, links='record')}
    return {'files': tree_files(path)}


# Snapshot

def take_snapshot(sources, destination):
    """Copy every named source with writers stopped; returns the manifest written beside the copies.

    `sources` maps a name to {'kind': 'sqlite' | 'git' | 'tree', 'path': ...}.
    """
    destination = Path(destination)
    if destination.is_symlink():
        raise KelmoError('E_SNAPSHOT', 'Snapshot destination cannot be a symlink.')
    if destination.exists() and any(destination.iterdir()):
        raise KelmoError('E_SNAPSHOT', 'The snapshot destination is not empty.')
    if not isinstance(sources, dict) or not sources:
        raise KelmoError('E_INPUT', 'Name at least one source.')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    entries = {}
    before = {}
    for name, source in sorted(sources.items()):
        if source.get('kind') not in SOURCE_KINDS:
            raise KelmoError('E_INPUT', f'Source {_plain_name(name)}: a kind among {SOURCE_KINDS}.')
        _plain_name(name)
        before[name] = _fingerprint(source['kind'], source['path'])
    try:
        _copy_sources(sources, destination, before, entries)
    except BaseException:
        # The destination was empty or new, so everything in it is this attempt's partial copy.
        shutil.rmtree(destination, ignore_errors=True)
        raise
    for name, source in sorted(sources.items()):
        # The barrier: nothing changed while any source was copied.
        if _fingerprint(source['kind'], source['path']) != before[name]:
            shutil.rmtree(destination)
            raise KelmoError('E_SNAPSHOT', f'Source {name} changed during the snapshot; stop its writer and retry.')
        if source['kind'] != 'sqlite' and entries[name]['files'] != before[name]['files']:
            shutil.rmtree(destination)
            raise KelmoError('E_SNAPSHOT', f'The copy of {name} differs from its source.')
    return _finish_snapshot(destination, entries)


def _copy_sources(sources, destination, before, entries):
    for name, source in sorted(sources.items()):
        kind, path, target = source['kind'], Path(source['path']), destination / name
        if kind == 'sqlite':
            target.touch(mode=0o600, exist_ok=False)
            with _source_database(path) as origin, \
                    closing(sqlite3.connect(target)) as copy:
                origin.backup(copy)
                if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise KelmoError('E_SNAPSHOT', f'The copy of {name} fails its integrity check.')
            entry = {'kind': kind, 'sha256': _file_sha(target), 'archive': archive_census(target)}
            with closing(sqlite3.connect(target)) as copy:
                is_journal = copy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                                          ).fetchone() is not None
            if is_journal:
                entry['journal'] = journal_census(target)
        else:
            shutil.copytree(path, target, symlinks=True)
            entry = {'kind': kind, 'files': tree_files(target, links=_links(kind))}
            if kind == 'git':
                entry['head'], entry['refs'] = before[name]['head'], before[name]['refs']
        entries[name] = entry


def _finish_snapshot(destination, entries):
    manifest = {'schema': 1, 'id': str(uuid.uuid4()), 'created_at': _now(), 'sources': entries}
    manifest['sha256'] = _sha(json.dumps(entries, sort_keys=True).encode('utf-8'))
    _write_json(destination / MANIFEST, manifest)
    return manifest


def verify_snapshot(snapshot):
    """Re-read every copy and compare it with the manifest; returns the manifest when all hold."""
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / MANIFEST).read_text(encoding='utf-8'))
    if _sha(json.dumps(manifest['sources'], sort_keys=True).encode('utf-8')) != manifest['sha256']:
        raise KelmoError('E_SNAPSHOT', 'The snapshot manifest does not match its own hash.')
    for name, entry in manifest['sources'].items():
        path = snapshot / _plain_name(name)
        if entry['kind'] == 'sqlite':
            if _file_sha(path) != entry['sha256']:
                raise KelmoError('E_SNAPSHOT', f'The copy of {name} changed after the snapshot.')
        elif tree_files(path, links=_links(entry['kind'])) != entry['files']:
            raise KelmoError('E_SNAPSHOT', f'The copy of {name} changed after the snapshot.')
    return manifest


# Inventory

def inventory(snapshot, *, queues=()):
    """Exactly one disposition for every operation, request and queued job the snapshot holds.

    `queues` names tree sources whose files are pending jobs of an old writer (editor queue,
    client spool); each must be drained before cutover. Blocking items prevent cutover.
    """
    manifest = verify_snapshot(snapshot)
    items = []
    for name, entry in sorted(manifest['sources'].items()):
        journal = entry.get('journal', {})
        if journal and 'session_jobs' not in journal:
            journal = dict(journal, session_jobs=journal_census(Path(snapshot) / name).get('session_jobs', []))
        for operation_id, state, terminal, archive_id, _source_hash in journal.get('operations', []):
            if archive_id is not None and state in ('archived', 'applied'):
                disposition = 'import'
            elif terminal:
                disposition = 'carry-unresolved'
            else:
                disposition = 'resume-after-cutover'
            items.append({'source': name, 'kind': 'operation', 'id': operation_id, 'state': state,
                          'disposition': disposition})
        for operation_id, kind, state in journal.get('action_requests', []):
            disposition = 'settle-before-cutover' if state in ('open', 'prepared') else 'import'
            items.append({'source': name, 'kind': 'request:' + kind, 'id': operation_id, 'state': state,
                          'disposition': disposition})
        for job_id, state, checksum in journal.get('session_jobs', []):
            disposition = ('resume-after-cutover' if state in ('pending', 'running', 'archived') else
                           'import' if state in ('wiki_applied', 'wiki_unchanged', 'skipped') else
                           'carry-unresolved')
            items.append({'source': name, 'kind': 'session-job', 'id': job_id, 'state': state,
                          'disposition': disposition, 'snapshot_hash': checksum})
        if entry['kind'] != 'sqlite' and name in queues:
            for relative in sorted(entry['files']):
                items.append({'source': name, 'kind': 'queued-job', 'id': relative, 'state': 'pending',
                              'disposition': 'drain-before-cutover'})
        for archive_id, version, content_sha in entry.get('archive') or ():
            items.append({'source': name, 'kind': 'record', 'id': f'{archive_id}@{version}', 'state': content_sha,
                          'disposition': 'import'})
    seen = set()
    for item in items:
        identity = (item['source'], item['kind'], item['id'])
        if identity in seen:
            # Two contents for one record version, or one request twice: the source needs repair first.
            raise KelmoError('E_SNAPSHOT', f'{identity[1]} {identity[2]} in {identity[0]} appears twice.')
        seen.add(identity)
    blocking = [item for item in items if item['disposition'] in ('settle-before-cutover', 'drain-before-cutover')]
    counts = {}
    for item in items:
        counts[item['disposition']] = counts.get(item['disposition'], 0) + 1
    return {'snapshot': manifest['id'], 'items': items, 'blocking': blocking, 'counts': counts}


# Import

def _state(control):
    path = Path(control) / IMPORT_STATE
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def _place_tree(source, target, files, *, links='refuse'):
    if target.exists():
        if tree_files(target, links=links) != files:
            raise KelmoError('E_IMPORT', f'{target} exists with other content; import never overwrites it.')
        return
    # A copy interrupted halfway stays under the temporary name, so a resume starts it again.
    temporary = target.with_name(target.name + '.importing')
    if temporary.exists():
        shutil.rmtree(temporary)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(mode=0o700)
    staged = temporary / 'tree'
    shutil.copytree(source, staged, symlinks=True)
    if tree_files(staged, links=links) != files:
        raise KelmoError('E_IMPORT', f'The copy of {source.name} differs from its snapshot.')
    staged.chmod(0o700)
    if target.exists():
        raise KelmoError('E_IMPORT', 'The destination appeared during the copy; nothing was replaced.')
    os.replace(staged, target)
    temporary.rmdir()


def _place_file(source, target, sha256):
    if target.exists():
        if _file_sha(target) != sha256:
            raise KelmoError('E_IMPORT', f'{target} exists with other content; import never overwrites it.')
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.' + target.name + '.importing-')
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        if _file_sha(temporary) != sha256:
            raise KelmoError('E_IMPORT', 'The copied database differs from its snapshot.')
        if target.exists():
            raise KelmoError('E_IMPORT', 'The destination appeared during the copy; nothing was replaced.')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_authority(path):
    """The authority that already owns a Kelmo archive copy, or None for an archive no Kelmo opened."""
    with _source_database(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_writer_state'"
                            ).fetchone():
            return None
        owners = sorted({row[0] for row in conn.execute('SELECT authority_id FROM archive_writer_state')})
    if len(owners) > 1:
        raise KelmoError('E_IMPORT', 'The archive copy names several authorities; name the one to import.')
    return owners[0] if owners else None


def _checked_settings(settings):
    try:
        data = tomllib.loads(settings)
    except tomllib.TOMLDecodeError as error:
        raise KelmoError('E_INPUT', f'Carried settings are not valid TOML: {error}') from error
    if RESERVED_SETTINGS & set(data):
        raise KelmoError('E_INPUT', 'Carried settings may not set ' + ', '.join(sorted(RESERVED_SETTINGS & set(data))))
    return settings if not settings or settings.endswith('\n') else settings + '\n'


def carried_settings(text):
    """The tables of a kelmo.toml other than the identity keys and [storage], verbatim."""
    sections, keep = [], False
    for line in text.splitlines(keepends=True):
        header = line.strip()
        if header.startswith('['):
            name = header.strip('[]').split('.')[0].strip()
            keep = name not in RESERVED_SETTINGS
        if keep:
            sections.append(line)
    body = ''.join(sections).strip()
    return _checked_settings(body + '\n' if body else '')


def _snapshot_stored(checksum, hashes, bases, files):
    """Whether one journal session snapshot can be read back from a copied sessions tree.

    A whole snapshot is a file whose bytes hash to its identity. A delta snapshot (one with a base in
    session_snapshot_storage) is its base's prefix followed by its own file, so its file never hashes to
    its identity: it needs that file and a base chain ending at a whole snapshot. The copied bytes are
    already checked against the manifest, and sessions.read_snapshot verifies each assembled snapshot
    when it is read.
    """
    seen = set()
    while True:
        if (not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)
                or checksum in seen or checksum not in hashes):
            return False
        seen.add(checksum)
        entry = files.get(f'transcripts/{checksum}.jsonl')
        if entry is None:
            return False
        if checksum not in bases:
            return entry.startswith(checksum + ':')
        checksum = bases[checksum]


def import_snapshot(snapshot, control, *, archive, vault, journal=None, roots, authority_id=None,
                    label='imported', registry_relative='knowledge/provenance/registry.json',
                    wiki_relative='knowledge/wiki', settings='', state_sources=None, state_sessions=None,
                    state_telemetry=None, stop_after=None):
    """Place a verified snapshot into a schema-2 vault and prove it arrived; resumable step by step.

    `archive`, `vault` and `journal` name snapshot sources; `state_sources` names a tree source of
    captured source bytes placed under state_root/sources; state_sessions carries state_root/sessions
    and state_telemetry the server-side search log. `roots` gives state_root, archive_root and
    knowledge_git_root. `settings` is TOML ([embedding], [telemetry], [budget], ...) written after
    [storage]. Without `authority_id`, a Kelmo archive keeps the authority that owns it. `stop_after`
    ends after a named step, as an interruption would.
    """
    from .journal import Journal
    from .service import Service
    snapshot, control = Path(snapshot), Path(control)
    manifest = verify_snapshot(snapshot)
    sources = manifest['sources']
    state_trees = {'sources': state_sources, 'sessions': state_sessions}
    for name, kind in ((archive, 'sqlite'), (vault, 'git'), (journal, 'sqlite'), (state_telemetry, 'sqlite'),
                       *((name, 'tree') for name in state_trees.values())):
        if name is not None and (sources.get(name) or {}).get('kind') != kind:
            raise KelmoError('E_INPUT', f'Snapshot source {name} is not a {kind} copy.')
    if journal is not None:
        with _source_database(snapshot / journal) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_snapshots'"
                                  ).fetchone():
                files = sources[state_sessions]['files'] if state_sessions else {}
                hashes = {checksum for (checksum,) in connection.execute('SELECT hash FROM session_snapshots')}
                bases = {}
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='session_snapshot_storage'").fetchone():
                    bases = dict(connection.execute('SELECT hash, base_hash FROM session_snapshot_storage '
                                                    'WHERE base_hash IS NOT NULL'))
                if not all(_snapshot_stored(checksum, hashes, bases, files) for checksum in hashes):
                    raise KelmoError('E_IMPORT', 'Journal session snapshots need their complete '
                                    'state_sessions tree; preserve the original snapshot and supply it.')
    settings = _checked_settings(settings)
    state = _state(control)
    if state is None:
        owner = _snapshot_authority(snapshot / archive)
        if authority_id is not None and owner is not None and authority_id != owner:
            raise KelmoError('E_IMPORT', 'The archive copy belongs to another authority.')
        state = {'snapshot': manifest['id'], 'authority_id': authority_id or owner or str(uuid.uuid4()),
                 'steps': {}}
    if state['snapshot'] != manifest['id']:
        raise KelmoError('E_IMPORT', 'This vault is importing another snapshot.')
    state_root, archive_root, git_root = (Path(roots[name]) for name in ('state_root', 'archive_root',
                                                                           'knowledge_git_root'))
    request = {'control': str(control.resolve()), 'archive': archive, 'vault': vault,
               'journal': journal, 'state_sources': state_sources,
               'roots': {name: str(Path(roots[name]).resolve())
                         for name in ('state_root', 'archive_root', 'knowledge_git_root')},
               'authority_id': authority_id or state['authority_id'], 'label': label,
               'registry_relative': registry_relative, 'wiki_relative': wiki_relative,
               'settings_sha256': _sha(settings.encode('utf-8'))}
    if state_sessions is not None:
        request['state_sessions'] = state_sessions
    if state_telemetry is not None:
        request['state_telemetry'] = state_telemetry
    if 'request' not in state and state['steps']:
        raise KelmoError('E_IMPORT', 'This older import receipt did not bind its input plan. '
                        'Preserve this destination and import into fresh roots with the reviewed specification.')
    if 'request' in state and state['request'] != request:
        raise KelmoError('E_IMPORT', 'Import arguments changed after the first step; resume with the same '
                        'sources, roots and settings, or choose a fresh destination.')
    state['request'] = request

    def step(name, action):
        if name in state['steps']:
            return
        state['steps'][name] = action() or True
        _write_json(control / IMPORT_STATE, state)
        if stop_after == name:
            raise KelmoError('E_INTERRUPTED', f'Import stopped after {name}.')

    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_json(control / IMPORT_STATE, state)
    step('vault', lambda: _place_tree(snapshot / vault, git_root, sources[vault]['files'], links='record'))
    step('archive', lambda: _place_file(snapshot / archive, archive_root / 'archive.db', sources[archive]['sha256']))

    def place_journal():
        target = state_root / 'journal.sqlite3'
        if journal is not None:
            _place_file(snapshot / journal, target, sources[journal]['sha256'])
        elif not target.exists():
            state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            Journal(target, create=True).close()

    step('journal', place_journal)
    if state_telemetry is not None:
        from .telemetry import FILE
        step('telemetry', lambda: _place_file(snapshot / state_telemetry, state_root / FILE,
                                              sources[state_telemetry]['sha256']))
    for directory, name in state_trees.items():
        if name is not None:
            step(directory, lambda name=name, directory=directory: _place_tree(
                snapshot / name, state_root / directory, sources[name]['files']))

    def configure():
        text = ('schema_version = 2\nwriter = "local"\nvault_id = ' + json.dumps(state['authority_id'])
                + '\nnode_label = ' + json.dumps(label) + '\n\n[storage]\n'
                + ''.join(f'{key} = {json.dumps(str(roots[key]))}\n'
                          for key in ('state_root', 'archive_root', 'knowledge_git_root'))
                + f'registry_relative = {json.dumps(registry_relative)}\nwiki_relative = {json.dumps(wiki_relative)}\n'
                + ('\n' + settings if settings else ''))
        path = control / 'kelmo.toml'
        if path.exists() and path.read_text(encoding='utf-8') != text:
            raise KelmoError('E_IMPORT', 'The vault configuration differs from this import.')
        if not path.exists():
            temporary = path.with_name('kelmo.toml.tmp')
            temporary.write_text(text, encoding='utf-8')
            os.replace(temporary, path)

    step('config', configure)

    def claim_wiki():
        # The imported vault belongs to this authority by construction; without the claim every
        # publication would refuse with E_WIKI_OWNER until someone ran kelmo init by hand.
        from .wiki.engine import activate_wiki
        activate_wiki(git_root / wiki_relative, state['authority_id'])

    step('wiki', claim_wiki)

    def open_service():
        # Opening is the schema upgrade: the archive is claimed by this authority and migrated in place.
        from .config import Config
        from .server import OriginBindings
        from .server_admin import archive_writer
        config = Config.load(control)
        enrollment = config.local_bindings().describe()
        enrollment['writer'] = archive_writer(config)
        with Service(control, bindings=OriginBindings.from_description(enrollment)):
            pass

    step('open', open_service)

    def verify():
        after = archive_census(archive_root / 'archive.db')
        expected = sources[archive]['archive']
        if [list(row) for row in after] != [list(row) for row in expected]:
            missing = {tuple(row) for row in expected} - {tuple(row) for row in after}
            raise KelmoError('E_IMPORT', f'{len(missing)} archive record versions did not arrive unchanged.')
        if _git(git_root, 'rev-parse', 'HEAD') != sources[vault]['head']:
            raise KelmoError('E_IMPORT', 'The vault Git history did not arrive at its snapshot head.')
        if _git(git_root, 'show-ref') != sources[vault]['refs']:
            raise KelmoError('E_IMPORT', 'The vault Git branches and tags did not arrive unchanged.')
        if journal is not None:
            # Opening may add the requests table to an older journal; an added empty table loses nothing.
            census, expected = journal_census(state_root / 'journal.sqlite3'), sources[journal]['journal']
            if (census['operations'] != expected['operations']
                    or census.get('action_requests', []) != expected.get('action_requests', [])):
                raise KelmoError('E_IMPORT', 'Journal operations or requests did not arrive unchanged.')
            if 'session_state' in expected and census['session_state'] != expected['session_state']:
                raise KelmoError('E_IMPORT', 'Journal session state did not arrive unchanged.')
        for directory, name in state_trees.items():
            if name is not None and tree_files(state_root / directory) != sources[name]['files']:
                raise KelmoError('E_IMPORT', f'Captured {directory} bytes did not arrive unchanged.')
        return {'records': len(after), 'head': sources[vault]['head']}

    step('verify', verify)
    return state


# Cutover tail

def mark_cutover(control):
    """Record the imported state as the cutover baseline; writes after it form the tail."""
    from .config import Config
    config = Config.load(control)
    state = _state(control)
    if state is None or 'verify' not in state['steps']:
        raise KelmoError('E_IMPORT', 'Mark cutover only after a verified import.')
    path = Path(control) / CUTOVER
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    census = journal_census(config.state / 'journal.sqlite3')
    baseline = {'snapshot': state['snapshot'], 'records': archive_census(config.archive),
                'head': _git(config.knowledge_git_root, 'rev-parse', 'HEAD'),
                'operations': census['operations'], 'action_requests': census.get('action_requests', []),
                'session_state': census['session_state'],
                'marked_at': _now()}
    _write_json(path, baseline)
    return baseline


def tail(control):
    """Writes accepted after cutover: new record versions, Git commits, and journal operations and
    StateAPI requests and durable session state that changed since the baseline."""
    from .config import Config
    config = Config.load(control)
    baseline = json.loads((Path(control) / CUTOVER).read_text(encoding='utf-8'))
    known = {tuple(row) for row in baseline['records']}
    records = [row for row in archive_census(config.archive) if tuple(row) not in known]
    head = _git(config.knowledge_git_root, 'rev-parse', 'HEAD')
    commits = (_git(config.knowledge_git_root, 'rev-list', baseline['head'] + '..' + head).splitlines()
               if head != baseline['head'] else [])
    census = journal_census(config.state / 'journal.sqlite3')
    known_operations = {tuple(row) for row in baseline['operations']}
    operations = [row[0] for row in census['operations'] if tuple(row) not in known_operations]
    known_requests = {tuple(row) for row in baseline.get('action_requests', [])}
    requests = [row[0] for row in census.get('action_requests', []) if tuple(row) not in known_requests]
    known_sessions = baseline.get('session_state', {})
    current_sessions = census['session_state']
    session_tables = sorted(table for table in known_sessions.keys() | current_sessions.keys()
                            if known_sessions.get(table) != current_sessions.get(table))
    return {'records': records, 'commits': commits, 'operations': operations, 'requests': requests,
            'session_tables': session_tables,
            'empty': not (records or commits or operations or requests or session_tables)}


def rollback_check(control):
    """Refuse a rollback to the pre-cutover snapshot while it would lose accepted writes."""
    found = tail(control)
    if not found['empty']:
        raise KelmoError('E_TAIL', f"Rollback would lose {len(found['records'])} record versions, "
                                  f"{len(found['commits'])} commits, {len(found['operations'])} operations and "
                                  f"{len(found['requests'])} requests and changes in "
                                  f"{len(found['session_tables'])} session tables after cutover; recover forward instead.")
    return found


def vault_sources(config):
    """Everything a vault needs to come back: the archive, the journal, the knowledge repository, the
    captured source and transcript bytes and the server-side search log. The embedding model, caches,
    locks and the executor binding are controller configuration and are installed again instead."""
    from . import telemetry
    if (config.state / 'wiki-transaction.json').exists():
        raise KelmoError('E_IMPORT', 'A wiki transaction is unfinished; let it settle first.')
    sources = {'archive': {'kind': 'sqlite', 'path': config.archive},
               'journal': {'kind': 'sqlite', 'path': config.state / 'journal.sqlite3'},
               'vault': {'kind': 'git', 'path': config.knowledge_git_root}}
    if (config.state / 'sources').is_dir():
        sources['sources'] = {'kind': 'tree', 'path': config.state / 'sources'}
    if (config.state / 'sessions').is_dir():
        sources['sessions'] = {'kind': 'tree', 'path': config.state / 'sessions'}
    if telemetry.path(config).is_file():
        sources['telemetry'] = {'kind': 'sqlite', 'path': telemetry.path(config)}
    return sources


def restore_plan(config, sources):
    """The `snapshot import` specification that brings a snapshot of `sources` back, minus its roots."""
    return {'archive': 'archive', 'journal': 'journal', 'vault': 'vault', 'authority_id': config.vault_id,
            'label': config.label, 'registry_relative': config.registry_relative,
            'wiki_relative': config.wiki_relative,
            'settings': carried_settings((config.root / 'kelmo.toml').read_text(encoding='utf-8')),
            **{key: name for key, name in (('state_sources', 'sources'), ('state_sessions', 'sessions'),
                                           ('state_telemetry', 'telemetry')) if name in sources}}


BACKUP_INDEX = 'backups.json'
LAST_BACKUP = 'last-backup.json'
RESTORE_PLAN = 'restore.json'
BACKUP_NAME = re.compile(r'vault-\d{8}-\d{6}-[0-9a-f]{8}')


def backup(control, directory, *, keep=7):
    """One verified whole-vault snapshot under `directory`; the newest `keep` this tool made stay.

    The writers must be stopped (server fenced, workers stopped): the double-read barrier refuses a copy
    that changed underneath, and the worker lock is held for the copy so no local publication starts.
    The snapshot carries `restore.json`, the import specification a restore drill completes with fresh
    roots, and the vault records the verified result for `kelmo doctor`.
    """
    from .config import Config
    from .publication import coordinator_lock
    config = Config.load(control)
    if type(keep) is not int or keep < 1:
        raise KelmoError('E_INPUT', 'Backup retention must be a positive whole number.')
    directory = Path(directory).expanduser()
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise KelmoError('E_INPUT', 'The backup directory must be a real directory.')
    directory = directory.resolve()
    if any(directory.is_relative_to(root.resolve()) or root.resolve().is_relative_to(directory)
           for root in (config.root, config.state, config.archive.parent, config.knowledge_git_root)):
        raise KelmoError('E_INPUT', 'Keep backups outside the vault and its storage roots.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / (datetime.now(UTC).strftime('vault-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    try:
        with coordinator_lock(config):
            sources = vault_sources(config)
            manifest = take_snapshot(sources, destination)
        verify_snapshot(destination)
        _write_json(destination / RESTORE_PLAN, restore_plan(config, sources))
    except BaseException:
        # A copy that did not verify is no backup, and retention only removes what the index names.
        shutil.rmtree(destination, ignore_errors=True)
        raise
    index = directory / BACKUP_INDEX
    made = json.loads(index.read_text(encoding='utf-8')) if index.is_file() and not index.is_symlink() else []
    made = [name for name in made if isinstance(name, str) and BACKUP_NAME.fullmatch(name)
            and (directory / name / MANIFEST).is_file() and name != destination.name] + [destination.name]
    rotated = made[:-keep]
    for name in rotated:
        shutil.rmtree(directory / name)
    _write_json(index, made[-keep:])
    record = {'snapshot': manifest['id'], 'path': str(destination), 'created_at': manifest['created_at'],
              'verified_at': _now(), 'manifest_sha256': manifest['sha256'], 'sources': sorted(sources),
              'records': len(manifest['sources']['archive']['archive'] or ())}
    _write_json(config.state / LAST_BACKUP, record)
    return dict(record, rotated=rotated)


def last_backup(config):
    """The vault's last verified backup record with its age in hours, or None when it has none."""
    path = config.state / LAST_BACKUP
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        verified = datetime.fromisoformat(record['verified_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return {'unreadable': True}
    return dict(record, age_hours=round((datetime.now(UTC) - verified).total_seconds() / 3600, 1))


def forward_recover(control, snapshot, destination, *, roots):
    """Snapshot the current vault, tail included, and import it into a fresh destination.

    The destination keeps the vault's identity, storage layout and settings, holds its captured source
    bytes, and is marked as the new cutover baseline. Executor bindings under the state root are
    controller configuration and are bound again on the destination.
    """
    from .config import Config
    config = Config.load(control)
    sources = vault_sources(config)
    found = tail(control)
    take_snapshot(sources, snapshot)
    state = import_snapshot(snapshot, destination, roots=roots, **restore_plan(config, sources))
    recovered = Config.load(destination)
    present = {tuple(row) for row in archive_census(recovered.archive)}
    census = journal_census(recovered.state / 'journal.sqlite3')
    operations = {row[0] for row in census['operations']}
    requests = {row[0] for row in census.get('action_requests', [])}
    lost = ([row for row in found['records'] if tuple(row) not in present]
            + [op for op in found['operations'] if op not in operations]
            + [op for op in found['requests'] if op not in requests])
    if lost or _git(recovered.knowledge_git_root, 'rev-parse', 'HEAD') != _git(config.knowledge_git_root,
                                                                                'rev-parse', 'HEAD'):
        raise KelmoError('E_IMPORT', 'Forward recovery lost accepted writes.')
    mark_cutover(destination)
    return {'state': state, 'tail': found}
