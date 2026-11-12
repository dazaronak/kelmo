"""Parse explicit wiki relationships using markdown-it source maps."""

import copy
import hashlib
import posixpath
import re
import threading
import unicodedata
from collections import OrderedDict
from pathlib import PurePosixPath

import yaml
from markdown_it import MarkdownIt


def slugify(text: str) -> str:
    """A convenience lookup key, never a page identity."""
    text = unicodedata.normalize('NFKC', text).strip().casefold()
    text = re.sub(r'[\s_]+', '-', text)
    text = ''.join(c for c in text if c.isalnum() or c in '-/:')
    return re.sub(r'-{2,}', '-', text).strip('-')


def _relative_target(text: str) -> bool:
    return text.startswith(('./', '../')) or '..' in PurePosixPath(text).parts


def target_key(text: str, *, allow_parent: bool = False) -> str:
    """Remove wiki syntax while preserving path case, punctuation and Unicode."""
    text = unicodedata.normalize('NFC', text).strip()
    if text.startswith('[[') and text.endswith(']]'):
        text = text[2:-2]
    text = text.split('|', 1)[0].split('#', 1)[0].strip()
    text = text.removesuffix('.md')
    path = PurePosixPath(text)
    if not text or path.is_absolute():
        return ''
    if '..' in path.parts and not allow_parent:
        return ''
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return ''
    normalized = posixpath.normpath(text)
    return '' if normalized in ('.', '..') else normalized


def lookup_keys(entity_id: str, aliases=()) -> set[str]:
    """All convenience keys; callers must retain every matching identity."""
    parts = entity_id.split('/')
    values = ['/'.join(parts[index:]) for index in range(len(parts))]
    values.extend(aliases)
    return {key for value in values if (key := slugify(value))}


def _wikilink_rule(state, silent: bool) -> bool:
    start = state.pos
    if not state.src.startswith('[[', start):
        return False
    if start and state.src[start - 1] == '!':
        cursor = start - 2
        while cursor >= 0 and state.src[cursor] == '\\':
            cursor -= 1
        if (start - 2 - cursor) % 2 == 0:
            return False
    end = state.src.find(']]', start + 2)
    if end < 0 or end + 2 > state.posMax:
        return False
    raw = state.src[start + 2:end].split('|', 1)[0].split('#', 1)[0].strip()
    if not raw:
        return False
    target = target_key(raw, allow_parent=True)
    if not silent:
        token = state.push('wikilink', '', 0)
        token.content = raw
        token.meta = {
            'target': target or unicodedata.normalize('NFC', raw),
            'line': state.env['wiki_block_line'] + state.src.count('\n', 0, start),
            'relative': _relative_target(raw),
            'invalid': not bool(target),
        }
    state.pos = end + 2
    return True


def _inline_with_source_map(state):
    for token in state.tokens:
        if token.type == 'inline':
            state.env['wiki_block_line'] = token.map[0] + 1 if token.map else 1
            if token.children is None:
                token.children = []
            state.md.inline.parse(token.content, state.md, state.env, token.children)


def _markdown_tokens(content: str):
    markdown = MarkdownIt('commonmark')
    markdown.inline.ruler.before('link', 'wikilink', _wikilink_rule)
    markdown.core.ruler.at('inline', _inline_with_source_map)
    return markdown.parse(content)


def extract_wikilinks(content: str) -> list[str]:
    return list(dict.fromkeys(
        child.meta['target']
        for token in _markdown_tokens(content)
        for child in token.children or []
        if child.type == 'wikilink' and not child.meta['invalid']
    ))


def _frontmatter_span(raw: str):
    """One span controls YAML decoding, metadata marks and body line offsets."""
    raw = raw.removeprefix('\ufeff')
    lines = raw.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None or not re.fullmatch(r'-{3,}\s*', lines[start].rstrip('\r\n')):
        return {}, {}, raw, 0
    end = next((
        i for i in range(start + 1, len(lines))
        if re.fullmatch(r'(?:-{3,}|\.\.\.)\s*', lines[i].rstrip('\r\n'))
    ), None)
    if end is None:
        raise ValueError('Unterminated YAML frontmatter')
    loader = yaml.SafeLoader(''.join(lines[start + 1:end]))
    try:
        node = loader.get_single_node()
        if node is None:
            metadata, marks = {}, {}
        else:
            if not isinstance(node, yaml.MappingNode):
                raise ValueError('Frontmatter must be a mapping')
            seen = set()
            for key, _ in node.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag not in {
                    'tag:yaml.org,2002:str', 'tag:yaml.org,2002:merge',
                }:
                    raise ValueError('Frontmatter keys must be strings')
                if key.value in seen:
                    raise ValueError('Duplicate frontmatter key')
                seen.add(key.value)
            loader.flatten_mapping(node)
            metadata = loader.construct_document(node)
            marks = {key.value: start + 2 + key.start_mark.line for key, _ in node.value}
    finally:
        loader.dispose()
    return metadata, marks, ''.join(lines[end + 1:]), end + 1


