"""The daemon's own files (lock, spec, options, job state) live in the vault's state directory. A vault with
bound storage keeps that directory outside the vault, and nothing may then appear under `<vault>/.state`."""
import json
import uuid
from types import SimpleNamespace

from kelmo.config import Config
from kelmo.core import daemon_admin, instance, spec as core_spec
from kelmo.core.jobs import Jobs


def bound_vault(tmp_path):
    root = (tmp_path / 'vault').resolve()
    root.mkdir()
    data = (tmp_path / 'data').resolve()
    roots = {name: data / name for name in ('state_root', 'archive_root', 'knowledge_git_root')}
    for path in roots.values():
        path.mkdir(parents=True)
    storage = ''.join(f'{name} = {json.dumps(str(path))}\n' for name, path in roots.items())
    (root / 'kelmo.toml').write_text(
        f'schema_version = 2\nwriter = "local"\nvault_id = "{uuid.uuid4()}"\nnode_label = "bound"\n\n[storage]\n'
        + storage + 'registry_relative = "knowledge/provenance/registry.json"\nwiki_relative = "knowledge/wiki"\n',
        encoding='utf-8')
    return root, roots['state_root']


def test_daemon_files_of_a_vault_with_bound_storage_live_in_its_state_root(tmp_path):
    root, state = bound_vault(tmp_path)
    assert Config.load(root).state == state

    assert instance.state_dir(root) == state
    assert instance.lock_path(root) == state / 'daemon.lock'
    assert daemon_admin._state(root) == state
    built = core_spec.build(root, core_command=['kelmo', 'core-run'])
    assert built['state_dir'] == str(state)  # kelmod's lock and executor records follow it
    assert Jobs(SimpleNamespace(root=root, storage=None))._state_path == state / 'jobs.json'

    daemon_admin.write_options(daemon_admin._state(root), {'role': 'vault'})
    assert (state / daemon_admin.OPTIONS).is_file()
    assert daemon_admin.lock_free(root) is True
    assert not (root / '.state').exists()


def test_a_default_or_new_vault_keeps_its_daemon_files_under_dot_state(tmp_path):
    new = (tmp_path / 'new').resolve()
    assert instance.state_dir(new) == new / '.state'  # not initialized yet: where `init` will put it
    assert core_spec.build(new, core_command=['kelmo', 'core-run'])['state_dir'] == str(new / '.state')
