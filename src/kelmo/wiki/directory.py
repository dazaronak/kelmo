"""Dependency-free checks on a selected wiki directory, shared by initialization and the setup preview."""
from pathlib import Path


def has_content(directory):
    """Whether a selected wiki directory holds content a new vault must not take over.

    Only a file or symlink outside `.git` counts: the empty skeleton an interrupted initialization left
    behind (directories and an empty Git history) is taken over by the retry.
    """
    directory = Path(directory)
    return directory.is_dir() and any(
        (path.is_file() or path.is_symlink()) for path in directory.rglob('*')
        if path.relative_to(directory).parts[0] != '.git')
