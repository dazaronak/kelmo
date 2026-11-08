import uuid
from pathlib import Path

import pytest

from test_embedding_contract import fake_registry, pinned_model as pinned_model  # noqa: F401
from kelmo.config import Config
from kelmo.model_selection import local_profile, use


def test_selecting_local_model_preserves_other_settings_and_backs_up(tmp_path, pinned_model, monkeypatch):
    monkeypatch.setattr('kelmo.embeddings.backend_model_description', fake_registry())
    root = tmp_path / 'vault'
    root.mkdir()
    path = root / 'kelmo.toml'
    before = ('schema_version = 1\nwriter = "local"\nnode_label = "fixture"\nvault_id = "'
              + str(uuid.uuid4()) + '"\n\n[hot]\npages = ["person.md"]\n').encode()
    path.write_bytes(before)
    profile = local_profile('synthetic/model', pinned_model[0], pooling='mean', normalization='l2')
    preview = use(root, profile, dry_run=True)
    assert preview['changed'] and not preview['applied'] and path.read_bytes() == before
    applied = use(root, profile)
    assert applied['applied'] and Path(applied['backup']).read_bytes() == before
    config = Config.load(root)
    assert config.embedding['model'] == 'synthetic/model'
    assert config.hot['pages'] == ['person.md']
    assert not use(root, profile)['changed']


def test_changed_model_is_refused_without_replacing_config(tmp_path, pinned_model, monkeypatch):
    from kelmo.errors import KelmoError
    monkeypatch.setattr('kelmo.embeddings.backend_model_description', fake_registry())
    root = tmp_path / 'vault'
    root.mkdir()
    path = root / 'kelmo.toml'
    before = 'schema_version = 1\nwriter = "local"\nnode_label = "fixture"\nvault_id = "' + str(uuid.uuid4()) + '"\n'
    path.write_text(before)
    profile = local_profile('synthetic/model', pinned_model[0], pooling='mean', normalization='l2')
    (pinned_model[0] / 'model.onnx').write_bytes(b'replaced weights')
    with pytest.raises(KelmoError) as refused:
        use(root, profile)
    assert refused.value.code == 'E_MODEL_IMPORT'
    assert path.read_text() == before
