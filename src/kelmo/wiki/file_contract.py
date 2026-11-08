"""Offline checks for one proposed Wiki v3 family. No publication or archive access."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
from importlib.resources import files
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Mapping
import unicodedata
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from markdown_it import MarkdownIt
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
import yaml

from kelmo.store.graph.parser import _inline_with_source_map, _wikilink_rule

from . import pages
from .engine import valid_wiki_target

VERSION = 'wiki-file-contract/3.0-proposal.2'
SCHEMA_URI = 'urn:kelmo:wiki-file-contract:3.0:'
MARKERS = ('<!-- kb -->', '<!-- /kb -->', '<!-- narrative -->', '<!-- /narrative -->')
HEADINGS = {
    'person': ['Mandatory rules', 'Person facts', 'General actions'],
    'project-map': ['Current map', 'Constraints', 'Actions', 'Detail pages'],
    'topic': ['Current scope', 'Feature boxes', 'Legacy evidence'],
    'feature': ['Behavior', 'Action state', 'Related pages'],
}


def canonical_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _same_json(left, right):
    return canonical_json(left) == canonical_json(right)


def entity_revision(value: dict) -> str:
    return sha256(canonical_json({k: v for k, v in value.items()
                                  if k not in ('revision', 'previous_revision')}))


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class ControllerInput:
    """An independent controller snapshot, never constructed from worker data by this module.

    Source IDs name supplied immutable bytes, not paths the validator opens. The caller
    establishes origin/enrollment; consistency of a supplied receipt is not authentication.
    """
    contract: dict
    baseline: dict
    scope: dict
    census: dict
    sources: Mapping[str, bytes]
    baseline_manifest: bytes
    registry_target: str
    registry_source_id: str
    state_source_id: str
    evidence_source_id: str
    history_source_id: str
    owning_receipts_source_id: str
    repositories: tuple[str, ...]
    evidence_reads: Mapping[str, str]
    protected_fact_keys: tuple[str, ...]
    narrative_dependencies: Mapping[str, Mapping[str, tuple[str, ...]]]
    page_catalog: bytes | None = None
    page_catalog_pin: dict | None = None


@dataclass(frozen=True)
class ValidationResult:
    structural_valid: bool
    errors: tuple[Issue, ...]
    unresolved: tuple[Issue, ...]
    coverage: dict
    identities: dict
    runtime_accepted: bool = False


def _no_retrieval(uri):
    raise NoSuchResource(ref=uri)


def _validators():
    resources = []
    schemas = {}
    directory = files('kelmo.wiki').joinpath('schemas/wiki-file-contract/3.0')
    for name in ('page', 'records', 'worker-manifest'):
        schema = json.loads(directory.joinpath(name + '.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
        resources.append((schema['$id'], Resource.from_contents(schema)))
    registry = Registry(retrieve=_no_retrieval).with_resources(resources)
    return {name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
            for name, schema in schemas.items()}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key: ' + key)
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _pointer_parts(locator):
    if locator == '':
        return ()
    if not locator.startswith('/') or re.search(r'~(?![01])', locator):
        raise ValueError('invalid JSON pointer')
    return tuple(part.replace('~1', '/').replace('~0', '~') for part in locator.split('/')[1:])


def _pointer(value, locator):
    for part in _pointer_parts(locator):
        if isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', part):
                raise ValueError('noncanonical array pointer')
            value = value[int(part)]
        else:
            value = value[part]
    return value


def _leaves(value, path=''):
    if isinstance(value, dict) and value:
        for key, child in sorted(value.items()):
            yield from _leaves(child, path + '/' + key.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list) and value:
        for i, child in enumerate(value):
            yield from _leaves(child, path + '/' + str(i))
    else:
        yield path


def _range(raw, locator):
    match = re.fullmatch(r'bytes:(0|[1-9][0-9]*):(0|[1-9][0-9]*)', locator)
    if not match:
        raise ValueError('expected canonical UTF-8 byte range')
    start, end = map(int, match.groups())
    if not 0 <= start <= end <= len(raw):
        raise ValueError('byte range outside source')
    raw[:start].decode('utf-8')
    raw[start:end].decode('utf-8')
    return raw[start:end]


def _output_span(raw, locator):
    if locator.startswith('bytes:'):
        _range(raw, locator)
        return tuple(map(int, locator.split(':')[1:]))
    match = re.fullmatch(r'span:([a-z-]+):(0|[1-9][0-9]*):([a-z-]+):(0|[1-9][0-9]*)', locator)
    if not match:
        raise ValueError('expected anchored output span')
    anchors = {'bof': 0, 'eof': len(raw)}
    for name, marker in zip(('kb-start', 'kb-end', 'narrative-start', 'narrative-end'), MARKERS):
        literal = marker.encode()
        positions = list(re.finditer(rb'(?m)^' + re.escape(literal) + rb'$', raw))
        if len(positions) == 1 and raw.count(literal) == 1:
            anchors[name] = positions[0].start() if name.endswith('start') else positions[0].end()
    left, offset, right, stop = match.groups()
    if left not in anchors or right not in anchors:
        raise ValueError('output anchor missing or ambiguous')
    start, end = anchors[left] + int(offset), anchors[right] + int(stop)
    _range(raw, f'bytes:{start}:{end}')
    return start, end


def _structural_bytes(raw, start, end):
    """Pin generated bytes while leaving separately validated managed content editable."""
    spans = []
    for opening, closing in (MARKERS[:2], MARKERS[2:]):
        if raw.count(opening.encode()) == raw.count(closing.encode()) == 1:
            first, last = raw.index(opening.encode()), raw.index(closing.encode()) + len(closing)
            if start <= first < last <= end:
                spans.append((first, last))
    cursor, chunks = start, []
    for first, last in sorted(spans):
        chunks.append(raw[cursor:first])
        cursor = last
    return b''.join(chunks) + raw[cursor:end]


def _canonical_path(path):
    return (isinstance(path, str) and bool(path) and '\\' not in path and '\x00' not in path
            and not PurePosixPath(path).is_absolute() and str(PurePosixPath(path)) == path
            and all(part not in ('.', '..') and not part.startswith('.')
                    for part in path.split('/')))


def _read(root, relative):
    if root.is_symlink():
        raise ValueError('symlink transport root')
    if not _canonical_path(relative):
        raise ValueError('noncanonical relative path')
    path = root
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('symlink in supplied path')
    if not path.is_file():
        raise ValueError('supplied file missing')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('supplied path outside transport root')
    return path.read_bytes()


def _output_path(name, outputs):
    if not _canonical_path(name):
        raise ValueError('noncanonical destination path')
    if name in outputs:
        return name
    matches = [path for path in outputs if PurePosixPath(path).name == name]
    if len(matches) != 1:
        raise ValueError('output destination missing or ambiguous')
    return matches[0]


def _output_location(reference, outputs):
    name, locator = reference.split('#', 1)
    return _output_path(name, outputs), locator


def _json_output_location(reference, outputs):
    path, locator = _output_location(reference, outputs)
    return path, _pointer_parts(locator)


class _UniqueYaml(yaml.SafeLoader):
    pass


def _yaml_object(loader, node):
    value = {}
    for key, item in node.value:
        name = loader.construct_object(key)
        if name in value:
            raise ValueError('duplicate frontmatter key')
        value[name] = loader.construct_object(item)
    return value


_UniqueYaml.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_object)


def _inline_text(token):
    """Plain declarations only; identifiers in comments/code cannot supply evidence."""
    return ''.join(child.content if child.type == 'text' else
                   ' ' if child.type == 'softbreak' else
                   '\n' if child.type == 'hardbreak' else ''
                   for child in token.children or [])


def _action_parts(token):
    """Account for every visible value token; unsupported content invalidates the field."""
    parts = []
    for child in token.children or []:
        if child.type in ('text', 'code_inline'):
            parts.append((child.type, child.content))
        elif child.type == 'softbreak':
            parts.append((child.type, ' '))
        elif child.type == 'hardbreak':
            parts.append((child.type, '\n'))
        elif child.type == 'wikilink':
            source = child.meta['source'][2:-2]
            parts.append((child.type, source.split('|', 1)[-1]))
        elif child.type not in ('em_open', 'em_close', 'strong_open', 'strong_close', 'link_open', 'link_close'):
            return None
    return parts


def _action_text(token):
    parts = _action_parts(token)
    return ''.join(text for _, text in parts) if parts is not None else None


def _plain_marker(token, identity, at, *, stripped=False):
    marker = '[a:' + identity + ']'
    parts = _action_parts(token)
    if parts is None:
        return False
    if stripped:
        text = ''.join(value for _, value in parts)
        at += len(text) - len(text.lstrip())
    cursor = 0
    for kind, text in parts:
        if kind == 'text' and cursor <= at and at + len(marker) <= cursor + len(text):
            return text[at - cursor:at - cursor + len(marker)] == marker
        cursor += len(text)
    return False


def _feature_subject(token, link_ids):
    """The Feature cell contains one complete link, with no additional subject claim."""
    if _action_parts(token) is None:
        return None
    inside, count = False, 0
    for child in token.children or []:
        if child.type == 'link_open':
            if inside or count:
                return None
            inside, count = True, count + 1
        elif child.type == 'link_close':
            inside = False
        elif child.type == 'wikilink':
            if inside or count:
                return None
            count += 1
        elif not inside and child.type not in ('em_open', 'em_close', 'strong_open', 'strong_close'):
            if child.type != 'text' or child.content.strip():
                return None
    identities = link_ids(token)
    return next(iter(identities)) if count == 1 and not inside and len(identities) == 1 else None


def _source_wikilink(state, silent):
    start = state.pos
    if not _wikilink_rule(state, silent):
        return False
    if not silent:
        state.tokens[-1].meta['source'] = state.src[start:state.pos]
    return True


def _markdown():
    markdown = MarkdownIt('commonmark').enable('table')
    markdown.inline.ruler.before('link', 'wikilink', _source_wikilink)
    markdown.core.ruler.at('inline', _inline_with_source_map)
    return markdown


def _only_well_formed_comments(text):
    """Allow only complete comment syntax, without browser error recovery."""
    cursor, count = 0, 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        if not text.startswith('<!--', cursor):
            return False
        end = text.find('-->', cursor + 4)
        if end < 0:
            return False
        body = text[cursor + 4:end]
        if (body.startswith(('>', '->')) or body.endswith('<!-')
                or any(part in body for part in ('<!--', '-->', '--!>'))):
            return False
        cursor, count = end + 3, count + 1
    return count > 0


# Element names a browser renders as HTML, current and obsolete, plus the SVG and MathML roots.
HTML_ELEMENTS = frozenset('''
    a abbr acronym address applet area article aside audio b base basefont bdi bdo bgsound big blink
    blockquote body br button canvas caption center cite code col colgroup data datalist dd del details
    dfn dialog dir div dl dt em embed fieldset figcaption figure font footer form frame frameset h1 h2
    h3 h4 h5 h6 head header hgroup hr html i iframe image img input ins isindex kbd keygen label legend
    li link listing main map mark marquee math menu menuitem meta meter multicol nav nextid nobr noembed
    noframes noscript object ol optgroup option output p param picture plaintext pre progress q rb rp
    rt rtc ruby s samp script search section select slot small source spacer span strike strong style
    sub summary sup svg table tbody td template textarea tfoot th thead time title tr track tt u ul var
    video wbr xmp'''.split())
# A value placeholder such as <ts>, <session-id> or <run folder>: words only, no attribute value,
# quote or slash, and a first word that names no HTML element.
PLACEHOLDER = re.compile(r'<([A-Za-z][A-Za-z0-9_-]*)(?: [A-Za-z0-9_-]+)*>')


def _only_placeholders(text):
    found = list(PLACEHOLDER.finditer(text))
    return (bool(found) and not PLACEHOLDER.sub('', text).strip()
            and not any(match.group(1).lower() in HTML_ELEMENTS for match in found))


def _has_raw_html(tokens, *, allow_comments=False, allow_placeholders=False):
    pending = list(tokens)
    while pending:
        token = pending.pop()
        if token.type == 'html_inline' and allow_placeholders and _only_placeholders(token.content):
            continue
        if token.type in ('html_inline', 'html_block') and not (
                allow_comments and _only_well_formed_comments(token.content)):
            return True
        pending.extend(token.children or [])
    return False


def _inline_signature(children, before=None):
    result = []
    for child in children:
        content = child.content
        last = before is not None and child.type == 'text' and before in content
        if last:
            content = content.split(before, 1)[0]
        if child.type in ('text', 'softbreak', 'hardbreak'):
            content = content if child.type == 'text' else ' ' if child.type == 'softbreak' else '\n'
            if result and result[-1][0] == 'text':
                result[-1] = ('text', result[-1][1] + content)
            else:
                result.append(('text', content))
        elif child.type == 'wikilink':
            result.append(('wikilink', child.meta['source']))
        else:
            result.append((child.type, child.tag, content, tuple(sorted(child.attrs.items()))))
        if last:
            break
    if result and result[0][0] == 'text':
        result[0] = ('text', result[0][1].lstrip())
    if result and result[-1][0] == 'text':
        result[-1] = ('text', result[-1][1].rstrip())
    return [part for part in result if part != ('text', '')]


def _expected_signature(text):
    return _inline_signature(_markdown().parseInline(text)[0].children or [])


def _visible_lines(tokens):
    return [line.strip() for token in tokens if token.type == 'inline'
            for line in _inline_text(token).splitlines() if line.strip()]


def _heading_texts(tokens, tag):
    return [_inline_text(tokens[i + 1]) for i, token in enumerate(tokens[:-1])
            if token.type == 'heading_open' and token.tag == tag
            and tokens[i + 1].type == 'inline']


def _region_tokens(tokens, body, opening, closing):
    # Select from the whole page parse so a surrounding comment/fence stays hidden.
    first = body[:body.index(opening)].count('\n') + 1
    last = body[:body.index(closing)].count('\n')
    selected, inside = [], False
    for token in tokens:
        if token.map is not None:
            inside = first <= token.map[0] < last and token.map[1] <= last
        if inside:
            selected.append(token)
    return selected


def _declared_groups(lines, pattern):
    matches = [match.groups() for line in lines for match in re.finditer(pattern, line)]
    return matches[0] if len(matches) == 1 else None


PER_ACTION_DECLARATIONS = {'work_revision': 'Work revision', 'artifact_id': 'Deployment artifact'}


def _plain_prefix(token, value):
    """True when `value` starts the token's visible text inside one plain text part, not code."""
    parts = _action_parts(token)
    if parts is None:
        return False
    text = ''.join(part for _, part in parts)
    at = len(text) - len(text.lstrip())
    cursor = 0
    for kind, part in parts:
        if cursor <= at < cursor + len(part):
            return kind == 'text' and part[at - cursor:at - cursor + len(value)] == value
        cursor += len(part)
    return False


