"""Regression edges for high descriptors and a removed-but-loaded legacy worker."""
import errno
import fcntl
import json
import os
import resource
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kelmo.client import StdioChannel
from kelmo.core import daemon_admin
from kelmo.embeddings import _Channel
from kelmo.errors import KelmoError
from kelmo.install import setup as installer
from kelmo.server import PipeReader, decode_frame, encode_frame
from kelmo.service import Service
from kelmo.store.errors import ArchiveEmbeddingError
from test_embedding_contract import FakeBroker


HIGH_FD = 1024


@pytest.fixture
def high_fd_headroom():
    original_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard <= HIGH_FD + 1:
        pytest.skip(f"inherited RLIMIT_NOFILE hard limit {hard} cannot allocate fd {HIGH_FD + 1}")
    target = 2048 if hard == resource.RLIM_INFINITY else min(2048, hard)
    if original_soft != resource.RLIM_INFINITY and original_soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError) as error:
            pytest.skip(f"cannot raise soft RLIMIT_NOFILE to {target} within inherited hard limit: {error}")
    try:
        yield
    finally:
        if original_soft != resource.RLIM_INFINITY:
            resource.setrlimit(resource.RLIMIT_NOFILE, (original_soft, hard))


def duplicate_high(fd):
    try:
        return fcntl.fcntl(fd, fcntl.F_DUPFD, HIGH_FD)
    except OSError as error:
        if error.errno == errno.EMFILE:
            pytest.skip(f"inherited RLIMIT_NOFILE soft limit cannot allocate an fd above {HIGH_FD - 1}")
        raise


def test_broker_channel_roundtrips_with_high_request_and_answer_fds(high_fd_headroom):
    broker = FakeBroker(lambda request: [{"id": request["id"], "ok": True}])
    request_fd = answer_fd = None
    try:
        request_fd = duplicate_high(broker.fds[0])
        answer_fd = duplicate_high(broker.fds[1])
        channel = _Channel(request_fd, answer_fd)
        assert channel.call("probe", [], timeout=1)["ok"] is True
    finally:
        for fd in (request_fd, answer_fd):
            if fd is not None:
                os.close(fd)
        broker.close()


