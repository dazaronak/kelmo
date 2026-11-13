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

## Batched apply

With `[editor] batch_apply = true`, an `apply` pass holds one conversation with
the model per batch of waiting notes from one project instead of one per note. A
batch conversation takes up to 8 model calls. It is off by default.
`batch_max_sources` (default 8) caps how many notes go into one batch; the editor
budget usually decides the real size.

Each note in a batch still ends on its own: published with its own facts,
recorded as already stated (its reason names the fact keys that say so), refused
with its own error code, or handed back to the same pass to run alone. A note the
model's answer never mentions is not settled on the batch's word. The same goes
for a batch whose conversation ends without a proposal the editor can read: the
model answers `unresolved`, the rounds run out, the executor fails, the answer
stays malformed after its repairs, the model keeps asking for something the
editor cannot serve, or what the batch proposed is refused as a whole. Each note
then gets its own consultation in the same pass and spends none of its attempts
for the batch. A note that cannot be placed alone either is refused there, under
its own attempt count.

A batch never takes a note past an older note of the same project that it could
not consult. When another process holds a note, a note waits for a quota reset,
or its source cannot be read, the batch ends there and the notes captured after
it wait; older notes still take their turn. The same holds inside a batch: when
the model's answer leaves a note out, the batch publishes only the notes captured
before it, that note is consulted alone next, and the notes after it are
consulted again in the same pass.

A pass that dies in the middle of a batch loses no note and publishes none
twice. On the next pass a note the batch had committed is settled from that
commit, and any other note is settled from the answer the batch recorded or
asked again. This also holds when `batch_apply` was turned off in between.

```sh
kelmo --vault /srv/memory dream apply --ready --json
kelmo --vault /srv/memory dream status --json
```

`apply --ready` publishes only ready projects. A project is ready once
`[editor] batch_ready_sources` of its notes wait for their first pass; the
default is the value of `batch_max_sources`, and parked notes do not count. The
pass leaves a project once fewer notes than that remain, so a small project
waits for the next full pass. Without `batch_apply`, `--ready` is refused with
`E_CONFIG`. `dream status` reports `waiting` (first-pass notes per project) and
`ready` (the projects a ready pass would take now; empty while `batch_apply` is
off). It reads the journal and the registry file, without opening the store or
calling the model.

The daemon reads `[editor]` when it starts. After changing `batch_apply`,
`batch_max_sources` or `batch_ready_sources`, run `kelmo daemon stop` and
`kelmo daemon start`; until then its scheduled passes use the old values, while
`dream status` already shows the new `ready_at`.

Pause affects subsequent passes. It does not interrupt a running provider call.
Overlapping runs use one process lock, the daemon's own Dreaming job included: a
run that finds the lock held answers `busy`. Through the installed daemon
service, `dream apply` and `dream run` wait up to 15 minutes for the result. A
pass that takes longer keeps running in the service and the command answers
`running`; `kelmo daemon status` shows the job while it runs. Without the
service the command starts a daemon for the call and waits until the pass ends,
because that daemon stops when its caller leaves. A quota deferral persists its reset
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