def _per_action_declarations(tokens):
    """Feature-panel paragraphs `<Label> of <action-id>: <value>.`, one per displayed action.

    Each declaration is its own paragraph and its action ID must be plain visible text. A
    malformed, repeated or code-supplied identity poisons that field for the whole panel instead
    of binding the value to some other action.
    """
    found = {name: {} for name in PER_ACTION_DECLARATIONS}
    poisoned = set()
    paragraph = False
    for token in tokens:
        if token.type == 'paragraph_open':
            paragraph = True
        elif token.type == 'paragraph_close':
            paragraph = False
        elif paragraph and token.type == 'inline':
            text = _action_text(token)
            line = text.strip() if text is not None else ''
            for name, label in PER_ACTION_DECLARATIONS.items():
                if not line.startswith(label + ' of '):
                    continue
                match = re.fullmatch(re.escape(label) + r' of ([a-z0-9._-]+): ([a-z0-9._-]+)\.', line)
                if (not match or match[1] in found[name]
                        or not _plain_prefix(token, line[:match.end(1)])):
                    poisoned.add(name)
                    continue
                found[name][match[1]] = match[2]
    return found, poisoned


def _action_projections(tokens, link_ids, layout):
    """Read the existing action table columns or general-action sentence fields."""
    projections = []
    lines = [line.strip() for token in tokens if token.type == 'inline'
             and (text := _action_text(token)) is not None for line in text.splitlines() if line.strip()]
    shared = _declared_groups(lines, r'^Subject: ([a-z0-9._-]+)/([a-z0-9._-]+)\. Feature ID: ([a-z0-9._-]+)\.')
    declared = {'work_revision': _declared_groups(lines, r'\bWork revision: ([a-z0-9._-]+)\.'),
                'artifact_id': _declared_groups(lines, r'^Deployment artifact: ([a-z0-9._-]+)\.')}
    per_action, poisoned = _per_action_declarations(tokens) if layout == 'feature' else ({}, set())

    def bound(name, identity):
        # A shared declaration and per-action ones together are ambiguous; neither is trusted.
        if name in poisoned:
            return None
        if per_action.get(name):
            return None if declared[name] else per_action[name].get(identity)
        return declared[name][0] if declared[name] else None
    table, header, paragraph = False, False, False
    headers, row = [], []
    for token in tokens:
        if token.type == 'table_open':
            table, headers = True, []
        elif token.type == 'table_close':
            table = False
        elif token.type == 'thead_open':
            header = True
        elif token.type == 'thead_close':
            header = False
        elif table and token.type == 'tr_open':
            row = []
        elif table and token.type == 'inline':
            row.append(token)
        elif table and token.type == 'tr_close':
            if header:
                texts = [_action_text(cell) for cell in row]
                headers = [text.strip().casefold() for text in texts] if None not in texts else []
                continue
            if len(headers) != len(set(headers)) or len(row) != len(headers):
                continue
            cells = dict(zip(headers, row))
            texts = {name: _action_text(cell) for name, cell in cells.items()}
            if any(text is None for text in texts.values()):
                continue
            fields = {name: text.strip() for name, text in texts.items()}
            identity = fields.get('action id', fields.get('action', ''))
            match = re.fullmatch(r'(.*?)\[a:([a-z0-9._-]+)\]', identity)
            identity_cell = cells.get('action id', cells.get('action'))
            if match and _plain_marker(identity_cell, match[2], match.start(2) - 3, stripped=True):
                if 'action id' in cells and match[1].strip():
                    continue
                kind = fields.get('kind', match[1].strip()).casefold()
                if match[1].strip() and match[1].strip().casefold() != kind:
                    continue
                subjects = [fields['subject']] if 'subject' in fields else []
                if 'feature' in cells:
                    subjects.append(_feature_subject(cells['feature'], link_ids))
                if layout == 'feature':
                    subjects.append(shared[1] if shared else None)
                subject = subjects[0] if subjects and all(x == subjects[0] for x in subjects) else None
                values = dict(kind=kind, state=fields.get('state'), owner=fields.get('owner'), subject_id=subject,
                              repository_id=fields.get('repository') or None,
                              destination=fields.get('destination/environment') or None, evidence=fields.get('evidence'))
                if layout == 'feature':
                    values.update(subject_kind=shared[0] if shared else None, feature_id=shared[2] if shared else None)
                    if kind in ('push', 'merge', 'deploy'):
                        values['work_revision'] = bound('work_revision', match[2])
                    if kind == 'deploy':
                        values['artifact_id'] = bound('artifact_id', match[2])
                projections.append((match[2], 'table', values))
        elif token.type == 'paragraph_open':
            paragraph = True
        elif token.type == 'paragraph_close':
            paragraph = False
        elif paragraph and token.type == 'inline':
            text = _action_text(token)
            if text is None:
                continue
            match = re.search(
                r'\[a:([a-z0-9._-]+)\]: ([^.]+)\. Subject: ([^;]+); '
                r'kind: ([^;]+); owner: ([^.]+)\. Evidence: ([A-Z]+), ([a-z0-9._-]+) v([0-9]+); '
                r'owning receipt: ([a-z0-9._-]+)\.', text)
            if match and _plain_marker(token, match[1], match.start(1) - 3):
                key, state, subject, kind, owner, mark, source, version, receipt = match.groups()
                projections.append((key, 'general', dict(kind=kind.strip().casefold(), state=state.strip(),
                    owner=owner.strip(), subject_id=subject.strip(), evidence_mark=mark,
                    evidence_source=source, evidence_version=version, operation_receipt_id=receipt)))
    for name, values in per_action.items():
        carried = {key for key, form, fields in projections if form == 'table' and name in fields}
        if set(values) - carried:
            # A declaration for an action this panel does not show (or that has no such field)
            # was substituted or misplaced; no displayed action keeps a value from this field.
            for key, form, fields in projections:
                if form == 'table' and name in fields:
                    fields[name] = None
    return projections


