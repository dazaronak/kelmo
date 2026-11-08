"""Scan a selected wiki and atomically publish its derived graph."""

import hashlib
import json
import os
import posixpath
import sqlite3
import stat
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from kelmo.store.graph.analysis import analyze_graph
from kelmo.store.graph.index import rebuild_entity_index
from kelmo.store.graph.parser import lookup_keys, parse_text, slugify


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _safe_relative(value: str) -> str:
    return ''.join(
        f'\\u{ord(c):04x}' if ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF else c
        for c in value
    ) or '.'


def _error(file: str, reason: str) -> dict:
    return {'file': _safe_relative(file), 'reason': reason}


def _valid_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and '..' not in path.parts
        and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _capture(root: Path):
    """Read through directory descriptors; no descendant symlink is followed."""
    files, stamps, errors = {}, {}, []

    def walk(directory, prefix):
        before = _stamp(os.fstat(directory))
        stamps[prefix or '.'] = before
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            relative = f'{prefix}/{name}' if prefix else name
            if not _valid_relative(relative):
                errors.append(_error(relative, 'unsafe_path'))
                continue
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                expected = _stamp(info)
                if stat.S_ISLNK(info.st_mode):
                    errors.append(_error(relative, 'unsafe_path'))
                    continue
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                    try:
                        if _stamp(os.fstat(child)) != expected:
                            raise ValueError('scan_changed')
                        walk(child, relative)
                    finally:
                        os.close(child)
                    if _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected:
                        raise ValueError('scan_changed')
                elif name.endswith('.md'):
                    if not stat.S_ISREG(info.st_mode):
                        errors.append(_error(relative, 'unsafe_path'))
                        continue
                    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
                    try:
                        opened = os.fstat(descriptor)
                        if not stat.S_ISREG(opened.st_mode) or _stamp(opened) != expected:
                            raise ValueError('scan_changed')
                        with os.fdopen(descriptor, 'rb', closefd=False) as source:
                            data = source.read()
                        if _stamp(os.fstat(descriptor)) != expected:
                            raise ValueError('scan_changed')
                        if _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected:
                            raise ValueError('scan_changed')
                        files[relative], stamps[relative] = data, expected
                    finally:
                        os.close(descriptor)
            except OSError:
                errors.append(_error(relative, 'read_error'))
            except ValueError:
                errors.append(_error(relative, 'scan_changed'))
        if _stamp(os.fstat(directory)) != before:
            errors.append(_error(prefix or '.', 'scan_changed'))

    descriptor = None
    try:
        initial = root.lstat()
        if stat.S_ISLNK(initial.st_mode):
            errors.append(_error('.', 'unsafe_path'))
        elif not stat.S_ISDIR(initial.st_mode):
            errors.append(_error('.', 'wiki_unavailable'))
        else:
            descriptor = os.open(root, _DIR_FLAGS)
            if _stamp(os.fstat(descriptor)) != _stamp(initial):
                errors.append(_error('.', 'scan_changed'))
            walk(descriptor, '')
            if _stamp(root.lstat()) != _stamp(initial):
                errors.append(_error('.', 'scan_changed'))
    except (OSError, ValueError):
        errors.append(_error('.', 'wiki_unavailable'))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return files, stamps, errors


def _entity_type_from_path(relative: str) -> str:
    return {'projects': 'PROJECT', 'people': 'PERSON', 'tools': 'TOOL'}.get(
        PurePosixPath(relative).parts[0].casefold(), 'ARTICLE'
    )


def _virtual_node(entity_id, name, entity_type):
    return {
        'id': entity_id, 'name': name, 'type': entity_type,
        'file_path': None, 'line_number': None, 'status': None, 'aliases': [],
    }


def _edge(source, target, relation, file, line, confidence='EXTRACTED'):
    return {
        'source_id': source, 'target_id': target, 'relation': relation,
        'confidence': confidence, 'weight': 1.0, 'source_file': file, 'source_line': line,
    }


def _digest(files) -> str:
    digest = hashlib.sha256(b'kelmo-selected-wiki-graph-v1\0')
    for relative, data in sorted(files.items()):
        path = relative.encode('utf-8')
        for part in (path, data):
            digest.update(len(part).to_bytes(8, 'big'))
            digest.update(part)
    return digest.hexdigest()


