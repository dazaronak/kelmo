"""Embedding: the pinned fastembed record model, token windows and the vector checks the chunk index relies on.

`embed(text, cache_dir=...)` is the existing record-level entry point and keeps its behaviour. The chunk
index takes vectors only from an EmbeddingProvider (kelmo.embeddings) and stores them here, after
checking that they belong to the exact embedding space they claim, with the count, purpose, dimension,
representation and finiteness the space requires.
"""
import hashlib
import json
import re
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from kelmo.store.config import EMBEDDING_DIM, EMBEDDING_MODEL
from kelmo.store.errors import ArchiveEmbeddingError

_models = {}
_model_lock = threading.Lock()
MODEL_NAME = EMBEDDING_MODEL
_load_ms = None


def model_load_ms():
    """Milliseconds this process spent loading the embedding model, or None while it has loaded none. A warm
    process keeps the first load's number, so a caller reports it once."""
    return _load_ms


def get_model(cache_dir=None):
    """Lazy-init fastembed TextEmbedding (thread-safe)."""
    global _load_ms
    cache = str(Path(cache_dir or Path.home() / '.cache/kelmo/embeddings').expanduser().resolve())
    if cache not in _models:
        with _model_lock:
            if cache not in _models:
                from fastembed import TextEmbedding
                provisioned = Path(cache) / 'model'
                options = {'specific_model_path': str(provisioned)} if provisioned.is_dir() else {}
                began = time.monotonic()
                _models[cache] = TextEmbedding(MODEL_NAME, cache_dir=cache, local_files_only=True,
                                              threads=2, **options)
                if _load_ms is None:
                    _load_ms = round((time.monotonic() - began) * 1000, 1)
    return _models[cache]


def embed(text: str, *, cache_dir=None) -> bytes:
    """Embed text into float32 bytes. Raises ArchiveEmbeddingError on failure."""
    if not text or not text.strip():
        raise ArchiveEmbeddingError("Empty text", input_length=0, model=MODEL_NAME)
    try:
        model = get_model(cache_dir)
        vec = next(iter(model.embed([text])))
        result = np.asarray(vec, dtype=np.float32).tobytes()
        expected_len = EMBEDDING_DIM * 4
        if len(result) != expected_len:
            raise ArchiveEmbeddingError(
                f"Expected {EMBEDDING_DIM} dims ({expected_len} bytes), got {len(result)} bytes",
                model=MODEL_NAME, input_length=len(text),
            )
        return result
    except ArchiveEmbeddingError:
        raise
    except Exception as e:
        raise ArchiveEmbeddingError(
            str(e), model=MODEL_NAME, input_length=len(text)
        ) from e


# --- identities ---

def identity_id(record) -> str:
    """sha256 of a record's canonical JSON: the stored id of an EmbeddingSpace or ChunkProfile.

    Every field takes part, so two spaces that share a model name and dimension but differ in any
    transform, tokenizer or representation never share stored vectors."""
    body = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def canonical_json(record) -> str:
    return json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ensure_space(conn, space, *, verified: bool, note: str | None = None) -> str:
    """Record an embedding space inside the caller's write transaction and return its id.

    A space its provider has proven is marked verified; a verified row is never downgraded, and a
    stored body that differs from this one is refused as corruption."""
    from kelmo.store.db import now_iso
    from kelmo.store.errors import ArchiveStorageError

    if not conn.in_transaction:
        raise ArchiveStorageError("Recording an embedding space requires a write transaction",
                              operation="ensure_space")
    space_id, body = identity_id(space), canonical_json(space)
    conn.execute(
        "INSERT OR IGNORE INTO embedding_spaces (space_id, space_json, verified, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (space_id, body, int(verified), note, now_iso()),
    )
    stored = conn.execute("SELECT space_json, verified FROM embedding_spaces WHERE space_id = ?",
                          (space_id,)).fetchone()
    if stored[0] != body:
        raise ArchiveStorageError("Stored embedding space differs from its identity", operation="ensure_space",
                              space_id=space_id)
    if verified and not stored[1]:
        conn.execute("UPDATE embedding_spaces SET verified = 1 WHERE space_id = ?", (space_id,))
    return space_id


