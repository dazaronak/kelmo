"""Write the kelmo/2 contract files that the Python and the Rust codec both read.

    uv run --locked python tools/protocol_export.py           # write
    uv run --locked python tools/protocol_export.py --check   # exit 1 if any file would change

Outputs:
  tests/contract/kelmo2/header.json       the header fields, the kinds that may and must carry each, the codes
  tests/contract/kelmo2/operations.json   every op: lanes, read or write, replay class, arguments
  tests/contract/kelmo2/tools.json        the 15 MCP tool definitions, as list_tools answers them
  tests/contract/frames/*.bin, manifest.json   golden frames, valid and refused
  tests/contract/identity.json            canonical JSON and identity_id of spaces and profiles
  docs/PROTOCOLS.md                       the protocol reference, generated from the same tables

A change of any of these files is a protocol change.
"""
import argparse
import asyncio
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from kelmo.core import operations, wire  # noqa: E402

CONTRACT = ROOT / 'tests' / 'contract'
CALL_ID = '0123456789abcdef0123456789abcdef'
OTHER_ID = 'fedcba9876543210fedcba9876543210'


def dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n'


def header_contract():
    fields = {name: {'kinds': sorted(allowed), 'required_on': sorted(required)}
              for name, (allowed, required) in wire._FIELDS.items()}
    return {'version': wire.VERSION, 'max_frame': wire.MAX_FRAME, 'max_header': wire.MAX_HEADER,
            'kinds': sorted(wire.KINDS), 'body_types': sorted(wire.BODY_TYPES), 'fields': fields,
            'patterns': {'id': wire.ID.pattern, 'op': wire.OP.pattern, 'lane': wire.LANE.pattern,
                         'error.code': wire.ERROR_CODE.pattern},
            'origin_fields': sorted(wire.ORIGIN_FIELDS),
            'daemon_codes': {'retryable': sorted(wire.DAEMON_RETRYABLE),
                             'final': sorted(wire.DAEMON_CODES - wire.DAEMON_RETRYABLE)},
            'limit_codes': {'malformed': 'E_PROTOCOL', 'over_bound': 'E_LIMIT'}}


def tools_contract():
    from kelmo.mcp import server
    tools = asyncio.run(server('/nonexistent').list_tools())
    return sorted((tool.to_mcp_tool().model_dump(mode='json', exclude_none=True) for tool in tools),
                  key=lambda tool: tool['name'])


def call(**extra):
    return {'v': 2, 'kind': 'call', 'id': CALL_ID, 'op': 'state.search', 'lane': 'read', 'deadline_ms': 5000,
            'body_type': 'json', **extra}


