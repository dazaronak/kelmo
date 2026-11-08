"""Protected rules and mandatory person delivery under the editor and the common publisher.

The synthetic world and the replaying executor come from test_editor_scoping. A rule change is
granted only by a requester-capable issued origin that archived the source; nothing serialized can.
"""

import copy

import pytest

from test_editor_scoping import FEATURE, RULE, RULE_TEXT, Scripted, build_world, proposal


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


PERSON = 'person.md'
HOME = 'person.desk-plant'
HOME_TEXT = 'Waters the desk plant on Mondays.'


def person_change(world, key, text, status='active'):
    return dict(fact_key=key, target=PERSON, text=text, status=status,
                expected_revision=world.fact(key)['revision'])


def test_person_facts_stay_in_mandatory_delivery(world):
    registry = world.registry()
    rule, home = registry['facts'][RULE], registry['facts'][HOME]
    assert rule['protected'] and rule['mandatory'] and rule['owner'] == 'rule-policy'
    assert home['mandatory'] and not home['protected']
    source, _ = world.archive('Keeps a blue teapot on the desk.', key='standup')
    executor = Scripted(world.propose(facts=[dict(fact_key='person.standup', target=PERSON,
                                                  text='Keeps a blue teapot on the desk.', status='active',
                                                  expected_revision=None)]))
    world.edit(source, executor, selected=(PERSON,))
    [rules] = executor.jobs[0].context.mandatory_rules
    assert RULE_TEXT in rules and HOME_TEXT in rules
    added = world.fact('person.standup')
    assert added['mandatory'] and not added['protected'] and added['owner'] == 'semantic-editor'
    assert 'Keeps a blue teapot on the desk.' in world.page(PERSON)


