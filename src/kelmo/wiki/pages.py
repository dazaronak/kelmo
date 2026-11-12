"""The page model: explicit page entries in the registry and derived work status.

Each registered page has one entry (kind, category, parent, title, summary, priority), and each
fact may carry one optional role.

Status is never stored for a page. A work page is in progress or backlog exactly while one of its
facts is, so a page closes itself when its last open fact ships and nothing can make a page and its
facts disagree. Hubs, topics and prose pages never carry open facts.

A fact's revision is computed here, in one place, and covers every stored field that changes how the
page renders, `role` included. Giving a fact a role therefore changes its revision, and a note
captured against the old revision conflicts instead of silently dropping the role.
"""
from __future__ import annotations

import copy
import base64
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import weakref

from kelmo.contracts import ContextBinding, ContextBudget, EvidenceRef, Generation, PageBatch, PageSnapshot
from kelmo.errors import KelmoError

KINDS = ('work', 'topic', 'hub', 'prose')
CATEGORIES = ('projects', 'areas', 'topics', 'people', 'you')
PRIORITIES = ('H1', 'H2', 'H3', '')
ROLES = ('unresolved', 'constraint', 'state', 'history')
ENTRY_FIELDS = ('kind', 'category', 'parent', 'title', 'summary', 'priority')
SUMMARY_MAX = 200
TITLE_MAX = 120
OPEN = ('in-progress', 'backlog')
MAX_PARENT_DEPTH = 3
REVISION_FIELDS = ('fact_key', 'target', 'text', 'status')
INDEX_ROW_LIMIT = 200
PROJECT = re.compile(r'[a-z0-9_-]{1,60}')
GENERATION_CACHE_LIMIT = 96  # answers about adoption per commit (booleans)

# Generation snapshots, one per committed generation for the whole process, shared by every KnowledgeReader. A
# snapshot holds the parsed registry (hundreds of MiB on a large vault) and the pages read from it, so a daemon
# that lives for days keeps the current generation and the one before it (a reader still finishing on it), not a
# copy per reader per generation. A snapshot is read-only once built; `KnowledgeReader.registry` hands out copies.
SNAPSHOT_CACHE_LIMIT = 2
_SNAPSHOTS = OrderedDict()  # (vault id, repository scope, manifest path, commit) -> snapshot, kept
_IN_USE = weakref.WeakValueDictionary()  # the same keys -> every snapshot someone still holds, kept or not
_SNAPSHOTS_LOCK = threading.Lock()
# One snapshot is built at a time: a build parses a whole registry, and readers that ask for the same generation
# at once must get the one copy, not one each.
_SNAPSHOT_BUILD = threading.Lock()


class _Snapshot(dict):
    """A generation snapshot; a dict subclass only so that the in-use index can refer to it weakly."""


def cached_snapshots():
    """How many generation snapshots this process keeps (those still in use beyond them are not counted)."""
    with _SNAPSHOTS_LOCK:
        return len(_SNAPSHOTS)


def _cached_snapshot(key):
    with _SNAPSHOTS_LOCK:
        snapshot = _SNAPSHOTS.get(key)
        if snapshot is None:
            snapshot = _IN_USE.get(key)
            if snapshot is None:
                return None
            _SNAPSHOTS[key] = snapshot
            _evict()
        _SNAPSHOTS.move_to_end(key)
        return snapshot


def _evict():
    while len(_SNAPSHOTS) > SNAPSHOT_CACHE_LIMIT:
        _SNAPSHOTS.popitem(last=False)


def drop_cached_snapshots():
    """Forgets the kept snapshots (a core over its memory budget); one still in use stays shared while it is."""
    with _SNAPSHOTS_LOCK:
        _SNAPSHOTS.clear()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def fact_revision(fact):
    """The revision of one stored fact: the four fields every version carries, plus its role when it has one."""
    body = {name: fact[name] for name in REVISION_FIELDS}
    if fact.get('role') is not None:
        body['role'] = fact['role']
    return digest(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + '\n')


def entry_revision(entry):
    body = json.dumps({name: entry.get(name, '') for name in ENTRY_FIELDS}, ensure_ascii=False, sort_keys=True)
    return digest(body)


# Projects kept at the wiki top level (`<project>/<page>.md`), the shape engine.valid_wiki_target admits
# beside `projects/<project>/`; their pages belong to the project like any other.
TOP_LEVEL_PROJECTS = ('life',)


def project_of(target):
    parts = Path(target).parts
    if len(parts) >= 3 and parts[0] == 'projects':
        return parts[1]
    return parts[0] if len(parts) == 2 and parts[0] in TOP_LEVEL_PROJECTS else None


def project_directory(project):
    """The wiki directory that holds a project's pages, without the trailing slash."""
    return project if project in TOP_LEVEL_PROJECTS else 'projects/' + project


def project_entry(registry, project):
    """The page a project's new work hangs from: its project map, else its own index page
    (`<directory>/<project>.md`), else None."""
    maps = sorted(name for name, page in registry['pages'].items()
                  if project_of(name) == project and page.get('layout') == 'project-map')
    if maps:
        return maps[0]
    index = project_directory(project) + '/' + project + '.md'
    entry = registry['pages'].get(index)
    return index if entry is not None and entry['kind'] != 'prose' else None


def page_status(target, facts):
    """Derived from the page's facts and never stored: the first open status wins, retired only when nothing else is left."""
    statuses = {fact['status'] for fact in facts.values() if fact['target'] == target}
    for status in ('in-progress', 'backlog', 'active', 'shipped', 'retired'):
        if status in statuses:
            return status
    return 'shipped'


def hot_section(target, entry, facts):
    """WIP, Backlog or None: only a work page with open facts is a HOT item."""
    if entry['kind'] != 'work':
        return None
    return {'in-progress': 'WIP', 'backlog': 'Backlog'}.get(page_status(target, facts))


def _invalid(target, problem, repair):
    from .engine import WikiProblem
    return WikiProblem('page ' + str(target)[:120] + ': ' + problem + '; permitted repair: ' + repair,
                       target=str(target) if isinstance(target, str) else None, repair=repair)