def raw(header_bytes, body_bytes, total=None, head_length=None):
    payload_length = 2 + len(header_bytes) + len(body_bytes)
    return (struct.pack('>IH', payload_length if total is None else total,
                        len(header_bytes) if head_length is None else head_length) + header_bytes + body_bytes)


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def frames():
    """(name, bytes, expectation). Expectation: {'header', 'body'} or {'code'}."""
    valid = [
        ('call', call(), {'query': 'basalt', 'limit': 5}),
        ('call_origin_key', call(op='state.capture', lane='write', key='client/a/request-1',
                                 origin={'client_id': 'mac-work', 'machine_tag': 'mac-work', 'session': None}),
         {'content': 'Ledger, кедр и łąka "quoted" \\ back\nslash   😀', 'filename': 'n.md'}),
        ('reply_ok', {'v': 2, 'kind': 'reply', 'id': CALL_ID, 'ok': True, 'body_type': 'json'},
         {'items': [{'score': 0.125, 'tiny': 1e-05, 'big': 1e+16, 'whole': 2.0}], 'count': -3}),
        ('reply_error', {'v': 2, 'kind': 'reply', 'id': CALL_ID, 'ok': False, 'body_type': 'json',
                         'error': {'code': 'E_OVERLOADED', 'message': 'Lane read is full.', 'retryable': True,
                                   'retry_after_ms': 250}}, None),
        ('cancel', {'v': 2, 'kind': 'cancel', 'id': CALL_ID, 'body_type': 'json'}, None),
        ('event', {'v': 2, 'kind': 'event', 'id': OTHER_ID, 'op': 'admin.status', 'body_type': 'json'},
         {'cores': []}),
        ('hello', {'v': 2, 'kind': 'hello', 'id': OTHER_ID, 'body_type': 'json', 'protocols': ['kelmo/2'],
                   'operations': ['state.search', 'state.capture']}, {}),
        ('empty_body_string', call(), ''),
    ]
    out = [(name, wire.encode(header, body), {'header': header, 'body': body, 'canonical': True})
           for name, header, body in valid]
    good = json_bytes(call())
    refused = [
        ('truncated', raw(good, b'{}')[:-3], 'E_PROTOCOL'),
        ('trailing_bytes', raw(good, b'{}') + b'x', 'E_PROTOCOL'),
        ('over_limit_prefix', struct.pack('>I', wire.MAX_FRAME + 1) + b'\x00\x02{}', 'E_LIMIT'),
        ('header_length_outside', raw(good, b'{}', head_length=len(good) + 10), 'E_PROTOCOL'),
        ('header_length_zero', raw(b'', b'{}'), 'E_PROTOCOL'),
        ('header_invalid_utf8', raw(b'{"v":2,"kind":"\xff"}', b'{}'), 'E_PROTOCOL'),
        ('body_invalid_utf8', raw(good, b'"\xc3\x28"'), 'E_PROTOCOL'),
        ('body_not_json', raw(good, b'{"a":'), 'E_PROTOCOL'),
        ('body_nan', raw(good, b'NaN'), 'E_PROTOCOL'),
        ('body_float_overflow', raw(good, b'1e400'), 'E_PROTOCOL'),
        ('body_integer_overflow', raw(good, b'18446744073709551616'), 'E_PROTOCOL'),
        ('body_lone_surrogate', raw(good, b'"\\ud800"'), 'E_PROTOCOL'),
        ('header_not_object', raw(b'[1]', b'{}'), 'E_PROTOCOL'),
        ('unknown_field', raw(json_bytes(call(extra=1)), b'{}'), 'E_PROTOCOL'),
        ('field_of_other_kind', raw(json_bytes(call(ok=True)), b'{}'), 'E_PROTOCOL'),
        ('missing_deadline', raw(json_bytes({k: v for k, v in call().items() if k != 'deadline_ms'}), b'{}'),
         'E_PROTOCOL'),
        ('wrong_version', raw(json_bytes(call(v=1)), b'{}'), 'E_PROTOCOL'),
        ('bad_id', raw(json_bytes(call(id='XYZ')), b'{}'), 'E_PROTOCOL'),
        ('bad_op', raw(json_bytes(call(op='search')), b'{}'), 'E_PROTOCOL'),
        ('negative_deadline', raw(json_bytes(call(deadline_ms=-1)), b'{}'), 'E_PROTOCOL'),
        ('origin_without_client', raw(json_bytes(call(origin={'session': 's'})), b'{}'), 'E_PROTOCOL'),
        ('reply_ok_with_error', raw(json_bytes({'v': 2, 'kind': 'reply', 'id': CALL_ID, 'ok': True,
                                                'body_type': 'json', 'error': {'code': 'E_X', 'message': '',
                                                                               'retryable': False}}), b'null'),
         'E_PROTOCOL'),
        ('unknown_kind', raw(json_bytes({'v': 2, 'kind': 'ping', 'id': CALL_ID, 'body_type': 'json'}), b'null'),
         'E_PROTOCOL'),
    ]
    out += [(name, data, {'code': code}) for name, data, code in refused]
    return out


