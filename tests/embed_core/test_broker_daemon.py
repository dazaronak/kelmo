"""A synthetic broker is restarted by the embed core and backfill restores pending vectors."""
import json
import os
import signal
import sys

from daemon_harness import wait_for
from embed_core_support import NEEDS_KELMOD, PROJECT, cold_broker_vault, kelmod_call, start_vault_daemon

from kelmo.config import Config
from kelmo.contracts import EmbeddingSpace
from kelmo.store.memory.embed import identity_id


BROKER = '''import base64, json, os, struct, sys
from pathlib import Path
space, space_id, policy = json.loads(Path(sys.argv[1]).read_text())
pid, blocked = Path(sys.argv[2]), Path(sys.argv[3])
if blocked.exists():
    sys.exit(1)
pid.write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    if blocked.exists():
        answer = {"id": request["id"], "error": "offline"}
    elif request["kind"] == "health":
        answer = {"id": request["id"], "ok": True, "space": space, "input": policy}
    else:
        vector = base64.b64encode(struct.pack("<" + "f" * space["dimension"],
                                             *([space["dimension"] ** -0.5] * space["dimension"]))).decode()
        answer = {"id": request["id"], "space_id": space_id,
                  "vectors": [vector for text in request["texts"]]}
    print(json.dumps(answer), flush=True)
'''


@NEEDS_KELMOD
def test_broker_restarts_and_backfill_indexes_pending_record(tmp_path, monkeypatch):
    root = cold_broker_vault(tmp_path, monkeypatch)
    settings = Config.load(root).embedding
    script, spec, pid, blocked = (tmp_path / name for name in ('broker.py', 'broker.json', 'broker.pid', 'blocked'))
    script.write_text(BROKER)
    spec.write_text(json.dumps([settings['space'], identity_id(EmbeddingSpace.from_dict(settings['space'])),
                                settings['input']]))
    config = root / 'kelmo.toml'
    command = [sys.executable, str(script), str(spec), str(pid), str(blocked)]
    config.write_text(config.read_text().replace('provider = "broker"',
                                                 'provider = "broker"\ncommand = ' + json.dumps(command)))
    daemon = start_vault_daemon(root, tmp_path / 'run')

    def tool(name, arguments):
        result = kelmod_call(daemon, 'state.tool', {'tool': name, 'arguments': arguments})
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def store(content):
        return tool('memory_store', {'content': content, 'kind': 'event', 'domain': 'dev', 'project': PROJECT})

    def search():
        return tool('memory_search', {'query_en': 'synthetic orchard', 'query_ru': 'synthetic orchard',
                                      'mode': 'semantic', 'project': PROJECT})

    try:
        first = store('synthetic orchard apples')['id']
        assert any(item['id'] == first for item in search()['items'])
        old_pid = int(pid.read_text())
        blocked.touch()
        os.kill(old_pid, signal.SIGKILL)
        second = store('synthetic orchard pears')['id']
        blocked.unlink()

        def recovered():
            response = kelmod_call(daemon, 'state.tool', {'tool': 'memory_search', 'arguments': {
                'query_en': 'synthetic orchard', 'query_ru': 'synthetic orchard', 'mode': 'semantic',
                'project': PROJECT}})
            if response.returncode:
                return False
            result = json.loads(response.stdout)
            return int(pid.read_text()) != old_pid and any(item['id'] == first for item in result['items'])

        wait_for(recovered, timeout=60, what='the broker to restart and semantic search to recover')
        job = kelmod_call(daemon, 'state.job_run', {'job': 'backfill'})
        assert job.returncode == 0, job.stdout + job.stderr
        assert json.loads(job.stdout)['written'] > 0
        assert any(item['id'] == second for item in search()['items'])
    finally:
        daemon.stop()
