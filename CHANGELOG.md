# Changelog

Every change a user can see, newest first. A version heading matches the tag that published it: `v0.2.0` is
`## [0.2.0]`. A feature reaches `main` through a merge commit whose message carries the same notes as its
entry here.

## [Unreleased]

### Added

- The owner is told when a Dreaming run fails, or when the findings pass keeps reading nothing while archived
  notes wait behind its cursor. Such a cursor is held by an operation that never reached the archive, and
  nothing behind it was ever read; before, both went to the worker log only. This covers the daemon's daily
  run and the runs of the timer `kelmo dream schedule` writes, and a daily run that cannot start because the
  scheduler's check before it fails (an unreadable `kelmo.toml`, say). The message goes to the `[alerts]`
  command once when it starts (for a held cursor, once the first waiting note has waited a day), once a day
  while it lasts, and once when a run completes again. The findings pass's idle answer now carries `waiting`,
  the number of archived notes behind the cursor, and with any waiting, `waiting_since`, when the first of
  them reached the archive.

## [0.3.1] - 2026-11-15

### Added

- The worker checks that the executor gives the model this build's instructions. `kelmo executor` builds
  the instructions and the answer schema from the Kelmo it is installed in, so an executor command that
  runs from another install and was left on another build made the model answer under other rules, with
  nothing reporting it. `kelmo executor` now answers the task `identify` with a digest of the instructions
  and schema it sends for each kind of job, without a model call. The worker asks before an apply pass
  consults its first note, before a note is edited at once under `mode = "immediate"`, before the findings
  pass of `dream run`, before a session job and before a resolution request, and compares only the digests
  of the jobs it is about to send. When one differs the work does not start, no model call is made and no
  attempt is charged: an apply pass stops with `E_EXECUTOR` under `blocked`, `dream run` answers `blocked`
  for its findings pass, and the owner is alerted once and then once a day. A command that stops answering
  the question after a difference was found stays refused. A command that names no instructions is let
  through, and the owner is told at once and reminded once a day until it does: a `kelmo executor` older
  than 0.3.1 answers the question as a job, which costs it one small model call an hour while there is
  work. A command that is not `kelmo executor` can answer that its instructions are its own
  (`docs/EXECUTORS.md`). `kelmo dream status` lists what each command last answered under `executors`. The
  digests do not cover the provider's arguments, the decoding of an answer, or a command behind
  `--provider custom`.
- `[editor] batch_budget_tokens` raises the budget of one batch conversation. A batch carries the pages and
  the reconciliation receipts of several notes next to the same person rules and project index, so it can
  be given more than one note's budget. Without the key, or with a value below it, a batch runs in the
  largest budget among its notes, as before.
- A batch says why it left a note unsettled. A note the batch answered and did not settle gets an
  `editor_batch` event with the reason, and the batch's first note gets an `editor_batch_answer` event with
  what each model call answered and what it was refused for. The pass report lists every note a batch left
  waiting under `unsettled`, with its reason, and the worker log gets one line per batch that left a note.
  Before, a batch that published nothing left no trace of the cause.
- `[editor] coverage = true` makes the editor account for every part of a note. Off by default. The note
  is cut into numbered units (sentences, table rows, code blocks) and sent with its text. The answer names,
  on each fact, narrative and page, the units it states, and lists every other unit under one of four
  reasons: an existing fact already states it, it only supports an item, it makes no claim, or it belongs
  to another project or on a page the budget left out. The editor checks each reason as far as code can: a
  unit said to be stated already has to share a number, path or code with the fact named for it, and a unit
  that names one is never accepted as making no claim. An answer that leaves a unit out is sent back with
  the unit numbers and the start of each unit's text. In a batch the model is asked only for those units
  and for the notes it gave no item, and the rest of its proposal is kept. A conversation gets two more
  rounds for this. The accounting never costs a note: when the repairs are spent, the proposal that only
  left units open is published, on the single path too. Each note's trace gets an `editor_coverage` event
  with what became of its units, including the numbers, paths and quotations its facts dropped. The pass
  report sums the published notes under `coverage`, and a pass that left units unaccounted for prints one
  line to the worker log. The checks bound the reason an answer gives, not what its facts say: only a
  second reading of the note against what was published checks that. Without the key the instructions and
  the answer's schema say nothing about units.

### Changed

- A batch whose notes together propose more than 30 facts is committed. A batch proposal may carry 30 facts
  for each of its notes, but the check that runs right before the commit applied one note's ceiling of 30
  to all of them together. Such a commit was refused as a whole (`E_UNION` with `E_FACT_CHECK`), the note
  that carried it was parked uncharged, the others waited behind it, and the next pass could end the same
  way. Kelmo 0.3.0 has this fault too. When that check refuses a fact for its key, text, status or
  revision, the refusal now names the cause instead of the general sentence about references and receipts.
