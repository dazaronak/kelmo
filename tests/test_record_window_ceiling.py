"""A standing measurement: one vector per record plus the model's window means a record's meaning is its
first window of text. A fact written past that window does not reach the vector the search ranks the record with,
so the same fact written first is found by its own question and written last is not. This is the main risk of one
vector per record, and it is measured
here against the real model so a change to the model, the window or a future chunking/re-embed shows up as a red
instead of a rotted note.

The case needs the pinned embedding model, so it skips naming that prerequisite on a host that names none."""
import os
from pathlib import Path

import numpy as np
import pytest

from kelmo.store.memory.embed import embed
from kelmo.model_store import FILES


def model_directory():
    """The pinned model files this host offers, or None when it names none."""
    value = os.environ.get('KELMO_MODEL_DIR') or os.environ.get('KELMO_GATE_MODEL_DIR')
    if not value:
        return None
    root = Path(value).expanduser()
    return root if all((root / name).is_file() for name in FILES) else None


@pytest.fixture
def cache(tmp_path):
    """A cache directory holding the pinned model, so embed() ranks with the real model and not a stand-in."""
    directory = model_directory()
    if directory is None:
        pytest.skip('KELMO_MODEL_DIR (or KELMO_GATE_MODEL_DIR) must name the directory holding the five '
                    'pinned model files; this host names none, so the record-window ceiling cannot be measured here')
    cache = tmp_path / 'embeddings'
    cache.mkdir(parents=True)
    (cache / 'model').symlink_to(directory)
    return str(cache)


def vector(cache, text):
    return np.frombuffer(embed(text, cache_dir=cache), dtype=np.float32)


def cosine(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


# A head long enough to fill the window on its own, so anything written after it sits beyond the vector.
HEAD = ("The lighthouse keeper logs the tide every hour, and the harbour master copies the readings into the "
        "ledger by the window, and the ferry timetable is pinned beside the chandlery door because sailors read it "
        "there rather than online, and the weather board by the slipway shows the wind from the morning report "
        "that the coastguard reads out at six. ")
BODY = HEAD * 8
TAIL = ("The night ferry used to leave from the north pier, a berth that floods at every spring tide; "
        "it now leaves from the south pier, so the crossing runs in winter as well as in summer.")
TAIL_QUERY = "why does the night ferry leave from the south pier in winter as well as in summer"


def test_a_fact_written_past_the_window_is_not_in_the_record_vector(cache):
    """Two records that share the window and differ only past it embed to the same vector: the tail is invisible."""
    head_vector = vector(cache, BODY)
    with_tail_a = vector(cache, BODY + " " + TAIL)
    with_tail_b = vector(cache, BODY + " " + "A completely different closing claim about nothing at all here.")
    assert cosine(head_vector, with_tail_a) == pytest.approx(1.0, abs=1e-6)
    assert cosine(with_tail_a, with_tail_b) == pytest.approx(1.0, abs=1e-6)


def test_a_difference_inside_the_window_is_seen(cache):
    """The same model does see a difference placed inside the window, so the ceiling is the window and not the model."""
    early_a = vector(cache, "ZEBRACORN fox " + BODY)
    early_b = vector(cache, "PLATYPUS dog " + BODY)
    assert cosine(early_a, early_b) < 0.9


def test_the_same_fact_written_first_is_closer_to_its_own_question_than_written_last(cache):
    """Search ranks a record by the distance between its vector and the query's. A fact the vector never carried
    cannot answer the question that asks for it; written first it does."""
    claim_first = vector(cache, BODY + " " + TAIL)
    condition_first = vector(cache, TAIL + " " + BODY)
    query = vector(cache, TAIL_QUERY)
    claim_score = cosine(claim_first, query)
    condition_score = cosine(condition_first, query)
    assert condition_score > 0.6
    assert claim_score < 0.5
    assert condition_score - claim_score > 0.2
