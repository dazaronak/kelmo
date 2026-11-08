import json
import os
import sys
import time

import pytest

from kelmo.cli import call


def test_broker_pipes_reach_only_the_wrapped_command_and_are_reaped(tmp_path):
    broker, child, pid = tmp_path / "broker.py", tmp_path / "child.py", tmp_path / "broker.pid"
    survived = tmp_path / "descendant-survived"
    broker.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "for line in sys.stdin:\n"
        " message=json.loads(line); print(json.dumps({'id':message['id'],'ok':True}),flush=True)\n")
    child.write_text(
        "import json,os,subprocess,sys\n"
        "request,answer=map(int,os.environ['KELMO_EMBED_FDS'].split(','))\n"
        "os.write(request,b'{\"id\":\"synthetic\",\"kind\":\"health\",\"texts\":[]}\\n')\n"
        "assert json.loads(os.fdopen(answer).readline())=={'id':'synthetic','ok':True}\n"
        "code=\"import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(.25); Path(\"+repr(sys.argv[1])+\").write_text('alive')\"\n"
        "p=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True)\n"
        "assert p.stdout.readline().strip()=='ready'\n"
        "raise SystemExit(7)\n")
    code = call(["broker", "--command-json", json.dumps([sys.executable, str(broker), str(pid)]),
                 "--", sys.executable, str(child), str(survived)])
    assert code == 7
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
    time.sleep(.4)
    assert not survived.exists()


def test_sigterm_reaches_the_wrapped_command_and_the_launcher_waits_for_its_exit(tmp_path):
    import signal
    import subprocess
    broker, child, finished = tmp_path / "broker.py", tmp_path / "child.py", tmp_path / "finished"
    broker.write_text("import sys\nfor line in sys.stdin: pass\n")
    child.write_text(
        "import signal,sys,time\nfrom pathlib import Path\n"
        "stop=[]\nsignal.signal(signal.SIGTERM,lambda *a: stop.append(1))\n"
        "print('ready',flush=True)\n"
        "while not stop: time.sleep(.05)\n"
        "time.sleep(4)\nPath(sys.argv[1]).write_text('done')\n")
    launcher = ("import sys\nfrom kelmo.cli import call\n"
                "sys.exit(call(['broker','--command-json',sys.argv[1],'--',sys.executable,sys.argv[2],sys.argv[3]]))\n")
    process = subprocess.Popen([sys.executable, "-c", launcher, json.dumps([sys.executable, str(broker)]),
                                str(child), str(finished)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        # The child takes longer than the old 3 s grace to finish what it was doing.
        assert process.wait(timeout=10) == 0
        assert finished.read_text() == "done"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
