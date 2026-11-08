"""The archive exception hierarchy."""


class ArchiveError(Exception):
    """Base exception. All archive exceptions subclass this."""
    code: int = -32000

    def __init__(self, message: str, **data):
        super().__init__(message)
        self.data = data


class ArchiveValidationError(ArchiveError):
    """Invalid input (unknown kind/domain, bad format)."""
    code = -32001


class ArchiveNotFoundError(ArchiveError):
    """Entity not found by id."""
    code = -32002


class ArchiveStorageError(ArchiveError):
    """Database operation failed (lock, corruption, IO)."""
    code = -32003


class ArchiveEmbeddingError(ArchiveError):
    """Embedding model failed to load or embed."""
    code = -32004


class ArchiveMigrationError(ArchiveError):
    """Migration failed (version mismatch, error threshold exceeded)."""
    code = -32005


class ArchiveConflictError(ArchiveError):
    """An update's expected version differs from the stored version."""
    code = -32006


class ArchiveLegacyRecordError(ArchiveError):
    """Stored rows hold values an older archive accepted that this operation cannot interpret.

    The rows are never changed or skipped; `data['records']` names them and
    their raw bytes stay readable through memory_get.
    """
    code = -32007
