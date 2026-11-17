# Changelog

Every change a user can see, newest first. A version heading matches the tag that published it: `v0.2.0` is
`## [0.2.0]`. A feature reaches `main` through a merge commit whose message carries the same notes as its
entry here.

## [Unreleased]

### Fixed

- A resolution request whose model call the core's stop ended is given back at once, without being
  charged an attempt. It used to stay claimed until its lease ran out, with the attempt counted. If the
  journal cannot be written at that moment, the request still waits out its lease and the attempt counts,
  as before; the stop is reported either way.
- Reading one operation from the journal no longer scans every event ever recorded. The journal gets an index
  on its events by operation and stage the first time a writer opens it; on a synthetic journal of 20,000
  operations and 160,000 events, reading one operation went from about 5.7 ms to 0.02 ms. Commands and passes
  that read many operations, such as the findings pass, the queue and the worker, gain the most. Building the
  index on a large journal takes a moment once; nothing else changes.
- `memory_search` and `memory_list` no longer scan every operation for each record they return. Each row looks up
  the record's operations by its archive id, and the journal now indexes that column too; on 20,000 operations
  one lookup went from about 1 ms to 0.006 ms, about 0.5 s saved on a 500-item `memory_list` page.
- A core that receives bytes from `kelmod` that are not a frame (a length over the frame bound, a header that
  does not parse or has a field of the wrong JSON type, a body nested too deeply) now ends its channel the
  way a closed channel ends: it says why in `kelmod`'s log and asks
  its work to stop. The error used to escape the channel's loop as a Python traceback.
- `kelmod call --deadline-ms` now also bounds the part before the call is sent. A daemon that accepted the
  connection and then never answered the hello, or never read the call, left `kelmod` waiting with no
  output; the connection, the hello and the write of the call now end with `E_DEADLINE` five seconds after
  the deadline, retryable when the call was never sent or carries a key. The wait for the reply is
  unchanged: the deadline plus five seconds, counted from the moment the call was written.
- `kelmod call` and `kelmod status` connect only through a runtime directory that belongs to the user and
  is closed to everyone else, as the MCP gateway already did. Through a directory open to others, or a
  symlink, they refuse with `E_RUNTIME` before anything is sent; a directory that is not there, an empty
  `--runtime-dir` included, is a retryable `E_TRANSPORT` without a connect. After the connect they also check
  that the daemon runs under the same user, and refuse with `E_RUNTIME` before the hello if not, so a
  directory swapped in after the check cannot take the call. Without `XDG_RUNTIME_DIR` and `TMPDIR` the
  directory is under `/tmp`, where another local user could have created it first.
- `kelmo dream` on a shared-client machine is refused the way other server commands are: `E_CAPABILITY`, "runs
  on the server", where `dream status` and the other local actions used to answer `E_NOT_INITIALIZED` as if the
  vault were missing. `dream run` and `dream apply` are no longer sent to the client's daemon, which has no dream
  job; they get the same refusal.
- Tool calls that write through the edge core now take the outbox in the order they came. A new call could
  take it ahead of an earlier one that had just been woken, and a busy session could keep another session's
  write waiting until its deadline. Hooks still go ahead of tool calls, but never twice in a row past a
  waiting tool call, and hooks among themselves also go in the order they came. A caller whose wait ran out
  holds nobody back and no longer takes the lock if its turn comes after that.
- Dreaming's findings pass skips a record that was forgotten after it was archived and moves past it.
  It used to fail with `E_FORGOTTEN` on such a record, and since the cursor moves only after a
  successful pass, every later pass failed on the same record. The run lists it in `skipped` with
  `"reason": "forgotten"`.
- A session note forgotten while the session worker waits for its reconcile call is no longer published: the
  job ends `wiki_unchanged`. So is one forgotten after an attempt that got the editor's answer but stopped
  before its publication was prepared; that request is refused. A session note whose publication was already
  prepared is finished as before, and a replay of a finished one still makes no second commit.
