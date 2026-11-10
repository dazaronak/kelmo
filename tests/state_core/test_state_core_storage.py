"""The state core's storage. Every read runs on a reader, and a reader sees only committed rows.
A keyed write applies once however often it is sent. The writer connection is guarded by the baton,
and a create that waits does not hold another client's write."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from daemon_harness import wait_for
from state_core_support import capture, count_operations, profile_path, writes_on_reader
from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

from kelmo.core.baton import BatonError
from kelmo.core.context import Context, Origin
from kelmo.core.state_core import READS, Routed
from kelmo.errors import KelmoError

# Each state core read, called through Routed; `found` names one captured and one archived record.
READ_CALLS = {
    'page_index': lambda r, found: r.page_index(),
    'page_facts': lambda r, found: r.page_facts(APP_PAGE),
    'editor_context': lambda r, found: r.editor_context('app', 20000),
    'graph_query': lambda r, found: r.graph_query(action='search', query='shared-feature'),
    'graph_stats': lambda r, found: r.graph_stats(),
    'graph_read': lambda r, found: r.graph_read('projects/app/shared-feature'),
    'source_path': lambda r, found: r.source_path(r.trace(found.operation)['operation']),
    'source_packet': lambda r, found: r.source_packet(found.operation),
    'queued': lambda r, found: r.queued(),
    'queue': lambda r, found: r.queue(),
    'orphan_sources': lambda r, found: r.orphan_sources(),
    'trace': lambda r, found: r.trace(found.operation),
    'failed_operations': lambda r, found: r.failed_operations(),
    'status': lambda r, found: r.status(),
    'archive_record': lambda r, found: r.archive_record(found.archive_id),
    'show': lambda r, found: r.show(found.archive_id),
    'doctor': lambda r, found: r.doctor(),
    'list': lambda r, found: r.list(),
    'search': lambda r, found: r.search('Archived'),
    'compat_metadata': lambda r, found: r.compat_metadata(found.archive_id),
}


def count_named(core, filename):
    return core.storage.read(lambda reader: reader.journal.conn.execute(
        'SELECT COUNT(*) FROM operations WHERE filename=?', (filename,)).fetchone()[0])


def answered_within(seconds, call):
    """The value of `call`, which must return within `seconds`; its own error is raised here."""
    outcome = {}

    def run():
        try:
            outcome['value'] = call()
        except BaseException as error:
            outcome['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), 'the call waited on the writer transaction'
    if 'error' in outcome:
        raise outcome['error']
    return outcome['value']


def test_every_read_runs_on_a_reader_without_a_write(world_factory, core_factory):
    world = world_factory()
    ref = world.archive('Archived note for the reader check.', principal='local',
                        key='reader-check')
    operation = world.service.journal.conn.execute(
        'SELECT id FROM operations WHERE filename=?', ('reader-check.md',)).fetchone()[0]
    found = SimpleNamespace(operation=operation, archive_id=ref.archive_id)
    reader = Routed(core_factory(world.root).storage)

    assert set(READ_CALLS) == READS
    for name, call in READ_CALLS.items():
        try:
            call(reader, found)
        except KelmoError as error:
            assert not writes_on_reader(error), name


def test_reader_answers_and_sees_only_commits(world_factory, core_factory):
    core = core_factory(world_factory().root)
    before = count_operations(core)
    opened, release = threading.Event(), threading.Event()

    def hold(writer):
        connection = writer.journal.conn
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'INSERT INTO operations (id, request_key, source_hash, source_path, filename, role, '
            'intent, state, terminal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('open-op', 'open-key', 'a' * 64, 'open', 'open.md', 'implementer', 'archive',
             'captured', 0, '2026-11-10T00:00:00+00:00'))
        opened.set()
        release.wait(10)
        connection.commit()

    writer = threading.Thread(target=core.storage.write, args=(hold,))
    writer.start()
    try:
        assert opened.wait(10)
        during = answered_within(10, lambda: count_operations(core))
    finally:
        release.set()
        writer.join(10)
    assert during == before
    assert count_operations(core) == before + 1


def test_fifty_concurrent_sends_of_one_keyed_write_apply_it_once(shared, core_factory):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    origin = core.profile.bindings.origin('mac-a')
    frame = capture('Sent fifty times at once.', 'once.md', 'once')
    before = count_operations(core)
    with ThreadPoolExecutor(max_workers=50) as pool:
        replies = list(pool.map(lambda _: core.client_request(origin, frame), range(50)))

    for reply in replies:
        if reply['ok']:
            continue
        assert reply['error']['code'] == 'E_BUSY' and reply['error']['retryable'] is True, reply
    assert len([reply for reply in replies if reply['ok'] and not reply.get('replayed')]) == 1
    retry = core.client_request(origin, frame)
    assert retry['ok'] is True and retry['replayed'] is True
    assert count_operations(core) == before + 1


def test_the_writer_connection_is_refused_outside_the_baton(world_factory, core_factory):
    core = core_factory(world_factory().root)
    with pytest.raises(BatonError):
        _ = core.storage.writer.journal.conn
    rows = core.storage.write(
        lambda writer: writer.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0])
    assert rows == 0


def test_a_create_that_waits_does_not_hold_another_clients_write(shared, core_factory):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    arguments = {'content': 'A note that waits for its operation.', 'project': 'app',
                 'filename': 'waiting.md', 'targets': [APP_PAGE], 'idempotency_key': 'wait-a',
                 'wait': 5}
    waiting = ThreadPoolExecutor(max_workers=1)
    try:
        context = Context.local('wait-a', 60, origin=Origin('mac-a'))
        pending = waiting.submit(core.handle_tool, context,
                                 {'tool': 'memory_create', 'arguments': arguments})
        wait_for(lambda: count_named(core, 'waiting.md') == 1, timeout=10,
                 what='the waiting create')

        started = time.monotonic()
        reply = core.client_request(core.profile.bindings.origin('mac-b'),
                                    capture('B writes while A waits.', 'other.md', 'other-b'))
        elapsed = time.monotonic() - started
        assert reply['ok'] is True and elapsed < 2
        assert pending.result(timeout=30)['operation']['id']
    finally:
        waiting.shutdown(wait=True)
