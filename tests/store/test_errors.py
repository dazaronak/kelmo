"""Tests for error hierarchy."""
from kelmo.store.errors import (
    ArchiveError,
    ArchiveValidationError,
    ArchiveNotFoundError,
    ArchiveStorageError,
    ArchiveEmbeddingError,
    ArchiveMigrationError,
)


def test_base_error_has_code_and_data():
    err = ArchiveError("test", foo="bar")
    assert str(err) == "test"
    assert err.code == -32000
    assert err.data == {"foo": "bar"}


def test_validation_error_code():
    err = ArchiveValidationError(
        "Unknown kind: 'foo'",
        field="kind",
        received="foo",
        valid_values=["decision", "milestone"],
    )
    assert err.code == -32001
    assert err.data["field"] == "kind"
    assert err.data["received"] == "foo"


def test_not_found_error_code():
    err = ArchiveNotFoundError("not found", id="abc", entity_type="memory")
    assert err.code == -32002


def test_storage_error_code():
    err = ArchiveStorageError("db locked", operation="store")
    assert err.code == -32003


def test_embedding_error_code():
    err = ArchiveEmbeddingError("model crash", model="test", input_length=42)
    assert err.code == -32004


def test_migration_error_code():
    err = ArchiveMigrationError("version mismatch", expected_version=1, found_version=0)
    assert err.code == -32005


def test_all_errors_are_archive_error():
    for cls in [ArchiveValidationError, ArchiveNotFoundError, ArchiveStorageError,
                ArchiveEmbeddingError, ArchiveMigrationError]:
        assert issubclass(cls, ArchiveError)
