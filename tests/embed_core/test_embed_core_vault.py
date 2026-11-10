"""The embedding core under kelmod on a cold-broker vault: its chunks, and a refused direct call."""
import json

from embed_core_support import (
    NEEDS_KELMOD,
    PROJECT,
    RECORD,
    VALID_FROM,
    archive_chunks,
    cold_broker_vault,
    kelmod_call,
    start_vault_daemon,
    store_like_memory_store,
)

from kelmo.service import Service


def store_through_kelmod(daemon, content):
    """memory_store through the state core, as an MCP client calls it."""
    arguments = {'content': content, 'kind': 'event', 'domain': 'dev', 'project': PROJECT,
                 'valid_from': VALID_FROM}
    stored = kelmod_call(daemon, 'state.tool', {'tool': 'memory_store', 'arguments': arguments})
    assert stored.returncode == 0, stored.stdout + stored.stderr


@NEEDS_KELMOD
def test_chunks_through_the_embedding_core_equal_the_in_process_chunks(tmp_path, monkeypatch):
    kelmod_root = cold_broker_vault(tmp_path / 'kelmod', monkeypatch)
    daemon = start_vault_daemon(kelmod_root, tmp_path / 'kelmod-run')
    try:
        store_through_kelmod(daemon, RECORD)
    finally:
        daemon.stop()
    plain_root = cold_broker_vault(tmp_path / 'plain', monkeypatch)
    with Service(plain_root) as service:
        store_like_memory_store(service, RECORD)
        profile = service.archive_api.chunk_index.profile_id()
    through_core = archive_chunks(kelmod_root)
    in_process = archive_chunks(plain_root)
    assert len(in_process) >= 2
    assert len({row[0] for row in through_core}) == 1
    assert [row[1:] for row in through_core] == [row[1:] for row in in_process]
    assert {row[3] for row in through_core} == {profile}


@NEEDS_KELMOD
def test_a_client_cannot_call_the_embedding_core_directly(tmp_path, monkeypatch):
    root = cold_broker_vault(tmp_path, monkeypatch)
    daemon = start_vault_daemon(root, tmp_path / 'run')
    try:
        refused = kelmod_call(daemon, 'embed.count', {'texts': ['synthetic'], 'prefix': ''})
    finally:
        daemon.stop()
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert json.loads(refused.stdout)['error']['code'] == 'E_FORBIDDEN'
