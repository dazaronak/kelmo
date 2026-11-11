"""Fault cases for real kelmod cores and the durable client outbox.

Every vault and record is synthetic. The archive core runs as a normal Python core because the test
environment cannot nest its confinement boundary.
"""
import errno
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from daemon_harness import KELMOD, SRC, Daemon, kill_group, wait_for
from kelmo.config import Config
from kelmo.core.spec import build


IDENTITY = {
    "GIT_AUTHOR_NAME": "Synthetic Fixture",
    "GIT_COMMITTER_NAME": "Synthetic Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}
RECORD_PREFIX = "Synthetic daemon fault-matrix record"


def _start(root, workdir, *, embed_core=False):
    """Start kelmod with real state and in-process archive cores, optionally with the real embed core."""
    if not KELMOD.is_file():
        pytest.fail(f"kelmod is not built at {KELMOD}")
    runtime = Path(tempfile.mkdtemp(prefix="kf", dir="/tmp"))
    scratch = workdir / "scratch"
    scratch.mkdir(exist_ok=True)
    spec_path = workdir / "spec.json"
    profile = workdir / "server.json"
    profile.write_text(json.dumps({
        "schema": "kelmo-server/1", "authority_id": Config.load(root).vault_id,
        "writer": {"writer_id": "local", "epoch": 1}, "archive_process": "in-process",
        "clients": [{"client_id": "local", "machine_tag": "synthetic-machine"}],
    }))
    spec = build(root, server_profile=profile, runtime_dir=runtime, source_root=SRC, archive_core=True,
                 archive_in_process=True, embed_core=embed_core)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    log = (workdir / "kelmod.log").open("a", encoding="utf-8")
    proc = subprocess.Popen([str(KELMOD), "serve", "--spec", str(spec_path)],
                            stdout=log, stderr=log)
    daemon = Daemon(proc, spec_path, runtime, scratch, log)
    try:
        expected = {"state", "archive"} | ({"embed"} if embed_core else set())

        def expected_cores_ready():
            status = daemon.status()
            if status is None:
                return False
            cores = {core["name"]: core for core in status["cores"]}
            return expected <= cores.keys() and all(cores[name]["ready"] for name in expected)

        wait_for(expected_cores_ready, timeout=120, what="real Python cores to become ready")
    except BaseException:
        daemon.stop()
        raise
    return daemon


def _restart(daemon):
    """Reopen the same kelmod runtime and root after the daemon process is gone."""
    log = (daemon.spec_path.parent / "kelmod.log").open("a", encoding="utf-8")
    proc = subprocess.Popen([str(KELMOD), "serve", "--spec", str(daemon.spec_path)],
                            stdout=log, stderr=log)
    daemon.proc, daemon.log = proc, log
    wait_for(daemon.all_ready, timeout=120, what="real Python cores after kelmod restart")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fresh initialized vault and real kelmod instance, cleaned up even after injected exits."""
    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)
    from kelmo.service import Service

    root = tmp_path / "vault"
    Service.initialize(root, "synthetic-fault-matrix")
    daemon = _start(root, tmp_path)
    daemon._fault_matrix_pids = set()
    _remember_core_pids(daemon, daemon._fault_matrix_pids)
    try:
        yield daemon
    finally:
        _remember_core_pids(daemon, daemon._fault_matrix_pids)
        daemon.stop()
        for pid in daemon._fault_matrix_pids:
            kill_group(pid)


def _remember_core_pids(daemon, pids):
    """Adds the pid of every core the daemon reports, so a test can end what an injected death left behind."""
    status = daemon.status()
    if status is not None:
        pids.update(core["pid"] for core in status["cores"] if core.get("pid") is not None)


def _call(client, operation, body=None, *, key=None, timeout=60):
    call_id = client.call(operation, body or {}, key=key, deadline_ms=timeout * 1000)
    return client.reply(call_id, timeout=timeout)


def _lane_in_flight(daemon, core_name, lane):
    core = daemon.core(core_name)
    return core is not None and core["lanes"][lane]["in_flight"] > 0


def _kill_during_call(daemon, client, core_name, lane, operation, body, *, key=None):
    before = daemon.core(core_name)
    assert before is not None and before["ready"]
    daemon._fault_matrix_pids.add(before["pid"])
    os.kill(before["pid"], signal.SIGSTOP)
    try:
        call_id = client.call(operation, body, key=key, deadline_ms=120000)
        wait_for(lambda: _lane_in_flight(daemon, core_name, lane), timeout=20,
                 what=f"{core_name}.{lane} to report the call in flight")
        kill_group(before["pid"])
        wait_for(lambda: (lambda current: current is not None and current["ready"]
                          and current["pid"] != before["pid"])(daemon.core(core_name)),
                 timeout=120, what=f"{core_name} to restart")
        return client.reply(call_id, timeout=120)
    finally:
        kill_group(before["pid"])


def _assert_replay_outcome(reply, code="E_CORE_RESTARTED"):
    """The call was replayed and answered, or refused as retryable with the code of the core that died: the
    daemon's own code for the core serving the call, the state core's E_ARCHIVE_PROCESS for the archive behind
    it."""
    header, _ = reply
    if header["ok"]:
        return
    error = header["error"]
    assert error["code"] == code, header
    assert error["retryable"] is True, header


def _assert_stored_or_retryable(reply):
    """A keyed store interrupted behind the state core is accepted or refused as retryable, whatever the code:
    the state core answers from its own ledger (a retry that is not yet eligible is E_BUSY)."""
    header, _ = reply
    assert header["ok"] or header["error"]["retryable"] is True, header


def _retry_until_stored(client, request, key, timeout=90):
    """Sends the same keyed request until it is accepted. Every refusal on the way must be retryable."""
    end = time.monotonic() + timeout
    while True:
        header, _ = _call(client, "state.compat", request, key=key)
        if header["ok"]:
            return header
        assert header["error"]["retryable"] is True, header
        assert time.monotonic() < end, header
        time.sleep(1)


def _records(client):
    header, body = _call(client, "state.list", {"limit": 500, "offset": 0})
    assert header["ok"] is True, header
    return body["items"]


def _store_request(content):
    return {"tool": "memory_store", "arguments": {
        "content": content, "kind": "event", "domain": "dev", "project": "fault-matrix",
        "observed_at": "2026-11-11T00:00:00+00:00",
    }}


@pytest.mark.parametrize(
    ("core_name", "lane", "code"),
    [("state", "read", "E_CORE_RESTARTED"), ("archive", "read", "E_ARCHIVE_PROCESS")],
    ids=["state-core", "archive-core"],
)
def test_read_in_flight_at_core_death_is_replayed_or_retryable(world, core_name, lane, code):
    client = world.client()
    try:
        reply = _kill_during_call(world, client, core_name, lane, "state.list",
                                  {"limit": 20, "offset": 0})
        _assert_replay_outcome(reply, code)
        if not reply[0]["ok"]:
            header, _ = _call(client, "state.list", {"limit": 20, "offset": 0})
            assert header["ok"] is True, header
        assert world.core(core_name)["ready"] is True
    finally:
        client.close()


def test_keyed_write_retried_after_state_core_death_leaves_one_record(world):
    client = world.client()
    key = "fault-matrix-state-keyed-write"
    content = f"{RECORD_PREFIX} state keyed write."
    request = _store_request(content)
    try:
        first = _kill_during_call(world, client, "state", "handler", "state.compat", request, key=key)
        _assert_replay_outcome(first)
        retried = _call(client, "state.compat", request, key=key)
        assert retried[0]["ok"] is True, retried[0]
        matches = [row for row in _records(client) if row.get("content") == content]
        assert len(matches) == 1
    finally:
        client.close()


FILLER = " ".join(f"synthetic-word-{index}" for index in range(12000))


def _catch_archive_write(daemon, client, request, key):
    """Sends one keyed request and kills the archive core if its archive write is caught dispatched and
    unanswered. A request's first archive call is a read (the writer check, a lookup), so a core stopped before
    the call never shows a write; the write is caught on the way instead: the core is stopped the moment its
    write lane is busy, and the catch counts only if the lane is still busy afterwards (a stopped core cannot
    answer). Returns (True, reply of the interrupted call) or (False, reply of the completed call)."""
    before = daemon.core("archive")
    assert before is not None and before["ready"]
    daemon._fault_matrix_pids.add(before["pid"])
    call_id = client.call("state.compat", request, key=key, deadline_ms=120000)
    caught, started, end = False, False, time.monotonic() + 30
    while time.monotonic() < end:
        if _lane_in_flight(daemon, "archive", "write"):
            os.kill(before["pid"], signal.SIGSTOP)
            time.sleep(0.3)
            caught = _lane_in_flight(daemon, "archive", "write")
            break
        busy = _lane_in_flight(daemon, "state", "handler")
        if started and not busy:
            break
        started = started or busy
    if not caught:
        os.kill(before["pid"], signal.SIGCONT)
        return False, client.reply(call_id, timeout=120)
    kill_group(before["pid"])
    wait_for(lambda: (lambda current: current is not None and current["ready"]
                      and current["pid"] != before["pid"])(daemon.core("archive")),
             timeout=120, what="archive to restart")
    return True, client.reply(call_id, timeout=120)


def _kill_archive_while_it_writes(daemon, client, attempts=20):
    """Sends keyed stores until one is caught with its archive write in flight, and kills the archive core
    there. Returns (key, request, content, reply of the interrupted call)."""
    for attempt in range(attempts):
        key = f"fault-matrix-archive-keyed-write-{attempt}"
        content = f"{RECORD_PREFIX} archive keyed write {attempt}. {FILLER}"
        request = _store_request(content)
        caught, reply = _catch_archive_write(daemon, client, request, key)
        if caught:
            return key, request, content, reply
        assert reply[0]["ok"] is True, reply[0]
    pytest.fail(f"no archive write was caught in flight in {attempts} stores")


def test_keyed_write_retried_after_archive_core_death_leaves_one_record(world):
    client = world.client()
    try:
        key, request, content, first = _kill_archive_while_it_writes(world, client)
        _assert_stored_or_retryable(first)
        _retry_until_stored(client, request, key)
        matches = [row for row in _records(client) if row.get("content") == content]
        assert len(matches) == 1
    finally:
        client.close()


def test_update_whose_archive_write_dies_with_the_core_is_retried_to_one_new_version(world):
    """An update is not a keyed archive operation: its request key is bound to the version it creates (a receipt).
    The archive core dies with the write in flight; the same request again must end in exactly one new version."""
    client = world.client()
    try:
        header, stored = _call(client, "state.compat", _store_request(f"{RECORD_PREFIX} to be updated."),
                               key="fault-matrix-update-base")
        assert header["ok"] is True, header
        record, version = stored["id"], 1
        for attempt in range(20):
            key = f"fault-matrix-archive-update-{attempt}"
            content = f"{RECORD_PREFIX} updated {attempt}. {FILLER}"
            request = {"tool": "memory_update", "arguments": {"id": record, "content": content,
                                                              "expected_version": version}}
            caught, first = _catch_archive_write(world, client, request, key)
            if not caught:
                assert first[0]["ok"] is True, first[0]
                version += 1
                continue
            _assert_stored_or_retryable(first)
            _retry_until_stored(client, request, key)
            header, body = _call(client, "state.compat", request, key=key)
            assert header["ok"] is True and body["version"] == version + 1, (header, body)
            header, history = _call(client, "state.compat", {"tool": "memory_history",
                                                             "arguments": {"id": record, "limit": 100}})
            assert header["ok"] is True, header
            # History holds the replaced versions; the present one is read with get.
            assert len(history["versions"]) == version
            header, current = _call(client, "state.compat", {"tool": "memory_get",
                                                             "arguments": {"id": record, "full": True}})
            assert header["ok"] is True, header
            assert (current["content"], current["version"]) == (content, version + 1)
            return
        pytest.fail("no archive update was caught in flight in 20 updates")
    finally:
        client.close()


def test_store_whose_first_archive_call_dies_with_the_core_is_retried_to_one_record(world):
    """The archive core dies while a keyed store waits on its first archive call, the writer check."""
    client = world.client()
    key = "fault-matrix-archive-writer-check"
    content = f"{RECORD_PREFIX} archive writer check."
    request = _store_request(content)
    try:
        first = _kill_during_call(world, client, "archive", "read", "state.compat", request, key=key)
        _assert_stored_or_retryable(first)
        _retry_until_stored(client, request, key)
        matches = [row for row in _records(client) if row.get("content") == content]
        assert len(matches) == 1
    finally:
        client.close()


def test_kelmod_sigkill_restart_keeps_acknowledged_records_and_keys(world):
    daemon = world
    client = daemon.client()
    expected = {}
    try:
        for index in range(2):
            key = f"fault-matrix-ack-{index}"
            content = f"{RECORD_PREFIX} acknowledged {index}."
            header, _ = _call(client, "state.compat", _store_request(content), key=key)
            assert header["ok"] is True, header
            expected[key] = content

        original_pid = daemon.proc.pid
        for name in ("state", "archive"):
            core = daemon.core(name)
            if core is not None:
                daemon._fault_matrix_pids.add(core["pid"])
        daemon.proc.kill()
        daemon.proc.wait(timeout=30)
        daemon.log.close()
        _restart(daemon)
        client.close()
        client = daemon.client()

        for key, content in expected.items():
            header, _ = _call(client, "state.compat", _store_request(content), key=key)
            assert header["ok"] is True, header
        contents = [row.get("content") for row in _records(client)]
        for content in expected.values():
            assert contents.count(content) == 1
        assert daemon.proc.pid != original_pid
    finally:
        client.close()


def test_outbox_enospc_refuses_enqueue_without_a_partial_row(tmp_path):
    from kelmo.client import SharedClient
    from kelmo.config import ClientProfile

    profile = ClientProfile(tmp_path / "client", "synthetic-authority", (str(KELMOD),), 1.0,
                            {"claude": (), "codex": ()}, 1024)
    client = SharedClient(profile, channel_factory=lambda: pytest.fail("ENOSPC must stop before transport"))
    outbox = client.outbox
    insert = outbox._insert

    def disk_full_after_insert(op, args, key):
        insert(op, args, key)
        raise OSError(errno.ENOSPC, "synthetic outbox disk full")

    outbox._insert = disk_full_after_insert
    try:
        with pytest.raises(OSError) as refused:
            client.submit("capture", {"content": "Synthetic pending write."}, "disk-full-key")
        assert refused.value.errno == errno.ENOSPC
        assert outbox.get("disk-full-key") is None
        assert outbox.pending() == []
    finally:
        client.close()
