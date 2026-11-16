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


def _oversized(tmp_path, monkeypatch, mode):
    """prepare() arguments whose delivery points at a context reference for its bound work."""
    import hashlib
    from types import SimpleNamespace

    from kelmo import hub_embed
    from test_hub_delivery import snapshot
    monkeypatch.setattr(hub_embed, 'sync', lambda *args, **kwargs: {'verified': True, 'changed': False, **mode})
    value = snapshot(1, 0)
    content = '<!-- kelmo generation 1 -->\n' + 'M' * 10500
    value['files']['mandatory.md'] = {'text': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
    callbacks = []
    return callbacks, dict(config=SimpleNamespace(state=tmp_path), payload={'session_id': 'session'},
                           snapshot=value, project='alpha', work='WORK_' + 'x' * 12000, pending=[],
                           work_path='projects/alpha/work.md', provider='claude', current=tmp_path / 'hot',
                           stale=False, acknowledge=callbacks.append)


def test_a_reference_used_again_counts_as_used_now(tmp_path, monkeypatch):
    from kelmo import hub_delivery
    _, args = _oversized(tmp_path, monkeypatch, {})
    hub_delivery.prepare(**args)
    [reused] = list((tmp_path / 'hub-context').glob('*.json'))
    _age(reused, 40)
    assert str(reused) in hub_delivery.prepare(**args)
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


def test_a_prompt_answered_with_the_marker_does_not_count_as_a_use_of_the_reference(tmp_path, monkeypatch):
    """Only a delivery that shows the pointer uses the reference; the one-line marker shows none."""
    from kelmo import hub_delivery
    callbacks, args = _oversized(tmp_path, monkeypatch, {'mode': 'hook'})
    first = hub_delivery.prepare(**args)
    [reference] = list((tmp_path / 'hub-context').glob('*.json'))
    assert str(reference) in first
    for callback in callbacks:
        callback()
    _age(reference, 40)
    second = hub_delivery.prepare(**args)
    assert str(reference) not in second, second
    assert time.time() - reference.stat().st_mtime > 30 * DAY, 'a marker-only answer refreshed the reference'
