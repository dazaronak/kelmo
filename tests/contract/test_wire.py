"""The kelmo/2 contract fixtures, read by the Python codec exactly as the Rust codec reads them, and a
cross-language round trip of random frames through the Rust codec."""
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from kelmo.core import wire
from kelmo.errors import KelmoError

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / 'tests' / 'contract'
MANIFEST = json.loads((CONTRACT / 'frames' / 'manifest.json').read_text())


def data(entry):
    return (CONTRACT / 'frames' / entry['file']).read_bytes()


@pytest.mark.parametrize('entry', MANIFEST, ids=[entry['name'] for entry in MANIFEST])
def test_golden_frames_decode_encode_or_refuse_as_the_manifest_says(entry):
    if 'code' in entry:
        with pytest.raises(KelmoError) as refused:
            wire.decode(data(entry))
        assert refused.value.code == entry['code']
    else:
        header, body = wire.decode(data(entry))
        assert (header, body) == (entry['header'], entry['body'])
        assert wire.encode(header, body) == data(entry)


def test_a_stream_fed_byte_by_byte_yields_the_frames_in_order():
    valid = [entry for entry in MANIFEST if 'code' not in entry]
    reader, seen = wire.FrameReader(), []
    for byte in b''.join(data(entry) for entry in valid):
        seen += reader.feed(bytes([byte]))
    assert reader.pending == 0
    assert seen == [(entry['header'], entry['body']) for entry in valid]


def test_a_frame_at_the_bound_passes_and_one_byte_more_is_refused():
    header = {'v': 2, 'kind': 'event', 'id': '0' * 32, 'op': 'admin.status', 'body_type': 'json'}
    fill = wire.MAX_FRAME - 2 - len(wire.canonical(header).encode()) - 2
    frame = wire.encode(header, 'a' * fill)
    assert len(frame) == 4 + wire.MAX_FRAME and wire.decode(frame)[1] == 'a' * fill
    with pytest.raises(KelmoError) as refused:
        wire.encode(header, 'a' * (fill + 1))
    assert refused.value.code == 'E_LIMIT'
    with pytest.raises(KelmoError) as early:
        wire.FrameReader().feed((wire.MAX_FRAME + 1).to_bytes(4, 'big'))
    assert early.value.code == 'E_LIMIT', 'an oversized prefix is refused before its bytes arrive'


def test_contract_files_are_current():
    result = subprocess.run([sys.executable, str(ROOT / 'tools' / 'protocol_export.py'), '--check'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_daemon_codes_are_known_to_the_server_retry_rule():
    from kelmo.server import RETRYABLE
    assert wire.DAEMON_RETRYABLE | {'E_CANCELLED'} <= RETRYABLE
    assert 'E_FORBIDDEN' not in RETRYABLE


def random_value(rng, depth=0):
    choice = rng.randrange(8 if depth < 3 else 5)
    if choice == 0:
        return rng.choice([None, True, False])
    if choice == 1:
        return rng.randint(-2 ** 63, 2 ** 64 - 1)
    if choice == 2:
        return rng.choice([rng.uniform(-1e6, 1e6), rng.random() * 10 ** rng.randint(-30, 30), 0.0, -0.0])
    if choice in (3, 4):
        alphabet = 'abcXYZ09 "\\\n\t\x01\x7fкедрłą 😀'
        return ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    if choice in (5, 6):
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f'k{rng.randint(0, 99)}{rng.choice("aéz")}': random_value(rng, depth + 1)
            for _ in range(rng.randint(0, 4))}


def random_header(rng):
    kind = rng.choice(sorted(wire.KINDS))
    header = {'v': 2, 'kind': kind, 'id': '%032x' % rng.getrandbits(128), 'body_type': 'json'}
    if kind in ('call', 'event'):
        header['op'] = rng.choice(['state.search', 'archive.capture', 'embed.count', 'admin.ping'])
    if kind == 'call':
        header['deadline_ms'] = rng.randint(0, 86_400_000)
        if rng.random() < 0.5:
            header['lane'] = rng.choice(['read', 'write', 'handler'])
        if rng.random() < 0.3:
            header['key'] = 'client/a/' + str(rng.randint(0, 10 ** 9))
        if rng.random() < 0.3:
            header['origin'] = {'client_id': 'mac-work', 'machine_tag': rng.choice(['ct105', None]),
                                'session': 'кедр'}
    if kind == 'reply':
        header['ok'] = rng.random() < 0.7
        if not header['ok']:
            header['error'] = {'code': 'E_BUSY', 'message': 'busy ✓', 'retryable': True}
    if kind == 'hello':
        header['protocols'], header['operations'] = ['kelmo/2'], ['state.search']
    return header


def roundtrip_binary():
    explicit = os.environ.get('KELMO_WIRE_ROUNDTRIP')
    path = Path(explicit) if explicit else ROOT / 'rust' / 'target' / 'debug' / 'examples' / 'wire_roundtrip'
    assert path.is_file(), f'{path} is missing; build it with tools/build_kelmod.sh'
    return path


def test_ten_thousand_random_frames_round_trip_through_the_rust_codec():
    rng = random.Random(20261110)
    frames = [(random_header(rng), random_value(rng)) for _ in range(10_000)]
    stream = b''.join(wire.encode(header, body) for header, body in frames)
    result = subprocess.run([str(roundtrip_binary())], input=stream, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == stream, 'the Rust codec re-encodes every frame to the same canonical bytes'
    assert wire.FrameReader().feed(result.stdout) == frames
