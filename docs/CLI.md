# Administrator CLI

`kelmo admin <verb>` is the operator surface over the selected vault's archive. It is a CLI
command only: no MCP tool reaches it, and the ordinary `kelmo` commands and adoption
never hold an administrator grant. Each call issues an operator grant for exactly the verb it runs (`purge-authorize` gets the `purge` grant)
(`archive_admin.operator_grant`); `ArchiveAdmin` refuses a verb the grant does not name, a grant of
another vault and a grant it did not issue. The grant separates capabilities for code that follows
it. It is not a security boundary: the CLI user and the vault owner share OS trust, and any code in
the same process can issue a grant itself.

Add `--vault PATH` to select the vault and `--json` for the versioned envelope
`{schema_version, ok, result[, error]}`. Without `--json` the verbs print plain text (below). A failed doctor exits 1 with `E_HEALTH`; maintenance with failed
records exits 1 with `E_PROCESSING`. Everything else exits 0 or reports the usual error envelope.

The read verbs `count`, `stats`, `doctor`, `backup`, `dump` and `search-log` work from the vault's
configuration through read-only connections. They do not open the archive writer, create schema,
claim a writer identity or repair anything, so they work when the Service cannot open (a stale or
foreign writer epoch, a failed schema step). A missing archive is `E_STATE` for `count`, `stats`,
`backup`, `dump` and `search-log`, and a `failed` doctor report; a damaged one is `E_STATE` naming
the doctor. They do not change what they read, with one exception: when `[telemetry]` is
explicitly configured, `search-log` first applies its retention in a separate write transaction,
then reads (below). The other verbs open the vault's Service and its writer.

## Verbs

