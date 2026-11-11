"""A refused early chunk does not hide later chunks in a bounded walk."""
from types import SimpleNamespace

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.memory import backfill


def test_cursor_advances_past_individually_refused_chunks(monkeypatch):
    missing = [1, 2, 3]
    monkeypatch.setattr(backfill, 'require_idle', lambda *args: None)
    monkeypatch.setattr(backfill, 'bound_space', lambda *args: object())
    monkeypatch.setattr(backfill, 'identity_id', lambda *args: 'synthetic')
    monkeypatch.setattr(backfill, 'missing_chunks', lambda *args: list(missing))
    monkeypatch.setattr(backfill, 'chunk_states', lambda conn, ids: [{'chunk_id': i, 'text': str(i)} for i in ids])
    monkeypatch.setattr(backfill, 'pending_from', lambda row, vector: row['chunk_id'])

    def embed(provider, space, texts):
        if '1' in texts:
            raise ArchiveEmbeddingError('input too long', reason='input_too_long')
        return [b'vector'] * len(texts)

    def attach(conn, space, items, **kwargs):
        for item in items:
            missing.remove(item)
        return {'written': len(items)}

    monkeypatch.setattr(backfill, 'embed_documents', embed)
    monkeypatch.setattr(backfill, 'attach_vectors', attach)
    args = {'profile_id': 'synthetic', 'batch': 1, 'limit': 1, 'sleep': lambda _: None,
            'progress': lambda _: None}
    first = backfill.backfill(SimpleNamespace(), object(), **args)
    second = backfill.backfill(SimpleNamespace(), object(), after=first['cursor'], **args)
    third = backfill.backfill(SimpleNamespace(), object(), after=second['cursor'], **args)
    assert first['failed'] == [1]
    assert (first['cursor'], second['cursor'], third['cursor']) == ('1', '2', None)
    assert missing == [1]


def test_provider_failure_restarts_at_first_missing_chunk(monkeypatch):
    missing = [1, 2, 3]
    examined = []
    monkeypatch.setattr(backfill, 'require_idle', lambda *args: None)
    monkeypatch.setattr(backfill, 'bound_space', lambda *args: object())
    monkeypatch.setattr(backfill, 'identity_id', lambda *args: 'synthetic')
    monkeypatch.setattr(backfill, 'missing_chunks', lambda *args: list(missing))
    monkeypatch.setattr(backfill, 'chunk_states',
                        lambda conn, ids: [{'chunk_id': i, 'text': str(i)} for i in ids])
    monkeypatch.setattr(backfill, 'pending_from', lambda row, vector: row['chunk_id'])

    def fail(provider, space, texts):
        examined.append(texts)
        raise ArchiveEmbeddingError('provider offline', reason='embedding_unavailable')

    monkeypatch.setattr(backfill, 'embed_documents', fail)
    args = {'profile_id': 'synthetic', 'batch': 1, 'limit': 1, 'progress': lambda _: None}
    first = backfill.backfill(SimpleNamespace(), object(), **args)
    second = backfill.backfill(SimpleNamespace(), object(), **args)
    assert first['stopped'] and second['stopped']
    assert first['cursor'] is None and second['cursor'] is None
    assert examined == [['1'], ['1']]

    monkeypatch.setattr(backfill, 'embed_documents', lambda provider, space, texts: [b'vector'] * len(texts))
    monkeypatch.setattr(backfill, 'attach_vectors', lambda conn, space, items, **kwargs: {'written': len(items)})
    complete_step = backfill.backfill(SimpleNamespace(), object(), **args)
    assert complete_step['stopped'] is None
    assert complete_step['cursor'] == '1'
