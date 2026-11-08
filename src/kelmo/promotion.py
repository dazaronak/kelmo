"""Fact promotion: a note's declared wiki-changes become one editor proposal.

The registry has one semantic writer, the scoped editor, and the editor publishes only
prepared intents. A promoted note already carries a prepared proposal: its wiki-changes block
names every fact and its fact-checks block carries the reconciliation receipts the gate demands.
No model is consulted; the declared facts answer the editor as a ModelExecutor outcome, and the
editor's own normalization, reconciliation gate, ownership guard, baseline check and publication
apply unchanged.
"""
import json
import re

from .errors import KelmoError
from .executor import ModelOutcome
from .wiki import engine, pages

FACT_FIELDS = ('fact_key', 'target', 'text', 'status', 'expected_revision')
# A declared proposal never expands, so the budget only has to hold the selected pages (estimated
# tokens, UTF-8 bytes / 2.5).
BUDGET_TOKENS = 1_000_000
CHECKS = re.compile(r'^```fact-checks\s*\n(.*?)^```\s*$', re.MULTILINE | re.DOTALL)


class DeclaredChanges:
    """A ModelExecutor that answers the editor with the facts one note declares."""

    # The declared proposal cannot change, so a refusal is not sent back for repair.
    revises = False

    def __init__(self, proposal):
        self.proposal = proposal

    def execute(self, job):
        return ModelOutcome('proposal', self.proposal)


def request_key(operation_id, events, existing=None):
    """The editor key this attempt runs under.

    Only an explicit queue retry advances the attempt number; a published receipt survives a crash.
    `existing(key)` returns the editor request recorded under a key, if any: an earlier attempt that
    is still open, prepared or finished is resumed under its own key, so the retry settles the
    recorded answer, publishes the prepared intent or replays the receipt instead of proposing the
    same facts a second time against the registry that first attempt already changed. A new attempt
    key is used only when every recorded attempt ended refused.
    """
    attempts = max(0, sum(row['stage'] == 'queued' for row in events) - 1)
    keys = ['promote:' + operation_id + ':' + str(attempt) for attempt in range(attempts + 1)]
    for key in reversed(keys) if existing is not None else ():
        row = existing(key)
        if row is not None and row['state'] in ('open', 'prepared', 'finished'):
            return key
    return keys[-1]


def declared_request(registry, content, packet_project):
    """The editor request one promoted note declares: project, selected pages, proposal, rule_change.

    `replaces` retires each replaced fact in the same proposal, with the registry's own current
    revision as its receipt. Every page the proposal reaches must already be registered: the
    editor edits registered pages and creates pages only through its own proposals.
    """
    changes = engine.extract_changes(content)
    if changes is None:
        raise KelmoError('E_INPUT', 'Promotion requires a valid wiki-changes block.')
    blocks = CHECKS.findall(content)
    try:
        checks = json.loads(blocks[0]) if blocks else []
    except ValueError as error:
        raise KelmoError('E_INPUT', 'The fact-checks block is not JSON.') from error
    facts, targets, declared = [], [], {change['fact_key'] for change in changes}
    for change in changes:
        facts.append({name: change[name] for name in FACT_FIELDS})
        prior = registry['facts'].get(change['fact_key'])
        targets += [change['target']] + ([prior['target']] if prior else [])
        for key in change['replaces']:
            replaced = registry['facts'].get(key)
            if replaced is None:
                raise KelmoError('E_INPUT', 'fact ' + change['fact_key'] + ' replaces ' + key
                                + ', which the registry does not hold.')
            if key in declared or replaced['status'] == 'retired':
                continue
            declared.add(key)
            facts.append({'fact_key': key, 'target': replaced['target'], 'text': replaced['text'],
                          'status': 'retired', 'expected_revision': replaced['revision']})
            checks.append({'fact_key': key, 'verified_match': {'result': 'existing', 'fact_key': key,
                                                               'revision': replaced['revision']}})
            targets.append(replaced['target'])
    selected = list(dict.fromkeys(targets))
    unregistered = [target for target in selected if target not in registry['pages']]
    if unregistered:
        raise KelmoError('E_INPUT', 'Promotion reaches registered pages only; '
                                   'unregistered: ' + ', '.join(unregistered[:5])
                        + '. Create the page through the editor first, or target an existing page.')
    person = engine.person_targets(registry)
    guarded = any(target in person for target in selected) or any(
        registry['facts'][fact['fact_key']].get('protected') or registry['facts'][fact['fact_key']].get('mandatory')
        for fact in facts if fact['fact_key'] in registry['facts'])
    project = packet_project if isinstance(packet_project, str) and pages.PROJECT.fullmatch(packet_project) else None
    if project in (None, 'default'):
        project = pages.project_of(selected[0]) or project or 'default'
    return {'project': project, 'selected': selected, 'rule_change': guarded,
            'proposal': {'facts': facts, 'fact_checks': checks}}
