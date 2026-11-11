"""The archive's broker transport over the embedding core."""
from concurrent.futures import Future

import pytest

from kelmo.core.embed_client import EmbedChannel
from kelmo.errors import KelmoError
from kelmo.store.errors import ArchiveEmbeddingError


class FakeLink:
    def __init__(self, *, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def call(self, operation, body, context, lane=None):
        self.calls.append((operation, body, context, lane))
        future = Future()
        if self.error is not None:
            future.set_exception(self.error)
        else:
            future.set_result(self.answer)
        return future


@pytest.mark.parametrize('kind,lane', [('health', 'query'), ('query', 'query'), ('passage', 'passage')])
def test_embed_channel_routes_each_purpose(kind, lane):
    answer = {'id': 'synthetic', 'vectors': []}
    link = FakeLink(answer=answer)
    assert EmbedChannel(link).call(kind, ['text'], 3.0) == answer
    operation, body, context, actual_lane = link.calls[0]
    assert operation == 'embed.embed'
    assert body == {'purpose': kind, 'texts': ['text'], 'timeout': 3.0}
    assert actual_lane == lane
    assert 3.0 < context.remaining() <= 8.0


@pytest.mark.parametrize('error,reason', [
    (KelmoError('E_EMBEDDING', 'timeout: expired'), 'timeout'),
    (KelmoError('E_EMBEDDING', 'embedding_unavailable: closed'), 'embedding_unavailable'),
    (KelmoError('E_EMBEDDING', 'protocol_error: invalid'), 'protocol_error'),
    (KelmoError('E_CORE_RESTARTED', 'closed'), 'embedding_unavailable'),
    (KelmoError('E_TRANSPORT', 'closed'), 'embedding_unavailable'),
    (KelmoError('E_UNAVAILABLE', 'closed'), 'embedding_unavailable'),
    (KelmoError('E_OVERLOADED', 'busy'), 'embedding_unavailable'),
    (KelmoError('E_DEADLINE', 'expired'), 'timeout'),
    (TimeoutError(), 'timeout'),
])
def test_embed_channel_converts_refusals(error, reason):
    with pytest.raises(ArchiveEmbeddingError) as refused:
        EmbedChannel(FakeLink(error=error)).call('query', ['text'], 1.0)
    assert refused.value.data['reason'] == reason


@pytest.mark.parametrize('changed,reason', [('space', 'space_mismatch'), ('policy', 'space_mismatch')])
def test_broker_verify_keeps_identity_and_policy_checks_over_embed_channel(tmp_path, changed, reason):
    from kelmo.embeddings import BrokeredProvider
    from test_embedding_contract import TOKENIZER, broker_space, health_answer

    tokenizer = tmp_path / 'tokenizer.json'
    tokenizer.write_bytes(TOKENIZER)
    space = broker_space()
    reported = broker_space(pooling='cls') if changed == 'space' else space
    policy = {'query_prefix': 'different: '} if changed == 'policy' else {}
    answer = health_answer({'id': 'synthetic'}, reported, input_changes=policy)
    provider = BrokeredProvider(None, None, space, tokenizer=tokenizer,
                                channel=EmbedChannel(FakeLink(answer=answer)))
    with pytest.raises(ArchiveEmbeddingError) as refused:
        provider.verify()
    assert refused.value.data['reason'] == reason
    assert provider.identity_verified is False


def test_broker_verifies_matching_space_over_embed_channel(tmp_path):
    from kelmo.embeddings import BrokeredProvider
    from test_embedding_contract import TOKENIZER, broker_space, health_answer

    tokenizer = tmp_path / 'tokenizer.json'
    tokenizer.write_bytes(TOKENIZER)
    space = broker_space()
    answer = health_answer({'id': 'synthetic'}, space)
    provider = BrokeredProvider(None, None, space, tokenizer=tokenizer,
                                channel=EmbedChannel(FakeLink(answer=answer)))
    assert provider.verify() == space
    assert provider.identity_verified is True
