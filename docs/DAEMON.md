# Daemon and background work

One `kelmod` process per machine serves MCP sessions over a local HTTP
endpoint, hooks, the CLI and, on a vault, the background work. It starts
and supervises its cores as child processes: on a vault the state core (journal,
outbox and jobs), the archive core and the embedding core; on a shared client
one edge core. The user service runs it as `kelmo daemon run`; see
[setup](SETUP.md) for the service and `kelmo daemon status|stop|uninstall`.

Background work runs as jobs on the state core: one queue pass (`worker`), Dreaming (`dream`), vector
backfill, re-embedding and the graph rebuild. At most one job of a kind runs at
a time, and a job step yields to interactive writes. A job holds the journal's
writer only while it changes the journal. Model calls, waits for another lock and
a note's publication (rendering pages, checking the registry, the Git commit) run
without the writer, so a capture waits for the journal change in progress, not for
a whole publication. Publications still run one at a time. Server profiles use the same
persistent daemon and accept remote clients through the SSH gateway.
`kelmo worker daemon --vault PATH
--executor-config executor.json` runs the same queue pass in the foreground
(`--once` for one tick). Without executor configuration, ordinary queued work
runs and resolution requests remain pending.

The scheduler runs queue passes, Dreaming and vector backfill when `[embedding]`
is configured. Backfill starts five minutes after the scheduler, then ten minutes
after each run ends. Re-embedding and graph rebuild require explicit submission.
External backup and Git sync schedules remain operator-managed.

Dreaming runs daily and also applies when 20 first-pass notes are waiting.
A daily run that does not complete (its findings pass fails, or the core dies
under it) is tried again 15 minutes later, at most three times a day. A daily
run ended by the daemon's own stop or by a usage limit costs no try.
Scheduled apply waits 15 minutes after a pass, or two hours from its start if
none of its notes was published. A pass cut short by the daemon's own stop does
not count against progress. It resumes a minute after the stop while any of its
notes still waits, even when fewer notes wait than would start a new pass; the
third cut in a row waits the 15 minutes. A pass the daemon did not see end,
because the core died under it, waits 15 minutes from its start, or two hours if
none of its notes was published by then. Paused or deferred Dreaming waits until it can
run. A server fence holds all scheduled jobs until it is lifted: no job starts,
and a running backfill stops before its next batch. The daemon also refuses a job
submitted by hand and any command that changes the vault (`worker run`,
`queue retry`) with `E_FENCED`. It still answers `status`, `search` and other
reads. A Dreaming run that started before the fence finishes, so wait until
`kelmo daemon status` shows no active job before fencing for a copy. A vault
served by the daemon needs no separate Dreaming or backfill timer.

`kelmo daemon status --json` reports the jobs under `jobs`: `active`, the recent
outcomes, the last outcome and the last failure of each kind (the failure is kept
across restarts, with its end time), and `schedule` with the reason a job is held
(`paused`, `deferred`, `stopped`, `no_progress`, `min_interval` or `fenced`).

The trusted executor configuration is JSON with argv (an absolute executable followed by
arguments), timeout_seconds (default 120), max_attempts (default 3) and poll_seconds
(default 2). The executable reads one resolution payload as JSON on stdin and
returns one proposal JSON object on stdout. Use a provider-specific wrapper for
CLIs that have their own output envelope. The queued note cannot select commands.
The executor uses the user's environment for authentication and runs in the OS
temporary directory. This is not a sandbox or a restriction on its tool access.

Execution has a timeout and a one MiB stdout/stderr limit. Failure details are
reported as codes without printing provider stderr. The invocation's process
group is cleaned on every exit, including successful replies whose parent left
children running. Attempts and backoff persist in SQLite. Exhausted resolution work
becomes needs_review; ordinary transient failures stop retrying after the configured
attempt limit and remain visible as failed. Pending work survives daemon restarts.
SIGTERM/SIGINT stop after the current bounded tick. Model semantics and authentication
must be verified separately for the configured adapter.

A failed resolution attempt adds `failed_attempts` with operation/resolution IDs,
the error code and exhaustion state. A one-shot CLI tick then returns `ok=false`
and a nonzero exit code. The long-running daemon keeps polling; refunded busy
claims do not count as failed attempts. An ordinary source whose retry budget runs
out is settled as unresolved: its outcome reason names the cause, the facts it
carried and the number of attempts, and the tick reports `E_ATTEMPTS` in
`failed_attempts`. The operation stays visible as failed rather than disappearing
from the queue.

Contention is a wait, never a failure: a note or session job that meets another
writer (a held worker or dreaming lock, SQLite busy, a Git index or ref lock,
another attempt's claim as `E_BUSY` or `E_CLAIM`) keeps its attempt and is retried
after 60 seconds, however long the other writer holds it. Session jobs that wait
are reported under `busy_waits`. A busy journal does not stop the daemon; the
tick is skipped and the next one runs. `kelmo doctor` names a wait older than an
hour under `long_waits` and a Git lock older than ten minutes under
`stale_git_locks`, since a lock a crashed process left is never cleared here.

A rate-limited provider answers `{"outcome":"quota_wait","reset_at":EPOCH_SECONDS}`
instead of a proposal, where the reset time is at most 24 hours ahead. The request
returns to pending with its next attempt at that time and the claim is refunded, so
a quota wait never spends the failure budget. The tick reports it in `quota_waits`
with the resolution ID, operation ID and reset time, and not in `failed_attempts`.
A quota answer with no schedulable reset time is an executor failure.

The answer may name its reason: `"reason":"limit"` (a usage limit, the default) or
`"reason":"login"` (the provider login is lost; tried again every 15 minutes). The
built-in Claude adapter reads both from the Claude CLI's own API-error fields, never
from the model's text: a usage limit waits for the reset its message names. A lost
login needs a person, so the worker daemon alerts: once when the outage starts, once
a day while it lasts, and once when the provider answers again. The message goes to
the command in `kelmo.toml` `[alerts]` on stdin (`command`, an absolute argv that exits
0 once delivered; optional `label`, default "Kelmo"); the command owns the channel
and its secrets. Without `[alerts]` the message is a worker log line only. The outage
marker `login-outage.json` in the state directory keeps a restart from repeating or
dropping an alert; an undelivered alert is tried again on the next lost-login answer.
`examples/telegram-alert.py` is such a command for a Telegram chat:

```toml
[alerts]
command = ["/usr/bin/python3", "-I", "/opt/kelmo/telegram-alert.py", "/etc/kelmo/telegram.conf"]
label = "Server Kelmo"
```

Wiki/Git persistence errors use the same bounded retry path as other resolution
failures. A dirty managed page stays untouched, the operation remains visible,
and the daemon continues processing unrelated work. An invalid proposal is not
replayed as an accepted response; the next attempt invokes the executor again.

The native executor request allows eight MiB of JSON so an escaped one-MiB
source and its fact context fit without truncation. Source intake and executor
output retain their one-MiB limits.

Native adapters accept `kelmo executor --timeout SECONDS`. Setup uses 560 seconds
for the provider and 600 seconds for its wrapper (the daemon's ceiling), leaving
cleanup time: an editing round over a large context can run for minutes on a
small model, and a cap below the real duration turns every long round into
`E_EXECUTOR`. A failed executor's own refusal (its `--json` envelope on stdout, or
its last stderr line) is kept in the failure reason. A wrapper
termination forwards through provider cleanup; output and timeout failures remain
bounded even when the provider ignores SIGTERM. The timeout bounds this attempt,
not external work that a provider intentionally detaches from its process group.
