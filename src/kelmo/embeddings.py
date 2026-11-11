"""EmbeddingProvider implementations: a pinned local fastembed model and the launcher's brokered channel.

Both are configured explicitly (no host, model or path default, no download, no fallback from one to
the other) and both describe one complete EmbeddingSpace. Query and document texts take separate
paths, because a model may embed them differently. Every failure is an ArchiveEmbeddingError whose `reason`
names it; a batch that is malformed, of another space or not finite never leaves a provider.

Each provider also states its InputPolicy: the tokenizer it feeds, the special tokens its loader adds to
it, the longest input its model takes without truncating and the prefixes it prepends. Chunks are
counted against exactly that path, and an input longer than the limit is refused rather than truncated.
The space's query_transform and document_transform end with that path (embed.bound_transform), so the
space identity changes with any part of it.

The local space and policy come from the hash-verified pinned files of the provisioned model, read the
way fastembed reads them. The broker's space and policy are a declaration until the broker's own health
answer (ok true) reports the same whole space and an input policy that the declared transforms name,
and every vector answer must name that space; a broker that cannot is unsupported, never assumed
verified.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import sys
import threading
import time
import uuid
import weakref
from pathlib import Path

import numpy as np

from .contracts import CONTRACT_VERSION, EmbeddingSpace, VectorBatch

# The files fastembed's loader reads besides the weights; the input policy is unprovable without them.
POLICY_FILES = ('tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json')


def _error(message, reason, **data):
    from kelmo.store.errors import ArchiveEmbeddingError
    return ArchiveEmbeddingError(message, reason=reason, **data)


def _check_texts(texts):
    if isinstance(texts, str) or not isinstance(texts, (list, tuple)) or not texts:
        raise _error('Embedding needs a nonempty list of texts', 'empty_input')
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise _error('Empty text', 'empty_input', input_length=0)
    return list(texts)


def _batch(space, purpose, raw, count):
    """A validated VectorBatch from a model's raw output; anything malformed becomes ArchiveEmbeddingError.
    Each vector must already have the shape (dimension,): a matrix is refused, never flattened, and a
    text, bool or object value is refused, never parsed into a number."""
    from kelmo.store.memory.embed import numeric_array
    try:
        arrays = [numeric_array(vector) for vector in raw]
        for array in arrays:
            if array.shape != (space.dimension,):
                raise ValueError(f'a vector of shape {array.shape}, expected ({space.dimension},)')
        vectors = tuple(tuple(float(value) for value in array) for array in arrays)
        batch = VectorBatch(space, purpose, vectors)
        batch.validate_count(count)
    except (TypeError, ValueError) as error:
        raise _error(f'Malformed vectors: {error}', 'malformed_vectors') from error
    return batch


def _refuse_long(window, texts):
    """Refuse, never truncate, an input the model would cut: counted with prefix and special tokens."""
    for position, text in enumerate(texts):
        tokens = window.count(text)
        if tokens > window.budget:
            raise _error(f'Input {position} has {tokens} tokens; the model takes {window.budget}',
                         'input_too_long', tokens=tokens, limit=window.budget)


# The largest max_length tokenizers' enable_truncation accepts (a usize).
_USIZE_MAX = sys.maxsize * 2 + 1


def model_input_limit(tokenizer_config):
    """The limit fastembed truncates at, read as its loader reads it.

    fastembed 0.8.0 load_tokenizer takes max_length or model_max_length, or the minimum of the two when
    both are present, and passes that value straight to enable_truncation: there is no other cap on that
    path, so none is added here. A value that path cannot use is refused rather than replaced: a missing,
    non-integer or non-positive one, and one above what enable_truncation accepts (such as the
    model_max_length = 1e30 "no limit" sentinel), on which the loader itself fails."""
    limits = [tokenizer_config[key] for key in ('model_max_length', 'max_length') if key in tokenizer_config]
    if not limits or any(type(limit) is not int or limit < 1 for limit in limits):
        raise ValueError('tokenizer_config.json states no usable model_max_length or max_length')
    if min(limits) > _USIZE_MAX:
        raise ValueError(f'the input limit {min(limits)} is a "no limit" sentinel the loader cannot apply')
    return min(limits)


class LocalFastEmbedProvider:
    """A provisioned fastembed model read from model_dir with local_files_only; nothing is downloaded.

    `files` pins model files as {name: (size, sha256)}; describe() hashes them, so the space names the
    exact artifact and tokenizer. The pinned record model is verified against its own fixed table. A
    custom model must pin everything its loader may read: the configuration and tokenizer files, exactly
    one ONNX graph and every other regular file in model_dir (external data, sidecars); an unpinned file,
    a link or another file type anywhere under model_dir is refused. Its pooling, projection and
    normalization are the operator's declaration, not a measured property: they are checked only against
    artifact metadata that states them (config.json hidden_size, 1_Pooling/config.json), and a detectable
    disagreement is refused.

    Queries go through query_embed and documents through passage_embed, each after its configured
    prefix, which this provider prepends itself. Both transforms name the whole input path (prefix,
    special-token additions, input limit), so a change to any of them is another embedding space."""

    identity_verified = True

    def __init__(self, model_dir, *, model, files, pooling, projection, normalization, dimension,
                 query_prefix='', document_prefix=''):
        from .model_store import record_model_pin
        self.model_dir = Path(model_dir).expanduser()
        self.model_name = model
        self.files = {name: tuple(pin) for name, pin in dict(files).items()}
        self.spec = dict(pooling=pooling, projection=projection, normalization=normalization,
                         dimension=dimension)
        # The pinned bytes are checked against their own table and skip the custom-model spec checks, so a
        # false spec claim for them is refused here, as configuration refuses it.
        self.pinned_record_model = record_model_pin(model, self.files, self.spec)
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self._space = None
        self._policy = None
        self._special = None
        self._windows = None
        self._model = None
        self._lock = threading.Lock()

    def _verify(self):
        """The artifact identities once every file the loader may read is proven to be its pinned bytes."""
        from .model_store import ModelImportError, check_layout, verify_artifact, verify_complete_artifact
        if not self.model_dir.is_dir() or self.model_dir.is_symlink():
            raise _error(f'No provisioned model at {self.model_dir}', 'model_missing')
        try:
            if self.pinned_record_model:
                return verify_artifact(self.model_dir, self.files)
            check_layout(self.files)
            return verify_complete_artifact(self.model_dir, self.files)
        except (ModelImportError, OSError, ValueError) as error:
            raise _error(f'The provisioned model cannot be verified: {error}', 'model_missing') from error

    def describe(self) -> EmbeddingSpace:
        if self._space is None:
            from importlib.metadata import PackageNotFoundError, version

            from kelmo.store.memory.embed import bound_transform
            identity = self._verify()
            try:
                library = 'fastembed-' + version('fastembed')
            except PackageNotFoundError as error:
                raise _error('fastembed is not installed', 'model_missing') from error
            if not self.pinned_record_model:
                self._check_backend_layout()
                self._check_declared_spec()
            policy = self._read_policy(identity['tokenizer_identity'])
            self._space = EmbeddingSpace(
                CONTRACT_VERSION, f'{self.model_name}@{identity["artifact_identity"]}',
                identity['tokenizer_identity'],
                bound_transform(f'{library}:query_embed', policy.query_prefix, policy),
                bound_transform(f'{library}:passage_embed', policy.document_prefix, policy),
                self.spec['pooling'], self.spec['projection'], self.spec['normalization'],
                self.spec['dimension'], 'float32', 'little')
            self._policy = policy
        return self._space

    def _check_backend_layout(self):
        """Refuse a custom model the backend would not load as pinned. TextEmbedding loads only names its
        registry knows (an unregistered name raises inside fastembed, add_custom_model is not used here),
        reading the registry's model_file and additional_files from model_dir: each must be pinned, and the
        registry dimension must be the declared one."""
        description = backend_model_description(self.model_name)
        if description is None:
            raise _error(f'fastembed does not register {self.model_name!r}; its TextEmbedding loader cannot load '
                         'it from a local directory', 'model_unsupported')
        graph = description.get('model_file')
        extra = description.get('additional_files') or []
        if not isinstance(graph, str) or graph not in self.files:
            raise _error(f'The backend loads {graph!r} for {self.model_name!r}; that file is not the pinned graph',
                         'model_unsupported')
        unpinned = [name for name in extra if name not in self.files]
        if unpinned:
            raise _error('The backend also reads unpinned files: ' + ', '.join(map(str, unpinned)), 'model_unsupported')
        dimension = description.get('dim')
        if type(dimension) is int and dimension != self.spec['dimension']:
            raise _error(f'The backend registers dimension {dimension}, the configuration declares '
                         f'{self.spec["dimension"]}', 'space_mismatch')

    def _check_declared_spec(self):
        """Refuse a declared dimension or pooling that the pinned artifact's own metadata contradicts."""
        try:
            config = json.loads(self._pinned('config.json'))
            hidden = config.get('hidden_size') if isinstance(config, dict) else None
            pooling = (json.loads(self._pinned('1_Pooling/config.json'))
                       if '1_Pooling/config.json' in self.files else {})
        except ValueError as error:
            raise _error(f'The pinned model metadata is unreadable: {error}', 'model_missing') from error
        if self.spec['projection'] == 'none' and type(hidden) is int and hidden != self.spec['dimension']:
            raise _error(f'config.json states hidden_size {hidden}, the configuration declares dimension '
                         f'{self.spec["dimension"]}', 'space_mismatch')
        modes = {'pooling_mode_cls_token': 'cls', 'pooling_mode_mean_tokens': 'mean',
                 'pooling_mode_max_tokens': 'max'}
        stated = [name for key, name in modes.items() if isinstance(pooling, dict) and pooling.get(key) is True]
        if len(stated) == 1 and stated[0] != self.spec['pooling']:
            raise _error(f'1_Pooling/config.json states {stated[0]} pooling, the configuration declares '
                         f'{self.spec["pooling"]}', 'space_mismatch')

    def _pinned(self, name):
        """A pinned file's bytes, refused unless they are still the pinned bytes."""
        if name not in self.files:
            raise _error(f'{name} is not pinned; the model input policy cannot be proven', 'input_policy_unproven')
        try:
            data = (self.model_dir / name).read_bytes()
        except OSError as error:
            raise _error(f'{name} is missing from the provisioned model', 'model_missing') from error
        size, checksum = self.files[name]
        if len(data) != size or hashlib.sha256(data).hexdigest() != checksum:
            raise _error(f'{name} differs from its pinned bytes', 'model_missing')
        return data

    def _read_policy(self, tokenizer_identity):
        """Tokenizer, special-token additions, limit and prefixes exactly as fastembed's loader reads the
        pinned files: it adds every string or mapping value of special_tokens_map.json in file order and
        skips other values (lists such as additional_special_tokens)."""
        from kelmo.store.memory.embed import InputPolicy, preparation_digest, special_token_additions
        data = {name: self._pinned(name) for name in POLICY_FILES}
        try:
            config = json.loads(data['tokenizer_config.json'])
            special = json.loads(data['special_tokens_map.json'])
            if not isinstance(config, dict) or not isinstance(special, dict):
                raise ValueError('tokenizer_config.json and special_tokens_map.json must be mappings')
            limit = model_input_limit(config)
            additions = special_token_additions(
                [token for token in special.values() if isinstance(token, (str, dict))])
        except ValueError as error:
            raise _error(f'The pinned model states no input policy: {error}', 'input_policy_unproven') from error
        self._special = additions
        return InputPolicy(tokenizer_identity, limit, self.query_prefix, self.document_prefix,
                           preparation_digest(additions))

    def input_policy(self):
        """The input policy the space was described with (describe() reads it from the pinned files)."""
        self.describe()
        return self._policy

    def token_window(self, budget=None, *, purpose='document'):
        """A TokenWindow over the model's own tokenizer, special tokens added as fastembed adds them,
        counting the prefix this provider prepends. A budget above the model's limit is refused."""
        from kelmo.store.errors import ArchiveValidationError
        from kelmo.store.memory.embed import TokenWindow
        policy = self.input_policy()
        budget = policy.max_tokens if budget is None else budget
        if type(budget) is not int or budget < 1 or budget > policy.max_tokens:
            raise ArchiveValidationError(f'Token budget {budget} is outside 1..{policy.max_tokens}', field='token_budget')
        prefix = policy.document_prefix if purpose == 'document' else policy.query_prefix
        window = TokenWindow.from_bytes(self._pinned('tokenizer.json'), budget=budget, prefix=prefix,
                                        special_tokens=self._special)
        if (window.identity, window.preparation) != (policy.tokenizer_identity, policy.preparation):
            raise _error('tokenizer.json differs from the space it describes', 'model_missing')
        return window

    def _load(self):
        """The loaded model, its files hashed again under the load lock right before loading: weights
        replaced after describe(), and for a custom model any file added since, are refused
        (model_missing), never embedded under the old identity."""
        with self._lock:
            if self._model is None:
                self.describe()
                self._verify()
                self._model = load_text_embedding(self.model_name, self.model_dir)
            return self._model

    def health(self):
        """The model is verified, its input policy proven and its weights load."""
        from kelmo.store.errors import ArchiveEmbeddingError
        self.input_policy()
        try:
            self._load()
        except ArchiveEmbeddingError:
            raise
        except Exception as error:
            raise _error(f'The model does not load: {error}', 'embedding_unavailable') from error
        return {'ok': True}

    def embed_queries(self, texts) -> VectorBatch:
        return self._embed(texts, 'query')

    def embed_documents(self, texts) -> VectorBatch:
        return self._embed(texts, 'document')

    def _embed(self, texts, purpose):
        texts = _check_texts(texts)
        space = self.describe()
        if self._windows is None:
            self._windows = {kind: self.token_window(purpose=kind) for kind in ('query', 'document')}
        _refuse_long(self._windows[purpose], texts)
        try:
            model = self._load()
            if purpose == 'query':
                raw = list(model.query_embed([self.query_prefix + text for text in texts]))
            else:
                raw = list(model.passage_embed([self.document_prefix + text for text in texts]))
        except Exception as error:
            from kelmo.store.errors import ArchiveEmbeddingError
            if isinstance(error, ArchiveEmbeddingError):
                raise
            raise _error(str(error), 'embedding_unavailable', model=self.model_name) from error
        return _batch(space, purpose, raw, len(texts))


