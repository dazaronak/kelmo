import json

from kelmo.cli import call
from kelmo.config import Config
from kelmo import server_admin

pytest_plugins = ["test_fact_checks"]


def test_unreadable_snapshot_database_names_wal_and_dump_recovery(tmp_path, capsys):
    database, spec = tmp_path / "archive.db", tmp_path / "sources.json"
    database.write_bytes(b"not a SQLite database")
    spec.write_text(json.dumps({"archive": {"kind": "sqlite", "path": str(database)}}))
    destination = tmp_path / "snapshot"
    assert call(["snapshot", "create", "--spec", str(spec), "--destination", str(destination),
                 "--writers-stopped", "--apply", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "E_SNAPSHOT" and "WAL/SHM" in error["message"]
    assert "owning account" in error["message"] and not (destination / "manifest.json").exists()


def test_import_refuses_relative_paths_that_leave_the_knowledge_root(tmp_path, capsys):
    roots = {name: str(tmp_path / "imported" / name) for name in ("state_root", "archive_root", "knowledge_git_root")}
    for key, value in (("wiki_relative", "../other-repo/wiki"), ("registry_relative", "/etc/registry.json")):
        spec = tmp_path / (key + ".json")
        spec.write_text(json.dumps({"archive": "archive", "vault": "vault", "roots": roots, key: value}))
        assert call(["--vault", str(tmp_path / "target"), "snapshot", "import", "--snapshot",
                     str(tmp_path / "snapshot"), "--spec", str(spec), "--json"]) == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "E_INPUT" and key in error["message"]
    assert not (tmp_path / "imported").exists()


def test_public_snapshot_import_preserves_rotated_writer(service, tmp_path, capsys):
    def invoke(*argv):
        code = call([*map(str, argv), "--json"])
        answer = json.loads(capsys.readouterr().out)
        assert code == 0, answer
        return answer["result"]

    root = service.root
    profile, receipt = tmp_path / "server.json", tmp_path / "writer.json"
    server_admin.initialize(root, profile, apply=True)
    server_admin.rotate(root, profile, "primary", 1, receipt, apply=True)
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({
        "archive": {"kind": "sqlite", "path": str(service.config.archive)},
        "vault": {"kind": "git", "path": str(root)},
        "journal": {"kind": "sqlite", "path": str(service.config.state / "journal.sqlite3")}}))
    snapshot = tmp_path / "snapshot"
    assert not invoke("snapshot", "create", "--spec", sources, "--destination", snapshot)["applied"]
    assert not snapshot.exists()
    invoke("snapshot", "create", "--spec", sources, "--destination", snapshot,
           "--writers-stopped", "--apply")
    assert invoke("snapshot", "verify", "--snapshot", snapshot)["verified"]
    assert snapshot.stat().st_mode & 0o077 == 0
    assert (snapshot / "archive").stat().st_mode & 0o077 == 0
    target, spec = tmp_path / "imported-control", tmp_path / "import.json"
    roots = {name: str(tmp_path / "imported" / name)
             for name in ("state_root", "archive_root", "knowledge_git_root")}
    spec.write_text(json.dumps({"archive": "archive", "vault": "vault",
                               "journal": "journal", "roots": roots}))
    assert not invoke("--vault", target, "snapshot", "import", "--snapshot", snapshot,
                      "--spec", spec)["applied"]
    assert not target.exists()
    result = invoke("--vault", target, "snapshot", "import", "--snapshot", snapshot,
                    "--spec", spec, "--apply")
    assert "verify" in result["steps"]
    assert Config.load(target).archive.stat().st_mode & 0o077 == 0
    assert Config.load(target).knowledge_git_root.stat().st_mode & 0o077 == 0
    assert server_admin.archive_writer(Config.load(target)) == {"writer_id": "primary", "epoch": 2}
    invoke("--vault", target, "snapshot", "mark-cutover", "--apply")
    assert invoke("--vault", target, "snapshot", "rollback-check")["empty"]
    report = tmp_path / "deployment-report"
    command = ("--vault", target, "snapshot", "report", "--destination", report, "--include", profile)
    assert not invoke(*command)["applied"] and not report.exists()
    assert invoke(*command, "--apply")["files"] == 3
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert manifest["consistent_snapshot"] is False and len(manifest["files"]) == 3
    assert report.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in report.iterdir())
    changed_root = tmp_path / "different-archive"
    roots["archive_root"] = str(changed_root)
    spec.write_text(json.dumps({"archive": "archive", "vault": "vault",
                               "journal": "journal", "roots": roots}))
    assert call(["--vault", str(target), "snapshot", "import", "--snapshot", str(snapshot),
                 "--spec", str(spec), "--apply", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "E_IMPORT"
    assert not changed_root.exists()
