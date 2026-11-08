"""Install an explicitly supplied Linux sandbox executable with a pinned hash."""
import hashlib
import sys
from pathlib import Path

from ..errors import KelmoError
from .setup import write


def provision(prefix, source, expected_hash, *, apply=False):
    if source is None and expected_hash is None:
        return None
    if source is None or expected_hash is None:
        raise KelmoError("E_INPUT", "Supply --bwrap-source and --bwrap-sha256 together.")
    if sys.platform != "linux":
        raise KelmoError("E_INPUT", "A supplied bubblewrap executable is only used on Linux.")
    source = Path(source).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise KelmoError("E_INPUT", "Bubblewrap source must be a regular file.")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_hash:
        raise KelmoError("E_INPUT", "Bubblewrap source does not match the selected SHA-256.")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    destination = Path(prefix) / "lib" / version / "libexec/bwrap"
    result = {"path": str(destination), "sha256": digest, "installed": False}
    if apply:
        result.update(write(destination, data))
        destination.chmod(0o755)
        result["installed"] = True
    return result