def _expected_action_fields(action, layout, form):
    values = {key: action[key] for key in ('kind', 'state', 'owner', 'subject_id')}
    event = action['accepted_event']
    evidence = event['evidence_ref']
    if form == 'general':
        values.update(evidence_mark=event['evidence_mark'], evidence_source=evidence['source_id'],
                      evidence_version=str(evidence['version']), operation_receipt_id=event['operation_receipt_id'])
    else:
        destination = action.get('remote_ref') if action['kind'] in ('push', 'merge') else action.get('environment')
        values.update(repository_id=action.get('repository_id'), destination=destination,
                      evidence=f'{evidence["source_id"]} v{evidence["version"]}')
        if layout == 'feature':
            values.update(subject_kind=action['subject_kind'], feature_id=action.get('feature_id', action['subject_id']))
            if action['kind'] in ('push', 'merge', 'deploy'):
                values['work_revision'] = action.get('work_revision')
            if action['kind'] == 'deploy':
                values['artifact_id'] = action['artifact_id']
    return values


@dataclass
class _Check:
    root: Path
    controller: ControllerInput
    allow_fixture: bool
    errors: list[Issue] = field(default_factory=list)
    unresolved: list[Issue] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    identities: dict = field(default_factory=lambda: {'sources': {}, 'outputs': {}})

    def error(self, code, path, detail):
        self.errors.append(Issue(code, path, detail))

    def schema(self, validator, value, path, code='schema'):
        errors = list(validator.iter_errors(value))
        for error in sorted(errors, key=lambda e: (str(list(e.absolute_path)), e.message)):
            self.error(code, path + '/' + '/'.join(map(str, error.absolute_path)), error.message)
        return not errors

    def result(self):
        def order(issue):
            return issue.code, issue.path, issue.detail
        return ValidationResult(not self.errors, tuple(sorted(set(self.errors), key=order)),
                                tuple(sorted(set(self.unresolved), key=order)), self.totals,
                                self.identities)

    def run(self, manifest_path):
        validators = _validators()
        raw = _read(self.root, manifest_path)
        self.identities['manifest_sha256'] = sha256(raw)
        manifest = _json(raw)
        if not self.schema(validators['worker-manifest'], manifest, manifest_path):
            return
        controller = self.controller
        for name in ('contract', 'baseline', 'scope'):
            self.schema(validators['worker-manifest'].evolve(schema={
                '$ref': SCHEMA_URI + 'worker-manifest#/properties/' + name}),
                getattr(controller, name), 'controller/' + name, 'input')
        if not isinstance(controller.sources, Mapping):
            self.error('input', 'controller/sources', 'source byte mapping required')
        if not isinstance(controller.evidence_reads, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in controller.evidence_reads.items()):
            self.error('input', 'controller/evidence_reads', 'receipt ID mapping required')
        if not isinstance(controller.narrative_dependencies, Mapping) or any(
                not isinstance(value, Mapping) or set(value) != {'facts', 'actions'}
                or any(not isinstance(refs, (tuple, list)) or any(not isinstance(x, str) for x in refs)
                       for refs in value.values()) for value in controller.narrative_dependencies.values()):
            self.error('input', 'controller/narrative_dependencies', 'dependency reference maps required')
        if self.errors:
            return
        catalog = self.catalog_snapshot(validators['worker-manifest'])
        if catalog is None:
            return
        census = controller.census
        if not self.schema(validators['worker-manifest'].evolve(
                schema={'$ref': SCHEMA_URI + 'worker-manifest#/$defs/census'}), census, 'controller/census'):
            return
        self.identities['contract'] = dict(controller.contract)
        self.identities['baseline'] = dict(controller.baseline)
        self.identities['census_sha256'] = sha256(canonical_json(census))
        if 'fixture' in manifest and not self.allow_fixture:
            self.error('fixture', manifest_path, 'fixture transport is refused by default admission')
        if manifest['review'] != {'semantic_preservation': 'pending', 'protected_rules': 'pending',
                                  'approval': 'not_requested'}:
            self.error('ownership', 'review', 'workers cannot issue review/approval authority')
        if not _same_json(manifest['contract'], controller.contract) or controller.contract['version'] != VERSION:
            self.error('contract', 'contract', 'worker contract differs from controller pin')
        contract = _read(self.root, controller.contract['path'])
        self.identities['contract_actual_sha256'] = sha256(contract)
        if sha256(contract) != controller.contract['sha256']:
            self.error('contract', 'contract', 'contract bytes do not match pin')
        if not _same_json(manifest['baseline'], controller.baseline) or not _same_json(census['baseline'], controller.baseline):
            self.error('baseline', 'baseline', 'generation, head, epoch or registry baseline differs')
        if census['issuer']['authority_id'] != controller.baseline['generation']['authority_id']:
            self.error('census', 'issuer', 'census authority differs from assignment')
        if manifest['scope'].get('adoption_unit_ids') != controller.scope['adoption_unit_ids']:
            self.error('adoption', 'scope', 'adoption assignment differs')
        if not _same_json(manifest['scope'], controller.scope) or not _same_json(census['scope'], controller.scope):
            self.error('census', 'scope', 'worker/census scope differs from controller assignment')
        census_ref = manifest['inputs']['census']
        if census_ref['locator'] != '' or census_ref['sha256'] != self.identities['census_sha256']:
            self.error('census', 'inputs/census', 'census identity differs from trusted snapshot')
        if 'fixture' in manifest and not _same_json(manifest['fixture'].get('controller_census'), census):
            self.error('census', 'fixture/controller_census', 'worker copy cannot replace controller census')
        declared = self.unique(manifest['inputs']['files'], 'source_id', 'source')
        trusted = self.unique(census['sources'], 'source_id', 'census')
        role_sources = {controller.registry_source_id, controller.state_source_id,
                        controller.evidence_source_id, controller.history_source_id,
                        controller.owning_receipts_source_id}
        if not role_sources <= set(trusted):
            self.error('source', 'controller', 'required role snapshots must be pinned census sources')
        if set(declared) != set(trusted) | {census_ref['source_id']}:
            self.error('source', 'inputs/files', 'source ID set differs from controller census plus census file')
        for source_id, item in declared.items():
            if not _canonical_path(item['path']):
                self.error('path', item['path'], 'source paths must be canonical enrolled relative paths')
            if source_id in trusted and item != trusted[source_id]:
                self.error('source', source_id, 'worker source pin differs from trusted source')
            data = controller.sources.get(source_id)
            if not isinstance(data, bytes):
                self.error('source', source_id, 'caller did not supply required source bytes')
                continue
            self.identities['sources'][source_id] = sha256(data)
            if sha256(data) != item['sha256']:
                self.error('source', source_id, 'source bytes do not match exact hash')
        if controller.sources.get(census_ref['source_id']) != canonical_json(census):
            self.error('census', 'inputs/census', 'actual supplied census bytes differ')
        if self.errors:
            return
        baseline = _json(controller.baseline_manifest)
        self.identities['baseline_manifest_sha256'] = sha256(controller.baseline_manifest)
        old = _json(controller.sources[controller.registry_source_id])
        state = _json(controller.sources[controller.state_source_id])
        evidence = _json(controller.sources[controller.evidence_source_id])
        history = _json(controller.sources[controller.history_source_id])
        owning = _json(controller.sources[controller.owning_receipts_source_id])
        for name, value in (('baseline', baseline), ('registry', old), ('state', state),
                            ('evidence', evidence), ('history', history), ('owning', owning)):
            self.schema(validators['worker-manifest'].evolve(schema={
                '$ref': SCHEMA_URI + 'worker-manifest#/$defs/source-' + name}), value,
                'controller/source-' + name, 'input')
        if self.errors:
            return
        self.legacy_snapshot(old)
        if (sha256(controller.baseline_manifest) != controller.baseline['generation']['manifest_sha256']
                or sha256(controller.sources[controller.registry_source_id]) != controller.baseline['registry_sha256']
                or any(baseline.get(k) != controller.baseline[k]
                       for k in ('repository_head', 'writer_epoch', 'registry_sha256'))
                or any(baseline.get(k) != v for k, v in controller.baseline['generation'].items()
                       if k != 'manifest_sha256')):
            self.error('baseline', 'baseline', 'supplied baseline bytes do not establish pinned generation')
        outputs = self.unique(manifest['outputs']['files'], 'path', 'path')
        targets = self.unique(manifest['outputs']['files'], 'target', 'path')
        if set(outputs) != set(controller.scope['output_paths']):
            self.error('path', 'outputs', 'output path set differs from assignment')
        prior = {item['target']: item['sha256'] for item in baseline['artifacts']}
        if (not _canonical_path(controller.registry_target)
                or not controller.registry_target.endswith('.json')
                or prior.get(controller.registry_target) != controller.baseline['registry_sha256']):
            self.error('baseline', 'controller/registry_target',
                       'assigned registry destination must bind the pinned baseline registry hash')
        raw_outputs = {}
        registry = None
        for path, item in outputs.items():
            data = _read(self.root, path)
            raw_outputs[path] = data
            self.identities['outputs'][path] = {'target': item['target'], 'sha256': sha256(data)}
            if sha256(data) != item['sha256']:
                self.error('output-hash', path, 'output bytes differ from manifest')
            if item['expected_sha256'] != prior.get(item['target']):
                self.error('baseline', path, 'target baseline hash differs from generation')
            if item['target'].endswith('.json'):
                if item['target'] != controller.registry_target:
                    self.error('ownership', path, 'registry destination differs from controller assignment')
                if item['expected_sha256'] != controller.baseline['registry_sha256']:
                    self.error('baseline', path, 'registry output must expect the pinned registry hash')
                if registry is not None or not _canonical_path(item['target']):
                    self.error('path', path, 'exactly one assigned registry required')
                registry = _json(data)
        if registry is None or not self.schema(validators['records'], registry, 'records'):
            self.error('schema', 'records', 'a valid single v3 registry is required')
            return
        if not self.reference_graph(registry, catalog):
            return
        self.registry(registry, old, state, history, manifest)
        self.coverage(census, manifest, raw_outputs)
        self.evidence(evidence, owning, registry, manifest, state)
        self.projections(registry, manifest, raw_outputs, targets, validators['page'])
        self.output_coverage(census, raw_outputs)
        self.unresolved.extend(Issue('legacy-evidence', x['source_id'] + '#' + x['locator'], x['reason'])
                               for x in manifest['unresolved'])
        self.unresolved.extend([
            Issue('runtime-required', 'delivery', 'Startup/compact delivery and overflow refusal require runtime acceptance.'),
            Issue('runtime-required', 'review', 'Independent meaning/loss review and receipt authentication remain required.'),
            Issue('runtime-required', 'publisher', 'Locking, visibility, transition legality and crash recovery are not checked offline.'),
        ])

    def unique(self, items, key, code):
        result = {}
        for item in items:
            value = item[key]
            if value in result:
                self.error(code, str(value), 'duplicate ' + key)
            result[value] = item
        return result

    def catalog_snapshot(self, validator):
        raw, pin = self.controller.page_catalog, self.controller.page_catalog_pin
        if raw is None and pin is None:
            return {'pages': {}, 'redirects': {}}
        if not isinstance(raw, bytes) or pin is None:
            self.error('catalog', 'controller/page_catalog', 'catalog bytes and independent pin required together')
            return None
        if not self.schema(validator.evolve(schema={
                '$ref': SCHEMA_URI + 'worker-manifest#/$defs/page-catalog-pin'}),
                pin, 'controller/page_catalog_pin', 'catalog'):
            return None
        actual = sha256(raw)
        self.identities['page_catalog'] = dict(pin, sha256=actual, expected_sha256=pin['sha256'])
        if actual != pin['sha256']:
            self.error('catalog', 'controller/page_catalog', 'catalog bytes differ from trusted pin')
            return None
        catalog = _json(raw)
        if not self.schema(validator.evolve(schema={
                '$ref': SCHEMA_URI + 'worker-manifest#/$defs/page-catalog'}),
                catalog, 'controller/page_catalog', 'catalog'):
            return None
        if (any(catalog[k] != pin[k] for k in ('catalog_id', 'authority_id'))
                or catalog['authority_id'] != self.controller.baseline['generation']['authority_id']):
            self.error('catalog', 'controller/page_catalog', 'catalog identity/authority differs from assignment')
            return None
        return catalog

    def reference_graph(self, registry, catalog):
        """Resolve metadata across the supplied family and read-only external snapshot."""
        before = len(self.errors)
        assigned, external = registry['pages'], catalog['pages']
        assigned_ids = {p['page_id'] for p in assigned.values()}
        reserved = assigned_ids | set(registry['actions']) | set(registry['narratives'])
        if set(assigned) & set(external) or reserved & {p['page_id'] for p in external.values()}:
            self.error('catalog', 'pages', 'catalog must not shadow assigned targets or entity IDs')
        if set(registry['redirects']) & set(catalog['redirects']):
            self.error('catalog', 'redirects', 'worker cannot replace catalog redirects')
        self.reference_pages = {**external, **assigned}
        self.reference_redirects = {**catalog['redirects'], **registry['redirects']}
        page_ids = self.unique(list(self.reference_pages.values()), 'page_id', 'identity')
        for target, page_id in registry['redirects'].items():
            if page_id not in assigned_ids:
                self.error('catalog', target, 'worker redirects cannot create external catalog entries')
        aliases = {}
        canonical = set(self.reference_pages) | set(self.reference_redirects)
        for target, page in self.reference_pages.items():
            if not valid_wiki_target(target):
                self.error('path', target, 'invalid existing wiki target grammar')
            for alias in page['aliases']:
                if (not alias.strip() or unicodedata.normalize('NFC', alias) != alias
                        or alias in aliases or alias in canonical or alias + '.md' in canonical):
                    self.error('alias', target, 'ambiguous, shadowing or noncanonical alias: ' + alias)
                aliases[alias] = page['page_id']
            seen, current = set(), target
            while current:
                if (current not in self.reference_pages or current in seen
                        or len(seen) > pages.MAX_PARENT_DEPTH):
                    self.error('parent', target, 'missing/cyclic/too-deep parent')
                    break
                seen.add(current)
                current = self.reference_pages[current]['parent']
        for target, page_id in self.reference_redirects.items():
            if not valid_wiki_target(target) or target in self.reference_pages or page_id not in page_ids:
                self.error('alias', target, 'invalid redirect or missing identity')
        return len(self.errors) == before

    def legacy_snapshot(self, old):
        """The narrow legacy-summary-assessment/1 adapter keeps original fields intact."""
        if old.get('version') not in (1, 2):
            self.error('compatibility', 'baseline', 'only v1/v2 imports are supported')
        for target, page in old.get('pages', {}).items():
            # Validate the current six-field view without discarding the preserved source.
            projected = {k: page[k] for k in pages.ENTRY_FIELDS}
            projected['revision'] = page['revision']
            try:
                pages.validate_entry(target, projected)
            except ValueError as error:
                self.error('compatibility', target, str(error))
            assessment = {'summary_input_hash', 'summary_assessed_by'} & page.keys()
            null_assessment = (page.get('summary_input_hash') is None
                               and page.get('summary_assessed_by') is None)
            named_assessment = (isinstance(page.get('summary_input_hash'), str)
                                and re.fullmatch('[0-9a-f]{64}', page['summary_input_hash'])
                                and isinstance(page.get('summary_assessed_by'), str))
            if assessment and (assessment != {'summary_input_hash', 'summary_assessed_by'}
                               or not (null_assessment or named_assessment)):
                self.error('compatibility', target, 'legacy assessment/hash relationship incomplete')
        for key, fact in old['facts'].items():
            if fact['revision'] != pages.fact_revision(fact):
                self.error('revision', key, 'legacy fact role-aware revision differs')

    def registry(self, registry, old, state, history, manifest):
        entries = registry['pages']
        facts = registry['facts']
        actions = registry['actions']
        narratives = registry['narratives']
        registry_path = next(item['path'] for item in manifest['outputs']['files'] if item['target'].endswith('.json'))
        output_paths = {item['path'] for item in manifest['outputs']['files']}
        claim_destinations = set()
        for unit in self.controller.census['units']:
            if unit['unit'] == 'claim' and unit['operation'] == 'project':
                try:
                    claim_destinations.add(_json_output_location(unit['destination'], output_paths))
                except ValueError as error:
                    self.error('coverage', unit['unit_id'], str(error))
        for kind in ('facts', 'narratives'):
            for key, record in registry[kind].items():
                if _has_raw_html(_markdown().parse(record['text'])):
                    self.error('unsupported-html', key,
                               'raw HTML in required source text cannot establish a supported projection; original data must be retained')
                if (registry_path, (kind, key, 'text')) not in claim_destinations:
                    self.error('coverage', key, 'factual/narrative text requires a controller source-backed claim mapping')
        page_ids = self.unique(list(entries.values()), 'page_id', 'identity')
        if set(page_ids) != set(self.controller.scope['page_ids']):
            self.error('identity', 'pages', 'page IDs differ from full assigned family')
        if registry['authority_id'] != self.controller.baseline['generation']['authority_id']:
            self.error('identity', 'authority_id', 'registry authority differs')
        for target, page in entries.items():
            if not valid_wiki_target(target):
                self.error('path', target, 'invalid existing wiki target grammar')
            projected = {name: page[name] for name in pages.ENTRY_FIELDS}
            projected['revision'] = pages.entry_revision(projected)
            try:
                pages.validate_entry(target, projected)
            except ValueError as error:
                self.error('page', target, str(error))
            expected_layout = {'person': ('topic', 'you'), 'project-map': ('hub', 'projects'),
                               'topic': ('topic', 'projects'), 'feature': ('work', 'projects')}.get(page['layout'])
            if expected_layout and (page['kind'], page['category']) != expected_layout:
                self.error('page', target, 'layout/kind/category mismatch')
            invalid_feature = (page.get('feature_id') != page['page_id']
                               if page['layout'] == 'feature' else 'feature_id' in page)
            if invalid_feature:
                self.error('identity', target, 'feature ID belongs only to its feature page')
            if set(page['repositories']) - set(self.controller.repositories):
                self.error('ownership', target, 'unenrolled repository')
            if set(page['links']) - {p['page_id'] for p in self.reference_pages.values()}:
                self.error('link', target, 'link IDs do not name registered pages')
        entities = {'fact': facts, 'page': page_ids, 'action': actions, 'narrative': narratives}
        all_ids = []
        for kind, values in entities.items():
            for key, value in values.items():
                if kind != 'fact':
                    all_ids.append(key)
                expected_key = value[{'fact': 'fact_key', 'page': 'page_id', 'action': 'action_id', 'narrative': 'narrative_id'}[kind]]
                if key != expected_key:
                    self.error('identity', key, 'map key differs from entity identity')
                if value['revision'] != entity_revision(value):
                    self.error('revision', key, 'entity hash excludes only revision/previous_revision')
                if any(marker in value.get('text', '') for marker in MARKERS):
                    self.error('markers', key, 'record text contains region marker')
        if len(all_ids) != len(set(all_ids)):
            self.error('identity', 'entities', 'stable identity collision')
        display_ids = set()
        person = set()
        for key, fact in facts.items():
            if fact['id'] != sha256(key.encode())[:12] or fact['id'] in display_ids:
                self.error('identity', key, 'fact display ID mismatch/collision')
            display_ids.add(fact['id'])
            if fact['target'] not in entries or entries[fact['target']]['kind'] == 'prose':
                self.error('page', key, 'fact needs a registered non-prose target')
            elif fact['status'] in pages.OPEN and entries[fact['target']]['kind'] != 'work':
                self.error('page', key, 'open work requires a work page')
            if fact.get('index_target') and fact['index_target'] not in self.reference_pages:
                self.error('page', key, 'index target does not exist')
            if fact['protected'] and (fact['owner'] != 'rule-policy' or not fact['mandatory']):
                self.error('ownership', key, 'protected rule requires policy owner and mandatory delivery')
            if fact['target'] in entries and entries[fact['target']]['layout'] == 'person':
                person.add(key)
            if fact['note_id'] not in registry['notes']:
                self.error('evidence', key, 'note receipt missing')
            else:
                note = registry['notes'][fact['note_id']]
                if note['ark_id'] != fact['ark_id'] or note['evidence_refs'] != fact['evidence_refs']:
                    self.error('evidence', key, 'fact and note reference different evidence')
        for key, fact in old['facts'].items():
            current = facts.get(key)
            if current is None:
                self.error('preservation', key, 'existing fact identity omitted')
                continue
            target = fact['target']
            if target == 'person.md' or old.get('pages', {}).get(target, {}).get('category') == 'you':
                person.add(key)
            if not _same_json(current['compatibility'].get('legacy_fields'), fact):
                self.error('preservation', key, 'every original fact field/value/absence must survive')
            for name in ('fact_key', 'id', 'text', 'status', 'target', 'note_id', 'ark_id',
                         'role', 'expected_revision', 'replaces', 'index_target'):
                if ((name in current) != (name in fact)
                        or not _same_json(current.get(name), fact.get(name))):
                    self.error('preservation', key, 'deterministic import changed ' + name)
        for key in person:
            if key not in facts or not facts[key]['mandatory']:
                self.error('preservation', key, 'all person identities require startup+compact delivery membership')
        for key in self.controller.protected_fact_keys:
            fact = facts.get(key, {})
            if not fact.get('protected') or fact.get('owner') != 'rule-policy' or not fact.get('mandatory'):
                self.error('ownership', key, 'controller-inventoried protected rule lost edit protection')
        self.totals['person_facts'] = len(person)
        for kind in ('notes', 'pages'):
            for key, value in old.get(kind, {}).items():
                current = registry[kind].get(key, {})
                if not _same_json(current.get('compatibility', {}).get('legacy_fields'), value):
                    self.error('preservation', kind + '/' + key, 'legacy fields missing or changed')
                if kind == 'notes' and any(
                        (name in current) != (name in value)
                        or not _same_json(current.get(name), value.get(name))
                        for name in ('filename', 'ark_id')):
                    self.error('preservation', kind + '/' + key, 'active legacy note fields differ')
        if (('session_applications' in old) != ('session_applications' in registry)
                or not _same_json(old.get('session_applications'), registry.get('session_applications'))):
            self.error('preservation', 'session_applications', 'legacy session receipts changed')
        for key, action in actions.items():
            self.action(key, action, registry, state)
        if set(actions) != set(state['actions']):
            self.error('action', 'actions', 'state-owner identity set differs')
        for key, narrative in narratives.items():
            if narrative['page_id'] not in page_ids:
                self.error('dependency', key, 'narrative page missing')
            expected = self.controller.narrative_dependencies.get(key)
            actual = {kind: tuple(refs) for kind, refs in narrative['depends_on'].items()}
            if expected is None or any(set(actual[k]) != set(expected[k]) for k in ('facts', 'actions')):
                self.error('dependency', key, 'dependency identity set differs from supplied reviewed mapping')
            stale = False
            for kind in ('facts', 'actions'):
                for ref, revision in narrative['depends_on'][kind].items():
                    if ref not in registry[kind] or registry[kind][ref]['revision'] != revision:
                        stale = True
                        self.unresolved.append(Issue('stale-dependency', key, kind + '/' + ref))
            if stale and narrative['state'] != 'stale':
                self.error('dependency', key, 'changed/missing dependency cannot remain current')
        self.revisions(entities, old, state, history, manifest)

    def action(self, key, action, registry, state):
        kind = action['kind']
        required = {'push': {'repository_id', 'work_revision', 'remote_ref'},
                    'merge': {'repository_id', 'work_revision', 'remote_ref'},
                    'deploy': {'environment', 'artifact_id'}}.get(kind, set())
        forbidden = {'push': {'environment', 'artifact_id'}, 'merge': {'environment', 'artifact_id'},
                     'deploy': {'remote_ref'}}.get(kind, {'repository_id', 'work_revision', 'remote_ref', 'environment', 'artifact_id'})
        if required - action.keys() or forbidden & action.keys():
            self.error('action', key, 'kind-specific fields mismatch')
        if kind == 'deploy' and (('repository_id' in action) != ('work_revision' in action)):
            self.error('action', key, 'Git-derived deployment needs repository and work revision together')
        if 'repository_id' in action and action['repository_id'] not in self.controller.repositories:
            self.error('action', key, 'repository is not enrolled')
        if action['subject_kind'] == 'feature':
            if not any(p['layout'] == 'feature' and p['page_id'] == action['subject_id'] for p in registry['pages'].values()):
                self.error('action', key, 'feature subject missing')
        elif state['subject_bindings'].get(action['subject_id'], {}).get('subject_kind') != action['subject_kind']:
            self.error('action', key, 'general subject not enrolled')
        if 'feature_id' in action and (action['subject_kind'] != 'feature' or action['feature_id'] != action['subject_id']):
            self.error('action', key, 'feature ID differs from feature subject')
        if not _same_json(action, state['actions'].get(key)):
            self.error('action', key, 'projection differs from separately supplied state-owner snapshot')
        event = action['accepted_event']
        receipts = [r for r in state['receipts'] if r['operation_receipt_id'] == event['operation_receipt_id']]
        if len(receipts) != 1:
            self.error('action', key, 'exact owning StateAPI receipt missing/ambiguous')
            return
        receipt = receipts[0]
        binding = state['trusted_bindings'].get(receipt['origin']['client_id'], {})
        if (receipt['action_id'] != key or receipt['evidence_ref'] != event['evidence_ref']
                or receipt['accepted_state'] != action['state'] or event['transition'] != action['state']
                or receipt['evidence_mark'] != event['evidence_mark'] or receipt['outcome'] != 'accepted'
                or binding.get('principal_id') != receipt['attribution']['principal_id']
                or any(binding.get(k) != receipt['origin'][k] for k in ('machine_tag', 'authority_id'))
                or binding.get('role') != {'REQUESTER': 'requester', 'PRODUCER': 'producer'}[event['evidence_mark']]):
            self.error('action', key, 'receipt/origin/attribution consistency mismatch; labels supply no authentication')
        if event['correction_of'] is not None:
            prior = state.get('history', {}).get(key, [])
            if not any(p['accepted_event']['event_id'] == event['correction_of'] for p in prior):
                self.error('action', key, 'correction predecessor not retained')

    def revisions(self, entities, old, state, history, manifest):
        changes = {}
        for change in manifest['changes']:
            identity = (change['entity'], change['id'])
            if identity in changes:
                self.error('revision', change['id'], 'duplicate change')
            changes[identity] = change
        expected_changes = {(kind, key) for kind, values in entities.items() for key in values}
        if set(changes) != expected_changes:
            self.error('revision', 'changes', 'changes must enumerate the proposed entity set exactly')
        for (kind, key), change in changes.items():
            value = entities[kind].get(key)
            if not value:
                continue
            if kind == 'page':
                locator = value['compatibility'].get('source_locator', '')
                try:
                    expected_revision = _pointer(old, locator)['revision'] if locator else None
                except (KeyError, TypeError):
                    expected_revision = None
            else:
                expected_revision = old.get(kind + 's', {}).get(key, {}).get('revision')
            if change['proposed_revision'] != value['revision'] or change['expected_revision'] != expected_revision:
                self.error('revision', key, 'change revision does not match entity/current predecessor')
            if kind != 'action' and value['previous_revision'] != expected_revision:
                self.error('revision', key, 'entity predecessor differs from independently supplied baseline')
            lanes = {'fact': {'deterministic-import', 'semantic-promotion'}, 'page': {'page-projection'},
                     'action': {'state-import'}, 'narrative': {'narrative-edit'}}
            if change['validation'] not in lanes[kind]:
                self.error('ownership', key, 'entity submitted through another owner lane')
            if kind == 'fact' and change['validation'] == 'deterministic-import' and key not in old['facts']:
                self.error('reconciliation', key, 'new identity cannot bypass semantic promotion through the import lane')
        promotions = {c['id'] for c in changes.values() if c['validation'] == 'semantic-promotion'}
        checks = self.unique(manifest['fact_checks'], 'fact_key', 'reconciliation')
        if set(checks) != promotions:
            self.error('reconciliation', 'fact_checks', 'exactly one reconciliation check per semantic promotion required')
        for key in promotions:
            self.error('reconciliation', key, 'semantic promotion requires existing runtime fact_checks receipt validation; offline import cannot admit it')
        old_revisions = []
        for kind, values in (('fact', old['facts']), ('page', old.get('pages', {}))):
            for key, value in values.items():
                locator = '/pages/' + key.replace('~', '~0').replace('/', '~1')
                entity_id = key if kind == 'fact' else next((
                    p['page_id'] for p in entities['page'].values()
                    if p['compatibility'].get('source_locator') == locator), None)
                current = entities[kind].get(entity_id)
                if current:
                    old_revisions.append((kind, entity_id, value['revision'], current['revision']))
        for key, versions in history.items():
            for i, value in enumerate(versions):
                if value['revision'] != pages.fact_revision(value):
                    self.error('revision', key, 'legacy historical role-aware hash differs')
                if i and value['previous_revision'] != versions[i - 1]['revision']:
                    self.error('preservation', key, 'historical predecessor chain broken')
                if value['revision'] != old['facts'][key]['revision']:
                    old_revisions.append(('fact', key, value['revision'], value['revision']))
        mapped = [(r['entity'], r['id'], r['old_revision'], r['new_revision']) for r in manifest['revision_map']]
        if Counter(mapped) != Counter(old_revisions):
            self.error('revision', 'revision_map', 'imported current/historical revision mappings differ')
        expected_receipts = {(key, value['accepted_event']['operation_receipt_id']) for key, value in state['actions'].items()}
        if {(r['action_id'], r['operation_receipt_id']) for r in manifest['inputs']['state_receipts']} != expected_receipts:
            self.error('action', 'inputs/state_receipts', 'owning action receipt set differs')

    def coverage(self, census, manifest, outputs):
        units = self.unique(census['units'], 'unit_id', 'coverage')
        items = self.unique(manifest['coverage']['items'], 'unit_id', 'coverage')
        if set(units) != set(items):
            self.error('coverage', 'coverage', 'worker must cover every census unit exactly once')
        adoptions = self.unique(census['adoption_units'], 'unit_id', 'adoption')
        if set(adoptions) != set(self.controller.scope['adoption_unit_ids']):
            self.error('adoption', 'adoption_units', 'adoption set differs from controller scope')
        if set(adoptions) != {key for key, unit in units.items() if unit['operation'] == 'adopt'}:
            self.error('adoption', 'adoption_units', 'adoption records and adopt census units must be one-to-one')
        self.valid_adoptions = set()
        source_fields = {}
        source_spans = {}
        for unit_id, unit in units.items():
            item = items.get(unit_id)
            if not item:
                continue
            if (any(item[k] != unit[k] for k in ('source_id', 'locator', 'unit', 'sha256', 'destination'))
                    or item['disposition'] not in unit['allowed_dispositions']):
                self.error('coverage', unit_id, 'identity/disposition/destination differs from trusted census')
                continue
            if unit.get('delivery') == 'startup+compact' and item['disposition'] == 'retained-evidence':
                self.error('coverage', unit_id, 'mandatory semantic content cannot become cold evidence')
            if unit['unit'] == 'claim' and item['disposition'] == 'retained-evidence' and unit.get('delivery') != 'evidence':
                self.error('coverage', unit_id, 'semantic content cannot disappear through evidence retention')
            if unit['operation'] == 'adopt':
                adoption = adoptions.get(unit_id, {})
                if any(adoption.get(k) != unit[k] for k in ('source_id', 'locator', 'operation')) or adoption.get('expected_sha256') != unit['sha256']:
                    self.error('adoption', unit_id, 'adoption not bound to exact source span')
                self.unresolved.append(Issue('adoption-review', unit_id, 'Runtime permission/review acceptance remains required for the pinned source/output hashes.'))
            raw = self.controller.sources.get(unit['source_id'])
            try:
                if raw is None:
                    raise ValueError('census source bytes missing')
                if unit['unit'] == 'bytes' and not unit['locator'].startswith('bytes:'):
                    raise ValueError('byte units require an exact byte range')
                if unit['locator'].startswith('bytes:'):
                    value = _range(raw, unit['locator'])
                    start, end = map(int, unit['locator'].split(':')[1:])
                    if unit['unit'] == 'bytes':
                        source_spans.setdefault(unit['source_id'], []).append((start, end))
                        hashed = value
                    else:
                        value = value.decode()
                        hashed = canonical_json(value)
                else:
                    value = _pointer(_json(raw), unit['locator'])
                    hashed = canonical_json(value)
                    if unit['unit'] == 'field':
                        source_fields.setdefault(unit['source_id'], set()).add(unit['locator'])
                if sha256(hashed) != unit['sha256']:
                    raise ValueError('source unit hash differs from extracted bytes/value')
                destination = self.destination(unit['destination'], manifest, outputs)
                if unit['unit'] == 'bytes':
                    dest_bytes = (destination.encode() if isinstance(destination, str) else
                                  destination if isinstance(destination, bytes) else canonical_json(destination))
                    exact_range = any(x in unit['destination'] for x in ('#bytes:', '#span:')) and unit['operation'] != 'adopt'
                    if (value != dest_bytes) if exact_range else (value not in dest_bytes):
                        raise ValueError('original byte span lost at destination')
                else:
                    if isinstance(destination, bytes):
                        destination = destination.decode()
                    if not _same_json(destination, value):
                        raise ValueError('source value/claim changed or lost at destination')
                if unit['operation'] == 'adopt':
                    adoption = adoptions.get(unit_id, {})
                    if (any(adoption.get(k) != unit[k] for k in ('source_id', 'locator', 'operation'))
                            or adoption.get('expected_sha256') != unit['sha256']):
                        continue
                    path, first, last = self.output_reference(adoption['replacement_region'], outputs)
                    if sha256(_structural_bytes(outputs[path], first, last)) != adoption.get('output_sha256'):
                        self.error('adoption', unit_id, 'generated structural bytes differ from controller pin')
                    else:
                        self.valid_adoptions.add(unit_id)
            except (KeyError, IndexError, TypeError, ValueError, UnicodeError) as error:
                self.error('preservation', unit_id, str(error))
        for source_id, locators in source_fields.items():
            expected = set(_leaves(_json(self.controller.sources[source_id])))
            if expected != locators:
                self.error('coverage', source_id, 'JSON leaves, including empty containers, not fully enumerated')
        for source in census['sources']:
            source_id = source['source_id']
            try:
                value = _json(self.controller.sources[source_id])
            except ValueError:
                continue
            if isinstance(value, (dict, list)) and source_id not in source_fields:
                self.error('coverage', source_id, 'structured source has no explicit leaf mappings')
        for source_id, spans in source_spans.items():
            end = 0
            for start, stop in sorted(spans):
                if start > end:
                    self.error('coverage', source_id, 'unaccounted source byte gap')
                end = max(end, stop)
            if end != len(self.controller.sources[source_id]):
                self.error('coverage', source_id, 'unaccounted trailing source bytes')
        for source_id in {s['source_id'] for s in census['sources']}:
            if source_id not in source_spans:
                self.error('coverage', source_id, 'required corpus source lacks total byte accounting')
        counts = Counter(x['disposition'].replace('-', '_') for x in items.values())
        totals = {'input': len(units), **{k: counts[k] for k in ('mapped', 'retained_evidence', 'unresolved', 'discarded')}}
        self.totals.update(totals)
        if not _same_json(manifest['coverage']['totals'], totals) or manifest['losses']:
            self.error('coverage', 'totals', 'totals/lossless adoption mismatch')

    def output_coverage(self, census, outputs):
        """Account for output bytes too, including inserted text and EOF whitespace."""
        spans = {path: [] for path in outputs if path.endswith('.md')}

        def enroll(reference, code):
            try:
                path, start, end = self.output_reference(reference, outputs)
                if path not in spans:
                    raise ValueError('adoption requires a Markdown output')
                spans[path].append((start, end))
            except (ValueError, UnicodeError) as error:
                self.error(code, reference, str(error))

        for unit in census['units']:
            if (unit['owner'] == 'unmanaged' and unit['operation'] == 'preserve'
                    and unit['unit'] == 'bytes'):
                enroll(unit['destination'], 'preservation')
        for adoption in census['adoption_units']:
            if adoption['unit_id'] in self.valid_adoptions:
                enroll(adoption['replacement_region'], 'adoption')
        for path, raw in outputs.items():
            if path not in spans:
                continue
            for opening, closing in (MARKERS[:2], MARKERS[2:]):
                first, last = raw.find(opening.encode()), raw.find(closing.encode())
                if 0 <= first < last:
                    spans[path].append((first, last + len(closing.encode())))
            cursor = 0
            for start, end in sorted(spans[path]):
                if start > cursor:
                    self.error('preservation', path,
                               f'unassigned output bytes:{cursor}:{start} outside permitted regions')
                cursor = max(cursor, end)
            if cursor < len(raw):
                self.error('preservation', path,
                           f'unassigned output bytes:{cursor}:{len(raw)} including EOF')

    def output_reference(self, reference, outputs):
        path, locator = _output_location(reference, outputs)
        start, end = _output_span(outputs[path], locator)
        return path, start, end

    def destination(self, destination, manifest, outputs):
        name, locator = destination.split('#', 1)
        if name == 'manifest.json':
            if not self.allow_fixture:
                raise ValueError('fixture retention requires explicit example mode')
            return _pointer(manifest, locator)
        path, locator = _output_location(destination, outputs)
        raw = outputs[path]
        if locator.startswith('/') or not locator:
            return _pointer(_json(raw), locator)
        if locator.startswith(('bytes:', 'span:')):
            start, end = _output_span(raw, locator)
            return raw[start:end]
        # Adopted insertions are proposals; the source bytes are retained separately.
        if locator == 'generated-prefix':
            return raw
        raise ValueError('destination needs exact JSON pointer or byte range')

    def evidence(self, versions, owning, registry, manifest, state):
        ledger = {}
        for version in versions:
            ref = {k: version[k] for k in ('archive_id', 'version', 'content_sha256', 'source_id')}
            key = (ref['archive_id'], ref['version'])
            if key in ledger:
                self.error('evidence', ref['source_id'], 'duplicate immutable archive version')
            ledger[key] = version
            raw = self.controller.sources.get(ref['source_id'])
            if raw is None or sha256(raw) != ref['content_sha256'] or raw != version['content'].encode():
                self.error('evidence', ref['source_id'], 'exact archive-version bytes/hash missing or mismatched')
        refs = list(manifest['inputs']['evidence_refs'])
        for values in (registry['facts'], registry['notes'], registry['narratives']):
            for key, value in values.items():
                refs.extend(value['evidence_refs'])
                if 'evidence_resolution' in value:
                    exact = value['evidence_resolution'] == 'exact'
                    if exact != bool(value['evidence_refs']):
                        self.error('evidence', key, 'exact/unresolved evidence shape differs')
                    if not exact:
                        self.unresolved.append(Issue('legacy-evidence', key, 'Original identity retained; exact historical evidence is unresolved.'))
        for note_id, note in registry['notes'].items():
            if note['evidence_resolution'] == 'exact' and not any(
                    r['content_sha256'] == note_id and r['archive_id'] == note['ark_id']
                    for r in note['evidence_refs']):
                self.error('evidence', note_id, 'original note identity cannot be rebound to a newer archive head')
        refs.extend(a['accepted_event']['evidence_ref'] for a in registry['actions'].values())
        if {canonical_json(r) for r in manifest['inputs']['evidence_refs']} != {
                canonical_json({k: v[k] for k in ('archive_id', 'version', 'content_sha256', 'source_id')}) for v in versions}:
            self.error('evidence', 'inputs/evidence_refs', 'full pinned archive version set differs')
        for ref in refs:
            version = ledger.get((ref['archive_id'], ref['version']))
            if version is None or not _same_json({k: version[k] for k in ref}, ref):
                self.error('evidence', ref['source_id'], 'EvidenceRef must match all four immutable fields')
                continue
            if version.get('visibility') == 'hidden':
                operation = self.controller.evidence_reads.get(ref['source_id'])
                matches = [r for r in owning if r.get('operation_receipt_id') == operation
                           and _same_json(r.get('evidence_ref'), ref) and r.get('outcome') == 'accepted']
                if len(matches) != 1:
                    self.error('hidden-receipt', ref['source_id'], 'hidden reference lacks an exact owning receipt binding')
                else:
                    origin = matches[0].get('origin', {})
                    binding = state['trusted_bindings'].get(origin.get('client_id'))
                    authority = self.controller.baseline['generation']['authority_id']
                    if (not binding or origin.get('authority_id') != authority
                            or any(origin.get(k) != binding.get(k) or not origin.get(k)
                                   for k in ('authority_id', 'machine_tag'))):
                        self.error('hidden-receipt', ref['source_id'],
                                   'owning receipt origin differs from complete trusted binding/assignment')
        # Inspect the worker's transported receipt too; it cannot substitute for the caller's copy.
        if 'fixture' in manifest and not _same_json(manifest['fixture'].get('owning_receipts'), owning):
            self.error('hidden-receipt', 'fixture/owning_receipts', 'worker receipt differs from independently supplied exact binding')

    def projections(self, registry, manifest, outputs, targets, validator):
        actual_targets = {t for t in targets if t.endswith('.md')}
        if actual_targets != set(registry['pages']):
            self.error('projection', 'pages', 'output targets differ from registered family')
        for target, page in registry['pages'].items():
            if target not in targets:
                continue
            text = outputs[targets[target]['path']].decode('utf-8')
            match = re.match(r'\A---\n(.*?)\n---\n', text, re.S)
            if not match:
                self.error('projection', target, 'exact YAML frontmatter delimiters missing')
                continue
            try:
                front = yaml.load(match[1], Loader=_UniqueYaml)
            except (yaml.YAMLError, ValueError, TypeError) as error:
                self.error('projection', target, str(error))
                continue
            if not self.schema(validator.evolve(schema={'$ref': SCHEMA_URI + 'page#/$defs/frontmatter'}), front, target):
                continue
            expected = {k: page[k] for k in front if k in page}
            expected.update(wiki_contract=VERSION, target=target)
            if not _same_json(front, expected):
                self.error('projection', target, 'frontmatter differs from registry/generation')
            body = text[match.end():]
            body_tokens = _markdown().parse(body)
            if _has_raw_html(body_tokens, allow_comments=True):
                self.error('unsupported-html', target,
                           'raw HTML page context cannot establish visibility of required projections')
            if _heading_texts(body_tokens, 'h1') != [page['title']]:
                self.error('projection', target, 'exactly one matching H1 required')
            sequence = re.findall(r'<!--\s*/?(?:kb|narrative)\s*-->', body)
            has_narrative = any(n['page_id'] == page['page_id'] for n in registry['narratives'].values())
            required = list(MARKERS[:2]) + (list(MARKERS[2:]) if has_narrative else [])
            if sequence != required or any(body.count(marker) != int(marker in required) for marker in MARKERS):
                self.error('markers', target, 'duplicate/nested/reversed/missing region markers')
                continue
            if any(not re.search('^' + re.escape(marker) + '$', body, re.M) for marker in required):
                self.error('markers', target, 'marker must occupy its own line')
                continue
            kb_tokens = _region_tokens(body_tokens, body, *MARKERS[:2])
            kb_lines = _visible_lines(kb_tokens)
            kb = '\n'.join(kb_lines)
            if page['layout'] in HEADINGS and _heading_texts(kb_tokens, 'h2') != HEADINGS[page['layout']]:
                self.error('projection', target, 'managed section headings/order differ')
            facts = [f for f in registry['facts'].values() if f['target'] == target]
            found_facts = re.findall(r'\[f:([0-9a-f]{12})\]', kb)
            if Counter(found_facts) != Counter(f['id'] for f in facts):
                self.error('projection', target, 'managed facts omitted, duplicated or invented')
            for fact in facts:
                marker = '[f:' + fact['id'] + ']'
                lines = [line for line in kb_lines if marker in line]
                projections = [_inline_signature(token.children, before=marker) for token in kb_tokens
                               if token.type == 'inline' and any(
                                   child.type == 'text' and marker in child.content for child in token.children or [])]
                if projections != [_expected_signature(fact['text'])]:
                    self.error('projection', fact['fact_key'], 'full factual text/conditions/exceptions not rendered')
                unresolved_label = '[unresolved legacy state; retained status: ' + fact['status'] + ']'
                if fact['evidence_resolution'] == 'unresolved' and (not lines or unresolved_label not in lines[0]):
                    self.error('projection', fact['fact_key'], 'unresolved legacy binding must be visible')
            expected_actions = set()
            for key, action in registry['actions'].items():
                if ((action['subject_kind'] == 'feature' and action['subject_id'] == page['page_id'])
                        or (page['layout'] == 'project-map' and action.get('repository_id') in page['repositories'])
                        or self._context_page(action) == page['page_id']):
                    expected_actions.add(key)
            found_actions = re.findall(r'\[a:([a-z0-9._-]+)\]', kb)
            if Counter(found_actions) != Counter(expected_actions):
                self.error('projection', target, 'action panels omit/duplicate/invent projected action IDs')
            projections = _action_projections(kb_tokens, lambda token: self.link_ids(target, [token]), page['layout'])
            for key in expected_actions:
                action = registry['actions'][key]
                fields = [(form, values) for identity, form, values in projections if identity == key]
                form = 'table' if page['layout'] in ('feature', 'project-map') else 'general'
                if fields != [(form, _expected_action_fields(action, page['layout'], form))]:
                    self.error('projection', key, 'displayed action identity/destination/evidence fields differ')
            narrative_tokens = []
            if has_narrative:
                narrative_tokens = _region_tokens(body_tokens, body, *MARKERS[2:])
                narrative_lines = _visible_lines(narrative_tokens)
                if _heading_texts(narrative_tokens, 'h2') != ['Explanation']:
                    self.error('projection', target, 'narrative explanation heading missing')
                for key, narrative in registry['narratives'].items():
                    if narrative['page_id'] == page['page_id']:
                        labels = [line for line in narrative_lines if re.search(
                            r'\bNarrative: ' + re.escape(key) + r'\.', line)]
                        rendered = [_inline_signature(token.children or []) for token in narrative_tokens
                                    if token.type == 'inline']
                        if (_expected_signature(narrative['text']) not in rendered or
                                labels != [f'Narrative: {key}. State: {narrative["state"]}.']):
                            self.error('projection', key, 'narrative full text/state differs')
            if self.link_ids(target, kb_tokens + narrative_tokens) != set(page['links']):
                self.error('link', target, 'visible links differ from registered links')

    def _context_page(self, action):
        state = _json(self.controller.sources[self.controller.state_source_id])
        return state['subject_bindings'].get(action['subject_id'], {}).get('context_page_id')

    def link_ids(self, target, tokens):
        entries, redirects = self.reference_pages, self.reference_redirects
        by_id = {page['page_id']: path for path, page in entries.items()}
        destinations = []
        for token in tokens:
            for child in token.children or []:
                if child.type == 'link_open':
                    href = child.attrGet('href')
                    if not urlsplit(href).scheme and not href.startswith('#'):
                        destinations.append(unquote(href.split('#')[0]))
        # The shared inline rule sees the original source before escape decoding.
        literal = [child for token in tokens for child in token.children or [] if child.type == 'wikilink']
        for child in literal:
            raw = child.content
            if child.meta['invalid']:
                self.error('link', target, 'invalid literal link: ' + raw)
                continue
            name = raw.removeprefix('wiki/')
            name = name if name.endswith('.md') else name + '.md'
            local = posixpath.normpath(posixpath.join(posixpath.dirname(target), name))
            candidates = ([name] if name in entries else [local] if local in entries
                          else [t for t in entries if t.endswith('/' + name)])
            if not candidates and name in redirects:
                candidates = [by_id[redirects[name]]]
            if not candidates:
                candidates = [target for target, entry in entries.items()
                              if raw in entry['aliases']]
            if len(candidates) != 1:
                self.error('link', target, 'literal link unresolved/ambiguous: ' + raw)
            else:
                destinations.append('/' + candidates[0])
        resolved = set()
        for name in destinations:
            dest = name[1:] if name.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(target), name))
            if dest in redirects:
                dest = by_id[redirects[dest]]
            if dest not in entries:
                self.error('link', target, 'link unresolved: ' + name)
            else:
                resolved.add(entries[dest]['page_id'])
        return resolved


def validate_family(root: Path, manifest_path: str, *, controller: ControllerInput,
                    allow_fixture: bool = False) -> ValidationResult:
    """Validate supplied snapshots without mutation. A pass is never runtime acceptance.

    ``root`` is an explicit family transport directory; only assigned output files,
    the manifest and pinned contract are read. Sources are supplied as bytes by the
    independent caller. No discovery, home-directory fallback, DB or network I/O.
    """
    check = _Check(Path(root), controller, allow_fixture)
    try:
        check.run(manifest_path)
    except RecursionError:
        check.error('input', manifest_path, 'structured input exceeds supported nesting depth')
    except OSError as error:
        check.error('path', manifest_path, str(error))
    except (ValueError, UnicodeError, KeyError, IndexError, TypeError) as error:
        code = 'path' if isinstance(error, ValueError) and any(
            word in str(error) for word in ('path', 'symlink', 'file missing')) else 'input'
        check.error(code, manifest_path, str(error))
    return check.result()
