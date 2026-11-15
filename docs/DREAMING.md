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

## The executor must run this build

`kelmo executor` builds the model's instructions and the answer schema from the
Kelmo it is installed in, and the worker checks each answer against the rules of
its own. Usually both are one install. Where the executor command runs from
another one (another account, a wrapper with a runtime of its own), an upgrade
of the worker alone leaves the model answering under older rules, and nothing in
an answer shows it.

So the worker asks the executor command what it gives the model before work that
would use it. From 0.3.1 `kelmo executor` answers the task `identify` with its
version and, for each kind of job, a digest of the instructions and the answer
schema it sends, without calling a model. The question is put before an apply
pass consults its first note, before a note is edited at once under
`mode = "immediate"`, before the findings pass of `dream run`, before a session
job is started and before a resolution request is answered. Each of them
compares only the digests of the jobs it sends, so a release that changes
session extraction does not stop the editor, and the editor's accounting
instructions are compared only where `[editor] coverage` is on.

- When the digests are the worker's own, the work goes on. A different version
  with the same instructions is no difference. The answer is believed for ten
  minutes, so the command is not started for every note.
- When a digest differs, the work does not start: no model call is made and no
  attempt of a note, a session job or a resolution request is charged. An
  apply pass stops with `E_EXECUTOR` under `blocked`; the worker's tick leaves
  the notes it would edit where they are and still does what needs no model;
  `dream run` answers `blocked` for its findings pass, and the daemon tries that
  run again, up to three times that day; a session job and a resolution request
  go back unstarted. The owner is told once when it starts and once a day while
  it lasts, for each kind of work, and once more when the command gives this
  build's instructions again. Install this build where the executor command
  runs from, or bring the worker to the executor's build. The command is asked
  again a minute later.
- The command may name no instructions. A `kelmo executor` older than 0.3.1
  answers `identify` as a job, so this is what an executor left behind by an
  upgrade looks like. Nothing is refused, since a patch release must not stop a
  vault whose executor nobody touched. The owner is told at once and reminded
  once a day until the command names its instructions. The command is asked
  again once an hour while there is work; on an older Kelmo each question costs
  one small model call.
- The question itself may fail (a timeout, a non-zero exit, output that is not
  JSON). When a difference is already known for that command, the refusal
  stands: a command that stops answering was not repaired. Otherwise the work
  goes on, because only a known difference stops it. There is one line in the
  worker log, and the question is put again after a minute, then two, four, up
  to an hour. After three failures in a row the command counts as one that
  names no instructions, and the owner is told.

With an `[alerts]` command these messages go to it; without one each of them is
one line in the worker log. `kelmo dream status` lists what each executor command
last answered under `executors`: its name, `standing` (`same`, `differs`,
`unidentified`, `own`, or `unasked` for a command whose only questions so far
failed), when it was asked, the executor's version, the reason and the number
of questions that failed in a row. The worker's command, the one sessions use
and Dreaming's are separate entries when they are separate commands.

The digests cover the instructions and the schema `kelmo executor` sends. They
do not cover the provider's own arguments, the code that decodes an answer, or
what a command behind `--provider custom` adds or changes on its way to the
model: `kelmo executor` answers `identify` itself and that command is not asked.
A command that speaks the worker's protocol without `kelmo executor` says so
itself (`docs/EXECUTORS.md`, "Naming the instructions").

## Coverage

By default the editor decides what a note adds to the wiki and nothing records
what it left out. With `[editor] coverage = true` the answer has to say what
became of every part of the note. It is off by default and works the same for a
note alone and for a batch. Without the key the model's instructions and the
answer's schema say nothing about units, and an answer is read as before.

The note's text, without its frontmatter, is cut into numbered units: a
sentence, a table row, or a whole fenced code block. A semicolon ends a unit
only when what follows starts like a new sentence (a capital letter, a digit, a
quotation mark or code). A quotation, a bracketed remark and a code span are
never cut, unless the paragraph never closes the mark. The cut depends only on
the text, so the numbers are the same whenever the note is read, and nothing
about them is stored; each record carries the version of the cut (`cut`). The
request carries the units next to the text.

Each fact, narrative and page the answer proposes names the units it states.
Every other unit is listed with one of four reasons:

| Reason      | Meaning                                                          | What the editor checks                                                                                                                                                                                                                                                            |
| ----------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `stated`    | An existing fact or narrative already says it.                   | The named item is a fact or narrative, registered or proposed for the same note. It mentions a number, path or code the unit names; a unit that names none shares a word of five Latin letters or more with it, and one with no such word is accepted and listed as `unanchored`. |
| `evidence`  | It only supports an item, which reads complete without it.       | The named items are facts or narratives, registered or proposed for the same note.                                                                                                                                                                                                |
| `context`   | It makes no claim: a heading, a sentence that introduces a list. | The unit names no number, path or code and holds no quotation.                                                                                                                                                                                                                    |
| `elsewhere` | It belongs on a page of another project.                         | The name is another project, a page of another project, or a page the budget left out.                                                                                                                                                                                            |

