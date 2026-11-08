import multiprocessing

import pytest

from kelmo.errors import KelmoError
from kelmo.resolution import Resolution
from kelmo.service import Service
from kelmo.wiki import engine
from test_resolution import pending, resolved_changes

pytest_plugins = ['test_fact_checks']


def receive(channel):
    assert channel.poll(20), 'Fixture process did not reach its boundary'
    return channel.recv()


def finish(process):
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail('Fixture process did not finish')


def ownership_callback(root, action, job_id, token, channel):
    import kelmo.resolution as resolution
    with Service(root) as independent:
        queue = Resolution(independent)
        channel.send('ready')
        timestamp = channel.recv()
        resolution.time.time = lambda: timestamp
        flock = resolution.fcntl.flock

        def waiting(stream, flags):
            channel.send('locking')
            return flock(stream, flags)

        resolution.fcntl.flock = waiting
        if action == 'claim':
            result = queue.claim()
        elif action == 'fail':
            result = queue.fail(job_id, token, 'E_EXECUTOR', 1)
        else:
            result = queue.release_busy(job_id, token)
        channel.send(result)


@pytest.mark.parametrize('action,interrupted', [
    ('claim', False), ('fail', False), ('fail', True), ('release_busy', False),
])
def test_submit_serializes_ownership_callbacks(service, monkeypatch, action, interrupted):
    operation = pending(service)
    queue = Resolution(service)
    clock = [1000.0]
    monkeypatch.setattr('kelmo.resolution.time.time', lambda: clock[0])
    request = queue.claim(lease_seconds=1)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=ownership_callback,
                              args=(str(service.root), action, request['id'], request['token'], child))
    process.start()
    assert receive(parent) == 'ready'
    from kelmo import promotion
    declared, commit = promotion.declared_request, engine._commit_registry
    timestamp = 1002.0 if action == 'claim' else 1000.0
    callback_results = []

    def slow(*args, **kwargs):
        clock[0] = timestamp
        parent.send(timestamp)
        assert receive(parent) == 'locking'
        callback_results.append(receive(parent))
        return declared(*args, **kwargs)

    def publish(*args, **kwargs):
        commit(*args, **kwargs)
        if interrupted:
            raise SystemExit(99)

    def restore():
        monkeypatch.setattr(promotion, 'declared_request', declared)
        monkeypatch.setattr(engine, '_commit_registry', commit)

    def reclaim():
        clock[0] = 1002.0
        current = queue.claim()
        assert current['token'] != request['token']
        assert current['response'] == response
        restore()
        return current

    monkeypatch.setattr(promotion, 'declared_request', slow)
    monkeypatch.setattr(engine, '_commit_registry', publish)
    response = {'changes': resolved_changes(request)}
    try:
        if action == 'claim':
            # The lease ran out while this submission held ownership: the concurrent claim found the job
            # busy, and the editor refuses the expired claim before publishing anything.
            with pytest.raises(KelmoError) as raised:
                queue.submit(request['id'], request['token'], response)
            assert raised.value.code == 'E_CLAIM'
            current = reclaim()
            assert queue.submit(current['id'], current['token'], current['response'])['state'] == 'applied'
        elif interrupted:
            with pytest.raises(SystemExit):
                queue.submit(request['id'], request['token'], response)
            assert service.journal.get(operation['id'])['state'] == 'needs_resolution'
            current = reclaim()
            with pytest.raises(KelmoError) as raised:
                queue.submit(request['id'], request['token'], current['response'])
            assert raised.value.code == 'E_CLAIM'
            assert queue.submit(current['id'], current['token'], current['response'])['state'] == 'applied'
        else:
            assert queue.submit(request['id'], request['token'], response)['state'] == 'applied'
        assert callback_results == [False if action == 'release_busy' else None]
        assert engine.wiki_committed(service.root, wiki_root=service.config.wiki)
        current = service.journal.get(operation['id'])
        source = service.source_path(current).read_text(encoding='utf-8')
        assert source == request['payload']['source'] == service.show(current['archive_id'])['content']
        assert current['archive_id'] == operation['archive_id']
        assert service.status()['archive_records'] == 3
    finally:
        finish(process)

