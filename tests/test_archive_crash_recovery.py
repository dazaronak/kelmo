"""Controller-run, process-death probes for archive durability.

Run only from the controller's selected pytest environment. Every implementation
import occurs in a bounded child using KELMO_IMPORT_ROOT verbatim.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


CHILD = r"""
import json, os, pathlib, sqlite3, sys
sys.path.insert(0, os.environ['KELMO_IMPORT_ROOT'])
from kelmo.service import Service
import kelmo.service as selected_service
assert pathlib.Path(selected_service.__file__).resolve().is_relative_to(
    pathlib.Path(os.environ['KELMO_IMPORT_ROOT']).resolve())

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
marker = root.parent / (root.name + '.probe.json')
os.environ.update(GIT_AUTHOR_NAME='Synthetic Fixture', GIT_COMMITTER_NAME='Synthetic Fixture',
                  GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid')

def embed(*args, **kwargs):
    return bytes(1536)

if mode == 'prepare-archive':
    Service.initialize(root, 'crash-fixture')
    from kelmo.store.memory import store
    store.embed = embed
    with Service(root) as service:
        op = service.capture('synthetic archive crash evidence\n', 'crash.md', 'implementer')
        marker.write_text(json.dumps({'op': op['id']}))
elif mode == 'crash-archive':
    from kelmo.store.memory import store
    store.embed = embed
    with Service(root) as service:
        def die_on_owning_receipt(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_INSERT and arg1 == 'archive_receipts':
                os._exit(71)
            return sqlite3.SQLITE_OK
        service.archive.set_authorizer(die_on_owning_receipt)
        service.run(json.loads(marker.read_text())['op'])
    raise SystemExit(91)
elif mode == 'recover-archive':
    from kelmo.store.memory import store
    store.embed = embed
    with Service(root) as service:
        op_id = json.loads(marker.read_text())['op']
        before = service.journal.get(op_id)
        assert before['archive_id'] is None and before['state'] == 'queued'
        source = service.source_path(before)
        assert source.is_file() and source.read_text() == 'synthetic archive crash evidence\n'
        assert service.archive.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
        assert service.archive.execute('SELECT COUNT(*) FROM archive_receipts').fetchone()[0] == 0
        assert service.archive.execute('SELECT COUNT(*) FROM memories_fts').fetchone()[0] == 0
        # A dead local worker owns a non-expiring claim. Use the journal's explicit
        # dead-worker handover path; this is not an elapsed external lease.
        service.journal.handover_local_claim(op_id)
        result = service.run(op_id)
        assert result['state'] == 'archived' and result['archive_id']
        assert service.status()['archive_records'] == 1
        assert service.archive.execute('SELECT COUNT(*) FROM archive_receipts').fetchone()[0] == 1
elif mode == 'prepare-supersession':
    Service.initialize(root, 'crash-fixture')
    from kelmo.store.memory import store
    store.embed = embed
    with Service(root) as service:
        old_op = service.capture('synthetic predecessor evidence\n', 'old.md', 'implementer')
        old = service.run(old_op['id'])
        new_op = service.capture('synthetic replacement evidence\n', 'new.md', 'implementer',
                                 supersedes=old['archive_id'])
        marker.write_text(json.dumps({'op': new_op['id'], 'old': old['archive_id']}))
elif mode == 'crash-supersession':
    from kelmo.service import Service
    from kelmo.store.memory import store
    store.embed = embed
    with Service(root) as service:
        def die_before_hide(*args, **kwargs):
            os._exit(72)
        # The archive link commits before the independent hide operation.
        service.archive_api.hide = die_before_hide
        service.run(json.loads(marker.read_text())['op'])
    raise SystemExit(92)
elif mode == 'recover-supersession':
    from kelmo.store.memory import store
    def forbidden_embed(*args, **kwargs):
        raise AssertionError('accepted replacement must be reused without embedding')
    store.embed = forbidden_embed
    with Service(root) as service:
        data = json.loads(marker.read_text())
        op_id, old_id = data['op'], data['old']
        op = service.journal.get(op_id)
        assert op['archive_id'] is None and op['state'] == 'queued'
        assert service.archive_api.capture_receipt(op_id, service._operation_origin(op_id)) is not None
        link = service.archive.execute('SELECT supersedes FROM record_supersession WHERE id=?',
                                        (service.archive_api.capture_receipt(op_id, service._operation_origin(op_id)).archive_id,)).fetchone()
        assert link and link[0] == old_id
        assert service.archive_api.hidden_at(old_id) is None
        result = service.run(op_id)
        assert result['state'] == 'archived'
        assert service.archive_api.hidden_at(old_id) is not None
        assert service.status()['archive_records'] == 2
        assert service.archive.execute('SELECT COUNT(*) FROM archive_receipts').fetchone()[0] == 2
        assert service.archive.execute('SELECT COUNT(*) FROM record_supersession').fetchone()[0] == 1
        assert service.journal.get(op_id)['archive_id'] == result['archive_id']
else:
    raise SystemExit('unknown probe mode')
"""


def run_child(root, mode, expected):
    env = os.environ.copy()
    import_root = env.get("KELMO_IMPORT_ROOT", str(Path(__file__).resolve().parents[1] / "src"))
    if not Path(import_root).is_dir():
        pytest.fail("KELMO_IMPORT_ROOT must name the exact selected source import root")
    env["KELMO_IMPORT_ROOT"] = import_root
    synthetic_home = root.parent / "client-home"
    synthetic_home.mkdir(exist_ok=True)
    env.update(HOME=str(synthetic_home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(CHILD), str(root), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, (mode, result.returncode, result.stdout, result.stderr)
    return result


def test_archive_transaction_process_death_rolls_back_and_recovers(tmp_path):
    root = tmp_path / "archive-vault"
    run_child(root, "prepare-archive", 0)
    run_child(root, "crash-archive", 71)
    run_child(root, "recover-archive", 0)


def test_supersession_link_process_death_reuses_receipt_and_finishes_hide(tmp_path):
    root = tmp_path / "supersession-vault"
    run_child(root, "prepare-supersession", 0)
    run_child(root, "crash-supersession", 72)
    run_child(root, "recover-supersession", 0)
