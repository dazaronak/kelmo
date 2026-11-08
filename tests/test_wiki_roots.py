import json
import subprocess
import uuid
from types import SimpleNamespace
import pytest

from kelmo.config import Config
from kelmo.errors import KelmoError
from kelmo.wiki import engine, links


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def initialize_repository(root):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "master")
    git(root, "config", "user.name", "Kelmo Tests")
    git(root, "config", "user.email", "kelmo@example.invalid")


def test_projected_tree_preserves_modes_and_caller_index(tmp_path):
    root = tmp_path / 'repo'
    initialize_repository(root)
    script = root / 'run file'
    script.write_text('original')
    script.chmod(0o755)
    link = root / 'link'
    link.symlink_to('run file')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    script.write_text('staged')
    git(root, 'add', '.')
    index = git(root, 'write-tree')
    unusual = root / 'new\tpage\nwith unicode-ą.md'

    tree = engine._projected_tree(root, 'HEAD', {script: 'published', unusual: 'new'})

    assert engine.git_blob(root, tree + ':run file') == b'published'
    assert engine.git_blob(root, tree + ':' + unusual.name) == b'new'
    assert git(root, 'ls-tree', tree, '--', 'run file').startswith('100755 ')
    assert git(root, 'write-tree') == index
    assert script.read_text() == 'staged' and not unusual.exists()
    with pytest.raises(engine.Conflict, match='not a regular file'):
        engine._projected_tree(root, 'HEAD', {link: 'overwrite'})


def write_config(root, wiki_root=None):
    text = (
        'schema_version = 1\nwriter = "local"\nvault_id = '
        + json.dumps(str(uuid.uuid4()))
        + '\nnode_label = "test"\n'
    )
    if wiki_root is not None:
        text += "wiki_root = " + json.dumps(str(wiki_root)) + "\n"
    (root / "kelmo.toml").write_text(text)



def test_managed_link_health_ignores_existing_ordinary_links_in_same_page(tmp_path):
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    (wiki / 'home.md').write_text('# Home\n\n[[preexisting-missing]]\n\n'
                                 '<!-- kb -->\n- Managed fact.\n<!-- /kb -->\n')
    registry = {'facts': {'fixture.fact': {'target': 'home.md'}}}
    assert links.audit_managed(tmp_path / 'data', registry, wiki_root=wiki)['broken'] == []
    (wiki / 'home.md').write_text('# Home\n\n[[preexisting-missing]]\n\n'
                                 '<!-- kb -->\n- [[managed-missing]]\n<!-- /kb -->\n')
    assert links.audit_managed(tmp_path / 'data', registry, wiki_root=wiki)['broken'] == [
        ('home.md', 'managed-missing')]



def prepare_roots(tmp_path):
    """A vault whose knowledge repository is selected apart from its data root (schema 2)."""
    from kelmo.service import Service
    data_root = tmp_path / "data"
    Service.initialize(data_root, "fixture", wiki_root=tmp_path / "personal-wiki")
    return data_root, Config.load(data_root).wiki
def test_legacy_configuration_keeps_embedded_wiki_root(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    write_config(root)

    config = Config.load(root)

    assert config.wiki == root / "knowledge/wiki"
    assert config.registry == root / "knowledge/provenance/registry.json"
    assert config.as_dict()["wiki"] == str(root / "knowledge/wiki")


def test_explicit_wiki_root_must_be_separate_from_data_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    write_config(root, root / "wiki")

    with pytest.raises(KelmoError, match="separate"):
        Config.load(root)













def test_existing_wiki_nested_in_parent_repository_is_rejected(tmp_path):
    parent = tmp_path / "notes-repository"
    initialize_repository(parent)
    (parent / "wiki").mkdir()
    git(parent, "commit", "--allow-empty", "-q", "-m", "parent repository")

    with pytest.raises(KelmoError, match="own Git repository"):
        engine.configure_wiki(
            tmp_path / "data", parent / "wiki", vault_id=str(uuid.uuid4())
        )



@pytest.mark.parametrize("target", ["notes/.md", "notes//file.md"])
def test_wiki_targets_reject_hidden_or_noncanonical_components(target):
    change = {
        "fact_key": "fixture.target",
        "target": target,
        "text": "Canonical target.",
        "status": "active",
        "expected_revision": None,
        "replaces": [],
    }

    with pytest.raises(ValueError, match="wiki target"):
        engine.validate_changes([change])


def test_wiki_target_accepts_existing_ordinary_relative_path_contract():
    change = {
        "fact_key": "fixture.target",
        "target": "notes/ordinary-file.md",
        "text": "Canonical target.",
        "status": "active",
        "expected_revision": None,
        "replaces": [],
    }

    assert engine.validate_changes([change]) == [change]


def test_git_failure_reports_safe_actionable_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=73, stdout=b"", stderr=b"SECRET signing detail"
        ),
    )

    with pytest.raises(KelmoError) as error:
        engine.git(tmp_path, "commit", "-m", "synthetic")

    assert error.value.code == "E_GIT"
    assert "commit" in str(error.value)
    assert "73" in str(error.value)
    assert "locks, signing, or hooks" in str(error.value)
    assert "SECRET" not in str(error.value)