def validate_entry(target, entry):
    from .engine import valid_wiki_target
    if not valid_wiki_target(target):
        raise _invalid(target, 'the page target is not a relative Markdown page',
                       'name the page as projects/<project>/<page>.md or <page>.md under the wiki')
    if not isinstance(entry, dict) or not set(ENTRY_FIELDS) <= entry.keys():
        raise _invalid(target, 'the entry needs ' + ', '.join(ENTRY_FIELDS),
                       'supply the required page fields; compatibility metadata is retained')
    if entry['kind'] not in KINDS:
        raise _invalid(target, 'kind must be one of ' + ', '.join(KINDS), 'choose one of those kinds')
    if entry['category'] not in CATEGORIES:
        raise _invalid(target, 'category must be one of ' + ', '.join(CATEGORIES), 'choose one of those categories')
    if entry['priority'] not in PRIORITIES:
        raise _invalid(target, 'priority must be H1, H2, H3 or empty', 'choose H1, H2, H3 or the empty string')
    for name, limit in (('title', TITLE_MAX), ('summary', SUMMARY_MAX)):
        value = entry[name]
        if not isinstance(value, str) or len(value) > limit or any(char in value for char in '\n\r') or '<!--' in value:
            raise _invalid(target, name + ' must be one plain line of at most ' + str(limit) + ' characters',
                           'shorten ' + name + ' to a single line without markup')
    if not entry['title'].strip():
        raise _invalid(target, 'title is required', 'give the page a title')
    if entry['kind'] == 'work' and not entry['summary'].strip():
        raise _invalid(target, 'a work page needs a summary', 'write the one line a reader needs to place this work')
    parent = entry['parent']
    if not isinstance(parent, str) or (parent and not valid_wiki_target(parent)):
        raise _invalid(target, 'parent must be a wiki target or empty', 'name the parent page or leave it empty')
    if parent == target:
        raise _invalid(target, 'a page cannot be its own parent', 'name another page or none')
    if entry.get('revision') != entry_revision(entry):
        raise _invalid(target, 'entry revision mismatch', 'submit the entry without a revision and let it be computed')
    if 'summary_input_hash' in entry and (
        not isinstance(entry['summary_input_hash'], str)
        or not re.fullmatch(r'[0-9a-f]{64}', entry['summary_input_hash'])
    ):
        raise _invalid(target, 'invalid summary_input_hash', 'supply its original lowercase SHA-256')
    if 'summary_assessed_by' in entry and (
        not isinstance(entry['summary_assessed_by'], str) or not entry['summary_assessed_by'].strip()
    ):
        raise _invalid(target, 'invalid summary_assessed_by', 'supply its nonempty assessor identity')
    return entry


def validate_pages(pages, facts):
    """Registry-level page rules: entries valid, parents exist without cycles, kinds honoured by every fact."""
    if not isinstance(pages, dict):
        raise ValueError('invalid pages map')
    for target, entry in pages.items():
        validate_entry(target, entry)
        if entry['parent'] and entry['parent'] not in pages:
            raise _invalid(target, 'parent ' + str(entry['parent'])[:120] + ' has no page entry',
                           'give the parent a page entry too, or leave parent empty')
    for target in pages:
        seen, current = [], target
        while current:
            if current in seen:
                raise _invalid(target, 'parent cycle through ' + current, 'break the parent chain')
            seen.append(current)
            current = pages[current]['parent']
            if len(seen) > MAX_PARENT_DEPTH + 1:
                raise _invalid(target, 'parent chain deeper than ' + str(MAX_PARENT_DEPTH),
                               'keep the parent chain at most ' + str(MAX_PARENT_DEPTH) + ' pages deep')
    for key, fact in facts.items():
        if fact.get('role') is not None and fact['role'] not in ROLES:
            raise ValueError('fact ' + key + ': role must be one of ' + ', '.join(ROLES)
                             + '; permitted repair: store no role or choose one of those')
        if project_of(fact['target']) is None:
            continue
        entry = pages.get(fact['target'])
        if entry is None:
            raise ValueError('fact ' + key + ': target ' + fact['target']
                             + ' has no page entry; permitted repair: give that page a page entry')
        if entry['kind'] == 'prose':
            raise ValueError('fact ' + key + ': ' + fact['target'] + ' is a prose page and holds no facts'
                             + '; permitted repair: move the fact to a work, topic or hub page')
        if fact['status'] in OPEN and entry['kind'] != 'work':
            raise ValueError('fact ' + key + ': open work belongs on a work page, not the ' + entry['kind']
                             + ' page ' + fact['target'] + '; permitted repair: move it to a work page')
    return pages


def action_targets(registry, action):
    """Registered pages that show one accepted action; derived from the single registry, never stored.

    A page shows an action whose subject is the page itself or a page it links to. A person or task
    subject without its own page appears in the person context; no subject is assigned a guessed page.
    """
    entries = registry.get('pages', {})
    subject = action['subject_id']
    direct = sorted(target for target, entry in entries.items()
                    if entry['page_id'] == subject or subject in entry['links'])
    if direct or action['subject_kind'] not in ('person', 'task'):
        return tuple(direct)
    return tuple(sorted(target for target, entry in entries.items() if entry['layout'] == 'person'))


CONTRACT = 'wiki-file-contract/3.0-proposal.2'
KB_START, KB_END = '<!-- kb -->', '<!-- /kb -->'
NARRATIVE_START, NARRATIVE_END = '<!-- narrative -->', '<!-- /narrative -->'
V3_HEADINGS = {
    'person': ('Mandatory rules', 'Person facts', 'General actions'),
    'project-map': ('Current map', 'Constraints', 'Actions', 'Detail pages'),
    'topic': ('Current scope', 'Feature boxes', 'Legacy evidence'),
    'feature': ('Behavior', 'Action state', 'Related pages'),
    'legacy': ('Facts', 'Actions'),
}
MAP_COLUMNS = ('Feature', 'Repository', 'Action', 'Destination/environment', 'State', 'Owner', 'Evidence')
FEATURE_COLUMNS = ('Action ID', 'Repository', 'Kind', 'Destination/environment', 'State', 'Owner', 'Evidence')
HEADER = re.compile(r'---\r?\n.*?^---[ \t]*(?:\r?\n|\Z)', re.DOTALL | re.MULTILINE)


