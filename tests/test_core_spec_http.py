"""The HTTP endpoint's bearer token file, and the spec guard that refuses the endpoint on a server daemon.

Nothing here starts a process: the spec is data, and the token is a file the daemon reads.
"""

import re
import stat

import pytest

from kelmo.core import spec
from kelmo.errors import KelmoError

TOKEN_LINE = re.compile(r'[0-9a-f]{64}\n')


def test_http_token_is_a_private_64_hex_line(tmp_path):
    state = tmp_path / '.state'
    path = spec.http_token_at(state)
    assert path == state / 'http-token'
    assert TOKEN_LINE.fullmatch(path.read_text(encoding='utf-8'))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_directory_is_git_ignored(tmp_path):
    state = tmp_path / '.state'
    spec.http_token_at(state)
    assert (state / '.gitignore').read_text(encoding='utf-8') == '*\n'


def test_second_call_keeps_the_same_token(tmp_path):
    state = tmp_path / '.state'
    first = spec.http_token_at(state).read_bytes()
    assert spec.http_token_at(state).read_bytes() == first


@pytest.mark.parametrize('stale', [b'', b'abc\n', b'a' * 63 + b'\n'], ids=['empty', 'short', 'one-short'])
def test_short_token_file_is_replaced_by_a_full_token(tmp_path, stale):
    state = tmp_path / '.state'
    state.mkdir(mode=0o700)
    (state / 'http-token').write_bytes(stale)
    path = spec.http_token_at(state)
    assert TOKEN_LINE.fullmatch(path.read_text(encoding='utf-8'))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_http_port_is_refused_on_a_server_daemon(tmp_path):
    root = tmp_path / 'vault'
    with pytest.raises(KelmoError) as refused:
        spec.build(root, server_profile=tmp_path / 'server.json', http_port=0)
    assert refused.value.code == 'E_CONFIG'
    # The refusal comes before any token is minted.
    assert not (root / '.state' / 'http-token').exists()


def test_plain_daemon_meta_names_no_server_profile(tmp_path):
    assert spec.build(tmp_path / 'vault')['meta'] == {'role': 'vault', 'server_profile': None}


def test_server_profile_meta_holds_the_resolved_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    built = spec.build(tmp_path / 'vault', server_profile='server.json')
    assert built['meta'] == {'role': 'vault', 'server_profile': str((tmp_path / 'server.json').resolve())}
