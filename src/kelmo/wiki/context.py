"""Bounded editor context for one project.

An editor that has to fit what it knows about a project inside a bounded prompt gets three layers here: the
page index, the complete facts of the pages it selected, and a one-line digest of the open work on the pages
it did not. What does not fit is named — `omitted` carries the page and the fact count — so an omitted fact
is never read as a fact the store does not hold. The exact read of any page is `pages.page_facts`, which is
not bounded and is how a caller recovers what this view left out.

The estimate is the one the wiki budget already uses (`wiki/policy.py`: UTF-8 bytes / 2.5), so a context
budget and a vault budget are the same measure.
"""
from __future__ import annotations

import json
import math

from . import pages
from .policy import TOKEN_ESTIMATE
from kelmo.contracts import ContextBundle
from kelmo.errors import KelmoError

DIGEST_ROW = 40  # the fixed cost of one digest line's framing, beside its text


def estimate(text):
    return math.ceil(len(text.encode('utf-8')) / TOKEN_ESTIMATE)


def _row_size(row):
    return estimate(json.dumps(row, ensure_ascii=False, sort_keys=True))


def _digest_row(fact):
    return {'fact_key': fact['fact_key'], 'target': fact['target'], 'status': fact['status'],
            'text': fact['text'], 'id': fact['id'], 'revision': fact['revision']}


def editor_context(registry, project, budget_tokens, selected=()):
    """The bounded view of one project: index, selected facts, open-work digest, and what it left out.

    `selected` is the caller's own order and it is honoured: the first page asked for is the first page given
    facts, so a caller cannot be starved by another caller's choice, and no page's facts are split across the
    boundary — a page is complete or it is named in `omitted`.
    """
    if not isinstance(project, str) or not pages.PROJECT.fullmatch(project):
        raise ValueError('a project name must be 1 to 60 characters of a-z 0-9 _ -; permitted repair: '
                         'name the project as it appears in projects/<project>/')
    if not isinstance(budget_tokens, int) or budget_tokens < 1:
        raise ValueError('budget_tokens must be a positive integer')
    if not isinstance(selected, (list, tuple)) or any(not isinstance(target, str) for target in selected):
        raise ValueError('selected must be a list of page targets')
    index = pages.page_index(registry, [project])[project]
    facts = {fact['fact_key']: fact for fact in registry['facts'].values()
             if pages.project_of(fact['target']) == project}
    used = sum(_row_size(row) for row in index)
    result = {'project': project, 'budget_tokens': budget_tokens,
              'metric': 'estimated tokens: UTF-8 bytes / ' + str(TOKEN_ESTIMATE),
              'pages': list(index), 'selected': [], 'facts': {}, 'open_digest': [],
              'omitted': {'pages': [], 'facts': []}, 'truncated': False}
    if used > budget_tokens:
        raise ValueError('project ' + project + ': its page index alone is ' + str(used) + ' estimated tokens '
                         'against a budget of ' + str(budget_tokens) + '; permitted repair: raise the budget or '
                         'ask for one project at a time')
    for target in dict.fromkeys(selected):
        row = {'target': target, 'facts': len([key for key, fact in facts.items() if fact['target'] == target])}
        held = [fact for key, fact in sorted(facts.items()) if fact['target'] == target]
        cost = sum(_row_size(fact) for fact in held)
        if used + cost > budget_tokens:
            result['omitted']['pages'].append(row)
            result['truncated'] = True
            continue
        result['selected'].append(target)
        result['facts'][target] = held
        used += cost
    for key, fact in sorted(facts.items()):
        if fact['status'] not in pages.OPEN or fact['target'] in result['facts']:
            continue
        row = _digest_row(fact)
        cost = _row_size(row) + DIGEST_ROW
        if used + cost > budget_tokens:
            result['omitted']['facts'].append({'fact_key': key, 'target': fact['target']})
            result['truncated'] = True
            continue
        result['open_digest'].append(row)
        used += cost
    result['used_tokens'] = used
    return result


def generation_context(available, mandatory, generation, budget, source_mappings, *,
                       freshness, sources_complete=True):
    """Fit complete immutable pages while delivering every mandatory page whole.

    Mandatory pages are pages.mandatory_paths: person pages and every page holding a
    mandatory record. Each is delivered once, in mandatory_rules, even when it is also a
    project page. Missing budget is an explicit omission, not a missing page. Person content
    is never filtered by role, status or a legacy mandatory flag. A mandatory
    block that exceeds the ceiling refuses rather than silently dropping rules.
    Source mappings describe provenance; they do not authenticate evidence.
    """
    rules = tuple(page.content.decode('utf-8') for page in mandatory)
    used = sum(estimate(text) for text in rules)
    required = {page.path for page in mandatory}

    def cost(page):
        return estimate(page.path + '\n' + page.content.decode('utf-8'))

    if used > budget.max_tokens:
        raise KelmoError('E_BUDGET', 'The context ceiling cannot hold all mandatory content.')
    held, omissions = [], []
    for page in available:
        if page.generation != generation:
            raise KelmoError('E_GENERATION', 'Context pages cannot mix publication generations.')
        if page.path in required:
            # Already delivered whole as a mandatory rule page: counted and delivered once.
            continue
        if used + cost(page) <= budget.max_tokens:
            held.append(page)
            used += cost(page)
        else:
            omissions.append(page.path)
    if any(page.generation != generation for page in mandatory):
        raise KelmoError('E_GENERATION', 'Mandatory rules cannot mix publication generations.')
    return ContextBundle(generation, rules, tuple(held), tuple(omissions), freshness,
                         source_mappings, not omissions and freshness == 'current' and sources_complete)
