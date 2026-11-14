"""Tell the vault owner when work waits for a person: once when it starts, once a day while it lasts, and once
when it is over.

A usage limit needs no one: work waits for the reset by itself. A lost login waits for a person, and so
does a vault that takes no publication (`VaultWatch`): a prepared publication the recovery cannot finish,
uncommitted edits on the wiki's paths, a wiki over its budget. The message goes on stdin to the command
named in `[alerts]`; that command owns the channel and its secrets, so none of them reach Kelmo. A marker in
the state directory holds the outage start and the last delivered alert, so a restart neither repeats an
alert nor drops one; an undelivered alert is tried again on the next answer that shows the same outage.
"""
import json
import subprocess
import sys
import time

REMINDER_SECONDS = 86400
MARKER = 'login-outage.json'
VAULT_MARKER = 'vault-outage.json'


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


class VaultWatch(LoginWatch):
    """The same outage record for a vault that takes no publication (`kelmo.errors.VaultFailure`).

    The apply pass and the worker report every failure they stop at (`blocked`), and every publication they
    complete or vault check they get past with nothing stopping them (`published`). The owner hears of the
    outage once when it starts, with the note that met it, the code and the repair the failure names, once a
    day while it lasts, and once when it is over. Notes wait meanwhile, uncharged, so the alert is what keeps a
    failure that never heals from going unnoticed.
    """

    @classmethod
    def for_service(cls, service):
        settings = service.config.alerts or {}
        return cls(service.config.state / VAULT_MARKER, settings.get('command'), settings.get('label', 'Kelmo'))

    def saw(self, wait, now=None):
        raise TypeError('VaultWatch reads publication outcomes, not executor answers: use blocked or published.')

    def _outage(self):
        """The outage on record, or None. A marker that cannot be read as one starts a new record: keeping it
        would make every later alert fail, which is the silence the watch exists to prevent."""
        if not self.marker.exists():
            return None
        try:
            outage = json.loads(self.marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(outage, dict) or not isinstance(outage.get('since'), (int, float)):
            return None
        told = outage.get('told') if isinstance(outage.get('told'), dict) else {}
        # A delivery time that cannot be read counts as never delivered: that kind is told again, never silenced.
        return {'since': outage['since'],
                'told': {kind: at for kind, at in told.items()
                         if isinstance(kind, str) and isinstance(at, (int, float))}}

    def blocked(self, operation_id, code, reason, now=None):
        """A pass or a worker tick stopped at a vault failure (`code`, `reason`) while handling `operation_id`."""
        now = time.time() if now is None else now
        try:
            outage = self._outage() or {'since': now, 'told': {}}
            told = outage['told']
            # Each kind of failure keeps its own daily cadence within the one outage: a kind not told today is
            # told at once (also when nothing in between closed the record, as when a pass got past the vault's
            # checks and stopped at a new failure), and kinds that alternate are not told on every pass.
            last = told.get(code)
            if last is not None and now - last < REMINDER_SECONDS:
                return
            text = (f'{self.label}: no note can be published since {_when(outage["since"])}; notes wait and are '
                    f'not charged. Note {operation_id} met {code}: ' + ' '.join(str(reason).split())[:300])
            if last is not None:
                text += f' Day {int((now - outage["since"]) // REMINDER_SECONDS) + 1}.'
            # The alert goes before its record: a state directory that cannot be written (a full disk is one
            # vault failure that does this) must not silence it, and with no record kept the next report tells
            # it again. With no `[alerts]` command the journal line is the delivery, so it too comes once a day.
            delivered = self.send(text) or not self.argv
            self._save({'since': outage['since'], 'told': {**told, code: now} if delivered else told})
        except Exception as failure:  # noqa: BLE001 - Alerting must not turn a wait into a failed pass.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def published(self, now=None, *, applied=True):
        """An outage on record is over: a note was applied with no vault failure in the same pass or tick, or
        (`applied=False`) a pass got past the vault's checks and stopped at none, so a new outage is told at
        once instead of at the old one's daily reminder."""
        now = time.time() if now is None else now
        try:
            if not self.marker.exists():
                return
            outage = self._outage()
            # The marker goes first: one recovery message per outage, even if its delivery fails.
            self.marker.unlink()
            what = 'notes are published again' if applied else 'the vault takes publications again'
            if outage is None:
                self.send(f'{self.label}: {what}; waiting notes resume.')
                return
            hours = round((now - outage['since']) / 3600, 1)
            self.send(f'{self.label}: {what} after {hours} h; waiting notes resume.')
        except Exception as failure:  # noqa: BLE001 - Alerting must not turn a publication into a failure.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)