def test_broker_channel_reports_eof_with_high_fds(high_fd_headroom):
    request_read, request_write = os.pipe()
    answer_read, answer_write = os.pipe()
    request_fd = answer_fd = None
    try:
        request_fd = duplicate_high(request_write)
        answer_fd = duplicate_high(answer_read)
        os.close(answer_write)
        channel = _Channel(request_fd, answer_fd)
        with pytest.raises(ArchiveEmbeddingError) as refused:
            channel.call("probe", [], timeout=1)
        assert refused.value.data["reason"] == "embedding_unavailable"
    finally:
        for fd in (request_read, request_write, answer_read, answer_write, request_fd, answer_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_stdio_channel_roundtrips_with_high_request_and_answer_fds(high_fd_headroom):
    request_read, request_write = os.pipe()
    answer_read, answer_write = os.pipe()
    high_request = high_answer = None
    channel = None
    peer = None
    try:
        high_request = duplicate_high(request_write)
        high_answer = duplicate_high(answer_read)
        request_stream = os.fdopen(high_request, "wb", buffering=0)
        answer_stream = os.fdopen(high_answer, "rb", buffering=0)
        os.set_blocking(request_stream.fileno(), False)

        def serve_one():
            with os.fdopen(request_read, "rb") as requests:
                request = decode_frame(requests.readline())
                reply = {"v": 2, "kind": "reply", "id": request["id"], "ok": True}
                os.write(answer_write, encode_frame(reply))

        peer = threading.Thread(target=serve_one, daemon=True)
        peer.start()
        process = SimpleNamespace(stdin=request_stream, stdout=answer_stream, poll=lambda: 0)
        channel = StdioChannel.__new__(StdioChannel)
        channel.timeout = 1
        channel.grace = 0
        channel.process = process
        channel.reader = PipeReader(answer_stream)

        request = {"v": 2, "kind": "call", "id": "high-fd", "op": "probe",
                   "body_type": "json", "deadline_ms": 1000, "body": {}}
        assert channel.request(request)["id"] == "high-fd"
    finally:
        if channel is not None:
            channel.close()
        for fd in (request_read, request_write, answer_read, answer_write, high_request, high_answer):
            try:
                os.close(fd)
            except OSError:
                pass
        if peer is not None:
            peer.join(timeout=1)


class ExecAttempt(Exception):
    pass


def daemon_run_fixture(tmp_path, monkeypatch, platform):
    root = tmp_path / "vault"
    Service.initialize(root, "synthetic", project="upgrade-test")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(installer.sys, "platform", platform)
    options = {
        "role": "vault",
        "port": 47821,
        "runtime": str(tmp_path / "runtime"),
        "kelmod": str(tmp_path / "bin" / "kelmod"),
        "launcher": str(tmp_path / "bin" / "kelmo"),
        "worker_config": None,
        "server_profile": None,
    }
    daemon_admin.write_options(root / ".state", options)
    legacy_file = (home / "Library/LaunchAgents/org.kelmo.worker.plist" if platform == "darwin"
                   else home / ".config/systemd/user/kelmo.service")
    assert not legacy_file.exists()
    return root, options


def manager_result(platform, *, loaded):
    def run(command, **kwargs):
        if platform == "darwin":
            assert command[:2] == ["launchctl", "print"]
            assert command[-1].endswith("/org.kelmo.worker")
            if loaded:
                return subprocess.CompletedProcess(command, 0, b"loaded", b"")
            return subprocess.CompletedProcess(
                command, 113, b"", b'Could not find service "org.kelmo.worker" in domain')
        if platform == "linux":
            assert command == ["systemctl", "--user", "show", "--property=LoadState", "--value",
                               "kelmo.service"]
            state = b"loaded\n" if loaded else b"not-found\n"
            return subprocess.CompletedProcess(command, 0, state, b"")
        raise AssertionError(f"unexpected manager command: {command}")
    return run


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_run_refuses_manager_loaded_legacy_worker_when_its_file_is_gone(tmp_path, monkeypatch, platform):
    root, _ = daemon_run_fixture(tmp_path, monkeypatch, platform)
    manager_calls = []
    exec_calls = []
    run_manager = manager_result(platform, loaded=True)

    def manager(command, **kwargs):
        manager_calls.append(command)
        return run_manager(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", manager)

    def execv(*args):
        exec_calls.append(args)
        raise ExecAttempt

    monkeypatch.setattr(daemon_admin.os, "execv", execv)

    with pytest.raises(KelmoError):
        daemon_admin.run(root)

    assert manager_calls
    assert exec_calls == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_run_proceeds_when_manager_confirms_legacy_worker_absent(tmp_path, monkeypatch, platform):
    root, options = daemon_run_fixture(tmp_path, monkeypatch, platform)
    manager_calls = []
    exec_calls = []
    run_manager = manager_result(platform, loaded=False)

    def manager(command, **kwargs):
        manager_calls.append(command)
        return run_manager(command, **kwargs)

    def execv(path, argv):
        exec_calls.append((path, argv))
        raise ExecAttempt

    monkeypatch.setattr(subprocess, "run", manager)
    monkeypatch.setattr(daemon_admin.os, "execv", execv)

    with pytest.raises(ExecAttempt):
        daemon_admin.run(root)

    assert manager_calls
    assert exec_calls == [(options["kelmod"], [options["kelmod"], "serve", "--spec",
                                                  str(root / ".state" / "kelmod.spec.json")])]
    assert json.loads((root / ".state" / "kelmod.spec.json").read_text())["root"] == str(root.resolve())