def scan_wiki(wiki_dir) -> dict:
    """Capture, parse and verify one snapshot without accessing a database."""
    try:
        root = Path(wiki_dir).absolute()
        files, stamps, errors = _capture(root)
    except (OSError, ValueError, TypeError):
        root = None
        files, stamps, errors = {}, {}, [_error('.', 'wiki_unavailable')]
    documents, parsed_documents, candidates = {}, {}, defaultdict(set)
    for relative, data in sorted(files.items()):
        entity_id = unicodedata.normalize('NFC', relative[:-3])
        if not entity_id or entity_id.rsplit('/', 1)[-1] in ('', '.', '..'):
            errors.append(_error(relative, 'invalid_id'))
            continue
        if entity_id.casefold().startswith(('tag:', 'unresolved:')):
            errors.append(_error(relative, 'reserved_id'))
            continue
        if entity_id in documents:
            errors.append(_error(relative, 'duplicate_id'))
            continue
        try:
            parsed = parse_text(data)
            if parsed['error']:
                errors.append(_error(relative, 'parse_error'))
                continue
            if any(alias.casefold().startswith(('tag:', 'unresolved:')) for alias in parsed['aliases']):
                errors.append(_error(relative, 'reserved_alias'))
                continue
            node = {
                'id': entity_id,
                'name': parsed['title'] or PurePosixPath(relative).stem.replace('-', ' ').replace('_', ' ').title(),
                'type': _entity_type_from_path(relative), 'file_path': relative,
                'line_number': parsed['line_number'], 'status': parsed['status'],
                'aliases': parsed['aliases'],
            }
            documents[entity_id] = {**node, 'content': data.decode('utf-8')}
            parsed_documents[entity_id] = parsed
            for key in lookup_keys(entity_id, parsed['aliases']):
                candidates[key].add(entity_id)
        except Exception:
            errors.append(_error(relative, 'parse_error'))

    # Verification rereads bytes, not just timestamps, after parsing. Renames,
    # file-list changes and same-size edits therefore invalidate this scan.
    if root is not None:
        verified_files, verified_stamps, verification_errors = _capture(root)
        errors.extend(verification_errors)
        changed = {
            relative for relative in stamps.keys() | verified_stamps.keys()
            if stamps.get(relative) != verified_stamps.get(relative)
        }
        changed.update(
            relative for relative in files.keys() | verified_files.keys()
            if files.get(relative) != verified_files.get(relative)
        )
        errors.extend(_error(relative, 'scan_changed') for relative in sorted(changed))

    nodes = {key: {field: value for field, value in document.items() if field != 'content'}
             for key, document in documents.items()}
    edges, missing, ambiguous = {}, 0, 0
    for source, document in documents.items():
        parsed = parsed_documents[source]
        relations = [
            (link['target'], 'links_to', link['line'], link['relative'], link['invalid'])
            for link in parsed['links']
        ]
        if parsed['parent']:
            relations.append((
                parsed['parent'], 'child_of', parsed['metadata_lines'].get('parent', 1),
                parsed['parent_relative'], parsed['parent_invalid'],
            ))
        for target, relation, line, relative, invalid in relations:
            contextual = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            if invalid or contextual == '..' or contextual.startswith('../') or contextual.startswith('/'):
                errors.append(_error(document['file_path'], 'link_outside_wiki'))
                continue
            if relative:
                matches = {contextual} if contextual in documents else set()
            elif target in documents:
                matches = {target}
            elif contextual in documents:
                # Exact, then the linking page's own folder, then the unique short name: the order the
                # publisher's link check (wiki/links.py) and the original archive resolver use.
                matches = {contextual}
            else:
                matches = candidates.get(slugify(target), set())
            if len(matches) == 1:
                destination = next(iter(matches))
            else:
                missing += not matches
                ambiguous += len(matches) > 1
                destination = 'unresolved:' + (contextual if relative else target)
                nodes.setdefault(destination, _virtual_node(destination, target, 'CONCEPT'))
            if destination != source:
                edges.setdefault((source, destination, relation), _edge(
                    source, destination, relation, document['file_path'], line,
                    'AMBIGUOUS' if len(matches) > 1 else 'EXTRACTED',
                ))
        for tag in parsed['tags']:
            destination = 'tag:' + tag
            nodes.setdefault(destination, _virtual_node(destination, tag, 'TAG'))
            edges.setdefault((source, destination, 'tagged_with'), _edge(
                source, destination, 'tagged_with', document['file_path'],
                parsed['metadata_lines'].get('tags', 1),
            ))
    errors = [dict(file=file, reason=reason) for file, reason in sorted({
        (error['file'], error['reason']) for error in errors
    })]
    return {
        'documents': documents, 'nodes': nodes, 'edges': list(edges.values()),
        'errors': errors, 'source_digest': None if errors else _digest(files),
        'report': {
            'files': len(files), 'entities': len(nodes), 'edges': len(edges),
            'parse_errors': len(errors), 'unresolved_links': missing, 'ambiguous_links': ambiguous,
        },
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add compatible graph columns only, preserving any caller transaction."""
    additions = {
        'entities': {'status': 'TEXT', 'aliases': "TEXT NOT NULL DEFAULT '[]'", 'community_id': 'INTEGER',
                     'surprise_score': 'REAL', 'is_god_node': 'INTEGER DEFAULT 0'},
        'edges': {'source_file': 'TEXT', 'source_line': 'INTEGER'},
    }
    conn.execute('SAVEPOINT archive_graph_schema')
    try:
        for table, fields in additions.items():
            existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if not existing:
                raise ValueError('Archive graph tables are missing')
            for field, definition in fields.items():
                if field not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {field} {definition}')
        conn.execute('RELEASE archive_graph_schema')
    except BaseException:
        conn.execute('ROLLBACK TO archive_graph_schema')
        conn.execute('RELEASE archive_graph_schema')
        raise


def publish_graph(conn: sqlite3.Connection, snapshot: dict) -> dict:
    """Replace graph rows and their trigger-maintained FTS index atomically."""
    if snapshot['errors']:
        raise ValueError('Cannot publish an error snapshot')
    nodes = snapshot['nodes']
    if any(edge['source_id'] not in nodes or edge['target_id'] not in nodes for edge in snapshot['edges']):
        raise ValueError('Graph edge references a missing node')
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('SAVEPOINT archive_graph_publish')
    try:
        ensure_schema(conn)
        conn.execute('DELETE FROM edges')
        conn.execute('DELETE FROM entities')
        for node in sorted(nodes.values(), key=lambda item: item['id']):
            conn.execute(
                'INSERT INTO entities '
                '(id,name,type,file_path,line_number,status,aliases,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (node['id'], node['name'], node['type'], node['file_path'], node['line_number'],
                 node['status'], json.dumps(node['aliases'], ensure_ascii=False), now, now),
            )
        for edge in sorted(snapshot['edges'], key=lambda item: (item['source_id'], item['target_id'], item['relation'])):
            conn.execute(
                'INSERT INTO edges '
                '(source_id,target_id,relation,confidence,weight,source_file,source_line,created_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (*(edge[field] for field in (
                    'source_id', 'target_id', 'relation', 'confidence', 'weight', 'source_file', 'source_line',
                )), now),
            )
        conn.execute(
            'UPDATE entities SET '
            'out_degree=(SELECT COUNT(*) FROM edges WHERE source_id=entities.id), '
            'in_degree=(SELECT COUNT(*) FROM edges WHERE target_id=entities.id)'
        )
        # Derived topology and the name index belong to this snapshot: both publish with its rows or not at all.
        analysis = analyze_graph(conn)
        rebuild_entity_index(conn)
        conn.execute('RELEASE archive_graph_publish')
    except BaseException:
        conn.execute('ROLLBACK TO archive_graph_publish')
        conn.execute('RELEASE archive_graph_publish')
        raise
    return {'status': 'ok', **snapshot['report'], 'source_digest': snapshot['source_digest'], 'built_at': now,
            'analysis': analysis}


def rebuild_graph(conn: sqlite3.Connection, wiki_dir=None, dry_run: bool = False) -> dict:
    """Explicit rebuild of the supplied wiki; there is no implicit root."""
    if wiki_dir is None:
        return {'status': 'error', 'message': 'Select a wiki explicitly, or use kelmo graph rebuild --vault PATH.'}
    snapshot = scan_wiki(wiki_dir)
    if snapshot['errors']:
        return {
            'status': 'error', 'message': 'Wiki scan failed; previous graph retained',
            **snapshot['report'], 'errors': snapshot['errors'], 'source_digest': snapshot['source_digest'],
        }
    if dry_run:
        return {'status': 'dry_run', **snapshot['report'], 'source_digest': snapshot['source_digest']}
    return publish_graph(conn, snapshot)
