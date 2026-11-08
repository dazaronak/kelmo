"""One vault, two interfaces: the MCP server and the CLI must answer with the same records."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from kelmo.store.config import EMBEDDING_DIM
from kelmo.model_store import FILES
from kelmo.service import Service

SRC = Path(__file__).resolve().parents[1] / 'src'

RECORDS = [
    ('gate-runner.md', '---\nproject: parity\ntags: [gate, runner]\n---\n'
                        'The shared gate runner reads the gate.json that sits beside each suite.\n'),
    ('journal-entry.md', '---\nproject: parity\ntags: [gate, journal]\n---\n'
                         'The journal holds one entry per run, naming its commits and what it verified.\n'),
    ('warm-model.md', '---\nproject: warm\ntags: [model]\n---\n'
                      'A warm process loads the embedding model once and answers every later call from it.\n'),
    ('forgotten-record.md', '---\nproject: archive\ntags: [forget]\n---\n'
                            'A forgotten record stays on disk and is hidden from search and from list.\n'),
]


def deterministic_embed(text, *, cache_dir=None):
    """A stand-in vector that is the same in every process, so two interfaces can be compared on ranking."""
    rng = np.random.RandomState(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2 ** 31))
    value = rng.randn(EMBEDDING_DIM).astype(np.float32)
    return (value / np.linalg.norm(value)).tobytes()


def build(root, label, *, initialize=True):
    if initialize:
        Service.initialize(root, label)
    with Service(root) as service:
        for filename, content in RECORDS:
            service.run(service.capture(content, filename, 'implementer')['id'])
    return root


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault whose records carry a stand-in vector and whose queries get the same one."""
    monkeypatch.setattr('kelmo.store.memory.store.embed', deterministic_embed)
    monkeypatch.setattr('kelmo.store.memory.search.embed', deterministic_embed)
    return build(tmp_path / 'vault', 'parity')


@pytest.fixture
def lexical_vault(tmp_path, monkeypatch):
    """A vault whose records were stored with a stand-in vector while its queries reach for a model it lacks."""
    monkeypatch.setattr('kelmo.store.memory.store.embed', deterministic_embed)
    return build(tmp_path / 'lexical', 'lexical')


def model_directory():
    """The pinned model files this host offers, or None when it names none."""
    value = os.environ.get('KELMO_MODEL_DIR') or os.environ.get('KELMO_GATE_MODEL_DIR')
    if not value:
        return None
    root = Path(value).expanduser()
    return root if all((root / name).is_file() for name in FILES) else None


@pytest.fixture
def warm_vault(tmp_path):
    """A vault holding the pinned model, so the real embedding ranks the same records twice."""
    directory = model_directory()
    if directory is None:
        pytest.skip('KELMO_MODEL_DIR (or KELMO_GATE_MODEL_DIR) must name the directory holding the five '
                    'pinned model files; this host names none, so the model-backed comparison cannot run here')
    root = tmp_path / 'warm-vault'
    Service.initialize(root, 'warm-parity')
    cache = root / '.cache' / 'embeddings'
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'model').symlink_to(directory)
    return build(root, 'warm-parity', initialize=False)


def through_cli(root, *argv, stdin=''):
    from kelmo.cli import main
    out = io.StringIO()
    with contextlib.redirect_stdout(out), patch('sys.stdin', io.StringIO(stdin)):
        code = main(['--vault', str(root), '--json', *argv])
    return code, json.loads(out.getvalue())


def through_search(root, arguments):
    """The CLI call of the handler behind MCP memory_search, with the handler's answer as `result`."""
    code, envelope = through_cli(root, 'tool', 'chunk_search', stdin=json.dumps(arguments))
    return code, dict(envelope, result=envelope['result']['result']) if code == 0 else envelope


def spawned_cli(root, *argv, stdin=''):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', '--vault', str(root), '--json', *argv],
                          input=stdin, capture_output=True, text=True, timeout=180, check=False,
                          env=dict(os.environ, PYTHONPATH=str(SRC)))


@contextlib.asynccontextmanager
async def served(root, *, timings=False):
    """One server session holding one vault open, the way a client keeps a server for a whole job."""
    from fastmcp import Client
    from kelmo.mcp import Vault, server
    holder = Vault(root)
    try:
        async with Client(server(holder.root, holder.open, report_timings=timings)) as client:
            yield client
    finally:
        holder.close()


def cli_search(query, limit, filters=None):
    argv = ['search', query, '--limit', str(limit)]
    for name, values in (filters or {}).items():
        for value in (values if isinstance(values, list) else [values]):
            argv += ['--' + name, value]
    return argv