def leading_header(text):
    """The YAML frontmatter block at the start of a page, exactly as stored, or ''."""
    match = HEADER.match(text)
    return match.group(0) if match else ''


def header_record(text):
    """Compatibility evidence for explicit header adoption: the original bytes and their hash."""
    return {'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'text': text}


def _contract_header(header):
    return any(line.rstrip('\r') == 'wiki_contract: ' + CONTRACT for line in header.split('\n'))


def frontmatter(target, entry):
    """Readable projection of the registry entry; the registry stays the authority."""
    def quoted(value):
        return json.dumps(value, ensure_ascii=False)
    return '\n'.join([
        '---', 'wiki_contract: ' + CONTRACT, 'page_id: ' + entry['page_id'], 'target: ' + target,
        'kind: ' + entry['kind'], 'category: ' + entry['category'],
        'parent: ' + quoted(entry['parent']), 'title: ' + quoted(entry['title']),
        'summary: ' + quoted(entry['summary']), 'priority: ' + quoted(entry['priority']),
        'aliases: ' + quoted(entry['aliases']), 'tags: ' + quoted(entry['tags']), '---']) + '\n'


def _check_markers(target, text):
    """One marker per line, at most one pair per region, ordered, kb before narrative; else refuse."""
    lines = [line.rstrip('\r') for line in text.split('\n')]
    positions = {}
    for marker in (KB_START, KB_END, NARRATIVE_START, NARRATIVE_END):
        if text.count(marker) > 1 or text.count(marker) != lines.count(marker):
            raise _invalid(target, 'a managed marker is repeated or shares its line with other text',
                           'resolve the ambiguous marker by hand before publishing')
        positions[marker] = text.find(marker)
    for start, end in ((KB_START, KB_END), (NARRATIVE_START, NARRATIVE_END)):
        if (positions[start] < 0) != (positions[end] < 0) or positions[end] < positions[start]:
            raise _invalid(target, 'a managed region is unpaired or reversed',
                           'resolve the ambiguous marker by hand before publishing')
    if positions[NARRATIVE_START] >= 0 and positions[KB_END] > positions[NARRATIVE_START]:
        raise _invalid(target, 'the narrative region must follow the kb region',
                       'resolve the ambiguous marker by hand before publishing')


def _link(target, other, registry):
    entry = registry['pages'][other]
    relative = posixpath.relpath(other, posixpath.dirname(target) or '.')
    return '[' + entry['title'].replace('[', '(').replace(']', ')') + '](' + relative + ')'


def _cell(value):
    return '' if value is None else str(value).replace('|', '\\|')


def _fact_row(fact):
    labels = ['[f:' + fact['id'] + ']']
    if fact['evidence_resolution'] == 'exact':
        labels += ['[source:' + ref['source_id'] + ']' for ref in fact['evidence_refs']]
        if fact['status'] != 'active':
            labels.append('[status: ' + fact['status'] + ']')
    else:
        # The frozen file contract's label (file_contract projection check), not a new wording.
        labels.append('[unresolved legacy state; retained status: ' + fact['status'] + ']')
    return '- ' + fact['text'] + ' ' + ' '.join(labels)


def _label(kind):
    return kind[:1].upper() + kind[1:]


def _evidence(action):
    reference = action['accepted_event']['evidence_ref']
    return reference['source_id'] + ' v' + str(reference['version'])


def _destination(action):
    return action.get('remote_ref') if action['kind'] in ('push', 'merge') else action.get('environment')


def _general_sentence(action):
    """The existing general-action sentence; it names no Git or deployment field."""
    event = action['accepted_event']
    return (f"- {_label(action['kind'])} [a:{action['action_id']}]: {action['state']}. "
            f"Subject: {action['subject_id']}; kind: {action['kind']}; owner: {action['owner']}. "
            f"Evidence: {event['evidence_mark']}, {_evidence(action)}; "
            f"owning receipt: {event['operation_receipt_id']}.")


def _table(columns, rows):
    return ['| ' + ' | '.join(columns) + ' |', '| ' + ' | '.join('---' for _ in columns) + ' |',
            *('| ' + ' | '.join(_cell(value) for value in row) + ' |' for row in rows)]


def _map_rows(target, actions, registry, features):
    rows = []
    for action in actions:
        feature = features.get(action['subject_id'])
        rows.append((_link(target, feature, registry) if feature else action['subject_id'],
                     action.get('repository_id'), _label(action['kind']) + ' [a:' + action['action_id'] + ']',
                     _destination(action), action['state'], action['owner'], _evidence(action)))
    return _table(MAP_COLUMNS, rows)


def _feature_panel(entry, actions):
    lines = _table(FEATURE_COLUMNS, [('[a:' + action['action_id'] + ']', action.get('repository_id'),
                                      action['kind'], _destination(action), action['state'], action['owner'],
                                      _evidence(action)) for action in actions])
    lines += ['', f"Subject: feature/{entry['page_id']}. Feature ID: {entry.get('feature_id', entry['page_id'])}."]
    for name, label, kinds in (('work_revision', 'Work revision', ('push', 'merge', 'deploy')),
                               ('artifact_id', 'Deployment artifact', ('deploy',))):
        bound = [action for action in actions if action['kind'] in kinds]
        values = {action.get(name) for action in bound}
        if len(values) == 1 and None not in values:
            # Every displayed action of these kinds shares it: the single shared declaration.
            lines += ['', f'{label}: {values.pop()}.']
        else:
            # One paragraph per action that carries the field, keyed by its action ID; an action
            # without the field (a non-Git deployment) gets no annotation and no borrowed value.
            for action in bound:
                if name in action:
                    lines += ['', f"{label} of {action['action_id']}: {action[name]}."]
    return lines


def _narrative_region(entry, registry):
    items = sorted((key, value) for key, value in registry['narratives'].items()
                   if value['page_id'] == entry['page_id'])
    if not items:
        return None
    parts = ['## Explanation']
    for key, narrative in items:
        parts.append(f"Narrative: {key}. State: {narrative['state']}.")
        if narrative['state'] == 'current':
            parts.append(narrative['text'])
        else:
            # Stale text stays readable as evidence, quoted and labelled, never as current prose.
            parts.append('This explanation is stale: a fact or action it depends on changed after it was '
                         'written. The managed panels above carry the current state.')
            parts.append('\n'.join('> ' + line if line else '>' for line in narrative['text'].split('\n')))
        if narrative['evidence_refs']:
            parts.append('\n'.join(_table(('Source', 'Archive ID', 'Version'),
                                          [(ref['source_id'], ref['archive_id'], ref['version'])
                                           for ref in narrative['evidence_refs']])))
    return NARRATIVE_START + '\n\n' + '\n\n'.join(parts) + '\n\n' + NARRATIVE_END


def _kb_region(target, registry):
    entry = registry['pages'][target]
    layout = entry['layout']
    by_id = {value['page_id']: key for key, value in registry['pages'].items()}
    # A retired exact record may leave an ordinary page, never a person page and never a mandatory
    # or protected record: those pages are delivered whole as mandatory, so their text must be there.
    facts = [fact for _, fact in sorted(registry['facts'].items()) if fact['target'] == target
             and (layout == 'person' or fact.get('mandatory') or fact.get('protected')
                  or fact['status'] != 'retired' or fact['evidence_resolution'] != 'exact')]
    shown = [action for _, action in sorted(registry['actions'].items())
             if target in action_targets(registry, action)]
    features = {value['page_id']: key for key, value in registry['pages'].items() if value['layout'] == 'feature'}
    panel = [action for action in shown if action['subject_kind'] == 'feature' and action['subject_id'] == entry['page_id']]
    mapped = [action for action in shown if action['subject_kind'] == 'feature' and action not in panel]
    general = [_general_sentence(action) for action in shown if action['subject_kind'] != 'feature']
    # Every layout shows exactly its registered links; navigation adoption derived (hub, index,
    # parent, child and backlink relationships) is recorded in `links`, not added here.
    links = sorted({by_id[page_id] for page_id in entry['links'] if page_id in by_id})
    navigation = ['- ' + _link(target, other, registry) for other in links]
    sections = {name: [] for name in V3_HEADINGS[layout]}

    def joined(*groups):
        result = []
        for group in groups:
            result += ([''] if result and group else []) + list(group)
        return result

    def table(actions):
        return _map_rows(target, actions, registry, features) if actions else []

    if layout == 'person':
        sections['Mandatory rules'] = [_fact_row(f) for f in facts if f['protected']]
        sections['Person facts'] = joined([_fact_row(f) for f in facts if not f['protected']], navigation)
        sections['General actions'] = joined(general, table(panel + mapped))
    elif layout == 'project-map':
        repositories = (['Repository bindings: ' + ', '.join(entry['repositories']) + '.']
                        if entry['repositories'] else [])
        sections['Current map'] = joined([_fact_row(f) for f in facts if f.get('role') != 'constraint'],
                                         repositories)
        sections['Constraints'] = [_fact_row(f) for f in facts if f.get('role') == 'constraint']
        sections['Actions'] = joined(table(panel + mapped), general)
        sections['Detail pages'] = navigation
    elif layout == 'topic':
        sections['Current scope'] = joined([_fact_row(f) for f in facts if f['evidence_resolution'] == 'exact'],
                                           general)
        boxes = ['- ' + _link(target, other, registry)
                 + (': ' + registry['pages'][other]['summary'] if registry['pages'][other]['summary'] else '')
                 for other in links]
        sections['Feature boxes'] = joined(boxes, table(panel + mapped))
        sections['Legacy evidence'] = [_fact_row(f) for f in facts if f['evidence_resolution'] != 'exact']
    elif layout == 'feature':
        sections['Behavior'] = [_fact_row(f) for f in facts]
        sections['Action state'] = joined(_feature_panel(entry, panel) if panel else [], table(mapped), general)
        sections['Related pages'] = navigation
    else:
        sections['Facts'] = joined([_fact_row(f) for f in facts], navigation)
        sections['Actions'] = joined(table(panel + mapped), general)
    parts = ['## ' + name + ('\n\n' + '\n'.join(lines) if lines else '') for name, lines in sections.items()]
    return KB_START + '\n\n' + '\n\n'.join(parts) + '\n\n' + KB_END


def _header_title(header):
    for line in header.split('\n'):
        if line.startswith('title: '):
            try:
                value = json.loads(line[len('title: '):].rstrip('\r'))
            except ValueError:
                return None
            return value if isinstance(value, str) else None
    return None


def owned_heading(text):
    """The title of a publisher-generated H1, or None when the page's H1 is hand-written.

    The publisher generates a page as contract frontmatter, its title as the H1 and the kb region
    directly after it. Only that exact shape, with the H1 equal to the frontmatter title, is
    publisher-owned; any other H1 belongs to its author and is never rewritten.
    """
    header = leading_header(text)
    title = _header_title(header) if header and _contract_header(header) else None
    if title is None or not text[len(header):].startswith('\n# ' + title + '\n\n' + KB_START):
        return None
    return title


def unmanaged_text(text):
    """A page's own text: everything but its frontmatter, a publisher-generated H1 and the kb and narrative
    regions, which the publisher renders from registry entities. A region with ambiguous markers stays in."""
    body = text[len(leading_header(text)):]
    title = owned_heading(text)
    if title is not None:
        body = body[len('\n# ' + title + '\n'):]
    for start, end in ((KB_START, KB_END), (NARRATIVE_START, NARRATIVE_END)):
        if body.count(start) == body.count(end) == 1 and body.index(start) < body.index(end):
            before, rest = body.split(start, 1)
            body = before + rest.split(end, 1)[1]
    return body.strip()


def render_v3_page(target, existing, registry):
    """One page: managed frontmatter, kb and narrative regions; every other byte is kept.

    A new page is generated whole. An existing page keeps its unmanaged bytes. Its header is
    replaced only when it is already a contract header, or when explicit adoption recorded these
    exact original header bytes (an empty record authorizes inserting one). Ambiguous markers or
    a header changed since its adoption refuse instead of guessing.
    """
    entry = registry['pages'][target]
    header = frontmatter(target, entry)
    kb = _kb_region(target, registry)
    narrative = _narrative_region(entry, registry)
    if existing is None:
        text = header + '\n# ' + entry['title'] + '\n\n' + kb + '\n'
        return text + ('\n' + narrative + '\n' if narrative else '')
    _check_markers(target, existing)
    current = leading_header(existing)
    adopted = entry['compatibility'].get('header')
    owned = owned_heading(existing)
    if owned is not None and owned != entry['title']:
        # A publisher-generated H1 follows its title; a hand-written H1 is unmanaged bytes.
        existing = current + '\n# ' + entry['title'] + existing[len(current) + len('\n# ' + owned):]
    if current and _contract_header(current):
        existing = header + existing[len(current):]
    elif adopted is not None:
        if adopted.get('sha256') != header_record(current)['sha256']:
            raise _invalid(target, 'the adopted legacy header changed after its adoption',
                           'adopt the current header explicitly again')
        existing = header + existing[len(current):]
    if NARRATIVE_START in existing and narrative is None:
        held = existing.split(NARRATIVE_START, 1)[1].split(NARRATIVE_END, 1)[0]
        if held.strip():
            # The registry owns no narrative for this page, so these bytes have no publisher owner.
            raise _invalid(target, 'the narrative region holds text no registered narrative owns',
                           'move that text outside the narrative markers, or register it as a narrative')
    if KB_START in existing:
        before, rest = existing.split(KB_START, 1)
        existing = before + kb + rest.split(KB_END, 1)[1]
    elif NARRATIVE_START in existing:
        # kb must precede the narrative, or the next render refuses the order this one wrote.
        before, rest = existing.split(NARRATIVE_START, 1)
        separator = '' if not before or before.endswith('\n\n') else '\n' if before.endswith('\n') else '\n\n'
        existing = before + separator + kb + '\n\n' + NARRATIVE_START + rest
    else:
        separator = '' if existing.endswith('\n\n') else '\n' if existing.endswith('\n') else '\n\n'
        existing = existing + separator + kb + '\n'
    if NARRATIVE_START in existing:
        before, rest = existing.split(NARRATIVE_START, 1)
        region = narrative or NARRATIVE_START + '\n' + NARRATIVE_END
        existing = before + region + rest.split(NARRATIVE_END, 1)[1]
    elif narrative:
        position = existing.index(KB_END) + len(KB_END)
        existing = existing[:position] + '\n\n' + narrative + existing[position:]
    return existing


def page_index(registry, projects=None):
    """The short discovery view of the registry: one row per page, work pages carrying what places them.

    A caller that has to fit a page list inside a bounded prompt reads this and asks for the facts of the
    pages it selected. `projects=None` covers every project the registry names; a project with no page is
    present with an empty list, so an empty project is an answer and not a missing key. Rows are sorted by
    target, so two calls on the same registry read the same rows in the same order.
    """
    if projects is not None and any(not isinstance(name, str) or not PROJECT.fullmatch(name) for name in projects):
        raise ValueError('a project name must be 1 to 60 characters of a-z 0-9 _ -; permitted repair: '
                         'name the project as it appears in projects/<project>/')
    wanted = sorted(set(projects)) if projects is not None else sorted(
        {project for fact in registry['facts'].values() if (project := project_of(fact['target']))}
        | {project for target in registry['pages'] if (project := project_of(target))})
    result = {project: [] for project in wanted}
    for target, entry in sorted(registry['pages'].items()):
        project = project_of(target)
        if project not in result:
            continue
        row = {'target': target, 'kind': entry['kind'], 'title': entry['title'],
               'status': page_status(target, registry['facts'])}
        if entry['kind'] == 'work':
            row.update({name: entry[name] for name in ('summary', 'priority', 'parent')})
        result[project].append(row)
    return result


def page_facts(registry, target):
    """Exact read of one page: its entry, its derived status and every fact sitting on it, revisions included.

    Discovery is bounded, this is not: a caller that selected a page gets its complete current facts, which is
    what makes a fact omitted from the index different from a fact the store does not hold. A page with no
    entry is still answered, because a page outside a project may hold facts and no entry, and the caller
    asked about this page.
    """
    from .engine import valid_wiki_target
    if not valid_wiki_target(target):
        raise _invalid(target, 'the page target is not a relative Markdown page',
                       'name the page as projects/<project>/<page>.md or <page>.md under the wiki')
    entry = registry['pages'].get(target)
    facts = [{'fact_key': fact['fact_key'], 'text': fact['text'], 'status': fact['status'],
              'id': fact['id'], 'revision': fact['revision'], 'role': fact.get('role'),
              'note_id': fact['note_id']}
             for key, fact in sorted(registry['facts'].items()) if fact['target'] == target]
    return {'target': target, 'entry': entry, 'status': page_status(target, registry['facts']),
            'facts': facts, 'hot': hot_section(target, entry, registry['facts']) if entry else None}


def generation_paths(config):
    """Knowledge paths relative to their existing Git root; no directories are created."""
    root = Path(getattr(config, 'knowledge_git_root', config.root))
    registry = Path(config.registry)
    wiki = Path(config.wiki)
    for path in (root, registry, wiki):
        if (not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents))
                or path != path.resolve()):
            raise KelmoError('E_CONFIG', 'Knowledge bindings require real canonical absolute paths.')
    if not registry.is_relative_to(root) or not wiki.is_relative_to(root) or wiki == root:
        raise KelmoError('E_CAPABILITY', 'Generation reads require registry and wiki inside the bound knowledge Git root.')
    if registry.is_relative_to(wiki):
        raise KelmoError('E_CONFIG', 'The registry must be separate from the wiki subtree.')
    if registry.suffix != '.json' or registry.name == 'generation.json':
        raise KelmoError('E_CONFIG', 'Registry and generation require distinct JSON file bindings.')
    return (root, registry.relative_to(root).as_posix(), wiki.relative_to(root).as_posix(),
            registry.with_name('generation.json').relative_to(root).as_posix())


