"""One validated configuration for the selected vault."""
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .errors import KelmoError
    from .budget import public as public_budget, validate as validate_budget
    from .hot import validate as validate_hot
    from .session_settings import validate as validate_sessions
else:
    from errors import KelmoError
    from budget import public as public_budget, validate as validate_budget
    from hot import validate as validate_hot
    from session_settings import validate as validate_sessions


_SPACE_FIELDS = ('contract_version', 'artifact_identity', 'tokenizer_identity', 'query_transform',
                 'document_transform', 'pooling', 'projection', 'normalization', 'dimension', 'dtype',
                 'byte_order')
# Editor context budget of a plain-text creation when the caller names none. The editor sends the
# archived source, its reconciliation receipts and every mandatory person page whole, then the selected
# and requested pages, so a vault with large mandatory pages raises this in [editor] instead of per call.
DEFAULT_EDITOR_BUDGET = 24000
# When archived notes reach the editor: as the worker archives them, or in the Dreaming pass.
EDITOR_MODES = ('immediate', 'nightly')
# Ceiling on the sources one `kelmo dream apply` batch consults the model about at once, when
# [editor] batch_apply is on. The budget decides the batch's real size; this only bounds how many
# sources one model answer has to cover.
DEFAULT_BATCH_MAX_SOURCES = 8