def _empty_result() -> dict:
    return {
        'tags': [], 'aliases': [], 'wikilinks': [], 'links': [],
        'title': None, 'parent': None, 'parent_relative': False,
        'parent_invalid': False, 'status': None, 'line_number': None,
        'metadata_lines': {}, 'error': None,
    }


def _label(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Metadata strings must not contain control characters')
    return unicodedata.normalize('NFC', value).strip()


# The parse of each page content, for the graph scan and the link audit that read the same pages one after
# the other (a doctor run, a publication). A wiki of a few thousand pages fits; older contents fall out.
_PARSED = OrderedDict()
_PARSED_LIMIT = 4096
_PARSED_LOCK = threading.Lock()


def drop_parsed():
    """Forgets the kept parses (a core over its memory budget)."""
    with _PARSED_LOCK:
        _PARSED.clear()


def parse_text(raw: str | bytes) -> dict:
    """Parse supplied content without reopening a file or replacing bad UTF-8. The same content is parsed once
    per process while it stays among the last _PARSED_LIMIT; every caller gets its own copy."""
    try:
        data = raw.encode('utf-8') if isinstance(raw, str) else raw
    except UnicodeError:
        data = None
    if not isinstance(data, bytes):
        return _parse_text(raw)
    key = hashlib.sha256(data).digest()
    with _PARSED_LOCK:
        known = _PARSED.get(key)
        if known is not None:
            _PARSED.move_to_end(key)
    if known is None:
        known = _parse_text(raw)
        with _PARSED_LOCK:
            _PARSED[key] = known
            while len(_PARSED) > _PARSED_LIMIT:
                _PARSED.popitem(last=False)
    return copy.deepcopy(known)


def _parse_text(raw):
    result = _empty_result()
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str):
            raise TypeError('Markdown content must be text or bytes')
        metadata, marks, body, offset = _frontmatter_span(raw)
        result['metadata_lines'] = marks
        for field in ('tags', 'aliases'):
            value = metadata.get(field, [])
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f'{field} must be a string or list of strings')
            result[field] = list(dict.fromkeys(
                label for item in value if (label := _label(item))
            ))
        for field in ('title', 'parent', 'status'):
            value = metadata.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f'{field} must be a string')
            result[field] = _label(value) if value is not None else None
        if result['parent']:
            parent = result['parent']
            if parent.startswith('[[') and parent.endswith(']]'):
                parent = parent[2:-2]
            result['parent_relative'] = _relative_target(parent)
            key = target_key(parent, allow_parent=True)
            result['parent_invalid'] = not bool(key)
            result['parent'] = key or parent
        else:
            result['parent'] = None
        seen = set()
        tokens = _markdown_tokens(body)
        for index, token in enumerate(tokens):
            if token.type == 'heading_open' and token.tag == 'h1' and result['line_number'] is None:
                result['line_number'] = token.map[0] + 1 + offset
                if not result['title']:
                    result['title'] = tokens[index + 1].content
            for child in token.children or []:
                if child.type != 'wikilink':
                    continue
                link = dict(child.meta)
                identity = (link['target'], link['relative'], link['invalid'])
                if identity not in seen:
                    seen.add(identity)
                    link['line'] += offset
                    result['links'].append(link)
        result['wikilinks'] = [link['target'] for link in result['links'] if not link['invalid']]
        if result['line_number'] is None:
            result['line_number'] = marks.get('title', 1)
    except yaml.YAMLError:
        result['error'] = 'Malformed YAML frontmatter'
    except UnicodeError:
        result['error'] = 'Invalid UTF-8'
    except (ValueError, TypeError) as error:
        result['error'] = str(error)
    return result


def parse_file(path: str) -> dict:
    """Compatibility entry point; parse_text also accepts these exact bytes."""
    try:
        with open(path, 'rb') as source:
            return parse_text(source.read())
    except (OSError, ValueError, TypeError):
        result = _empty_result()
        result['error'] = 'Cannot read Markdown file'
        return result
