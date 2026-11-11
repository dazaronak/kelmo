"""Stub core for the daemon black-box tests. kelmod starts it with its channel on descriptor 3.

    stub_core.py CORE SCRATCH [--crash-at-start] [--linger SECONDS]

CORE is `state` or `archive`. Each invocation a test checks is appended to SCRATCH/CORE.seen as a
JSON line, so a test reads from the files what the core really received, and in what order.
`--linger SECONDS` sleeps that long after LINK.serve() returns, then writes SCRATCH/CORE.finished
(content: the pid), so a test can tell whether the core got to finish its own shutdown.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from kelmo.core.corelink import CoreLink
from kelmo.errors import KelmoError

LINK = None  # set in main; the relay handler reaches the other core through it
KEPT = []  # memory held by the alloc operation until trim releases it


def client_id(context):
    return context.origin.client_id if context.origin else None


def record(scratch, core, op, body, context):
    entry = {"op": op, "pid": os.getpid(), "at": time.time(), "tag": body.get("tag"),
             "origin": client_id(context)}
    with (scratch / f"{core}.seen").open("a") as seen:
        seen.write(json.dumps(entry) + "\n")


def crash_handler(core, scratch):
    """Exits the process on the first invocation of this core, answers from the second on."""
    def crash(context, body):
        record(scratch, core, "crash", body, context)
        marker = scratch / f"{core}.crashed"
        if not marker.exists():
            marker.touch()
            os._exit(9)
        return {"recovered": True, "pid": os.getpid()}
    return crash


def relay(context, body):
    """Calls `body["op"]` through the link; the outcome returns as data, so a test can read its code."""
    future = LINK.call(body["op"], body.get("body", {}), context)
    try:
        result = future.result(timeout=context.remaining() + 5)
    except KelmoError as error:
        return {"ok": False, "code": error.code, "retryable": getattr(error, "retryable", None)}
    return {"ok": True, "result": result}


def handlers(core, scratch):
    def echo(context, body):
        return {"core": core, "pid": os.getpid(), "client_id": client_id(context), "body": body}

    def sleep(context, body):
        record(scratch, core, "sleep", body, context)
        end = time.monotonic() + float(body.get("seconds", 0))
        while time.monotonic() < end:
            context.check()
            time.sleep(0.01)
        context.check()
        return {"tag": body.get("tag"), "pid": os.getpid()}

    def alloc(context, body):
        KEPT.append(bytearray(b"\x01") * (int(body["mb"]) << 20))
        return {"mb": body["mb"], "pid": os.getpid()}

    def freeze(context, body):
        os.kill(os.getpid(), signal.SIGSTOP)
        return {}

    if core == "archive":
        table = {"echo": (echo, "fast"), "relay": (relay, "fast")}
    else:
        crash = crash_handler(core, scratch)
        table = {
            "echo": (echo, "fast"),
            "relay": (relay, "fast"),
            "sleep": (sleep, "work"),
            "crash_once": (crash, "work"),
            "crash_keyed": (crash, "work"),
            "crash_never": (crash, "work"),
            "alloc": (alloc, "fast"),
            "freeze": (freeze, "fast"),
        }
    return {f"{core}.{name}": pair for name, pair in table.items()}


def trim_handler(core, scratch):
    def trim():
        with (scratch / f"{core}.trim").open("a") as line:
            line.write("trim\n")
        KEPT.clear()
    return trim


def linger_seconds(argv):
    """The value of `--linger SECONDS`, or None when the option is absent."""
    if "--linger" not in argv:
        return None
    return float(argv[argv.index("--linger") + 1])


def main(argv):
    global LINK
    if "--crash-at-start" in argv:
        os._exit(3)
    core, scratch = argv[0], Path(argv[1])
    with (scratch / f"{core}.pids").open("a") as pids:
        pids.write(f"{os.getpid()}\n")
    trim = trim_handler(core, scratch) if core == "state" else None
    LINK = CoreLink.from_descriptor(3, handlers=handlers(core, scratch),
                                    lanes={"fast": 8, "work": 2}, trim=trim)
    LINK.serve()
    linger = linger_seconds(argv)
    if linger is not None:
        time.sleep(linger)
        (scratch / f"{core}.finished").write_text(f"{os.getpid()}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
