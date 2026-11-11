"""Additional durable-write fault cases against real kelmod cores."""
import os
import signal
import sys
from pathlib import Path

import pytest
from daemon_harness import kill_group, wait_for
from test_fault_matrix import (
    IDENTITY,
    RECORD_PREFIX,
    _assert_stored_or_retryable,
    _records,
    _remember_core_pids,
    _restart,
    _retry_until_stored,
    _start,
    _store_request,
)

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from embed_core.embed_core_support import cold_broker_vault  # noqa: E402


def _kill_remembered_cores(daemon, pids):
    _remember_core_pids(daemon, pids)
    daemon.stop()
    for pid in pids:
        kill_group(pid)


def test_kelmod_killed_during_concurrent_keyed_stores_retries_each_key_once(tmp_path, monkeypatch):
    """kelmod dies with four keyed stores in flight; acknowledged and retried writes remain once each."""
    from kelmo.service import Service

    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "vault"
    Service.initialize(root, "synthetic-kelmod-kill")
    daemon = _start(root, tmp_path)
    pids, clients = set(), []
    acknowledged = "Synthetic acknowledged record before concurrent daemon death."
    expected = {"fault-matrix-kelmod-ack": acknowledged}
    try:
        client = daemon.client()
        clients.append(client)
        call_id = client.call("state.compat", _store_request(acknowledged),
                              key="fault-matrix-kelmod-ack", deadline_ms=120000)
        header, _ = client.reply(call_id, timeout=120)
        assert header["ok"] is True, header
        client.close()
        clients.remove(client)

        filler = " ".join(f"synthetic-word-{index}" for index in range(12000))
        pending = {}
        for index in range(4):
            key = f"fault-matrix-kelmod-in-flight-{index}"
            content = f"{RECORD_PREFIX} kelmod in-flight {index}. {filler}"
            pending[key] = (content, _store_request(content))
            connection = daemon.client()
            clients.append(connection)
            connection.call("state.compat", pending[key][1], key=key, deadline_ms=120000)

        try:
            wait_for(lambda: (lambda core: core is not None and
                              core["lanes"]["handler"]["in_flight"] > 0)(daemon.core("state")),
                     timeout=30, what="state.handler keyed stores to be in flight")
        except AssertionError as error:
            pytest.fail(f"no keyed store was caught in state.handler before kelmod death: {error}")

        _remember_core_pids(daemon, pids)
        original_pid = daemon.proc.pid
        daemon.proc.kill()
        daemon.proc.wait(timeout=30)
        daemon.log.close()
        for connection in clients:
            connection.close()
        clients.clear()
        _restart(daemon)
        _remember_core_pids(daemon, pids)
        assert daemon.proc.pid != original_pid

        for key, (content, request) in pending.items():
            expected[key] = content
            retry_client = daemon.client()
            try:
                _retry_until_stored(retry_client, request, key)
            finally:
                retry_client.close()

        reader = daemon.client()
        try:
            contents = [row.get("content") for row in _records(reader)]
            for content in expected.values():
                assert contents.count(content) == 1
        finally:
            reader.close()
    finally:
        for connection in clients:
            connection.close()
        _kill_remembered_cores(daemon, pids)


def test_embed_core_killed_during_keyed_store_retries_to_one_record(tmp_path, monkeypatch):
    """The embedding core dies during a keyed store; retrying the same key leaves one archive record."""
    root = cold_broker_vault(tmp_path / "broker-vault", monkeypatch)
    daemon = _start(root, tmp_path, embed_core=True)
    pids, client, embed_pid = set(), None, None
    killed = False
    try:
        client = daemon.client()
        embed = daemon.core("embed")
        if embed is None or not embed["ready"]:
            pytest.fail("embedding core did not become ready")
        embed_pid = embed["pid"]
        pids.add(embed_pid)
        os.kill(embed_pid, signal.SIGSTOP)
        key = "fault-matrix-embed-keyed-write"
        content = f"{RECORD_PREFIX} embedding core interrupted. " + " ".join(
            f"synthetic-word-{index}" for index in range(12000))
        request = _store_request(content)
        call_id = client.call("state.compat", request, key=key, deadline_ms=120000)
        try:
            wait_for(lambda: (lambda core: core is not None and
                              core["lanes"]["count"]["in_flight"] > 0)(daemon.core("embed")),
                     timeout=30, what="embed.count to be in flight")
        except AssertionError as error:
            pytest.fail(f"no embed.count call was caught in flight: {error}")

        kill_group(embed_pid)
        killed = True
        wait_for(lambda: (lambda current: current is not None and current["ready"]
                          and current["pid"] != embed_pid)(daemon.core("embed")),
                 timeout=120, what="embedding core to restart")
        _remember_core_pids(daemon, pids)
        _assert_stored_or_retryable(client.reply(call_id, timeout=120))
        _retry_until_stored(client, request, key)
        matches = [row for row in _records(client) if row.get("content") == content]
        assert len(matches) == 1
    finally:
        if client is not None:
            client.close()
        if embed_pid is not None and not killed:
            kill_group(embed_pid)
        _kill_remembered_cores(daemon, pids)
