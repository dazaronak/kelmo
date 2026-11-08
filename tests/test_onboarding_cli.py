"""Storage selection through the public command contract."""
import json
import subprocess

from kelmo.cli import main


def test_init_accepts_separate_new_wiki_path(tmp_path, capsys):
    data = tmp_path / 'data'
    wiki = tmp_path / 'my-wiki'
    assert main(['init', '--vault', str(data), '--wiki', str(wiki),
                 '--label', 'fixture', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is True
    assert result['result']['wiki'] == str(wiki / 'knowledge/wiki')
    assert result['result']['schema_version'] == 2
    assert result['result']['storage']['knowledge_git_root'] == str(wiki)
    assert result['result']['registry'] == 'current'
    # The first publication (the initial project map) landed in the selected repository, not the vault.
    assert (wiki / 'knowledge/provenance/registry.json').is_file()
    assert (wiki / 'knowledge/wiki/projects/default/default.md').is_file()
    assert not (data / 'knowledge').exists()
    assert subprocess.run(['git', '-C', str(wiki), 'status', '--porcelain'], capture_output=True,
                          text=True, check=True).stdout == ''


def test_existing_wiki_content_is_refused_and_preserved(tmp_path, capsys):
    data = tmp_path / 'data'
    wiki = tmp_path / 'my-wiki'
    wiki.mkdir()
    original = b'# My knowledge\n\nKeep the organization and these bytes.\n'
    page = wiki / 'home.md'
    page.write_bytes(original)
    args = ['init', '--vault', str(data), '--wiki', str(wiki), '--label', 'fixture', '--json']
    assert main(args) != 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected['error']['code'] == 'E_WIKI_ENROLL'
    assert page.read_bytes() == original
    assert not (data / 'kelmo.toml').exists()


def test_reinitialization_cannot_silently_switch_wiki(tmp_path, capsys):
    data = tmp_path / 'data'
    wiki = tmp_path / 'wiki'
    args = ['init', '--vault', str(data), '--wiki', str(wiki), '--label', 'fixture', '--json']
    assert main(args) == 0
    capsys.readouterr()
    assert main(['init', '--vault', str(data), '--wiki', str(tmp_path / 'other'),
                 '--label', 'fixture', '--json']) != 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'E_CONFIG'


def test_reinitialization_reports_retained_label(tmp_path, capsys):
    root = tmp_path / 'vault'
    assert main(['init', '--vault', str(root), '--label', 'original', '--json']) == 0
    capsys.readouterr()
    assert main(['init', '--vault', str(root), '--label', 'different', '--json']) == 0
    result = json.loads(capsys.readouterr().out)['result']
    assert result['node_label'] == 'original'
    assert result['warnings'] and 'label' in result['warnings'][0]


def test_missing_wiki_does_not_hide_archive_or_operation_status(tmp_path):
    from kelmo.service import Service
    data, wiki = tmp_path / 'data', tmp_path / 'wiki'
    Service.initialize(data, 'fixture', wiki_root=wiki)
    (wiki / 'knowledge/wiki').rename(tmp_path / 'disconnected-wiki')
    with Service(data) as service:
        captured = service.capture('Synthetic evidence retained while wiki is offline.', 'offline.md')
        assert service.trace(captured['id'])['operation']['state'] == 'captured'
        assert service.status()['archive_records'] == 0


def test_relative_wiki_selection_is_cwd_relative_and_survives_pair_relocation(tmp_path, monkeypatch, capsys):
    from kelmo.config import Config
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    monkeypatch.chdir(bundle)
    args = ['init', '--vault', 'data', '--wiki', 'wiki', '--label', 'fixture', '--json']
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['result']['wiki'] == str(bundle / 'wiki/knowledge/wiki')
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['result']['existing'] is True
    moved = tmp_path / 'moved'
    monkeypatch.chdir(tmp_path)
    bundle.rename(moved)
    assert Config.load(moved / 'data').wiki == moved / 'wiki/knowledge/wiki'
    assert Config.load(moved / 'data').registry == moved / 'wiki/knowledge/provenance/registry.json'