- A promote note whose editor call fails because the core is stopping now waits for the next start
  without being charged an attempt, as a stop met earlier in the same run already did, also when the editor
  answers busy. It used to be staged failed and charged: retried later for a transient code, needing a
  manual retry otherwise.
- A sandboxed core that woke its channel reader after the channel was closed wrote a byte into whatever
  file had since been given the freed descriptor number. The wake now does nothing once the channel is
  closed.
- A resolution request whose model call the core's stop ended is given back at once, without being
  charged an attempt. It used to stay claimed until its lease ran out, with the attempt counted. A journal
  still busy after its own wait is tried again, at most twice and only while the stop leaves room for
  another wait. If it still cannot be written, the request waits out its lease and the attempt counts, as
  before; the stop is reported either way.
- The edge core's status no longer gets ahead of a hook waiting for the outbox. Status read the outbox
  only after a zero-timeout turn at the owner lock, and right after a hook had held it, that turn could
  come before the next hook's. It now reads the outbox only while nobody holds the lock or waits for it,
  and waits at most half a second for another thread already reading it. Otherwise it answers at once with
  `"outbox": {"busy": true}`, where it used to answer `{"error": "The edge core is busy ..."}`, the same
  shape as a failed read. A failed read still answers `{"error": ...}`.
- On an edge machine `kelmo status` and `kelmo client status` no longer hold the outbox while they wait for
  the authority. A hook that arrived meanwhile waited for the whole command and could hand its session a
  cached context marked unavailable; it now gets the outbox at once. `kelmo client status --probe` checks the
  authority on a channel of its own and closes it, so it no longer replaces the channel the client's requests
  use. `kelmo status` reads the authority over the open kelmo/2 channel, which carries several requests at
  once, or else over a channel of its own, so a delivery never waits for its reply or its hello. `kelmo client
  flush`, `resend` and the other commands (`trace`, `show`) still hold the outbox for the whole command, and
  the tools' reads keep it while they wait on a channel that carries one request at a time, so a hook queued
  meanwhile goes before a delivery. A delivery that needs the channel while another caller is still connecting
  gives up at its own deadline, with its writes kept, instead of waiting for that caller's hello; such a
  delivery, one that gave up because another request held the channel, one whose deadline passed before its
  request went out, and one refused before a byte went out (an operation the server does not offer, a frame
  over the limit) no longer counts as an attempt; the busy and late ones no longer show a refusal as the
  entry's last. When the authority's hello, the delivery's own or another caller's, does not answer in time,
  an entry with no refusal yet shows `E_TRANSPORT` as the reason it waits. The stall warning names that reason
  as the "last reason". On an edge machine, while another caller is reading or rewriting the outbox, or
  another process holds its file, `kelmo status`, `kelmo client status` and the MCP tool `memory_status`
  answer with `"outbox": {"busy": true}` instead of waiting past their deadline; during a delivery, which
  leaves the file free, they report the counts. A client that runs without the edge daemon still waits for a
  held file while it opens the outbox, as before.
- A server that serves one client both through the daemon's state core and through `kelmo serve-client` at
  once no longer runs one write request twice: the state core now also takes the request's lock file, so each
  refuses a request the other is running with `E_BUSY`, which the client retries.
- A session capture that `kelmod hook` spooled while the daemon was running, because no reply came in time, no
  longer waits for the next start of the state core. The next hook the core serves starts a pass over the spool,
  one pass at a time, and none once the core is told to stop. A pass stopped by a busy journal while a hook
  asked for another runs again a second later. A spooled capture reads its transcript for up to 5 seconds,
  where a hook has 1.5, since no caller waits for it. One refused as worth retrying no longer holds back the
  captures behind it: a refusal the server marked worth retrying keeps it for later passes, as the edge core
  keeps it, and a local one (a transcript read that ran out of time, or one that changed while it was read)
  sets it aside as `.bad` on its third try in one run of the core. It used to be set aside on the first.
