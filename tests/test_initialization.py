import json
import subprocess

from kelmo.cli import call
from kelmo.config import Config
from kelmo import server_admin


def test_interrupted_initialization_leaves_no_partial_vault(tmp_path, monkeypatch):
    import pytest
    from kelmo.initialization import initialize
    from kelmo.wiki import engine
    root = tmp_path / "vault"
    original = engine.initialize_registry
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("power loss after publication")
    monkeypatch.setattr(engine, "initialize_registry", interrupted)
    with pytest.raises(RuntimeError, match="power loss"):
        initialize(root, "synthetic", project="example",
                   git_name="Synthetic", git_email="synthetic@example.invalid")
    # The vault is built in a private staging directory and appears only when complete.
    assert not root.exists()
    monkeypatch.setattr(engine, "initialize_registry", original)
    result = initialize(root, "synthetic", project="example",
                        git_name="Synthetic", git_email="synthetic@example.invalid")
    assert not result["existing"] and result["registry"] == "current"
    assert engine.load_registry(root)["pages"]["projects/example/example.md"]["layout"] == "project-map"


def test_new_cli_vault_can_create_and_reinitialize_after_writer_rotation(tmp_path, capsys):
    root = tmp_path / "vault"
    arguments = ["init", "--vault", str(root), "--label", "synthetic", "--project", "example",
                 "--git-name", "Synthetic", "--git-email", "synthetic@example.invalid", "--json"]
    assert call(arguments) == 0
    initialized = json.loads(capsys.readouterr().out)["result"]
    assert initialized["registry"] == "current"
    config = Config.load(root)
    assert json.loads(config.registry.read_bytes())["version"] == 3
    assert subprocess.check_output(["git", "-C", str(root), "config", "--local",
                                    "--get", "user.email"], text=True).strip() == "synthetic@example.invalid"
    assert call(["create", "--vault", str(root), "--project", "example",
                 "--text", "First project decision.", "--json"]) == 0
    captured = json.loads(capsys.readouterr().out)["result"]
    assert captured["operation"]["state"] == "queued"
    profile, receipt = tmp_path / "server.json", tmp_path / "rotation.json"
    server_admin.initialize(root, profile, apply=True)
    server_admin.rotate(root, profile, "primary", 1, receipt, apply=True)
    assert call(arguments) == 0
    assert json.loads(capsys.readouterr().out)["result"]["existing"]


def test_doctor_uses_the_selected_embedding_provider(tmp_path, monkeypatch):
    from dataclasses import replace
    from kelmo.doctor import diagnose
    from kelmo.initialization import initialize
    from types import SimpleNamespace
    root = tmp_path / "vault"
    initialize(root, "synthetic", git_name="Synthetic", git_email="synthetic@example.invalid")
    config = replace(Config.load(root), embedding={"provider": "broker", "synthetic": True})
    monkeypatch.setattr(Config, "load", lambda root: config)
    calls = []

    def embed(texts):
        calls.append(texts)
        return SimpleNamespace(validate_count=lambda count: calls.append(count))

    monkeypatch.setattr("kelmo.embeddings.provider_from_settings",
                        lambda settings: SimpleNamespace(embed_queries=embed))
    result = diagnose(root)
    assert result["checks"]["embedding_model"] is True
    assert calls == [["Synthetic model health check"], 1]


def test_doctor_outside_the_broker_names_the_missing_channel(tmp_path, monkeypatch):
    from dataclasses import replace
    from kelmo.doctor import diagnose
    from kelmo.initialization import initialize
    root = tmp_path / "vault"
    initialize(root, "synthetic", git_name="Synthetic", git_email="synthetic@example.invalid")
    config = replace(Config.load(root), embedding={"provider": "broker", "fds_env": "SYNTHETIC_EMBED_FDS"})
    monkeypatch.setattr(Config, "load", lambda root: config)
    monkeypatch.delenv("SYNTHETIC_EMBED_FDS", raising=False)

    def unreachable(settings):
        raise RuntimeError("no channel")

    monkeypatch.setattr("kelmo.embeddings.provider_from_settings", unreachable)
    result = diagnose(root)
    assert result["checks"]["embedding_model"] is False
    assert any(warning.startswith("SYNTHETIC_EMBED_FDS is unset: doctor ran outside the embedding broker")
               for warning in result["warnings"])