| Verb                           | Options                                                 | Result                                                                                                                                                                                                                                                                                                                     |
| ------------------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kelmo admin count`           |                                                         | Archive rows, hidden ones included: a bare integer, JSON `{count}`                                                                                                                                                                                                                                                         |
| `kelmo admin stats`           |                                                         | Counts by kind/domain/project, file size, schema, index identity (`vector_index`, `profile_id`, `space_id`), `vectors_pending`, archive/forgotten/superseded counts, `search_log_mode`, logged-search count. No path or provider setting                                                                                   |
| `kelmo admin doctor`          | `--probe-embedding`                                     | `{status: healthy, degraded or failed, checks}`; checks are PASS, WARN (degraded), FAIL, SKIP (not applicable or not probed). The embedding service is called only with `--probe-embedding`; an unpublished graph is WARN                                                                                                  |
| `kelmo admin backup`          | `--dir PATH`, `--keep N` (14)                           | Consistent sqlite backup-API snapshot, integrity- and count-checked, published under a new name (default `archive/backups/`). Only backups this tool made there, listed in `.kelmo-backups.json`, are rotated; the oldest beyond `--keep` are removed. Text: `<path> <bytes> bytes`, then `rotated out <name>` lines |
| `kelmo admin dump`            | `--output PATH`                                         | The same snapshot published as a new 0600 file; without `--output` the bytes stream to stdout and nothing is left on disk. `--json` needs `--output`                                                                                                                                                                       |
| `kelmo admin graph`           |                                                         | Rebuild of the selected wiki graph, derived topology and name index (`kelmo graph rebuild`)                                                                                                                                                                                                                               |
| `kelmo admin entity-index`    |                                                         | `{entities}` after rebuilding `entities_fts` from the published entity rows                                                                                                                                                                                                                                                |
| `kelmo admin reembed`         | `--batch N` (64), `--force`                             | `rebuild_chunks` summary (version-, hash- and profile-guarded); needs `[embedding]`                                                                                                                                                                                                                                                                        |
| `kelmo admin backfill`        | `--batch N`                                             | `backfill_vectors` summary (space-verified), or `{skipped: "another backfill is running"}`; needs `[embedding]`                                                                                                                                                                                                                       |
| `kelmo admin search-log`      | `--limit N` (500)                                       | Last logged searches, oldest first: JSON lines, JSON `{searches}`                                                                                                                                                                                                                                                          |
| `kelmo admin purge-plan`      | `MEMORY_ID`                                             | Dependents, required disposition actions, refused dependents, removed and retained copies, `plan_sha256`                                                                                                                                                                                                                   |
| `kelmo admin purge-authorize` | `MEMORY_ID --disposition ID --plan-sha256 HASH`         | Records a named disposition for that exact plan in the journal (needs the `purge` capability); removes nothing (below)                                                                                                                                                                                                     |
| `kelmo admin purge`           | `MEMORY_ID --disposition ID`                            | Executes a disposition recorded with `purge-authorize`: physical purge report and receipt (below)                                                                                                                                                                                                                          |

Exports never replace a path. `backup` and `dump` refuse an existing destination, a dangling link
included (`E_EXISTS`), refuse a missing directory and refuse any path inside the vault's `.state/`,
`archive/` (except a backup into `archive/backups/`), `.cache/` or `.git/` (`E_INPUT`). The snapshot
is written to a private temporary file beside the destination and published by a hard link, which
fails rather than replace a file created meanwhile.

Text-mode output of `graph`, `entity-index`, `reembed` and `backfill`
is the result as one JSON line; `stats`, `purge-plan`, `purge-authorize` and `purge`
print indented JSON;
`doctor` prints `[+]`/`[!]`/`[x]`/`[-]` lines on stderr.

## Search telemetry

Off unless `kelmo.toml` holds a `[telemetry]` table:

```toml
[telemetry]
search_log = "redacted"   # off | redacted | full
retention_days = 30       # 1..365
max_rows = 5000           # 1..100000
```

Redacted rows keep SHA-256 and length of each query variant, filter names, the client, the ranked
record ids, the duration and the retrieval identity (contract, index, generation, model, space and
profile ids, requested mode, degradation). Full rows also keep the query text and filter values.
Each successful `kelmo search` and MCP `memory_search` writes one row through its own connection
and transaction (a shared server whose archive runs as the restricted process does not log yet); a lock, full disk or schema fault is logged and dropped, never returned to the
search, and never commits or ends the caller's transaction. Inside that transaction any result id
of a record purged meanwhile is written as a purged marker.

A configured retention removes rows older than `retention_days` and beyond `max_rows` at each
write and again before `kelmo admin search-log` reads, so expired rows are never shown because
no search happened since; if it cannot be applied the read is refused with `E_BUSY`. Changing the
mode never redacts or deletes earlier rows. With no `[telemetry]` table nothing is pruned.
Backups and dumps contain the table.

## Physical purge

`kelmo forget` hides a record and keeps every byte. Supersession hides the corrected record.
Neither is deletion. `kelmo admin purge` is the separate physical removal, `ArchiveAdminAPI.purge`.

`kelmo admin purge-plan ID` lists, read-only:

- `required`: each dependent and the one action a disposition must name for it: owning receipts,
  hide receipts and journal operations of the record or of its bytes (also unfinished captures) are
  `void`; supersession links in either direction are `unlink`.
- `refused`: dependents no disposition can settle in this baseline: accepted wiki facts or registry
  notes citing the record, session application receipts of its source, an unsettled resolution job
  of a dependent operation, any external claim row on one, expired or not, and (once a state/editor
  journal is composed in) a prepared action or editor intent naming the record or its bytes. Their
  owners must remove the reference first; nothing retracts a fact, dismisses a job, withdraws an
  intent or overrides a lease here.
- `plan_sha256`, the review identity the operator authorizes: a hash of the archive-side dependents
  together with every required action and every refused dependent, so any dependent that appears
  later (a queued capture of the same bytes included) changes it. `archive_sha256` is a different
  value, the archive-side compare-and-swap the archive rechecks under its own lock; never pass it
  as the plan hash.
- `removes`, `retains`, `retained_spool`, `identical_content_records` and `whole_system_erasure: false`.

Knowledge dependents are read from the accepted registry, the committed
`knowledge/provenance/registry.json` the readers serve. Planning refuses with `E_CONFLICT` while a
wiki transaction is unfinished, while the working registry differs from the committed one, or when
a working registry has no committed generation, and with `E_CONFLICT` for a committed registry it
cannot validate. A vault with no registry at all has no knowledge dependents.

### Operator commands

```sh
kelmo admin purge-plan MEMORY_ID --vault VAULT --json
kelmo admin purge-authorize MEMORY_ID --disposition ERASE-ID --plan-sha256 HASH --vault VAULT
kelmo admin purge MEMORY_ID --disposition ERASE-ID --vault VAULT
```

1. `purge-plan` is read-only. Review it and copy its `plan_sha256`.
2. `purge-authorize` needs the `purge` capability and that exact hash; there is no default. It
   computes the full current plan again and refuses any refused dependent (`E_CAPABILITY`) and a hash
   that is no longer current (`E_CONFLICT`, run `purge-plan` again). It then stores, in one journal
   transaction, the disposition: id (1 to 128 letters, digits, `.`, `:`, `_`, `-`), vault id,
   operator principal, record id, plan hash and one action per required dependent, state
   `authorized`. It removes nothing. The same id with the same record and plan replays the stored
   disposition (`replayed: true`); the id with another record, plan or action set is refused
   (`E_IDEMPOTENCY`). A later changed plan is never approved under an earlier disposition: authorize
   it under a new id.
3. `purge` executes a stored disposition, separately and in its own process if wanted. Nothing is
   authorized on the way, and there is no MCP tool for either step.

Purge refuses, before any change: an unknown disposition or one stored for another vault
(`E_CAPABILITY`), one naming another record (`E_CAPABILITY`), an incomplete, extra or stale one, a
dependent that appeared after authorization included (`E_CONFLICT`), any refused dependent
(`E_CAPABILITY`), a busy worker (`E_BUSY`), a stale writer (`E_WRITER`), an open archive or
journal transaction (`E_STATE`) and a disposition the journal already settled while the archive
holds the record again, for example after a restore from an earlier backup (`E_STATE`: review the
plan and authorize a new id). Under the worker and capture locks it computes the plan again and
checks the disposition against it. Then, in one journal transaction, it rechecks that no external
claim row holds a planned operation (`E_CAPABILITY`, `lease:`) and records an execution intent
naming those operations; while the intent lasts, `claim` refuses them, so no claim can appear
between the archive commit and the journal half. Claims are never cleared. If the archive half
does not commit, the intent is withdrawn; an intent left by a process that died before the
archive commit keeps its operations unclaimable until the same purge is repeated, which withdraws
it and checks everything again. With `secure_delete` on before the first delete (the caller's
setting is restored) and in one archive `BEGIN IMMEDIATE`, it rechecks writer and the archive-side
hash, then
removes the record's receipts and source packets, the record, its versions, chunks, FTS postings (indexes optimized), every vector
derived from its text, its links and its ids in logged results, and writes the purge receipt and
tombstone keys (operation ids, content hashes, source ids). The
journal half follows: dead local worker claims are handed over, operations are staged `purged` (a
terminal durable stage) with a `purge` event, and last the intent is finished and the
disposition's state becomes `settled` together with its receipt, in one journal transaction.
Archive and journal are separate stores: a process that dies between them is finished by repeating
the same `purge`, which returns the stored archive receipt (`replayed: true`), rechecks writer and
claims, and completes the journal half. A claim row older than the intent found then is reported
as the refused `lease:` dependent (`E_CAPABILITY`) and left in place; the purge finishes once its
owner releases it. Another disposition for a purged record is refused (`E_IDEMPOTENCY`).

After a purge, its operations are terminal: `kelmo queue retry` of one refuses with `E_STATE`
like any terminal operation, and running a queued operation of the purged bytes stages it `purged`.
A new capture or a replayed archive capture of a tombstoned operation key, content hash or source
fails with `E_PURGED`, an archive update with tombstoned bytes or source fails with `E_PURGED`
(also for another record holding identical bytes), and reads of the record answer `E_PURGED`.

Removed: the archive's rows and derived index rows listed above; pages the purge frees in the main
database file are zeroed. Retained: captured source bytes in `.state/sources/`, journal history (the
disposition and its receipt included), Git history of the data repository and wiki, earlier
backups and dumps, older WAL frames until
the next checkpoint, pages earlier ordinary writes freed without `secure_delete` until `VACUUM`,
query text in full telemetry, and other records holding identical bytes (named in the report). A
purge is not whole-system erasure.
