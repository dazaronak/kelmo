"""CLI validation and bounds for the load harness's closed-loop pacing."""
import random

import pytest

from tools import loadtest


def test_run_defaults_to_unpaced(monkeypatch):
    seen = []
    monkeypatch.setattr(loadtest, 'run', seen.append)

    loadtest.main(['run', '--vault', '/unused'])

    assert seen[0].think_seconds == 0


@pytest.mark.parametrize('value', ['-0.1', 'nan', 'inf', '-inf'])
def test_run_rejects_invalid_think_seconds(monkeypatch, value):
    monkeypatch.setattr(loadtest, 'run', lambda _args: None)

    with pytest.raises(SystemExit) as error:
        loadtest.main(['run', '--vault', '/unused', '--think-seconds', value])

    assert error.value.code == 2


def test_initial_stagger_stays_within_think_window():
    stagger = loadtest.initial_stagger(random.Random(17), 0.25)

    assert 0 <= stagger < 0.25


@pytest.mark.parametrize(('think_seconds', 'remaining', 'expected'), [
    (0.0, 1.0, 0.0),
    (0.2, 1.0, 0.2),
    (0.2, 0.05, 0.05),
    (0.2, 0.0, 0.0),
])
def test_pacing_delay_is_clamped_to_remaining_deadline(think_seconds, remaining, expected):
    assert loadtest.pacing_delay(think_seconds, remaining) == expected
