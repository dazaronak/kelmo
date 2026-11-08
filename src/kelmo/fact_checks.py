"""Bind agent reconciliation declarations to durable local evidence."""
import json
import re
import uuid

from .errors import KelmoError
from .wiki import engine


def schema(connection):
    with connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS fact_searches (
            id TEXT PRIMARY KEY, binding TEXT NOT NULL UNIQUE,
            snapshot TEXT NOT NULL, candidates TEXT NOT NULL
        )''')


def binding(text, target):
    return engine.digest(engine.encoded([text, target]))


def select_candidates(facts, text, target):
    words = set(re.findall(r'\w+', text.casefold()))
    return sorted(
        ((key, fact) for key, fact in facts.items()
         if words & set(re.findall(r'\w+', fact['text'].casefold()))),
        key=lambda pair: (-len(words & set(re.findall(r'\w+', pair[1]['text'].casefold()))),
                          pair[1]['target'] != target,
                          pair[0]))


def candidate_snapshot(facts, text, target):
    return {key: fact['revision'] for key, fact in select_candidates(facts, text, target)[:20]}


def search(connection, facts, text, target):
    if not isinstance(text, str) or not text.strip() or len(text) > 400:
        raise KelmoError('E_INPUT', 'Provide proposed fact text, at most 400 characters.')
    ranked = select_candidates(facts, text, target)
    snapshot = engine.digest(engine.encoded(candidate_snapshot(facts, text, target)))
    identity = binding(text, target) + ':' + snapshot
    candidates = [dict(fact_key=key, text=fact['text'], target=fact['target'],
                       revision=fact['revision']) for key, fact in ranked[:20]]
    with connection:
        connection.execute('''INSERT OR IGNORE INTO fact_searches
            (id,binding,snapshot,candidates) VALUES (?,?,?,?)''',
            (uuid.uuid4().hex, identity, snapshot, json.dumps(candidates)))
    row = connection.execute('SELECT id,candidates FROM fact_searches WHERE binding=?',
                             (identity,)).fetchone()
    return {'search_id': row['id'], 'candidates': json.loads(row['candidates']),
            'total_facts': len(facts), 'matched_facts': len(ranked), 'truncated': len(ranked) > 20,
            'method': 'lexical', 'proves_absence': False}


class CheckProblem(Exception):
    """A fact check that failed, named by the fact it belongs to and the repair it needs."""


def reject(cause, repair, fact_key=None):
    fact_key = fact_key if isinstance(fact_key, str) and fact_key else None
    raise CheckProblem((('fact ' + fact_key + ': ') if fact_key else '')
                       + cause + '; permitted repair: ' + repair)


FALLBACK = ('Each fact needs a current existing reference or a matching search receipt in fact-checks; '
            'permitted repair: rebuild both blocks from one kelmo reconcile run.')


def validate(connection, registry, content):
    if engine.digest(content) in registry['notes']:
        return
    try:
        check(connection, registry, content)
    except CheckProblem as problem:
        raise KelmoError('E_FACT_CHECK', str(problem)) from None
    except ValueError as error:
        raise KelmoError('E_FACT_CHECK', str(error) or FALLBACK) from None
    except (KeyError, TypeError):
        raise KelmoError('E_FACT_CHECK', FALLBACK) from None


def check(connection, registry, content):
    changes = engine.extract_changes(content)
    if changes is None:
        reject('the note names no wiki facts to check',
               'add a wiki-changes block, or capture the note with archive intent')
    blocks = re.findall(r'^```fact-checks\s*\n(.*?)^```\s*$', content, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1:
        reject('the note must carry exactly one fact-checks block',
               'add one fact-checks block naming every promoted fact once')
    try:
        checks = json.loads(blocks[0])
    except ValueError:
        reject('the fact-checks block is not JSON', 'write the checks as a JSON array')
    if not isinstance(checks, list):
        reject('the fact-checks block must be an array of checks', 'write the checks as a JSON array')
    by_key = {}
    for item in checks:
        if not isinstance(item, dict) or set(item) != {'fact_key', 'verified_match'}:
            reject('a check carries fact_key and verified_match only',
                   'name the fact and give one verified_match',
                   item.get('fact_key') if isinstance(item, dict) else None)
        if not isinstance(item['fact_key'], str):
            reject('a check must name the fact it verifies', 'give each check the fact_key of a promoted fact')
        if item['fact_key'] in by_key:
            reject('its check appears more than once', 'keep one check per fact', item['fact_key'])
        by_key[item['fact_key']] = item['verified_match']
    promoted = {change['fact_key'] for change in changes}
    for key in sorted(set(by_key) - promoted):
        reject('the checks name a fact the note does not promote',
               'remove this check or add the fact to wiki-changes', key)
    for key in sorted(promoted - set(by_key)):
        reject('the note promotes this fact without a check',
               'run kelmo reconcile and add the check it supports', key)
    facts = registry['facts']
    for change in changes:
        key = change['fact_key']
        check_value = by_key[key]
        if not isinstance(check_value, dict):
            reject('its verified_match must be an existing reference or a not_found search receipt',
                   'copy one of the two shapes kelmo reconcile returns', key)
        prior = facts.get(key)
        if check_value.get('result') == 'existing':
            existing(connection, change, check_value, prior)
        elif check_value.get('result') == 'not_found':
            not_found(connection, change, check_value, prior, facts)
        else:
            reject('its verified_match result must be existing or not_found',
                   'use the result kelmo reconcile returned for this text and target', key)


def existing(connection, change, check_value, prior):
    key = change['fact_key']
    if set(check_value) != {'result', 'fact_key', 'revision'}:
        reject('an existing reference carries result, fact_key and revision only',
               'drop the extra fields', key)
    if check_value['fact_key'] != key:
        reject('its check names another fact', 'make the check name this fact', key)
    if prior is None:
        reject('the check claims an existing fact, but the registry has none',
               'run kelmo reconcile and use the not_found receipt it returns', key)
    if check_value['revision'] != prior['revision']:
        reject('the check names revision ' + str(check_value['revision'])[:12]
               + ' while the registry holds ' + prior['revision'][:12],
               'run kelmo reconcile and copy the current revision', key)
    if change['expected_revision'] != prior['revision']:
        reject('the change expects revision ' + str(change['expected_revision'])[:12]
               + ' while the check confirms ' + prior['revision'][:12],
               'set expected_revision to the revision the check names', key)


def not_found(connection, change, check_value, prior, facts):
    key = change['fact_key']
    if set(check_value) != {'result', 'search_id'}:
        reject('a not_found reference carries result and search_id only',
               'drop the extra fields', key)
    if prior is not None:
        reject('the check claims nothing matched, but this fact is already in the registry',
               'run kelmo reconcile and reference the existing fact with its current revision', key)
    if change['expected_revision'] is not None:
        reject('a not_found check cannot carry an expected revision',
               'set expected_revision to null for a new fact', key)
    if not isinstance(check_value['search_id'], str):
        reject('its search_id must be the id kelmo reconcile returned', 'copy the search_id from the receipt', key)
    receipt = connection.execute('SELECT binding FROM fact_searches WHERE id=?',
                                 (check_value['search_id'],)).fetchone()
    snapshot = engine.digest(engine.encoded(
        candidate_snapshot(facts, change['text'], change['target'])))
    expected = binding(change['text'], change['target']) + ':' + snapshot
    if receipt is None or receipt['binding'] != expected:
        reject('its search receipt does not match this text and target against the current registry',
               'run kelmo reconcile --text this_fact --target ' + change['target']
               + ' and use the search_id it returns', key)
