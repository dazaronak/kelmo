"""Selected-wiki graph cache and same-snapshot Markdown reads."""
import fcntl
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager

from kelmo.store.graph.analysis import ANALYSIS_VERSION, LABEL, analysis_stats, assignment_digest, implementation
from kelmo.store.graph.build import ensure_schema, publish_graph, scan_wiki
from kelmo.store.graph.query import ANALYTICS, NAVIGATION, graph_query, resolve_entity
from kelmo.store.graph.stats import graph_stats

from .errors import KelmoError
from .journal import now


GRAPH_SCHEMA_VERSION = 1
GRAPH_ALGORITHM_VERSION = 1
_SNAPSHOT_KEY = 'graph.snapshot'
_FAILURE_KEY = 'graph.failure'
_ACTIONS = set(NAVIGATION) | set(ANALYTICS)
_MESSAGES = {
    'GRAPH_ABSENT': 'No trusted graph snapshot has been published.',
    'GRAPH_STALE': 'Selected wiki content has changed.',
    'GRAPH_BINDING': 'Graph snapshot belongs to a different wiki, vault, or graph version.',
    'E_GRAPH_ROOT': 'Selected wiki is missing, unsafe, or not a directory.',
    'E_GRAPH_SCAN': 'Selected wiki could not be scanned safely.',
    'E_GRAPH_BUILD': 'Graph publication failed.',
    'E_GRAPH_DB': 'Graph database operation failed.',
}
_FAILURE_CODES = ('E_GRAPH_ROOT', 'E_GRAPH_SCAN', 'E_GRAPH_BUILD', 'E_GRAPH_DB')


def _diagnostic(code, error_count=None):
    value = {'code': code, 'message': _MESSAGES[code]}
    if type(error_count) is int and error_count > 0:
        value['error_count'] = error_count
    return value


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise KelmoError('E_INPUT', f'{name} must be a nonempty string.', 2)


def _has_binding(record):
    return (
        isinstance(record, dict)
        and all(isinstance(record.get(key), str) and record[key]
                for key in ('wiki', 'vault_id'))
        and all(type(record.get(key)) is int
                for key in ('schema_version', 'algorithm_version'))
    )


def _analysis_label(metadata, record, relations=None):
    """Snapshot identity of derived topology; a stale or failed snapshot never reaches this.

    `snapshot` stays the source digest; `implementation` and `assignment_sha256` name the algorithm
    build and the stored group/hub/score assignment, so the same source analysed by another
    implementation is never presented under the earlier assignment's name."""
    label = dict(LABEL, snapshot=metadata['source_digest'], built_at=metadata['built_at'],
                 implementation=metadata.get('analysis_implementation'),
                 assignment_sha256=record.get('assignment_sha256') if isinstance(record, dict) else None)
    if relations is not None:
        label['relations'] = relations
    return label


def _same_binding(record, binding):
    return _has_binding(record) and all(record.get(key) == value
                                        for key, value in binding.items())


def _metadata(binding, record, failure, digest):
    trusted = _has_binding(record) and all(
        isinstance(record.get(key), str) and record[key]
        for key in ('source_digest', 'built_at'))
    matches = trusted and _same_binding(record, binding)
    if not trusted:
        status, diagnostic = 'absent', _diagnostic('GRAPH_ABSENT')
    elif not matches:
        status, diagnostic = 'stale', _diagnostic('GRAPH_BINDING')
    elif record['source_digest'] != digest:
        status, diagnostic = 'stale', _diagnostic('GRAPH_STALE')
    else:
        status, diagnostic = 'current', None
    if _same_binding(failure, binding) and failure.get('code') in _FAILURE_CODES:
        status = 'failed'
        diagnostic = _diagnostic(failure['code'], failure.get('error_count'))
    return dict(
        binding, status=status,
        source_digest=record['source_digest'] if matches else None,
        built_at=record['built_at'] if matches else None,
        observed_source_digest=digest,
        diagnostics=[diagnostic] if diagnostic else [],
    )


@contextmanager
def _savepoint(connection):
    name = 'memory_graph_' + uuid.uuid4().hex
    connection.execute(f'SAVEPOINT {name}')
    try:
        yield
        connection.execute(f'RELEASE SAVEPOINT {name}')
    except BaseException:
        if connection.in_transaction:
            connection.execute(f'ROLLBACK TO SAVEPOINT {name}')
            connection.execute(f'RELEASE SAVEPOINT {name}')
        raise


GRAPH_OPTIONS = dict(action=None, entity_id=None, query=None, target_id=None, radius=1, relation_filter=None,
                     limit=50, community_id=None, exclude_god_nodes=False, min_surprise=0.0, snapshot=None,
                     assignment=None)


