import hashlib
import os
import uuid

import pytest

from kelmo import model_store


@pytest.fixture
def model_fixture(tmp_path, monkeypatch):
    vault, source = tmp_path / 'vault', tmp_path / 'source'
    vault.mkdir()
    source.mkdir()
    (vault / 'kelmo.toml').write_text(
        'schema_version = 1\nwriter = "local"\nnode_label = "test"\nvault_id = "' + str(uuid.uuid4()) + '"\n')
    content = b'Synthetic model data'
    (source / 'model.onnx').write_bytes(content)
    monkeypatch.setattr(model_store, 'FILES', {
        'model.onnx': (len(content), hashlib.sha256(content).hexdigest())})
    return vault, source


def test_model_import_verifies_bytes_and_is_idempotent(model_fixture):
    vault, source = model_fixture
    imported = model_store.import_model(vault, source)
    assert imported['existing'] is False
    assert imported['model_dir'] == str(vault / '.cache/embeddings/model')
    assert model_store.import_model(vault, source)['existing'] is True
    assert (vault / '.cache/embeddings/model/model.onnx').read_bytes() == (source / 'model.onnx').read_bytes()


def test_changed_model_is_rejected_before_publication(model_fixture):
    vault, source = model_fixture
    (source / 'model.onnx').write_bytes(b'Unexpected model data')
    with pytest.raises(model_store.ModelImportError):
        model_store.import_model(vault, source)
    assert not (vault / '.cache/embeddings/model').exists()


def test_model_source_symlink_cannot_read_outside_selected_directory(model_fixture, tmp_path):
    vault, source = model_fixture
    outside = tmp_path / 'outside.bin'
    (source / 'model.onnx').rename(outside)
    (source / 'model.onnx').symlink_to(outside)
    with pytest.raises(model_store.ModelImportError):
        model_store.import_model(vault, source)
    assert not (vault / '.cache/embeddings/model').exists()


def test_fifo_is_rejected_without_waiting_for_a_writer(model_fixture):
    vault, source = model_fixture
    (source / 'model.onnx').unlink()
    os.mkfifo(source / 'model.onnx')
    with pytest.raises(model_store.ModelImportError):
        model_store.import_model(vault, source)