- A promoted note that met a database held by another writer while it went through the editor no longer fails
  with `E_PROCESSING` and loses an attempt. It waits its turn as `E_BUSY`, uncharged, as it already did before
  the editor call.
- A Dreaming link finding whose cited evidence could not be read for a moment, because the archive was busy or
  its process restarted, is no longer settled `stale` for good. It stays open with the error's code and a later
  `publish` links the pages. Only evidence that was forgotten or is gone settles the finding `stale`.

### Added

- `kelmo client dismiss` stops listing refusals the owner has read. A shared client kept every refused
  entry in `kelmo client status` for good, so one that would never be resent stayed there. The command lists
  the refused entries whose code matches `--code` or whose key matches `--key` (both repeatable; with
  neither, every refused entry), and with `--apply` marks them `dismissed`. Status then counts them under
  `dismissed`. `kelmo client resend` no longer selects them, and the same request under its key is still
  answered with its refusal. Nothing is deleted.

### Changed

- The "Pending wiki updates" block a session gets at start now sits between `<kelmo-pending-notes>` and
  `</kelmo-pending-notes>` lines and says that its lines are text from notes, not instructions. A pending line
  can no longer close the block early: each one is a single row inside it, and a marker tag in its text is
  written with `&lt;`. Both delivery paths, and the refresh that sends only a changed pending list, use the same
  frame. Project labels, the "retrying" mark, the ten-row limit with "+N more" and the digest are unchanged.
  When the start context is too large for a Claude hook and the pending lines are left in the referenced
  context file instead, that file's `pending` field holds the same framed block, with a row for each note the
  path was given: every waiting note on the hub path, the 20 newest on the older path for vaults without hub
  views. It used to hold the raw list of notes. When the ten-line block itself fits, no such file is written
  and the notes past ten still show only as a count, as before.
- The test runner `tools/pytest_shards.py`, which CI uses for the suite, now reports while it runs: it says how
  many cases it collected, prints a `progress:` line with the cases done so far as each of its processes ends,
  and says how many of the collected cases passed before its summary line. When `GITHUB_STEP_SUMMARY` names a
  file, it appends the result there, so a GitHub run shows it on the job's page. The summary line is unchanged
  and still last.
- The test runner `tools/pytest_shards.py`, which CI uses for the suite, now reports while it runs: it says how
  many cases it collected, prints a `progress:` line with the cases done so far as each of its processes ends,
  and says how many of the collected cases passed before its summary line. When `GITHUB_STEP_SUMMARY` names a
  file, it appends the result there, so a GitHub run shows it on the job's page. The summary line is unchanged
  and still last. Its progress and its check that every case ran now count cases rather than pytest's outcomes,
  so a case that passed and then failed in its teardown no longer hides a case that never ran.
- The test runner `tools/pytest_shards.py`, which CI uses for the suite, now reports while it runs: it says how
  many cases it collected, prints a `progress:` line with the cases done so far as each of its processes ends,
  and says how many of the collected cases passed before its summary line. When `GITHUB_STEP_SUMMARY` names a
  file, it appends the result there, so a GitHub run shows it on the job's page. The summary line is unchanged
  and still last. Its progress and its check that every case ran now count cases rather than pytest's outcomes,
  so a case that passed and then failed in its teardown no longer hides a case that never ran. The
  measurement gate (`bash run-tests.sh`) shows those collected and progress lines as they come, while the
  source suite still runs, instead of only after the section ended.
- A request submitted again under the key of a refusal that `kelmo client resend` sent again is answered as
  the note it went out as: with that note's outcome, its refusal, or as queued under its new key. Before, the
  old key always answered that the request was queued and would be delivered on the next contact, even after
  the resent note had been delivered or refused. A client that no longer holds the old entry still gets the
  old refusal from the server.
- `kelmo client resend` counts a refusal as a duplicate only of a queued or delivered note with the same
  request: the same project, text, rule change, new work page and pending flag. Before, a note of any project
  with that text answered for it, whatever its other arguments.