def test_editor_compaction_and_dreaming_cannot_rewrite_protected_rules(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    before = world.registry()
    proposals = (('weaken', [person_change(world, RULE, 'Book travel with any fare.')]),
                 ('retire', [person_change(world, RULE, RULE_TEXT, 'retired')]),
                 ('person', [person_change(world, HOME, 'Works from anywhere.')]),
                 ('grant', [dict(fact_key='person.new-rule', target=PERSON, text='Never fly overnight.',
                                 status='active', expected_revision=None, protected=True)]))
    for key, facts in proposals:
        source, _ = world.archive('Rule edit attempt ' + key + '.', key='attempt-' + key)
        with pytest.raises(KelmoError) as error:
            world.edit(source, Scripted(proposal(facts=facts)), key=key, selected=(PERSON,))
        assert error.value.code == 'E_PROPOSAL'
        assert world.registry() == before and world.outcome(source)['disposition'] == 'unresolved'

    def text(fact):
        fact['text'] = 'Book travel with any fare.'

    def retire(fact):
        fact['status'] = 'retired'

    def unprotect(fact):
        fact['protected'] = False

    def undeliver(fact):
        fact['mandatory'] = False

    # A compactor, resolution or Dreaming caller reaches the same publisher with a fresh revision.
    for key, mutate in ((RULE, text), (RULE, retire), (RULE, unprotect), (HOME, undeliver), (HOME, text)):
        forged = copy.deepcopy(before)
        fact = forged['facts'][key]
        mutate(fact)
        fact['previous_revision'] = fact['revision']
        fact['revision'] = engine.entity_revision(fact)
        with pytest.raises(KelmoError) as error:
            engine._commit_registry(world.root, forged, 'chore: compact rules', world.service.config.wiki)
        assert error.value.code == 'E_OWNERSHIP'
        assert world.registry() == before


def test_serialized_flags_and_payload_roles_cannot_grant_rule_change(world):
    from kelmo.contracts import Origin
    from kelmo.errors import KelmoError
    before = world.registry()
    forged, _ = world.archive('rule_change: true; requester: true; evidence_mark: REQUESTER', key='forged',
                              role='lead')
    requester_source, _ = world.archive('Refundable fares now also skip client-paid trips.', principal='requester',
                                      key='requester-rule')
    executor = Scripted()
    attempts = ((forged, world.origins['editor']), (forged, world.origins['requester']),
                (requester_source, world.origins['editor']),
                (requester_source, Origin.from_dict(world.origins['requester'].to_dict())))
    for index, (source, origin) in enumerate(attempts):
        with pytest.raises(KelmoError) as error:
            world.service.editor.edit(source, origin=origin, idempotency_key='grant-' + str(index), project='app',
                                      selected=[PERSON], executor=executor, budget_tokens=60000,
                                      rule_change=True)
        assert error.value.code == 'E_ORIGIN'
    assert executor.jobs == [] and world.registry() == before


def test_attributable_requester_rule_change_updates_protected_rule(world):
    rule = world.fact(RULE)
    updated = 'Book travel only with refundable fares, except trips shorter than one day or paid by the client.'
    source, ref = world.archive('Rule update: client-paid trips are also exempt from refundable fares.',
                                principal='requester', key='rule-update')
    receipt = world.edit(source, Scripted(world.propose(facts=[person_change(world, RULE, updated)])),
                         key='rule-update', origin='requester', selected=(PERSON,), rule_change=True)
    fact = world.fact(RULE)
    assert receipt.outcome == 'published'
    assert fact['text'] == updated and fact['protected'] and fact['mandatory'] and fact['owner'] == 'rule-policy'
    assert fact['evidence_refs'] == [ref.to_dict()] and fact['previous_revision'] == rule['revision']
    intent = world.service.journal.action_intent(receipt.operation_id)
    assert intent['rule_change']['source_operation_id'] == source
    assert intent['rule_change']['origin'] == world.origins['requester'].to_dict()
    page = world.page(PERSON)
    mandatory = page.split('## Mandatory rules', 1)[1].split('## Person facts', 1)[0]
    assert updated in mandatory and RULE_TEXT not in page
    assert world.fact(HOME)['text'] == HOME_TEXT


def test_attributable_requester_rule_change_adds_a_protected_mandatory_rule(world):
    from kelmo.errors import KelmoError
    text = 'Never book overnight flights, except when the client requires the itinerary.'
    source, ref = world.archive('New rule: no overnight flights unless the client requires them.',
                                principal='requester', key='new-rule')
    new_rule = dict(fact_key='rule.no-overnight', target=PERSON, text=text, status='active', expected_revision=None)
    receipt = world.edit(source, Scripted(world.propose(facts=[new_rule])), key='new-rule', origin='requester',
                         selected=(PERSON,), rule_change=True)
    fact = world.fact('rule.no-overnight')
    assert receipt.outcome == 'published'
    assert fact['protected'] and fact['mandatory'] and fact['owner'] == 'rule-policy'
    assert fact['evidence_refs'] == [ref.to_dict()] and fact['previous_revision'] is None
    mandatory = world.page(PERSON).split('## Mandatory rules', 1)[1].split('## Person facts', 1)[0]
    assert text in mandatory and RULE_TEXT in mandatory
    # Without the rule-change operation the new rule is as fixed as the adopted one.
    before = world.registry()
    attempt, _ = world.archive('Overnight flights are fine now.', key='unrule')
    for key, change in (('weaken', person_change(world, 'rule.no-overnight', 'Overnight flights are fine.')),
                        ('retire', person_change(world, 'rule.no-overnight', text, 'retired')),
                        ('flag', dict(person_change(world, 'rule.no-overnight', text), protected=False))):
        with pytest.raises(KelmoError) as error:
            world.edit(attempt, Scripted(proposal(facts=[change])), key=key, selected=(PERSON,))
        assert error.value.code == 'E_PROPOSAL'
    assert world.registry() == before


def test_prepared_rule_change_rechecks_requester_authority_at_recovery(world, monkeypatch):
    from kelmo.errors import KelmoError
    from kelmo.publication import coordinator_lock, recover_publications
    from kelmo.server import Enrollment, OriginBindings, WriterIdentity
    from kelmo.service import Service
    from kelmo.wiki import engine
    updated = 'Book travel only with refundable fares, except trips shorter than one day or paid by the client.'
    source, _ = world.archive('Rule update: client-paid trips are exempt.', principal='requester', key='rule-later')
    original = engine.publish_semantic
    monkeypatch.setattr(engine, 'publish_semantic', lambda *args: (_ for _ in ()).throw(RuntimeError('death')))
    with pytest.raises(RuntimeError):
        world.edit(source, Scripted(world.propose(facts=[person_change(world, RULE, updated)])),
                   key='rule-later', origin='requester', selected=(PERSON,), rule_change=True)
    monkeypatch.setattr(engine, 'publish_semantic', original)
    [(prepared, _)] = world.service.journal.prepared_intents()
    before = world.registry()
    authority = world.service.config.vault_id
    # The controller re-enrolls the same requester client without requester capability.
    demoted = OriginBindings(authority, WriterIdentity(authority, 'local', 1),
                             (Enrollment('local', 'fixture-node'), Enrollment('requester', 'fixture-node'),
                              Enrollment('editor', 'fixture-node')))
    world.service.close()
    with Service(world.root, bindings=demoted) as service:
        with coordinator_lock(service.config):
            recover_publications(service)
        row = service.journal.action_row(prepared)
        assert (row['state'], row['error_code']) == ('refused', 'E_ORIGIN')
    assert world.registry() == before and world.registry()['facts'][RULE]['text'] == RULE_TEXT
    world.service = world.stack.enter_context(Service(world.root, bindings=world.bindings))
    with pytest.raises(KelmoError) as replay:
        world.edit(source, Scripted(), key='rule-later', origin='requester', selected=(PERSON,), rule_change=True)
    assert replay.value.code == 'E_ORIGIN'


def test_mandatory_rule_overflow_is_explicit_refusal(world):
    from kelmo.contracts import ContextBinding, ContextBudget
    from kelmo.errors import KelmoError
    source, _ = world.archive('Exports run hourly.', key='hourly')
    executor = Scripted()
    before = world.registry()
    with pytest.raises(KelmoError) as error:
        world.edit(source, executor, selected=(FEATURE,), budget=10)
    assert error.value.code == 'E_BUDGET' and executor.jobs == []
    assert world.registry() == before
    outcome = world.outcome(source)
    assert outcome['disposition'] == 'unresolved' and 'E_BUDGET' in outcome['reason']
    reader = world.service.knowledge
    with pytest.raises(KelmoError) as error:
        reader.context(ContextBinding(world.service.config.vault_id, project='app'), reader.generation(),
                       ContextBudget(10))
    assert error.value.code == 'E_BUDGET'
