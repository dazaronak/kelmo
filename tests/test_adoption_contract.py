"""Offline adoption checks: JSON/stdlib only, no storage or provider imports."""
import hashlib
import json
from pathlib import Path

import pytest


def contracts():
    import importlib.util
    assert importlib.util.find_spec('kelmo.contracts') is not None, 'adoption contract is absent'
    from kelmo import contracts
    return contracts


def test_exact_evidence_and_generation_round_trip():
    c = contracts()
    ref = c.EvidenceRef('synthetic-record', 2, hashlib.sha256(b'corrected').hexdigest(), 'source-2')
    assert c.EvidenceRef.from_dict(json.loads(json.dumps(ref.to_dict()))) == ref
    generation = c.Generation('synthetic-authority', 3, hashlib.sha256(b'manifest').hexdigest())
    assert c.Generation.from_dict(generation.to_dict()) == generation
    assert set(generation.to_dict()) == {'authority_id', 'publication_id', 'manifest_sha256'}
    for fields in ({'version': True}, {'version': 0}, {'content_sha256': 'wrong'}, {'requester': True}):
        with pytest.raises(ValueError):
            c.EvidenceRef.from_dict(dict(ref.to_dict(), **fields))


def test_source_bytes_are_preserved_and_metadata_cannot_grant_authority():
    c = contracts()
    source = c.SourcePacket(b'\xef\xbb\xbfOriginal\r\n', 'raw_note', 'source', 'note.md',
                            '2026-10-14T10:00:00Z')
    assert c.SourcePacket.from_dict(json.loads(json.dumps(source.to_dict()))) == source
    with pytest.raises(ValueError):
        c.SourcePacket.from_dict(dict(source.to_dict(), origin={'evidence_mark': 'REQUESTER'}))


def test_action_subjects_and_git_deployment_bindings_are_separate():
    c = contracts()
    ref = c.EvidenceRef('record', 1, 'a' * 64, 'source')
    task = c.ActionUpdate('task-1', 'task', 'sample-task', 'book', None, 1, 'completed', ref)
    assert task.repository_id is None and task.environment is None
    for kind in ('push', 'merge', 'deploy'):
        with pytest.raises(ValueError):
            c.ActionUpdate('action-' + kind, 'feature', 'feature', kind, None, 1, 'completed', ref)
    push = c.ActionUpdate('push-1', 'feature', 'feature', 'push', None, 1, 'completed', ref,
                          feature_id='feature', repository_id='repository', work_revision='synthetic-revision',
                          remote_ref='origin/test')
    deploy = c.ActionUpdate('deploy-1', 'feature', 'feature', 'deploy', None, 2, 'completed', ref,
                            feature_id='feature', environment='test', artifact_id='synthetic-artifact')
    assert push.action_id != deploy.action_id and push.kind != deploy.kind
    c.ActionUpdate('general-feature', 'feature', 'feature', 'review', None, 3, 'pending', ref)
    with pytest.raises(ValueError):
        c.ActionUpdate('unknown-subject', 'payload-path', '../escape', 'book', None, 1, 'completed', ref)
    with pytest.raises(ValueError):
        c.ActionUpdate('deploy-unpaired', 'feature', 'feature', 'deploy', None, 2, 'completed', ref,
                       environment='test', artifact_id='artifact', repository_id='repository')
    with pytest.raises(ValueError):
        c.ActionUpdate('push-1', 'feature', 'feature', 'push', 'a' * 64, 3, 'retract', ref,
                       feature_id='feature', repository_id='repository', work_revision='synthetic-revision', remote_ref='origin/test')


def test_vector_space_count_dimension_finiteness_and_purpose_are_checked():
    c = contracts()
    space = c.EmbeddingSpace('embedding-space/1', 'artifact:synthetic', 'tokenizer:synthetic',
                             'query-v1', 'document-v1', 'mean', 'none', 'l2', 2, 'float32', 'little')
    c.VectorBatch(space, 'query', ((0.1, 0.2),)).validate_count(1)
    for purpose, vectors in [('other', ((1., 2.),)), ('query', ((1.,),)),
                             ('query', ((float('nan'), 2.),)), ('document', ((True, 1.),))]:
        with pytest.raises(ValueError):
            c.VectorBatch(space, purpose, vectors)
    with pytest.raises(ValueError):
        c.VectorBatch(space, 'document', ((1., 2.),)).validate_count(2)