- A shared client removes its old HOT sets. Each publication installed a new set of HOT files under the
  client's `hot/` and none was ever removed, so the directory grew by every generation. When it installs a
  set, the client now removes, at most once a day, the sets older than two days. It keeps the eight newest
  whatever their age (the current set among them), every set a cached context names, and every set a session's
  delivery gave a path into while that session's record is younger than 30 days; a set installed again counts
  as new. A local vault that saves a copy for a cut context sweeps its sets the same way. An offline delivery
  whose set was removed says that no cached generation is left.
- A shared client's outbox gives its free space back again. Settled transcript pieces drop their bytes but
  leave their pages in `outbox.sqlite3`; since Kelmo 0.2.0 the edge daemon owns the outbox and only `kelmo
  client flush` compacted it, so the file only grew. A new outbox now gives pages back in small steps (SQLite
  incremental vacuum): a delivery pass that leaves nothing to send frees them 256 pages at a time as soon as
  that many are free, and stops at once for a hook that waits for the outbox, a hook that queued something, or
  another process that holds the file; the next pass a second later goes on. An outbox made by an earlier
  release is rewritten once into that mode, on the first pass with nothing to send, when its rewrite is
  predicted to take under a second; a rewrite whose copy phase runs past a quarter of a second is stopped and
  rolled back, and the file stays as it was. A larger one is rewritten once the daemon has served no call for
  ten minutes, and that rewrite is stopped and rolled back as soon as a hook waits for the outbox; it is
  looked at again as soon as such a quiet spell can have passed. An outbox whose bounded rewrite was stopped
  waits for such a quiet spell too. A step that frees nothing and a failure (a full disk, say) wait an hour
  before the next try, also across a restart of the daemon (`compaction.json` next to the outbox, readable by
  its owner only; a saved pause never waits longer than an hour, and a running daemon times its pauses on a
  clock that is never set back, so a wall clock set back does not stretch them). A rewrite refused because
  another process was reading the file waits a hundred times as long as that try took, an hour at most. A
  session event Kelmo skips (a headless run, Kelmo's own job) is left out of the skip records when the outbox
  stays busy for half a second. The daemon's status shows the last outcome under `compaction`. Under the
  daemon `kelmo client flush` no longer rewrites the file and reports `compacted: 0`; a client run on its own
  still compacts there, unless another process holds the file, and leaves it to the next flush then. `kelmo
  client flush` reports `pending: null` when another caller kept the outbox for its whole bound, and the
  daemon looks at a held outbox again a second later. `kelmo client status` and `kelmo status` report the
  outbox busy instead of waiting for it past their deadline.
- A delivery pass no longer waits a minute for another process that holds the outbox file, or for another
  thread in the outbox. Before it sends an entry, its reads and the count of the attempt wait no longer than
  the pass's deadline; then the pass ends with `stopped: E_BUSY`, the entry stays pending and counts no
  attempt. The code of a retryable answer is noted only while the file is free in time. An answer the server
  already gave is recorded if the file frees within the pass's deadline; otherwise the entry stays pending and
  the next pass gets the same answer again under its key, with no second effect on the server. A refusal of a
  transcript piece's base and the reset of that transcript's sent position are recorded together; the
  transcript goes again from its start with the session's next event. A session start whose writes were held
  back this way gets the cached context, as when the server cannot be reached.

## [0.3.2] - 2026-11-16

### Added

- The owner is told when a Dreaming run fails, or when the findings pass keeps reading nothing while archived
  notes it has not read wait behind its cursor; before, both went to the worker log only. This covers the
  daemon's daily run and the runs of the timer `kelmo dream schedule` writes, and a daily run that cannot
  start because the daemon's check before it fails (an unreadable `kelmo.toml`, say) while no run is under
  way. A timer run that never reaches Kelmo is not heard and its error stays in the timer's output: its
  `kelmo.toml` cannot be read, so there is no `[alerts]` command to use, or its command goes to an installed
  daemon service that is stopped. A run whose process died, killed or out of memory, is told when the next run
  starts. The message goes to the `[alerts]` command once when it starts (for notes waiting unread, once the
  first waiting note has waited a day), once a calendar day while it lasts, even after the clock was set back,
  and once when a run completes again. An alert the command fails to take is sent again with the next failure
  or idle answer: while it runs, the daemon waits at least 15 minutes between such tries (a restarted daemon
  may try at once), a timer tries once in each of its runs. A run that completes after an alert that never
  went out says in its one message what was missed. An alert this process sent is not sent again the same day
  even when the state directory cannot keep the record. The findings pass's idle answer now carries `waiting`,
  the number of archived notes behind the cursor that it has not read, and with any waiting, `waiting_since`,
  when the first of them reached the archive. Since the findings pass now reads notes in the order they
  reached the archive (see Changed), a note still on its way no longer holds its cursor and `waiting` stays 0;
  that alert is a guard for notes left unread anyway.

### Fixed

- `kelmo worker daemon` no longer stops with `KeyError: 'operation_id'` when the executor check sends a
  resolution request back. The loop takes its next pass, and the request stays pending with its attempts. The
  line about operations held by another attempt now names only operations skipped as busy or claimed, never a
  resolution request or a note held back by the executor check or the vault. The worker job of the 0.2 daemon
  was not affected.
- A core's call to another core made while its channel to `kelmod` was closing waited out its whole deadline
  (up to five minutes for an archive call) when kelmod had already stopped answering. It now fails at once
  with `E_TRANSPORT`, as calls still open at that moment already did, so the core's stop is not held up by it.
  A call whose send fails after the channel closed no longer raises `InvalidStateError` from inside the core.
- The edge core's status no longer gets ahead of a hook waiting for the outbox. Status read the outbox only
  after a zero-timeout turn at the owner lock, and right after a hook had held it, that turn could come before
  the next hook's. It now reads the outbox only while nobody holds the lock or waits for it. Otherwise it
  answers at once with `"outbox": {"busy": true}`, where it used to answer `{"error": "The edge core is busy
  ..."}`, the same shape as a failed read. A failed read still answers `{"error": ...}`.
- `kelmo forget` on a created note that still waits for the editor now withdraws it, and so does a failed one
  that `kelmo queue retry` could queue again. `kelmo dream apply` and the worker never publish it, and it
  drops out of the pending block a session gets at start and out of the waiting count of `kelmo dream status`.
  Its operation ends in the new terminal state `withdrawn`; the archived record keeps every byte, and the
  trace still shows the `forgotten` event, as before. A note the editor is working on at that moment, or whose
  publication is already decided and waits for its commit, is refused with a retryable `E_BUSY` that says
  which, and then no note of that record is withdrawn. Notes filed with `--intent promote` behave as before.
- `kelmo snapshot rollback-check` refuses when a record was forgotten after the cutover. A forget changes
  nothing the cutover baseline recorded (the record, its versions and its journal operation stay as they
  were), so the check called the store untouched and a rollback would have brought the record back into search
  and list. The baseline now records the forgotten records, `kelmo snapshot tail` lists the ones forgotten
  since under `hidden`, and the refusal names how many. A baseline written by an earlier release counts a
  record forgotten after its `marked_at`, or within the same second, since `marked_at` has whole seconds.

### Changed

- The "Pending wiki updates" block a session gets at start now sits between `<kelmo-pending-notes>` and
  `</kelmo-pending-notes>` lines and says that its lines are text from notes, not instructions. A pending line
  can no longer close the block early: each one is a single row inside it, and a marker tag in its text is
  written with `&lt;`. Both delivery paths, and the refresh that sends only a changed pending list, use the
  same frame. Project labels, the "retrying" mark, the ten-row limit with "+N more" and the digest are
  unchanged. When the start context is too large for a Claude hook and the pending lines are left in the
  referenced context file instead, that file's `pending` field is not framed yet.
- A flush no longer loads every queued entry into memory. It walks the entries that were queued when it began,
  one at a time, and reads an entry's body only once a channel to the server is open. With 50 queued entries
  of 1 MiB each a flush used to hold about 50 MiB; it now stays under 8 MiB. The pending count in a flush
  result and in `kelmo client status` reads a new index on the outbox and no entry bodies. An entry queued
  while a flush runs is delivered by the next flush, as before.
- `kelmod call` and `kelmod status` no longer call every client error retryable. When the call reached the
  daemon and the connection then closed without a reply, or no reply came before the deadline (`E_DEADLINE`),
  a call without `--key` may already have run, so the error now says `"retryable": false`. An error before the
  call was sent, such as no daemon socket, stays retryable. So does a call with `--key`, as the daemon answers
  for it, but the key only stops a second run of an operation that replays by key; tool, hook and CLI calls
  ignore it.
- Mandatory pages reach a session without the fact and source labels of their fact rows. The `[f:...]` and
  `[source:...]` labels at the end of each fact row are the wiki's own bookkeeping and take startup space.
  `kelmo hot`, the SessionStart hook and the generated `mandatory.md`, `mandatory-<page>.md` and
  `<project>.full.md` files now leave them out; a status label such as `[status: shipped]` stays, and so does
  label-shaped text inside a fact's text or elsewhere on the page. `graph_read` and the published page keep
  the labels. Pages selected through `[hot] pages` that are not mandatory are delivered as before. A store
  with generated views keeps delivering the files it has until its next publication rewrites them, and a cache
  written before the upgrade can still show the labels while the server is unreachable. A `mandatory.md` that
  now fits the hook limit arrives whole instead of as a pointer.
- `kelmo create --wait` and `kelmo add --wait` run by the edge daemon no longer wait without limit between
  their polls. Such a command lends the outbox to other callers while it waits, and it took the outbox back
  with no deadline, so it could hang behind another writer. It now takes it back before its wait ends. Every
  write that waits for the outbox now gives up when it is woken after its deadline, instead of taking a lock
  freed in the meantime and answering late. When `--wait-timeout` ends first, the command answers
  `wait_timed_out` with the operation, as documented, even while another writer holds the outbox; before, the
  wait went on until the writer let go. When the daemon's deadline for the call ends first (it gives a
  `--wait` command 900 seconds, so this takes a longer `--wait-timeout`), the command fails with `E_BUSY` and
  names the operation the server already accepted. Running the same command again reuses its idempotency key,
  so the note is not captured twice.
