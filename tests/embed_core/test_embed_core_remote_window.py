"""RemoteWindow: a TokenWindow whose counts come from the embedding core."""
import hashlib
from contextlib import contextmanager

import pytest
from embed_core_support import ADDITIONS, PREFIX, bpe_bytes, wordlevel_bytes

from kelmo.store.errors import ArchiveEmbeddingError
from kelmo.store.memory.embed import (
    RemoteWindow,
    TokenWindow,
    install_remote_counter,
    preparation_digest,
)

BUDGET = 64
TEXT = 'Рассада томатов [SPECIAL] переехала в теплицу.'


class FakeCounter:
    """Serves one tokenizer identity and preparation.

    It counts a text as its length with the prefix, or raises the error it was given.
    """

    def __init__(self, identity, preparation, fail=None):
        self.identity = identity
        self.preparation = preparation
        self.fail = fail
        self.calls = []

    def serves(self, identity, preparation):
        return (identity, preparation) == (self.identity, self.preparation)

    def count(self, texts, prefix):
        self.calls.append((list(texts), prefix))
        if self.fail is not None:
            raise self.fail
        return [len(prefix + text) for text in texts]


@contextmanager
def installed(counter):
    install_remote_counter(counter)
    try:
        yield counter
    finally:
        install_remote_counter(None)


def identity_of(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def make_window(data, special_tokens=ADDITIONS):
    return TokenWindow.from_bytes(data, budget=BUDGET, prefix=PREFIX, special_tokens=special_tokens)


def test_a_served_tokenizer_gets_a_remote_window_with_the_local_properties():
    data = bpe_bytes()
    local = make_window(data)
    with installed(FakeCounter(local.identity, local.preparation)):
        remote = make_window(data)
    assert isinstance(remote, RemoteWindow)
    assert (remote.budget, remote.prefix, remote.identity, remote.preparation) == (
        local.budget, local.prefix, local.identity, local.preparation)


def test_another_tokenizer_or_other_additions_stay_local_windows():
    data = bpe_bytes()
    with installed(FakeCounter(identity_of(data), preparation_digest(ADDITIONS))):
        other_tokenizer = TokenWindow.from_bytes(
            wordlevel_bytes(), budget=BUDGET, prefix=PREFIX, special_tokens=ADDITIONS)
        other_additions = TokenWindow.from_bytes(
            data, budget=BUDGET, prefix=PREFIX, special_tokens=[])
    assert type(other_tokenizer) is TokenWindow
    assert type(other_additions) is TokenWindow


def test_repeated_counts_of_one_text_ask_the_counter_once():
    data = bpe_bytes()
    counter = FakeCounter(identity_of(data), preparation_digest(ADDITIONS))
    with installed(counter):
        remote = make_window(data)
        assert remote.count(TEXT) == len(PREFIX + TEXT)
        assert remote.count(TEXT) == len(PREFIX + TEXT)
        assert remote.fits(TEXT) == (len(PREFIX + TEXT) <= BUDGET)
    assert counter.calls == [([TEXT], PREFIX)]


def test_a_counter_that_raises_is_embedding_unavailable():
    data = bpe_bytes()
    failing = FakeCounter(identity_of(data), preparation_digest(ADDITIONS),
                          fail=RuntimeError('synthetic channel closed'))
    with installed(failing):
        remote = make_window(data)
        with pytest.raises(ArchiveEmbeddingError) as refused:
            remote.count(TEXT)
    assert refused.value.data['reason'] == 'embedding_unavailable'
