"""Configuration: env vars, paths, constants."""
import os


def get_machine_tag() -> str:
    """Return machine tag from env or hostname-slug fallback."""
    tag = os.environ.get("KELMO_MACHINE_TAG", "")
    if tag:
        return tag
    # Fallback: slugified hostname
    import platform
    hostname = platform.node() or "local"
    slug = hostname.lower().replace(" ", "-").replace("_", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-").strip("-")
    return slug or "local"


def get_embed_fds(env_name: str = "KELMO_EMBED_FDS") -> tuple[int, int] | None:
    """A launcher-provided embedding channel as (request fd, answer fd), or None when the variable is unset.

    The variable is named by configuration; nothing here knows a host, a model or a service."""
    value = os.environ.get(env_name, "")
    if not value:
        return None
    parts = value.split(",")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise ValueError(f"{env_name} must be '<request fd>,<answer fd>'")
    return int(parts[0]), int(parts[1])


SCHEMA_VERSION = 1
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_LIBRARY = "fastembed"

# Chunk budget in characters. Prose fits a 512-token window at these sizes;
# denser text does not, so every chunk is also checked against the configured tokenizer.
CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 1300
CHUNK_OVERLAP_CHARS = 150
# A record contributes at most this many chunks to one ranking and to the fused result.
MAX_CHUNKS_PER_MEMORY = 2
# Candidates per ranking before reciprocal-rank fusion, and the fusion constant.
HYBRID_CANDIDATES = 200
RRF_K = 60
MAX_QUERY_VARIANTS = 4
# A record-grouping caller asks the chunk search for at most this many records: its public limit of 100
# plus a bounded pool for records that turn out hidden or changed when re-read.
MAX_RECORD_POOL = 120

VALID_KINDS = frozenset(["decision", "milestone", "preference", "rule", "pain", "event", "quirk"])
VALID_DOMAINS = frozenset(["dev", "life"])
