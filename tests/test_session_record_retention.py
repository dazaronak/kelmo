"""The daily HOT sweep removes per-session delivery records no delivery has written for 30 days."""

import os
import time

from kelmo.hub_delivery import refresh
from test_hot_set_retention import BINDING, DAY, Client


def _record(directory, name, days):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.json')
    path.write_text('{"core": null, "digest": "x"}')
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_removes_session_records_older_than_thirty_days(tmp_path):
    sessions = tmp_path / 'hub-sessions'
    old = [_record(sessions, f'old-{index}', days=31 + index) for index in range(3)]
    recent = [_record(sessions, f'recent-{index}', days=29 - index) for index in range(3)]
    Client(tmp_path).context(binding=BINDING)
    assert not [path.name for path in old if path.exists()], 'session records older than 30 days were kept'
    assert all(path.exists() for path in recent), 'a session record younger than 30 days was removed'


def test_a_session_whose_record_was_removed_gets_the_whole_context_without_a_restart_warning(tmp_path):
    state = tmp_path / 'hub-sessions' / 'gone.json'
    update = refresh(state, 'the whole context', verified=True, identity='identity', core='core')
    assert update['context'] == 'the whole context'


def test_the_sweep_leaves_records_behind_a_symlinked_sessions_folder_alone(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    report = _record(elsewhere, 'report', days=40)
    (tmp_path / 'hub-sessions').symlink_to(elsewhere, target_is_directory=True)
    Client(tmp_path).context(binding=BINDING)
    assert report.exists(), 'the sweep followed a symlinked hub-sessions folder'


def test_the_sweep_leaves_a_symlinked_record_and_its_target_alone(tmp_path):
    target = _record(tmp_path / 'elsewhere', 'target', days=40)
    sessions = tmp_path / 'hub-sessions'
    sessions.mkdir()
    link = sessions / 'linked.json'
    link.symlink_to(target)
    os.utime(link, (time.time() - 40 * DAY,) * 2, follow_symlinks=False)
    Client(tmp_path).context(binding=BINDING)
    assert link.is_symlink() and target.exists(), 'the sweep removed a symlinked record'
