"""Harness for the daemon black-box tests: the spec kelmod reads, a started daemon, a raw kelmo/2
client and polling helpers. Test data is synthetic; every path lives under the test's temp directory."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

from kelmo.core import wire

REPO = Path(__file__).resolve().parents[2]
KELMOD = Path(os.environ.get("KELMOD") or REPO / "rust" / "target" / "debug" / "kelmod")
SRC = REPO / "src"
STUB = Path(__file__).resolve().parent / "stub_core.py"

DEFAULT_LANES = {
    "fast": {"limit": 8, "queue": 256, "order": "fifo"},
    "work": {"limit": 1, "queue": 16, "order": "fifo"},
}

# (operation, lane, replay class) for every operation the stub cores serve.
OPERATIONS = [
    ("state.echo", "fast", "read"),
    ("state.relay", "fast", "read"),
    ("state.sleep", "work", "read"),
    ("state.crash_once", "work", "read"),
    ("state.crash_keyed", "work", "keyed"),
    ("state.crash_never", "work", "never"),
    ("state.alloc", "fast", "never"),
    ("state.freeze", "fast", "never"),
    ("archive.echo", "fast", "read"),
    ("archive.relay", "fast", "read"),
]


def build_spec(root, runtime, scratch, *, lanes=None, state_argv=(), state_budget=None, archive_argv=(),
               **options):
    """A spec with a `state` and an `archive` core, both run by the stub.

    `lanes` replaces single lanes of the defaults; `options` override top-level keys."""
    lanes = {**DEFAULT_LANES, **(lanes or {})}

    def core(name, argv, budget):
        return {"name": name, "argv": [sys.executable, str(STUB), name, str(scratch), *argv],
                "env": {"PYTHONPATH": str(SRC)}, "lanes": lanes, "budget": budget}

    spec = {
        "root": str(root),
        "runtime_dir": str(runtime),
        "origin": {"client_id": "test-cli"},
        "cores": [core("state", state_argv, state_budget), core("archive", archive_argv, None)],
        "operations": [{"name": name, "lanes": [lane], "replay": replay}
                       for name, lane, replay in OPERATIONS],
        "heartbeat_ms": 5000,
        "heartbeat_misses": 3,
        "sample_ms": 10000,
        "restart_max": 5,
        "restart_window_ms": 60000,
        "drain_timeout_ms": 5000,
    }
    spec.update(options)
    return spec


def new_id():
    return uuid.uuid4().hex


def wait_for(predicate, *, timeout=10.0, what="a condition"):
    """Polls `predicate` until it returns a true value, and returns that value."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= end:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.05)


def error_of(header):
    """The error of a reply that refused the call; fails when the call succeeded."""
    assert header["ok"] is False, header
    return header["error"]


