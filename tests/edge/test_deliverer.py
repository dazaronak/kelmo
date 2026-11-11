"""The deliverer keeps running when a spool pass raises something other than a KelmoError."""


def test_deliverer_keeps_running_after_an_unreadable_spool(edge, monkeypatch):
    core, client, _ = edge
    ingest = core.ingest_spool
    failures = []

    def unreadable_once():
        if not failures:
            failures.append(True)
            raise OSError('spool unreadable')
        return ingest()

    monkeypatch.setattr(core, 'ingest_spool', unreadable_once)
    client.flushed.clear()
    core._wake.set()
    assert client.flushed.wait(10), 'the deliverer ended on the spool error'
    assert core._deliverer.is_alive()
    client.flushed.clear()
    core._wake.set()
    assert client.flushed.wait(10), 'the deliverer ran no later pass'
    assert core._deliverer.is_alive()