def identity_contract():
    from kelmo.contracts import CONTRACT_VERSION, ChunkProfile, EmbeddingSpace
    from kelmo.store.memory.embed import canonical_json, identity_id
    tricky = 'q"uote\\back\x01ctl\ttab sep😀 кедр'
    records = [
        ('space', EmbeddingSpace(contract_version=CONTRACT_VERSION, artifact_identity='bge-m3@sha256:' + 'a' * 64,
                                 tokenizer_identity='sha256:' + 'b' * 64, query_transform=tricky,
                                 document_transform='passage: ' + tricky, pooling='cls', projection='none',
                                 normalization='l2', dimension=1024, dtype='float32', byte_order='little')),
        ('profile', ChunkProfile(version='kelmo-chunk/1', segmentation=tricky, token_budget=512, overlap=64,
                                 text_preparation='nfc', tokenizer_identity='sha256:' + 'c' * 64)),
    ]
    return [{'name': name, 'record': record.to_dict(), 'canonical': canonical_json(record),
             'id': identity_id(record)} for name, record in records]


def protocols_markdown(header, table):
    lines = ['# Protocols', '',
             'Generated by `tools/protocol_export.py` from the tables the code runs on; edit those, not this file.',
             '', '## Frame', '',
             '```text', 'u32 BE  total length = 2 + header length + body length, at most '
             f'{header["max_frame"]} bytes', 'u16 BE  header length (at most 65535)', 'header  UTF-8 JSON object',
             'body    UTF-8 JSON value (body_type "json")', '```', '',
             'Both header and body are canonical JSON: sorted keys, compact separators, non-ASCII kept, no NaN '
             'or infinity, integers within 64 bits. A malformed frame is refused with `E_PROTOCOL`, a frame over '
             'a bound with `E_LIMIT`.', '',
             'A reader accepts JSON whitespace after the body. A remote client ends the body of its `hello` with '
             'one newline, so a server that only reads lines (0.1.1) refuses the hello at once and the client '
             'falls back to `kelmo-client/1`.', '', '## Header fields', '',
             '| Field | On kinds | Required on |', '| --- | --- | --- |']
    for name, field in header['fields'].items():
        lines.append(f'| `{name}` | {", ".join(field["kinds"])} | {", ".join(field["required_on"]) or "-"} |')
    lines += ['', '## Daemon error codes', '',
              'Retryable: ' + ', '.join(f'`{c}`' for c in header['daemon_codes']['retryable']) + '. Final: '
              + ', '.join(f'`{c}`' for c in header['daemon_codes']['final']) + '.', '',
              '## Operations', '',
              'Replay class: `read` is sent again once after a core restart, `keyed` once under its key, '
              '`never` is reported as unsettled.', '',
              '| Operation | Lanes | Access | Replay | Arguments |', '| --- | --- | --- | --- | --- |']
    for op in table:
        arguments = ', '.join(f'`{n}`' + ('' if spec['required'] else '?') for n, spec in op['arguments'].items())
        lines.append(f'| `{op["name"]}` | {", ".join(op["lanes"])} | {op["access"]} | {op["replay"]} | '
                     f'{arguments or "-"} |')
    return '\n'.join(lines) + '\n'


def outputs():
    header = header_contract()
    table = [op.to_dict() for op in operations.table()]
    files = {
        CONTRACT / 'kelmo2' / 'header.json': dump(header).encode(),
        CONTRACT / 'kelmo2' / 'operations.json': dump(table).encode(),
        CONTRACT / 'kelmo2' / 'tools.json': dump(tools_contract()).encode(),
        CONTRACT / 'identity.json': dump(identity_contract()).encode(),
        ROOT / 'docs' / 'PROTOCOLS.md': protocols_markdown(header, table).encode(),
    }
    manifest = []
    for name, data, expectation in frames():
        files[CONTRACT / 'frames' / f'{name}.bin'] = data
        manifest.append({'name': name, 'file': f'{name}.bin', **expectation})
    files[CONTRACT / 'frames' / 'manifest.json'] = dump(manifest).encode()
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check', action='store_true', help='exit 1 if any output would change')
    args = parser.parse_args(argv)
    stale = []
    for path, data in outputs().items():
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if stale:
        print('Stale contract files (run tools/protocol_export.py):\n  ' + '\n  '.join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