def kill_group(pid):
    """Kills the process group a stub core leads; kelmod starts every core in its own group."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def connect(path, timeout=15.0):
    """Connects to the daemon socket, retrying while the backlog is full or the socket is unbound."""
    end = time.monotonic() + timeout
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= end:
                raise
            time.sleep(0.02)


class RawClient:
    """One kelmo/2 connection. Frames for other calls wait in an inbox until asked for."""

    def __init__(self, path, *, hello=True, origin=None):
        self.sock = connect(path)
        self.reader = wire.FrameReader()
        self.inbox = deque()
        if hello:
            header = {"v": 2, "kind": "hello", "id": new_id(), "body_type": "json",
                      "protocols": ["kelmo/2"], "operations": []}
            if origin is not None:
                header["origin"] = origin
            self.send(header)
            self.reply(header["id"])

    def send(self, header, body=None):
        self.sock.sendall(wire.encode(header, body))

    def call(self, op, body=None, *, lane=None, key=None, deadline_ms=10000, origin=None):
        call_id = new_id()
        header = {"v": 2, "kind": "call", "id": call_id, "op": op, "body_type": "json",
                  "deadline_ms": deadline_ms}
        if lane is not None:
            header["lane"] = lane
        if key is not None:
            header["key"] = key
        if origin is not None:
            header["origin"] = origin
        self.send(header, {} if body is None else body)
        return call_id

    def cancel(self, call_id):
        self.send({"v": 2, "kind": "cancel", "id": call_id, "body_type": "json"})

    def reply(self, call_id, timeout=15.0):
        """(header, body) of the frame answering `call_id`."""
        for index, (header, body) in enumerate(self.inbox):
            if header["id"] == call_id:
                del self.inbox[index]
                return header, body
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no reply to call {call_id}")
            self.sock.settimeout(left)
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("the daemon closed the connection")
            found = None
            for header, body in self.reader.feed(data):
                if found is None and header["id"] == call_id:
                    found = (header, body)
                else:
                    self.inbox.append((header, body))
            if found is not None:
                return found

    def close(self):
        self.sock.close()


class Daemon:
    """A started kelmod, its runtime directory and the scratch files its stub cores write."""

    def __init__(self, proc, spec_path, runtime, scratch, log):
        self.proc = proc
        self.spec_path = spec_path
        self.runtime = runtime
        self.scratch = scratch
        self.log = log

    @property
    def socket(self):
        return self.runtime / "d.sock"

    def status(self):
        """`kelmod status` as a dict, or None while the daemon does not answer."""
        result = subprocess.run([str(KELMOD), "status", "--runtime-dir", str(self.runtime)],
                                capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def core(self, name="state"):
        status = self.status()
        if status is None:
            return None
        return next(core for core in status["cores"] if core["name"] == name)

    def lane_counts(self, lane="work"):
        """(queued, in_flight) of a state-core lane; None while the status is unavailable."""
        core = self.core()
        if core is None:
            return None
        return core["lanes"][lane]["queued"], core["lanes"][lane]["in_flight"]

    def all_ready(self):
        status = self.status()
        return (status is not None and len(status["cores"]) == 2
                and all(core["ready"] for core in status["cores"]))

    def settled(self, restarts):
        """The state core is ready again and has been restarted `restarts` times."""
        core = self.core()
        return core is not None and core["ready"] and core["restarts"] == restarts

    def own_memory(self):
        """Resident memory of kelmod itself, in bytes."""
        memory = self.status()["memory_bytes"]
        assert memory is not None, "kelmod reports no resident memory on this platform"
        return memory

    def seen(self, core="state"):
        path = self.scratch / f"{core}.seen"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def tags(self, op="sleep"):
        return [entry["tag"] for entry in self.seen() if entry["op"] == op]

    def trims(self):
        path = self.scratch / "state.trim"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def client(self, *, hello=True, origin=None):
        return RawClient(self.socket, hello=hello, origin=origin)

    def wait_exit(self, timeout):
        return self.proc.wait(timeout=timeout)

    def stub_pids(self):
        pids = []
        for core in ("state", "archive"):
            path = self.scratch / f"{core}.pids"
            if path.exists():
                pids += [int(line) for line in path.read_text().split()]
        return pids

    def stop(self):
        """Stops kelmod with SIGTERM, then kills any stub core that outlived it."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        for pid in self.stub_pids():
            kill_group(pid)
        shutil.rmtree(self.runtime, ignore_errors=True)


def launch(workdir, **options):
    """Writes the spec under `workdir` and starts `kelmod serve` on it."""
    root, scratch = workdir / "vault", workdir / "scratch"
    root.mkdir(parents=True)
    scratch.mkdir()
    # A Unix socket path must stay under about 104 bytes, so the runtime directory is short.
    runtime = Path(tempfile.mkdtemp(prefix="kd", dir="/tmp"))
    spec_path = workdir / "spec.json"
    spec_path.write_text(json.dumps(build_spec(root, runtime, scratch, **options)))
    log = (workdir / "kelmod.log").open("w")
    command = [str(KELMOD), "serve", "--spec", str(spec_path)]
    proc = subprocess.Popen(command, stdout=log, stderr=log)
    return Daemon(proc, spec_path, runtime, scratch, log)
