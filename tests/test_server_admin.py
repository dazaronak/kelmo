import json

import pytest

from kelmo.errors import KelmoError
from kelmo import server_admin as admin
from kelmo.server import ServerProfile
from test_action_state import world_factory as world_factory  # noqa: F401


def test_enrollment_and_interrupted_writer_rotation_recover_forward(world_factory, tmp_path, monkeypatch):
    world = world_factory()
    path, receipt = tmp_path / "server.json", tmp_path / "rotation.json"
    assert not admin.initialize(world.root, path)["applied"]
    assert not path.exists()
    assert admin.initialize(world.root, path, apply=True)["fenced"]
    assert admin.enroll(world.root, path, "laptop", "machine", apply=True)["changed"]
    assert not admin.enroll(world.root, path, "laptop", "machine", apply=True)["changed"]
    assert ServerProfile.load(path).bindings.origin("laptop").machine_tag == "machine"
    original = admin.write

    def fail_profile(selected, data, **kwargs):
        if selected == path:
            raise KelmoError("E_IO", "Synthetic profile write failure.")
        return original(selected, data, **kwargs)

    monkeypatch.setattr(admin, "write", fail_profile)
    with pytest.raises(KelmoError, match="Synthetic"):
        admin.rotate(world.root, path, "server", 1, receipt, apply=True)
    assert receipt.exists()
    assert admin.inspect(world.root, path)["writer_matches"] is False
    with pytest.raises(KelmoError, match="Stale archive writer"):
        world.service.archive_api.assert_writer()
    monkeypatch.setattr(admin, "write", original)
    recovered = admin.rotate(world.root, path, "server", 1, receipt, apply=True)
    assert recovered["applied"] and recovered["backup"]
    assert admin.inspect(world.root, path)["writer_matches"] is True
    assert json.loads(path.read_bytes())["writer"] == {"writer_id": "server", "epoch": 2}
    assert admin.rotate(world.root, path, "server", 1, receipt, apply=True)["backup"] is None