def committed_paths(config):
    """Knowledge Git root, registry and manifest paths; unlike generation_paths, no wiki binding.

    Whether a committed registry exists is decided at HEAD, so it stays decidable for a store whose
    wiki does not read as a generation.
    """
    root = Path(getattr(config, 'knowledge_git_root', config.root))
    registry = Path(config.registry)
    for path in (root, registry):
        if (not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents))
                or path != path.resolve()):
            raise KelmoError('E_CONFIG', 'Knowledge bindings require real canonical absolute paths.')
    if not registry.is_relative_to(root):
        raise KelmoError('E_CAPABILITY', 'The registry is outside the bound knowledge Git root.')
    return (root, registry.relative_to(root).as_posix(),
            registry.with_name('generation.json').relative_to(root).as_posix())


def mandatory_paths(registry, paths):
    """Pages delivered whole in every startup context, selected in one place.

    Person pages (person.md, category `you`, layout `person`) and every page holding a canonical
    mandatory record (a fact marked mandatory or protected). Only pages present in this tree.
    """
    selected = {path for path, entry in registry.get('pages', {}).items()
                if entry.get('category') == 'you' or entry.get('layout') == 'person'}
    selected |= {fact['target'] for fact in registry['facts'].values()
                 if fact.get('mandatory') is True or fact.get('protected') is True}
    selected.add('person.md')
    return tuple(sorted(selected & set(paths)))


