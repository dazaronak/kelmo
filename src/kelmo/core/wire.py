"""The kelmo/2 frame: one length-prefixed header and body, the same on every seam of the daemon.

    u32 BE  total length = 2 + header length + body length, at most MAX_FRAME
    u16 BE  header length
    header  UTF-8 JSON object
    body    UTF-8 JSON value (body_type "json")

Every frame is written in canonical JSON (sorted keys, compact separators, non-ASCII kept, no NaN), so the
Python and Rust codecs produce the same bytes for the same values. Decoding refuses a malformed frame with
E_PROTOCOL and a frame over the bound with E_LIMIT; it never guesses.
"""
import json
import re
import struct

from ..errors import KelmoError

VERSION = 2
MAX_FRAME = 32 * 1048576
MAX_HEADER = 65535
KINDS = frozenset({'call', 'reply', 'cancel', 'event', 'hello'})
BODY_TYPES = frozenset({'json'})
ID = re.compile(r'[0-9a-f]{32}')
OP = re.compile(r'[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*')
LANE = re.compile(r'[a-z][a-z0-9_]{0,31}')
ERROR_CODE = re.compile(r'E_[A-Z0-9_]{1,40}')
ORIGIN_FIELDS = frozenset({'client_id', 'machine_tag', 'session'})

# Codes the daemon adds; each is retryable by the caller's own rule.
DAEMON_RETRYABLE = frozenset({'E_OVERLOADED', 'E_DEADLINE', 'E_CORE_RESTARTED', 'E_UNAVAILABLE'})
DAEMON_CODES = DAEMON_RETRYABLE | {'E_CANCELLED', 'E_FORBIDDEN'}

# Header fields: name -> (kinds the field may appear on, kinds that require it).
_FIELDS = {
    'v': (KINDS, KINDS),
    'kind': (KINDS, KINDS),
    'id': (KINDS, KINDS),
    'op': ({'call', 'event'}, {'call', 'event'}),
    'lane': ({'call'}, set()),
    'deadline_ms': ({'call'}, {'call'}),
    # On a hello, the identity a local connection speaks for (kelmod attach fixes it from its own
    # arguments); a call from a connection never carries one, the gateway attaches it.
    'origin': ({'call', 'event', 'hello'}, set()),
    'key': ({'call'}, set()),
    'ok': ({'reply'}, {'reply'}),
    'error': ({'reply'}, set()),
    'body_type': (KINDS, KINDS),
    'protocols': ({'hello'}, {'hello'}),
    'operations': ({'hello'}, {'hello'}),
}

_PREFIX = struct.Struct('>IH')


INT_MIN, INT_MAX = -(2 ** 63), 2 ** 64 - 1


