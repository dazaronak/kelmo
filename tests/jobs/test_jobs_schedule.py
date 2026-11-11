"""Dreaming's schedule: one run a day from DREAM_HOUR, with the day kept so a restart does not repeat it,
and an early apply once enough notes wait."""
from datetime import datetime

from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core.jobs import DREAM_HOUR, DREAM_WAITING_NOTES, Jobs


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute).timestamp()


def test_dreaming_runs_once_a_day_from_its_hour(world_factory, core_factory, monkeypatch):
    root = world_factory().root
    core = core_factory(root)
    now = [at(10, DREAM_HOUR - 1, 59)]
    jobs = Jobs(core, clock=lambda: now[0])
    monkeypatch.setattr(jobs, '_dream_hold', lambda: (None, None))
    assert jobs._dream_due() is None
    now[0] = at(10, DREAM_HOUR)
    assert jobs._dream_due() == 'run'
    jobs._record_dream_day()
    now[0] = at(10, 23)
    # A fresh scheduler reads the recorded day from the vault, as it does after a restart.
    restarted = Jobs(core, clock=lambda: now[0])
    monkeypatch.setattr(restarted, '_dream_hold', lambda: (None, None))
    assert restarted._dream_due() is None
    now[0] = at(11, DREAM_HOUR)
    assert restarted._dream_due() == 'run'


def test_dreaming_applies_at_once_when_enough_notes_wait(world_factory, core_factory, monkeypatch):
    core = core_factory(world_factory().root)
    jobs = Jobs(core, clock=lambda: at(10, 1))
    monkeypatch.setattr(jobs, '_dream_hold', lambda: (None, None))
    monkeypatch.setattr(jobs, '_waiting_notes', lambda: DREAM_WAITING_NOTES - 1)
    assert jobs._dream_due() is None
    monkeypatch.setattr(jobs, '_waiting_notes', lambda: DREAM_WAITING_NOTES)
    assert jobs._dream_due() == 'apply'