def graph_params(options):
    """Validated graph query options, the defaults filled in: navigation, topology analytics and the
    snapshot/assignment pins. Every graph surface, local or through the restricted archive, uses this."""
    values = dict(GRAPH_OPTIONS)
    if options.keys() - values.keys():
        raise KelmoError('E_INPUT', 'Unknown graph query option.', 2)
    values.update(options)
    action = values['action']
    if not isinstance(action, str) or action not in _ACTIONS:
        raise KelmoError('E_INPUT', 'Choose search, neighbors, impact, path, details, communities, '
                        'god_nodes or surprises.', 2)
    if values['community_id'] is not None and (type(values['community_id']) is not int
                                               or values['community_id'] < 0):
        raise KelmoError('E_INPUT', 'community_id must be a whole number, 0 or more.', 2)
    if type(values['exclude_god_nodes']) is not bool:
        raise KelmoError('E_INPUT', 'exclude_god_nodes must be true or false.', 2)
    if type(values['min_surprise']) not in (int, float) or not 0 <= values['min_surprise'] <= 1:
        raise KelmoError('E_INPUT', 'min_surprise must be a number between 0 and 1.', 2)
    values['min_surprise'] = float(values['min_surprise'])
    for name, meaning in (('snapshot', 'the source_digest of a published graph'),
                          ('assignment', 'the assignment_sha256 of published analytics')):
        if values[name] is not None and (not isinstance(values[name], str)
                                         or not re.fullmatch('[0-9a-f]{64}', values[name])):
            raise KelmoError('E_INPUT', f'{name} must be {meaning}.', 2)
    for name in ('entity_id', 'query', 'target_id'):
        if values[name] is not None:
            _require_text(values[name], name)
    relations = values['relation_filter']
    if relations is not None:
        if not isinstance(relations, list):
            raise KelmoError('E_INPUT', 'relation_filter must be a list of strings.', 2)
        for relation in relations:
            _require_text(relation, 'relation_filter item')
    if type(values['radius']) is not int or not 1 <= values['radius'] <= 2:
        raise KelmoError('E_INPUT', 'Graph radius must be between 1 and 2.', 2)
    if type(values['limit']) is not int or not 1 <= values['limit'] <= 200:
        raise KelmoError('E_INPUT', 'Graph limit must be between 1 and 200.', 2)
    if action not in ANALYTICS:
        _require_text(values['query'] if action == 'search' else values['entity_id'],
                      'query' if action == 'search' else 'entity_id')
    if action == 'path':
        _require_text(values['target_id'], 'target_id')
    return values


