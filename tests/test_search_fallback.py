import sqlite3
from unittest.mock import patch

import pytest

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.db import init_schema, open_connection
from kelmo.store.memory.search import memory_search


@pytest.fixture
def tmp_db(tmp_path):
    connection = open_connection(str(tmp_path / 'archive.db'))
    init_schema(connection)
    try:
        yield connection, tmp_path / 'archive.db'
    finally:
        connection.close()


@pytest.fixture
def service(tmp_path):
    from kelmo.service import Service

    root = tmp_path / 'vault'
    Service.initialize(root, 'test-node')
    with Service(root) as value:
        yield value


def _insert(connection, *, content, kind='event', domain='dev', project='fixture',
            tags='["fixture"]', machine_tag='test-node', valid_from='2026-09-08'):
    cursor = connection.execute(
        '''INSERT INTO memories
           (id,content,kind,domain,project,tags_json,machine_tag,valid_from,source,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (content, content, kind, domain, project, tags, machine_tag, valid_from,
         'synthetic', '2026-09-08T00:00:00Z', '2026-09-08T00:00:00Z'),
    )
    connection.commit()
    return cursor.lastrowid


def test_lexical_search_uses_fts_filters_limit_and_has_no_vector_score(tmp_db):
    connection, _ = tmp_db
    _insert(connection, content='needle matching first', kind='decision')
    _insert(connection, content='needle matching second', kind='decision')
    _insert(connection, content='needle excluded kind', kind='event')

    results = memory_search(
        connection, query='needle', mode='lexical', kind='decision', domain='dev',
        project='fixture', tags=['fixture'], since='2026-09-01', until='2026-09-30',
        machine_tag='test-node', limit=1,
    )

    assert len(results) == 1
    assert results[0]['kind'] == 'decision'
    assert 'distance' not in results[0]
    assert 'score' not in results[0]


def test_service_falls_back_with_typed_metadata_even_when_no_matches(service):
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        result = service.search('absent-token', with_metadata=True)

    assert result == {
        'items': [],
        'search': {
            'mode': 'lexical',
            'degraded': True,
            'reason': 'embedding_unavailable',
        },
    }


def test_service_preserves_list_contract_during_embedding_fallback(service):
    _insert(service.archive, content='fallback token survives')
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        results = service.search('fallback token')

    assert [result['content'] for result in results] == ['fallback token survives']


def test_service_does_not_treat_fts_database_failure_as_embedding_degradation(service):
    service.archive.execute('DROP TABLE memories_fts')
    service.archive.commit()
    with patch('kelmo.store.memory.search.embed', side_effect=ArchiveEmbeddingError('missing model')):
        with pytest.raises(sqlite3.OperationalError):
            service.search('synthetic', with_metadata=True)