def _check_ints(value):
    """Integers must fit i64 or u64: the Rust codec holds no wider integer, so a wider one would come back as a
    float and change the bytes."""
    if type(value) is int:
        if not INT_MIN <= value <= INT_MAX:
            raise KelmoError('E_PROTOCOL', 'Integer is outside the 64-bit range.')
    elif isinstance(value, dict):
        for item in value.values():
            _check_ints(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_ints(item)


def canonical(value):
    """Canonical JSON text of a value; refused if it holds anything JSON cannot carry exactly."""
    _check_ints(value)
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        text.encode('utf-8')
    except (TypeError, ValueError) as error:
        raise KelmoError('E_PROTOCOL', f'Value is not canonical JSON: {error}') from error
    return text


def encode(header, body=None, *, line_end=False):
    """Bytes of one frame. The header is validated before anything is written.

    `line_end` ends the body with a newline, inside the frame: a peer that reads lines (kelmo-client/1) then
    has one finished, invalid line and refuses it at once, instead of waiting for a line that never ends."""
    validate_header(header)
    head = canonical(header).encode('utf-8')
    data = canonical(body).encode('utf-8') + (b'\n' if line_end else b'')
    if len(head) > MAX_HEADER:
        raise KelmoError('E_LIMIT', 'Frame header exceeds 65535 bytes.')
    total = 2 + len(head) + len(data)
    if total > MAX_FRAME:
        raise KelmoError('E_LIMIT', 'Frame exceeds the bounded frame size.')
    return _PREFIX.pack(total, len(head)) + head + data


def decode(frame):
    """(header, body) of one complete frame, prefix included."""
    if len(frame) < 4:
        raise KelmoError('E_PROTOCOL', 'Frame is shorter than its length prefix.')
    total = struct.unpack_from('>I', frame)[0]
    if total > MAX_FRAME:
        raise KelmoError('E_LIMIT', 'Frame exceeds the bounded frame size.')
    if len(frame) != 4 + total:
        raise KelmoError('E_PROTOCOL', 'Frame length does not match its prefix.')
    return _decode_payload(frame[4:])


def _decode_payload(payload):
    if len(payload) < 2:
        raise KelmoError('E_PROTOCOL', 'Frame has no header length.')
    head_length = struct.unpack_from('>H', payload)[0]
    if head_length == 0 or 2 + head_length > len(payload):
        raise KelmoError('E_PROTOCOL', 'Frame header length is outside the frame.')
    header = _json(payload[2:2 + head_length], 'header')
    if not isinstance(header, dict):
        raise KelmoError('E_PROTOCOL', 'Frame header is not a JSON object.')
    validate_header(header)
    body = _json(payload[2 + head_length:], 'body')
    return header, body


def _reject_constant(name):
    raise ValueError(f'{name} is not JSON')


def _finite(text):
    value = float(text)
    if value in (float('inf'), float('-inf')):
        raise ValueError('number out of range')
    return value


def _json(data, part):
    try:
        value = json.loads(data.decode('utf-8'), parse_constant=_reject_constant, parse_float=_finite)
        canonical(value)  # lone surrogates and out-of-range integers are refused as the Rust codec refuses them
        return value
    except KelmoError as error:
        raise KelmoError('E_PROTOCOL', f'Frame {part} is not UTF-8 JSON.') from error
    except (UnicodeError, ValueError) as error:
        raise KelmoError('E_PROTOCOL', f'Frame {part} is not UTF-8 JSON.') from error


def _protocol(message):
    raise KelmoError('E_PROTOCOL', message)


def validate_header(header):
    if not isinstance(header, dict):
        _protocol('Frame header is not an object.')
    kind = header.get('kind')
    if kind not in KINDS:
        _protocol('Frame kind is unknown.')
    for name in header:
        allowed = _FIELDS.get(name)
        if allowed is None or kind not in allowed[0]:
            _protocol(f'Header field {name!r} is not part of a {kind} frame.')
    for name, (_, required) in _FIELDS.items():
        if kind in required and name not in header:
            _protocol(f'A {kind} frame requires header field {name!r}.')
    if header['v'] != VERSION or type(header['v']) is not int:
        _protocol('Frame version is not 2.')
    _match(ID, header['id'], 'id')
    if header['body_type'] not in BODY_TYPES:
        _protocol('Frame body type is unknown.')
    if 'op' in header:
        _match(OP, header['op'], 'op')
    if 'lane' in header:
        _match(LANE, header['lane'], 'lane')
    if 'deadline_ms' in header:
        value = header['deadline_ms']
        if type(value) is not int or not 0 <= value <= 86_400_000:
            _protocol('deadline_ms is not an integer between 0 and 86400000.')
    if 'origin' in header:
        _origin(header['origin'])
    if 'key' in header:
        key = header['key']
        if not isinstance(key, str) or not 1 <= len(key) <= 240 or not key.isprintable() or not key.isascii():
            _protocol('key is not 1 to 240 printable ASCII characters.')
    if kind == 'reply':
        if type(header['ok']) is not bool:
            _protocol('ok is not a boolean.')
        if header['ok'] == ('error' in header):
            _protocol('A reply carries an error exactly when ok is false.')
        if 'error' in header:
            _error(header['error'])
    if kind == 'hello':
        for name in ('protocols', 'operations'):
            value = header[name]
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                _protocol(f'{name} is not a list of names.')
    return header


def _match(pattern, value, name):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _protocol(f'{name} is malformed.')


def _origin(origin):
    if not isinstance(origin, dict) or set(origin) - ORIGIN_FIELDS or 'client_id' not in origin:
        _protocol('origin is not {client_id, machine_tag, session}.')
    for name, value in origin.items():
        if not (value is None and name != 'client_id') and not (isinstance(value, str) and 0 < len(value) <= 200):
            _protocol(f'origin.{name} is malformed.')


def _error(error):
    if not isinstance(error, dict) or not {'code', 'message', 'retryable'} <= set(error) \
            or set(error) - {'code', 'message', 'retryable', 'retry_after_ms'}:
        _protocol('error is not {code, message, retryable, retry_after_ms}.')
    _match(ERROR_CODE, error['code'], 'error.code')
    if not isinstance(error['message'], str) or type(error['retryable']) is not bool:
        _protocol('error message or retryable is malformed.')
    after = error.get('retry_after_ms')
    if after is not None and (type(after) is not int or after < 0):
        _protocol('error.retry_after_ms is not a non-negative integer.')


class FrameReader:
    """Incremental decoding of a byte stream into frames. Feed what arrives; take what is complete.

    A prefix that announces a frame over the bound is refused at once, before its bytes are buffered."""

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data):
        self._buffer += data
        frames = []
        while len(self._buffer) >= 4:
            total = struct.unpack_from('>I', self._buffer)[0]
            if total > MAX_FRAME:
                raise KelmoError('E_LIMIT', 'Frame exceeds the bounded frame size.')
            if len(self._buffer) < 4 + total:
                break
            payload = bytes(self._buffer[4:4 + total])
            del self._buffer[:4 + total]
            frames.append(_decode_payload(payload))
        return frames

    @property
    def pending(self):
        return len(self._buffer)


def reply(call_id, body=None, *, error=None):
    """A reply header for `call_id`: ok with a body, or an error {code, message, retryable[, retry_after_ms]}."""
    header = {'v': VERSION, 'kind': 'reply', 'id': call_id, 'ok': error is None, 'body_type': 'json'}
    if error is not None:
        header['error'] = error
    return header, body
