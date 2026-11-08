# Bounded Dreaming

Dreaming inspects newly archived evidence and related current pages. It records
contradictions, merge suggestions and split suggestions for review. Supported
links are published through the existing scoped editor. It cannot complete,
retire or reopen work based on age, or rewrite protected rules.

Configure the existing executor contract and ceilings:

```sh
kelmo --vault /srv/memory dream configure --executor-config /srv/config/executor.json --max-sources 5 --max-pages 8 --budget-tokens 24000 --json
kelmo --vault /srv/memory dream configure --executor-config /srv/config/executor.json --max-sources 5 --max-pages 8 --budget-tokens 24000 --apply --json
kelmo --vault /srv/memory dream status --json
```

Preview and status are read-only. First configuration starts paused and calls no
provider. Reconfiguration preserves the current pause state. These are ceilings,
not targets: an empty evidence window calls no model. A smaller packet is correct;
the runner does not pad it to fill a budget.

After explicitly enabling the work, run one pass:

```sh
kelmo --vault /srv/memory dream resume --json
kelmo --vault /srv/memory dream apply --json
kelmo --vault /srv/memory dream run --json
kelmo --vault /srv/memory dream findings --state open --json
kelmo --vault /srv/memory dream pause --json
```

`apply` publishes every waiting note through the scoped editor, now: notes held
by `[editor] mode = "nightly"`, notes parked after a refused attempt, notes that
waited for an executor. Each gets one attempt; the result lists the operation
ids applied, parked, failed, deferred or skipped. `run` does the same first, so
the findings pass reads the pages the notes changed, then runs the bounded
findings pass; its result carries the apply report under `apply`. Both honour
the pause and the quota deadline.

`apply` keeps capture order. It takes only notes captured before the pass began,
waits for an older create note still on its way to the editor (queued, or failed
on a cause the worker retries) and retries a note that met another writer's lock
in place before moving to newer ones. One wait budget of an hour covers the whole
pass; a note older than that is listed under `stalled` and the pass moves on.

Pause affects subsequent passes. It does not interrupt a running provider call.
Overlapping CLI runs use one process lock. A quota deferral persists its reset
time and later invocations wait without calling the provider again. Failed passes
return a nonzero exit status. The evidence cursor and findings remain durable.

For a shared authority, add `--server-profile /srv/config/server.json` to these
commands. A run or schedule also needs an enrolled `--client-id`; the client
cannot grant itself that identity over MCP.

## Prepare a schedule without activating it

```sh
kelmo --vault /srv/memory dream schedule --executable /absolute/runtime/bin/kelmo --directory /srv/review/dream-units --platform linux --interval 604800 --apply --json
```

This writes a systemd service and timer with verified backups. Use
`--platform darwin` to prepare a launchd plist. It never installs, loads, enables
or starts a scheduler. Install the reviewed files through the host's normal
service manager after a successful manual pass. Dreaming stays paused until the
explicit resume command; loading a schedule does not override the pause.
The interval is the operator's choice. It does not authorize unbounded provider
usage or a larger per-pass context.
