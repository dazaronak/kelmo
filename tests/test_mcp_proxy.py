"""Exercise reconnects against a real, synthetic stdio backend."""
import io
import json
import os
import signal
import sys
import time

import pytest

from kelmo.mcp_proxy import Proxy


BACKEND = """
import json, sys
from pathlib import Path
counter = Path(sys.argv[1])
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
ready = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "notifications/initialized":
        continue
    if method in ("initialize", "server/discover"):
        ready = True
        if number > 1 and sys.argv[2] == "reject":
            print(json.dumps({"id": msg["id"], "error": {"code": -1}}), flush=True)
            continue
    elif not ready:
        raise RuntimeError("opening not replayed")
    elif method == "write":
        sys.exit(0)
    print(json.dumps({"id": msg["id"], "result": {"start": number}}), flush=True)
    if method == "exit":
        sys.exit(0)
"""


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "backend did not settle"
        time.sleep(0.01)


def send(proxy, method, request_id):
    proxy.handle(json.dumps({"jsonrpc": "2.0", "method": method, "id": request_id}))


@pytest.fixture
def backend(tmp_path):
    source = tmp_path / "backend.py"
    source.write_text(BACKEND)
    counter = tmp_path / "starts"
    output = io.StringIO()
    proxy = Proxy([sys.executable, str(source), str(counter), "accept"],
                  attempts=2, backoff=0, handshake_timeout=1, output=output)
    yield proxy, output, counter
    proxy.stop()


@pytest.mark.parametrize("opening", ["initialize", "server/discover"])
def test_idle_restart_and_lost_write_are_not_replayed(backend, opening):
    proxy, output, counter = backend
    send(proxy, opening, 1)
    wait_for(lambda: '"id": 1' in output.getvalue())
    send(proxy, "exit", 2)
    wait_for(lambda: proxy.backend is None)
    send(proxy, "read", 3)
    wait_for(lambda: '"id": 3' in output.getvalue())
    send(proxy, "write", 4)
    wait_for(lambda: '"id": 4' in output.getvalue())
    send(proxy, "read", 5)
    wait_for(lambda: '"id": 5' in output.getvalue())
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4, 5]
    assert replies[2]["result"]["start"] == 2
    assert "outcome is unknown" in replies[3]["error"]["message"]
    assert replies[4]["result"]["start"] == 3
    assert counter.read_text() == "3"


def test_failed_reopening_does_not_send_the_new_request(backend):
    proxy, output, counter = backend
    send(proxy, "initialize", 1)
    wait_for(lambda: '"id": 1' in output.getvalue())
    send(proxy, "exit", 2)
    wait_for(lambda: proxy.backend is None)
    proxy.command[-1] = "reject"
    send(proxy, "write", 3)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert "request was not sent" in replies[-1]["error"]["message"]
    assert counter.read_text() == "3"


def test_stopping_proxy_reaps_backend_children(tmp_path):
    ready, survived = tmp_path / "ready", tmp_path / "survived"
    child = ("import os,time; from pathlib import Path; "
             f"Path({str(ready)!r}).write_text(str(os.getpid())); "
             f"time.sleep(0.5); Path({str(survived)!r}).touch()")
    parent = ("import subprocess,sys,time; "
              f"subprocess.Popen([sys.executable,'-c',{child!r}], "
              "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(10)")
    proxy = Proxy([sys.executable, "-c", parent], output=io.StringIO())
    try:
        send(proxy, "initialize", 1)
        wait_for(ready.exists)
        proxy.stop()
        time.sleep(0.7)
        assert not survived.exists(), "backend child survived proxy shutdown"
    finally:
        proxy.stop()
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


TOOL_BACKEND = """
import json, sys
from pathlib import Path
counter = Path(sys.argv[1])
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'notifications/cancelled':
        print(json.dumps({'id': msg['params']['requestId'], 'error': {'code': 0, 'message': 'Request cancelled'}}), flush=True)
        continue
    if 'id' not in msg or method == 'slow':
        continue
    arguments = (msg.get('params') or {}).get('arguments') or {}
    if arguments.get('lose_on') == number:
        sys.exit(0)
    print(json.dumps({'id': msg['id'], 'result': {'start': number}}), flush=True)
"""


@pytest.fixture
def tools(tmp_path):
    source = tmp_path / 'tools.py'
    source.write_text(TOOL_BACKEND)
    counter = tmp_path / 'starts'
    output = io.StringIO()
    proxy = Proxy([sys.executable, str(source), str(counter)], attempts=2, backoff=0, handshake_timeout=1,
                  output=output)
    yield proxy, output
    proxy.stop()


def call(proxy, request_id, name, **arguments):
    proxy.handle(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                             'params': {'name': name, 'arguments': arguments}}))


def replies(output):
    return {reply['id']: reply for reply in map(json.loads, output.getvalue().splitlines())}


def test_a_lost_read_is_asked_again_on_the_new_backend_and_a_lost_write_is_not(tools):
    proxy, output = tools
    send(proxy, 'initialize', 1)
    wait_for(lambda: 1 in replies(output))
    call(proxy, 2, 'memory_search', lose_on=1)
    wait_for(lambda: 2 in replies(output))
    assert replies(output)[2]['result'] == {'start': 2}, 'the read was not resent to the new backend'
    call(proxy, 3, 'memory_create', lose_on=2)
    wait_for(lambda: 3 in replies(output))
    assert 'outcome is unknown' in replies(output)[3]['error']['message']
    call(proxy, 4, 'graph_read', lose_on=3)
    call(proxy, 5, 'memory_search', lose_on=4)
    wait_for(lambda: 5 in replies(output))
    assert replies(output)[4]['result'] == {'start': 4}
    assert 'did not come back' not in output.getvalue()


def test_an_answer_to_a_cancelled_request_never_reaches_the_client(tools):
    proxy, output = tools
    send(proxy, 'initialize', 1)
    wait_for(lambda: 1 in replies(output))
    send(proxy, 'slow', 2)
    proxy.handle(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                             'params': {'requestId': 2, 'reason': 'interrupted'}}))
    send(proxy, 'ping', 3)
    wait_for(lambda: 3 in replies(output))
    time.sleep(0.1)
    assert 2 not in replies(output), 'a late answer to a cancelled request reached the client'
    proxy.handle(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                             'params': {'requestId': 3, 'reason': 'already answered'}}))
    proxy.handle(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                             'params': {'requestId': 99, 'reason': 'never sent'}}))
    assert 3 not in proxy.cancelled and 99 not in proxy.cancelled, 'only an in-flight request waits for an answer'