- A note delivered while the server's kelmod restarts is no longer lost. `kelmod attach` refused a request
  that was sent and then cut as final, even when it carried an idempotency key, so the client's outbox settled
  the note as refused for good. The refusal now marks a keyed request as cut: the note stays pending and the
  next flush sends it again under the same key. On its third cut it is refused with its effect unknown, so a
  note whose delivery `kelmod attach` reports cut every time cannot hold the notes behind it. This counting
  covers a shared client that reaches the server without the edge daemon; through the edge daemon a cut note
  was already sent again, and its cuts are not counted yet, nor is a bridge process that dies or times out
  under a note. Only a cut counts: a retryable refusal the server itself gives leaves the note pending however
  often it comes. The mark comes from the server's `kelmod attach`, so the server needs this release too: with
  an older server a cut note is refused at once, as before, and so it is for a client from before this
  release, which does not read the mark. A request without a key is still refused, with its effect reported as
  unknown.
- A read that a shared client sends is sent once more, within the call's deadline, when the channel to the
  server is cut under it. Reconnecting is tried for at most 15 seconds, pausing 0.25 seconds at first and
  doubling the pause up to 2 seconds. A request that writes, such as `reconcile` or `graph rebuild`, is not
  sent again.
- A call from the installed CLI to the local daemon follows kelmod's replay rule when kelmod drops the socket.
  A call cut before it was sent goes once more within the deadline, and so does one cut after it was sent when
  it changes nothing (a daemon status or scheduler read, or a command a fenced server still runs, such as
  `search`, `show` or `status`), or when it is a `create`, `add` or `promote` that carries
  `--idempotency-key`, which the server settles once. Any other command cut after it was sent fails at once
  with `E_TRANSPORT` and its effect reported as unknown, on the second attempt as on the first. The resend
  tries to reconnect for at most 15 seconds, and the whole call stays inside its deadline; a daemon that takes
  no connection in that time ends the call with `E_TRANSPORT`. A connection the daemon does not take in time,
  and a hello it never answers, are now `E_TRANSPORT` with nothing sent, not `E_DEADLINE`, so `kelmo dream` no
  longer reports a pass running that was never asked for.
