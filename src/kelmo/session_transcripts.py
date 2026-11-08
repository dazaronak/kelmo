"""Normalize provider transcript records while retaining explicit source coverage."""
import json
import time
from pathlib import Path

from .errors import KelmoError


def enrolled_transcript(path, roots, message):
    """The real transcript file a provider named, when it lies inside an enrolled location.

    Links are resolved before the check, so a location reached through one (a provider profile whose
    projects directory links to another store) is accepted, and a link that leads anywhere else is refused
    as outside. The caller still opens the returned file without following a final link."""
    source = Path(path).expanduser().absolute()
    if source.suffix != '.jsonl' or '..' in source.parts:
        raise KelmoError('E_TRANSCRIPT', message)
    real = source.resolve()
    if real.suffix != '.jsonl' or not any(real.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
        raise KelmoError('E_TRANSCRIPT', message)
    return real


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise KelmoError('E_CAPTURE_RETRY', 'Session capture deadline exceeded; retry capture.')


def _user_text(text):
    boundaries = (('# AGENTS.md instructions for ', '</INSTRUCTIONS>'),
                  ('<environment_context>', '</environment_context>'),
                  ('<recommended_plugins>', '</recommended_plugins>'))
    changed = True
    while changed:
        changed = False
        for opening, closing in boundaries:
            if text.startswith(opening) and closing in text:
                text = text.partition(closing)[2].lstrip()
                changed = True
    return text


def _content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(_content(item) for item in value)
    if isinstance(value, dict):
        return value.get('text', '') if isinstance(value.get('text', ''), str) else ''
    return ''


def _messages(provider, row, record):
    result = []

    def add(role, text, error=False):
        if role == 'user':
            text = _user_text(text)
        if text.strip():
            result.append({'record': record, 'role': role, 'text': text, **({'error': True} if error else {})})

    if provider == 'codex':
        if row.get('type') != 'response_item':
            return result
        body = row.get('payload')
        if not isinstance(body, dict):
            raise KelmoError('E_TRANSCRIPT', 'Invalid provider response record.')
        kind = body.get('type')
        if kind in ('function_call_output', 'custom_tool_call_output'):
            add('tool', _content(body.get('output')))
        elif kind in ('function_call', 'custom_tool_call'):
            add('tool_call', json.dumps({key: body.get(key) for key in ('name', 'arguments', 'input')},
                                        ensure_ascii=False))
        elif kind == 'message' and body.get('role') in ('user', 'assistant'):
            blocks = body.get('content')
            if isinstance(blocks, list):
                if not all(isinstance(block, dict) for block in blocks):
                    raise KelmoError('E_TRANSCRIPT', 'Invalid provider content block.')
                texts = [_content(block) for block in blocks]
                if body['role'] == 'user':
                    texts = [_user_text(text) for text in texts]
                add(body['role'], '\n'.join(text for text in texts if text.strip()))
            elif isinstance(blocks, str):
                add(body['role'], _content(blocks))
            else:
                raise KelmoError('E_TRANSCRIPT', 'Invalid provider message content.')
        return result
    if row.get('type') in ('response_item', 'event_msg', 'session_meta'):
        raise KelmoError('E_TRANSCRIPT', 'Transcript format does not match the selected provider.')
    if row.get('type') not in ('user', 'assistant'):
        return result
    message = row.get('message')
    if not isinstance(message, dict):
        raise KelmoError('E_TRANSCRIPT', 'Invalid provider message record.')
    role = row['type']
    blocks = message.get('content')
    if isinstance(blocks, str):
        add(role, blocks)
    elif isinstance(blocks, list):
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                raise KelmoError('E_TRANSCRIPT', 'Invalid provider content block.')
            if block.get('type') == 'tool_result':
                add('tool', _content(block.get('content')), block.get('is_error') is True)
            elif block.get('type') == 'tool_use':
                add('tool_call', json.dumps({'name': block.get('name'), 'input': block.get('input')},
                                            ensure_ascii=False))
            elif block.get('type') == 'text':
                texts.append(_content(block))
        add(role, '\n'.join(texts))
    else:
        raise KelmoError('E_TRANSCRIPT', 'Invalid provider message content.')
    return result


def _launch(provider, row):
    """True for a record that says the session was started headless (Claude Code `-p` or an SDK entry
    point, `codex exec`), False for one that names an interactive start, None for any other record."""
    if provider == 'codex':
        body = row.get('payload')
        if row.get('type') == 'session_meta' and isinstance(body, dict) and isinstance(body.get('originator'), str):
            return body['originator'] == 'codex_exec'
        return None
    entry = row.get('entrypoint')
    return entry.startswith('sdk') if isinstance(entry, str) else None


def parse(provider, raw, *, deadline=None):
    if provider not in ('claude', 'codex') or not isinstance(raw, bytes):
        raise KelmoError('E_INPUT', 'Select a supported transcript provider and byte input.')
    messages = []
    records = complete = offset = 0
    event_users = 0
    partial = False
    headless = None
    for line in raw.splitlines(keepends=True):
        _check_deadline(deadline)
        offset += len(line)
        if not line.strip():
            complete = offset
            continue
        try:
            row = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            if offset == len(raw) and not line.endswith(b'\n'):
                partial = True
                break
            raise KelmoError('E_TRANSCRIPT', 'Invalid complete transcript record; capture not accepted.') from error
        if not isinstance(row, dict):
            raise KelmoError('E_TRANSCRIPT', 'Transcript records must be objects.')
        records += 1
        complete = offset
        if headless is None:
            headless = _launch(provider, row)
        if provider == 'codex' and row.get('type') == 'event_msg':
            body = row.get('payload')
            if (isinstance(body, dict) and body.get('type') == 'user_message'
                    and _user_text(_content(body.get('message'))).strip()):
                event_users += 1
        messages.extend(_messages(provider, row, records))
    user_turns = sum(item['role'] == 'user' for item in messages)
    if event_users > user_turns:
        raise KelmoError('E_TRANSCRIPT', 'Codex user events lack matching supported message records.')
    # headless: the first record that names how the session started decides; none means interactive.
    return {'records': records, 'complete_bytes': complete, 'partial_tail': partial,
            'messages': messages, 'user_turns': user_turns, 'headless': bool(headless)}
