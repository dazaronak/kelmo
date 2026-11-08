"""Tests for config module."""
from kelmo.store.config import get_machine_tag


def test_machine_tag_from_env(monkeypatch):
    """KELMO_MACHINE_TAG env var is returned."""
    monkeypatch.setenv("KELMO_MACHINE_TAG", "laptop")
    assert get_machine_tag() == "laptop"


def test_machine_tag_default(monkeypatch):
    """Without env var, returns a non-empty string fallback."""
    monkeypatch.delenv("KELMO_MACHINE_TAG", raising=False)
    tag = get_machine_tag()
    assert isinstance(tag, str)
    assert len(tag) > 0
