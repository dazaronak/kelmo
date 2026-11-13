"""The scoped editor's view of one pinned generation.

One call answers what a model job may read: the archived source, every mandatory person page in
full, the project index, then the selected pages and every page the model asked for. A sent page is
its registry entities with their revisions plus its unmanaged text, so each fact reaches the model
once (a selected mandatory page is the exception: its full text and its entities are both sent). Pages linked to the selected ones stay index rows until the model asks for them: a project map
that every page links to costs one page, not its whole project. What the budget cannot hold is named
in `omitted`, and `omitted_budget` says for each of them what it needed and what was left; a page is
sent whole or not at all, and only sent pages become the editable scope.
The source and the mandatory rules are never trimmed: when they do not fit, the call refuses.
"""
from __future__ import annotations

import json

from kelmo.contracts import ContextBundle
from kelmo.errors import KelmoError

from . import pages
from .context import estimate


def _cost(value):
    return estimate(json.dumps(value, ensure_ascii=False, sort_keys=True))


def receipt_cost(reconciliations, candidates=()):
    """What the reconciliation receipts and the candidate facts they name cost the payload.

    The consult loop measures a reconcile round with this before running it, so the ceiling is decided
    with no search done and no page left unsent; `editor_view` charges exactly this figure. An empty
    candidate list costs nothing, because the payload does not carry the key then."""
    reconciliations, candidates = list(reconciliations), list(candidates)
    return _cost(reconciliations) + (_cost(candidates) if candidates else 0)


def dependencies(registry, target):
    """Registered pages one page depends on: parent, links both ways, narrative and action subjects."""
    entries = registry['pages']
    entry = entries[target]
    by_id = {value['page_id']: key for key, value in entries.items()}
    found = {by_id[page_id] for page_id in entry['links'] if page_id in by_id}
    found |= {key for key, value in entries.items() if entry['page_id'] in value['links']}
    if entry['parent'] in entries:
        found.add(entry['parent'])
    for narrative in registry['narratives'].values():
        if narrative['page_id'] != entry['page_id']:
            continue
        found |= {registry['facts'][key]['target'] for key in narrative['depends_on']['facts']
                  if key in registry['facts']}
        for action_id in narrative['depends_on']['actions']:
            if action_id in registry['actions']:
                found |= set(pages.action_targets(registry, registry['actions'][action_id]))
    for action in registry['actions'].values():
        shown = pages.action_targets(registry, action)
        if target in shown:
            found |= set(shown)
    found.discard(target)
    return sorted(found)


def entities(registry, target):
    """The registry entities one page shows, with the revisions a proposal must name."""
    entry = registry['pages'][target]
    return {
        'entry': {'target': target, 'page_id': entry['page_id'], 'layout': entry['layout'],
                  'title': entry['title'], 'summary': entry['summary'], 'priority': entry['priority'],
                  'revision': entry['revision']},
        'facts': [{'fact_key': key, 'text': fact['text'], 'status': fact['status'], 'role': fact.get('role'),
                   'revision': fact['revision'], 'owner': fact['owner'], 'protected': fact['protected'],
                   'mandatory': fact['mandatory']}
                  for key, fact in sorted(registry['facts'].items()) if fact['target'] == target],
        'narratives': [{'narrative_id': key, 'text': narrative['text'], 'state': narrative['state'],
                        'revision': narrative['revision'],
                        'depends_on': {kind: sorted(narrative['depends_on'][kind]) for kind in ('facts', 'actions')}}
                       for key, narrative in sorted(registry['narratives'].items())
                       if narrative['page_id'] == entry['page_id']],
        # Read-only: StateAPI owns every action and the panels rendered from it.
        'actions': [{'action_id': key, 'kind': action['kind'], 'state': action['state'],
                     'subject_id': action['subject_id'], 'revision': action['revision']}
                    for key, action in sorted(registry['actions'].items())
                    if target in pages.action_targets(registry, action)],
    }


def _mandatory(reader, generation):
    """The canonical mandatory set (pages.mandatory_paths) for editor and context use: person pages and
    every page holding a protected or mandatory fact, from any project. Delivered whole and read-only,
    never trimmed by the budget; delivery grants no edit scope."""
    return list(reader.mandatory(generation))


