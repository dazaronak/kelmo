"""Helpers shared by the state core tests: frames, replies, profile files and journal reads."""
import json

from test_shared_context import profile_value

from kelmo.server import PROTOCOL


def request(op, args=None, key=None, **extra):
    value = {'protocol': PROTOCOL, 'request_id': 'r1', 'op': op, 'args': args or {}}
    if key is not None:
        value['idempotency_key'] = key
    value.update(extra)
    return value


def capture(text, filename, key):
    return request('capture', {'content': text, 'filename': filename, 'role': 'collector'}, key=key)


def succeed(reply):
    assert reply['ok'] is True, reply
    return reply['result']


def operation_of(reply):
    return succeed(reply)['operation']['id']


def profile_path(shared, value=None):
    """Writes a server profile file for the shared vault; `value` replaces the default profile."""
    path = shared.tmp_path / 'profile.json'
    profile = profile_value(shared.world, shared.tmp_path) if value is None else value
    path.write_text(json.dumps(profile))
    return path


def count_operations(core):
    return core.storage.read(
        lambda reader: reader.journal.conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0])


def writes_on_reader(error):
    """Whether this error, or an error it was raised from, says a read-only journal was written."""
    current = error
    while current is not None:
        if 'readonly' in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False
