"""Import only the pinned model's data files, without executing package code."""
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

if __package__:
    from .config import Config
    from .errors import KelmoError
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config
    from errors import KelmoError

MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
SOURCE_REPO = 'qdrant/all-MiniLM-L6-v2-onnx'
REVISION = '5f1b8cd78bc4fb444dd171e59b18f3a3af89a079'
FILES = {
    'config.json': (650, '1b4d8e2a3988377ed8b519a31d8d31025a25f1c5f8606998e8014111438efcd7'),
    'model.onnx': (90387630, 'bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5'),
    'special_tokens_map.json': (695, '5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a'),
    'tokenizer.json': (711661, 'da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0'),
    'tokenizer_config.json': (1433, 'bd2e06a5b20fd1b13ca988bedc8763d332d242381b4fbc98f8fead4524158f79'),
}
# How fastembed's registry pools, projects and normalizes the pinned model; part of its embedding space.
PINNED_SPEC = {'pooling': 'mean', 'projection': 'none', 'normalization': 'l2', 'dimension': 384}


class ModelImportError(Exception):
    pass


def record_model_pin(model, files, spec):
    """Whether (model, files) is the pinned record model, decided in one place for configuration and for a
    provider built directly. Its pinned bytes fix the space, so a declared pooling, projection,
    normalization or dimension that differs from PINNED_SPEC is refused (ValueError), never described."""
    if model != MODEL or {name: tuple(pin) for name, pin in dict(files).items()} != FILES:
        return False
    wrong = [key for key, value in PINNED_SPEC.items()
             if type(spec.get(key)) is not type(value) or spec.get(key) != value]
    if wrong:
        raise ValueError('the pinned model fixes ' + ', '.join(f'{key} = {PINNED_SPEC[key]!r}' for key in wrong))
    return True


def open_source(root, path):
    relative = path.resolve().relative_to(root.resolve())
    current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
    finally:
        os.close(current)


def verify_file(path, expected, *, root=None, output=None):
    size, checksum = expected
    digest = hashlib.sha256()
    fd = open_source(root or path.parent, path)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != size:
            raise ModelImportError('Model file is missing, linked, or has an unexpected size.')
        total = 0
        while chunk := stream.read(min(1048576, size + 1 - total)):
            total += len(chunk)
            if total > size:
                raise ModelImportError('Model file changed during verification.')
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
    if digest.hexdigest() != checksum:
        raise ModelImportError('Model checksum does not match the pinned artifact.')


