"""Pure trust checks; runtime cases below are reserved for the controller."""
import pytest


def trust_contract():
    import importlib.util
    assert importlib.util.find_spec('kelmo.server') is not None, 'trusted origin boundary is absent'


def test_payload_cannot_enroll_itself_or_claim_requester():
    trust_contract()
    from kelmo.server import Enrollment, OriginBindings, WriterIdentity
    from kelmo.contracts import Origin
    bindings = OriginBindings('authority', WriterIdentity('authority', 'writer', 1),
                              (Enrollment('local', 'fixture-node'),))
    origin = bindings.origin('local', 'session')
    assert origin == Origin('authority', 'local', 'fixture-node', 'session')
    with pytest.raises(ValueError):
        bindings.validate(Origin.from_dict(origin.to_dict()))
    with pytest.raises(ValueError):
        bindings.origin('unenrolled')
    with pytest.raises(ValueError):
        Origin.from_dict(dict(origin.to_dict(), evidence_mark='REQUESTER'))
    with pytest.raises(ValueError):
        bindings.require_requester(origin)


def test_controller_bound_requester_and_repository_are_scoped():
    trust_contract()
    from kelmo.server import Enrollment, OriginBindings, RepositoryBinding, WriterIdentity
    bindings = OriginBindings('authority', WriterIdentity('authority', 'writer', 4),
                              (Enrollment('human-input', 'fixture-node', requester=True,
                                          repository_ids=('repo',)),),
                              (RepositoryBinding('repo', '/synthetic/repository'),))
    origin = bindings.origin('human-input')
    bindings.require_requester(origin)
    assert bindings.repository(origin, 'repo').root == '/synthetic/repository'
    with pytest.raises(ValueError):
        bindings.repository(origin, 'payload-path')
    with pytest.raises(ValueError):
        bindings.check_writer(WriterIdentity('authority', 'other-writer', 4))
    with pytest.raises(ValueError):
        bindings.check_writer(WriterIdentity('authority', 'writer', 3))


def test_authorities_cannot_reuse_issued_origin():
    trust_contract()
    from kelmo.server import Enrollment, OriginBindings, WriterIdentity
    left = OriginBindings('left', WriterIdentity('left', 'writer', 1), (Enrollment('local', 'node'),))
    right = OriginBindings('right', WriterIdentity('right', 'writer', 1), (Enrollment('local', 'node'),))
    with pytest.raises(ValueError):
        right.validate(left.origin('local'))


