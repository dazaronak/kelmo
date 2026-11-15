"""Checks that hold for every test."""
import pytest

from kelmo.wiki import engine

# A shared registry view (kelmo.wiki.engine.registry_view) is read by many callers and must never change; under
# the suite every view gets its fingerprint when parsed, so a test whose code changes one in place fails.
engine.CHECK_VIEWS = True


@pytest.fixture(autouse=True)
def _no_requested_stop_leaks():
    # A core's stop is requested process-wide (kelmo.stopping); one test's stop must not end the next test's work.
    from kelmo import stopping
    stopping.reset()
    yield
    stopping.reset()


@pytest.fixture(autouse=True)
def _no_executor_answer_leaks():
    # What an executor command said of its instructions is believed for minutes, process-wide
    # (kelmo.creation.instructions_standing); one test's command must not answer for the next test's.
    from kelmo import creation
    creation._STANDINGS.clear()
    creation._UNASKED.clear()
    yield


@pytest.fixture(autouse=True)
def _registry_views_stay_unchanged():
    yield
    changed = engine.changed_views()
    engine._VIEWS.clear()
    assert not changed, 'a shared registry view was changed in place'
