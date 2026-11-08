"""Pure-file admission checks; every snapshot is fictional and caller supplied."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = Path('docs/wiki-v3-examples')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def pointer(value, path):
    for part in path.split('/')[1:]:
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


@pytest.fixture
def packet(tmp_path):
    shutil.copytree(ROOT / EXAMPLES, tmp_path / EXAMPLES)
    (tmp_path / 'docs/WIKI-FILE-CONTRACT.md').write_bytes(
        (ROOT / 'docs/WIKI-FILE-CONTRACT.md').read_bytes())
    manifest = json.loads((tmp_path / EXAMPLES / 'manifest.json').read_bytes())
    # This is the controller's copy, frozen before any worker mutation.
    fixture = copy.deepcopy(manifest['fixture'])
    bindings = fixture.get('source_bindings', {
        item['source_id']: item['path'] for item in manifest['inputs']['files']
        if item['root'] == 'fixture'
    })
    sources = {}
    for source_id, path in bindings.items():
        value = pointer({'fixture': fixture}, path)
        sources[source_id] = value.encode() if isinstance(value, str) else encoded(value)
    state = fixture['state_owner_snapshot']
    controller = dict(
        contract=copy.deepcopy(manifest['contract']),
        baseline=copy.deepcopy(manifest['baseline']), scope=copy.deepcopy(manifest['scope']),
        census=copy.deepcopy(fixture['controller_census']), sources=sources,
        baseline_manifest=fixture['baseline_manifest_content'].encode(),
        registry_target='knowledge/provenance/registry.json',
        registry_source_id='demo-source-legacy', state_source_id='demo-source-state-owner',
        evidence_source_id='demo-source-evidence-ledger',
        history_source_id='demo-source-revision-history',
        owning_receipts_source_id='demo-source-owning-receipts',
        evidence_reads={'demo-source-hidden': 'demo.operation.hidden-sketch'},
        protected_fact_keys=('demo.rule.message-approval',),
        repositories=('demo.repo.harbor-web', 'demo.repo.harbor-tablet'),
        narrative_dependencies={key: {kind: tuple(refs) for kind, refs in n['depends_on'].items()}
                                for key, n in read_records(tmp_path)['narratives'].items()},
    )
    assert state['actions']['demo.action.book-workshop']['kind'] == 'book'
    return tmp_path, manifest, controller


def read_records(root):
    return json.loads((root / EXAMPLES / 'records.json').read_bytes())


def save_records(root, manifest, records):
    (root / EXAMPLES / 'records.json').write_bytes(encoded(records))
    refresh_outputs(root, manifest)


def refresh_outputs(root, manifest):
    for item in manifest['outputs']['files']:
        item['sha256'] = sha((root / item['path']).read_bytes())


def check(root, manifest, controller, *, allow_fixture=True):
    assert importlib.util.find_spec('kelmo.wiki.file_contract'), 'offline validator missing'
    module = importlib.import_module('kelmo.wiki.file_contract')
    (root / EXAMPLES / 'manifest.json').write_bytes(encoded(manifest))
    return module.validate_family(root, str(EXAMPLES / 'manifest.json'),
                                  controller=module.ControllerInput(**controller),
                                  allow_fixture=allow_fixture)


def codes(result):
    return {error.code for error in result.errors}


def test_worker_cannot_redirect_assigned_registry(packet):
    root, manifest, controller = packet
    output = next(item for item in manifest['outputs']['files']
                  if item['target'].endswith('.json'))
    output['target'] = 'rogue/registry.json'
    output['expected_sha256'] = None
    result = check(root, manifest, controller)
    assert not result.structural_valid


def test_portable_family_is_structural_only_and_does_not_mutate(packet):
    root, manifest, controller = packet
    (root / EXAMPLES / 'manifest.json').write_bytes(encoded(manifest))
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert not result.runtime_accepted
    assert result.unresolved
    assert result.coverage['discarded'] == 0
    assert result.coverage['person_facts'] == 2
    assert result.identities['contract'] == controller['contract']
    assert result.identities['sources']['demo-source-hidden'] == (
        '4c72a77bde3d0c0dfea7862b0a1e0ced0ee22621e590cc99e0461a7a2af82c4d')
    assert before == {str(p.relative_to(root)): p.read_bytes()
                      for p in root.rglob('*') if p.is_file()}
    assert result == check(root, manifest, controller)


@pytest.mark.parametrize(('mutation', 'expected'), [
    ('ordinary-person', 'projection'), ('exception', 'preservation'),
    ('census', 'census'), ('disposition', 'coverage'), ('kind', 'action'),
    ('baseline', 'baseline'), ('version', 'evidence'), ('legacy-field', 'preservation'),
    ('unmanaged', 'preservation'), ('wrong-owner', 'hidden-receipt'),
    ('wrong-receipt-version', 'hidden-receipt'), ('dependency', 'dependency'),
    ('unknown-owner', 'schema'), ('duplicate-marker', 'markers'),
    ('nested-marker', 'markers'), ('traversal', 'path'),
    ('source-bytes', 'source'), ('missing-source', 'source'),
    ('contract-bytes', 'contract'), ('revision', 'revision'),
    ('promotion', 'reconciliation'), ('missing-coverage', 'coverage'),
    ('missing-history', 'preservation'), ('missing-adoption', 'adoption'),
    ('unprotect', 'ownership'), ('rebind-note-head', 'evidence'),
    ('duplicate-output', 'path'), ('duplicate-json-key', 'input'),
    ('unknown-field', 'schema'), ('unreviewed-authority', 'ownership'),
    ('omitted-dependency', 'dependency'), ('missing-rule', 'preservation'),
    ('baseline-target', 'baseline'), ('source-pin', 'source'),
    ('forged-predecessor', 'revision'), ('alias-collision', 'alias'),
    ('parent-cycle', 'parent'), ('action-panel', 'projection'),
    ('new-fact-as-import', 'reconciliation'),
])
def test_rejects_preservation_and_authority_mutations(packet, mutation, expected):
    root, manifest, controller = packet
    records = read_records(root)
    person = root / EXAMPLES / 'person.md'
    if mutation == 'ordinary-person':
        person.write_text('\n'.join(line for line in person.read_text().split('\n')
                                   if '[f:e35da660ac01]' not in line))
    elif mutation == 'exception':
        records['facts']['demo.rule.message-approval']['text'] = 'Wait for approval.'
    elif mutation == 'census':
        manifest['fixture']['controller_census']['units'][0]['destination'] = 'invented'
        manifest['inputs']['census']['sha256'] = sha(encoded(manifest['fixture']['controller_census']))
    elif mutation == 'disposition':
        next(x for x in manifest['coverage']['items'] if x['unit_id'] == 'demo.unit.137')[
            'disposition'] = 'retained-evidence'
    elif mutation == 'kind':
        records['actions']['demo.action.preview-push']['kind'] = 'deploy'
    elif mutation == 'baseline':
        manifest['baseline']['writer_epoch'] += 1
    elif mutation == 'version':
        records['facts']['demo.preview.behavior']['evidence_refs'][0]['version'] = 2
    elif mutation == 'legacy-field':
        records['pages']['projects/harbor-demo/sailing-schedule.md'][
            'compatibility']['legacy_fields'].pop('summary_assessed_by')
    elif mutation == 'unmanaged':
        page = root / EXAMPLES / 'sailing-schedule.md'
        page.write_bytes(page.read_bytes().replace(b'Keep the old handbook wording', b'Changed wording'))
    elif mutation in ('wrong-owner', 'wrong-receipt-version'):
        receipts = manifest['fixture']['owning_receipts']
        if mutation == 'wrong-owner':
            receipts[0]['operation_receipt_id'] = 'demo.operation.somebody-else'
        else:
            receipts[0]['evidence_ref']['version'] = 2
    elif mutation == 'dependency':
        records['narratives']['demo.narrative.preview']['depends_on']['facts'][
            'demo.preview.behavior'] = '0' * 64
    elif mutation == 'unknown-owner':
        records['facts']['demo.person.sketchbook']['owner'] = 'worker'
    elif mutation == 'duplicate-marker':
        person.write_bytes(person.read_bytes() + b'\n<!-- kb -->\n<!-- /kb -->\n')
    elif mutation == 'nested-marker':
        person.write_bytes(person.read_bytes().replace(b'<!-- kb -->', b'<!-- kb -->\n<!-- narrative -->'))
    elif mutation == 'traversal':
        manifest['outputs']['files'][0]['path'] = '../escape.md'
    elif mutation == 'source-bytes':
        controller['sources']['demo-source-person'] += b'changed'
    elif mutation == 'missing-source':
        controller['sources'].pop('demo-source-person')
    elif mutation == 'contract-bytes':
        (root / 'docs/WIKI-FILE-CONTRACT.md').write_bytes(b'changed contract')
    elif mutation == 'revision':
        records['facts']['demo.person.sketchbook']['revision'] = '0' * 64
    elif mutation == 'promotion':
        manifest['changes'][0]['validation'] = 'semantic-promotion'
    elif mutation == 'missing-coverage':
        manifest['coverage']['items'].pop()
    elif mutation == 'missing-history':
        manifest['fixture']['revision_history']['demo.legacy.release'].pop(0)
    elif mutation == 'missing-adoption':
        manifest['scope']['adoption_unit_ids'] = []
    elif mutation == 'unprotect':
        records['facts']['demo.rule.message-approval'].update(protected=False, owner='semantic-editor')
    elif mutation == 'rebind-note-head':
        replacement = next(r for r in manifest['inputs']['evidence_refs']
                           if r['archive_id'] == 'demo.archive.design' and r['version'] == 2)
        for record in records['facts'].values():
            if record['ark_id'] == 'demo.archive.design':
                record['evidence_refs'] = [replacement]
        records['notes']['938e9c7262d30a0269e441df256979fe11e86c29362722bf62c14bd5831cb48c'][
            'evidence_refs'] = [replacement]
    elif mutation == 'duplicate-output':
        manifest['outputs']['files'].append(copy.deepcopy(manifest['outputs']['files'][0]))
    elif mutation == 'unknown-field':
        records['facts']['demo.person.sketchbook']['unmapped_new_field'] = 'not allowed'
    elif mutation == 'unreviewed-authority':
        manifest['review']['approval'] = 'accepted'
    elif mutation == 'omitted-dependency':
        records['narratives']['demo.narrative.preview']['depends_on']['facts'].pop('demo.legacy.release')
    elif mutation == 'missing-rule':
        records['facts'].pop('demo.rule.message-approval')
    elif mutation == 'baseline-target':
        manifest['outputs']['files'][0]['expected_sha256'] = None
    elif mutation == 'source-pin':
        manifest['inputs']['files'][0]['sha256'] = '0' * 64
    elif mutation == 'forged-predecessor':
        records['facts']['demo.person.sketchbook']['previous_revision'] = '0' * 64
        next(c for c in manifest['changes'] if c['id'] == 'demo.person.sketchbook')[
            'expected_revision'] = '0' * 64
    elif mutation == 'alias-collision':
        records['pages']['person.md']['aliases'] = ['Harbor Demo']
    elif mutation == 'parent-cycle':
        records['pages']['person.md']['parent'] = 'person.md'
    elif mutation == 'action-panel':
        path = root / EXAMPLES / 'feature.md'
        path.write_text(path.read_text().replace('| pending', '| completed'))
    elif mutation == 'new-fact-as-import':
        new_fact = copy.deepcopy(records['facts']['demo.preview.behavior'])
        new_fact.update(fact_key='demo.preview.invented', id=sha(b'demo.preview.invented')[:12],
                        previous_revision=None)
        new_fact['revision'] = sha(encoded({k: v for k, v in new_fact.items()
                                          if k not in ('revision', 'previous_revision')}))
        records['facts']['demo.preview.invented'] = new_fact
        manifest['changes'].append(dict(entity='fact', id='demo.preview.invented',
                                         expected_revision=None, proposed_revision=new_fact['revision'],
                                         validation='deterministic-import'))
    (root / EXAMPLES / 'records.json').write_bytes(encoded(records))
    if mutation == 'duplicate-json-key':
        path = root / EXAMPLES / 'records.json'
        path.write_bytes(path.read_bytes().replace(b'"version": 3', b'"version": 3, "version": 3'))
    for item in manifest['outputs']['files']:
        if not item['path'].startswith('../'):
            item['sha256'] = sha((root / item['path']).read_bytes())
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert expected in codes(result), result.errors


def test_rejects_descendant_symlink_without_reading_target(packet):
    root, manifest, controller = packet
    page = root / EXAMPLES / 'person.md'
    original = page.read_bytes()
    page.unlink()
    outside = root.parent / 'outside.md'
    outside.write_bytes(original)
    page.symlink_to(outside)
    result = check(root, manifest, controller)
    assert 'path' in codes(result)


def test_fixture_is_refused_by_default_admission(packet):
    result = check(*packet, allow_fixture=False)
    assert 'fixture' in codes(result)


def test_no_fixture_transport_works_with_registry_compatibility_retention(packet):
    root, manifest, controller = packet
    fixture = manifest.pop('fixture')
    records = read_records(root)
    records['compatibility']['migration_inputs'] = fixture
    for units in (manifest['coverage']['items'], controller['census']['units']):
        for unit in units:
            unit['destination'] = unit['destination'].replace(
                'manifest.json#/fixture/', 'records.json#/compatibility/migration_inputs/')
    census = controller['census']
    manifest['inputs']['census']['sha256'] = sha(encoded(census))
    next(x for x in manifest['inputs']['files'] if x['source_id'] == 'demo-source-census')[
        'sha256'] = sha(encoded(census))
    controller['sources']['demo-source-census'] = encoded(census)
    save_records(root, manifest, records)
    result = check(root, manifest, controller, allow_fixture=False)
    assert result.structural_valid, result.errors
    assert not result.runtime_accepted


@pytest.mark.parametrize('fault', ['owner', 'version'])
def test_hidden_receipt_checks_actual_supplied_bytes_not_only_worker_copy(packet, fault):
    root, manifest, controller = packet
    receipt = manifest['fixture']['owning_receipts'][0]
    if fault == 'owner':
        receipt['operation_receipt_id'] = 'demo.operation.other'
    else:
        receipt['evidence_ref']['version'] = 2
    # A controller can supply a completely hashed but internally inconsistent snapshot.
    # Rehashing that snapshot must not create an owning capability.
    data = encoded(manifest['fixture']['owning_receipts'])
    controller['sources']['demo-source-owning-receipts'] = data
    for files in (controller['census']['sources'], manifest['inputs']['files']):
        next(x for x in files if x['source_id'] == 'demo-source-owning-receipts')['sha256'] = sha(data)
    manifest['fixture']['controller_census'] = copy.deepcopy(controller['census'])
    census = encoded(controller['census'])
    controller['sources']['demo-source-census'] = census
    manifest['inputs']['census']['sha256'] = sha(census)
    next(x for x in manifest['inputs']['files'] if x['source_id'] == 'demo-source-census')['sha256'] = sha(census)
    assert 'hidden-receipt' in codes(check(root, manifest, controller))


def test_packaged_registry_refuses_unknown_remote_reference():
    from jsonschema.exceptions import _WrappedReferencingError
    from kelmo.wiki.file_contract import _validators

    validator = _validators()['page'].evolve(schema={'$ref': 'https://offline.invalid/schema'})
    with pytest.raises(_WrappedReferencingError):
        validator.validate({})


def test_changed_dependency_can_only_be_preserved_with_visible_stale_label(packet):
    root, manifest, controller = packet
    records = read_records(root)
    narrative = records['narratives']['demo.narrative.preview']
    narrative['depends_on']['facts']['demo.preview.behavior'] = '0' * 64
    narrative['state'] = 'stale'
    narrative['revision'] = sha(encoded({k: v for k, v in narrative.items()
                                         if k not in ('revision', 'previous_revision')}))
    next(x for x in manifest['changes'] if x['id'] == 'demo.narrative.preview')[
        'proposed_revision'] = narrative['revision']
    path = root / EXAMPLES / 'feature.md'
    path.write_text(path.read_text().replace('State: current.', 'State: stale.'))
    save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert any(item.code == 'stale-dependency' for item in result.unresolved)


def repin_census(manifest, controller):
    census_bytes = encoded(controller['census'])
    manifest['fixture']['controller_census'] = copy.deepcopy(controller['census'])
    controller['sources']['demo-source-census'] = census_bytes
    manifest['inputs']['census']['sha256'] = sha(census_bytes)
    next(x for x in manifest['inputs']['files'] if x['source_id'] == 'demo-source-census')[
        'sha256'] = sha(census_bytes)




@pytest.mark.parametrize('field', ['authority_id', 'machine_tag', 'both'])
def test_hidden_receipt_origin_must_match_complete_enrollment(packet, field):
    root, manifest, controller = packet
    source_id = 'demo-source-owning-receipts'
    receipts = manifest['fixture']['owning_receipts']
    if field in ('authority_id', 'both'):
        receipts[0]['origin']['authority_id'] = 'demo.authority.foreign'
    if field in ('machine_tag', 'both'):
        receipts[0]['origin']['machine_tag'] = 'foreign-machine'
    old_size = len(controller['sources'][source_id])
    data = encoded(receipts)
    controller['sources'][source_id] = data
    for sources in (manifest['inputs']['files'], controller['census']['sources']):
        next(x for x in sources if x['source_id'] == source_id)['sha256'] = sha(data)
    for unit in controller['census']['units']:
        if unit['source_id'] != source_id:
            continue
        if unit['unit'] == 'bytes':
            assert unit['locator'] == f'bytes:0:{old_size}'
            unit.update(locator=f'bytes:0:{len(data)}', sha256=sha(data))
        else:
            unit['sha256'] = sha(encoded(pointer(receipts, unit['locator'])))
        item = next(x for x in manifest['coverage']['items'] if x['unit_id'] == unit['unit_id'])
        item.update(locator=unit['locator'], sha256=unit['sha256'])
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'hidden-receipt' in codes(result), result.errors


@pytest.mark.parametrize('concealment', ['comment', 'fence', 'inline-code', 'duplicate'])
def test_narrative_state_must_have_one_visible_matching_label(packet, concealment):
    root, manifest, controller = packet
    records = read_records(root)
    narrative = records['narratives']['demo.narrative.preview']
    narrative['depends_on']['facts']['demo.preview.behavior'] = '0' * 64
    narrative['state'] = 'stale'
    narrative['revision'] = sha(encoded({k: v for k, v in narrative.items()
                                         if k not in ('revision', 'previous_revision')}))
    next(x for x in manifest['changes'] if x['id'] == narrative['narrative_id'])[
        'proposed_revision'] = narrative['revision']
    label = 'Narrative: demo.narrative.preview. State: stale.'
    extra = {'comment': f'<!-- {label} -->', 'fence': f'```text\n{label}\n```',
             'inline-code': f'`{label}`', 'duplicate': label}[concealment]
    path = root / EXAMPLES / 'feature.md'
    path.write_text(path.read_text().replace('<!-- /narrative -->', extra + '\n\n<!-- /narrative -->'))
    save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert 'projection' in codes(result), result.errors


@pytest.mark.parametrize(('name', 'field', 'old', 'replacement'), [
    ('feature.md', 'state', 'pending', 'completed (previously pending)'),
    ('feature.md', 'kind', 'merge', 'deploy (previously merge)'),
    ('feature.md', 'owner', 'state-api', 'worker (previously state-api)'),
    ('project-map.md', 'state', 'pending', 'completed (previously pending)'),
    ('project-map.md', 'kind', 'Merge', 'Deploy (previously Merge)'),
    ('person.md', 'state', 'completed', 'pending (previously completed)'),
    ('person.md', 'kind', 'kind: book', 'kind: deploy (previously book)'),
    ('person.md', 'owner', 'owner: state-api', 'owner: worker (previously state-api)'),
])
def test_action_projection_compares_declared_fields_exactly(packet, name, field, old, replacement):
    root, manifest, controller = packet
    path = root / EXAMPLES / name
    action_id = 'demo.action.book-workshop' if name == 'person.md' else 'demo.action.preview-merge'
    lines = path.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if '[a:' + action_id + ']' in line:
            assert old in line
            if name == 'person.md':
                lines[i] = line.replace(old, replacement, 1)
            else:
                cells = line.split('|')
                column = {'kind': 3, 'state': 5, 'owner': 6}[field]
                assert old in cells[column]
                cells[column] = cells[column].replace(old, replacement, 1)
                lines[i] = '|'.join(cells)
    path.write_text(''.join(lines))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert 'projection' in codes(result), (field, result.errors)


@pytest.mark.parametrize(('content', 'concealment'), [
    ('fact', 'comment'), ('fact', 'fence'), ('fact', 'inline-code'),
    ('fact-prefix', 'visible'), ('narrative', 'comment'), ('narrative', 'fence'),
    ('link', 'comment'), ('link', 'fence'), ('action', 'comment'), ('action', 'fence'),
])
def test_hidden_or_contradictory_text_cannot_supply_visible_projection(packet, content, concealment):
    root, manifest, controller = packet
    name = 'feature.md' if content == 'narrative' else 'person.md'
    path = root / EXAMPLES / name
    records = read_records(root)
    wanted = {'fact': '[f:e35da660ac01]', 'fact-prefix': '[f:e35da660ac01]',
              'narrative': records['narratives']['demo.narrative.preview']['text'],
              'link': '[Current project map]', 'action': '[a:demo.action.book-workshop]'}[content]
    lines = path.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if wanted not in line:
            continue
        if content == 'fact-prefix':
            lines[i] = line.replace('- ', '- False claim. ', 1)
        elif concealment == 'comment':
            lines[i] = '<!-- ' + line.rstrip() + ' -->\n'
        elif concealment == 'fence':
            lines[i] = '```text\n' + line + '```\n'
        else:
            lines[i] = '`' + line.rstrip() + '`\n'
    path.write_text(''.join(lines))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    expected = 'link' if content == 'link' else 'projection'
    assert expected in codes(result), result.errors


@pytest.mark.parametrize('location', ['manifest', 'source'])
def test_deeply_nested_json_returns_stable_input_error(packet, location):
    from kelmo.wiki.file_contract import ControllerInput, validate_family

    root, manifest, controller = packet
    deeply_nested = b'[' * 10000 + b'0' + b']' * 10000
    if location == 'source':
        source_id = controller['registry_source_id']
        controller['sources'][source_id] = deeply_nested
        for sources in (manifest['inputs']['files'], controller['census']['sources']):
            next(x for x in sources if x['source_id'] == source_id)['sha256'] = sha(deeply_nested)
        repin_census(manifest, controller)
    (root / EXAMPLES / 'manifest.json').write_bytes(
        deeply_nested if location == 'manifest' else encoded(manifest))
    result = validate_family(root, str(EXAMPLES / 'manifest.json'),
                             controller=ControllerInput(**controller), allow_fixture=True)
    assert not result.structural_valid
    assert 'input' in codes(result)
    assert result == validate_family(root, str(EXAMPLES / 'manifest.json'),
                                     controller=ControllerInput(**controller), allow_fixture=True)


@pytest.mark.parametrize('location', ['eof', 'before-kb', 'between-regions', 'eof-space', 'expanded-preserve'])
def test_unassigned_outside_insertions_refuse_without_crashing(packet, location):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    path = root / EXAMPLES / 'sailing-schedule.md'
    original = path.read_bytes()
    if location in ('eof', 'expanded-preserve'):
        changed = original + b'\nManual correction: the release is confirmed.\n'
    elif location == 'eof-space':
        changed = original + b'\n'
    elif location == 'before-kb':
        changed = original.replace(b'<!-- kb -->', b'Manual correction: release confirmed.\n\n<!-- kb -->')
    else:
        changed = original.replace(b'<!-- narrative -->', b'Manual correction: release confirmed.\n\n<!-- narrative -->')
    path.write_bytes(changed)
    if location == 'expanded-preserve':
        unit = next(x for x in controller['census']['units'] if x['unit_id'] == 'demo.unit.143')
        unit['destination'] = f'sailing-schedule.md#span:narrative-end:0:narrative-end:{102 + len(changed) - len(original)}'
        next(x for x in manifest['coverage']['items'] if x['unit_id'] == unit['unit_id'])[
            'destination'] = unit['destination']
        repin_census(manifest, controller)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'preservation' in codes(result), result.errors
    assert not {'input', 'path'} & codes(result), result.errors


@pytest.mark.parametrize(('field', 'value'), [
    ('filename', 'different.md'), ('expected_revision', '0' * 64),
    ('replaces', ['demo.person.sketchbook']), ('index_target', 'person.md'),
])
def test_active_legacy_fields_cannot_diverge_from_opaque_original(packet, field, value):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    records = read_records(root)
    if field == 'filename':
        note = records['notes']['879b7df9f9b4dfeb1118b6afc2f9d76d6f76b0a8db5d533296717d2883dee732']
        note[field] = value
        assert note['compatibility']['legacy_fields'][field] != value
    else:
        fact = records['facts']['demo.person.sketchbook']
        fact[field] = value
        fact['revision'] = sha(encoded({k: v for k, v in fact.items()
                                        if k not in ('revision', 'previous_revision')}))
        next(x for x in manifest['changes'] if x['id'] == fact['fact_key'])[
            'proposed_revision'] = fact['revision']
        next(x for x in manifest['revision_map'] if x['id'] == fact['fact_key'])[
            'new_revision'] = fact['revision']
    save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert 'preservation' in codes(result), result.errors
    assert 'input' not in codes(result), result.errors


@pytest.mark.parametrize('replacement', [True, 1.0])
def test_preserved_json_leaf_must_retain_its_numeric_type(packet, replacement):
    root, manifest, controller = packet
    records = read_records(root)
    source_id = controller['registry_source_id']
    source = json.loads(controller['sources'][source_id])
    key = 'demo.person.sketchbook'
    source['facts'][key]['legacy_counter'] = 1
    records['facts'][key]['compatibility']['legacy_fields']['legacy_counter'] = 1
    data = encoded(source)
    old_size = len(controller['sources'][source_id])
    controller['sources'][source_id] = data
    manifest['fixture']['legacy_registry'] = copy.deepcopy(source)
    manifest['fixture']['baseline_registry_content'] = data.decode()
    baseline = json.loads(controller['baseline_manifest'])
    baseline['registry_sha256'] = sha(data)
    next(x for x in baseline['artifacts'] if x['target'] == controller['registry_target'])[
        'sha256'] = sha(data)
    controller['baseline_manifest'] = encoded(baseline)
    manifest['fixture']['baseline_manifest_content'] = encoded(baseline).decode()
    controller['baseline']['registry_sha256'] = sha(data)
    controller['baseline']['generation']['manifest_sha256'] = sha(encoded(baseline))
    manifest['baseline'] = copy.deepcopy(controller['baseline'])
    controller['census']['baseline'] = copy.deepcopy(controller['baseline'])
    next(x for x in manifest['outputs']['files'] if x['target'] == controller['registry_target'])[
        'expected_sha256'] = sha(data)
    for sources in (manifest['inputs']['files'], controller['census']['sources']):
        next(x for x in sources if x['source_id'] == source_id)['sha256'] = sha(data)
    for unit in controller['census']['units']:
        if unit['source_id'] == source_id and unit['unit'] == 'bytes':
            assert unit['locator'] == f'bytes:0:{old_size}'
            unit.update(locator=f'bytes:0:{len(data)}', sha256=sha(data))
            next(x for x in manifest['coverage']['items'] if x['unit_id'] == unit['unit_id']).update(
                locator=unit['locator'], sha256=unit['sha256'])
    unit = dict(unit_id='test.unit.opaque-counter', source_id=source_id,
                locator=f'/facts/{key}/legacy_counter', unit='field', sha256=sha(encoded(1)),
                owner='registry', operation='preserve', allowed_dispositions=['mapped'],
                destination=f'records.json#/facts/{key}/compatibility/legacy_fields/legacy_counter')
    controller['census']['units'].append(unit)
    manifest['coverage']['items'].append({k: v for k, v in unit.items()
                                        if k not in ('owner', 'operation', 'allowed_dispositions')} | {
                                            'disposition': 'mapped', 'reason': 'Preserve opaque input.'})
    manifest['coverage']['totals']['input'] += 1
    manifest['coverage']['totals']['mapped'] += 1
    repin_census(manifest, controller)

    def save_counter():
        fact = records['facts'][key]
        fact['revision'] = sha(encoded({k: v for k, v in fact.items()
                                        if k not in ('revision', 'previous_revision')}))
        next(x for x in manifest['changes'] if x['id'] == key)['proposed_revision'] = fact['revision']
        next(x for x in manifest['revision_map'] if x['id'] == key)['new_revision'] = fact['revision']
        save_records(root, manifest, records)

    save_counter()
    baseline_result = check(root, manifest, controller)
    assert baseline_result.structural_valid, baseline_result.errors
    records['facts'][key]['compatibility']['legacy_fields']['legacy_counter'] = replacement
    save_counter()
    result = check(root, manifest, controller)
    assert 'preservation' in codes(result), result.errors
    assert not result.structural_valid


@pytest.mark.parametrize('link', [
    '[[Harbor Demo]]', '[Map](harbor-demo.md)', '[[Harbor Demo|Project map]]',
    '[[projects/harbor-demo/harbor-demo#Current map|Project map]]',
])
def test_registered_alias_and_canonical_map_links_remain_valid(packet, link):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    permission_bytes = controller['sources']['demo-source-census']
    path = root / EXAMPLES / 'feature.md'
    original = path.read_bytes()
    path.write_text(path.read_text().replace('[Current project map](harbor-demo.md)', link))
    assert len(path.read_bytes()) != len(original)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment
    assert controller['sources']['demo-source-census'] == permission_bytes


def test_alias_collision_still_refuses_registered_identity_selection(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    records = read_records(root)
    records['pages']['person.md']['aliases'].append('Harbor Demo')
    save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'alias' in codes(result), result.errors


def test_eof_insertion_needs_an_explicit_controller_adoption(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    path = root / EXAMPLES / 'sailing-schedule.md'
    original = path.read_bytes()
    path.write_bytes(original + b'\nProposed manual annotation.\n')
    refresh_outputs(root, manifest)
    assert 'preservation' in codes(check(root, manifest, controller))
    reference = f'sailing-schedule.md#bytes:{len(original)}:{path.stat().st_size}'
    source_id = 'demo-source-legacy-page'
    source_size = len(controller['sources'][source_id])
    locator = f'bytes:{source_size}:{source_size}'
    unit = dict(unit_id='test.unit.explicit-eof', source_id=source_id, locator=locator,
                unit='bytes', sha256=sha(b''), owner='unmanaged', operation='adopt',
                allowed_dispositions=['mapped'], destination=reference)
    controller['census']['units'].append(unit)
    controller['census']['adoption_units'].append(dict(
        unit_id=unit['unit_id'], source_id=source_id, locator=locator, expected_sha256=sha(b''),
        operation='adopt', replacement_region=reference, authorization='proposal-only',
        output_sha256=sha(path.read_bytes()[len(original):])))
    for scope in (controller['scope'], controller['census']['scope'], manifest['scope']):
        scope['adoption_unit_ids'].append(unit['unit_id'])
    manifest['coverage']['items'].append({k: v for k, v in unit.items()
                                        if k not in ('owner', 'operation', 'allowed_dispositions')} | {
                                            'disposition': 'mapped', 'reason': 'Explicit proposed insertion.'})
    manifest['coverage']['totals']['input'] += 1
    manifest['coverage']['totals']['mapped'] += 1
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert not result.runtime_accepted


def replace_supplied_source(manifest, controller, source_id, data):
    controller['sources'][source_id] = data
    for sources in (manifest['inputs']['files'], controller['census']['sources']):
        next(x for x in sources if x['source_id'] == source_id)['sha256'] = sha(data)
    repin_census(manifest, controller)


@pytest.mark.parametrize(('role', 'path', 'malformed'), [
    ('registry_source_id', '', []), ('registry_source_id', '/facts', []),
    ('registry_source_id', '/facts/demo.person.sketchbook', []),
    ('registry_source_id', '/notes', []), ('registry_source_id', '/pages', []),
    ('state_source_id', '', []), ('state_source_id', '/actions', []),
    ('state_source_id', '/receipts', [[]]), ('state_source_id', '/subject_bindings', []),
    ('state_source_id', '/trusted_bindings/demo.client.owner', []),
    ('state_source_id', '/history/demo.action.book-workshop', {}),
    ('evidence_source_id', '', {}), ('evidence_source_id', '', [[]]),
    ('history_source_id', '', []), ('history_source_id', '/demo.legacy.release', [[]]),
    ('owning_receipts_source_id', '', {}), ('owning_receipts_source_id', '', [[]]),
])
def test_role_snapshots_refuse_malformed_shapes_without_crashing(packet, role, path, malformed):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    source_id = controller[role]
    value = json.loads(controller['sources'][source_id])
    if path:
        parent, key = path.rsplit('/', 1)
        pointer(value, parent)[key] = malformed
    else:
        value = malformed
    replace_supplied_source(manifest, controller, source_id, encoded(value))
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'input' in codes(result), result.errors


def test_orphan_adoption_never_grants_output_bytes(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    path = root / EXAMPLES / 'feature.md'
    start = path.stat().st_size
    path.write_bytes(path.read_bytes() + b'Unassigned release claim.\n')
    unit_id = 'test.unit.orphan'
    for scope in (controller['scope'], controller['census']['scope'], manifest['scope']):
        scope['adoption_unit_ids'].append(unit_id)
    controller['census']['adoption_units'].append(dict(
        unit_id=unit_id, source_id='demo-source-empty-feature', locator='bytes:0:0',
        expected_sha256='0' * 64, operation='adopt', authorization='proposal-only',
        output_sha256=sha(path.read_bytes()[start:]),
        replacement_region=f'feature.md#bytes:{start}:{path.stat().st_size}'))
    repin_census(manifest, controller)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'adoption' in codes(result), result.errors


@pytest.mark.parametrize('escaped', [r'\[\[Harbor Demo\]\]', r'\[[Harbor Demo]]', '![[Harbor Demo]]'])
def test_escaped_or_embedded_double_brackets_do_not_supply_an_edge(packet, escaped):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    old = '[Current project map](harbor-demo.md)'
    path = root / EXAMPLES / 'feature.md'
    path.write_text(path.read_text().replace(old, escaped + ' ' * (len(old) - len(escaped))))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert 'link' in codes(result), result.errors


def accepted_person_markdown(packet, markdown):
    root, manifest, controller = packet
    source_id = controller['registry_source_id']
    original_bytes = controller['sources'][source_id]
    legacy = json.loads(original_bytes)
    records = read_records(root)
    key = 'demo.person.sketchbook'
    old_text = legacy['facts'][key]['text']
    fact = legacy['facts'][key]
    fact['text'] = markdown
    fact['revision'] = sha(encoded({k: fact[k] for k in ('fact_key', 'target', 'text', 'status', 'role')}))
    current = records['facts'][key]
    current.update(text=markdown, previous_revision=fact['revision'])
    current['compatibility']['legacy_fields'] = copy.deepcopy(fact)
    current['revision'] = sha(encoded({k: v for k, v in current.items()
                                      if k not in ('revision', 'previous_revision')}))
    next(x for x in manifest['changes'] if x['id'] == key).update(
        expected_revision=fact['revision'], proposed_revision=current['revision'])
    next(x for x in manifest['revision_map'] if x['id'] == key).update(
        old_revision=fact['revision'], new_revision=current['revision'])
    data = encoded(legacy)
    manifest['fixture']['legacy_registry'] = copy.deepcopy(legacy)
    manifest['fixture']['baseline_registry_content'] = data.decode()
    baseline = json.loads(controller['baseline_manifest'])
    baseline['registry_sha256'] = sha(data)
    next(x for x in baseline['artifacts'] if x['target'] == controller['registry_target'])[
        'sha256'] = sha(data)
    controller['baseline_manifest'] = encoded(baseline)
    manifest['fixture']['baseline_manifest_content'] = encoded(baseline).decode()
    controller['baseline']['registry_sha256'] = sha(data)
    controller['baseline']['generation']['manifest_sha256'] = sha(encoded(baseline))
    manifest['baseline'] = copy.deepcopy(controller['baseline'])
    controller['census']['baseline'] = copy.deepcopy(controller['baseline'])
    next(x for x in manifest['outputs']['files'] if x['target'] == controller['registry_target'])[
        'expected_sha256'] = sha(data)
    for unit in controller['census']['units']:
        if unit['source_id'] != source_id:
            continue
        if unit['unit'] == 'bytes':
            assert unit['locator'] == f'bytes:0:{len(original_bytes)}'
            unit.update(locator=f'bytes:0:{len(data)}', sha256=sha(data))
        else:
            unit['sha256'] = sha(encoded(pointer(legacy, unit['locator'])))
        next(x for x in manifest['coverage']['items'] if x['unit_id'] == unit['unit_id']).update(
            locator=unit['locator'], sha256=unit['sha256'])
    replace_supplied_source(manifest, controller, source_id, data)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace(old_text, markdown + ' ' * (len(old_text) - len(markdown))))
    save_records(root, manifest, records)
    return root, manifest, controller


@pytest.mark.parametrize('markdown', ['Use **git add** only.', 'Use `git add` only.'])
def test_visible_fact_markdown_preserves_emphasis_and_inline_code(packet, markdown):
    result = check(*accepted_person_markdown(packet, markdown))
    assert result.structural_valid, result.errors


def test_fact_formatting_cannot_be_lost_in_the_rendered_projection(packet):
    root, manifest, controller = accepted_person_markdown(packet, 'Use **git add** only.')
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('Use **git add** only.', 'Use git add only.    '))
    refresh_outputs(root, manifest)
    assert 'projection' in codes(check(root, manifest, controller))


def test_visible_fact_wiki_label_cannot_change_behind_the_same_target(packet):
    root, manifest, controller = accepted_person_markdown(packet, 'See [[Harbor Demo|current project]].')
    assert check(root, manifest, controller).structural_valid
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('|current project]]', '|retired project]]'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors


@pytest.mark.parametrize('mutation', ['header-claim', 'separator-claim', 'missing-anchor', 'duplicate-anchor', 'eof-anchor'])
def test_anchored_ranges_do_not_grant_extra_bytes(packet, mutation):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    path = root / EXAMPLES / 'feature.md'
    raw = path.read_bytes()
    if mutation == 'header-claim':
        raw = raw.replace(b'<!-- kb -->', b'The release is confirmed.\n\n<!-- kb -->')
    elif mutation == 'separator-claim':
        raw = raw.replace(b'<!-- narrative -->', b'The release is confirmed.\n\n<!-- narrative -->')
    elif mutation == 'missing-anchor':
        raw = raw.replace(b'<!-- /narrative -->', b'<!-- narrative missing -->')
    elif mutation == 'duplicate-anchor':
        raw += b'<!-- /narrative -->\n'
    else:
        # A distinct but overbroad assignment still cannot bypass its structural pin.
        unit = next(x for x in controller['census']['units'] if x['unit_id'] == 'demo.unit.469')
        adoption = next(x for x in controller['census']['adoption_units'] if x['unit_id'] == unit['unit_id'])
        unit['destination'] = adoption['replacement_region'] = 'feature.md#span:narrative-end:0:eof:0'
        next(x for x in manifest['coverage']['items'] if x['unit_id'] == unit['unit_id'])[
            'destination'] = unit['destination']
        repin_census(manifest, controller)
        raw += b'The release is confirmed.\n'
    path.write_bytes(raw)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'adoption', 'preservation'} & codes(result), result.errors
    assert 'input' not in codes(result), result.errors


@pytest.mark.parametrize('mutation', ['source', 'locator', 'hash', 'coverage', 'source-bytes'])
def test_adoption_requires_the_matching_extracted_and_covered_source_unit(packet, mutation):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    adoption = controller['census']['adoption_units'][0]
    if mutation == 'source':
        adoption['source_id'] = 'demo-source-legacy-page'
    elif mutation == 'locator':
        adoption['locator'] = 'bytes:0:0'
    elif mutation == 'hash':
        adoption['expected_sha256'] = '0' * 64
    elif mutation == 'coverage':
        manifest['coverage']['items'] = [x for x in manifest['coverage']['items']
                                         if x['unit_id'] != adoption['unit_id']]
    else:
        source_id = adoption['source_id']
        data = controller['sources'][source_id].replace(b'# Harbor Demo rules', b'# Harbor Demo other')
        assert data != controller['sources'][source_id]
        replace_supplied_source(manifest, controller, source_id, data)
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'adoption', 'coverage', 'preservation'} & codes(result), result.errors
    assert 'input' not in codes(result), result.errors


@pytest.mark.parametrize(('name', 'old', 'new'), [
    ('person.md', 'Subject: demo.subject.paper-workshop;', 'Subject: demo.subject.other-workshop;'),
    ('feature.md', 'Subject: feature/demo.feature.update-preview.', 'Subject: feature/demo.feature.other-preview.'),
    ('project-map.md', '[Sailing update preview](update-preview.md) | demo.repo.harbor-web | Merge',
     '[Sailing update preview](sailing-schedule.md) | demo.repo.harbor-web | Merge'),
])
def test_worker_cannot_change_the_rendered_action_subject(packet, name, old, new):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


def test_preservation_accepts_the_exact_canonical_output_path(packet):
    root, manifest, controller = packet
    for rows in (controller['census']['units'], manifest['coverage']['items']):
        unit = next(x for x in rows if x['unit_id'] == 'demo.unit.143')
        unit['destination'] = 'docs/wiki-v3-examples/' + unit['destination']
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors


def test_output_path_resolution_prefers_exact_path_and_refuses_ambiguous_basename(packet):
    from kelmo.wiki.file_contract import ControllerInput, _Check
    root, _, controller = packet
    validator = _Check(root, ControllerInput(**controller), False)
    outputs = {'note.md': b'A', 'other/note.md': b'B', 'else/only.md': b'C'}
    for reference, expected in (('note.md#bytes:0:1', b'A'), ('other/note.md#bytes:0:1', b'B'),
                                ('only.md#bytes:0:1', b'C')):
        assert validator.destination(reference, {}, outputs) == expected
        path, first, last = validator.output_reference(reference, outputs)
        assert outputs[path][first:last] == expected
    outputs.pop('note.md')
    outputs['another/note.md'] = b'D'
    for reference in ('note.md#bytes:0:1', '../note.md#bytes:0:1', 'other//note.md#bytes:0:1'):
        with pytest.raises(ValueError):
            validator.destination(reference, {}, outputs)
        with pytest.raises(ValueError):
            validator.output_reference(reference, outputs)


def test_a_symlinked_transport_root_refuses(packet):
    from kelmo.wiki.file_contract import ControllerInput, validate_family
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    link = root / 'root-alias'
    link.symlink_to(root, target_is_directory=True)
    result = validate_family(link, str(EXAMPLES / 'manifest.json'),
                             controller=ControllerInput(**controller), allow_fixture=True)
    assert not result.structural_valid
    assert 'path' in codes(result), result.errors


def test_a_symlinked_parent_alias_is_a_normal_transport_path(packet):
    from kelmo.wiki.file_contract import ControllerInput, validate_family
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    alias = root / 'parent-alias'
    alias.symlink_to(root.parent, target_is_directory=True)
    result = validate_family(alias / root.name, str(EXAMPLES / 'manifest.json'),
                             controller=ControllerInput(**controller), allow_fixture=True)
    assert result.structural_valid, result.errors


def split_unmanaged_tail(packet, *, rearrange=False):
    root, manifest, controller = packet
    source_id = 'demo-source-legacy-page'
    raw = controller['sources'][source_id]
    source = raw[196:298]
    assert len(source) == 102
    template = next(x for x in controller['census']['units'] if x['unit_id'] == 'demo.unit.143')
    item = next(x for x in manifest['coverage']['items'] if x['unit_id'] == template['unit_id'])
    controller['census']['units'].remove(template)
    manifest['coverage']['items'].remove(item)
    for index, (first, last) in enumerate(((0, 2), (2, 52), (52, 102))):
        offset = first if not rearrange or index == 0 else 54 - first
        unit = dict(template, unit_id=f'test.unit.tail-{index}',
                    locator=f'bytes:{196 + first}:{196 + last}',
                    sha256=sha(source[first:last]),
                    destination=f'sailing-schedule.md#span:narrative-end:{offset}:narrative-end:{offset + last - first}')
        controller['census']['units'].append(unit)
        manifest['coverage']['items'].append(dict(item, **{k: unit[k] for k in (
            'unit_id', 'locator', 'sha256', 'destination')}))
    manifest['coverage']['totals']['input'] += 2
    manifest['coverage']['totals']['mapped'] += 2
    repin_census(manifest, controller)
    if rearrange:
        path = root / EXAMPLES / 'sailing-schedule.md'
        assert path.read_bytes().endswith(source)
        path.write_bytes(path.read_bytes()[:-100] + source[52:] + source[2:52])
        refresh_outputs(root, manifest)


@pytest.mark.parametrize('mutation', ['reorder', 'duplicate', 'overlap-destination'])
def test_worker_unmanaged_reorder_refuses_with_unchanged_trusted_destinations(packet, mutation):
    root, manifest, controller = packet
    split_unmanaged_tail(packet)
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'sailing-schedule.md'
    raw = path.read_bytes()
    if mutation == 'reorder':
        path.write_bytes(raw[:-100] + raw[-50:] + raw[-100:-50])
    elif mutation == 'duplicate':
        path.write_bytes(raw[:-50] + raw[-100:-50])
    else:
        item = next(x for x in manifest['coverage']['items'] if x['unit_id'] == 'test.unit.tail-2')
        item['destination'] = 'sailing-schedule.md#span:narrative-end:2:narrative-end:52'
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    if mutation == 'overlap-destination':
        assert any(x.code == 'coverage' and x.path == 'test.unit.tail-2'
                   and x.detail == 'identity/disposition/destination differs from trusted census' for x in result.errors)
    else:
        unit_id = 'test.unit.tail-1' if mutation == 'reorder' else 'test.unit.tail-2'
        assert any(x.code == 'preservation' and x.path == unit_id
                   and x.detail == 'original byte span lost at destination' for x in result.errors)
    assert controller == assignment


def test_controller_authorized_reorder_and_census_array_order_are_distinct_from_worker_tampering(packet):
    root, manifest, controller = packet
    split_unmanaged_tail(packet, rearrange=True)
    controller['census']['units'].reverse()
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors


def test_overlapping_evidence_mappings_remain_valid(packet):
    root, manifest, controller = packet
    original = next(x for x in controller['census']['units'] if x['owner'] == 'archive'
                    and x['unit'] == 'bytes' and 'retained-evidence' in x['allowed_dispositions'])
    source = controller['sources'][original['source_id']]
    assert len(source) > 33 and original['locator'].startswith('bytes:0:')
    unit = dict(original, unit_id='test.unit.overlapping-evidence', locator='bytes:1:33', sha256=sha(source[1:33]))
    item = next(x for x in manifest['coverage']['items'] if x['unit_id'] == original['unit_id'])
    controller['census']['units'].append(unit)
    manifest['coverage']['items'].append(dict(item, unit_id=unit['unit_id'], locator=unit['locator'], sha256=unit['sha256']))
    manifest['coverage']['totals']['input'] += 1
    manifest['coverage']['totals'][item['disposition'].replace('-', '_')] += 1
    repin_census(manifest, controller)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors


def supply_catalog(controller, catalog=None):
    if catalog is None:
        catalog = dict(schema_version=1, catalog_id='demo.catalog.read-only',
                       authority_id=controller['baseline']['generation']['authority_id'], pages={
                           'projects/outside/overview.md': dict(page_id='demo.outside.overview', parent='',
                                                               aliases=['Outside overview']),
                           'projects/outside/reference.md': dict(page_id='demo.outside.reference',
                               parent='projects/outside/overview.md', aliases=['Outside reference'])},
                       redirects={'projects/outside/old-reference.md': 'demo.outside.reference'})
    controller['page_catalog'] = encoded(catalog)
    controller['page_catalog_pin'] = {k: catalog[k] for k in ('catalog_id', 'authority_id')}
    controller['page_catalog_pin']['sha256'] = sha(controller['page_catalog'])
    return catalog


def revise_page(root, manifest, records, target):
    page = records['pages'][target]
    page['revision'] = sha(encoded({k: v for k, v in page.items()
                                   if k not in ('revision', 'previous_revision')}))
    next(x for x in manifest['changes'] if x['id'] == page['page_id'])['proposed_revision'] = page['revision']
    next(x for x in manifest['revision_map'] if x['id'] == page['page_id'])['new_revision'] = page['revision']
    save_records(root, manifest, records)


def add_catalog_link(packet, link):
    root, manifest, _ = packet
    records = read_records(root)
    records['pages']['projects/harbor-demo/update-preview.md']['links'].append('demo.outside.reference')
    path = root / EXAMPLES / 'feature.md'
    path.write_text(path.read_text().replace('<!-- /kb -->', link + '\n\n<!-- /kb -->'))
    revise_page(root, manifest, records, 'projects/harbor-demo/update-preview.md')


@pytest.mark.parametrize('link', [
    '[Reference](../outside/reference.md)', '[[projects/outside/reference]]',
    '[[Outside reference]]', '[Old reference](../outside/old-reference.md)',
])
def test_scoped_family_resolves_controller_catalog_without_external_outputs(packet, link):
    root, manifest, controller = packet
    supply_catalog(controller)
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    add_catalog_link(packet, link)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment
    assert result.identities['page_catalog']['sha256'] == controller['page_catalog_pin']['sha256']
    assert len(manifest['outputs']['files']) == 5


def test_scoped_parent_can_resolve_through_read_only_catalog_hierarchy(packet):
    root, manifest, controller = packet
    supply_catalog(controller)
    records = read_records(root)
    target = 'projects/harbor-demo/update-preview.md'
    records['pages'][target]['parent'] = 'projects/outside/reference.md'
    path = root / EXAMPLES / 'feature.md'
    path.write_text(path.read_text().replace('parent: "projects/harbor-demo/sailing-schedule.md"',
                                            'parent: "projects/outside/reference.md"'))
    # This is a separate assignment prepared before validation or any worker mutation.
    adoption = next(x for x in controller['census']['adoption_units'] if x['unit_id'] == 'demo.unit.467')
    adoption['output_sha256'] = sha(path.read_bytes().split(b'<!-- kb -->')[0])
    repin_census(manifest, controller)
    revise_page(root, manifest, records, target)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors


def test_ambiguous_external_basename_refuses_without_changing_catalog_assignment(packet):
    root, manifest, controller = packet
    catalog = supply_catalog(controller)
    catalog['pages']['projects/another/reference.md'] = dict(page_id='demo.outside.other-reference', parent='', aliases=[])
    supply_catalog(controller, catalog)
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    add_catalog_link(packet, '[[reference]]')
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert any(x.code == 'link' and x.detail == 'literal link unresolved/ambiguous: reference' for x in result.errors)
    assert controller == assignment


@pytest.mark.parametrize('mutation', ['bytes', 'pin', 'identity', 'authority', 'alias', 'target', 'page-id',
                                     'redirect', 'missing-parent', 'cycle'])
def test_catalog_pins_and_reference_graph_conflicts_refuse(packet, mutation):
    root, manifest, controller = packet
    catalog = supply_catalog(controller)
    assert check(root, manifest, controller).structural_valid
    if mutation == 'bytes':
        controller['page_catalog'] += b' '
    elif mutation == 'pin':
        controller['page_catalog_pin']['sha256'] = '0' * 64
    elif mutation == 'identity':
        controller['page_catalog_pin']['catalog_id'] = 'demo.catalog.other'
    else:
        entry = catalog['pages']['projects/outside/reference.md']
        if mutation == 'authority':
            catalog['authority_id'] = 'demo.authority.other'
        elif mutation == 'alias':
            entry['aliases'].append('Harbor Demo')
        elif mutation == 'target':
            catalog['pages']['person.md'] = catalog['pages'].pop('projects/outside/reference.md')
        elif mutation == 'page-id':
            entry['page_id'] = 'demo.person'
        elif mutation == 'redirect':
            catalog['redirects']['person.md'] = 'demo.outside.reference'
        elif mutation == 'missing-parent':
            entry['parent'] = 'projects/outside/missing.md'
        else:
            entry['parent'] = 'projects/outside/reference.md'
        supply_catalog(controller, catalog)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'catalog', 'alias', 'identity', 'parent'} & codes(result), result.errors


@pytest.mark.parametrize('mutation', ['no-catalog', 'worker-copy', 'missing-target', 'registry-expansion', 'output-expansion'])
def test_worker_cannot_invent_catalog_or_expand_the_assigned_outputs(packet, mutation):
    root, manifest, controller = packet
    catalog = supply_catalog(controller)
    assert check(root, manifest, controller).structural_valid
    add_catalog_link(packet, '[Reference](../outside/reference.md)')
    if mutation in ('no-catalog', 'worker-copy'):
        controller.pop('page_catalog')
        controller.pop('page_catalog_pin')
        if mutation == 'worker-copy':
            manifest['fixture']['page_catalog'] = catalog
    elif mutation == 'missing-target':
        supply_catalog(controller, dict(catalog, pages={}, redirects={}))
    else:
        records = read_records(root)
        extra = copy.deepcopy(records['pages']['person.md'])
        extra.update(page_id='demo.outside.reference', parent='', aliases=[])
        records['pages']['projects/outside/reference.md'] = extra
        if mutation == 'output-expansion':
            path = root / EXAMPLES / 'outside.md'
            path.write_text('# Unassigned outside page\n')
            manifest['outputs']['files'].append(dict(path=str(EXAMPLES / 'outside.md'),
                target='projects/outside/reference.md', sha256=sha(path.read_bytes()), expected_sha256=None))
        save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'link', 'catalog', 'identity', 'path'} & codes(result), result.errors
    if mutation == 'output-expansion':
        assert any(x.code == 'path' and x.detail == 'output path set differs from assignment' for x in result.errors)


@pytest.mark.parametrize('mutation', ['raw-shape', 'pages-shape', 'pin-only', 'bytes-only', 'pin-shape', 'entry-shape'])
def test_catalog_input_shape_and_presence_fail_without_crashing(packet, mutation):
    root, manifest, controller = packet
    catalog = supply_catalog(controller)
    assert check(root, manifest, controller).structural_valid
    if mutation == 'raw-shape':
        controller['page_catalog'] = []
    elif mutation == 'pages-shape':
        catalog['pages'] = []
        supply_catalog(controller, catalog)
    elif mutation == 'entry-shape':
        catalog['pages']['projects/outside/reference.md'] = []
        supply_catalog(controller, catalog)
    elif mutation == 'pin-only':
        controller['page_catalog'] = None
    elif mutation == 'bytes-only':
        controller['page_catalog_pin'] = None
    else:
        controller['page_catalog_pin'] = []
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'catalog' in codes(result), result.errors


@pytest.mark.parametrize('mutation', ['fixture-copy', 'worker-redirect'])
def test_worker_catalog_substitution_cannot_replace_the_actual_trusted_snapshot(packet, mutation):
    root, manifest, controller = packet
    catalog = supply_catalog(controller)
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    if mutation == 'fixture-copy':
        catalog['pages']['projects/outside/forged.md'] = dict(page_id='demo.outside.forged', parent='', aliases=[])
        manifest['fixture']['page_catalog'] = catalog
        add_catalog_link(packet, '[Forged](../outside/forged.md)')
        records = read_records(root)
        records['pages']['projects/harbor-demo/update-preview.md']['links'][-1] = 'demo.outside.forged'
    else:
        records = read_records(root)
        records['redirects']['projects/outside/worker-route.md'] = 'demo.outside.reference'
    revise_page(root, manifest, records, 'projects/harbor-demo/update-preview.md')
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'link', 'catalog'} & codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('wrapper', ['<span hidden>', '<span style="display:none">', '<span style="visibility:hidden">'])
@pytest.mark.parametrize('projection', ['person-fact', 'stale-label'])
def test_html_wrappers_cannot_hide_required_projections(packet, projection, wrapper):
    root, manifest, controller = packet
    if projection == 'stale-label':
        records = read_records(root)
        narrative = records['narratives']['demo.narrative.preview']
        narrative['state'] = 'stale'
        narrative['depends_on']['facts']['demo.preview.behavior'] = '0' * 64
        narrative['revision'] = sha(encoded({k: v for k, v in narrative.items()
                                           if k not in ('revision', 'previous_revision')}))
        next(x for x in manifest['changes'] if x['id'] == narrative['narrative_id'])[
            'proposed_revision'] = narrative['revision']
        path = root / EXAMPLES / 'feature.md'
        path.write_text(path.read_text().replace('State: current.', 'State: stale.'))
        save_records(root, manifest, records)
        text = 'Narrative: demo.narrative.preview. State: stale.'
    else:
        path = root / EXAMPLES / 'person.md'
        text = read_records(root)['facts']['demo.person.sketchbook']['text']
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path.write_text(path.read_text().replace(text, wrapper + text + '</span>'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'unsupported-html' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('html', [
    '<img alt="except: only after approval">',
    '<span title="except: only after approval"></span>',
    '<!-- except: only after approval -->',
])
def test_html_bearing_source_cannot_lose_an_exception_or_be_silently_rewritten(packet, html):
    root, manifest, controller = accepted_person_markdown(packet, 'Keep ' + html)
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace(html, ' ' * len(html)))
    refresh_outputs(root, manifest)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'unsupported-html' in codes(result), result.errors
    assert controller == assignment
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('markdown', [r'Use \<approval\> only.', 'Use &lt;approval&gt; only.', 'Use `<span hidden>` only.'])
def test_literal_angle_brackets_and_code_remain_visible_fact_content(packet, markdown):
    result = check(*accepted_person_markdown(packet, markdown))
    assert result.structural_valid, result.errors


@pytest.mark.parametrize('name', ['feature.md', 'project-map.md'])
@pytest.mark.parametrize('kind', ['push', 'merge', 'deploy'])
@pytest.mark.parametrize('field', ['repository', 'destination', 'evidence-source', 'evidence-version'])
def test_all_displayed_action_table_bindings_match_the_accepted_record(packet, name, kind, field):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    action = read_records(root)['actions']['demo.action.preview-' + kind]
    path = root / EXAMPLES / name
    lines = path.read_text().splitlines(keepends=True)
    old, new = {
        'repository': (action['repository_id'], 'demo.repo.harbor-tablet'),
        'destination': (action.get('remote_ref', action.get('environment')), 'production' if kind == 'deploy' else 'origin/other'),
        'evidence-source': ('demo-source-events', 'demo-source-policy'),
        'evidence-version': ('demo-source-events v1', 'demo-source-events v2'),
    }[field]
    for index, line in enumerate(lines):
        if '[a:' + action['action_id'] + ']' in line:
            assert old in line
            lines[index] = line.replace(old, new)
    path.write_text(''.join(lines))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize(('name', 'old', 'new'), [
    ('feature.md', 'Work revision: ' + '1' * 40, 'Work revision: ' + '2' * 40),
    ('feature.md', 'Deployment artifact: demo.artifact.preview-build.', 'Deployment artifact: demo.artifact.other-build.'),
    ('person.md', 'Evidence: REQUESTER,', 'Evidence: PRODUCER,'),
    ('person.md', 'demo-source-workshop-booked v2;', 'demo-source-workshop-plan v2;'),
    ('person.md', 'demo-source-workshop-booked v2;', 'demo-source-workshop-booked v1;'),
    ('person.md', 'owning receipt: demo.operation.workshop-booked.', 'owning receipt: demo.operation.other-workshop.'),
])
def test_shared_and_general_action_declarations_remain_bound(packet, name, old, new):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('kind', ['facts', 'narratives'])
def test_source_backed_claims_accept_canonical_registry_paths(packet, kind):
    root, manifest, controller = packet
    for rows in (controller['census']['units'], manifest['coverage']['items']):
        for unit in rows:
            if unit['unit'] == 'claim' and unit['destination'].startswith('records.json#/' + kind + '/'):
                unit['destination'] = str(EXAMPLES) + '/' + unit['destination']
    repin_census(manifest, controller)
    assignment = copy.deepcopy(controller)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment


def test_raw_style_block_cannot_change_mandatory_projection_visibility(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('<!-- kb -->',
        '<!-- kb -->\n\n<style>li { display: none; }</style>'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'unsupported-html' in codes(result), result.errors
    assert controller == assignment


def test_unsupported_projection_does_not_skip_unmanaged_preservation_checks(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    person = root / EXAMPLES / 'person.md'
    text = read_records(root)['facts']['demo.person.sketchbook']['text']
    person.write_text(person.read_text().replace(text, '<span hidden>' + text + '</span>'))
    topic = root / EXAMPLES / 'sailing-schedule.md'
    topic.write_bytes(topic.read_bytes()[:-102])
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert {'unsupported-html', 'preservation'} <= codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('name', ['person.md', 'feature.md'])
@pytest.mark.parametrize('lose_unmanaged', [False, True])
def test_html_between_comments_is_not_a_comment_only_exception(packet, name, lose_unmanaged):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    records_before = (root / EXAMPLES / 'records.json').read_bytes()
    path = root / EXAMPLES / name
    path.write_text(path.read_text().replace('<!-- kb -->',
        '<!-- kb -->\n\n<!-- audit --><style>li, p {display:none}</style><!-- audit -->'))
    if lose_unmanaged:
        topic = root / EXAMPLES / 'sailing-schedule.md'
        topic.write_bytes(topic.read_bytes()[:-102])
    refresh_outputs(root, manifest)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'unsupported-html' in codes(result), result.errors
    if lose_unmanaged:
        assert 'preservation' in codes(result), result.errors
    assert controller == assignment
    assert (root / EXAMPLES / 'records.json').read_bytes() == records_before
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('text', [
    '<!-- first --><!-- second -->',
    '<!-- first\n audit --> \n <!-- second -->',
    r'\<style\>li, p {display:none}\</style\>',
    '&lt;style&gt;li, p {display:none}&lt;/style&gt;',
])
def test_genuine_comments_and_escaped_literal_markup_remain_valid(packet, text):
    root, manifest, controller = packet
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('<!-- kb -->', '<!-- kb -->\n\n' + text + '\n'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment


@pytest.mark.parametrize('name', ['feature.md', 'project-map.md'])
@pytest.mark.parametrize('field', ['repository', 'kind', 'destination', 'state', 'owner', 'evidence'])
@pytest.mark.parametrize('mode', ['exact-code', 'contradiction-code'])
def test_action_cells_compare_all_visible_inline_code_content(packet, name, field, mode):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / name
    columns = {'repository': 2, 'kind': 3, 'destination': 4, 'state': 5, 'owner': 6, 'evidence': 7}
    lines = path.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if '[a:demo.action.preview-push]' in line:
            cells = line.split('|')
            value = cells[columns[field]].strip()
            marker = ' [a:demo.action.preview-push]' if name == 'project-map.md' and field == 'kind' else ''
            if marker:
                value = value.removesuffix(marker)
            if mode == 'exact-code':
                value = '**`' + value + '`**'
            else:
                value += ' ` (actually origin/main)`'
            cells[columns[field]] = ' ' + value + marker + ' '
            lines[index] = '|'.join(cells)
    path.write_text(''.join(lines))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    if mode == 'exact-code':
        assert result.structural_valid, result.errors
    else:
        assert not result.structural_valid
        assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('name', ['feature.md', 'person.md'])
@pytest.mark.parametrize('mode', ['exact-code', 'contradiction-code'])
def test_action_declarations_use_complete_visible_values(packet, name, mode):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / name
    declarations = (('feature/demo.feature.update-preview', 'Feature ID: demo.feature.update-preview',
                     'Work revision: ' + '1' * 40, 'Deployment artifact: demo.artifact.preview-build')
                    if name == 'feature.md' else (
                        'completed. Subject:', 'Subject: demo.subject.paper-workshop',
                        'kind: book', 'owner: state-api', 'Evidence: REQUESTER,',
                        'demo-source-workshop-booked v2', 'owning receipt: demo.operation.workshop-booked'))
    text = path.read_text()
    for old in declarations:
        assert old in text
        # Keep the existing declaration labels and punctuation outside the value.
        prefix, value = old.rsplit(': ', 1) if ': ' in old else ('', old)
        suffix = ''
        if value.endswith(','):
            value, suffix = value[:-1], ','
        if old == 'completed. Subject:':
            prefix, value, suffix = '', 'completed', '. Subject:'
        changed = ('`' + value + '`' if mode == 'exact-code' else value + '` (other)`')
        text = text.replace(old, (prefix + ': ' if prefix else '') + changed + suffix)
    path.write_text(text)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    if mode == 'exact-code':
        assert result.structural_valid, result.errors
    else:
        assert not result.structural_valid
        assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('content', ['` (actually another feature)`', '![another feature](other.png)'])
def test_feature_subject_cell_cannot_ignore_additional_content(packet, content):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'project-map.md'
    old = '[Sailing update preview](update-preview.md) | demo.repo.harbor-web | Push'
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old,
        '[Sailing update preview](update-preview.md) ' + content + ' | demo.repo.harbor-web | Push'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('field', ['repository', 'destination', 'evidence'])
def test_unsupported_cell_content_cannot_disappear(packet, field):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'feature.md'
    columns = {'repository': 2, 'destination': 4, 'evidence': 7}
    lines = path.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if '[a:demo.action.preview-push]' in line:
            cells = line.split('|')
            cells[columns[field]] += '![different value](other.png)'
            lines[index] = '|'.join(cells)
    path.write_text(''.join(lines))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize('name', ['person.md', 'feature.md'])
def test_inline_code_action_id_with_an_unbound_plain_decoy_is_not_a_marker(packet, name):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    identity = 'demo.action.book-workshop' if name == 'person.md' else 'demo.action.preview-push'
    marker = '[a:' + identity + ']'
    path = root / EXAMPLES / name
    text = path.read_text().replace(marker, '`' + marker + '`')
    if name == 'person.md':
        text = ''.join(line.rstrip('\n') + ' Unbound identifier: ' + marker + '\n'
                       if '`' + marker + '`' in line else line for line in text.splitlines(keepends=True))
    else:
        text = text.replace('<!-- /kb -->', 'Unbound identifier: ' + marker + '\n\n<!-- /kb -->')
    path.write_text(text)
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


def test_feature_subject_link_caption_retains_inline_code_formatting(packet):
    root, manifest, controller = packet
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'project-map.md'
    path.write_text(path.read_text().replace('[Sailing update preview](update-preview.md)',
                                            '[`Sailing update preview`](update-preview.md)'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment


def test_action_id_cell_cannot_ignore_visible_prefix_when_kind_is_separate(packet):
    root, manifest, controller = packet
    assert check(root, manifest, controller).structural_valid
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'feature.md'
    marker = '[a:demo.action.preview-push]'
    path.write_text(path.read_text().replace(marker, '`(actually demo.action.preview-merge)` ' + marker))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert not result.structural_valid
    assert 'projection' in codes(result), result.errors
    assert controller == assignment


@pytest.mark.parametrize(('comment', 'lose_unmanaged'), [
    ('<!-- audit --!><style>li, p {display:none}</style><!-- audit -->', False),
    ('<!-- audit --!><style>li, p {display:none}</style><!-- audit -->', True),
    ('<!--><style>li, p {display:none}</style><!-- audit -->', False),
    ('<!---><style>li, p {display:none}</style><!-- audit -->', False),
    ('<!-- outer <!-- inner -->', False),
    ('<!--' + 'audit <!-' + '-->', False),
])
def test_malformed_comment_exception_refuses_without_losing_preservation(packet, comment, lose_unmanaged):
    root, manifest, controller = packet
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('<!-- kb -->', '<!-- kb -->\n\n' + comment + '\n'))
    if lose_unmanaged:
        topic = root / EXAMPLES / 'sailing-schedule.md'
        topic.write_bytes(topic.read_bytes()[:-102])
    refresh_outputs(root, manifest)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    result = check(root, manifest, controller)
    assert 'unsupported-html' in codes(result), result.errors
    assert not result.structural_valid
    if lose_unmanaged:
        assert 'preservation' in codes(result), result.errors
    assert controller == assignment
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('comment', [
    '<!---->', '<!-- ordinary audit -->', '<!----> \n <!-- second -->',
    '<!--' + 'audit <!' + '-->', r'\<!-- audit --!>\<style\>literal\</style\>',
])
def test_well_formed_comment_exception_retains_valid_boundaries(packet, comment):
    root, manifest, controller = packet
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'person.md'
    path.write_text(path.read_text().replace('<!-- kb -->', '<!-- kb -->\n\n' + comment + '\n'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert result.structural_valid, result.errors
    assert controller == assignment


@pytest.mark.parametrize(('caption', 'valid'), [
    ('![Different feature](other.png)', False),
    ('Different wording', True),
    ('**Sailing** *update* `preview`', True),
])
def test_subject_link_caption_tokens_are_supported(packet, caption, valid):
    root, manifest, controller = packet
    assignment = copy.deepcopy(controller)
    path = root / EXAMPLES / 'project-map.md'
    path.write_text(path.read_text().replace('[Sailing update preview](update-preview.md)',
                                            '[' + caption + '](update-preview.md)'))
    refresh_outputs(root, manifest)
    result = check(root, manifest, controller)
    assert result.structural_valid == valid, result.errors
    if not valid:
        assert 'projection' in codes(result), result.errors
    assert controller == assignment


def test_subject_link_caption_checker_refuses_image_children():
    module = importlib.import_module('kelmo.wiki.file_contract')
    token = module._markdown().parseInline('[![Different feature](other.png)](update-preview.md)')[0]
    assert module._feature_subject(token, lambda _: {'demo.feature.update-preview'}) is None


@pytest.mark.parametrize(('kind', 'valid'), [('review', True), ('book', True), ('deploy', True),
                                             ('Review!', False), ('re', False), ('', False)])
def test_records_schema_accepts_enrolled_kind_grammar_not_only_the_pilot_names(kind, valid):
    module = importlib.import_module('kelmo.wiki.file_contract')
    records = read_records(ROOT)
    records['actions']['demo.action.book-workshop']['kind'] = kind
    errors = [error for error in module._validators()['records'].iter_errors(records)
              if list(error.absolute_path) == ['actions', 'demo.action.book-workshop', 'kind']]
    assert (not errors) == valid, errors


def example_feature_projections(text):
    module = importlib.import_module('kelmo.wiki.file_contract')
    kb = text[text.index('<!-- kb -->'):text.index('<!-- /kb -->')]
    return {key: values for key, _, values in
            module._action_projections(module._markdown().parse(kb), lambda token: set(), 'feature')}


def test_example_single_declarations_still_bind_every_displayed_action():
    text = (ROOT / EXAMPLES / 'feature.md').read_text()
    found = example_feature_projections(text)
    for kind in ('push', 'merge', 'deploy'):
        assert found['demo.action.preview-' + kind]['work_revision'] == '1' * 40
    assert found['demo.action.preview-deploy']['artifact_id'] == 'demo.artifact.preview-build'


@pytest.mark.parametrize('declaration', [
    'Work revision of demo.action.preview-push: ' + '2' * 40 + '.',
    'Deployment artifact of demo.action.preview-deploy: demo.artifact.other-build.',
])
def test_example_global_declaration_plus_per_action_one_is_ambiguous(declaration):
    text = (ROOT / EXAMPLES / 'feature.md').read_text()
    changed = text.replace('## Related pages', declaration + '\n\n## Related pages')
    found = example_feature_projections(changed)
    name = 'work_revision' if declaration.startswith('Work') else 'artifact_id'
    assert all(values.get(name) is None for values in found.values() if name in values)


@pytest.mark.parametrize('substitute', [False, True])
def test_full_family_binds_mixed_revisions_to_each_named_action(packet, substitute):
    from kelmo.wiki.file_contract import entity_revision
    root, manifest, controller = packet
    records = read_records(root)
    source_id = controller['state_source_id']
    state = manifest['fixture']['state_owner_snapshot']
    key = 'demo.action.preview-merge'
    revised = records['actions'][key]
    revised['work_revision'] = '2' * 40
    revised['revision'] = entity_revision(revised)
    state['actions'][key] = copy.deepcopy(revised)
    next(row for row in manifest['changes'] if row['id'] == key)['proposed_revision'] = revised['revision']
    narrative = records['narratives']['demo.narrative.preview']
    narrative['state'] = 'stale'
    narrative['revision'] = entity_revision(narrative)
    next(row for row in manifest['changes'] if row['id'] == narrative['narrative_id'])[
        'proposed_revision'] = narrative['revision']

    # Prepare a new independent controller fixture before mutating its proposed page.
    data = encoded(state)
    controller['sources'][source_id] = data
    for rows in (manifest['inputs']['files'], controller['census']['sources']):
        next(row for row in rows if row['source_id'] == source_id)['sha256'] = sha(data)
    for unit in controller['census']['units']:
        if unit['source_id'] != source_id:
            continue
        if unit['unit'] == 'bytes':
            unit.update(locator=f'bytes:0:{len(data)}', sha256=sha(data))
        else:
            unit['sha256'] = sha(encoded(pointer(state, unit['locator'])))
        next(row for row in manifest['coverage']['items'] if row['unit_id'] == unit['unit_id']).update(
            locator=unit['locator'], sha256=unit['sha256'])
    repin_census(manifest, controller)
    feature = root / EXAMPLES / 'feature.md'
    text = feature.read_text().replace('Work revision: ' + '1' * 40 + '. ', '')
    annotations = '\n\n'.join(
        f'Work revision of {action_id}: {action["work_revision"]}.'
        for action_id, action in records['actions'].items() if action_id.startswith('demo.action.preview-'))
    text = text.replace('## Related pages', annotations + '\n\n## Related pages')
    text = text.replace('State: current.', 'State: stale.')
    if substitute:
        text = text.replace('Work revision of ' + key + ':',
                            'Work revision of demo.action.preview-push:')
    feature.write_text(text)
    for name in ('harbor-demo.md', 'sailing-schedule.md'):
        path = root / EXAMPLES / name
        if path.exists():
            path.write_text('\n'.join(
                line.replace('1' * 40, '2' * 40) if '[a:' + key + ']' in line else line
                for line in path.read_text().split('\n')))
    save_records(root, manifest, records)
    result = check(root, manifest, controller)
    assert result.structural_valid is not substitute, result.errors
    if substitute:
        assert 'projection' in codes(result)
    else:
        assert any(issue.code == 'stale-dependency' for issue in result.unresolved)


def general_action_check(packet, kind, **fields):
    from kelmo.wiki.file_contract import ControllerInput, _Check
    root, _, controller = packet
    records = read_records(root)
    state = json.loads(controller['sources'][controller['state_source_id']])
    key = 'demo.action.book-workshop'
    for snapshot in (records['actions'][key], state['actions'][key]):
        snapshot.update(kind=kind, **fields)
    validator = _Check(root, ControllerInput(**controller), False)
    validator.action(key, records['actions'][key], records, state)
    return [error for error in validator.errors if error.path == key]


def test_offline_action_check_accepts_a_non_git_review_and_keeps_kind_specific_fields(packet):
    assert general_action_check(packet, 'review') == []
    assert any('kind-specific' in error.detail
               for error in general_action_check(packet, 'review', repository_id='demo.repo.harbor-web'))
