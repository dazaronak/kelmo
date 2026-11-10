"""The archive core on its channel.

The calls a state core makes, the refusal of an answer the wire cannot carry, and reads that
are answered while a write is still open.
"""
import socket
import threading

from archive_core_support import (Peer, answered, capture_body, in_process_start,
                                  spawn_archive_core, stop_archive_core)

from kelmo.archive_adapter import LocalArchiveAdapter
from kelmo.contracts import ArchiveVersion, EvidenceRef
from kelmo.core.archive_core import serve
from kelmo.core.corelink import CoreLink


def test_archive_core_says_hello_and_answers_counts_capture_and_get(vault, tmp_path):
    parent, child = socket.socketpair()
    proc = spawn_archive_core(vault, child.fileno(), tmp_path / 'archive-core.log')
    child.close()
    peer = Peer(parent)
    try:
        hello, _ = peer.frame()
        assert hello['kind'] == 'hello'
        assert {'archive.counts', 'archive.capture', 'archive.get'} <= set(hello['operations'])
        before = answered(peer, 'archive.counts', {})['archive_records']
        text = 'Synthetic note for the archive core channel check.'
        body = capture_body(vault, text, 'channel-1')
        ref = EvidenceRef.from_dict(answered(peer, 'archive.capture', body))
        assert answered(peer, 'archive.counts', {})['archive_records'] == before + 1
        got = answered(peer, 'archive.get', {'archive_id': ref.archive_id})
        version = ArchiveVersion.from_dict(got)
        assert version.evidence == ref
        assert version.source.content == text.encode('utf-8')
    finally:
        parent.close()
        stop_archive_core(proc)


def test_a_result_the_wire_cannot_carry_is_answered_with_e_processing():
    parent, child = socket.socketpair()

    def broken(context, body):
        return object()

    link = CoreLink.from_descriptor(child.detach(),
                                    handlers={'archive.broken': (broken, 'read')},
                                    lanes={'read': 1})
    serving = threading.Thread(target=link.serve, daemon=True)
    serving.start()
    peer = Peer(parent)
    try:
        hello, _ = peer.frame()
        assert hello['operations'] == ['archive.broken']
        header, _ = peer.call('archive.broken', {})
        assert header['ok'] is False and header['error']['code'] == 'E_PROCESSING'
    finally:
        parent.close()
        serving.join(30)
    assert not serving.is_alive()


def test_reads_are_answered_while_a_large_capture_is_still_open(
        vault, tmp_path, offline_embedding, monkeypatch):
    # The capture waits inside the write lane until the reads have answered. That hold is the
    # only injected delay; the finally block releases it whatever happens.
    gate, started, finished = threading.Event(), threading.Event(), threading.Event()
    capture = LocalArchiveAdapter.capture

    def held(self, source, origin, idempotency_key):
        started.set()
        gate.wait(60)
        try:
            return capture(self, source, origin, idempotency_key)
        finally:
            finished.set()

    monkeypatch.setattr(LocalArchiveAdapter, 'capture', held)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    parent, child = socket.socketpair()
    start = in_process_start(vault, scratch)
    serving = threading.Thread(target=serve, args=(start,),
                               kwargs={'confined': False, 'descriptor': child.detach()},
                               daemon=True)
    serving.start()
    peer = Peer(parent)
    # About 720 KB of text, under the one MiB limit on a source packet.
    text = 'Synthetic line of a large capture. ' * 20000
    try:
        peer.frame()
        before = answered(peer, 'archive.counts', {})['archive_records']
        write = peer.send('archive.capture', capture_body(vault, text, 'large-capture'))
        assert started.wait(30)
        reads = [peer.send('archive.counts', {}) for _ in range(4)]
        for read in reads:
            header, body = peer.reply(read)
            assert header['ok'] is True and body['archive_records'] == before
        assert not finished.is_set()
        gate.set()
        header, _ = peer.reply(write)
        assert header['ok'] is True
        assert answered(peer, 'archive.counts', {})['archive_records'] == before + 1
    finally:
        gate.set()
        parent.close()
        serving.join(60)