def test_preservation_fixture_has_exact_versions_visibility_and_distinct_actions():
    c = contracts()
    fixture = json.loads((Path(__file__).parent / 'fixtures/adoption/manifest.json').read_text())
    assert fixture['contract_version'] == c.CONTRACT_VERSION
    assert fixture['base_revision'] == '1c23f568a9ef0ab2f6846f7cdd236859d841ec13'
    versions = fixture['archive_versions']
    same = [item for item in versions if item['evidence']['archive_id'] == 'record-history']
    assert [item['evidence']['version'] for item in same] == [1, 2]
    for item in versions:
        ref = c.EvidenceRef.from_dict(item['evidence'])
        assert hashlib.sha256(item['content'].encode()).hexdigest() == ref.content_sha256
    assert any(item['hidden'] and item['superseded_by'] for item in versions)
    protected = fixture['protected_rule']
    assert 'except' in protected['text'] and protected['mandatory_delivery'] is True
    assert protected['text'] in fixture['wiki_files']['person.md']
    assert fixture['wiki_relative'] == 'knowledge/wiki'
    actions = [c.ActionUpdate.from_dict(item) for item in fixture['actions']]
    assert {item.kind for item in actions} == {'book', 'push', 'deploy'}
    assert len({item.action_id for item in actions}) == 3
    assert fixture['receipt']['evidence_refs'][0] == versions[2]['evidence']
    assert fixture['receipt']['operation_id'] == versions[2]['owning_operation']
    receipt = c.OperationReceipt.from_dict(fixture['receipt'])
    assert receipt.generation is None  # archived is distinct from published/delivered
    generation = c.Generation.from_dict(fixture['generation'])
    manifest = fixture['generation_manifest']
    assert generation.manifest_sha256 == hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for path, content in fixture['wiki_files'].items():
        assert manifest['page_artifacts'][path] == hashlib.sha256(content.encode()).hexdigest()


def test_invalid_snapshot_bytes_and_mixed_generation_are_refused():
    c = contracts()
    generation = c.Generation('authority', 1, 'a' * 64)
    other = c.Generation('authority', 2, 'b' * 64)
    content = b'Complete synthetic page.\n'
    page = c.PageSnapshot('projects/example/topic.md', content, hashlib.sha256(content).hexdigest(), generation)
    for path, digest in [('../escape.md', page.content_sha256), (page.path, 'c' * 64)]:
        with pytest.raises(ValueError):
            c.PageSnapshot(path, content, digest, generation)
    with pytest.raises(ValueError):
        c.PageBatch((page,), 1, None, other)
    with pytest.raises(ValueError):
        c.ContextBundle(other, ('Complete rule with exception.',), (page,), (), 'current', (), True)
    with pytest.raises(ValueError):
        c.SearchFilters.from_dict({'machine_tag': True})


def test_archive_version_cannot_rebind_evidence_to_different_bytes():
    c = contracts()
    source = c.SourcePacket(b'original', 'raw_note', 'source', 'note.md', '2026-10-14T10:00:00Z')
    ref = c.EvidenceRef('record', 1, 'a' * 64, 'source')
    with pytest.raises(ValueError):
        c.ArchiveVersion(ref, source, c.Origin('authority', 'client', 'node'), '2026-10-14T10:00:00Z', True)


def test_pending_vectors_are_explicit_admission_policy_and_wire_type_is_strict():
    c = contracts()
    source = c.SourcePacket(b'original', 'raw_note', 'source', 'note.md', '2026-10-14T10:00:00Z')
    assert source.require_vectors is False
    with pytest.raises(ValueError):
        c.SourcePacket.from_dict(dict(source.to_dict(), require_vectors='REQUESTER'))
