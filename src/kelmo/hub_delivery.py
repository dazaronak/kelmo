"""Verified HOT set storage and bounded session delivery.

The caller owns transport and must acknowledge a refresh only after its output succeeds.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from .errors import KelmoError


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def published_set(reader, generation, projects, enrollment_revision):
    """Read all generated files from one already pinned Git generation."""
    projects = sorted(set(projects))
    if any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', p)
           or p == 'mandatory' for p in projects):
        raise KelmoError('E_CONFIG', 'Invalid enrolled HOT project.')
    pinned = reader._pinned(generation)
    hot = PurePosixPath(reader.wiki_path).parent / 'hot'
    names = ['mandatory.md'] + [p + suffix for p in projects for suffix in ('.md', '.full.md')]
    files = {}
    try:
        for name in names:
            raw = reader._blob(pinned['entries'], str(hot / name))
            files[name] = {'text': raw.decode('utf-8'), 'sha256': _hash(raw)}
    except (OSError, UnicodeError) as error:
        raise KelmoError('E_GENERATION', 'The published HOT set is missing or invalid.') from error
    return _validate({'generation': generation.publication_id, 'enrollment_revision': enrollment_revision,
                      'manifest_sha256': generation.manifest_sha256, 'projects': projects, 'files': files})


def _validate(snapshot):
    try:
        generation, revision = snapshot['generation'], snapshot['enrollment_revision']
        projects, files = snapshot['projects'], snapshot['files']
        if (type(generation) is not int or generation < 1 or type(revision) is not int or revision < 0
                or not re.fullmatch(r'[0-9a-f]{64}', snapshot['manifest_sha256'])
                or not isinstance(projects, list) or len(set(projects)) != len(projects)
                or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', p)
                       or p == 'mandatory' for p in projects)):
            raise ValueError('invalid set identity')
        names = {'mandatory.md'} | {p + suffix for p in projects for suffix in ('.md', '.full.md')}
        if not isinstance(files, dict) or set(files) != names:
            raise ValueError('incomplete set')
        for item in files.values():
            raw = item['text'].encode('utf-8')
            if (item['sha256'] != _hash(raw)
                    or not item['text'].startswith(f'<!-- kelmo generation {generation} -->\n')):
                raise ValueError('file hash or generation mismatch')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise KelmoError('E_CACHE', 'HOT set is incomplete or failed validation.') from error
    return snapshot


def _set_metadata(root, name):
    root = Path(root)
    try:
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('invalid set name')
        directory = (root / name).resolve(strict=True)
        if directory.parent != root.resolve() or not directory.name.startswith('.set-'):
            raise ValueError('invalid current target')
        metadata_path = directory / 'set.json'
        if metadata_path.is_symlink():
            raise ValueError('invalid metadata')
        raw_metadata = metadata_path.read_bytes()
        metadata = json.loads(raw_metadata)
        expected_name = (f".set-{metadata['generation']}-{metadata['enrollment_revision']}-"
                         + _hash(_encoded(metadata)))
        if directory.name != expected_name or raw_metadata != _encoded(metadata):
            raise ValueError('metadata hash mismatch')
        projects = metadata['projects']
        if (not isinstance(projects, list)
                or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', p)
                       or p == 'mandatory' for p in projects)):
            raise ValueError('invalid project')
        names = {'mandatory.md'} | {p + suffix for p in projects for suffix in ('.md', '.full.md')}
        if not isinstance(metadata['files'], dict) or set(metadata['files']) != names:
            raise ValueError('invalid files')
        if (type(metadata['generation']) is not int or metadata['generation'] < 1
                or type(metadata['enrollment_revision']) is not int or metadata['enrollment_revision'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', metadata['manifest_sha256'])
                or any(not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                       for checksum in metadata['files'].values())):
            raise ValueError('invalid metadata identity')
        return directory, metadata
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise KelmoError('E_CACHE', 'No verified HOT metadata is available; preserve the cache for repair.') from error


def read_set(root, name='current'):
    directory, metadata = _set_metadata(root, name)
    try:
        files = {}
        for filename, checksum in metadata['files'].items():
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError('invalid file')
            files[filename] = {'sha256': checksum, 'text': path.read_text(encoding='utf-8')}
        metadata['files'] = files
        return _validate(metadata)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise KelmoError('E_CACHE', 'No verified complete HOT set is available.') from error


def _metadata(snapshot):
    result = {key: snapshot[key] for key in ('generation', 'enrollment_revision', 'manifest_sha256', 'projects')}
    result['files'] = {name: value['sha256'] for name, value in snapshot['files'].items()}
    return result


def install_set(root, snapshot):
    snapshot = _validate(snapshot)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = _metadata(snapshot)
        if (root / 'current').is_symlink():
            _, current = _set_metadata(root, 'current')
            if (snapshot['generation'] < current['generation']
                    or snapshot['enrollment_revision'] < current['enrollment_revision']):
                raise KelmoError('E_CACHE', 'Refusing an older HOT generation or enrollment revision.')
            if ((snapshot['generation'], snapshot['enrollment_revision'])
                    == (current['generation'], current['enrollment_revision']) and metadata != current):
                raise KelmoError('E_CACHE', 'HOT set changed without a generation or enrollment revision change.')
            if (snapshot['generation'] == current['generation']
                    and snapshot['manifest_sha256'] != current['manifest_sha256']):
                raise KelmoError('E_CACHE', 'One HOT generation cannot name different manifests.')
        checksum = _hash(_encoded(metadata))
        name = f".set-{snapshot['generation']}-{snapshot['enrollment_revision']}-{checksum}"
        destination = root / name
        if destination.exists():
            _, existing = _set_metadata(root, name)
            if existing != metadata:
                raise KelmoError('E_CACHE', 'Existing set metadata differs from the verified response.')
            try:
                read_set(root, name)
            except KelmoError:
                # Keep the verified metadata in place throughout repair so interruption retains its fence.
                for filename, item in snapshot['files'].items():
                    fd, staged = tempfile.mkstemp(prefix='.repair-', dir=destination)
                    try:
                        with os.fdopen(fd, 'wb') as out:
                            out.write(item['text'].encode('utf-8'))
                            out.flush()
                            os.fsync(out.fileno())
                        os.replace(staged, destination / filename)
                    finally:
                        Path(staged).unlink(missing_ok=True)
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
            try:
                for filename, item in snapshot['files'].items():
                    (staging / filename).write_text(item['text'], encoding='utf-8')
                (staging / 'set.json').write_bytes(_encoded(metadata))
                os.rename(staging, destination)
            finally:
                if staging.exists():
                    import shutil
                    shutil.rmtree(staging)
        # Verify staged bytes before making them visible, including an existing set directory.
        for filename, checksum_file in metadata['files'].items():
            if _hash((destination / filename).read_bytes()) != checksum_file:
                raise KelmoError('E_CACHE', 'Staged HOT set failed verification.')
        if (destination / 'set.json').read_bytes() != _encoded(metadata):
            raise KelmoError('E_CACHE', 'Staged HOT metadata failed verification.')
        fd, temporary_name = tempfile.mkstemp(prefix='.current-', dir=root)
        os.close(fd)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            temporary.symlink_to(name)
            os.replace(temporary, root / 'current')
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def context_reference(root, data):
    """Pin dynamic context to one private file whose name identifies its exact bytes."""
    raw = _encoded(data)
    directory = Path(root) / 'hub-context'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise KelmoError('E_CACHE', 'Context reference directory is unsafe.')
    path = directory / (_hash(raw) + '.json')
    if path.is_symlink():
        raise KelmoError('E_CACHE', 'Context reference file is unsafe.')
    if path.is_file() and path.read_bytes() == raw:
        return path
    fd, name = tempfile.mkstemp(prefix='.context-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def pending_text(pending):
    """The pending block agents see: at most ten rows, then a count, under a digest of the whole set."""
    pending = pending or []
    lines = [f"- {item['pending']} [{item.get('project') or '?'}" +
             (' (retrying)' if item.get('state') == 'parked' else '') + ']' for item in pending[:10]]
    if len(pending) > 10:
        lines.append(f'+{len(pending) - 10} more')
    return ('Pending wiki updates (digest ' + _hash(_encoded(pending)) + '):\n' + '\n'.join(lines)) if pending else ''


def header_line(snapshot, stale=False):
    return (f"Kelmo HOT {'STALE CACHE' if stale else 'generation'} {snapshot['generation']} "
            f"(manifest {snapshot['manifest_sha256'][:12]}, enrollment {snapshot['enrollment_revision']}).")


def unchanged_marker(snapshot, project, current=None):
    """One line in place of a bundle the session already received: names the generation and where
    to read it, so a copy lost to a rewind, a dropped hook output or eviction can be recovered."""
    where = (' read ' + str(Path(current) / ((project + '.md') if project else 'mandatory.md')) + '.'
             if current is not None else ' restart the session to receive it again.')
    return (f"Kelmo HOT generation {snapshot['generation']} unchanged (manifest {snapshot['manifest_sha256'][:12]}); "
            'delivered earlier this session. If it is missing from your context,' + where)


def assemble(header, mandatory, hub, work, pending, *, provider, pointer, work_pointer=None, mandatory_pointer=None, pending_pointer=None, limit=10000):
    pending = pending or []
    pending_digest = _hash(_encoded(pending))
    pending_block = pending_text(pending)
    full = '\n\n'.join(part for part in (header, mandatory, hub, work, pending_block) if part)
    if provider != 'claude' or len(full) <= limit:
        return full
    required = '\n\n'.join(part for part in (header, mandatory) if part)
    work_source = (work_pointer() if callable(work_pointer) else work_pointer) if work else None
    def pending_source():
        return (pending_pointer() if callable(pending_pointer) else pending_pointer) or 'the pending source supplied by the caller'
    def budget_error(reason):
        sources = ['mandatory at ' + (mandatory_pointer or pointer)]
        if hub:
            sources.append('hub at ' + pointer)
        if work and work_source:
            sources.append('bound work at ' + work_source)
        if pending:
            sources.append('pending updates at ' + pending_source())
        return KelmoError('E_BUDGET', reason + '; read or import ' + '; '.join(sources) + '.')
    suffix = '\n\nOmitted context: read or import ' + pointer
    if work and work_source:
        suffix += '; read bound work at ' + work_source
    suffix += '.'
    if len(required) > limit:
        raise budget_error('Whole mandatory content exceeds the Claude hook limit')
    # Prefer the useful, complete pending list over a partial hub; keep an explicit pointer for omissions.
    tail = ('\n\n' + pending_block if pending_block else '') + suffix
    if len(required) + len(tail) > limit:
        tail = ('\n\nPending updates omitted (digest ' + pending_digest + '); read '
                + pending_source() + '.' if pending else '') + suffix
    if len(required) + len(tail) > limit:
        raise budget_error('Whole mandatory content leaves no room for the omission notice')
    room = limit - len(required) - len(tail) - 2
    excerpt = hub[:max(0, room)].rstrip()
    return required + ('\n\n' + excerpt if excerpt else '') + tail


UNLINKED_NOTICE = ('No Kelmo project is linked to this directory, so no project hub was delivered. Run '
                   '`kelmo projects --json` to list projects and their repositories, or '
                   '`kelmo link --project NAME` in this repository to link it.')


def deliver(snapshot, project, work, pending, *, provider, current, stale=False, limit=10000, work_path=None,
            work_pointer=None, pending_pointer=None, unlinked=False):
    _validate(snapshot)
    if project and project not in snapshot['projects']:
        raise KelmoError('E_BINDING', 'Selected project is absent from the delivered HOT set.')
    files = snapshot['files']
    header = header_line(snapshot, stale)
    mandatory = files['mandatory.md']['text']
    # A session in a directory no project claims gets told so, instead of a bare generation line.
    hub = files[project + '.md']['text'] if project else (UNLINKED_NOTICE if unlinked else '')
    pointer = str(Path(current) / (project + '.md' if project else 'mandatory.md')) if current is not None else None
    return assemble(header, mandatory, hub, work, pending, provider=provider, pointer=pointer,
                    mandatory_pointer=str(Path(current) / 'mandatory.md') if current is not None else None, pending_pointer=pending_pointer,
                    work_pointer=work_pointer or (('the bound work page via kelmo graph read ' + work_path) if work_path else None),
                    limit=limit)


def session_state(root, payload, provider):
    native = payload.get('session_id') or payload.get('thread_id')
    if not isinstance(native, str) or not native:
        return None
    return Path(root) / 'hub-sessions' / (_hash(_encoded([provider, native])) + '.json')


RESTART_WARNING = 'Kelmo context changed. Restart the session if removed or contradicted facts remain in context.\n\n'


def refresh(state_path, bundle, *, verified, stale=False, compacted=False, identity=None, core=None,
            marker='', pending_only=''):
    """Return context and a zero-argument acknowledgement to invoke after successful output.

    The session record holds the digest of the last delivered identity and, when `core` is given,
    of the identity without its pending block. A verified, unchanged bundle is replaced by `marker`
    (empty by default). A verified bundle whose only change is the pending block is replaced by
    `pending_only` when given. The restart warning precedes a bundle whose core changed; a pending
    block alone removes or contradicts nothing.
    """
    state_path = Path(state_path)
    digest = _hash((bundle if identity is None else identity).encode('utf-8'))
    core_digest = _hash(core.encode('utf-8')) if core is not None else None
    try:
        record = json.loads(state_path.read_text(encoding='utf-8'))
        previous, previous_core = record.get('digest'), record.get('core')
    except (OSError, ValueError, AttributeError):
        previous = previous_core = None
    changed = previous != digest
    core_changed = changed if core_digest is None else previous_core != core_digest
    forced = not verified or stale or compacted
    if not changed and not forced:
        context = marker
    elif changed and not forced and pending_only and previous and not core_changed:
        context = pending_only
    else:
        context = bundle
        if context and previous and changed and core_changed:
            context = RESTART_WARNING + context

    def commit():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.delivery-', dir=state_path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(_encoded({'digest': digest, 'core': core_digest}))
            os.replace(name, state_path)
        finally:
            Path(name).unlink(missing_ok=True)
    return {'context': context, 'commit': commit}


def prepare(config, payload, snapshot, project, work, pending, *, provider, current, stale, acknowledge, notice='',
            work_path=None, aliases=None, work_pointer=None, unlinked=False, action='prompt'):
    """Synchronize the selected embed and prepare hook output, deferring acknowledgement.

    SessionStart (`action` 'start') always delivers the bundle. A later prompt delivers it again when
    it changed, when the set is stale, or when an instruction-file integration is selected but not
    verified; without any integration the session's own delivery record suffices, and an unchanged
    bundle becomes a one-line marker.
    """
    from .hub_embed import sync
    pending_pointer = None
    full = deliver(snapshot, project, work, pending, provider='codex', current=None, stale=stale, unlinked=unlinked)
    core = deliver(snapshot, project, work, [], provider='codex', current=None, stale=stale, unlinked=unlinked)
    published = snapshot['files'][project + '.full.md' if project else 'mandatory.md']['text']
    sync_failed = False
    try:
        selection = ({'verified': False, 'changed': False, 'repair': ''} if stale
                     else sync(config, project, provider, content=published, cwd=payload.get('cwd'), snapshot=snapshot, aliases=aliases))
    except (KelmoError, OSError, ValueError) as error:
        selection = {'verified': False, 'changed': False,
                     'repair': 'Hub embed update failed: ' + str(error) + '. It will retry next turn.'}
        sync_failed = True
    if provider == 'claude' and project and not stale and not sync_failed:
        # Claude Code also loads AGENTS.md, so a Codex block embedded in this repository is kept
        # current from the Claude hook too; otherwise the two would show different generations.
        try:
            companion = sync(config, project, 'codex', content=published, cwd=payload.get('cwd'),
                             snapshot=snapshot, aliases=aliases)
            if companion.get('changed'):
                selection = dict(selection, changed=True)
        except KelmoError as error:
            if error.code != 'E_CONFIG':  # an ambiguous Codex target is the Codex hook's own repair
                raise
        except (OSError, ValueError) as error:
            selection = dict(selection, repair='; '.join(filter(None, (
                selection.get('repair'), 'Codex block update failed: ' + str(error) + '. It will retry next turn.'))))
    repair = selection.get('repair') or ''
    hook_mode = selection.get('mode') == 'hook'
    installed = None if callable(current) else current  # a known copy the marker can point at
    state = session_state(config.state, payload, provider)
    identity = full + '\n' + repair + '\n' + notice
    core_identity = core + '\n' + repair + '\n' + notice
    previous = None
    if state is not None and acknowledge is not None:
        try:
            previous = json.loads(state.read_text(encoding='utf-8')).get('digest')
        except (OSError, ValueError, AttributeError):
            pass
    reserve = sum(len(part) + 2 for part in (repair, notice) if part)
    if previous and previous != _hash(identity.encode('utf-8')):
        reserve += len(RESTART_WARNING)
    complete = provider != 'claude' or len(full) <= 10000 - reserve
    if not complete:
        current = current() if callable(current) else current
        reference = None
        def omitted_source(field):
            nonlocal reference
            if reference is None:
                reference = context_reference(config.state, {
                    'generation': snapshot['generation'], 'enrollment_revision': snapshot['enrollment_revision'],
                    'manifest_sha256': snapshot['manifest_sha256'], 'project': project, 'stale': stale,
                    'work': work, 'work_path': work_path, 'pending': pending})
            return str(reference) + ' (' + field + ' field)'
        if work:
            def work_pointer():
                return omitted_source('work')
        if pending:
            def pending_pointer():
                return omitted_source('pending')
    else:
        current = None
    context = deliver(snapshot, project, work, pending, provider=provider, current=current, stale=stale,
                      limit=10000 - reserve if provider == 'claude' else 10000, work_path=work_path,
                      work_pointer=work_pointer, pending_pointer=pending_pointer, unlinked=unlinked)
    for part in (repair, notice):
        if part:
            context += '\n\n' + part
    if state is not None and acknowledge is not None:
        # An unverified or altered import must not suppress current memory; a missing `mode` (stale
        # cache, failed sync) means the same. Hook mode trusts this session's own delivery record.
        verified = (complete and not selection.get('changed')
                    and (hook_mode or bool(selection.get('verified'))))
        pending_only = ''
        if hook_mode:
            pending_only = '\n\n'.join(part for part in (
                header_line(snapshot, stale), pending_text(pending) or 'Pending wiki updates: none remain.',
                repair, notice) if part)
        update = refresh(state, context, verified=verified, stale=stale,
                         compacted=action == 'start' or payload.get('source') in ('compact', 'compaction'),
                         identity=identity, core=core_identity,
                         marker=unchanged_marker(snapshot, project, installed) if hook_mode else '',
                         pending_only=pending_only)
        context = update['context']
        if not sync_failed and complete:
            acknowledge(update['commit'])
    return context


def legacy_delivery(root, delivered, pending, *, provider, action, notice=''):
    """Budget the complete legacy response once, preserving every omitted byte."""
    from .hot import pending_block
    warning = ('Kelmo context refreshed; restart if earlier facts were removed or contradicted.'
               if action == 'prompt' else '')
    context = '\n\n'.join(part for part in (warning, delivered['context'], pending_block(pending), notice) if part)
    if provider != 'claude' or len(context) <= 10000:
        return context
    reference = context_reference(root, {'context': context, 'pending': pending,
                                         'generation': delivered.get('generation')})
    pointer = ('Kelmo HOT exceeds the hook limit. Read the complete saved context and pending updates at '
               + str(reference) + ' (context and pending fields).')
    return '\n\n'.join(part for part in (warning, pointer, notice) if part)