def _read_git(root, *args):
    """Read local Git objects without inherited repository overrides or transports."""
    if args[0] not in {'rev-parse', 'ls-tree', 'cat-file', 'log'}:
        raise ValueError('Only knowledge object inspection is permitted.')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment['GIT_OPTIONAL_LOCKS'] = '0'
    try:
        result = subprocess.run(
            ['git', '-c', 'protocol.allow=never', '-c', 'core.fsmonitor=false',
             '-C', str(root), *args], env=environment, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise KelmoError('E_GIT', 'Knowledge Git objects could not be inspected.') from error
    if result.returncode:
        raise KelmoError('E_GIT', 'Knowledge Git object inspection failed: ' + args[0])
    return result.stdout


def _knowledge_tree(config, tree):
    if not isinstance(tree, str) or not re.fullmatch(r'HEAD|[0-9a-f]{40}|[0-9a-f]{64}', tree):
        raise KelmoError('E_GENERATION', 'Knowledge inspection requires HEAD or an immutable Git object ID.')
    root, registry, wiki, generation = generation_paths(config)
    prefixes = (registry, generation, str(PurePosixPath(registry).parent / 'hot') + '/',
                str(PurePosixPath(wiki).parent / 'hot') + '/', wiki + '/')
    return _tree_entries(root, tree, prefixes)


def _tree_entries(root, tree, prefixes):
    if Path(os.fsdecode(_read_git(root, 'rev-parse', '--show-toplevel')).strip()) != root:
        raise KelmoError('E_CONFIG', 'Knowledge Git root does not match its explicit binding.')
    raw = _read_git(root, 'ls-tree', '-r', '--full-tree', '-z', tree, '--', *prefixes)
    entries = {}
    for row in raw.split(b'\0'):
        if not row:
            continue
        metadata, path = row.split(b'\t', 1)
        mode, kind, object_id = metadata.split()
        try:
            name = path.decode('utf-8')
        except UnicodeDecodeError as error:
            raise KelmoError('E_GENERATION', 'The knowledge tree contains a path that is not UTF-8.') from error
        if (PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or any(ord(char) < 32 for char in name)):
            raise KelmoError('E_GENERATION', 'The knowledge tree contains an unsafe path.')
        entries[name] = (mode, kind, object_id)
    return entries


def _tree_digest(config, entries):
    _, _, wiki, generation = generation_paths(config)
    excluded = {generation, *(wiki + '/' + name for name in ('entities.yml', '_template.md', 'log.md'))}
    rows = sorted((path.encode('utf-8'), mode, object_id)
                  for path, (mode, _kind, object_id) in entries.items() if path not in excluded)
    return hashlib.sha256(b''.join(path + b'\0' + mode + b'\0' + object_id + b'\n'
                                   for path, mode, object_id in rows)).hexdigest()


def managed_digest(config, tree):
    """Hash managed Git tree rows, excluding the manifest and hand-maintained pages."""
    return _tree_digest(config, _knowledge_tree(config, tree))


class KnowledgeReader:
    """Read adoption/1 snapshots from immutable generations in the existing repository.

    No checkout, cache directory, archive connection or publisher is created.
    Cursors are checksummed navigation positions, never authorization tokens.
    """

    def __init__(self, config, *, resolve_binding=None):
        self.config = config
        try:
            self.root, self.registry_path, self.wiki_path, self.manifest_path = generation_paths(config)
        except KelmoError as error:
            if error.code != 'E_CAPABILITY':
                raise
            # adopted() stays answerable; every generation read still refuses through _head.
            self.root, self.registry_path, self.manifest_path = committed_paths(config)
            self.wiki_path = None
        # Whether a commit carries a supported registry: a commit never changes, so the answer is kept.
        self._adopted = OrderedDict()
        self._held = threading.local()
        self._resolve_binding = resolve_binding
        self._scope = digest(json.dumps([str(self.root), self.registry_path, self.wiki_path]))

    @contextmanager
    def holding_head(self):
        """One read of the head serves every read inside, on this thread.

        A request that assembles one answer from several reads would otherwise ask Git for the head before each
        of them. It reads the head once here, works on that commit, and after leaving reads the head again
        (`generation()`) to learn whether a publication moved it meanwhile."""
        if getattr(self._held, 'active', False):
            yield
            return
        self._held.active, self._held.commit = True, None  # read by the first `_head()` inside
        try:
            yield
        finally:
            self._held.active, self._held.commit = False, None

    def _head(self):
        held = getattr(self._held, 'commit', None)
        if held is not None:
            return held
        commit = self._read_head()
        if getattr(self._held, 'active', False):
            self._held.commit = commit
        return commit

    def _read_head(self):
        # Recheck bindings even when every object requested is already cached.
        generation_paths(self.config)
        return self._top_and_head()

    def _top_and_head(self):
        """The commit HEAD names, after checking that Git's top level is still the bound root.
        One process answers both questions: the repository's top level, then the commit."""
        answer = os.fsdecode(_read_git(self.root, 'rev-parse', '--show-toplevel', 'HEAD^{commit}')).rstrip('\n')
        top, _, commit = answer.rpartition('\n')
        if Path(top.strip()) != self.root:
            raise KelmoError('E_CONFIG', 'Knowledge Git root changed from its bound repository.')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
            raise KelmoError('E_GIT', 'Knowledge Git object inspection failed: rev-parse')
        return commit

    def _blob(self, entries, path):
        entry = entries.get(path)
        if not entry or entry[0] not in (b'100644', b'100755') or entry[1] != b'blob':
            raise KelmoError('E_GENERATION', 'A required knowledge artifact is missing or is not a regular file: ' + path)
        return _read_git(self.root, 'cat-file', 'blob', entry[2].decode('ascii'))

    def _load(self, commit):
        key = (self.config.vault_id, self._scope, self.manifest_path, commit)
        snapshot = _cached_snapshot(key)
        if snapshot is not None:
            return snapshot
        with _SNAPSHOT_BUILD:
            snapshot = _cached_snapshot(key)
            if snapshot is not None:
                return snapshot
            snapshot = _Snapshot(self._build_snapshot(commit))
            with _SNAPSHOTS_LOCK:
                _SNAPSHOTS[key] = snapshot
                _IN_USE[key] = snapshot
                _evict()
            return snapshot

    def _build_snapshot(self, commit):
        entries = _knowledge_tree(self.config, commit)
        try:
            manifest_bytes = self._blob(entries, self.manifest_path)
            manifest = json.loads(manifest_bytes)
            sequence = manifest['sequence']
            parent = manifest['parent_sequence']
            if (type(manifest['schema']) is not int or manifest['schema'] != 1
                    or type(sequence) is not int or sequence < 1
                    or type(parent) is not int or parent != sequence - 1
                    or manifest['managed_digest'] != _tree_digest(self.config, entries)):
                raise ValueError('Invalid generation sequence or managed digest.')
            from .engine import registry_view
            registry = registry_view(self._blob(entries, self.registry_path))
            if 'counts' in manifest and manifest['counts'] != {
                'facts': len(registry['facts']), 'pages': len(registry.get('pages', {})),
                'notes': len(registry['notes']),
            }:
                raise ValueError('Generation counts differ from its pinned registry.')
            generation = Generation(self.config.vault_id, sequence, hashlib.sha256(manifest_bytes).hexdigest())
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise KelmoError('E_GENERATION', 'Knowledge generation, registry or tree digest is invalid.') from error
        prefix = self.wiki_path + '/'
        paths = []
        for path in entries:
            if not path.startswith(prefix) or not path.endswith('.md'):
                continue
            name = path[len(prefix):]
            if name in ('_template.md', 'log.md'):
                continue
            relative = PurePosixPath(name)
            if relative.as_posix() != name or any(part.startswith('.') for part in relative.parts):
                raise KelmoError('E_GENERATION', 'The generation contains a noncanonical page path.')
            paths.append(name)
        registered = set(registry.get('pages', {})) | {fact['target'] for fact in registry['facts'].values()}
        registered |= {fact['index_target'] for fact in registry['facts'].values() if 'index_target' in fact}
        if registered - set(paths):
            raise KelmoError('E_GENERATION', 'A registered page is absent from the pinned knowledge tree.')
        return {'commit': commit, 'generation': generation, 'registry': registry,
                'entries': entries, 'paths': tuple(sorted(paths)), 'pages': {}}

    def generation(self):
        return self._load(self._head())['generation']

    def adopted(self):
        """Whether a supported registry is committed at HEAD, never read from a working-tree edit.

        Only the registry and manifest at HEAD are read, so this does not depend on the wiki being
        readable as a generation. A committed registry stays present even when its generation later
        fails; callers then refuse instead of reading working pages.
        """
        root, registry_path, manifest_path = committed_paths(self.config)
        if (root, registry_path, manifest_path) != (self.root, self.registry_path, self.manifest_path):
            raise KelmoError('E_CONFIG', 'Knowledge bindings changed since this reader was created.')
        head = getattr(self._held, 'commit', None)
        if head is None:
            # Inside a hold this read is the hold's one read of the head, so the answer and every later read
            # are about the same commit. Outside it the top level is still checked on every call: a kept
            # answer for a commit must not vouch for another repository that holds the same commit.
            head = self._head() if getattr(self._held, 'active', False) else self._top_and_head()
        if head in self._adopted:
            self._adopted.move_to_end(head)
            return self._adopted[head]
        entries = _tree_entries(root, head, (registry_path, manifest_path))
        if self.registry_path not in entries and self.manifest_path not in entries:
            return self._remember_adopted(head, False)
        try:
            registry = json.loads(self._blob(entries, self.registry_path))
            version = registry['version']
            if type(version) is not int or version != 3:
                raise ValueError('Unsupported committed registry format.')
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise KelmoError('E_GENERATION', 'The committed registry is unreadable or has an unsupported format; '
                             'this release reads version 3 only.') from error
        return self._remember_adopted(head, True)

    def _remember_adopted(self, commit, answer):
        self._adopted[commit] = answer
        while len(self._adopted) > GENERATION_CACHE_LIMIT:
            self._adopted.popitem(last=False)
        return answer

    def registry(self, generation):
        """The exact registry one reachable generation pins; a copy, never a write handle."""
        return copy.deepcopy(self._pinned(generation)['registry'])

    def registry_view(self, generation):
        """The registry one generation pins, shared by the whole process and never to be changed; for lookups
        and comparisons, where `registry` would copy hundreds of MiB."""
        return self._pinned(generation)['registry']

    def registry_fingerprint(self, generation):
        """digest(encoded(registry)) of a generation's registry, computed once per process."""
        from .engine import fingerprint
        return fingerprint(self._pinned(generation)['registry'])

    def _pinned(self, generation):
        if not isinstance(generation, Generation) or generation.authority_id != self.config.vault_id:
            raise KelmoError('E_GENERATION', 'Knowledge generation belongs to a different authority.')
        head = self._head()
        current = self._load(head)
        if current['generation'] == generation:
            return current
        # Only commits reachable from this bound checkout can supply historical
        # generations. A caller cannot submit a Git ref or arbitrary object ID.
        commits = _read_git(self.root, 'log', '--format=%H', head, '--', self.manifest_path)
        for commit in commits.decode('ascii').splitlines():
            entries = _knowledge_tree(self.config, commit)
            if self.manifest_path not in entries:
                # A commit that deleted the manifest pins no generation; older history may.
                continue
            raw = self._blob(entries, self.manifest_path)
            if hashlib.sha256(raw).hexdigest() == generation.manifest_sha256:
                selected = self._load(commit)
                if selected['generation'] == generation:
                    return selected
        raise KelmoError('E_GENERATION', 'The requested generation is unavailable in the bound repository.')

    def _page(self, snapshot, path):
        if path not in snapshot['pages']:
            raw = self._blob(snapshot['entries'], self.wiki_path + '/' + path)
            try:
                raw.decode('utf-8')
                page = PageSnapshot(path, raw, hashlib.sha256(raw).hexdigest(), snapshot['generation'])
            except (UnicodeError, ValueError) as error:
                raise KelmoError('E_GENERATION', 'A pinned page is not canonical UTF-8 Markdown.') from error
            snapshot['pages'][path] = page
        return snapshot['pages'][path]

    @staticmethod
    def _project_paths(snapshot, project):
        if not isinstance(project, str) or not PROJECT.fullmatch(project):
            raise KelmoError('E_INPUT', 'Name one canonical knowledge project.')
        if project == 'person':
            return tuple(path for path in snapshot['paths'] if path == 'person.md')
        if project == 'life':
            return tuple(path for path in snapshot['paths'] if path.startswith('life/'))
        return tuple(path for path in snapshot['paths'] if project_of(path) == project)

    def _cursor(self, generation, project, after):
        raw = json.dumps({'version': 1, 'scope': self._scope, 'generation': generation.to_dict(),
                          'project': project, 'after': after}, sort_keys=True, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode('ascii') + '.' + hashlib.sha256(raw).hexdigest()

    def _position(self, cursor, generation, project, paths):
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError
            encoded, checksum = cursor.split('.')
            raw = base64.b64decode(encoded, altchars=b'-_', validate=True)
            value = json.loads(raw)
            if (checksum != hashlib.sha256(raw).hexdigest()
                    or set(value) != {'version', 'scope', 'generation', 'project', 'after'}
                    or type(value['version']) is not int or value['version'] != 1
                    or value['scope'] != self._scope or value['project'] != project
                    or Generation.from_dict(value['generation']) != generation
                    or value['after'] not in paths):
                raise ValueError
            return paths.index(value['after']) + 1
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise KelmoError('E_CURSOR', 'Cursor does not match this generation, project and root binding.') from error

    def pages(self, project, generation, cursor=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise KelmoError('E_INPUT', 'Page batch limit must be between 1 and 1000.')
        snapshot = self._pinned(generation)
        paths = self._project_paths(snapshot, project)
        start = 0 if cursor is None else self._position(cursor, generation, project, paths)
        selected = paths[start:start + limit]
        next_cursor = self._cursor(generation, project, selected[-1]) if start + len(selected) < len(paths) else None
        return PageBatch(tuple(self._page(snapshot, path) for path in selected), len(paths), next_cursor, generation)

    def mandatory(self, generation):
        """Every mandatory page of one generation, whole (mandatory_paths); not a project listing."""
        snapshot = self._pinned(generation)
        return tuple(self._page(snapshot, path) for path in mandatory_paths(snapshot['registry'], snapshot['paths']))

    def page(self, path, generation):
        from .links import LinkIndex
        snapshot = self._pinned(generation)
        if path in snapshot['paths']:
            return self._page(snapshot, path)
        if 'links' not in snapshot:
            try:
                snapshot['links'] = LinkIndex({name: self._page(snapshot, name).content for name in snapshot['paths']})
            except ValueError as error:
                raise KelmoError('E_GENERATION', 'A page alias index could not be built.') from error
        try:
            matches = snapshot['links'].resolve(path)
        except ValueError as error:
            raise KelmoError('E_GENERATION', str(error)) from error
        if not matches:
            raise KelmoError('E_NOT_FOUND', 'No page matches this path or alias in the pinned generation.')
        if len(matches) != 1:
            raise KelmoError('E_AMBIGUOUS', 'Page target is ambiguous; candidates: ' + ', '.join(matches))
        return self._page(snapshot, matches[0])

    def context(self, binding, generation, budget):
        from .context import generation_context
        if not isinstance(binding, ContextBinding):
            raise KelmoError('E_BINDING', 'Context requires an explicit repository binding.')
        if self._resolve_binding is not None:
            binding = self._resolve_binding(binding)
        elif binding.repository_id != self.config.vault_id or binding.worktree_id is not None:
            raise KelmoError('E_BINDING', 'Resolve the repository/worktree enrollment before reading context.')
        if not isinstance(binding, ContextBinding):
            raise KelmoError('E_BINDING', 'The controller could not resolve this repository binding.')
        if not isinstance(budget, ContextBudget):
            raise KelmoError('E_INPUT', 'Context requires an explicit token budget.')
        snapshot = self._pinned(generation)
        paths = self._project_paths(snapshot, binding.project)
        mandatory = mandatory_paths(snapshot['registry'], snapshot['paths'])
        references, mapped = [], 0
        for note_id, note in snapshot['registry']['notes'].items():
            # Receipts carry an explicit list; an unresolved one stays an unmapped source.
            stored = note.get('evidence_refs', [note['evidence_ref']] if 'evidence_ref' in note else [])
            mapped += bool(stored)
            for value in stored:
                try:
                    reference = EvidenceRef.from_dict(value)
                    if reference.archive_id != note['ark_id'] or reference.content_sha256 != note_id:
                        raise ValueError('Source mapping differs from its registry receipt.')
                    references.append(reference)
                except (TypeError, ValueError) as error:
                    raise KelmoError('E_GENERATION', 'A stored exact source mapping is malformed.') from error
        return generation_context(
            tuple(self._page(snapshot, path) for path in paths),
            tuple(self._page(snapshot, path) for path in mandatory),
            generation, budget, tuple(references),
            freshness='current' if self.generation() == generation else 'stale',
            sources_complete=mapped == len(snapshot['registry']['notes']))