def editor_view(reader, generation, project, selected, budget_tokens, *, source_text=None, evidence=None,
                expansions=(), reconciliations=(), sources=None, candidates=()):
    """Everything one editor job may read from `generation`, within `budget_tokens` estimated tokens.

    `reconciliations` are the fact_checks search receipts already issued for this job; like the
    source, they are sent whole or the call refuses. Each names its matching facts by key, and
    `candidates` carries the whole of each one once for the payload, however many searches returned it.
    `readable` names mandatory dependency pages delivered with their entities: visible for dependencies,
    never editable scope.

    `fixed_tokens` is what the payload carries before any page and before the receipts: the source, every
    mandatory rule and the project index; `used_tokens` less `receipt_tokens` adds the pages sent. The
    consult loops read them to bound the receipts of the next reconcile round.

    `sources` (batched Dreaming apply) replaces the single source
    with a list of `{'operation_id', 'text', 'evidence'}` (optionally `hint`) in capture order: the
    payload then names `sources` instead of `source` and the bundle carries every reference. Without
    it the view is the one-source view, byte for byte.
    """
    registry = reader.registry(generation)
    if not isinstance(project, str) or not pages.PROJECT.fullmatch(project):
        raise KelmoError('E_INPUT', 'Name one canonical knowledge project.')
    if type(budget_tokens) is not int or budget_tokens < 1:
        raise KelmoError('E_INPUT', 'The editor needs a positive token budget.')
    selected = list(dict.fromkeys(selected))
    unknown = [target for target in [*selected, *expansions] if target not in registry['pages']]
    if not selected or unknown:
        raise KelmoError('E_INPUT', 'Select registered pages only; unknown: ' + ', '.join(unknown[:5]))
    batch = None if sources is None else list(sources)
    if batch is None:
        source = {'evidence': evidence.to_dict(), 'text': source_text}
        blocks, references, cost = [source], (evidence,), _cost(source)
    else:
        if not batch or not all(isinstance(item, dict) and {'operation_id', 'text', 'evidence'} <= item.keys()
                               and not item.keys() - {'operation_id', 'text', 'evidence', 'hint'}
                               and isinstance(item['operation_id'], str) and isinstance(item['text'], str)
                               for item in batch):
            raise KelmoError('E_INPUT', 'A batch view names each source with its operation id, text and evidence.')
        blocks = [{**{'operation_id': item['operation_id'], 'evidence': item['evidence'].to_dict(),
                      'text': item['text']}, **({'hint': dict(item['hint'])} if item.get('hint') else {})}
                  for item in batch]
        references = tuple(item['evidence'] for item in batch)
        cost = _cost(blocks)
    mandatory = _mandatory(reader, generation)
    # A mandatory page that a selected or expanded page depends on carries its entities and revisions,
    # so its facts and actions can be named in depends_on; it stays read-only unless it is itself selected.
    needed = {other for target in [*selected, *expansions]
              for other in dependencies(registry, target)} - set(selected)
    rules, readable = [], []
    for page in mandatory:
        rule = {'target': page.path, 'content': page.content.decode('utf-8')}
        if page.path in needed and page.path in registry['pages']:
            rule.update(entities(registry, page.path), readable=True)
            readable.append(page.path)
        rules.append(rule)
    index = [{'target': target, 'title': entry['title'], 'kind': entry['kind'], 'layout': entry['layout'],
              'summary': entry['summary']}
             for target, entry in sorted(registry['pages'].items()) if pages.project_of(target) == project]
    reconciliations = list(reconciliations)
    candidates = list(candidates)
    charged = receipt_cost(reconciliations, candidates)
    fixed = (cost
             + sum(_cost(rule) if rule.get('readable') else estimate(rule['target'] + '\n' + rule['content'])
                   for rule in rules))
    used = fixed + charged
    if used > budget_tokens:
        raise KelmoError('E_BUDGET', 'The editor budget cannot hold the archived source, its reconciliation '
                                    'receipts and every mandatory person rule; nothing was sent and nothing is '
                                    'labelled complete.')
    index_cost = _cost(index)
    if used + index_cost > budget_tokens:
        raise KelmoError('E_BUDGET', 'The editor budget cannot hold the page index of project ' + project + '.')
    used += index_cost
    fixed += index_cost
    required = {rule['target'] for rule in rules}
    sent, snapshots, omitted, unmet = [], [], [], []
    for target in dict.fromkeys([*selected, *expansions]):
        page = reader.page(target, generation)
        block = {'target': target, 'selected': target in selected,
                 'unmanaged_text': pages.unmanaged_text(page.content.decode('utf-8')), **entities(registry, target)}
        cost = _cost(block)
        if used + cost > budget_tokens:
            omitted.append(target)
            # What the page needed and what the budget still left after the fixed parts of the payload
            # and the pages already sent in this same call: without these numbers a refusal cannot be read
            # from outside the vault.
            unmet.append({'target': target, 'needs_tokens': cost, 'left_tokens': budget_tokens - used})
            continue
        sent.append(block)
        snapshots.append(page)
        used += cost
    scope = [block['target'] for block in sent]
    freshness = 'current' if reader.generation() == generation else 'stale'
    complete = bool(sent) and not omitted and freshness == 'current'
    bundle = ContextBundle(generation, tuple(rule['content'] for rule in rules), tuple(snapshots),
                           tuple(omitted), freshness, references, complete)
    payload = {'project': project, 'source': blocks[0] if batch is None else blocks,
               'reconciliations': reconciliations,
               'mandatory': rules, 'index': index, 'pages': sent,
               'omitted': omitted, 'complete': complete,
               'expandable': [row['target'] for row in index if row['target'] not in scope
                              and row['target'] not in required],
               'budget': {'max_tokens': budget_tokens, 'used_tokens': used,
                          'receipt_tokens': charged,
                          'metric': 'estimated tokens: UTF-8 bytes / 2.5'}}
    if candidates:
        # The whole text of every candidate the receipts name, once for the whole payload: a fact a
        # second search returned adds no bytes, only its key to that search's own entry.
        payload['candidates'] = candidates
    if batch is not None:
        payload['sources'] = payload.pop('source')
    return {'bundle': bundle, 'payload': payload, 'scope': scope, 'omitted': omitted,
            'omitted_budget': unmet, 'used_tokens': used, 'readable': readable,
            'fixed_tokens': fixed, 'receipt_tokens': charged}