- A batch is sized before its first model call. It takes notes in capture order while the budget still
  sends every page they selected and leaves room for their receipts (2 000 estimated tokens per unit of a
  note and at least 10 000 per note, at most a quarter of the budget). The notes that do not fit lead the
  next batch of the same pass. Before, a batch was cut only when nothing fit at all, and its receipts
  could push a selected page out of the conversation. Where the person rules, the project index and one
  note's pages already fill most of a note's budget, a batch without `batch_budget_tokens` shrinks to one
  note.
- The receipts of a batch can no longer push a page out: a reconcile round is allowed only into the room
  the pages leave, as for a single note.
- Reconciliation receipts may take up to half of a request's budget, for one note and for a batch. It was
  a quarter. A reconciled fact costs about 4 000 estimated tokens with its candidates, so a batch in a
  budget of 500 000 had room for about 31 new facts whatever the number of its notes, and the notes lost
  what did not fit.
- A reconcile round of a batch takes up to 30 requests per note of the batch, the same as its proposal may
  carry facts. Before, the round was cut to its first 30 requests without a word, and the facts of the
  newest notes got no receipt.
- A batch whose reconcile round passes the receipt ceiling is told to ask only for the oldest notes that
  fit and to leave the others out of its answer whole. Those notes are not asked for again in that
  conversation and run alone afterwards. Before, the refusal told the model to propose with the receipts
  it had, which meant dropping facts.
- A reconcile round over the receipt ceiling and an expand request the budget cannot serve no longer spend
  one of the batch's two repairs. The model is told why and answers again; a third such refusal ends the
  conversation. An expand request for a page the budget leaves out is refused with what the page needs,
  instead of being accepted and not served.
- When a batch proposal is refused only because it leaves notes without an item, the model is asked for
  those notes alone and its answer for them is added to the proposal; the items of the notes already
  answered stay as they are. Before, the answer replaced the proposal, so an answer that carried only the
  missing part lost everything proposed earlier and sent every note of the batch to run alone.
- A batch whose conversation ends on a later answer that was refused, or whose model answers `unresolved`
  once a proposal stands, settles the proposal that stood before it: the notes it answered are published and
  the ones it left out run alone.
- A note of a batch whose own page the budget leaves out is not asked for again in that conversation. It
  runs alone afterwards.
- The editor's instructions say what to send after a refusal: for a batch the whole proposal or only the
  part the refusal names, for a single note the whole proposal. They also say that a note of a batch which
  a newer note supersedes still needs an item of its own.
- Kelmo 0.3.0 refuses a `kelmo.toml` that names `batch_budget_tokens` or `coverage`, whatever their
  values. Remove both keys before going back to it.

## [0.3.0] - 2026-11-14

### Added

- `kelmo dream apply --ready` publishes only the projects that have at least `[editor] batch_ready_sources`
  notes waiting for their first pass. It needs `[editor] batch_apply = true`; without it the option is refused
  with a message that names the setting.
- `[editor] batch_ready_sources` sets how many waiting notes make a project ready. It defaults to
  `batch_max_sources`.
- `kelmo dream status` reports `waiting` (the first-pass notes of each project), `ready` and `ready_at`.
- An outage alert. When the vault cannot take a publication, the owner hears about it once when it starts,
  once a day while it lasts and once when it ends. It goes through the `[alerts]` command; without one, the
  worker's journal gets the same line once a day.

### Changed

- A vault that cannot take any publication no longer parks notes or spends their attempts. That covers
  uncommitted edits in the wiki, a commit that cannot be written, a wiki over its budget or not rendering as
  it stands, and a prepared publication the recovery cannot finish. The pass stops, every note keeps its
  place and its attempts, and the owner is alerted. Before, every note was charged and could fail for good
  after three passes.
- A note refused for a reason the vault caused (the wiki moved while the model was asked, the note's request
  changed, a batch commit was refused as a whole) holds back the newer notes of its project until it is
  asked again. A newer note no longer publishes ahead of it.
- A note whose publication was committed before a crash settles from that commit on its next turn. It is not
  asked again and not parked.
- A read that fails is tried again before it counts against a note, and a lock or a busy database never
  costs a note an attempt.
- `kelmo dream apply` through a running daemon answers `{"state": "running"}` when its wait ends before the
  pass does. The pass goes on in the daemon, and `kelmo daemon status` lists it under jobs.
- Batched apply (`[editor] batch_apply`, still off by default) now runs on the 0.2 daemon: one conversation
  for a batch of a project's waiting notes, each note settled on its own. A note the batch cannot settle goes
  through the single-note path in the same pass, uncharged.

Nothing was removed or renamed. Error codes keep their meaning, and no stored data needs a migration.

## [0.2.0] - 2026-11-13

- Kelmo runs as one Rust daemon per machine, `kelmod`, with single-owner cores.

## [0.1.1] - 2026-11-09

- A refusal the vault causes no longer spends a note's attempts, a retried note gets its editor attempts
  back, and a budget refusal names the pages it left out with their numbers.
- Batched apply behind `[editor] batch_apply`, off by default.

## [0.1.0] - 2026-11-08

- First public release.