def mcp_search(query, limit, filters=None):
    arguments = {'query_en': query, 'query_ru': query, 'limit': limit}
    for name, values in (filters or {}).items():
        key = 'tags' if name == 'tag' else name
        arguments[key] = values if isinstance(values, list) else ([values] if key == 'tags' else values)
    return arguments


def reports_of(text):
    return [json.loads(line)['timings'] for line in text.splitlines() if '"timings"' in line]


@pytest.mark.asyncio
async def test_search_answers_the_same_records_and_metadata_both_ways(vault):
    query = 'which runner reads the manifest that sits beside a suite'
    code, cli = through_search(vault, mcp_search(query, 4))
    assert code == 0, cli
    async with served(vault) as client:
        mcp = (await client.call_tool('memory_search', mcp_search(query, 4))).data
    assert mcp['items']
    assert mcp == cli['result']
    assert mcp['search'] == {'mode': 'hybrid', 'degraded': False, 'reason': None, 'semantic': True}


@pytest.mark.asyncio
async def test_filters_narrow_to_the_same_records_both_ways(vault):
    for query, limit, filters in (('record', 10, {'project': 'parity'}),
                                  ('gate', 10, {'tag': ['gate']}),
                                  ('gate', 10, {'project': 'warm'}),
                                  ('record', 10, {'since': '2020-01-01'}),
                                  ('record', 10, {'project': 'parity', 'tag': ['runner']})):
        code, cli = through_search(vault, mcp_search(query, limit, filters))
        assert code == 0, (filters, cli)
        async with served(vault) as client:
            mcp = (await client.call_tool('memory_search', mcp_search(query, limit, filters))).data
        assert mcp == cli['result'], (filters, mcp, cli['result'])
        # The record index the CLI's own search ranks holds the same records.
        code, records = through_cli(vault, *cli_search(query, limit, filters))
        assert code == 0 and {item['id'] for item in mcp['items']} <= {item['id'] for item in records['result']['items']}


@pytest.mark.asyncio
async def test_a_write_through_the_server_is_visible_to_the_next_call_and_to_the_cli(vault):
    query = 'a server that keeps its vault open'
    async with served(vault) as client:
        stored = (await client.call_tool('memory_store', {
            'content': 'A warm server keeps its vault open between calls.', 'kind': 'event', 'domain': 'dev',
            'project': 'parity', 'tags': ['server']})).data
        assert stored['version'] == 1
        trace = (await client.call_tool('memory_trace', {'operation_id': stored['operation_id']})).data
        assert [event['stage'] for event in trace['events']][-1] == 'archived'
        found = (await client.call_tool('memory_search', mcp_search(query, 10))).data
        assert any(item['id'] == stored['id'] for item in found['items'])

    async with served(vault) as client:
        again = (await client.call_tool('memory_search', mcp_search(query, 10))).data
    code, cli = through_search(vault, mcp_search(query, 10))
    assert code == 0, cli
    assert any('keeps its vault open' in item['content'] for item in cli['result']['items'])
    assert again == cli['result']


def test_a_server_without_a_model_says_so_once(lexical_vault):
    from kelmo.mcp import announce, warm_model
    with Service(lexical_vault) as service:
        warm = warm_model(service)
    assert warm['loaded'] is False and warm['reason'] == 'embedding_unavailable'
    stream = io.StringIO()
    announce(warm, lexical_vault, stream=stream)
    assert stream.getvalue().count('\n') == 1
    assert 'model unavailable' in stream.getvalue() and 'lexical fallback' in stream.getvalue()


@pytest.mark.parametrize('kind', ['local', 'broker'])
def test_configured_provider_warmup_never_uses_the_record_model(tmp_path, monkeypatch, kind):
    from kelmo.mcp import announce, warm_model
    calls = []

    def record_model(*args):
        calls.append('wrong record model')
        raise AssertionError('The configured provider owns this vault')

    provider = SimpleNamespace(
        health=lambda: calls.append('health') or {'ok': True},
        input_policy=lambda: calls.append('input policy') or object(),
    )
    monkeypatch.setattr('kelmo.store.memory.embed.get_model', record_model)
    service = SimpleNamespace(config=SimpleNamespace(cache=tmp_path, embedding={'provider': kind}),
                              archive_api=SimpleNamespace(chunk_index=SimpleNamespace(provider=provider)))
    warm = warm_model(service)
    assert warm['loaded'] is True and warm['provider'] == kind
    assert set(calls) == {'health', 'input policy'}
    stream = io.StringIO()
    announce(warm, tmp_path, stream=stream)
    assert kind in stream.getvalue() and 'lexical fallback' not in stream.getvalue()


