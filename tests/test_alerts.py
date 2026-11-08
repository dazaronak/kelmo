"""A lost executor login reaches its owner once, then once a day, and once more when it is back."""
import json
import sys
import time

import pytest

from test_resolution import pending
from kelmo import quota
from kelmo.alerts import LoginWatch
from kelmo.config import validate_alerts
from kelmo.errors import KelmoError

pytest_plugins = ['test_fact_checks']


def recorder(tmp_path, exit_code=0):
    """An alert command that appends each message to messages.txt and exits with exit_code."""
    script = tmp_path / 'alert.py'
    script.write_text('import sys\nwith open(sys.argv[1], "a") as out:\n    out.write(sys.stdin.read() + "\\n")\n'
                      f'sys.exit({exit_code})\n')
    return [sys.executable, str(script), str(tmp_path / 'messages.txt')]


def messages(tmp_path):
    path = tmp_path / 'messages.txt'
    return path.read_text().splitlines() if path.exists() else []


def test_a_wait_names_its_reason_and_nothing_else_may_ride_along():
    reset_at = time.time() + 60
    assert quota.read({'outcome': 'quota_wait', 'reset_at': reset_at, 'reason': 'login'}) == pytest.approx(reset_at)
    with pytest.raises(quota.QuotaWait) as waited:
        quota.checked({'outcome': 'quota_wait', 'reset_at': reset_at, 'reason': 'login'})
    assert waited.value.reason == 'login' and 'login is lost' in str(waited.value)
    with pytest.raises(KelmoError) as refused:
        quota.checked({'outcome': 'quota_wait', 'reset_at': reset_at, 'reason': 'holiday'})
    assert refused.value.code == 'E_EXECUTOR'


def test_a_lost_login_alerts_once_then_daily_then_once_when_back(tmp_path):
    watch = LoginWatch(tmp_path / 'login-outage.json', recorder(tmp_path), 'Server Kelmo')
    start = 1_800_000_000
    lost = quota.QuotaWait(start + 900, 'login')
    watch.saw(lost, now=start)
    watch.saw(lost, now=start + 900)
    assert len(messages(tmp_path)) == 1
    assert messages(tmp_path)[0].startswith('Server Kelmo: the executor login is lost since')
    watch.saw(lost, now=start + 86400)
    assert len(messages(tmp_path)) == 2 and messages(tmp_path)[1].endswith('Day 2.')
    # A usage-limit answer comes from a working login: the outage is over.
    watch.saw(quota.QuotaWait(start + 90000, 'limit'), now=start + 90000)
    assert 'works again after 25.0 h' in messages(tmp_path)[2]
    assert not (tmp_path / 'login-outage.json').exists()
    watch.saw(None, now=start + 90100)
    assert len(messages(tmp_path)) == 3


def test_an_undelivered_alert_is_tried_again_on_the_next_lost_login(tmp_path):
    watch = LoginWatch(tmp_path / 'login-outage.json', recorder(tmp_path, exit_code=1))
    lost = quota.QuotaWait(time.time() + 900, 'login')
    watch.saw(lost, now=1_800_000_000)
    watch.saw(lost, now=1_800_000_060)
    assert len(messages(tmp_path)) == 2, 'nothing was delivered, so the next answer tries again'
    assert json.loads((tmp_path / 'login-outage.json').read_text())['alerted_at'] is None


def test_the_worker_watches_every_checked_answer_and_lets_go_on_exit(service, tmp_path, capsys):
    from kelmo.daemon import run
    pending(service)
    answer = tmp_path / 'executor.py'
    answer.write_text('import json\nprint(json.dumps({"outcome": "quota_wait", "reset_at": '
                      + repr(time.time() + 900) + ', "reason": "login"}))\n')
    config = tmp_path / 'executor.json'
    config.write_text(json.dumps({'argv': [sys.executable, str(answer)], 'timeout_seconds': 30,
                                  'max_attempts': 3, 'poll_seconds': 2}))
    result = run(service, config, once=True)
    assert result['quota_waits'][0]['reset_at'] > time.time()
    assert (service.config.state / 'login-outage.json').exists()
    assert 'the executor login is lost' in capsys.readouterr().err
    assert quota.observer is None


def test_a_waiting_session_job_is_reported_as_a_wait_not_a_failure(tmp_path):
    from unittest.mock import patch
    from kelmo.daemon import tick
    from kelmo.service import Service
    from test_session_worker import prepared
    root, job_id = prepared(tmp_path)
    answer = tmp_path / 'executor.py'
    answer.write_text('import json, time\nprint(json.dumps({"outcome": "quota_wait", "reset_at": time.time() + 900}))\n')
    with Service(root) as service, patch('kelmo.store.memory.store.embed', return_value=bytes(1536)):
        result = tick(service, [sys.executable, str(answer)])
    assert 'failed_attempts' not in result
    assert result['quota_waits'] == [{'session_job_id': job_id, 'reset_at': pytest.approx(time.time() + 900, abs=60)}]


def test_the_alert_settings_take_an_absolute_command_and_a_short_label():
    assert validate_alerts(None) is None
    assert validate_alerts({'command': ['/usr/bin/true']}) == {'command': ['/usr/bin/true'], 'label': 'Kelmo'}
    for bad in ({'command': ['true']}, {'command': []}, {'command': ['/usr/bin/true'], 'label': ''},
                {'command': ['/usr/bin/true'], 'token': 'x'}):
        with pytest.raises(KelmoError) as refused:
            validate_alerts(bad)
        assert refused.value.code == 'E_CONFIG'
