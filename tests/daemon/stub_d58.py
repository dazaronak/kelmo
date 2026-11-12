"""Focused daemon stub for D58 lifecycle and inherited-RLIMIT regressions."""
import json
import os
import resource
import sys
import time
from pathlib import Path

from kelmo.core.corelink import CoreLink


def marker(scratch, name):
    return scratch / name


def main(argv):
    if "--crash-at-start" in argv:
        os._exit(3)

    core, scratch = argv[0], Path(argv[1])
    options = set(argv[2:])
    with (scratch / f"{core}.pids").open("a") as pids:
        pids.write(f"{os.getpid()}\n")
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    marker(scratch, f"{core}.limits.json").write_text(json.dumps(limits))

    retained = []
    trigger = marker(scratch, f"{core}.soft-triggered")
    if core == "state" and "--soft-trigger-once" in options and not trigger.exists():
        trigger.touch()
        retained.append(bytearray(b"x" * (128 * 1024 * 1024)))

    def echo(context, body):
        return {"pid": os.getpid(), "body": body}

    def crash_once(context, body):
        path = marker(scratch, f"{core}.crashed-once")
        if not path.exists():
            path.touch()
            os._exit(9)
        return {"recovered": True}

    def crash_always(context, body):
        os._exit(9)

    handlers = {f"{core}.echo": (echo, "fast")}
    trim = None
    if core == "state":
        handlers.update({
            "state.crash_once": (crash_once, "work"),
            "state.crash_never": (crash_always, "work"),
        })

        def trim():
            marker(scratch, "state.trim-seen").touch()

    original_admin = CoreLink._admin

    def observed_admin(link, op, header):
        if op == "admin.recycle":
            with marker(scratch, f"{core}.recycles").open("a") as line:
                line.write("asked\n")
            if "--decline-recycle" in options:
                link._reply(header["id"], {"recycle": False, "busy": ["job"]})
            else:
                link._reply(header["id"], {"recycle": True})
            return
        if op == "admin.stop" and core == "state":
            marker(scratch, "state.admin-stop").touch()
            if "--hold-admin-stop" in options:
                release = marker(scratch, "state.release-admin-stop")
                while not release.exists():
                    time.sleep(0.01)
            if "--ignore-admin-stop" in options:
                return
        original_admin(link, op, header)

    CoreLink._admin = observed_admin
    link = CoreLink.from_descriptor(3, handlers=handlers,
                                    lanes={"fast": 8, "work": 2}, trim=trim)
    link.serve()
    linger = "--linger-after-stop"
    if linger in argv:
        time.sleep(float(argv[argv.index(linger) + 1]))
    marker(scratch, f"{core}.finished").write_text(str(os.getpid()))


if __name__ == "__main__":
    main(sys.argv[1:])