def verified_space(provider):
    """The provider's embedding space once the provider has established it.

    A provider that reports identity_verified=False must prove its space through verify(); one that
    cannot is refused, never assumed to be the space it declares."""
    if not getattr(provider, "identity_verified", True):
        verify = getattr(provider, "verify", None)
        if verify is None:
            raise ArchiveEmbeddingError("The provider cannot establish its embedding space",
                                    reason="embedding_identity_unverified")
        verify()
    return provider.describe()


# --- vectors ---

def numeric_array(vector) -> np.ndarray:
    """A vector's values as float64, accepted only when they already are numbers: a numeric numpy array,
    or a list or tuple of ints and floats. Text, bool, None and other objects raise ValueError instead
    of being parsed or coerced into plausible numbers; nested sequences are not flattened."""
    if isinstance(vector, np.ndarray):
        if vector.dtype.kind not in "fiu":
            raise ValueError(f"vector values of dtype {vector.dtype} are not numbers")
        return vector.astype(np.float64)
    if not isinstance(vector, (list, tuple)):
        raise ValueError(f"a vector must be an array, list or tuple, not {type(vector).__name__}")
    for value in vector:
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
            raise ValueError(f"vector value {value!r} is not a number")
    return np.asarray(vector, dtype=np.float64)


def vector_blobs(batch, space, purpose: str, count: int) -> list[bytes]:
    """float32 little-endian blobs of a provider's batch, after every check the store relies on.

    Raises ArchiveEmbeddingError (reason names the defect) for vectors of another space or purpose, a
    wrong count or dimension, an unsupported representation or a value that is not finite."""
    def refuse(message, reason):
        return ArchiveEmbeddingError(message, reason=reason)

    batch_space = getattr(batch, "space", None)
    if batch_space is None or canonical_json(batch_space) != canonical_json(space):
        raise refuse("Vectors belong to a different embedding space", "space_mismatch")
    if getattr(batch, "purpose", None) != purpose:
        raise refuse(f"Expected {purpose} vectors, got {getattr(batch, 'purpose', None)}", "purpose_mismatch")
    if space.dtype != "float32" or space.byte_order != "little":
        raise refuse("The chunk index stores float32 little-endian vectors only", "unsupported_representation")
    vectors = getattr(batch, "vectors", None)
    if not isinstance(vectors, (tuple, list)) or len(vectors) != count:
        raise refuse(f"Expected {count} vectors", "malformed_vectors")
    blobs = []
    for vector in vectors:
        try:
            values = numeric_array(vector)
        except (TypeError, ValueError) as error:
            raise refuse("Vector values are not numbers", "malformed_vectors") from error
        if values.shape != (space.dimension,):
            raise refuse(f"Expected {space.dimension} dimensions, got {values.shape}", "malformed_vectors")
        with np.errstate(over="ignore", invalid="ignore"):
            stored = values.astype("<f4")
        if not np.isfinite(values).all() or not np.isfinite(stored).all():
            raise refuse("Vector values must be finite float32 numbers", "malformed_vectors")
        blobs.append(stored.tobytes())
    return blobs


# --- token windows ---

# The fields an AddedToken mapping may carry, as the embedding loader passes them to AddedToken(**token).
_ADDED_TOKEN_FIELDS = {"content": str, "single_word": bool, "lstrip": bool, "rstrip": bool,
                       "normalized": bool, "special": bool}


def special_token_additions(tokens) -> list:
    """The ordered special-token additions a loader makes after reading tokenizer.json, validated.

    Each is a nonempty string or an AddedToken mapping with a nonempty `content`. ValueError for anything
    else: a malformed or incomplete list is refused, never read as a model that adds nothing."""
    if isinstance(tokens, (str, bytes)) or not isinstance(tokens, (list, tuple)):
        raise ValueError("special-token additions must be a list")
    checked = []
    for token in tokens:
        if isinstance(token, dict):
            if (set(token) - set(_ADDED_TOKEN_FIELDS)
                    or any(type(value) is not _ADDED_TOKEN_FIELDS[key] for key, value in token.items())
                    or not token.get("content")):
                raise ValueError(f"malformed special token {token!r}")
            checked.append(dict(token))
        elif isinstance(token, str) and token:
            checked.append(token)
        else:
            raise ValueError(f"malformed special token {token!r}")
    return checked


