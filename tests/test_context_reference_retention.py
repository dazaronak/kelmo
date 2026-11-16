"""The daily HOT sweep removes context references no delivery has used for 30 days; a reused one stays."""

import os
import time

from kelmo.hub_delivery import context_reference
from test_hot_set_retention import BINDING, DAY, Client


def _age(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_the_sweep_removes_references_unused_for_thirty_days(tmp_path):
    old = context_reference(tmp_path, {'pending': ['old']})
    recent = context_reference(tmp_path, {'pending': ['recent']})
    _age(old, 40)
    _age(recent, 20)
    Client(tmp_path).context(binding=BINDING)
    assert not old.exists(), 'a context reference unused for 40 days was kept'
    assert recent.exists(), 'a context reference used 20 days ago was removed'


def test_a_reference_used_again_counts_as_used_now(tmp_path):
    reused = context_reference(tmp_path, {'pending': ['same']})
    _age(reused, 40)
    assert context_reference(tmp_path, {'pending': ['same']}) == reused
    Client(tmp_path).context(binding=BINDING)
    assert reused.exists(), 'a reference a delivery used again today was removed'


def test_the_sweep_leaves_files_behind_a_symlinked_folder_alone(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    report = elsewhere / 'report.json'
    report.write_text('{}')
    _age(report, 40)
    for name in ('hub-context', 'hub-sessions'):
        (tmp_path / name).symlink_to(elsewhere, target_is_directory=True)
    Client(tmp_path).context(binding=BINDING)
    assert report.exists(), 'the sweep removed a file through a symlinked folder'
