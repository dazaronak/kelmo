"""A scripted kelmo/2 daemon on a Unix socket, for tests of the clients that talk to kelmod.

It stands where kelmod's `d.sock` would be, in a short private directory (a Unix socket path must stay
under about 104 bytes, and `daemon_client` refuses a directory others can read). Each connection it
accepts follows one `Connection` of its script: it answers the client's hello or ends there, then meets
each call frame with the next action, and records every call frame it read.

Actions on a call: `ANSWER` replies ok with what `answer(header, body, n)` returns (n counts the calls read
so far, all connections together); `CLOSE` closes; `RESET` resets the connection; `SILENT` keeps it open and
says nothing until the daemon is closed. `hold(seconds, then)` waits before doing `then`.
"""
import shutil
import socket
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path

from kelmo.core import wire

ANSWER, CLOSE, RESET, SILENT = 'answer', 'close', 'reset', 'silent'


def hold(seconds, then):
    return ('hold', seconds, then)


class Connection:
    """`hello=False` reads the client's hello and closes without answering it. `gone` keeps the socket path
    unbound that many seconds after this connection ends, as a daemon between two lives."""

    def __init__(self, *calls, hello=True, gone=0.0):
        self.calls, self.hello, self.gone = list(calls), hello, gone


def default_answer(header, body, number):
    return {'answer': number}


class ScriptedDaemon:
    def __init__(self, *script, answer=default_answer):
        self.directory = Path(tempfile.mkdtemp(prefix='ks', dir='/tmp'))
        self.path = self.directory / 'd.sock'
        self.answer = answer
        self.script = list(script)
        self.calls = []
        self.connections = 0
        self.closing = threading.Event()
        self.server = self._listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _listen(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(8)
        return server

    @staticmethod
    def _frame(conn, reader):
        while True:
            data = conn.recv(65536)
            if not data:
                return None
            frames = reader.feed(data)
            if frames:
                return frames[0]

    def _serve(self):
        for connection in self.script:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            self._one(conn, connection)
            if connection.gone:
                self.server.close()
                self.path.unlink()
                time.sleep(connection.gone)
                self.server = self._listen()

    def _one(self, conn, connection):
        reader = wire.FrameReader()
        try:
            hello = self._frame(conn, reader)
            if hello is None or not connection.hello:
                return
            conn.sendall(wire.encode({'v': wire.VERSION, 'kind': 'hello', 'id': hello[0]['id'],
                                      'body_type': 'json', 'protocols': ['kelmo/2'], 'operations': []}))
            for action in connection.calls:
                frame = self._frame(conn, reader)
                if frame is None:
                    return
                self.calls.append(frame)
                if isinstance(action, tuple):
                    time.sleep(action[1])
                    action = action[2]
                if action == ANSWER:
                    header, body = frame
                    conn.sendall(wire.encode(*wire.reply(header['id'], self.answer(header, body, len(self.calls)))))
                    continue
                if action == RESET:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                elif action == SILENT:
                    self.closing.wait(60)
                return
        except OSError:
            return
        finally:
            conn.close()

    def close(self):
        self.closing.set()
        self.server.close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


def new_id():
    return uuid.uuid4().hex
