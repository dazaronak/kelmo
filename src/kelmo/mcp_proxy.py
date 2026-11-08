"""Keep the client-side stdio connection alive across backend restarts.

The accepted protocol opening is replayed. A lost write has an unknown outcome
and is never resent; a lost read (a read-only tool or a listing) is resent once
on the new backend, because asking again cannot change anything. A response to a
request the client cancelled is dropped: the client no longer knows that ID and
treats a late answer as a broken connection.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


OPENINGS = {"initialize", "server/discover"}
READ_METHODS = {"tools/list", "resources/list", "resources/templates/list", "prompts/list", "ping"}
READ_TOOLS = {"memory_search", "memory_get", "memory_history", "memory_list", "memory_stats", "memory_status",
              "memory_trace", "memory_doctor", "graph_query", "graph_read", "graph_stats"}


def resendable(message):
    """A request whose loss can be repaired by asking again: a listing or a read-only tool call."""
    method = message.get("method")
    if method in READ_METHODS:
        return True
    params = message.get("params")
    return method == "tools/call" and isinstance(params, dict) and params.get("name") in READ_TOOLS


def terminate(process):
    """Stop the owned backend group even if its leader has already exited."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class Proxy:
    def __init__(self, command, *, attempts=3, backoff=2, handshake_timeout=25,
                 output=None):
        self.command = command
        self.attempts, self.backoff = attempts, backoff
        self.handshake_timeout = handshake_timeout
        self.output = output or sys.stdout
        self.lock = threading.RLock()
        self.handshake = threading.Condition(self.lock)
        self.backend = None
        self.opening = None
        self.initialized = None
        self.pending = {}
        self.openings = {}
        self.replay_id = None
        self.replay_ok = None
        # Lost reads waiting for the next backend, and request IDs the client cancelled.
        self.resend = {}
        self.cancelled = set()

    def emit(self, message):
        line = message if isinstance(message, str) else json.dumps(message)
        self.output.write(line if line.endswith("\n") else line + "\n")
        self.output.flush()

    def error(self, request_id, message):
        self.emit({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32000, "message": message}})

    def detach(self, process):
        if self.backend is not process:
            return
        self.backend = None
        pending, self.pending = self.pending, {}
        # A dead backend sends no late answers, so the ids awaiting one are done.
        self.cancelled.clear()
        self.openings.clear()
        self.handshake.notify_all()
        for request_id, (method, line, retries) in pending.items():
            if request_id in self.cancelled:
                continue
            if retries > 0:
                self.resend[request_id] = (method, line, retries - 1)
                continue
            self.error(request_id, f"MCP transport lost while '{method}' was in flight; "
                       "the outcome is unknown. Verify the result before retrying a write.")

    def recover(self):
        """Resend lost reads on a new backend, or answer them as failed when none starts."""
        with self.lock:
            if not self.resend:
                return
            waiting, self.resend = self.resend, {}
            if self.backend is None or self.backend.poll() is not None:
                if self.backend is not None:
                    terminate(self.backend)
                    self.detach(self.backend)
                if not self.connect():
                    for request_id, (method, _, _) in waiting.items():
                        self.error(request_id, f"MCP transport lost while '{method}' was in flight and "
                                   "the backend did not come back; nothing was changed. Retry the read.")
                    return
            for request_id, entry in waiting.items():
                self.pending[request_id] = entry
                if not self.send(entry[1]):
                    self.stop()
                    return

    def read_backend(self, process):
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                with self.lock:
                    if self.backend is not process:
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        message = None
                    if isinstance(message, dict) and "id" in message and "method" not in message:
                        request_id = message["id"]
                        if request_id == self.replay_id:
                            self.replay_ok = "result" in message and "error" not in message
                            self.handshake.notify_all()
                            continue
                        self.pending.pop(request_id, None)
                        if request_id in self.cancelled:
                            self.cancelled.discard(request_id)
                            continue
                        candidate = self.openings.pop(request_id, None)
                        if candidate is not None and "result" in message and "error" not in message:
                            self.opening = candidate
                    self.emit(line)
        finally:
            process.stdout.close()
            with self.lock:
                self.detach(process)
            # A backend may close stdout without exiting.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            finally:
                terminate(process)
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            self.recover()

    def stop(self):
        with self.lock:
            process = self.backend
            if process is not None:
                self.detach(process)
                terminate(process)

    def send(self, line):
        try:
            self.backend.stdin.write(line if line.endswith("\n") else line + "\n")
            self.backend.stdin.flush()
            return True
        except (BrokenPipeError, OSError, AttributeError, ValueError):
            return False

    def connect(self):
        for attempt in range(self.attempts):
            try:
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, text=True,
                                           encoding="utf-8", bufsize=1, start_new_session=True)
            except OSError:
                process = None
            self.backend = process
            if process is not None:
                self.replay_id = "__memory_reconnect_" + uuid.uuid4().hex
                self.replay_ok = None
                threading.Thread(target=self.read_backend, args=(process,), daemon=True).start()
                if self.opening is None:
                    return True
                replay = json.dumps(dict(self.opening, id=self.replay_id))
                if self.send(replay):
                    self.handshake.wait_for(
                        lambda: self.replay_ok is not None or self.backend is not process,
                        timeout=self.handshake_timeout)
                    if self.backend is process and self.replay_ok is True:
                        if self.opening["method"] != "initialize" or self.initialized is None:
                            return True
                        if self.send(self.initialized):
                            return True
                self.stop()
            if attempt + 1 < self.attempts:
                time.sleep(self.backoff * (attempt + 1))
        return False

    def handle(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            message = {}
        if not isinstance(message, dict):
            message = {}
        method = message.get("method")
        is_request = "id" in message and isinstance(method, str)
        request_id = message.get("id")
        with self.lock:
            if self.backend is None or self.backend.poll() is not None:
                # Drain lost requests before replacing the reader's generation.
                if self.backend is not None:
                    terminate(self.backend)
                    self.detach(self.backend)
                if method in OPENINGS:
                    self.opening, self.initialized = None, None
                if not self.connect():
                    if is_request:
                        self.error(request_id, "MCP backend is unreachable; this request was not sent.")
                    return
            if method in OPENINGS and is_request:
                self.openings[request_id] = message
            elif method == "notifications/initialized":
                self.initialized = line
            elif method == "notifications/cancelled":
                params = message.get("params")
                cancelled = params.get("requestId") if isinstance(params, dict) else None
                if cancelled is not None:
                    # Only a request still in flight can get a late answer to drop.
                    if self.pending.pop(cancelled, None) is not None:
                        self.cancelled.add(cancelled)
                    self.resend.pop(cancelled, None)
            if is_request:
                self.pending[request_id] = (method, line, 1 if resendable(message) else 0)
            if not self.send(line):
                # A failed flush can still have delivered bytes.
                self.stop()

    def run(self, stream=None):
        try:
            for line in stream or sys.stdin:
                if line.strip():
                    self.handle(line)
        finally:
            self.stop()


def serve(root, *, executable="kelmo"):
    args = ["--vault", str(Path(root).expanduser().resolve()), "mcp"]
    if Path(executable).is_absolute():
        command = [executable, *args]
    else:
        # Pin the running package, including source checkouts, without ambient
        # startup hooks or a PATH lookup that could select another installation.
        packages = str(Path(__file__).resolve().parent.parent)
        bootstrap = ("import sys; sys.path.insert(0, " + repr(packages) + "); "
                     "from kelmo.cli import main; raise SystemExit(main())")
        command = [sys.executable, "-I", "-S", "-c", bootstrap, *args]
        site = str(Path(sys.prefix) / "lib" /
                   f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
        command[4] = "import sys; sys.path.append(" + repr(site) + "); " + bootstrap
    Proxy(command).run()
