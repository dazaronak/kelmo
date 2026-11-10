"""The embedding core's counts, description and trim, against the Python TokenWindow it replaces."""
import hashlib

import pytest
from embed_core_support import (
    ADDITIONS,
    NEEDS_KELMOD,
    PREFIX,
    EmbedCore,
    bpe_bytes,
    synthetic_texts,
    wordlevel_bytes,
)

from kelmo.store.memory.embed import TokenWindow, preparation_digest

BUDGET = 8192
TOKENIZERS = {'wordlevel': wordlevel_bytes, 'bpe': bpe_bytes}


def write_tokenizer(tmp_path, data):
    path = tmp_path / 'tokenizer.json'
    path.write_bytes(data)
    return path


@NEEDS_KELMOD
@pytest.mark.parametrize('kind', sorted(TOKENIZERS))
def test_every_count_equals_the_python_token_window(tmp_path, kind):
    data = TOKENIZERS[kind]()
    path = write_tokenizer(tmp_path, data)
    window = TokenWindow.from_bytes(data, budget=BUDGET, prefix=PREFIX, special_tokens=ADDITIONS)
    texts = synthetic_texts()
    expected = [window.count(text) for text in texts]
    preparation = preparation_digest(ADDITIONS)
    with EmbedCore(path, tmp_path / 'embed.log', ADDITIONS, preparation) as core:
        counts, answers = core.count(texts, PREFIX)
    assert counts == expected
    assert answers == {(window.identity, window.preparation)}


@NEEDS_KELMOD
def test_describe_names_the_file_bytes_and_the_preparation_given(tmp_path):
    data = wordlevel_bytes()
    preparation = preparation_digest(ADDITIONS)
    path = write_tokenizer(tmp_path, data)
    with EmbedCore(path, tmp_path / 'embed.log', ADDITIONS, preparation) as core:
        header, body = core.call('embed.describe', {})
    assert header['ok'] is True, header
    assert body == {'tokenizer_identity': 'sha256:' + hashlib.sha256(data).hexdigest(),
                    'preparation': preparation}


@NEEDS_KELMOD
def test_trim_drops_the_tokenizer_and_the_next_count_answers_the_same(tmp_path):
    data = bpe_bytes()
    path = write_tokenizer(tmp_path, data)
    texts = synthetic_texts()[:200]
    preparation = preparation_digest(ADDITIONS)
    with EmbedCore(path, tmp_path / 'embed.log', ADDITIONS, preparation) as core:
        assert core.status()['tokenizer_loaded'] is False
        first = core.count(texts, PREFIX)
        assert core.status()['tokenizer_loaded'] is True
        assert core.trim() is True
        assert core.status()['tokenizer_loaded'] is False
        assert core.count(texts, PREFIX) == first
