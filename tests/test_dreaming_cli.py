import json
import plistlib
import sys
from pathlib import Path

import pytest

from kelmo.cli import call
from test_editor_scoping import build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_dreaming_preview_pause_and_quota_deadline_avoid_extra_provider_calls(world, tmp_path, capsys):
    script, counter, config = tmp_path / "executor.py", tmp_path / "calls", tmp_path / "executor.json"
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "assert json.load(sys.stdin)['task']=='dream'\n"
        "p=Path(sys.argv[1]); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))\n"
        "print(json.dumps({'outcome':'quota_wait','reset_at':time.time()+300}))\n")
    config.write_text(json.dumps({"argv": [sys.executable, str(script), str(counter)], "timeout_seconds": 5}))

    def invoke(*args):
        code = call(["--vault", str(world.root), "dream", *map(str, args), "--json"])
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        return output["result"]

    tables = lambda: list(world.service.journal.conn.execute("SELECT name FROM sqlite_master"))  # noqa: E731
    before = tables()
    assert invoke("status")["paused"]
    assert not invoke("configure", "--executor-config", config)["applied"]
    assert tables() == before
    assert invoke("configure", "--executor-config", config, "--apply")["paused"]
    assert invoke("run")["state"] == "paused"
    assert not counter.exists()
    invoke("resume")
    assert invoke("run")["state"] == "deferred"
    assert counter.read_text() == "1"
    assert invoke("run")["state"] == "deferred"
    assert counter.read_text() == "1"
    assert invoke("pause")["paused"]
    for platform in ("linux", "darwin"):
        directory = tmp_path / platform
        arguments = ("schedule", "--executable", sys.executable, "--directory", directory,
                     "--platform", platform, "--interval", "604800")
        assert not invoke(*arguments)["applied"] and not directory.exists()
        schedule = invoke(*arguments, "--apply")
        assert schedule["paused"] and not schedule["scheduler_started"]
        if platform == "darwin":
            value = plistlib.loads(Path(schedule["files"][0]).read_bytes())
            assert value["StartInterval"] == 604800 and value["RunAtLoad"] is False
        else:
            assert "OnUnitActiveSec=604800s" in Path(schedule["files"][1]).read_text()