@pytest.mark.parametrize('unavailable', ['no channel', 'unhealthy', 'unproven identity'])
def test_configured_provider_failure_never_warms_a_different_model(tmp_path, monkeypatch, unavailable):
    from kelmo.mcp import announce, warm_model
    calls = []

    def record_model(*args):
        calls.append('wrong record model')
        return object()

    def unproven():
        raise RuntimeError('Unproven configured input identity')

    provider = None if unavailable == 'no channel' else SimpleNamespace(
        health=lambda: {'ok': unavailable != 'unhealthy'}, input_policy=unproven,
    )
    monkeypatch.setattr('kelmo.store.memory.embed.get_model', record_model)
    service = SimpleNamespace(config=SimpleNamespace(cache=tmp_path, embedding={'provider': 'broker'}),
                              archive_api=SimpleNamespace(chunk_index=SimpleNamespace(
                                  provider=provider, unavailable='channel-not-opened')))
    warm = warm_model(service)
    assert warm['loaded'] is False and warm['provider'] == 'broker'
    if unavailable == 'no channel':
        assert 'channel-not-opened' in warm['detail']
    if unavailable == 'unhealthy':
        assert 'reports unhealthy' in warm['detail']
    assert calls == []
    stream = io.StringIO()
    announce(warm, tmp_path, stream=stream)
    assert 'broker' in stream.getvalue() and 'lexical fallback' in stream.getvalue()


def test_archive_owned_provider_is_not_misreported_as_unavailable(tmp_path, monkeypatch):
    from kelmo.mcp import announce, warm_model

    def record_model(*args):
        raise AssertionError('An archive-owned provider cannot warm the local record model')

    monkeypatch.setattr('kelmo.store.memory.embed.get_model', record_model)
    service = SimpleNamespace(config=SimpleNamespace(cache=tmp_path, embedding={'provider': 'broker'}),
                              archive_api=SimpleNamespace())
    warm = warm_model(service)
    assert warm['loaded'] is False and warm['reason'] == 'warmup_unsupported'
    stream = io.StringIO()
    announce(warm, tmp_path, stream=stream)
    assert 'archive adapter' in stream.getvalue() and 'lexical fallback' not in stream.getvalue()


@pytest.mark.asyncio
async def test_a_server_without_a_model_answers_lexically_as_the_cli_does(lexical_vault, capsys):
    query = 'which runner reads the manifest that sits beside a suite'
    async with served(lexical_vault, timings=True) as client:
        mcp = (await client.call_tool('memory_search', mcp_search(query, 4))).data
    code, cli = through_search(lexical_vault, mcp_search(query, 4))
    assert code == 0, cli
    assert mcp == cli['result']
    assert mcp['search'] == {'mode': 'lexical', 'degraded': True, 'reason': 'embedding_unavailable',
                             'semantic': False}
    lines = reports_of(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert lines[0]['command'] == 'memory_search' and 'model_load_ms' not in lines[0]


@pytest.mark.asyncio
async def test_a_warm_server_loads_the_model_once_and_names_no_load_per_call(warm_vault, capsys):
    from kelmo.mcp import announce, warm_model
    with Service(warm_vault) as warming:
        warm = warm_model(warming)
    assert warm['loaded'] is True, warm
    assert warm['model_load_ms'] >= 0
    stream = io.StringIO()
    announce(warm, warm_vault, stream=stream)
    assert stream.getvalue().count('\n') == 1 and 'model loaded in' in stream.getvalue()

    query = 'a forgotten record hidden from search and list'
    async with served(warm_vault, timings=True) as client:
        first = (await client.call_tool('memory_search', mcp_search(query, 4))).data
        second = (await client.call_tool('memory_search', mcp_search(query, 4))).data
    assert first == second
    assert first['search'] == {'mode': 'hybrid', 'degraded': False, 'reason': None, 'semantic': True}
    lines = reports_of(capsys.readouterr().err)
    assert len(lines) == 2, lines
    for line in lines:
        assert line['command'] == 'memory_search'
        assert 'model_load_ms' not in line, line

    cold = spawned_cli(warm_vault, '--timings', 'tool', 'chunk_search', stdin=json.dumps(mcp_search(query, 4)))
    assert cold.returncode == 0, cold.stdout + cold.stderr
    answer = json.loads(cold.stdout)['result']['result']
    assert answer['items'] == first['items']
    assert answer['search'] == first['search']
    cold_lines = reports_of(cold.stderr)
    assert len(cold_lines) == 1, cold.stderr
    assert cold_lines[0]['command'] == 'tool'
    assert cold_lines[0]['model_load_ms'] > 0