- The last capture of a session in a local vault is no longer lost to a moment of contention. A `SessionEnd`
  hook captured once, and a busy journal or another capture holding the session intake ended it. It now tries
  again for up to 5 seconds. Every wait for the journal counts against that time, opening it included, and so
  does the backup a session end takes before it upgrades an older session schema (an unfinished backup is
  removed). The time the hook waits for the daemon's writer counts too, so a session end that stays busy fails
  within the 7 seconds `kelmod hook` gives a capture hook, even while another write holds that writer. Before,
  an exclusive lock on the journal could hold the hook for up to 30 seconds. `Stop` and `PreCompact` still
  make one attempt, as before.
- A capture hook that found the session intake busy no longer closes the state core's journal. Until the
  daemon restarted, every write it served afterwards failed.
- An apply pass takes the notes that exist when it begins, read by id, and leaves notes captured during it to
  the next pass. It used to compare capture times with its start time, so after the system clock stepped back
  every pass skipped a note captured before the step until the clock caught up again. Which notes are the
  pass's now follows the order the journal recorded them in, read before the daemon's pass waits for the
  journal, so a clock step during that wait moves no note into or out of the pass. A usage limit's wait no
  longer holds Dreaming after the clock stepped back below the moment the limit was met: the next pass starts,
  and a limit still in force defers it again. `kelmo dream status` shows that moment as `deferred_at`.
