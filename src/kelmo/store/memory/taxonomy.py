"""Taxonomy validation: kind and domain enums."""
from kelmo.store.config import VALID_KINDS, VALID_DOMAINS
from kelmo.store.errors import ArchiveValidationError


def validate_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise ArchiveValidationError(
            f"Unknown kind: '{kind}'",
            field="kind", received=kind, valid_values=sorted(VALID_KINDS),
        )


def validate_domain(domain: str) -> None:
    if domain not in VALID_DOMAINS:
        raise ArchiveValidationError(
            f"Unknown domain: '{domain}'",
            field="domain", received=domain, valid_values=sorted(VALID_DOMAINS),
        )


def validate_kind_domain(kind: str, domain: str) -> None:
    validate_kind(kind)
    validate_domain(domain)