A unit sent `elsewhere` to a page of the same project that the conversation was
not given is refused, and the editor sends that page with the next round, so the
model can state the unit there.

An item of another note of the batch never accounts for a unit: that note may be
published later or not at all. An item the proposal checks dropped accounts for
none either, unless it restates a registered fact.

An answer that leaves a unit in no item and under no reason is sent back with
the unit numbers and the start of each unit's text, as one of the conversation's
two repairs. A note alone sends its whole proposal again. A batch is asked only
for those units and for the notes it gave no item, and the rest of its proposal
is kept. A conversation gets two more model calls under coverage, 7 for a note
and 10 for a batch, because stating a unit takes a reconcile round and a
proposal. For a note alone, a reconcile round over the receipt ceiling spends no
repair under coverage, as in a batch; a third one ends the conversation.

The accounting never costs a note. A proposal with nothing wrong but units left
open stands while the model is asked for the rest, and a later answer replaces
it only when it leaves fewer units open. If the repairs or the rounds run out, a
later answer is refused for something else, the executor fails, the quota runs
out, or the model answers `unresolved`, that proposal is published and the units
still open are recorded. One case is as it was without coverage: when the wiki
moved on while the model answered, the answer is refused as stale and the note
is asked again on the next pass.

`kelmo trace` shows an `editor_coverage` event on the note for each answer about
it: the number of units, how many are stated by proposed items, how many are
under each reason with their unit numbers, and the units still `open`. Three
measures refuse nothing. `unanchored` lists units stated by items that keep none
of the numbers, paths and code the unit names, and units the code had nothing to
look for in (a sentence in another script than Latin with no number or path).
`thin` lists units whose items keep fewer than half. `dropped` lists each stated
unit with the numbers, paths, code and quotations its items do not keep, up to
six a unit. The pass report sums the notes it published under `coverage`, and a
pass that left units open prints one line to the worker log.

The checks bound the label an answer gives a unit, not what its item says. The
units an item names are the model's own statement, and whether an `evidence`
unit really is only evidence is its judgement. The record names those units by
number, so a person can read exactly them; an independent reading of the note
against the published items is the only check of the content itself.
A note the model declines whole with `unresolved` ends `unresolved` with the
model's reason, as before, and is recorded with every unit open.

## Batched apply

With `[editor] batch_apply = true`, the pass over waiting notes (`dream apply`,
and the first part of `dream run`) holds one conversation with the model per
batch of waiting notes from one project instead of one per note. A batch
conversation takes up to 8 model calls. It is off by default.
`batch_max_sources` (default 8) caps how many notes go into one batch; the
budget decides the real size.

`batch_budget_tokens` raises the budget of one batch conversation. A batch
carries the pages and the reconciliation receipts of several notes next to the
same person rules and project index, so a vault with large pages can give it
more than one note's budget. A batch never runs in less than the largest budget
among its notes: without the key, or with a smaller value, it runs in that.

Kelmo 0.3.0 refuses a `kelmo.toml` that names `batch_budget_tokens` or
`coverage`, whatever their values. Remove both keys before going back to it.

A batch is sized before its first model call. It takes notes in capture order
while the budget still sends every page they selected and leaves room for their
receipts: 2 000 estimated tokens per unit of a note (see Coverage below for
units) and at least 10 000 per note, at most a quarter of the budget. The notes
that do not fit lead the next batch of the same pass. Receipts are allowed only
into the room the pages leave, and never past half of the budget, so they never
push a page out of the conversation. A reconciled fact costs about 4 000
estimated tokens with its candidates, and a reconcile round takes up to 30
requests per note of the batch. Where the person rules, the project index and one note's pages
already fill most of a note's budget, a batch without `batch_budget_tokens`
shrinks to one note.

Each note in a batch still ends on its own: published with its own facts,
recorded as already stated (its reason names the fact keys that say so), refused
with its own error code, or handed back to the same pass to run alone. The
batch's word settles neither a note the model's answer never mentions nor a note
the answer gives something an earlier note of the batch also claims (the same
fact, page or work page). The second kind is asked alone after that earlier
note, and the notes captured after it wait for it.

A batch whose conversation ends without a proposal the editor can read is
handled the same way. That happens when the model answers `unresolved`, the
rounds run out, the executor fails, the answer stays malformed after its
repairs, the model keeps asking for something the editor cannot serve, the pages
and receipts it gathered no longer fit beside even the oldest note, the editor
refuses what the batch proposed as a whole (the notes together exceed the wiki
budget, for example), or the editor itself fails on the batch. Each note then
gets its own consultation in the same pass and spends none of its attempts for
the batch. A note that cannot be placed alone either is refused there, under its
own attempt count.