class Graph:
    def __init__(self, service):
        self.service = service

    def query(self, **kwargs):
        return self._run('query', graph_params(kwargs))

    def stats(self):
        binding = self._binding()
        snapshot, diagnostic = self._scan(self.service.config.wiki)
        try:
            with _savepoint(self.service.archive):
                record = self._load(_SNAPSHOT_KEY)
                failure = self._load(_FAILURE_KEY)
                if diagnostic:
                    failure = dict(binding, code=diagnostic['code'])
                    if 'error_count' in diagnostic:
                        failure['error_count'] = diagnostic['error_count']
                metadata = _metadata(binding, record, failure,
                                     snapshot['source_digest'] if snapshot else None)
                result = {'graph': metadata, 'counts': graph_stats(self.service.archive)}
                if metadata['status'] == 'current':
                    result['analysis'] = dict(analysis_stats(self.service.archive),
                                              **_analysis_label(metadata, record))
                return result
        except sqlite3.Error as error:
            raise KelmoError('E_GRAPH_DB', _MESSAGES['E_GRAPH_DB']) from error

    def rebuild(self):
        return self._run('rebuild')

    def read(self, entity_id=None):
        _require_text(entity_id, 'entity_id')
        return self._run('read', {'entity_id': entity_id})

    def _binding(self):
        config = self.service.config
        # analysis_version: a snapshot published before derived topology lacks it and is rebuilt.
        # analysis_implementation: another analysis build (networkx release) republishes the groups.
        return dict(wiki=str(config.wiki), vault_id=config.vault_id,
                    schema_version=GRAPH_SCHEMA_VERSION,
                    algorithm_version=GRAPH_ALGORITHM_VERSION,
                    analysis_version=ANALYSIS_VERSION,
                    analysis_implementation=implementation())

    def _load(self, key):
        row = self.service.archive.execute(
            'SELECT value FROM archive_meta WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _put(self, key, value):
        self.service.archive.execute(
            '''INSERT INTO archive_meta (key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))

    def _remember_failure(self, binding, diagnostic):
        value = dict(binding, failed_at=now(), code=diagnostic['code'])
        if 'error_count' in diagnostic:
            value['error_count'] = diagnostic['error_count']
        self._put(_FAILURE_KEY, value)
        return value

    def _persist_failure(self, binding, diagnostic):
        try:
            with _savepoint(self.service.archive):
                self._remember_failure(binding, diagnostic)
        except sqlite3.Error as error:
            raise KelmoError('E_GRAPH_DB',
                            'Graph database operation failed; diagnostics could not be saved.') from error

    @staticmethod
    def _scan(wiki):
        try:
            if any(path.is_symlink() for path in (wiki, *wiki.parents)) or not wiki.is_dir():
                return None, _diagnostic('E_GRAPH_ROOT')
            snapshot = scan_wiki(wiki)
            if not isinstance(snapshot, dict):
                return None, _diagnostic('E_GRAPH_SCAN')
            errors = snapshot.get('errors')
            if not isinstance(errors, (list, tuple)) or errors:
                count = len(errors) if isinstance(errors, (list, tuple)) else None
                return None, _diagnostic('E_GRAPH_SCAN', count)
            if (not isinstance(snapshot.get('documents'), dict)
                    or not isinstance(snapshot.get('source_digest'), str)
                    or not snapshot['source_digest']):
                return None, _diagnostic('E_GRAPH_SCAN')
            return snapshot, None
        except Exception:  # Scanner faults must not expose source text or filesystem errors.
            return None, _diagnostic('E_GRAPH_SCAN')

    def _run(self, operation, parameters=None):
        connection = self.service.archive
        config = self.service.config
        with config.internal('.state/graph.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Config.load supplies a canonical path. Do not resolve a newly introduced symlink.
            wiki = config.wiki
            binding = self._binding()
            snapshot, diagnostic = self._scan(wiki)
            phase = 'database'
            try:
                with _savepoint(connection):
                    ensure_schema(connection)
                    record = self._load(_SNAPSHOT_KEY)
                    failure = self._load(_FAILURE_KEY)
                    if diagnostic:
                        failure = self._remember_failure(binding, diagnostic)
                        metadata = _metadata(binding, record, failure, None)
                        result = KelmoError(diagnostic['code'], diagnostic['message'])
                    else:
                        digest = snapshot['source_digest']
                        metadata = _metadata(binding, record, failure, digest)
                        if operation == 'rebuild' or metadata['status'] != 'current':
                            phase = 'publish'
                            publish_graph(connection, snapshot)
                            record = dict(binding, source_digest=digest, built_at=now(),
                                          assignment_sha256=assignment_digest(connection))
                            self._put(_SNAPSHOT_KEY, record)
                            connection.execute('DELETE FROM archive_meta WHERE key=?', (_FAILURE_KEY,))
                            metadata = _metadata(binding, record, None, digest)
                        phase = 'result'
                        if operation == 'rebuild':
                            result = {'graph': metadata, 'counts': graph_stats(connection),
                                      'analysis': dict(analysis_stats(connection),
                                                       **_analysis_label(metadata, record))}
                        elif operation == 'query':
                            result = self._query_snapshot(connection, parameters, metadata, record)
                        else:
                            result = self._read_snapshot(snapshot, parameters['entity_id'], metadata)
            except sqlite3.Error as error:
                # A read over intact published rows is not a failed publication: only the database and
                # publish phases are recorded as the graph's failure.
                if phase in ('database', 'publish'):
                    self._persist_failure(binding, _diagnostic('E_GRAPH_DB'))
                raise KelmoError('E_GRAPH_DB', _MESSAGES['E_GRAPH_DB']) from error
            except Exception as error:
                if phase != 'publish':
                    raise
                self._persist_failure(binding, _diagnostic('E_GRAPH_BUILD'))
                raise KelmoError('E_GRAPH_BUILD', _MESSAGES['E_GRAPH_BUILD']) from error
            # Semantic read errors must not roll back a successful refresh or failure diagnostics.
            if isinstance(result, KelmoError):
                raise result
            return result

    @staticmethod
    def _query_snapshot(connection, parameters, metadata, record):
        """Answer from the current snapshot; a caller holding a named older snapshot or analytics
        assignment is refused, never silently answered from a different one."""
        parameters = dict(parameters)
        named = parameters.pop('snapshot')
        assignment = parameters.pop('assignment')
        if named is not None and named != metadata['source_digest']:
            return KelmoError('E_GRAPH_STALE', 'The named graph snapshot is no longer current; '
                             'query again without it, or with the current source_digest.')
        if assignment is not None and assignment != (record or {}).get('assignment_sha256'):
            return KelmoError('E_GRAPH_STALE', 'The named analytics assignment is no longer current; '
                             'query again without it, or with the current assignment_sha256.')
        result = {'result': graph_query(connection, **parameters), 'graph': metadata}
        # Navigation nodes carry the derived community_id, surprise_score and is_god_node too, and
        # exclude_god_nodes filters by derived hubs: every answer names the assignment behind them.
        relations = (sorted({item.casefold() for item in parameters['relation_filter']})
                     if parameters['relation_filter'] is not None and parameters['action'] == 'surprises'
                     else None)
        result['analysis'] = _analysis_label(metadata, record, relations)
        return result

    def _read_snapshot(self, snapshot, entity_id, metadata):
        page_id, error = resolve_entity(self.service.archive, entity_id)
        if error:
            return KelmoError('E_AMBIGUOUS',
                             'Page identifier is ambiguous; use an explicit wiki-relative ID.')
        document = snapshot['documents'].get(page_id) if isinstance(page_id, str) else None
        if document is None:
            return KelmoError('E_NOT_FOUND', 'Requested entity is not a current wiki page.')
        return {'id': page_id, 'file_path': document['file_path'],
                'content': document['content'], 'graph': metadata}