def artifact_identity(files):
    """sha256 over the pinned (name, size, sha256) table: the identity of a model artifact, not its name."""
    table = {name: [size, checksum] for name, (size, checksum) in sorted(files.items())}
    return 'sha256:' + hashlib.sha256(json.dumps(table, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_artifact(model_dir, files=None):
    """Hash every pinned file of a provisioned model directory and return its identities.

    Raises ModelImportError for a missing, linked, resized or changed file; nothing is downloaded.
    The tokenizer identity is the pinned hash of tokenizer.json, so it equals TokenWindow's identity
    for the same bytes."""
    files = FILES if files is None else files
    model_dir = Path(model_dir).expanduser().resolve()
    if 'tokenizer.json' not in files:
        raise ModelImportError('A pinned model needs tokenizer.json among its files.')
    for name, expected in files.items():
        verify_file(model_dir / name, expected, root=model_dir)
    return {'artifact_identity': artifact_identity(files),
            'tokenizer_identity': 'sha256:' + files['tokenizer.json'][1]}


# What fastembed's loader reads besides the graph (load_tokenizer requires each of these).
CUSTOM_REQUIRED = ('config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json')


def check_layout(files):
    """Refuse a custom model's pin table that cannot name what the loader reads: every name a relative
    path inside the model directory, the four configuration and tokenizer files at its top, and exactly
    one ONNX graph (with several, which one the backend loads is not proven). External-data and other
    sidecar files are pinned like any other file; verify_complete_artifact refuses one that is not."""
    for name in files:
        parts = name.split('/') if isinstance(name, str) else []
        if not parts or name.startswith('/') or '\\' in name or any(part in ('', '.', '..') for part in parts):
            raise ModelImportError(f'{name!r} is not a relative path inside the model directory.')
    missing = [name for name in CUSTOM_REQUIRED if name not in files]
    if missing:
        raise ModelImportError('The pin table lacks ' + ', '.join(missing) + '.')
    graphs = sorted(name for name in files if name.endswith('.onnx'))
    if len(graphs) != 1:
        raise ModelImportError(f'Exactly one .onnx graph must be pinned; found {len(graphs)}.')


def verify_complete_artifact(model_dir, files):
    """verify_artifact for a model whose loader may read any file of its directory: every entry under
    model_dir must be a directory or a regular file (a link or any other type is refused), and every
    regular file must be pinned, so nothing the backend reads escapes the artifact identity."""
    model_dir = Path(model_dir).expanduser()
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise ModelImportError('The model directory is missing or a link.')
    root = model_dir.resolve()
    found = set()
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                continue
            relative = path.relative_to(root).as_posix()
            if not stat.S_ISREG(mode):
                raise ModelImportError(f'{relative} is a link or not a regular file.')
            found.add(relative)
    unpinned = sorted(found - set(files))
    if unpinned:
        raise ModelImportError('Files the loader may read are not pinned: ' + ', '.join(unpinned) + '.')
    return verify_artifact(model_dir, files)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def import_model(vault, source):
    vault, source = Path(vault).resolve(), Path(source).expanduser().resolve()
    config = Config.load(vault)
    cache = config.internal('.cache/embeddings')
    artifact = source if (source / 'model.onnx').exists() else source / ('models--qdrant--all-MiniLM-L6-v2-onnx/snapshots/' + REVISION)
    inputs = {}
    for name, expected in FILES.items():
        path = (artifact / name).resolve()
        if not path.is_relative_to(source):
            raise ModelImportError('A model source escapes the selected directory.')
        verify_file(path, expected, root=source)
        inputs[name] = path
    target = cache / 'model'
    if target.is_symlink():
        raise ModelImportError('Existing model storage is not a regular directory.')
    if target.exists():
        for name, expected in FILES.items():
            if (target / name).is_symlink():
                raise ModelImportError('Existing model files cannot be symlinks.')
            verify_file(target / name, expected)
            with (target / name).open('rb') as existing:
                os.fsync(existing.fileno())
        sync_directory(target)
        sync_directory(cache)
        return {'model': MODEL, 'model_dir': str(target), 'source_repo': SOURCE_REPO, 'revision': REVISION,
                'existing': True, 'verified_files': len(FILES)}
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    sync_directory(vault)
    sync_directory(vault / '.cache')
    sync_directory(cache)
    with tempfile.TemporaryDirectory(prefix='.model-import-', dir=cache) as temporary:
        stage = Path(temporary)
        for name, original in inputs.items():
            destination = stage / name
            with destination.open('xb') as writer:
                verify_file(original, FILES[name], root=source, output=writer)
                writer.flush()
                os.fsync(writer.fileno())
            verify_file(destination, FILES[name])
        (stage / 'manifest.json').write_text(json.dumps({'model': MODEL, 'source_repo': SOURCE_REPO, 'revision': REVISION, 'files': FILES}, indent=2))
        with (stage / 'manifest.json').open('rb') as manifest:
            os.fsync(manifest.fileno())
        sync_directory(stage)
        stage.rename(target)
        sync_directory(cache)
    return {'model': MODEL, 'model_dir': str(target), 'source_repo': SOURCE_REPO, 'revision': REVISION,
            'existing': False, 'verified_files': len(FILES)}


def source_paths(source):
    source = Path(source).expanduser().resolve()
    artifact = source if (source / 'model.onnx').exists() else source / ('models--qdrant--all-MiniLM-L6-v2-onnx/snapshots/' + REVISION)
    files, directories = [], {source}
    for name in FILES:
        path = (artifact / name).resolve()
        if not path.is_relative_to(source):
            raise ModelImportError('A model source escapes the selected directory.')
        files.append(path)
        parent = path.parent
        while parent.is_relative_to(source):
            directories.add(parent)
            if parent == source:
                break
            parent = parent.parent
    return files, directories


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = import_model(args.vault, args.source)
    except (KelmoError, ModelImportError, OSError, ValueError, RuntimeError) as error:
        code = error.code if isinstance(error, KelmoError) else 'E_MODEL_IMPORT'
        message = str(error) if isinstance(error, (KelmoError, ModelImportError)) else 'Model files could not be imported or verified.'
        if args.json:
            print(json.dumps({'schema_version': 1, 'ok': False, 'error': {'code': code, 'message': message}}))
        else:
            print(code + ': ' + message, file=sys.stderr)
        return 1
    print(json.dumps({'schema_version': 1, 'ok': True, 'result': result}) if args.json
          else f"{result['model']}: {result['verified_files']} verified files")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
