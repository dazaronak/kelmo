"""Sustained traffic from many clients: kelmod's own resident memory stays flat and small."""
import os
import threading
import time

from daemon_harness import RawClient

MB = 1024 * 1024
CLIENTS = 200


def test_two_hundred_clients_keep_kelmod_memory_flat(start):
    seconds = float(os.environ.get("KELMO_SOAK_SECONDS", "60"))
    daemon = start()
    began = time.monotonic()
    stop_at = began + seconds
    calls = [0] * CLIENTS
    failures = []

    def drive(index):
        try:
            client = RawClient(daemon.socket)
            while time.monotonic() < stop_at:
                header, _ = client.reply(client.call("state.echo", {"client": index}))
                if header["ok"] is not True:
                    failures.append(header["error"]["code"])
                calls[index] += 1
            client.close()
        except Exception as error:  # a client that dies fails the test instead of ending quietly
            failures.append(repr(error))

    threads = [threading.Thread(target=drive, args=(index,)) for index in range(CLIENTS)]
    for thread in threads:
        thread.start()
    samples = []
    while time.monotonic() - began < seconds:
        samples.append((time.monotonic() - began, daemon.own_memory()))
        time.sleep(1)
    for thread in threads:
        thread.join()
    samples.append((time.monotonic() - began, daemon.own_memory()))

    baseline_at = min(10.0, seconds / 2)  # the 10th second; a run shorter than 20 s uses its middle
    baseline = next(memory for at, memory in samples if at >= baseline_at)
    final = samples[-1][1]
    assert failures == []
    assert all(count > 0 for count in calls)
    assert final - baseline <= 5 * MB, samples
    assert max(memory for _, memory in samples) < 30 * MB, samples
