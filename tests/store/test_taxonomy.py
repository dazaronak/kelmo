"""Tests for taxonomy validation."""
import pytest
from kelmo.store.memory.taxonomy import validate_kind, validate_domain, validate_kind_domain
from kelmo.store.errors import ArchiveValidationError


def test_valid_kinds():
    for kind in ["decision", "milestone", "preference", "rule", "pain", "event", "quirk"]:
        validate_kind(kind)


def test_invalid_kind():
    with pytest.raises(ArchiveValidationError) as exc_info:
        validate_kind("bogus")
    assert exc_info.value.data["field"] == "kind"
    assert exc_info.value.data["received"] == "bogus"
    assert "decision" in exc_info.value.data["valid_values"]


def test_valid_domains():
    for domain in ["dev", "life"]:
        validate_domain(domain)


def test_invalid_domain():
    with pytest.raises(ArchiveValidationError) as exc_info:
        validate_domain("work")
    assert exc_info.value.data["field"] == "domain"


def test_validate_kind_domain_both_valid():
    validate_kind_domain("decision", "dev")


def test_validate_kind_domain_invalid_kind():
    with pytest.raises(ArchiveValidationError):
        validate_kind_domain("bogus", "dev")


def test_validate_kind_domain_invalid_domain():
    with pytest.raises(ArchiveValidationError):
        validate_kind_domain("decision", "bogus")
