from pathlib import Path

import pytest


def install(path, disabled):
    from kelmo.install.instructions import install_instructions
    return install_instructions(path, disable_native=disabled)


def test_install_repeat_and_toggle_preserve_surrounding_bytes(tmp_path):
    path = tmp_path / 'AGENTS.md'
    original = b'# My rules\r\n\r\nKeep this exactly.'
    path.write_bytes(original)
    first = install(path, True)
    assert Path(first['backup']).read_bytes() == original
    written = path.read_bytes()
    assert written.startswith(original)
    assert 'Do not use' in path.read_text()
    assert install(path, True)['changed'] is False
    assert path.read_bytes() == written
    path.write_bytes(written + b'\n# Other instructions\n')
    install(path, False)
    assert path.read_bytes().startswith(original)
    assert path.read_bytes().endswith(b'\n# Other instructions\n')
    assert 'Do not use' not in path.read_text()
    assert 'kelmo search' in path.read_text()
    install(path, True)
    assert path.read_text().count('<!-- kelmo:begin -->') == 1


@pytest.mark.parametrize('text', [
    '<!-- kelmo:begin -->',
    '<!-- kelmo:end -->\n<!-- kelmo:begin -->',
    '<!-- kelmo:begin -->\n<!-- kelmo:begin -->\n<!-- kelmo:end -->',
])
def test_ambiguous_markers_do_not_modify_file(tmp_path, text):
    from kelmo.errors import KelmoError
    path = tmp_path / 'CLAUDE.md'
    path.write_text(text)
    with pytest.raises(KelmoError):
        install(path, True)
    assert path.read_text() == text
    assert list(tmp_path.glob('*.bak*')) == []


def test_new_file_and_symlink_refusal(tmp_path):
    from kelmo.errors import KelmoError
    path = tmp_path / 'new/CLAUDE.md'
    result = install(path, False)
    assert result['backup'] is None
    assert result['native_memory_disabled'] is False
    link = tmp_path / 'AGENTS.md'
    link.symlink_to(path)
    original = path.read_bytes()
    with pytest.raises(KelmoError):
        install(link, True)
    assert path.read_bytes() == original