def preparation_digest(tokens) -> str:
    """Identity of the token preparation on top of tokenizer.json: sha256 of the canonical JSON of the
    ordered special-token additions. The same raw tokenizer with other additions cuts text differently."""
    body = json.dumps(special_token_additions(tokens), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


# A window over the raw tokenizer.json alone: nothing is added after loading it.
NO_ADDITIONS = preparation_digest([])


class _SharedTokenizer:
    """One parsed tokenizer and the lock every encode on it takes. A parsed tokenizer.json of a real model
    costs hundreds of megabytes, so windows over the same bytes and additions hold one copy per process."""
    __slots__ = ("tokenizer", "lock", "__weakref__")

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.lock = threading.Lock()


# (identity, preparation) -> the parsed copy; an entry lives as long as some window holds it.
_SHARED_TOKENIZERS = weakref.WeakValueDictionary()
_SHARED_TOKENIZERS_LOCK = threading.Lock()


def _parse_tokenizer(data: bytes, additions):
    try:
        from tokenizers import AddedToken, Tokenizer
    except ImportError as error:
        raise ArchiveEmbeddingError("could not import 'tokenizers'", reason="tokenizer_missing") from error
    try:
        tokenizer = Tokenizer.from_str(data.decode("utf-8"))
        for token in additions:
            tokenizer.add_special_tokens([AddedToken(**token) if isinstance(token, dict) else token])
    except Exception as error:
        raise ArchiveEmbeddingError("unreadable tokenizer.json", reason="tokenizer_unusable") from error
    return tokenizer


class TokenWindow:
    """Counts the tokens a model sees for one stored text: prefix, text and special tokens, never truncated.

    A tokenizer.json may ship with truncation or padding enabled; both are switched off and checked,
    and an encoding that still reports overflow is refused, so a count is never capped at the window.
    `identity` names the raw tokenizer.json bytes and `preparation` the special tokens added on top of it;
    together they name the tokenizer a count was made with. Windows built from the same bytes and additions
    share one parsed tokenizer and its lock."""

    def __init__(self, tokenizer, *, budget: int, prefix: str = "", identity: str, preparation: str = NO_ADDITIONS):
        if type(budget) is not int or budget < 1:
            raise ValueError("Token budget must be a positive integer")
        shared = tokenizer if isinstance(tokenizer, _SharedTokenizer) else _SharedTokenizer(tokenizer)
        with shared.lock:
            shared.tokenizer.no_truncation()
            shared.tokenizer.no_padding()
            if shared.tokenizer.truncation is not None or shared.tokenizer.padding is not None:
                raise ArchiveEmbeddingError("The tokenizer still truncates or pads", reason="tokenizer_unusable")
        self._shared = shared
        self._tokenizer = shared.tokenizer
        self._lock = shared.lock
        self.budget = budget
        self.prefix = prefix
        self.identity = identity
        self.preparation = preparation

    @classmethod
    def from_file(cls, path, *, budget: int, prefix: str = "", special_tokens=()):
        """A window over a local tokenizer.json; its identity is the sha256 of the exact bytes parsed."""
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ArchiveEmbeddingError(f"no tokenizer.json at {path}", reason="tokenizer_missing") from error
        return cls.from_bytes(data, budget=budget, prefix=prefix, special_tokens=special_tokens)

    @classmethod
    def from_bytes(cls, data: bytes, *, budget: int, prefix: str = "", special_tokens=()):
        """A window over tokenizer.json bytes; `special_tokens` are added in order the way the embedding loader
        adds them (a string or an AddedToken mapping each), so a literal special token counts as the model
        sees it, and their digest becomes the window's preparation."""
        try:
            additions = special_token_additions(special_tokens)
        except ValueError as error:
            raise ArchiveEmbeddingError(f"malformed special-token additions: {error}",
                                    reason="input_policy_unproven") from error
        identity = "sha256:" + hashlib.sha256(data).hexdigest()
        preparation = preparation_digest(additions)
        # Held across the parse, so two windows asked for at once still parse the file once.
        with _SHARED_TOKENIZERS_LOCK:
            shared = _SHARED_TOKENIZERS.get((identity, preparation))
            if shared is None:
                shared = _SharedTokenizer(_parse_tokenizer(data, additions))
                _SHARED_TOKENIZERS[(identity, preparation)] = shared
        return cls(shared, budget=budget, prefix=prefix, identity=identity, preparation=preparation)

    def count(self, text: str) -> int:
        with self._lock:
            encoding = self._tokenizer.encode(self.prefix + text, add_special_tokens=True)
        if encoding.overflowing:
            raise ArchiveEmbeddingError("The tokenizer truncated a count", reason="tokenizer_unusable")
        return len(encoding.ids)

    def fits(self, text: str) -> bool:
        return self.count(text) <= self.budget


# --- input policies ---

@dataclass(frozen=True)
class InputPolicy:
    """What a provider actually feeds its model: the tokenizer (identity of tokenizer.json, as in the
    EmbeddingSpace), the special tokens its loader adds on top of it (`preparation`, a
    preparation_digest), the longest input it accepts without truncating, and the prefixes it prepends.
    A provider capability, kept outside the serialized EmbeddingSpace and ChunkProfile records."""
    tokenizer_identity: str
    max_tokens: int
    query_prefix: str
    document_prefix: str
    preparation: str

    def __post_init__(self):
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if not isinstance(self.query_prefix, str) or not isinstance(self.document_prefix, str):
            raise ValueError("prefixes must be text")
        if not isinstance(self.preparation, str) or not re.fullmatch("sha256:[0-9a-f]{64}", self.preparation):
            raise ValueError("preparation must be a preparation digest")


# Reasons that name one input the provider refuses, as opposed to a provider that cannot embed at all.
INPUT_REASONS = frozenset({"malformed_vectors", "input_too_long", "empty_input"})


def input_policy(provider) -> InputPolicy:
    """The provider's verified input policy; a provider that cannot state one is refused."""
    policy_of = getattr(provider, "input_policy", None)
    if policy_of is None:
        raise ArchiveEmbeddingError("The provider states no input policy; its vectors cannot be bound to chunks",
                                reason="input_policy_unproven")
    return policy_of()


def input_binding(prefix: str, policy: InputPolicy) -> str:
    """The canonical name of one input path: the prefix prepended, the special-token additions on top of
    the space's tokenizer, the tokenizer's own special tokens counted (add_special_tokens), and the
    longest input taken without truncating."""
    body = {"added": policy.preparation, "max_tokens": policy.max_tokens, "prefix": prefix,
            "special_tokens": "tokenizer+added"}
    return "input=" + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bound_transform(base: str, prefix: str, policy: InputPolicy) -> str:
    """An EmbeddingSpace transform that names its whole input path, so the space identity changes with it."""
    return f"{base};{input_binding(prefix, policy)}"


def transforms_bind(space, policy: InputPolicy) -> bool:
    """Whether the space's query and document transforms name exactly this policy's input paths."""
    return (isinstance(space.query_transform, str) and isinstance(space.document_transform, str)
            and space.query_transform.endswith(";" + input_binding(policy.query_prefix, policy))
            and space.document_transform.endswith(";" + input_binding(policy.document_prefix, policy)))


def bound_policy(provider):
    """(space, policy) of a provider whose verified space names the input path it actually uses.

    A provider whose space does not is refused (input_policy_unproven): its vectors could not be told
    apart from those of another prefix, other additions or another limit under the same space."""
    space = verified_space(provider)
    policy = input_policy(provider)
    if not transforms_bind(space, policy):
        raise ArchiveEmbeddingError("The provider's embedding space does not name the input path it uses",
                                reason="input_policy_unproven")
    return space, policy


def check_window(provider, window):
    """(space, policy) once the counting window is proven to be the provider's own input path.

    Raises ArchiveEmbeddingError while the provider cannot establish its space or policy (lexical work may go
    on), and ArchiveValidationError when it can and the window disagrees: another tokenizer, other special-token
    additions, another document prefix, or a budget above the longest input the model takes without
    truncating."""
    from kelmo.store.errors import ArchiveValidationError

    space, policy = bound_policy(provider)
    if window.identity != space.tokenizer_identity or policy.tokenizer_identity != space.tokenizer_identity:
        raise ArchiveValidationError("The counting tokenizer is not the embedding model's tokenizer",
                                 field="tokenizer", window=window.identity, space=space.tokenizer_identity)
    if getattr(window, "preparation", None) != policy.preparation:
        raise ArchiveValidationError("Chunks would be counted without the special tokens the model's loader adds",
                                 field="special_tokens", window=getattr(window, "preparation", None),
                                 provider=policy.preparation)
    if window.prefix != policy.document_prefix:
        raise ArchiveValidationError("Chunks would be counted with a prefix the provider does not embed",
                                 field="document_prefix")
    if window.budget > policy.max_tokens:
        raise ArchiveValidationError(f"Token budget {window.budget} exceeds the model's input limit "
                                 f"{policy.max_tokens}", field="token_budget")
    return space, policy
