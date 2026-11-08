import json
import sys
from pathlib import Path

import pytest

from kelmo.interface import build_parser
from kelmo.install.setup import setup
from kelmo.server_admin import initialize
from platform_support import UNSUPPORTED


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_confined_server_setup_refuses_without_a_sandbox_launcher(tmp_path, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.service import Service

    vault = tmp_path / "vault"
    Service.initialize(vault, "synthetic")
    profile = tmp_path / "server.json"
    initialize(vault, profile, apply=True)
    assert json.loads(profile.read_bytes()).get("archive_process", "confined") == "confined"
    monkeypatch.setattr("kelmo.cli.dispatch", lambda *a, **k: {
        "healthy": True, "checks": {"archive_integrity": True},
        "runtime_isolation": {"launcher_available": False}})
    parser, _ = build_parser("")
    args = parser.parse_args(["setup", "--vault", str(vault), "--home", str(tmp_path / "home"),
                              "--executable", sys.executable, "--client", "none", "--skip-model",
                              "--no-start", "--allow-native-memory", "--server-profile", str(profile)])
    with pytest.raises(KelmoError) as raised:
        setup(args)
    assert raised.value.code == "E_SETUP" and "sandbox launcher" in str(raised.value)


@pytest.mark.skipif(UNSUPPORTED is not None, reason=UNSUPPORTED or '')
def test_prepare_server_without_clients_or_model_download(tmp_path, monkeypatch):
    monkeypatch.setattr("kelmo.linux_runtime.launcher", lambda: Path(sys.executable))
    monkeypatch.setattr("kelmo.install.setup.fetch_model",
                        lambda *args: (_ for _ in ()).throw(AssertionError("download")))
    parser, _ = build_parser("")
    args = parser.parse_args(["setup", "--vault", str(tmp_path / "vault"), "--home", str(tmp_path / "home"),
                              "--executable", sys.executable, "--client", "none", "--skip-model",
                              "--no-start", "--allow-native-memory", "--project", "example",
                              "--git-name", "Synthetic", "--git-email", "synthetic@example.invalid"])
    args.dry_run = True
    assert setup(args)["model"]["action"] == "skip"
    assert list(tmp_path.iterdir()) == []
    args.dry_run = False
    result = setup(args)
    assert result["clients"] == [] and result["memory_ready"]
    assert result["search_degraded"] and not result["background_started"]
    assert not (tmp_path / "home/.claude").exists()
    assert not (tmp_path / "home/.codex").exists()
    args.server_profile = tmp_path / "server.json"
    initialize(args.vault, args.server_profile, apply=True)
    assert setup(args)["clients"] == []
    from kelmo.install.setup import service_definition
    for platform in ("linux", "darwin"):
        _, contents = service_definition(args.home, args.executable, args.vault, platform,
                                          None, args.server_profile)
        assert b"--server-profile" in contents and str(args.server_profile).encode() in contents
        # A stop waits for the model call in flight instead of killing it.
        assert (b"KillMode=mixed\nTimeoutStopSec=660" if platform == "linux"
                else b"<key>ExitTimeOut</key>\n\t<integer>660</integer>") in contents
    assert json.loads(args.server_profile.read_bytes())["fenced"]
    args.provider = "custom"
    args.provider_executable = Path(sys.executable)
    probes = []
    monkeypatch.setattr("kelmo.daemon.execute",
                        lambda argv, *a, **kw: probes.append(argv) or {"ok": True})
    assert setup(args)["executor_ready"]
    assert len(probes) == 1
    argv = probes[0]
    assert argv[argv.index("--provider") + 1] == "custom" and "--model" not in argv
    assert json.loads(argv[argv.index("--command-json") + 1]) == [sys.executable]