- The findings pass of `kelmo dream run` reads evidence again when a capture rests in `captured`. Its cursor
  followed capture time and stopped at the first note not yet archived, so one collector capture that is never
  archived held it for good and every pass answered `idle`. The pass now reads notes in the order they reached
  the archive, so a note still on its way holds nothing back and is read in its turn once it is archived,
  `--max-sources` notes a pass. A finished pass never reads a note twice. A forgotten or purged record no
  longer fails the pass: it is passed. A cursor written by 0.3.1 is converted on the first pass: the notes
  0.3.1 had not reached are read, one window of `--max-sources` per pass, and what it read is not read again,
  with one exception. A note at or before the old cursor that failed (a moment's `E_BUSY` included) and was
  archived after a retry is read once more unless a finding already cites it, because the journal of 0.3.1
  cannot show whether that build read it, and reading it twice is better than never reading it. After that
  pass a 0.3.1 build cannot read the cursor.
- A daily Dreaming run that fails waits 15 minutes from the end of the run before the scheduler tries it
  again. The wait used to count from the start, so a run that took longer than that and failed was started
  again at once, and the three daily tries could go in about an hour. The same wait holds for a run that
  started one day and failed the next. A run that fails before it starts (its lock, Dreaming's settings) is
  now one of the three tries and waits the same 15 minutes; it used to be started again on every scheduler
  tick, all day. A run that answers a usage limit whose reset time has already passed still costs no try, but
  now waits the same 15 minutes instead of starting again on every scheduler tick. The tries and the wait are
  read again when the run starts, so a run that failed just after the scheduler looked gets the answer
  `not_due` instead of an extra try. A run the daemon's stop ends, or one whose call to another part of the
  daemon fails because it is stopping, costs no try and keeps the wait it found; a run that had already
  answered with a failure keeps its try. A `kelmo dream run` by hand is not limited.

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
