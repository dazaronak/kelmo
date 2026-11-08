import sys
import pytest

from kelmo.ssh_gate import command


def test_gate_pins_identity_and_rejects_caller_arguments():
    args = (sys.executable, "/srv/memory", "/srv/profile.json", "laptop")
    allowed = command(*args, "kelmo-client")
    assert allowed[-2:] == ["--client-id", "laptop"]
    assert allowed[1:4] == ["--vault", "/srv/memory", "serve-client"]
    for request in ("", "kelmo-client administrator", "kelmo-client; id"):
        with pytest.raises(ValueError, match="Only"):
            command(*args, request)