A proposal refused only because it leaves notes without an item is kept. The
model is asked for those notes alone, and what it answers for them is added to
the proposal; the items of the notes already answered stay as they are. If the
conversation ends on a later answer that was refused, or the model then answers
`unresolved`, the batch settles the proposal that stood before it: the notes it
answered are published, and the ones it left out run alone. A
reconcile round over the receipt ceiling, or a request for a page the budget
cannot send, is refused with the reason and costs the batch none of its two
repairs; a third such refusal ends the conversation. A round over the receipt
ceiling tells the model to ask only for the oldest notes that fit and to leave
the others out of its answer whole: thinning every note to fit loses what is
dropped, and a note left out whole loses nothing. A note left out that way, and
a note whose own page the budget leaves out, is not asked for again in that
conversation and runs alone afterwards.

A note whose batch answered and did not settle it says why. `kelmo trace` shows
an `editor_batch` event on it with the reason, and the first note of the batch
carries `editor_batch_answer`: the model calls of the conversation, what each
one answered and what it was refused for. The pass report lists every note a
batch left waiting under `unsettled`, with its reason, including the notes the
batch was sized without. The worker log gets one line for each batch that left
a note.

A batch commit that is refused, right away or by a later recovery, is handled
the same way too: that refusal (`E_UNION`) names no note, so no note spends an
attempt. The note whose row carried the commit is asked again alone on the next
pass, and the notes captured after it wait for it.

A batch never takes a note past an older note of the same project that it could
not consult. When another process holds a note, a note waits for a quota reset,
or its archived source cannot be used (it no longer matches its receipt, for
example), the batch ends there and the notes captured after it wait; older
notes still take their turn. A note whose own stored request can no longer be
used also ends the batch before it, and the notes after it follow in the same
pass. If a page it selected is gone, the note is refused on its own turn and
spends one of its attempts. If its settings changed after an earlier attempt
opened it (a raised budget, a renamed project), it spends no attempt and is
asked again under the new settings on the next pass.

A note refused for a reason the vault caused (its settings changed, the wiki
moved on while the model answered, its batch's commit was refused whole) holds
the notes of its project captured after it for the rest of the pass, on both
paths, because its claim is still to come. Inside a batch the rule is the same:
when the model's answer leaves a note out, the batch publishes only the notes
captured before it, that note is consulted alone next, and the notes after it
are consulted again in the same pass.

A pass that dies in the middle of a batch loses no note and publishes none
twice. On the next pass a note the batch had committed is settled from that
commit, with no model call. Every other note of the batch is asked again and
spends none of its attempts for the pass that died. This also holds when
`batch_apply` was turned off in between. A committed note is settled before
anything else about it is checked, so a change made since (its origin revoked,
its stored request no longer readable) cannot refuse it or leave it waiting.
The same holds for a note published alone whose pass died before recording it.

Some failures stop every publication, not one note: a commit that cannot be
written, a prepared publication the recovery cannot finish, uncommitted edits
in the wiki's files, a working registry or committed page that cannot be read,
an archive the storage cannot read, a working registry that differs from the
last commit, or a wiki that is over its budget or does not render as it stands.
A note never pays for one of them, batched or not.

A read of the wiki or of the archive that fails is tried again. It counts
against the note only when the wiki or the archive as a whole reads and that
note's read still fails. A note whose captured source or archived record alone
cannot be read, or reads but is damaged (a record that does not parse, a file
that is no longer text), is refused for it and spends one attempt, and the other
notes go on.

The pass stops at the first failure that stops every publication and names it
under `blocked` (the note, the code, the reason). Every note keeps its place and
its attempts, a note that published before the failure stays applied, and the
next pass publishes a commit that stayed prepared with no model call. The
recovery and the working tree are checked before the model is asked, and a batch
also checks first that the wiki renders and fits its budget. A note asked alone
learns whether the wiki renders and fits only after the model answers. It keeps
the answer, and the first pass after the repair publishes it without asking
again. Either way, a vault that stays broken costs no conversation on later
passes.

With `[alerts]` set, the owner hears of it once when it starts, once a day while
it lasts, and once when it is over: when a note is published again, or when a
pass reads the vault taking a publication and stops at nothing. A kind of
failure not yet told that day is told when it is met. Each kind keeps its own
daily reminder, so failures that alternate are not told on every pass. The alert
goes out even when its record cannot be written. The worker in `immediate` mode
stops its tick the same way.

```sh
kelmo --vault /srv/memory dream apply --ready --json
kelmo --vault /srv/memory dream status --json
```

`apply --ready` publishes only ready projects. A project is ready once
`[editor] batch_ready_sources` of its notes wait for their first pass; the
default is the value of `batch_max_sources`, and parked notes do not count. The
pass leaves a project once fewer notes than that remain, so a small project
waits for the next full pass. A note its batch sent to run alone still gets that
turn in the same pass, unless an older note of the project waits ahead of it. Without `batch_apply`, `--ready` is refused with
`E_CONFIG`. `dream status` reports `waiting` (first-pass notes per project) and
`ready` (the projects a ready pass would take now; empty while `batch_apply` is
off). It reads the journal and the registry file, without opening the store or
calling the model.

The daemon reads `[editor]` when it starts. After changing `batch_apply`,
`batch_max_sources`, `batch_ready_sources`, `batch_budget_tokens` or `coverage`, run
`kelmo daemon stop` and
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