def validate_embedding(data, root):
    """The optional [embedding] table: an explicitly configured chunk-index provider, or None.

    provider = "local": model, model_dir (a provisioned directory; relative paths are inside the
    vault); for any model other than the pinned record model also files ({"relative/path" = [size,
    sha256]} for every regular file in model_dir: config.json, tokenizer.json, tokenizer_config.json,
    special_tokens_map.json, exactly one .onnx graph and any external-data or sidecar file; at run time
    an unpinned file or a link in model_dir is refused), and pooling, projection, normalization and
    dimension, which the pinned model fixes. For a custom model those four are the operator's declaration,
    checked only where the artifact's own metadata states them. Optional: query_prefix, document_prefix.
    The counting tokenizer is always model_dir/tokenizer.json.
    provider = "broker": fds_env (the variable holding the launcher's pipes), tokenizer (a local copy of
    the space's tokenizer.json) and the whole declared space, float32 little-endian, whose
    query_transform and document_transform end with the broker's input binding
    (kelmo.store.memory.embed.bound_transform), and the input table those transforms name (max_tokens,
    query_prefix, document_prefix, special_tokens), from which chunks are cut while the broker is down; a
    broker reporting another input path is refused. Optional for both: token_budget (default and
    ceiling: the model's input limit), target_chars, max_chars, overlap_chars, which every ingestion,
    re-cut and profile of this vault uses. Nothing has a host or model default and nothing downloads.
    """
    if data is None:
        return None
    try:
        return _embedding(data, root)
    except (ValueError, KeyError, TypeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid [embedding] configuration: {error}') from error


def _embedding(data, root):
    import importlib
    from kelmo.store.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS, CHUNK_TARGET_CHARS

    def sibling(name):
        if __package__:
            return importlib.import_module('.' + name, __package__)
        return importlib.import_module(name)

    if not isinstance(data, dict):
        raise TypeError('[embedding] must be a table')
    common = {'provider', 'token_budget', 'target_chars', 'max_chars', 'overlap_chars'}
    # The local tokenizer is always the provisioned model's own; broker prefixes are the broker's to
    # report, because only the bytes it actually embeds can be counted.
    allowed = {'local': common | {'model', 'model_dir', 'files', 'pooling', 'projection', 'normalization',
                                  'dimension', 'query_prefix', 'document_prefix'},
               'broker': common | {'fds_env', 'space', 'tokenizer', 'input', 'command'}}.get(data.get('provider'))
    if allowed is None:
        raise ValueError('provider must be "local" or "broker"')
    unknown = set(data) - allowed
    if unknown:
        raise ValueError('keys not accepted for this provider: ' + ', '.join(sorted(unknown)))

    def text(key, default=None):
        value = data.get(key, default)
        if not isinstance(value, str) or (default is None and not value.strip()):
            raise ValueError(f'{key} must be text')
        return value

    def integer(key, default=None, minimum=1):
        value = data.get(key, default)
        if type(value) is not int or value < minimum:
            raise ValueError(f'{key} must be an integer >= {minimum}')
        return value

    def path(key):
        value = Path(text(key)).expanduser()
        return str(value if value.is_absolute() else root / value)

    settings = {
        'provider': data.get('provider'),
        'query_prefix': text('query_prefix', ''),
        'document_prefix': text('document_prefix', ''),
        # Absent: the model's own input limit. Present: checked against that limit when the provider
        # states it, since only the provisioned files or the broker know it.
        'token_budget': integer('token_budget') if 'token_budget' in data else None,
        'target_chars': integer('target_chars', CHUNK_TARGET_CHARS),
        'max_chars': integer('max_chars', CHUNK_MAX_CHARS),
        'overlap_chars': integer('overlap_chars', CHUNK_OVERLAP_CHARS, minimum=0),
    }
    if not settings['overlap_chars'] < settings['target_chars'] <= settings['max_chars']:
        raise ValueError('chunk sizes need overlap_chars < target_chars <= max_chars')
    if settings['provider'] == 'local':
        model_store = sibling('model_store')
        settings['model'] = text('model')
        settings['model_dir'] = path('model_dir')
        overridden = sorted(set(model_store.PINNED_SPEC) & set(data))
        if settings['model'] == model_store.MODEL and 'files' not in data and overridden:
            raise ValueError('the pinned model fixes ' + ', '.join(overridden))
        files = model_store.FILES if settings['model'] == model_store.MODEL and 'files' not in data else data['files']
        if not isinstance(files, dict):
            raise ValueError('files must be a table of pinned model files')
        settings['files'] = {}
        for name, pin in files.items():
            if not isinstance(pin, (list, tuple)) or len(pin) != 2:
                raise ValueError(f'files.{name} must be [size, sha256]')
            size, checksum = pin
            if (type(size) is not int or size < 0 or not isinstance(checksum, str)
                    or not re.fullmatch('[0-9a-f]{64}', checksum)):
                raise ValueError(f'files.{name} must be [size, sha256]')
            settings['files'][name] = (size, checksum)
        # An explicit table equal to the pinned one is the pinned model too (the provider decides the same
        # way): any of the four it declares must be the pinned value.
        pinned = model_store.record_model_pin(settings['model'], settings['files'],
                                              {**model_store.PINNED_SPEC, **{key: data[key] for key in overridden}})
        if not pinned:
            try:
                model_store.check_layout(settings['files'])
            except model_store.ModelImportError as error:
                raise ValueError(f'files: {error}') from error
        for key in ('pooling', 'projection', 'normalization'):
            settings[key] = model_store.PINNED_SPEC[key] if pinned else text(key)
        settings['dimension'] = model_store.PINNED_SPEC['dimension'] if pinned else integer('dimension')
        settings['tokenizer'] = str(Path(settings['model_dir']) / 'tokenizer.json')
    else:
        contracts = sibling('contracts')
        settings['query_prefix'] = settings['document_prefix'] = None
        settings['fds_env'] = text('fds_env')
        if 'command' in data:
            command = data['command']
            if (not isinstance(command, list) or not command
                    or any(not isinstance(arg, str) or not arg.strip() for arg in command)
                    or not Path(command[0]).is_absolute()):
                raise ValueError('command must be a non-empty list of non-empty strings with an absolute argv[0]')
            settings['command'] = list(command)
        space = data['space']
        if not isinstance(space, dict) or set(space) != set(_SPACE_FIELDS):
            raise ValueError('space must name every EmbeddingSpace field')
        declared = contracts.EmbeddingSpace.from_dict(space)
        if (declared.contract_version, declared.dtype, declared.byte_order) != (
                contracts.CONTRACT_VERSION, 'float32', 'little'):
            raise ValueError('the broker space must be this contract version, float32, little-endian')
        if not all(';input=' in transform for transform in (declared.query_transform, declared.document_transform)):
            raise ValueError('the broker space transforms must name their input path (;input=...)')
        settings['space'] = declared.to_dict()
        settings['tokenizer'] = path('tokenizer')
        settings['input'] = _declared_input(data['input'], declared, settings['token_budget'])
    return settings


def _declared_input(value, space, token_budget):
    """The broker input path declared offline, so chunks can be cut while the broker is down: max_tokens,
    query_prefix, document_prefix and special_tokens (the loader's additions, in order; [] for none). It
    must be exactly the path the declared space's transforms name."""
    from kelmo.store.memory.embed import InputPolicy, preparation_digest, special_token_additions, transforms_bind
    if not isinstance(value, dict) or set(value) != {'max_tokens', 'query_prefix', 'document_prefix',
                                                      'special_tokens'}:
        raise ValueError('input must name max_tokens, query_prefix, document_prefix and special_tokens')
    additions = special_token_additions(value['special_tokens'])
    policy = InputPolicy(space.tokenizer_identity, value['max_tokens'], value['query_prefix'],
                         value['document_prefix'], preparation_digest(additions))
    if not transforms_bind(space, policy):
        raise ValueError('the declared space transforms do not name the declared input path')
    if token_budget is not None and token_budget > policy.max_tokens:
        raise ValueError(f'token_budget {token_budget} exceeds the declared input limit {policy.max_tokens}')
    return {'max_tokens': policy.max_tokens, 'query_prefix': policy.query_prefix,
            'document_prefix': policy.document_prefix, 'special_tokens': additions}


def validate_telemetry(data):
    """The optional [telemetry] table, or None (search telemetry off).

    search_log = "off" | "redacted" | "full": redacted keeps query hashes and lengths, filter names and
    ranked ids; full also keeps query text and filter values. retention_days (1..365, default 30) and
    max_rows (1..100000, default 5000) bound what is kept. Nothing else is accepted."""
    if data is None:
        return None
    from kelmo.store.memory.search_log import DEFAULTS, MODES
    try:
        if not isinstance(data, dict):
            raise TypeError('[telemetry] must be a table')
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise ValueError('keys not accepted: ' + ', '.join(sorted(unknown)))
        settings = dict(DEFAULTS, **data)
        if settings['search_log'] not in MODES:
            raise ValueError('search_log must be "off", "redacted" or "full"')
        for key, ceiling in (('retention_days', 365), ('max_rows', 100000)):
            if type(settings[key]) is not int or not 1 <= settings[key] <= ceiling:
                raise ValueError(f'{key} must be an integer between 1 and {ceiling}')
        return settings
    except (ValueError, TypeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid [telemetry] configuration: {error}') from error


def validate_editor(data):
    """The optional [editor] table, or None (built-in defaults).

    budget_tokens (a positive integer) is the creation budget used when a caller names none; the
    MCP memory_create tool never names one. mode is `immediate` (default: the worker publishes a note
    as soon as it is archived) or `nightly` (archived notes wait for `kelmo dream apply`).
    batch_apply (a boolean, default false) lets one `kelmo dream apply` pass consult the model once
    per batch of waiting sources of one project instead of once per source; batch_max_sources (a
    positive integer) caps the sources in one batch; batch_ready_sources (a positive integer, default
    the value of batch_max_sources) is how many first-pass notes of one project make it ready for
    `kelmo dream apply --ready`. Nothing else is accepted."""
    if data is None:
        return None
    try:
        if not isinstance(data, dict):
            raise TypeError('[editor] must be a table')
        unknown = set(data) - {'budget_tokens', 'mode', 'batch_apply', 'batch_max_sources',
                               'batch_ready_sources'}
        if unknown:
            raise ValueError('keys not accepted: ' + ', '.join(sorted(unknown)))
        settings = {'budget_tokens': data.get('budget_tokens', DEFAULT_EDITOR_BUDGET),
                    'mode': data.get('mode', 'immediate'),
                    'batch_apply': data.get('batch_apply', False),
                    'batch_max_sources': data.get('batch_max_sources', DEFAULT_BATCH_MAX_SOURCES)}
        settings['batch_ready_sources'] = data.get('batch_ready_sources', settings['batch_max_sources'])
        if type(settings['budget_tokens']) is not int or settings['budget_tokens'] <= 0:
            raise ValueError('budget_tokens must be a positive integer')
        if settings['mode'] not in EDITOR_MODES:
            raise ValueError('mode must be one of ' + ', '.join(EDITOR_MODES))
        if type(settings['batch_apply']) is not bool:
            raise ValueError('batch_apply must be a boolean')
        if type(settings['batch_max_sources']) is not int or settings['batch_max_sources'] <= 0:
            raise ValueError('batch_max_sources must be a positive integer')
        if type(settings['batch_ready_sources']) is not int or settings['batch_ready_sources'] <= 0:
            raise ValueError('batch_ready_sources must be a positive integer')
        return settings
    except (ValueError, TypeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid [editor] configuration: {error}') from error


def validate_alerts(data):
    """The optional [alerts] table, or None (alerts go to the worker log only).

    command is the argv that receives one alert text on stdin and exits 0 once it is delivered; its first
    element is an absolute path. label (default "Kelmo") starts every message. Nothing else is
    accepted."""
    if data is None:
        return None
    try:
        if not isinstance(data, dict):
            raise TypeError('[alerts] must be a table')
        unknown = set(data) - {'command', 'label'}
        if unknown:
            raise ValueError('keys not accepted: ' + ', '.join(sorted(unknown)))
        command, label = data.get('command'), data.get('label', 'Kelmo')
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) and part and '\0' not in part for part in command)
                or not Path(command[0]).is_absolute()):
            raise ValueError('command must be a list of strings starting with an absolute path')
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('label must be a short non-empty string')
        return {'command': list(command), 'label': label}
    except (ValueError, TypeError) as error:
        raise KelmoError('E_CONFIG', f'Invalid [alerts] configuration: {error}') from error