def backend_model_description(model):
    """fastembed's registry entry for a model name (a dict with model, dim, model_file and additional_files),
    or None when the backend does not register the name. Read from TextEmbedding.list_supported_models(),
    the loader's own listing; nothing is downloaded."""
    try:
        from fastembed import TextEmbedding
        listing = TextEmbedding.list_supported_models()
    except (ImportError, AttributeError) as error:
        raise _error(f'The fastembed model registry cannot be read: {error}', 'model_unsupported') from error
    for entry in listing:
        if isinstance(entry, dict) and entry.get('model') == model:
            return entry
    return None


def load_text_embedding(model, model_dir):
    """fastembed over an explicit local directory, never the network."""
    from fastembed import TextEmbedding
    model_dir = Path(model_dir)
    return TextEmbedding(model, cache_dir=str(model_dir.parent), local_files_only=True,
                         specific_model_path=str(model_dir), threads=2)


class _Channel:
    """JSON lines over the launcher's pipes, one request at a time, each bounded by one deadline.

    The deadline covers waiting for the channel, writing the request and reading its answer. While the
    request is being written, answers already in the pipe are read and dropped: requests are serialized,
    so they can only be late answers to earlier ones, and leaving them would let a broker blocked on a
    full answer pipe and a client blocked on a full request pipe wait for each other. Every request
    carries a random id, so a late answer is never taken for the answer to the current request, whichever
    provider sent it. A line that is not JSON, not an object or names no request id is a protocol error
    (reason protocol_error): the channel is closed at once rather than waiting out the deadline, and no
    caller mistakes it for one bad input. A write cut short by the deadline leaves half a line in the
    pipe, so the channel is closed for good; an end of file or an OS error means the pipe itself is gone (`broken`). A channel
    never closes the descriptors it is given; shared_channel gives it duplicates it closes itself."""

    MAX_ANSWER = 16 << 20

    def __init__(self, request_fd, answer_fd):
        self.request_fd = request_fd
        self.answer_fd = answer_fd
        os.set_blocking(request_fd, False)
        self.lock = threading.Lock()
        self.buffer = b''
        self.closed = False
        self.broken = False

    def call(self, kind, texts, timeout):
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=max(timeout, 0)):
            raise _error(f'embedding channel stayed busy for {timeout:.1f} s', 'timeout')
        try:
            if self.closed:
                raise _error('embedding channel is closed', 'embedding_unavailable')
            request_id = uuid.uuid4().hex
            line = json.dumps({'id': request_id, 'kind': kind, 'texts': texts}).encode() + b'\n'
            try:
                self._write(line, deadline)
                while True:
                    answer = self._read_answer(deadline)
                    if answer['id'] == request_id:
                        return answer
                    # Another request's id: a late answer to one that timed out, dropped.
            except TimeoutError:
                raise _error(f'embedding broker did not answer within {timeout:.1f} s', 'timeout') from None
            except (OSError, EOFError) as error:
                self.closed = self.broken = True
                raise _error(f'embedding channel failed: {error}', 'embedding_unavailable') from error
            except ValueError as error:
                self.closed = True
                raise _error(f'embedding channel failed: {error}', 'protocol_error') from error
        finally:
            self.lock.release()

    def _write(self, line, deadline):
        view = memoryview(line)
        with selectors.DefaultSelector() as selector:
            selector.register(self.answer_fd, selectors.EVENT_READ)
            selector.register(self.request_fd, selectors.EVENT_WRITE)
            while view:
                left = deadline - time.monotonic()
                if left <= 0:
                    if len(view) != len(line):
                        self.closed = True
                    raise TimeoutError
                ready = {key.fd for key, _ in selector.select(left)}
                if self.answer_fd in ready:
                    self._drain()
                if self.request_fd in ready:
                    try:
                        view = view[os.write(self.request_fd, view):]
                    except BlockingIOError:
                        pass

    @staticmethod
    def _answer(line):
        """One answer line, refused (ValueError, a protocol error) unless it is a JSON object naming a request."""
        try:
            answer = json.loads(line)
        except RecursionError as error:
            raise ValueError('answer nested too deeply') from error
        if not isinstance(answer, dict) or not isinstance(answer.get('id'), str):
            # Not an answer to any request this channel sent: the broker breaks the protocol.
            raise ValueError(f'an answer that names no request: {str(answer)[:80]!r}')
        return answer

    def _drain(self):
        """Read what the broker has written while a request is being written. Every complete line is
        checked like any answer and then dropped, since during a write it can only be a late answer to an
        earlier request; a malformed one is the same protocol error it is on the read path."""
        data = os.read(self.answer_fd, 1 << 16)
        if not data:
            raise EOFError('the launcher closed the channel')
        *complete, self.buffer = (self.buffer + data).split(b'\n')
        for line in complete:
            self._answer(line)
        if len(self.buffer) > self.MAX_ANSWER:
            raise ValueError('answer too long')

    def _read_answer(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.answer_fd, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise TimeoutError
                data = os.read(self.answer_fd, 1 << 16)
                if not data:
                    raise EOFError('the launcher closed the channel')
                self.buffer += data
                if len(self.buffer) > self.MAX_ANSWER:
                    raise ValueError('answer too long')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return self._answer(line)


# One channel per pipe pair in this process. Weak values: a channel no provider holds any more leaves the
# registry with its last provider and closes its own duplicate descriptors.
_channels = weakref.WeakValueDictionary()
_channels_lock = threading.Lock()
# Pipe pairs that may hold half a request because a process using them died during a call. A channel over
# one is handed out closed, and a launcher passes it to no further process.
_poisoned = set()


def _pipe_identity(fd):
    """The open object a descriptor refers to (device, inode), whatever its number."""
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _close_descriptors(*fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def shared_channel(request_fd, answer_fd):
    """The channel of this process over this pipe pair, shared by every provider built on it, so they
    share one lock, one read buffer and one request order and never read each other's answers.

    The registry is keyed by the pipe objects, not by descriptor numbers, and each channel works on its
    own duplicates of the caller's descriptors, closed when the last provider holding the channel is gone.
    While a channel lives its pipes therefore stay allocated: a pipe opened later, even under recycled
    numbers, is another object with another identity and gets its own channel, never a dead one. A
    broken channel (its pipe ended) is never handed out again; a closed one over the same live pipes stays
    closed, because half a request may be in them. The caller's descriptors are never closed here."""
    key = (_pipe_identity(request_fd), _pipe_identity(answer_fd))
    with _channels_lock:
        channel = _channels.get(key)
        if channel is None or channel.broken:
            owned = []
            try:
                owned.append(os.dup(request_fd))
                owned.append(os.dup(answer_fd))
                channel = _Channel(*owned)
            except OSError:
                _close_descriptors(*owned)
                raise
            weakref.finalize(channel, _close_descriptors, *owned)
            _channels[key] = channel
        if key in _poisoned:
            channel.closed = True
        return channel


def poison_channel(request_fd, answer_fd):
    """Mark a pipe pair as possibly holding half a request: another process using it died during a call.
    It stays closed in this process for good, exactly as a channel whose own write was cut short."""
    key = (_pipe_identity(request_fd), _pipe_identity(answer_fd))
    with _channels_lock:
        _poisoned.add(key)
        channel = _channels.get(key)
        if channel is not None:
            channel.closed = True


def channel_poisoned(request_fd, answer_fd):
    key = (_pipe_identity(request_fd), _pipe_identity(answer_fd))
    with _channels_lock:
        return key in _poisoned


class BrokeredProvider:
    """The launcher's embedding channel: JSON lines {"id", "kind", "texts"} out, {"id", "vectors"} back.

    Documents travel with kind "passage", queries with "query", health with "health". verify() requires
    the health answer to say ok: true, to report the same whole space as the configuration and to report
    its input policy {"max_tokens", "query_prefix", "document_prefix", "special_tokens"}: the prefixes the
    broker itself prepends and the special tokens its loader adds to tokenizer.json, in order ([] when
    it adds none, never omitted); texts are sent as they are. The declared space's transforms must name
    exactly that input path (embed.bound_transform), so a broker that changes prefixes, additions or limit
    under the same declared space is refused (space_mismatch). Every vector answer must carry that space's
    space_id, so a broker that changed model after verification is refused. Inputs are counted with the
    configured tokenizer, whose bytes must be the space's tokenizer, plus the reported additions, and an
    input over the limit is refused. Providers over the same pipes share one channel (shared_channel).
    """

    MAX_TEXTS = 64

    def __init__(self, request_fd, answer_fd, space, *, tokenizer, query_timeout=5.0, document_timeout=60.0,
                 channel=None):
        if not isinstance(space, EmbeddingSpace):
            raise TypeError('A brokered provider needs its declared EmbeddingSpace')
        self._space = space
        self._tokenizer = Path(tokenizer)
        self._channel = shared_channel(request_fd, answer_fd) if channel is None else channel
        self._additions = None
        self._policy = None
        self._windows = None
        self.query_timeout = query_timeout
        self.document_timeout = document_timeout
        self.identity_verified = False

    @classmethod
    def from_environment(cls, space, env_name, **options):
        from kelmo.store.config import get_embed_fds
        try:
            fds = get_embed_fds(env_name)
        except ValueError as error:
            raise _error(f'{env_name} does not name an embedding channel: {error}', 'embedding_unavailable') from error
        if fds is None:
            raise _error(f'no embedding channel: {env_name} is not set', 'embedding_unavailable')
        try:
            return cls(*fds, space, **options)
        except OSError as error:
            raise _error(f'{env_name} names unusable descriptors: {error}', 'embedding_unavailable') from error

    def describe(self) -> EmbeddingSpace:
        return self._space

    def _call(self, kind, texts, timeout):
        try:
            answer = self._channel.call(kind, texts, timeout)
        except Exception:
            # A broker that timed out or failed may come back as another model: prove it again.
            self.identity_verified = False
            raise
        if 'error' in answer:
            # An explicit failure is as much a reason to prove the identity again as an exception.
            self.identity_verified = False
            raise _error(f'embedding broker: {answer["error"]}', 'embedding_unavailable')
        return answer

    def health(self):
        answer = self._call('health', [], self.query_timeout)
        if answer.get('ok') is not True:
            self.identity_verified = False
            raise _error('The embedding broker reports it is not healthy', 'embedding_unavailable')
        return answer

    def verify(self) -> EmbeddingSpace:
        from kelmo.store.memory.embed import InputPolicy, preparation_digest, special_token_additions, transforms_bind
        # An earlier proof does not survive a failed one: until this call succeeds nothing is verified, and
        # the old policy and windows cannot be used.
        self.identity_verified = False
        self._additions = self._policy = self._windows = None
        answer = self.health()
        reported, policy = answer.get('space'), answer.get('input')
        if reported is None:
            raise _error('The embedding broker does not report its embedding space; it is unsupported until '
                         'it does', 'embedding_identity_unverified')
        try:
            space = EmbeddingSpace.from_dict(reported)
        except (KeyError, TypeError, ValueError) as error:
            raise _error('The embedding broker reports an invalid space', 'embedding_identity_unverified') from error
        if space != self._space:
            raise _error('The embedding broker serves a different embedding space', 'space_mismatch')
        try:
            if not isinstance(policy, dict):
                raise TypeError('no input policy')
            additions = special_token_additions(policy['special_tokens'])
            policy = InputPolicy(space.tokenizer_identity, policy['max_tokens'], policy['query_prefix'],
                                 policy['document_prefix'], preparation_digest(additions))
        except (KeyError, TypeError, ValueError) as error:
            raise _error(f'The embedding broker reports no complete input policy ({error}); it is unsupported '
                         'until it does', 'input_policy_unproven') from error
        if not transforms_bind(self._space, policy):
            # Same declared space, another input path: its vectors would not be the declared space's.
            raise _error('The embedding broker reports an input path its declared space does not name',
                         'space_mismatch')
        windows = {kind: self._window(policy, additions, policy.max_tokens, kind) for kind in ('query', 'document')}
        self._additions, self._policy, self._windows = additions, policy, windows
        self.identity_verified = True
        return space

    def _window(self, policy, additions, budget, purpose):
        """A window over the configured tokenizer file, refused unless its bytes and additions are the
        verified policy's tokenizer and preparation."""
        from kelmo.store.memory.embed import TokenWindow
        prefix = policy.document_prefix if purpose == 'document' else policy.query_prefix
        window = TokenWindow.from_file(self._tokenizer, budget=budget, prefix=prefix, special_tokens=additions)
        if (window.identity, window.preparation) != (policy.tokenizer_identity, policy.preparation):
            raise _error('The configured tokenizer is not the broker space\'s tokenizer', 'input_policy_unproven')
        return window

    def input_policy(self):
        if not self.identity_verified:
            self.verify()
        return self._policy

    def token_window(self, budget=None, *, purpose='document'):
        from kelmo.store.errors import ArchiveValidationError
        policy = self.input_policy()
        budget = policy.max_tokens if budget is None else budget
        if type(budget) is not int or budget < 1 or budget > policy.max_tokens:
            raise ArchiveValidationError(f'Token budget {budget} is outside 1..{policy.max_tokens}', field='token_budget')
        return self._window(policy, self._additions, budget, purpose)

    def embed_queries(self, texts) -> VectorBatch:
        return self._embed(texts, 'query', 'query', self.query_timeout)

    def embed_documents(self, texts) -> VectorBatch:
        return self._embed(texts, 'document', 'passage', self.document_timeout)

    def _embed(self, texts, purpose, kind, timeout):
        from kelmo.store.memory.embed import identity_id
        texts = _check_texts(texts)
        if not self.identity_verified:
            self.verify()
        _refuse_long(self._windows[purpose], texts)
        expected = identity_id(self._space)
        width = self._space.dimension * 4
        raw = []
        for start in range(0, len(texts), self.MAX_TEXTS):
            batch = texts[start:start + self.MAX_TEXTS]
            answer = self._call(kind, batch, timeout)
            if answer.get('space_id') != expected:
                self.identity_verified = False
                raise _error('A vector answer does not name the verified space', 'space_mismatch')
            try:
                vectors = [base64.b64decode(item, validate=True) for item in answer['vectors']]
            except (KeyError, TypeError, ValueError) as error:
                raise _error(f'malformed answer from the embedding broker: {error}', 'malformed_vectors') from error
            if len(vectors) != len(batch) or any(len(vector) != width for vector in vectors):
                raise _error(f'Expected {len(batch)} vectors of {width} bytes', 'malformed_vectors')
            raw.extend(np.frombuffer(vector, dtype='<f4') for vector in vectors)
        return _batch(self._space, purpose, raw, len(texts))


def provider_from_settings(settings):
    """The provider a validated [embedding] configuration names; None when none is configured."""
    if settings is None:
        return None
    if settings['provider'] == 'local':
        return LocalFastEmbedProvider(
            settings['model_dir'], model=settings['model'], files=settings['files'],
            pooling=settings['pooling'], projection=settings['projection'],
            normalization=settings['normalization'], dimension=settings['dimension'],
            query_prefix=settings['query_prefix'], document_prefix=settings['document_prefix'])
    return BrokeredProvider.from_environment(EmbeddingSpace.from_dict(settings['space']), settings['fds_env'],
                                             tokenizer=settings['tokenizer'])


def declared_policy(settings):
    """The broker's declared input policy from configuration, refused unless the declared space's
    transforms name exactly it. Checked again against the broker's own health answer at verify()."""
    from kelmo.store.memory.embed import InputPolicy, preparation_digest, special_token_additions, transforms_bind
    space = EmbeddingSpace.from_dict(settings['space'])
    declared = settings['input']
    try:
        additions = special_token_additions(declared['special_tokens'])
        policy = InputPolicy(space.tokenizer_identity, declared['max_tokens'], declared['query_prefix'],
                             declared['document_prefix'], preparation_digest(additions))
    except (KeyError, TypeError, ValueError) as error:
        raise _error(f'The declared broker input policy is incomplete: {error}', 'input_policy_unproven') from error
    if not transforms_bind(space, policy):
        raise _error('The declared broker space does not name the declared input path', 'input_policy_unproven')
    return policy, additions


def declared_window(settings, provider=None):
    """The TokenWindow chunks of this configuration are cut against, built without any live embedding call,
    so text, chunks and FTS stay durable while the model or broker is down.

    Local: the provider's own window over its pinned tokenizer and policy files (hashed, never loaded as a
    model). Broker: the configured tokenizer.json and the declared input policy, refused unless their
    identities are the declared space's; the live broker must later report the same path to get vectors.
    The configured token_budget applies when set, never above the declared input limit."""
    from kelmo.store.errors import ArchiveValidationError
    from kelmo.store.memory.embed import TokenWindow
    budget = settings.get('token_budget')
    if settings['provider'] == 'local':
        if provider is None:
            raise _error('No local provider was built from these settings', 'model_missing')
        return provider.token_window(budget)
    policy, additions = declared_policy(settings)
    budget = policy.max_tokens if budget is None else budget
    if type(budget) is not int or budget < 1 or budget > policy.max_tokens:
        raise ArchiveValidationError(f'Token budget {budget} is outside 1..{policy.max_tokens}', field='token_budget')
    window = TokenWindow.from_file(settings['tokenizer'], budget=budget, prefix=policy.document_prefix,
                                   special_tokens=additions)
    if (window.identity, window.preparation) != (policy.tokenizer_identity, policy.preparation):
        raise _error('The configured tokenizer is not the declared space\'s tokenizer', 'input_policy_unproven')
    return window


def token_window_from_settings(settings, provider):
    """The TokenWindow chunks of this configuration are cut against: the input path of the provider the
    caller already holds (opening another one would open another channel), with the configured
    token_budget when one is set (never above the model's limit)."""
    if provider is None:
        raise TypeError('token_window_from_settings needs the provider already built from these settings')
    return provider.token_window(settings.get('token_budget'))
