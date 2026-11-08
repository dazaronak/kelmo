"""Tell the vault owner when the executor's provider login is lost, once a day while it stays lost, and once
when the provider answers again.

A usage limit needs no one: work waits for the reset by itself. A lost login waits for a person, so it is
the one wait that alerts. The message goes on stdin to the command named in `[alerts]`; that command owns
the channel and its secrets, so none of them reach Kelmo. A marker in the state directory holds the outage
start and the last delivered alert, so a restart neither repeats an alert nor drops one; an undelivered
alert is tried again on the next lost-login answer.
"""
import json
import subprocess
import sys
import time

REMINDER_SECONDS = 86400
MARKER = 'login-outage.json'


def _when(epoch):
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(epoch))


class LoginWatch:
    def __init__(self, marker, argv=None, label='Kelmo', timeout=30):
        self.marker, self.argv, self.label, self.timeout = marker, argv, label, timeout

    @classmethod
    def for_service(cls, service):
        settings = service.config.alerts or {}
        return cls(service.config.state / MARKER, settings.get('command'), settings.get('label', 'Kelmo'))

    def saw(self, wait, now=None):
        """Read one checked executor answer: a QuotaWait, or None when the provider answered."""
        now = time.time() if now is None else now
        try:
            if wait is not None and wait.reason == 'login':
                self._lost(now)
            elif self.marker.exists():
                self._back(now)
        except Exception as error:  # noqa: BLE001 - Alerting must not turn a wait into a failed attempt.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(error).__name__}), file=sys.stderr, flush=True)

    def _lost(self, now):
        outage = json.loads(self.marker.read_text(encoding='utf-8')) if self.marker.exists() else None
        if outage is None:
            outage = {'since': now, 'alerted_at': None}
            self._save(outage)
        last = outage['alerted_at']
        if last is not None and now - last < REMINDER_SECONDS:
            return
        text = (f'{self.label}: the executor login is lost since {_when(outage["since"])}. Work waits and is '
                'tried again every 15 minutes; log in on the executor account.')
        if last is not None:
            text += f' Day {int((now - outage["since"]) // REMINDER_SECONDS) + 1}.'
        if self.send(text):
            self._save({**outage, 'alerted_at': now})

    def _back(self, now):
        outage = json.loads(self.marker.read_text(encoding='utf-8'))
        # The marker goes first: one recovery message per outage, even if its delivery fails.
        self.marker.unlink()
        hours = round((now - outage['since']) / 3600, 1)
        self.send(f'{self.label}: the executor login works again after {hours} h; waiting work resumes.')

    def _save(self, outage):
        partial = self.marker.with_name(self.marker.name + '.partial')
        partial.write_text(json.dumps(outage), encoding='utf-8')
        partial.replace(self.marker)

    def send(self, text):
        """Journal line plus the configured command; True once the command accepts the message."""
        print('kelmo worker: alert: ' + text, file=sys.stderr, flush=True)
        if not self.argv:
            return False
        try:
            done = subprocess.run(self.argv, input=text, text=True, capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(json.dumps({'alert_delivery': 'failed', 'error': type(error).__name__}), file=sys.stderr, flush=True)
            return False
        if done.returncode != 0:
            print(json.dumps({'alert_delivery': 'failed', 'exit': done.returncode}), file=sys.stderr, flush=True)
        return done.returncode == 0