def test_configure_wiki_uses_environment_identity_when_vault_parent_is_missing(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "missing" / "nested" / "vault"
    wiki_root = tmp_path / "wiki"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Environment Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Environment Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")

    engine.configure_wiki(data_root, wiki_root, vault_id=str(uuid.uuid4()))

    assert git(wiki_root, "log", "-1", "--pretty=%an <%ae>") == (
        "Environment Author <author@example.invalid>"
    )




def test_dangling_wiki_root_symlink_is_rejected_by_config_and_enrollment(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    wiki_link = tmp_path / "wiki-link"
    wiki_link.symlink_to(tmp_path / "missing-wiki", target_is_directory=True)
    write_config(data_root, wiki_link)

    with pytest.raises(KelmoError, match="symlink") as config_error:
        Config.load(data_root)
    with pytest.raises(KelmoError, match="symlink") as enrollment_error:
        engine.configure_wiki(data_root, wiki_link, vault_id=str(uuid.uuid4()))
    for error in (config_error.value, enrollment_error.value):
        assert str(wiki_link) in str(error)
        assert "Select a real non-symlink path" in str(error)


def test_second_vault_cannot_enroll_owned_wiki(tmp_path):
    first = tmp_path / "first-data"
    second = tmp_path / "second-data"
    wiki = tmp_path / "wiki"
    initialize_repository(first)
    initialize_repository(second)
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    engine.configure_wiki(first, wiki, vault_id=first_id)
    engine.activate_wiki(wiki, first_id)

    with pytest.raises(KelmoError) as error:
        engine.configure_wiki(second, wiki, vault_id=second_id)

    assert error.value.code == "E_WIKI_OWNER"


def test_failed_initialization_before_activation_does_not_poison_retry(tmp_path):
    data = tmp_path / "data"
    wiki = tmp_path / "wiki"
    initialize_repository(data)
    first_id, replacement_id = str(uuid.uuid4()), str(uuid.uuid4())
    engine.configure_wiki(data, wiki, vault_id=first_id)

    engine.configure_wiki(data, wiki, vault_id=replacement_id)
    engine.activate_wiki(wiki, replacement_id)


def test_missing_owner_fails_wiki_write_with_actionable_core_error(tmp_path):
    data_root, wiki_root = prepare_roots(tmp_path)
    engine._owner_path(wiki_root).unlink()

    import registry_seed
    with pytest.raises(KelmoError) as error:
        registry_seed.seed(data_root, pages_settings={"notes/existing.md": dict(
            page_id="existing-notes", layout="topic", title="Notes")})

    assert error.value.code == "E_WIKI_OWNER"
    assert "kelmo init --vault" in str(error.value)


@pytest.mark.parametrize("owner_state", ["missing", "malformed", "foreign", "symlink"])
def test_wiki_owner_health_is_false_for_untrusted_metadata(tmp_path, owner_state):
    data_root, wiki_root = prepare_roots(tmp_path)
    owner = engine._owner_path(wiki_root)
    if owner_state == "missing":
        owner.unlink()
    elif owner_state == "malformed":
        owner.write_text("{")
    elif owner_state == "foreign":
        owner.write_text(json.dumps({"version": 1, "vault_id": str(uuid.uuid4())}))
    else:
        owner.unlink()
        owner.symlink_to(tmp_path / "outside-owner")

    assert not engine.wiki_owner_valid(data_root, wiki_root)


def test_wiki_owner_health_accepts_embedded_and_matching_external_wiki(tmp_path):
    data_root, wiki_root = prepare_roots(tmp_path)

    assert engine.wiki_owner_valid(data_root, wiki_root)
    from kelmo.service import Service
    embedded = tmp_path / "embedded"
    Service.initialize(embedded, "fixture")
    assert engine.wiki_owner_valid(embedded, embedded / "knowledge/wiki")


def test_reinitializing_published_vault_repairs_missing_owner(tmp_path):
    from kelmo.service import Service

    data_root, wiki_root = tmp_path / "data", tmp_path / "wiki"
    Service.initialize(data_root, "fixture", wiki_root=wiki_root)
    engine._owner_path(wiki_root).unlink()

    Service.initialize(data_root, "fixture", wiki_root=wiki_root)

    assert engine.wiki_owner_valid(data_root, wiki_root)


def test_interrupted_owner_activation_publishes_no_partial_marker(tmp_path, monkeypatch):
    data_root, wiki_root = tmp_path / "data", tmp_path / "wiki"
    initialize_repository(data_root)
    vault_id = str(uuid.uuid4())
    engine.configure_wiki(data_root, wiki_root, vault_id=vault_id)
    owner = engine._owner_path(wiki_root)

    def fail_link(*_args, **_kwargs):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(engine.os, "link", fail_link)
    with pytest.raises(OSError, match="synthetic interruption"):
        engine.activate_wiki(wiki_root, vault_id)

    assert not owner.exists()
    assert list(owner.parent.glob(".kelmo-owner-*")) == []
