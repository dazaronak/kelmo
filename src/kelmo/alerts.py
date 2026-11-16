"""Tell the vault owner when work waits for a person: once when it starts, once a day while it lasts, and once
when it is over.

A usage limit needs no one: work waits for the reset by itself. A lost login waits for a person, and so
does a vault that takes no publication (`VaultWatch`): a prepared publication the recovery cannot finish,
uncommitted edits on the wiki's paths, a wiki over its budget. The message goes on stdin to the command
named in `[alerts]`; that command owns the channel and its secrets, so none of them reach Kelmo. A marker in
the state directory holds the outage start and the last delivered alert, so a restart neither repeats an
alert nor drops one; an undelivered alert is tried again on the next answer that shows the same outage.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime

REMINDER_SECONDS = 86400
MARKER = 'login-outage.json'
VAULT_MARKER = 'vault-outage.json'
EXECUTOR_MARKER = 'executor-identity.json'
DREAM_MARKER = 'dream-watch.json'
# How long the findings pass may answer idle while archived notes wait behind its cursor before the owner hears
# of it: the daemon's run is daily, so two runs a day apart.
DREAM_IDLE_SECONDS = 86400
# Refusals told by this process, for a state directory that cannot keep the record: {(file, command, work): time}.
_REFUSALS_TOLD = {}


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


class DreamWatch(LoginWatch):
    """What the owner hears of Dreaming runs (`core.jobs`): the daemon's daily run and those a timer starts.

    - `idle(waiting)`: the findings pass found nothing to read. With archived notes waiting behind its cursor
      (`waiting` > 0) an operation that never reached the archive holds the cursor, and nothing behind it is ever
      read: once that has lasted DREAM_IDLE_SECONDS the owner is told, then once a day while it lasts. With none
      waiting the vault is only quiet, and the run completed.
    - `failed(code, reason)`: a run failed. Told at once, then once a day for each code.
    - `ran()`: a run completed. Both records end; one recovery message goes out when anything was told.

    The marker keeps both records, so a restart neither repeats an alert nor drops one. As in `VaultWatch`, with
    no `[alerts]` command the journal line is the delivery, so it too comes once a day.
    """

    @classmethod
    def for_config(cls, config):
        settings = config.alerts or {}
        return cls(config.state / DREAM_MARKER, settings.get('command'), settings.get('label', 'Kelmo'))

    def saw(self, wait, now=None):
        raise TypeError('DreamWatch reads Dreaming run outcomes: use idle, failed or ran.')

    def _records(self):
        """{'stuck': {...} | None, 'failed': {...} | None}. A marker that cannot be read starts afresh: keeping it
        would make every later alert fail, which is the silence the watch exists to prevent."""
        empty = {'stuck': None, 'failed': None}
        if not self.marker.exists():
            return empty
        try:
            data = json.loads(self.marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        stuck, failed = data.get('stuck'), data.get('failed')
        if not (isinstance(stuck, dict) and isinstance(stuck.get('since'), (int, float))
                and (stuck.get('told') is None or isinstance(stuck.get('told'), (int, float)))):
            stuck = None
        if isinstance(failed, dict) and isinstance(failed.get('since'), (int, float)):
            told = failed.get('told') if isinstance(failed.get('told'), dict) else {}
            failed = {'since': failed['since'],
                      'told': {code: at for code, at in told.items()
                               if isinstance(code, str) and isinstance(at, (int, float))}}
        else:
            failed = None
        return {'stuck': stuck, 'failed': failed}

    def _keep(self, records):
        if records['stuck'] is None and records['failed'] is None:
            if self.marker.exists():
                self.marker.unlink()
            return
        self._save(records)

    def _guarded(self, work):
        try:
            work()
        except Exception as failure:  # noqa: BLE001 - Alerting must not turn a run into a failure.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def idle(self, waiting, now=None, since=None):
        """`since` (ISO 8601) is when the first waiting note reached the archive: the hold is at least that
        old, even when this is the first run to see it."""
        now = time.time() if now is None else now
        held = datetime.fromisoformat(since).timestamp() if since else now
        if not waiting:
            self.ran(now)
            return

        def work():
            records = self._records()
            told_failure = records['failed'] is not None and bool(records['failed']['told'])
            records['failed'] = None  # the run completed
            stuck = records['stuck'] or {'since': min(held, now), 'told': None}
            records['stuck'] = stuck
            last = stuck['told']
            if now - stuck['since'] >= DREAM_IDLE_SECONDS and (last is None or now - last >= REMINDER_SECONDS):
                text = (f'{self.label}: the findings pass has read nothing since {_when(stuck["since"])} while '
                        f'{waiting} archived notes wait behind its cursor: an operation that never reached the '
                        'archive holds it.')
                if last is not None:
                    text += f' Day {int((now - stuck["since"]) // REMINDER_SECONDS) + 1}.'
                if self.send(text) or not self.argv:
                    stuck['told'] = now
            self._keep(records)
            if told_failure:
                self.send(f'{self.label}: the Dreaming run completes again.')
        self._guarded(work)

    def failed(self, code, reason, now=None):
        now = time.time() if now is None else now

        def work():
            records = self._records()
            failed = records['failed'] or {'since': now, 'told': {}}
            records['failed'] = failed
            last = failed['told'].get(code)
            if last is not None and now - last < REMINDER_SECONDS:
                self._keep(records)
                return
            text = (f'{self.label}: the Dreaming run failed with {code}: '
                    + ' '.join(str(reason).split())[:300] + '. It is tried again later.')
            if last is not None:
                text += f' Failing since {_when(failed["since"])}.'
            # The alert goes before its record, so a state directory that cannot be written does not silence it.
            if self.send(text) or not self.argv:
                failed['told'] = {**failed['told'], code: now}
            self._keep(records)
        self._guarded(work)

    def ran(self, now=None):
        now = time.time() if now is None else now

        def work():
            if not self.marker.exists():
                return
            records = self._records()
            told = ((records['stuck'] is not None and records['stuck']['told'] is not None)
                    or (records['failed'] is not None and bool(records['failed']['told'])))
            # The marker goes first: one recovery message, even if its delivery fails.
            self.marker.unlink()
            if told:
                self.send(f'{self.label}: the Dreaming run completes again; the findings pass reads '
                          'new evidence.')
        self._guarded(work)


class ExecutorWatch(LoginWatch):
    """What each executor command said when it was asked which instructions it gives the model
    (`creation.instructions_standing`), kept in the state directory for `kelmo dream status` and for the
    next process, and what the owner is told about it.

    One file holds an entry for every command, by the digest of its argv: the worker's, the one sessions
    use and Dreaming's may be three different commands, and none of them answers for another.

    - `same`: the command names this build's instructions. Nothing is told, except that an earlier
      trouble with this command is over.
    - `differs`: it names other instructions. The work that would use them does not start (`refused`), and
      the owner is told once when that starts and once a day while it lasts, for each kind of work.
    - `unidentified`: it names none, or the question keeps failing. A `kelmo executor` older than 0.3.1
      answers the question as a job, so this is what an executor left behind by an upgrade looks like.
      Nothing is refused; the owner is told at once and reminded once a day until the command names its
      instructions, since one worker log line was all there was the time an executor stayed six days on
      another build.
    - `own`: a command that is not `kelmo executor` says its instructions are its own. Nothing is checked
      and no reminder is sent; one message ends an earlier trouble with the command.
    - `unasked`: every question to it has failed so far (`failed` counts them).
    """

    @classmethod
    def for_config(cls, config):
        settings = config.alerts or {}
        return cls(config.state / EXECUTOR_MARKER, settings.get('command'), settings.get('label', 'Kelmo'))

    @classmethod
    def for_service(cls, service):
        return cls.for_config(service.config)

    def saw(self, wait, now=None):
        raise TypeError('ExecutorWatch reads what an executor said of its instructions: use same, own, '
                        'differs, unidentified, failed or refused.')

    def entries(self):
        """Every command on record: {command digest: entry}. A file that cannot be read as one is none."""
        try:
            held = json.loads(self.marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        commands = held.get('commands') if isinstance(held, dict) else None
        if not isinstance(commands, dict):
            return {}
        return {command: entry for command, entry in commands.items()
                if isinstance(command, str) and isinstance(entry, dict)
                and entry.get('standing') in ('same', 'own', 'differs', 'unidentified', 'unasked')}

    def entry(self, command):
        return self.entries().get(command)

    def _keep(self, command, entry, read=None):
        """Write one command's entry and leave the others as they are. A state directory that cannot be
        written loses the record, never the alert or the refusal: both were decided before this. `read` is
        the entry the caller started from, when it only adds to it (a reminder time, a failure count): if
        another process recorded a newer answer meanwhile, that answer stays."""
        try:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            # Read, change and replace under one lock: two processes that write the entries of different
            # commands at once lose neither. The partial file is this process's own.
            with open(self.marker.with_name(self.marker.name + '.lock'), 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                commands = self.entries()
                current = commands.get(command)
                if (read is not None and current is not None
                        and (current.get('asked_at') or 0) > (read.get('asked_at') or 0)):
                    return
                commands[command] = entry
                partial = self.marker.with_name(f'{self.marker.name}.{os.getpid()}.partial')
                partial.write_text(json.dumps({'commands': commands}), encoding='utf-8')
                partial.replace(self.marker)
        except OSError as failure:
            print(json.dumps({'executor_record': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    @staticmethod
    def _due(last, now):
        """Whether a reminder is due: never told, a day ago or more, or told at a time still ahead of the
        clock (a clock that was set back must not silence a reminder)."""
        return not isinstance(last, (int, float)) or not 0 <= now - last < REMINDER_SECONDS

    def _over(self, before, name):
        """The message that ends an earlier trouble with this command, when the owner was told of one."""
        if before is None:
            return
        if before.get('standing') == 'unidentified' and before.get('alerted_at'):
            self.send(f'{self.label}: the executor command {name} now names the instructions it gives the '
                      'model.')
        elif before.get('standing') == 'differs' and before.get('refused'):
            self.send(f'{self.label}: the executor command {name} gives the model this build\'s instructions '
                      'again; the work that waited for it resumes.')

    def same(self, command, name, ours, version, now=None):
        now = time.time() if now is None else now
        try:
            before = self.entry(command)
            self._keep(command, {'standing': 'same', 'name': name, 'ours': ours, 'asked_at': now,
                                 'executor_version': version})
            self._over(before, name)
        except Exception as failure:  # noqa: BLE001 - A record must not turn a check into a failed pass.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def own(self, command, name, ours, now=None):
        now = time.time() if now is None else now
        try:
            before = self.entry(command)
            self._keep(command, {'standing': 'own', 'name': name, 'ours': ours, 'asked_at': now})
            if before is None:
                return
            if before.get('standing') == 'unidentified' and before.get('alerted_at'):
                self.send(f'{self.label}: the executor command {name} now says its instructions are its own; '
                          'they are not checked.')
            elif before.get('standing') == 'differs' and before.get('refused'):
                self.send(f'{self.label}: the executor command {name} now says its instructions are its own; '
                          'the work that waited for it resumes.')
        except Exception as failure:  # noqa: BLE001
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def differs(self, command, name, ours, version, reason, differing=(), now=None):
        now = time.time() if now is None else now
        try:
            before = self.entry(command)
            lasting = (before is not None and before.get('standing') == 'differs'
                       and before.get('ours') == ours)
            self._keep(command, {'standing': 'differs', 'name': name, 'ours': ours, 'asked_at': now,
                                 'executor_version': version, 'reason': reason,
                                 'differing': sorted(differing),
                                 'refused': before.get('refused') if lasting
                                 and isinstance(before.get('refused'), dict) else {}})
        except Exception as failure:  # noqa: BLE001
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def refused(self, command, name, work, reason, now=None):
        """`work` ('notes', 'the findings pass', 'session jobs', 'resolution requests') was kept from a
        command that gives the model other instructions for it. Told once when it starts and once a day
        while it lasts, for each kind of work. The decision is the caller's and was made before this: a
        record that cannot be read or written costs a repeated message, never a silent refusal."""
        now = time.time() if now is None else now
        try:
            entry = self.entry(command)
            if entry is not None and entry.get('standing') != 'differs':
                # A newer answer of this command is on record (another process asked after a repair): the
                # caller's memory of a difference is about to expire, and nothing is said over the record.
                return
            told = entry.get('refused') if entry is not None and isinstance(entry.get('refused'), dict) else {}
            # With no record to read (the state directory cannot be written) this process remembers what
            # it told, so a worker that looks for work every few seconds does not send on every look.
            mine = (str(self.marker), command, work)
            if not self._due(told.get(work, _REFUSALS_TOLD.get(mine) if entry is None else None), now):
                return
            text = (f'{self.label}: {work} wait for the executor command {name} and are not charged. '
                    + ' '.join(str(reason).split())[:400])
            if self.send(text) or not self.argv:
                _REFUSALS_TOLD[mine] = now
                if entry is not None:
                    self._keep(command, {**entry, 'refused': {**told, work: now}}, read=entry)
        except Exception as failure:  # noqa: BLE001 - Alerting must not turn a refusal into a failed tick.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def unidentified(self, command, name, ours, reason, now=None, asked=True, failures=0):
        """The command named no instructions. `asked=False` keeps the time of the last question: the answer
        on record was reused instead of asking again."""
        now = time.time() if now is None else now
        try:
            before = self.entry(command)
            lasting = (before is not None and before.get('standing') == 'unidentified'
                       and before.get('ours') == ours)
            entry = {'standing': 'unidentified', 'name': name, 'ours': ours, 'reason': reason,
                     'since': before['since'] if lasting and isinstance(before.get('since'), (int, float))
                     else now,
                     'asked_at': now if asked or not lasting else before.get('asked_at', now),
                     'alerted_at': before.get('alerted_at') if lasting else None,
                     'failures': failures if asked else (before or {}).get('failures', 0)}
            if self._due(entry['alerted_at'], now):
                text = (f'{self.label}: the executor command {name} does not name the instructions it gives '
                        f'the model (since {_when(entry["since"])}): {reason}. A `kelmo executor` older than '
                        '0.3.1 answers this way: if the command runs one from another install, bring that '
                        'install to this build, since notes are edited under its instructions meanwhile. '
                        'This is repeated once a day until the command names its instructions.')
                if lasting and entry['alerted_at']:
                    text += f' Day {int((now - entry["since"]) // REMINDER_SECONDS) + 1}.'
                if self.send(text) or not self.argv:
                    entry['alerted_at'] = now
            self._keep(command, entry, read=None if asked else before)
        except Exception as failure:  # noqa: BLE001 - Alerting must not turn a check into a failed pass.
            print(json.dumps({'alert_delivery': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)

    def failed(self, command, name, ours, now=None):
        """The question itself failed. Returns how many times in a row it has now failed for this command
        and this build (1 when no record can be kept)."""
        now = time.time() if now is None else now
        try:
            before = self.entry(command)
            again = before is not None and before.get('ours') == ours
            failures = (before.get('failures', 0) if again else 0) + 1
            entry = dict(before) if again else {'standing': 'unasked', 'name': name, 'ours': ours}
            self._keep(command, {**entry, 'failures': failures, 'failed_at': now}, read=before if again else None)
            return failures
        except Exception as failure:  # noqa: BLE001
            print(json.dumps({'executor_record': 'failed', 'error': type(failure).__name__}), file=sys.stderr,
                  flush=True)
            return 1
