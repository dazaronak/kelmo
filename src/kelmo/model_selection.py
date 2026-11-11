"""Discover installed backend models and select verified local or broker profiles."""
import hashlib
import json
import os
from pathlib import Path
import stat

from .config import Config, validate_embedding
from .config_edit import replace_table
from .errors import KelmoError
from . import model_store


def catalog(query=None):
    from fastembed import TextEmbedding
    models = TextEmbedding.list_supported_models()
    if query:
        query = query.casefold()
        models = [item for item in models if query in (item['model'] + ' ' + item.get('description', '')).casefold()]
    keys = ('model', 'dim', 'size_in_GB', 'license', 'description', 'model_file')
    return {'models': [{key: item.get(key) for key in keys} for item in models],
            'downloads_started': False}


def pin_directory(directory):
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        raise KelmoError('E_MODEL_IMPORT', 'Select a real local model directory.')
    pins = {}
    for current, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise KelmoError('E_MODEL_IMPORT', 'Model artifacts must be regular files without links.')
            if any(part == '.env' or part.startswith('.env.') for part in path.parts):
                raise KelmoError('E_MODEL_IMPORT', 'Environment files do not belong in model artifacts.')
            digest = hashlib.sha256()
            with os.fdopen(model_store.open_source(directory, path), 'rb') as source:
                while chunk := source.read(1048576):
                    digest.update(chunk)
            pins[path.relative_to(directory).as_posix()] = [info.st_size, digest.hexdigest()]
    model_store.check_layout(pins)
    return pins


def local_profile(model, directory, *, pooling=None, normalization=None, projection='none', dimension=None,
                  query_prefix='', document_prefix=''):
    from .embeddings import backend_model_description
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        raise KelmoError('E_MODEL_IMPORT', 'Select a provisioned local directory with --from.')
    result = {'provider': 'local', 'model': model, 'model_dir': str(directory.resolve()),
              'query_prefix': query_prefix, 'document_prefix': document_prefix}
    if model == model_store.MODEL:
        spec = {**model_store.PINNED_SPEC, 'projection': projection}
        spec.update({key: value for key, value in {'pooling': pooling, 'normalization': normalization,
                                                  'dimension': dimension}.items() if value is not None})
        try:
            model_store.record_model_pin(model, model_store.FILES, spec)
        except ValueError as error:
            raise KelmoError('E_INPUT', str(error)) from error
        model_store.verify_artifact(directory)
        return result
    description = backend_model_description(model)
    if description is None:
        raise KelmoError('E_INPUT', 'This backend does not register the model; use kelmo models list or a broker profile.')
    if pooling is None:
        path = directory / '1_Pooling/config.json'
        if path.is_file() and not path.is_symlink():
            with os.fdopen(model_store.open_source(directory, path), 'r') as source:
                declared = json.load(source)
            modes = [mode for key, mode in (('pooling_mode_mean_tokens', 'mean'),
                                             ('pooling_mode_cls_token', 'cls'), ('pooling_mode_max_tokens', 'max'))
                     if declared.get(key) is True]
            pooling = modes[0] if len(modes) == 1 else None
    if pooling is None or normalization is None:
        raise KelmoError('E_INPUT', 'This model needs --pooling and --normalization declarations, or a complete '
                                   '--profile. They cannot be inferred from its name.')
    result.update(files=pin_directory(directory), pooling=pooling, normalization=normalization,
                  projection=projection, dimension=dimension or description.get('dim'))
    return result


def use(root, profile, *, dry_run=False):
    from kelmo.store.errors import ArchiveEmbeddingError
    from .embeddings import provider_from_settings
    config = Config.load(root)
    settings = validate_embedding(profile, config.root)
    if settings is None:
        raise KelmoError('E_INPUT', 'An embedding profile must be a JSON object.')
    # Local artifacts are checked without inference or downloads. Broker channels are owner-managed
    # runtime resources and are deliberately not opened by configuration.
    if settings['provider'] == 'local':
        try:
            provider_from_settings(settings).describe()
        except ArchiveEmbeddingError as error:
            raise KelmoError('E_MODEL_IMPORT', str(error)) from error
    result = replace_table(root, 'embedding', profile, dry_run=dry_run)
    return dict(result, provider=settings['provider'], model=settings.get('model'),
                model_dir=settings.get('model_dir'), reindex_required=result['changed'],
                next_step='Restart the daemon, then run kelmo admin reembed for this vault.'
                          if result['changed'] else 'The selected embedding profile is unchanged.')


def status(root):
    config = Config.load(root)
    return {'configured': config.embedding is not None, 'embedding': config.embedding}
