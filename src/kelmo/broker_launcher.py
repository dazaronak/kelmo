"""Bind an owner-selected stdio embedding broker to one command's lifetime."""
import os
import re
import signal
import subprocess

from .daemon import validate_argv
from .errors import KelmoError


class _Interrupted(Exception):
    def __init__(self, number):
        self.number = number


def _stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader can exit before descendants that still own the pipe or ignore TERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run(broker_command, child_command, *, fds_env="KELMO_EMBED_FDS"):
    validate_argv(broker_command, "E_INPUT")
    validate_argv(child_command, "E_INPUT")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*_FDS", fds_env):
        raise KelmoError("E_INPUT", "Choose an uppercase channel variable ending in _FDS.")
    broker = child = None
    handlers = {}

    def interrupt(number, frame):
        if child is None:
            raise _Interrupted(number)
        # The wrapped command owns its shutdown: it gets the signal and the broker serves it until it exits.
        try:
            os.killpg(child.pid, number)
        except ProcessLookupError:
            pass

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, interrupt)
        broker = subprocess.Popen(broker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  start_new_session=True, bufsize=0)
        descriptors = (broker.stdin.fileno(), broker.stdout.fileno())
        environment = dict(os.environ)
        environment[fds_env] = ",".join(map(str, descriptors))
        child = subprocess.Popen(child_command, env=environment, pass_fds=descriptors,
                                 start_new_session=True)
        code = child.wait()
        return code if code >= 0 else 128 - code
    except _Interrupted as error:
        return 128 + error.number
    except OSError as error:
        raise KelmoError("E_BROKER", "The embedding broker or wrapped command could not be started.") from error
    finally:
        # Ignore repeated termination while reaping the two owned process groups.
        for number in handlers:
            signal.signal(number, signal.SIG_IGN)
        _stop(child)
        _stop(broker)
        if broker is not None:
            broker.stdin.close()
            broker.stdout.close()
        for number, handler in handlers.items():
            signal.signal(number, handler)
