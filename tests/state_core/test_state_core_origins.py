"""Enrolled clients are served under their own origin. Operations, traces and status are filtered by
origin, a request cannot name its origin, and a removed client or a fence is refused on its next
call."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from state_core_support import (capture, count_operations, operation_of, profile_path, request,
                                succeed)

from kelmo.config import Config
from kelmo.core.context import Context, Origin
from kelmo.errors import KelmoError
from test_action_state import APP_PAGE, world_factory as world_factory  # noqa: F401
from test_shared_context import profile_value, shared as shared  # noqa: F401


def mark_failed(core, operation):
    core.storage.write(lambda writer: writer.journal.stage(
        operation, 'failed', terminal=True, error_code='E_SYNTHETIC'))


def test_clients_interleave_writes_and_see_only_their_own_operations(shared, core_factory):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    a, b = core.profile.bindings.origin('mac-a'), core.profile.bindings.origin('mac-b')
    first_a = operation_of(core.client_request(a, capture('First from mac-a.', 'a-1.md', 'a-1')))
    first_b = operation_of(core.client_request(b, capture('First from mac-b.', 'b-1.md', 'b-1')))
    second_a = operation_of(core.client_request(a, capture('Second from mac-a.', 'a-2.md', 'a-2')))
    second_b = operation_of(core.client_request(b, capture('Second from mac-b.', 'b-2.md', 'b-2')))
    assert len({first_a, first_b, second_a, second_b}) == 4

    own = succeed(core.client_request(a, request('trace', {'operation_id': first_a})))
    assert own['operation']['id'] == first_a
    for mine, theirs in ((a, first_b), (b, first_a)):
        refused = core.client_request(mine, request('trace', {'operation_id': theirs}))
        assert refused['ok'] is False and refused['error']['code'] == 'E_NOT_FOUND'

    mark_failed(core, first_a)
    mark_failed(core, first_b)
    for origin, operation in ((a, first_a), (b, first_b)):
        failed = succeed(core.client_request(origin, request('status')))['failed_operations']
        assert [item['id'] for item in failed] == [operation]


def test_one_key_from_two_origins_makes_two_operations(shared, core_factory):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    a, b = core.profile.bindings.origin('mac-a'), core.profile.bindings.origin('mac-b')
    capture_a = operation_of(
        core.client_request(a, capture('Same key, mac-a.', 'same-a.md', 'shared-key')))
    capture_b = operation_of(
        core.client_request(b, capture('Same key, mac-b.', 'same-b.md', 'shared-key')))
    assert capture_a != capture_b

    tool_operations = {}
    for name in ('mac-a', 'mac-b'):
        arguments = {'content': f'Tool note from {name}.', 'project': 'app', 'filename': 'tool.md',
                     'targets': [APP_PAGE], 'idempotency_key': 'shared-tool-key'}
        context = Context.local('tool-' + name, 30, origin=Origin(name))
        reply = core.handle_tool(context, {'tool': 'memory_create', 'arguments': arguments})
        tool_operations[name] = reply['operation']['id']
    assert tool_operations['mac-a'] != tool_operations['mac-b']

    for mine, theirs in ((a, tool_operations['mac-b']), (b, tool_operations['mac-a']),
                         (a, capture_b), (b, capture_a)):
        refused = core.client_request(mine, request('trace', {'operation_id': theirs}))
        assert refused['ok'] is False and refused['error']['code'] == 'E_NOT_FOUND'


@pytest.mark.parametrize('frame', [
    request('status', origin={'client_id': 'mac-b'}),
    request('status', {'client_id': 'mac-b'}),
    request('capture', {'content': 'x', 'filename': 'x.md', 'role': 'collector',
                        'origin': {'client_id': 'mac-b'}}, key='k'),
])
def test_a_request_that_names_its_origin_or_extra_fields_is_refused(shared, core_factory, frame):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    before = count_operations(core)
    reply = core.client_request(core.profile.bindings.origin('mac-a'), frame)
    assert reply['ok'] is False and reply['error']['code'] == 'E_PROTOCOL'
    assert count_operations(core) == before


def test_a_removed_client_is_refused_and_a_fence_refuses_only_writes(shared, core_factory):
    value = profile_value(shared.world, shared.tmp_path)
    path = profile_path(shared, value)
    core = core_factory(shared.world.root, profile_path=path)
    a, b = core.profile.bindings.origin('mac-a'), core.profile.bindings.origin('mac-b')
    operation_of(core.client_request(b, capture('Before removal.', 'before.md', 'before')))

    value['clients'] = [item for item in value['clients'] if item['client_id'] != 'mac-b']
    path.write_text(json.dumps(value))
    with pytest.raises(KelmoError) as error:
        core.client_request(b, request('status'))
    assert error.value.code == 'E_ORIGIN'

    value['fenced'] = True
    path.write_text(json.dumps(value))
    refused = core.client_request(a, capture('While fenced.', 'fenced.md', 'fenced'))
    assert refused['ok'] is False and refused['error']['code'] == 'E_FENCED'
    assert succeed(core.client_request(a, request('status')))['writer'] == 'shared'


def test_the_daemons_own_caller_is_local_on_a_server_vault_and_is_no_client(shared, core_factory):
    value = profile_value(shared.world, shared.tmp_path)
    path = profile_path(shared, value)
    core = core_factory(shared.world.root, profile_path=path)
    local = Origin('local')

    def context(name, origin):
        return Context.local(name, 30, origin=origin)

    # The operator's CLI, jobs and status come with the daemon's own origin, which the profile does not enroll.
    assert core._operator(local) is True and core._operator(None) is True
    assert core._operator(Origin('mac-a')) is False
    status = core.handle_cli(context('operator-status', local),
                             {'argv': ['status', '--vault', str(shared.world.root), '--json']})
    assert isinstance(status, dict)
    assert 'schedule' in core.handle_scheduler_status(context('operator-jobs', local), {})

    # It is no client: what a client does there still needs an enrolled one.
    for refused in (lambda: core.client_request(local, request('status')),
                    lambda: core.handle_hook(context('local-hook', local), {'action': 'session-start'}),
                    lambda: core.handle_tool(context('local-tool', local), {'tool': 'memory_status'}),
                    lambda: core._operator(Origin('mac-unknown'))):
        with pytest.raises(KelmoError) as error:
            refused()
        assert error.value.code == 'E_ORIGIN'
    for handler, body in ((core.handle_cli, {'argv': ['status']}),
                          (core.handle_job_run, {'job': 'graph'}),
                          (core.handle_scheduler_status, {}),
                          (core.handle_embedding_health, {})):
        with pytest.raises(KelmoError) as error:
            handler(context('client-call', Origin('mac-a')), body)
        assert error.value.code == 'E_FORBIDDEN'

    # A fenced server runs no job and no CLI command that writes, whoever asks; it still answers reads.
    value['fenced'] = True
    path.write_text(json.dumps(value))
    with pytest.raises(KelmoError) as error:
        core.handle_job_run(context('operator-job', local), {'job': 'graph'})
    assert error.value.code == 'E_FENCED'
    vault = ['--vault', str(shared.world.root)]
    for argv in (['worker', 'run'], ['queue', 'retry', 'no-such-operation'], ['graph', 'rebuild']):
        with pytest.raises(KelmoError) as error:
            core.handle_cli(context('operator-write', local), {'argv': [*argv, *vault]})
        assert error.value.code == 'E_FENCED'
    for argv in (['status'], ['queue', 'list']):
        core.handle_cli(context('operator-read', local), {'argv': [*argv, *vault, '--json']})

    # A profile that enrolls a client named `local` serves that name as a client, not as the operator.
    value['fenced'] = False
    value['clients'] = value['clients'] + [{'client_id': 'local', 'machine_tag': 'server-itself'}]
    path.write_text(json.dumps(value))
    assert core._operator(local) is False


def test_operator_reads_are_answered_while_a_job_holds_the_writer(shared, core_factory):
    """A Dreaming pass holds the baton for minutes; `kelmo status` and the other reads must not wait for it."""
    import threading
    core = core_factory(shared.world.root)
    local = Origin('local')
    vault = ['--vault', str(shared.world.root), '--json']
    held, release = threading.Event(), threading.Event()

    def job():
        with core.storage.baton.hold():
            held.set()
            release.wait(60)
    holder = threading.Thread(target=job, daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        reads = (['status'], ['list', '--limit', '3'], ['search', 'synthetic', '--limit', '3'], ['queue', 'list'],
                 ['pages'], ['graph', 'stats'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            for argv in reads:
                future = pool.submit(core.handle_cli, Context.local('operator-read', 30, origin=local),
                                     {'argv': [*argv, *vault]})
                assert isinstance(future.result(timeout=20), (dict, list)), argv
            write = pool.submit(core.handle_cli, Context.local('operator-write', 30, origin=local),
                                {'argv': ['queue', 'retry', 'no-such-operation', *vault]})
            with pytest.raises(TimeoutError):
                write.result(timeout=1)  # a command that writes waits for the writer
            release.set()
            with pytest.raises(KelmoError):
                write.result(timeout=20)  # then runs: the operation does not exist
    finally:
        release.set()
        holder.join(10)


def test_twenty_clients_share_one_writer_and_at_most_four_readers(shared, core_factory):
    clients = [{'client_id': f'client-{index:02d}', 'machine_tag': f'machine-{index:02d}'}
               for index in range(20)]
    value = profile_value(shared.world, shared.tmp_path, clients=clients)
    core = core_factory(shared.world.root, profile_path=profile_path(shared, value))
    origins = [core.profile.bindings.origin(item['client_id']) for item in clients]
    with ThreadPoolExecutor(max_workers=20) as pool:
        replies = list(pool.map(
            lambda origin: core.client_request(origin, request('status')), origins))
    assert all(reply['ok'] is True for reply in replies)

    counts = core.status()
    assert counts['writer_services'] == 1
    assert 1 <= counts['reader_services'] <= 4

    config = Config.load(core.root)
    locks = []
    for name in ('.state/client-requests', '.state/client-sessions'):
        locks += config.internal(name).glob('*.lock')
    assert locks == []
