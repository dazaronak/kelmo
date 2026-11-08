import subprocess

import pytest

from kelmo.errors import KelmoError
from kelmo.initialization import initialize
from kelmo.knowledge_sync import synchronize


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def commit(path, name, content):
    (path / name).write_text(content)
    git(path, "add", "--", name)
    git(path, "commit", "-qm", "Synthetic change")


def clone(remote, destination):
    subprocess.run(["git", "clone", "-q", str(remote), str(destination)], check=True, capture_output=True)
    git(destination, "config", "user.name", "Synthetic")
    git(destination, "config", "user.email", "synthetic@example.invalid")


def test_sync_merges_and_pushes_local_fixture_and_refuses_conflict_before_checkout_change(tmp_path):
    root, bare, peer = tmp_path / "vault", tmp_path / "remote.git", tmp_path / "peer"
    initialize(root, "synthetic", git_name="Synthetic", git_email="synthetic@example.invalid")
    subprocess.run(["git", "clone", "--bare", "-q", str(root), str(bare)], check=True, capture_output=True)
    git(root, "remote", "add", "origin", str(bare))
    clone(bare, peer)
    commit(root, "local.txt", "local\n")
    commit(peer, "remote.txt", "remote\n")
    git(bare, "fetch", "-q", str(peer), "master:master")
    before = git(root, "rev-parse", "HEAD")
    assert not synchronize(root, "origin", "master")["applied"]
    assert git(root, "rev-parse", "HEAD") == before
    result = synchronize(root, "origin", "master", apply=True, push=True)
    assert result["pushed"] and result["ahead"] == 2
    assert (root / "remote.txt").read_text() == "remote\n"
    assert git(root, "rev-parse", result["backup_ref"]) == before
    assert git(bare, "rev-parse", "master") == result["after"]
    conflict = tmp_path / "conflict-peer"
    clone(bare, conflict)
    commit(conflict, "local.txt", "remote conflict\n")
    git(bare, "fetch", "-q", str(conflict), "master:master")
    commit(root, "local.txt", "local conflict\n")
    before = git(root, "rev-parse", "HEAD")
    with pytest.raises(KelmoError, match="branches conflict"):
        synchronize(root, "origin", "master", apply=True)
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "status", "--porcelain") == ""