@dataclass(frozen=True)
class Config:
    root: Path
    vault_id: str
    label: str
    wiki: Path
    hot: dict | None = None
    sessions: dict | None = None
    budget: dict | None = None
    schema_version: int = 1
    state_root: Path | None = None
    archive_root: Path | None = None
    knowledge_root: Path | None = None
    registry_relative: str = 'knowledge/provenance/registry.json'
    wiki_relative: str = 'knowledge/wiki'
    # Validated [embedding] settings; None selects the accepted record API.
    embedding: dict | None = None
    telemetry: dict | None = None
    # Validated [editor] settings; None selects the built-in creation budget.
    editor: dict | None = None
    # Validated [alerts] settings; None keeps alerts in the worker log.
    alerts: dict | None = None

    @property
    def editor_budget(self):
        """The creation budget for a caller that names none."""
        return (self.editor or {}).get('budget_tokens', DEFAULT_EDITOR_BUDGET)

    @property
    def editor_mode(self):
        """`immediate`: the worker publishes a note as soon as it is archived. `nightly`: archived notes
        wait for `kelmo dream apply` (the scheduled Dreaming pass, or the owner running it now)."""
        return (self.editor or {}).get('mode', 'immediate')

    @property
    def batch_apply(self):
        """True when one `kelmo dream apply` pass holds one conversation with the model per batch of waiting sources
        of a project. Off unless the vault names `[editor] batch_apply = true`."""
        return bool((self.editor or {}).get('batch_apply', False))

    @property
    def batch_max_sources(self):
        """The ceiling on the sources one apply batch may carry; the budget usually decides the real size."""
        return (self.editor or {}).get('batch_max_sources', DEFAULT_BATCH_MAX_SOURCES)

    @property
    def batch_ready_sources(self):
        """How many first-pass notes of one project make it ready for `kelmo dream apply --ready`.

        Its own setting, not the batch ceiling: a vault can wait for ten notes of a project and still ask
        the model about eight at a time. When the vault names none it is the value of `batch_max_sources`."""
        editor = self.editor or {}
        return editor.get('batch_ready_sources', editor.get('batch_max_sources', DEFAULT_BATCH_MAX_SOURCES))

    @property
    def state(self):
        return self.state_root if self.state_root is not None else self.root / '.state'

    @property
    def archive(self):
        return (self.archive_root if self.archive_root is not None else self.root / 'archive') / 'archive.db'

    @property
    def knowledge_git_root(self):
        return self.knowledge_root if self.knowledge_root is not None else self.root

    @property
    def cache(self):
        return self.root / '.cache/embeddings'

    @property
    def registry(self):
        return self.knowledge_git_root / self.registry_relative

    @classmethod
    def load(cls, root):
        root = Path(root).expanduser().resolve()
        path = root / 'kelmo.toml'
        if path.is_symlink():
            raise KelmoError('E_CONFIG', 'Vault configuration cannot be a symlink.')
        if not path.is_file():
            raise KelmoError('E_NOT_INITIALIZED', 'Vault is not initialized; run kelmo init.')
        try:
            with path.open('rb') as stream:
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ValueError
            data = tomllib.loads(raw.decode('utf-8'))
            if type(data['schema_version']) is not int or data['schema_version'] not in (1, 2) or data['writer'] != 'local':
                raise ValueError
            if not isinstance(data['vault_id'], str):
                raise ValueError
            uuid.UUID(data['vault_id'])
            label = data['node_label']
            if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', label):
                raise ValueError
            configured_wiki = data.get('wiki_root')
            storage = None
            if data['schema_version'] == 2:
                storage = data['storage']
                if not isinstance(storage, dict) or configured_wiki is not None:
                    raise ValueError
                roots = []
                for name in ('state_root', 'archive_root', 'knowledge_git_root'):
                    value = storage[name]
                    if not isinstance(value, str) or not value.strip() or '\x00' in value:
                        raise ValueError
                    selected = Path(value).expanduser()
                    roots.append(cls.checked_path(selected if selected.is_absolute() else root / selected))
                state, archive, knowledge = roots
                for index, first in enumerate(roots):
                    if first == root or root.is_relative_to(first):
                        raise ValueError
                    for second in roots[index + 1:]:
                        if first.is_relative_to(second) or second.is_relative_to(first):
                            raise ValueError
                    if first.exists() and not first.is_dir():
                        raise ValueError
                for name in ('registry_relative', 'wiki_relative'):
                    value = storage[name]
                    if (not isinstance(value, str) or not value or '\x00' in value
                            or Path(value).is_absolute() or '..' in Path(value).parts
                            or str(Path(value)) != value or any(ord(char) < 32 or char in '*?[]:\\' for char in value)
                            or any(part.startswith('.') for part in Path(value).parts)):
                        raise ValueError
                registry = cls.checked_path(knowledge / storage['registry_relative'])
                wiki = cls.checked_path(knowledge / storage['wiki_relative'])
                if (registry.suffix != '.json' or registry.name == 'generation.json'
                        or registry.is_relative_to(wiki) or wiki.is_relative_to(registry)):
                    raise ValueError
                if (registry.exists() and not registry.is_file()) or (wiki.exists() and not wiki.is_dir()):
                    raise ValueError
                for selected in (state, archive, wiki, registry.parent):
                    cls.check_descendants(selected)
            elif configured_wiki is None:
                wiki = root / 'knowledge/wiki'
            else:
                if not isinstance(configured_wiki, str) or not configured_wiki.strip():
                    raise ValueError
                wiki = Path(configured_wiki).expanduser()
                if not wiki.is_absolute():
                    wiki = root / wiki
                if any(path.is_symlink() for path in (wiki, *wiki.parents)):
                    raise KelmoError(
                        'E_CONFIG',
                        f'Selected wiki path {wiki} uses a symlink. '
                        'Select a real non-symlink path.',
                    )
                wiki = wiki.resolve()
                if wiki == root or wiki.is_relative_to(root) or root.is_relative_to(wiki):
                    raise KelmoError('E_CONFIG', 'An explicit wiki root must be separate from the data root.')
        except (ValueError, KeyError, TypeError) as error:
            raise KelmoError('E_CONFIG', 'Unsupported or invalid vault configuration.') from error
        return cls(root, data['vault_id'], label, wiki, validate_hot(data.get('hot'), root),
                   validate_sessions(data.get('sessions'), root), validate_budget(data.get('budget'), root),
                   data['schema_version'], state if storage is not None else None,
                   archive if storage is not None else None, knowledge if storage is not None else None,
                   storage['registry_relative'] if storage is not None else 'knowledge/provenance/registry.json',
                   storage['wiki_relative'] if storage is not None else 'knowledge/wiki',
                   validate_embedding(data.get('embedding'), root),
                   validate_telemetry(data.get('telemetry')), editor=validate_editor(data.get('editor')),
                   alerts=validate_alerts(data.get('alerts')))

    @staticmethod
    def checked_path(path):
        path = Path(path)
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise KelmoError('E_CONFIG', 'Selected path uses a symlink. Select a real non-symlink path.')
        return path.resolve()

    @classmethod
    def check_descendants(cls, root):
        if root.is_dir():
            for path in root.rglob('*'):
                if path.is_symlink():
                    raise KelmoError('E_CONFIG', 'Bound storage descendants cannot be symlinks.')

    def internal(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise KelmoError('E_STATE', 'Internal path must stay inside the vault.')
        path = self.root
        if relative.parts and relative.parts[0] == '.state':
            path, relative = self.state, Path(*relative.parts[1:])
        elif relative.parts and relative.parts[0] == 'archive':
            path, relative = self.archive.parent, Path(*relative.parts[1:])
        elif relative.parts[:2] == ('knowledge', 'wiki'):
            path, relative = self.wiki, Path(*relative.parts[2:])
        elif relative.parts and relative.parts[0] == 'knowledge':
            # Provenance and HOT live beside the wiki in the bound knowledge repository (schema 2).
            path, relative = self.knowledge_git_root, relative
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise KelmoError('E_STATE', 'Internal vault paths cannot be symlinks.')
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise KelmoError('E_STATE', 'Internal vault paths cannot be symlinks.')
        return path

    def local_bindings(self):
        """Trusted local identity comes from the selected controller-owned vault."""
        from .server import OriginBindings
        return OriginBindings.local(self.vault_id, self.label)

    def as_dict(self):
        result = {'schema_version': self.schema_version, 'vault_id': self.vault_id, 'root': str(self.root),
                    'node_label': self.label, 'writer': 'local', 'archive': str(self.archive),
                    'wiki': str(self.wiki), 'cache': str(self.cache), 'hot': self.hot,
                    'sessions': self.sessions, 'budget': public_budget(self.budget)}
        if self.schema_version == 2:
            result['storage'] = {'state_root': str(self.state), 'archive_root': str(self.archive.parent),
                                 'knowledge_git_root': str(self.knowledge_git_root),
                                 'registry_relative': self.registry_relative, 'wiki_relative': self.wiki_relative}
        if self.embedding is not None:
            result['embedding'] = {key: value for key, value in self.embedding.items() if key != 'files'}
        if self.telemetry is not None:
            result['telemetry'] = dict(self.telemetry)
        if self.editor is not None:
            result['editor'] = dict(self.editor)
        if self.alerts is not None:
            result['alerts'] = {'command': list(self.alerts['command']), 'label': self.alerts['label']}
        return result


CLIENT_FILE = 'client.toml'
CLIENT_FIELDS = frozenset({'schema_version', 'mode', 'authority_id', 'command', 'timeout_seconds', 'sessions'})
# The largest transcript one session_capture frame carries: base64 (4 bytes per 3) of it plus a 1 MiB
# envelope stays within server.MAX_FRAME (32 MiB).
MAX_CLIENT_SNAPSHOT_BYTES = (32 * 1048576 - 1048576) // 4 * 3


@dataclass(frozen=True)
class ClientProfile:
    """An explicit opt-in shared-client root: the channel command and the authority it must reach.

    The command is written by an installer or the user, never by a request or a server reply.
    The client keeps only its outbox and a labelled read cache; it is never a writer.
    """
    root: Path
    authority_id: str
    command: tuple
    timeout_seconds: float
    sessions: dict
    max_snapshot_bytes: int

    @property
    def state(self):
        return self.root / '.client'

    @staticmethod
    def selected(root):
        root = Path(root).expanduser()
        return (root / CLIENT_FILE).is_file() and not (root / 'kelmo.toml').exists()

    @classmethod
    def load(cls, root):
        root = Path(root).expanduser().resolve()
        path = root / CLIENT_FILE
        if path.is_symlink() or not path.is_file():
            raise KelmoError('E_NOT_INITIALIZED', 'No shared-client profile is configured here.')
        return cls.parse(root, path.read_bytes())

    @classmethod
    def parse(cls, root, raw):
        """Validate a prospective profile without creating its directory or opening a channel."""
        root = Path(root).expanduser().resolve()
        try:
            if len(raw) > 65536:
                raise ValueError
            data = tomllib.loads(raw.decode('utf-8'))
            if set(data) - CLIENT_FIELDS or data['schema_version'] != 1 or data['mode'] != 'shared':
                raise ValueError
            authority = data['authority_id']
            if not isinstance(authority, str):
                raise ValueError
            uuid.UUID(authority)
            command = data['command']
            if (not isinstance(command, list) or not command or not all(isinstance(item, str) and item
                                                                         and '\x00' not in item for item in command)
                    or not Path(command[0]).is_absolute()):
                raise ValueError
            timeout = data.get('timeout_seconds', 20)
            if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
                raise ValueError
            raw_sessions = data.get('sessions', {})
            if not isinstance(raw_sessions, dict) or set(raw_sessions) - {'claude', 'codex', 'max_snapshot_bytes'}:
                raise ValueError
            limit = raw_sessions.get('max_snapshot_bytes', 16 * 1048576)
            if type(limit) is not int or not 1 <= limit <= MAX_CLIENT_SNAPSHOT_BYTES:
                raise ValueError
            sessions = {}
            for provider in ('claude', 'codex'):
                roots = raw_sessions.get(provider, [])
                if not isinstance(roots, list) or not all(isinstance(item, str) and Path(item).is_absolute()
                                                         for item in roots):
                    raise ValueError
                sessions[provider] = tuple(str(Path(item)) for item in roots)
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise KelmoError('E_CONFIG', 'Unsupported or invalid shared-client profile.') from error
        return cls(root, authority, tuple(command), float(timeout), sessions, limit)

    def as_dict(self):
        return {'mode': 'shared', 'root': str(self.root), 'authority_id': self.authority_id,
                'command': list(self.command), 'timeout_seconds': self.timeout_seconds,
                'sessions': {key: list(value) for key, value in self.sessions.items()},
                'max_snapshot_bytes': self.max_snapshot_bytes, 'writer': False}