class TestRuntimeArchiveBoundary:
    """Controller-only SQLite/native cases; never selected by the pure command."""

    @pytest.fixture
    def service(self, tmp_path, monkeypatch):
        from kelmo.service import Service
        monkeypatch.setenv('GIT_AUTHOR_NAME', 'Synthetic Fixture')
        monkeypatch.setenv('GIT_COMMITTER_NAME', 'Synthetic Fixture')
        monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'fixture@example.invalid')
        monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'fixture@example.invalid')
        root = tmp_path / 'vault'
        Service.initialize(root, 'fixture-node', project='fixture')
        with Service(root) as service:
            yield service

    @staticmethod
    def missing_model(monkeypatch):
        from kelmo.store.errors import ArchiveEmbeddingError
        def absent(*args, **kwargs):
            raise ArchiveEmbeddingError('synthetic unavailable model')
        monkeypatch.setattr('kelmo.store.memory.store.embed', absent)
        monkeypatch.setattr('kelmo.store.memory.search.embed', absent)

    def test_runtime_real_injected_adapter_captures_text_and_reopens(self, service, monkeypatch):
        from kelmo.archive_adapter import LocalArchiveAdapter
        from kelmo.contracts import ArchiveAPI
        from kelmo.service import Service
        self.missing_model(monkeypatch)
        root = service.root
        bindings = service.config.local_bindings()
        adapter = LocalArchiveAdapter(service.config.archive, service.config.cache, bindings)
        assert isinstance(adapter, ArchiveAPI)
        def prohibited(*args, **kwargs):
            raise AssertionError('Service constructor must not open archive when injected')
        with monkeypatch.context() as injection_guard:
            injection_guard.setattr('kelmo.service.open_connection', prohibited)
            with Service(root, archive_api=adapter, bindings=bindings) as injected:
                assert injected.archive_api is adapter
                assert injected.archive is adapter.connection
                op = injected.capture('needle original\r\n', '2026-10-01_note.md', 'implementer')
                result = injected.run(op['id'])
                assert result['state'] == 'archived' and result['durable_stage'] == 'archived'
                ref = adapter.get(result['archive_id']).evidence
                version = adapter.verify_receipt(ref, op['id'], injected._operation_origin(op['id']))
                assert version.source.content == b'needle original\r\n' and version.vectors_pending is True
                assert version.source.valid_from == '2026-10-01'
                assert injected.journal.evidence_record(op['id']) == ref.to_dict()
        with Service(root) as reopened:
            hit = reopened.search('needle', with_metadata=True)
            assert hit['search'] == {'mode': 'lexical', 'degraded': True, 'reason': 'embedding_unavailable'}
            assert [row['id'] for row in hit['items']] == [ref.archive_id]
            assert reopened.show(ref.archive_id)['content'] == 'needle original\r\n'
            assert reopened.capture('needle original\r\n', '2026-10-01_note.md', 'implementer')['id'] == op['id']
            assert reopened.status()['archive_records'] == 1

    def test_runtime_original_source_packet_survives_archive_and_restart(self, service, monkeypatch):
        from kelmo.contracts import SourcePacket
        from kelmo.service import Service
        self.missing_model(monkeypatch)
        packet = SourcePacket(b'{"exit_code":0,"message":"pushed"}\r\n', 'git_receipt',
                              'producer:stable:17', 'receipt.md', '2026-10-13T22:10:00+02:00',
                              kind='milestone', project='fixture', tags=('producer',),
                              valid_from='2026-10-13')
        operation = service.capture_source(packet, service.origin, 'original-packet')
        assert service.source_packet(operation['id']) == packet
        archived = service.run(operation['id'])
        assert archived['state'] == 'archived'
        assert service.archive_api.get(archived['archive_id']).source == packet
        with Service(service.root) as reopened:
            assert reopened.source_packet(operation['id']) == packet
            assert reopened.capture_source(packet, reopened.origin, 'original-packet')['id'] == operation['id']

    def test_runtime_source_packet_replay_checks_metadata_and_trusted_origin(self, service):
        from dataclasses import replace
        from kelmo.contracts import Origin, SourcePacket
        from kelmo.errors import KelmoError
        packet = SourcePacket(b'exact receipt\n', 'git_receipt', 'event-1', 'receipt.md',
                              '2026-10-13T20:10:00Z', project='fixture')
        operation = service.capture_source(packet, service.origin, 'same-packet')
        for changed in (replace(packet, source_id='event-2'),
                        replace(packet, observed_at='2026-10-14T20:10:00Z'),
                        replace(packet, tags=('changed',))):
            with pytest.raises(KelmoError) as refused:
                service.capture_source(changed, service.origin, 'same-packet')
            assert refused.value.code == 'E_IDEMPOTENCY'
        before = sorted(path.name for path in service.config.internal('.state/sources').iterdir())
        for unissued in (None, Origin.from_dict(service.origin.to_dict())):
            with pytest.raises(KelmoError) as refused:
                service.capture_source(replace(packet, content=b'forged origin\n'), unissued, 'forged')
            assert refused.value.code == 'E_ORIGIN'
        assert sorted(path.name for path in service.config.internal('.state/sources').iterdir()) == before
        assert service.source_packet(operation['id']) == packet

    def test_runtime_capture_preserves_the_callers_unfinished_transaction(self, service):
        from kelmo.contracts import SourcePacket
        from kelmo.errors import KelmoError
        connection = service.journal.conn
        connection.execute('CREATE TABLE caller_sentinel (value TEXT)')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO caller_sentinel VALUES (?)', ('uncommitted',))
        spool = service.config.internal('.state/sources')
        existed, before = spool.exists(), sorted(spool.glob('*'))
        packet = SourcePacket(b'exact source\n', 'git_receipt', 'event-1', 'receipt.md',
                              '2026-10-14T12:00:00Z')
        try:
            with pytest.raises(KelmoError) as refused:
                service.capture_source(packet, service.origin, 'caller-owned')
            assert refused.value.code == 'E_STATE' and connection.in_transaction
            assert connection.execute('SELECT value FROM caller_sentinel').fetchone()[0] == 'uncommitted'
            assert sorted(spool.glob('*')) == before and spool.exists() == existed
        finally:
            connection.rollback()
        assert connection.execute('SELECT COUNT(*) FROM caller_sentinel').fetchone()[0] == 0

    def test_runtime_changed_source_metadata_cannot_be_archived(self, service, monkeypatch):
        import json
        from kelmo.contracts import SourcePacket
        from kelmo.errors import KelmoError
        self.missing_model(monkeypatch)
        packet = SourcePacket(b'original event\n', 'git_receipt', 'event-1', 'receipt.md',
                              '2026-10-13T20:10:00Z')
        operation = service.capture_source(packet, service.origin, 'packet-integrity')
        metadata = service.journal.source_metadata(operation['id'])['metadata']
        metadata['packet']['source_id'] = 'another-event'
        with service.journal.conn:
            service.journal.conn.execute('UPDATE operation_sources SET metadata=? WHERE operation_id=?',
                                         (json.dumps(metadata), operation['id']))
        with pytest.raises(KelmoError) as refused:
            service.source_packet(operation['id'])
        assert refused.value.code == 'E_SOURCE'
        failed = service.run(operation['id'])
        assert failed['state'] == 'failed' and failed['error_code'] == 'E_SOURCE'
        assert service.status()['archive_records'] == 0

    def test_runtime_legacy_capture_keeps_the_packet_behind_its_own_receipt(self, service, monkeypatch):
        self.missing_model(monkeypatch)
        source = '---\nproject: fixture\ndomain: dev\ntags: [original]\n---\nEvidence.\r\n'
        operation = service.capture(source, '2026-10-13_note.md', 'implementer')
        archived = service.run(operation['id'])
        assert archived['state'] == 'archived'
        packet = service.source_packet(operation['id'])
        assert packet.content == source.encode() and packet.source_kind == 'raw_note'
        assert packet.source_id == operation['source_hash'] and packet.project == 'fixture'
        assert packet.observed_at == operation['created_at'] and packet.valid_from == '2026-10-13'
        assert packet.tags == ('fixture-node', 'role:implementer', 'original')
        assert service.archive_api.get(archived['archive_id']).source == packet

    def test_runtime_archive_commit_before_journal_stage_reuses_real_receipt(self, service, monkeypatch):
        from kelmo.service import Service
        self.missing_model(monkeypatch)
        op = service.capture('needle crash evidence\n', 'crash.md', 'implementer')
        # The archive committed, but the coordinator has not saved its archived stage.
        committed = service._archive(op, 'needle crash evidence\n')
        assert service.journal.get(op['id'])['archive_id'] is None
        def no_second_embedding(*args, **kwargs):
            raise AssertionError('archive receipt must replay without embedding')
        monkeypatch.setattr('kelmo.store.memory.store.embed', no_second_embedding)
        with Service(service.root) as reopened:
            result = reopened.run(op['id'])
            assert result['state'] == 'archived' and result['archive_id'] == committed['id']
            assert reopened.status()['archive_records'] == 1

    def test_runtime_hidden_evidence_requires_exact_owner_origin_and_version(self, service, monkeypatch):
        from dataclasses import replace
        from kelmo.contracts import Origin
        from kelmo.errors import KelmoError
        self.missing_model(monkeypatch)
        op = service.capture('needle retained evidence\n', 'retained.md', 'implementer')
        result = service.run(op['id'])
        version = service.archive_api.get(result['archive_id'])
        origin = service._operation_origin(op['id'])
        assert service.forget(result['archive_id'])['forgotten'] is True
        for read in (lambda: service.show(result['archive_id']), lambda: service.archive_api.get(result['archive_id'])):
            with pytest.raises(KelmoError) as refused:
                read()
            assert refused.value.code == 'E_FORGOTTEN'
        assert service.archive_api.verify_receipt(version.evidence, op['id'], origin).source.content == b'needle retained evidence\n'
        for ref, owner, principal in [(replace(version.evidence, version=2), op['id'], origin),
                                      (version.evidence, 'wrong-owner', origin),
                                      (version.evidence, op['id'], Origin.from_dict(origin.to_dict()))]:
            with pytest.raises(KelmoError) as refused:
                service.archive_api.verify_receipt(ref, owner, principal)
            assert refused.value.code in ('E_RECEIPT', 'E_ORIGIN')
        assert service.search('needle') == [] and service.list()['items'] == []
        assert service.archive_record(result['archive_id'], operation_id=op['id'])['content'] == 'needle retained evidence\n'

    def test_runtime_collector_opt_in_and_legacy_supersession_admission_survive(self, service, monkeypatch):
        from kelmo.errors import KelmoError
        from kelmo.wiki import engine
        self.missing_model(monkeypatch)
        collector = service.capture('collector evidence\n', 'collector.md', 'collector')
        assert service.run(collector['id'])['state'] == 'captured'
        assert service.status()['archive_records'] == 0
        assert engine.load_registry(service.root)['version'] == engine.REGISTRY_VERSION
        old = service.run(service.capture('needle old\n', 'old.md', 'implementer')['id'])
        correction = service.capture('needle new\n', 'new.md', 'implementer', supersedes=old['archive_id'])
        failed = service.run(correction['id'])
        assert failed['state'] == 'failed' and failed['error_code'] == 'E_EMBEDDING'
        assert service.show(old['archive_id'])['content'] == 'needle old\n'
        monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *args, **kwargs: bytes(1536))
        corrected = service.run(service.retry(correction['id'])['id'])
        assert corrected['state'] == 'archived'
        assert [item['id'] for item in service.search('needle')] == [corrected['archive_id']]
        with pytest.raises(KelmoError) as refused:
            service.show(old['archive_id'])
        assert refused.value.code == 'E_FORGOTTEN'

    def test_runtime_wiki_failure_retry_verifies_hidden_receipt_and_publishes_once(self, service, monkeypatch):
        import json
        from kelmo.wiki import engine
        self.missing_model(monkeypatch)
        check = service.reconcile('Synthetic accepted fact.', 'projects/fixture/fixture.md')
        changes = [dict(fact_key='fixture.example', target='projects/fixture/fixture.md',
                        text='Synthetic accepted fact.', status='active', expected_revision=None, replaces=[])]
        checks = [dict(fact_key='fixture.example', verified_match=dict(result='not_found', search_id=check['search_id']))]
        content = '```wiki-changes\n' + json.dumps(changes) + '\n```\n```fact-checks\n' + json.dumps(checks) + '\n```\n'
        op = service.capture(content, 'promote.md', 'lead', 'promote')
        commit = engine._commit_registry
        def failed_commit(*args, **kwargs):
            raise RuntimeError('synthetic publisher interruption')
        monkeypatch.setattr(engine, '_commit_registry', failed_commit)
        failed = service.run(op['id'])
        assert failed['state'] == 'failed' and failed['durable_stage'] == 'archived'
        service.forget(failed['archive_id'])
        monkeypatch.setattr(engine, '_commit_registry', commit)
        def no_second_embedding(*args, **kwargs):
            raise AssertionError('retry must not store again')
        monkeypatch.setattr('kelmo.store.memory.store.embed', no_second_embedding)
        final = service.run(service.retry(op['id'])['id'])
        assert final['state'] == 'applied' and final['archive_id'] == failed['archive_id']
        assert service.status()['facts'] == 1 and service.status()['archive_records'] == 1
        assert service.run(op['id'])['state'] == 'applied'

    def test_runtime_storage_error_rolls_back_text_fts_and_receipt(self, service, monkeypatch):
        import sqlite3
        self.missing_model(monkeypatch)
        service.archive.execute('DROP TABLE memories_fts')
        service.archive.commit()
        op = service.capture('broken storage evidence\n', 'broken.md', 'implementer')
        with pytest.raises(sqlite3.OperationalError):
            service._archive(op, 'broken storage evidence\n')
        assert service.status()['archive_records'] == 0
        assert service.archive.execute('SELECT COUNT(*) FROM archive_receipts').fetchone()[0] == 0

    def test_runtime_stale_writer_epoch_cannot_archive(self, service, monkeypatch):
        from kelmo.errors import KelmoError
        self.missing_model(monkeypatch)
        op = service.capture('stale writer evidence\n', 'stale.md', 'implementer')
        service.archive.execute('UPDATE archive_writer_state SET epoch=epoch+1')
        service.archive.commit()
        with pytest.raises(KelmoError) as refused:
            service._archive(op, 'stale writer evidence\n')
        assert refused.value.code == 'E_WRITER'
        with pytest.raises(KelmoError) as capture_refused:
            service.capture('foreign writer evidence\n', 'foreign.md', 'implementer')
        assert capture_refused.value.code == 'E_WRITER'
        assert service.status()['archive_records'] == 0

    def test_runtime_supersession_committed_before_journal_stage_recovers(self, service, monkeypatch):
        from kelmo.service import Service
        self.missing_model(monkeypatch)
        old = service.run(service.capture('needle previous\n', 'previous.md', 'implementer')['id'])
        monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *args, **kwargs: bytes(1536))
        op = service.capture('needle correction\n', 'correction.md', 'implementer', supersedes=old['archive_id'])
        stored = service._archive(op, 'needle correction\n')
        service._supersede(op['id'], stored['id'], old['archive_id'])
        assert service.journal.get(op['id'])['archive_id'] is None
        def no_second_embedding(*args, **kwargs):
            raise AssertionError('own receipt must recover after supersession')
        monkeypatch.setattr('kelmo.store.memory.store.embed', no_second_embedding)
        with Service(service.root) as reopened:
            result = reopened.run(op['id'])
            assert result['state'] == 'archived' and result['archive_id'] == stored['id']
            assert reopened.status()['archive_records'] == 2
            assert [item['id'] for item in reopened.search('needle')] == [stored['id']]

    def test_runtime_legacy_owning_receipt_is_bound_without_reembedding(self, service, monkeypatch):
        import json
        from kelmo.service import Service
        self.missing_model(monkeypatch)
        op = service.capture('needle legacy source\n', 'legacy.md', 'implementer')
        result = service.run(op['id'])
        # A pre-adoption fixture has a journal archive_id, but no exact receipt/origin tables populated.
        with service.archive:
            service.archive.execute('DELETE FROM archive_receipts')
            service.archive.execute('DELETE FROM archive_sources')
            service.archive.execute('DELETE FROM archive_version_sources')
        with service.journal.conn:
            service.journal.conn.execute('DELETE FROM operation_origins')
            service.journal.conn.execute('DELETE FROM operation_sources')
            for event in service.trace(op['id'])['events']:
                event['details'].pop('evidence_ref', None)
                service.journal.conn.execute('UPDATE events SET details=? WHERE sequence=?',
                                             (json.dumps(event['details']), event['sequence']))
        with Service(service.root) as reopened:
            assert reopened.archive_record(result['archive_id'], operation_id=op['id'])['content'] == 'needle legacy source\n'
            assert reopened.archive_api.capture_receipt(op['id'], reopened._operation_origin(op['id'])).source_id == op['source_hash']
            assert reopened.status()['archive_records'] == 1

    def test_runtime_future_versioned_rows_are_not_fabricated_as_version_one(self, service, monkeypatch):
        from kelmo.errors import KelmoError
        self.missing_model(monkeypatch)
        result = service.run(service.capture('future version source\n', 'future.md', 'implementer')['id'])
        with service.archive:
            service.archive.execute('UPDATE memories SET version=2 WHERE id=?', (result['archive_id'],))
        with pytest.raises(KelmoError) as refused:
            service.archive_api.get(result['archive_id'], 1)
        assert refused.value.code == 'E_CAPABILITY'

    def test_runtime_pending_plain_record_cannot_supersede_until_indexed(self, service, monkeypatch):
        self.missing_model(monkeypatch)
        old = service.run(service.capture('needle original target\n', 'target.md', 'implementer')['id'])
        plain = service.run(service.capture('needle pending replacement\n', 'pending.md', 'implementer')['id'])
        assert service.archive_api.get(plain['archive_id']).vectors_pending is True
        correction = service.capture('needle pending replacement\n', 'pending.md', 'implementer', supersedes=old['archive_id'])
        failed = service.run(correction['id'])
        assert failed['state'] == 'failed' and failed['error_code'] == 'E_EMBEDDING'
        assert service.show(old['archive_id'])['content'] == 'needle original target\n'
        monkeypatch.setattr('kelmo.store.memory.store.embed', lambda *args, **kwargs: bytes(1536))
        final = service.run(service.retry(correction['id'])['id'])
        assert final['state'] == 'archived' and final['archive_id'] == plain['archive_id']
        assert service.archive_api.get(plain['archive_id']).vectors_pending is False
        assert service.status()['archive_records'] == 2

    def test_runtime_concurrent_hide_reuses_receipt_after_writer_lock(self, service, monkeypatch):
        from kelmo.archive_adapter import LocalArchiveAdapter
        self.missing_model(monkeypatch)
        record = service.run(service.capture('hide race source\n', 'hide-race.md', 'implementer')['id'])
        first = service.archive_api
        second = LocalArchiveAdapter(service.config.archive, service.config.cache, service.bindings)
        connection = first.connection
        competing = []

        class InterleavedConnection:
            def execute(self, sql, *args):
                cursor = connection.execute(sql, *args)
                if sql.startswith('SELECT * FROM archive_hides') and not competing:
                    competing.append(second.hide(record['archive_id'], service.origin, 'same-hide'))
                return cursor

            def __getattr__(self, name):
                return getattr(connection, name)

            def __enter__(self):
                connection.__enter__()
                return self

            def __exit__(self, *args):
                return connection.__exit__(*args)

        monkeypatch.setattr(first, 'connection', InterleavedConnection())
        try:
            result = first.hide(record['archive_id'], service.origin, 'same-hide')
            assert competing and result == competing[0]
            assert connection.execute('SELECT COUNT(*) FROM archive_hides WHERE operation_id=?',
                                      ('same-hide',)).fetchone()[0] == 1
        finally:
            second.close()

    def test_runtime_supersession_rechecks_epoch_before_writing_link(self, service, monkeypatch):
        from kelmo.store.db import open_connection
        from kelmo.errors import KelmoError
        self.missing_model(monkeypatch)
        old = service.run(service.capture('original race source\n', 'old-race.md', 'implementer')['id'])
        new = service.run(service.capture('replacement race source\n', 'new-race.md', 'implementer')['id'])
        adapter = service.archive_api
        original_check = adapter._origin
        other = open_connection(str(service.config.archive))
        advanced = []

        def advance_after_early_check(origin):
            original_check(origin)
            if not advanced:
                assert not adapter.connection.in_transaction
                with other:
                    other.execute('UPDATE archive_writer_state SET epoch=epoch+1 WHERE authority_id=?',
                                  (service.bindings.authority_id,))
                advanced.append(True)

        monkeypatch.setattr(adapter, '_origin', advance_after_early_check)
        try:
            with pytest.raises(KelmoError) as refused:
                adapter.supersede(new['archive_id'], old['archive_id'], service.origin)
            assert refused.value.code == 'E_WRITER' and advanced
        finally:
            other.close()
        reopened = open_connection(str(service.config.archive))
        try:
            assert reopened.execute('SELECT COUNT(*) FROM record_supersession WHERE id=?',
                                    (new['archive_id'],)).fetchone()[0] == 0
            assert reopened.execute('SELECT COUNT(*) FROM record_visibility').fetchone()[0] == 0
        finally:
            reopened.close()
