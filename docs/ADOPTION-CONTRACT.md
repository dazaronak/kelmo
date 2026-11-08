# Adoption contract v1

Version: `adoption/1`. Intake behavior: `durable-text/1`. Public JSON envelope:
version 1.
This contract belongs to Kelmo. The independent wiki file
validator consumes its frozen proposal; it does not own runtime authority.

This contract fixes the local archive boundary and the shared record and API shapes.
Isolated checks cover process-death fixtures, journal-event recovery, concurrent
retries and caller-transaction preservation. They do not establish native
isolation, installed behavior or live activation.

## Record and API shapes

`src/kelmo/contracts.py` is the executable transport contract. Records are
frozen dataclasses with strict `from_dict` readers and `to_dict` writers. Unknown
fields fail. Bytes serialize as base64; arrays become tuples. Optional transport
fields use null when absent. Registry/file proposal omission rules remain a
separate format and do not silently change these transport records.

| Record            | Fields / boundary                                                                                                                                                                                                                                         |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| EvidenceRef       | `archive_id`, integer `version >= 1`, lowercase `content_sha256`, `source_id`. No latest reference.                                                                                                                                                       |
| Origin            | `authority_id`, `client_id`, `machine_tag`, optional `session_id`. A deserialized audit record has no authority.                                                                                                                                          |
| Generation        | `authority_id`, monotonic integer `publication_id`, `manifest_sha256`. Hash covers the generation manifest, not itself.                                                                                                                                   |
| SourcePacket      | Original UTF-8 `content` bytes, `source_kind`, `source_id`, Markdown basename `filename`, timezone-aware `observed_at`, kind/domain/project/tags/validity metadata. `require_vectors` can strengthen admission; it grants no principal privilege.         |
| ArchiveVersion    | Exact `evidence`, original `source`, audited `origin`, `stored_at`, `vectors_pending`. Source ID/hash must agree with evidence.                                                                                                                           |
| OperationReceipt  | `operation_id`, `idempotency_key`, `source_id`, `outcome`, exact `evidence_refs`, optional old/new revisions and `generation`. Archived, published and delivered are distinct.                                                                            |
| ActionUpdate      | `action_id`, `subject_kind`, `subject_id`, `kind`, `expected_revision`, `event_order`, `transition`, `evidence_ref`, optional `correction_of` and kind-specific bindings.                                                                                 |
| EmbeddingSpace    | Contract version, immutable artifact/tokenizer identities, paired query/document transforms, pooling/projection/normalization, dimension/dtype/byte order. A provider name or dimension alone cannot identify a space.                                    |
| ChunkProfile      | Version, segmentation, token budget/overlap, text preparation and tokenizer identity, separate from embedding space.                                                                                                                                      |
| VectorBatch       | Full space, query/document purpose, ordered immutable vectors. Count, dimension, representation and finite values are checked.                                                                                                                            |
| Retrieval records | `SearchFilters` includes kind/domain/project/tags/days/machine_tag; `SearchResult` pins exact hits, mode/degradation/reason/index identity. `ChunkLocator` pins version/hash/index generation/index; `ChunkWindow` bounds adjacent text.                  |
| Knowledge records | Page snapshots/batches and context are generation-bound. Proposals carry scoped paths, baseline hashes and exact evidence. Byte/hash mismatches and mixed generations fail. `KnowledgeAPI.publish` accepts only editor-prepared proposals (TECHNICAL.md). |

`ArchiveAPI` freezes capture/get/update/history/expand/search/hide with the
frozen design signatures. `KnowledgeAPI` freezes generation-pinned
pages/page/context/publish. `StateAPI.apply` and `EmbeddingProvider` query/document
methods use these same records. `ArchiveAdminAPI.purge` is a separate capability,
implemented by `archive_admin.ArchiveAdmin` under an issued operator grant and a
coordinated disposition (below). The local adoption adapter and its clients hold
no purge authority. Physical deletion is never translated into successful hiding.

The frozen wiki proposal supplies the concrete spellings `manifest_sha256`,
`deploy`, `completed`, `correction_of`, `work_revision`, `remote_ref`, `artifact_id`.
They carry the meanings of the frozen design. Subjects are feature/topic/person/task;
operation names are controller-enrolled; push, merge, deploy and book are the enrolled examples.
Push/merge require repository, work revision and remote destination. Deploy requires
environment/artifact; a Git-derived deploy supplies repository and work revision
together. Feature ID, when supplied, equals the feature subject ID. General actions
need no fabricated Git fields. Correct/retract requests name their accepted predecessor.
Record validation alone does not admit a state transition or authenticate REQUESTER.

## Trusted local boundary

`OriginBindings` is constructed by trusted launcher/controller code from the
selected Config authority/node label, or from explicit immutable client/repository
enrollments. Repository roots are bindings, never input paths. Local default enrollment
is client `local`, without requester privilege. Origin objects are issued by that binding
and checked by identity. Copying/decoding the same fields fails authentication.
Requester attribution needs an explicitly requester-capable enrollment, independent
of the old declared `lead` role. This code does not infer REQUESTER from a note or tag.

`WriterIdentity` binds authority/writer/epoch. Archive startup refuses a foreign
authority or mismatched persisted epoch. Mutation rechecks the epoch inside its
write transaction, including after embedding work. Service checks the writer before
capture, run and retry; existing worker/capture locks and journal claims remain.
This is the local prerequisite, not distributed writer fencing or OS confinement.
Direct Python code and the local user share process/OS trust. Remote authentication,
coordinator-wide fencing and restricted processes still belong to the server lane.

Service accepts `archive_api`, trusted `bindings` and an issued `origin` as
constructor-only injection. An injected archive needs the `ArchiveCoordinatorAPI`
extension: exact owning receipt/recovery operations, legacy binding, compatibility
records/list/search/counts, hide reason/timestamp, supersession, writer checks and
close. These operations are not CLI/MCP tools. Service owns the adapter lifetime.
With injection, the constructor does not open an archive connection itself.

Default Service constructs `LocalArchiveAdapter` over the selected owned archive
and verified cache. It uses the archive schema, FTS trigger, embedding function,
filtering/list/search and visibility/supersession mechanisms. There is no legacy
executable, second runner, network transport, secret or private deployment default.

## Durable archive and compatibility

The adapter embeds outside the transaction using the existing pinned local provider,
or, in a vault with `[embedding]`, the one configured chunk-index provider (below).
Only `ArchiveEmbeddingError` permits ordinary intake to continue without a vector.
One SQLite transaction writes full original text, trigger-maintained FTS, optional
vector, source metadata and the exact owning receipt. A failed storage write rolls
them back together. Raw-note identity and operation-key fingerprints retain replay
protection. The writer is checked again after embedding before accepting writes.

Service still syncs captured source bytes before journal registration. Reading
captured bytes decodes UTF-8 without universal newline normalization. Eligibility,
reconciliation, archive-before-wiki ordering, claims, outcome reporting and wiki
transaction recovery remain in the existing coordinator. Archived stages add exact
`evidence_ref` and pending-vector information. Journal origin rows preserve the
authenticated identity without changing existing operation fields.

A crash after archive commit but before the journal archived stage finds the owning
archive receipt and replays it. Supersession recovery checks that receipt before
refusing an already-hidden target. Retry/publication checks the recorded operation's
exact source, version/hash and origin; it never silently selects a current head.

Old local journal receipts can be bound only through a trusted constructor callback
that verifies existing archived/applied journal ownership, captured source bytes and
origin. The adapter then checks its exact stored bytes/source identity. An archive ID
or input dictionary cannot invent this callback or authorize a hidden read. A legacy
record without attribution is marked `legacy-unattested`, not enrolled or REQUESTER.

Public show/get, list and search exclude hidden evidence. Internal verification
accepts only an already-bound owning operation, exact reference and issued origin.
Exact-source dedup may give a distinct accepted capture its own receipt, including
for an already-hidden record. Receipt ownership is not first-operation-exclusive;
coordinator receipt capabilities remain internal and public visibility stays enforced.
Supersession links and hides the old record while retaining bytes; claim retraction
remains unimplemented, and physical purge is the separate administrator operation below.

Hide rechecks origin/writer, the record and the operation-key receipt after
`BEGIN IMMEDIATE`. A concurrent repeat returns the exact accepted receipt;
another record/origin under that key refuses. Supersession takes its own
`BEGIN IMMEDIATE`, rechecks origin/writer and both records, validates visibility
and the existing target, then inserts the link without a helper commit. Rejected
writer/state checks roll back that transaction. Both methods refuse an already
active caller transaction before entering their connection context, so they
cannot accidentally commit or roll back the caller's partial work. Link and
subsequent hide remain separate Service stages with their existing replay rules.

If archive hide commits before its journal event, a repeat reconciles the missing
`forgotten` event using the persisted visibility timestamp and existing operation
bindings. Public `forgotten=false` still means the repeat did not create the hide;
it does not suppress journal repair. A repeated accepted supersession likewise
reconciles its `superseded` event after validating the same link and completing
the predecessor's hide/forgotten reconciliation.

`Journal.event_once` serializes event lookup and append under `BEGIN IMMEDIATE`,
with the current archive writer checked after taking the journal lock. Matching
events remain unchanged; conflicting existing payloads refuse without rewriting
history. Service rejects an active caller journal transaction before archive
mutation; the helper also checks before entering its connection context.
No new archive authority, hide timestamp, receipt-ownership
policy, journal/schema or worker flow is introduced. Archive and journal remain
separate durable stores reconciled on retry, not one cross-store transaction.

Existing public CLI/MCP names, arguments, JSON envelope and record fields keep their
meaning. Python search still returns a list unless metadata is requested. Hybrid
search falls back only for embedding failure and labels even an empty lexical result;
storage failures propagate. Semantic-only requests fail when embeddings are unavailable.
Status adds `archive_contract_version` and `archive_intake_behavior`. Config schema 1
and 2 are both read. A registry in an older format is refused with `E_CAPABILITY`;
opening a store never rewrites its registry, generated pages or transcript settings.

Meaning recall and labelled degraded search assertions are unchanged by a
missing model. `collector/unknown` still stays captured. Promotion still
needs explicit lead intent and real reconciliation receipts. Legacy `--supersedes`
retains successful-index admission, so a failed replacement leaves its target visible
and retries normally. Same-ID updates use the version-preserving path below;
backfill is the chunk-index path below.

The archive adapter first supported immutable version 1; it now adds same-ID
updates, exact historical reads and bounded reads of stored current chunks.
`history` enumerates replaced versions in ascending order, excluding the current
head; `get` reads the head or an explicitly named version. A history cursor fixes
its upper bound when created. Missing historical bytes or chunk generations fail
explicitly; the adapter never fabricates old content or reconstructs old positions.

An update preserves all original metadata and owning receipts, including source
and origin metadata per version. Version checks repeat under the write lock,
including apparent no-ops. Changed text invalidates old vectors and chunk windows;
metadata-only changes preserve them. Embedding failure retains new text with
pending vectors. With `[embedding]` configured, the chunk generation follows the
same rule (below).

`Service.capture_source` retains the unchanged SourcePacket and issued origin.
Original bytes use the existing immutable source spool; metadata and a full-packet
hash are bound in the same journal transaction as capture. Replay checks both
bytes and metadata. Ordinary capture records its original derived packet too;
pre-adoption operations remain readable through their existing deterministic
source mapping. Public ID-only reads select the visible head, while an explicit
owning operation reads the exact receipt version.

A new raw-note capture selects an exact current version before identical history.
Vector-required capture can reuse the indexed current bytes with the same source
identity; it cannot borrow vectors from a different version's text. Existing
operation receipts remain bound to their original exact version. When only bytes
and source identity match, canonical archive metadata stays unchanged and each
operation's original packet stays in the journal. Deduplication does not attest an
older record's origin on behalf of the new caller. Repeating an identical correction
request returns its existing operation even after the replacement link or hide has
committed; a different correction still cannot create a competing visible head.

Historical `vectors_pending` describes the state recorded for that version, not
whether its old vector still exists in the current search index. Search reports
the current index identity and degradation separately. Listing order and its
fallback date ranges compare UTC instants at microsecond precision while retaining
the original stored strings; explicit validity dates remain calendar days. The
configured chunk search applies the same precise comparison to
`COALESCE(valid_from, created_at)` and raises `ArchiveLegacyRecordError` for an
unreadable row. The unconfigured record search and the record-index keyword lane
of a configured search still compare date strings.

### Chunk index

A vault opts in with `[embedding]` in `kelmo.toml`; without it every record API,
receipt and serialized shape above is unchanged. Service builds the configured
provider once and passes it with its settings and chunk sizes to the adapter.
Capture, update, search, expand, backfill and rebuild use that provider only;
there is no second channel and no fallback to another model.

- Durable text first. The chunk window is the provider's declared input path: a
  local provider's own tokenizer, or a broker's local tokenizer file plus the
  declared `[embedding.input]` table, which must match the declared space's bound
  transforms and bound `token_budget`. A cold broker therefore still stores text,
  metadata, fitted chunks and FTS, with truthful pending state, and never yields
  a verified space identity. `require_vectors` refuses and writes nothing.
- Inference outside SQL. Cutting and embedding happen before `BEGIN IMMEDIATE`;
  under the lock the adapter rechecks the head version, content hash and profile
  before admitting chunks and vectors with the record and its owning receipt.
- Versions. A text update replaces the chunk/FTS generation and keeps history;
  vector bytes without a recorded space go to `unverified_vectors`, never ranked.
  A metadata-only update keeps chunks and vectors and binds results to the
  present version. A no-op, a replay and a restart with the same settings write
  and embed nothing. Another input path is another profile until an explicit
  rebuild.
- Search. `Service.search`/`search_records` keep their shape and filters; one
  item per record. Filters, precise dates and visibility apply before ranking and
  the limit. Hybrid labels BM25-only degradation with the provider's reason,
  semantic raises `E_EMBEDDING`, partial coverage and zero hits stay labelled,
  and records the chunk index cannot answer for are labelled `unindexed_records`.
  The limit counts records: fusion stops at a bounded record pool (the limit
  plus a few, at most `MAX_RECORD_POOL`), each record holding at most two
  chunks, so records with two matching chunks cannot halve a limit above 50.
  `search_chunks` keeps `limit` as a chunk-row count for its other callers.
  Each ranked record is re-read outside the ranking snapshot and kept only while
  it is still visible at the same version and content hash; a record hidden or
  changed meanwhile is dropped. Replacements come from the same bounded pool:
  at most 16 spare records, also limited by each ranking's 200-chunk window.
  A full 100-record result can have no spare record in a single ranking.
  `ArchiveAPI.search` takes query variants and returns `EvidenceRef`,
  `ChunkLocator` and the index identity; `expand` resolves the same profile,
  version and bounded neighbours and refuses stale or foreign locators.
- Physical delete (`kelmo.store.memory.delete`) copies space-less vectors to
  `unverified_vectors` in its own transaction and refuses a caller transaction.
- Custom local models must be registered with the FastEmbed backend, whose model
  and additional files must be the pinned ones at the pinned dimension; an
  unregistered name fails explicitly.
- The pinned record model is recognised in one place (`model_store.record_model_pin`)
  by configuration and by a provider built directly, with or without an explicit
  pin table equal to its own; a declared pooling, projection, normalization or
  dimension other than its fixed values is refused, never described as its space.
- A broker `verify()` clears the earlier proof, policy and windows before it asks;
  a failed re-verification leaves nothing verified. A malformed answer line read
  while a request is still being written is the same `protocol_error` as on the
  read path; a well-formed answer to another request is dropped there.

Existing graph/diagnostics receive `Service.archive` only for a real local adapter.
Transport adapters fail explicitly on that direct-connection path; snapshot graph
transport remains server work.

The foundation is extended as follows:

- **Independent roots.** Schema-2 `Config` binds the archive, state, wiki and knowledge Git roots independently.
- **Generations.** `KnowledgeReader` reads coherent, hash-bound knowledge generations from that Git root.
- **StateAPI admission.** `StateCoordinator` admits explicit actions behind the existing worker lock and publishes them only through the single publisher.
  - The producer capability (`GitIngress.capture`) archives, at event occurrence, the original tool receipt together with the immutable Git event read then. Later admission consumes that archived event and never reads current refs. This covers first pushes of new branches and local merge commits.
  - A raw receipt admitted without such a capture must name the destination and both revisions in its own output. Otherwise it stays unresolved.
  - Capture runs only in the command's enrolled Git top-level and needs the receipt's own update or new-ref line. Its reserved intake namespaces are closed to public capture.
  - An action that no registered page shows is refused at admission (`E_BINDING`); no page is guessed for it.
  - The same producer tool receipt cannot reopen a retracted action. Sameness is the projection the parser reads, so re-serialization, a capture or hook envelope, and ignored blank lines do not make it new.
  - A new-branch receipt needs the tracking reflog to show exactly its creation. A branch deleted and recreated before capture is not distinguished, at any value.
  - A snapshot without capture provenance is refused without recording anything under its key.
  - Admission reduces against the pinned generation's committed registry. A dirty working baseline is refused before reduction and stays retryable under the same key.
  - Request settlement and its source outcome commit in one journal transaction.
  - A budget refusal predictable at admission is diagnosed before an intent is prepared, and stays retryable. A later environment change can still block recovery until repaired.
  - Installing the live harness hook that calls capture, and a shared transport, belong to later work.
- **Mandatory delivery.** Startup context (HOT and `KnowledgeReader.context`) delivers whole, once, every person page and every page holding a mandatory record, selected by one helper. The `person` project listing keeps its own meaning. Overflow refuses.
- **Pinned history.** A historical generation is found only in manifests reachable from HEAD. A commit that deleted the manifest is skipped; matching but invalid evidence and an unmeasured pin both refuse.

Current integration boundaries:

- Shared transport, server-owned identities and snapshot graph reads are
  implemented below; native client acceptance remains separate.
- Explicit administrator capabilities are described below and CLI.md.
- Installer and native isolation mechanisms require target-host acceptance.
- Snapshot/import mechanisms are described below; production execution
  requires its own authorization and receipts.

The mixed-action annotation grammar is not part of this contract; the wiki file format is described in [WIKI-FILE-CONTRACT.md](WIKI-FILE-CONTRACT.md).

### Administrator capabilities and purge

Record shapes, public names
and the existing CLI/MCP outputs are unchanged; additions are `kelmo admin`, graph analytics
actions with an `analysis` label and optional `snapshot`/`assignment` names, the opt-in
`[telemetry]` table, the terminal journal stage `purged`, the journal tables `purge_dispositions`
and `purge_intents`, and an `analysis` label on every graph query
answer. Graph nodes keep the legacy
`community_id`, `surprise_score` and `is_god_node` fields. [CLI.md](CLI.md) lists the verbs.

- Administrator verbs need an `AdminGrant` issued by `operator_grant` for this vault and verb;
  a copied grant, another vault's grant or an unnamed verb is `E_PERMISSION`. MCP exposes none,
  and adoption never issues one. The grant is a capability check for cooperating
  code, not a boundary against hostile code in the same process.
- The read verbs (count, stats, doctor, backup, dump, search-log) use `Config` and read-only
  connections: no writer identity, schema step or repair. They change nothing they read, except
  that `search-log` with an explicitly configured `[telemetry]` table first applies its retention
  in a separate write transaction. Exports publish a new file by hard link and never replace a
  path or write into the vault's runtime directories.
- Maintenance uses the chunk-index routines unchanged (`rebuild_chunks`, `backfill_vectors`); no other
  embedding path exists.
- Search telemetry is written by `search_log.record` through its own connection; any failure is
  swallowed and the caller's transaction is never committed or ended. A configured retention also
  applies before an operator reads. Retention of rows logged before any `[telemetry]` table exists
  is not decided by this contract and is left untouched.
- Disposition issuance: `ArchiveAdmin.authorize_purge(archive_id, disposition_id, plan_sha256)`
  (`kelmo admin purge-authorize`) needs the `purge` capability and the exact hash the operator
  read from `purge-plan`; there is no default. Under the capture lock it computes the full plan
  again, refuses refused dependents and a hash that is no longer current, and stores the named
  disposition (vault id, operator principal, record, plan hash, one action per required
  dependent, state `authorized`) in the existing journal's `purge_dispositions` table. Same id and
  payload replays; a reused id with other content refuses. It removes nothing and is not on MCP. A later changed plan is never approved under an earlier id:
  `plan_sha256` is the review identity over the archive-side dependents, every required action
  and every refused key, separate from the archive-side compare-and-swap `archive_sha256`, so a
  capture queued between review and authorization refuses.
- Knowledge dependents come from the committed registry (`HEAD:knowledge/provenance/registry.json`),
  never a working edit. An unfinished wiki transaction, a working registry that differs from it or
  has no committed generation, and an invalid committed registry refuse planning (`E_CONFLICT`).
  When journal tables of the later state/editor composition (`action_requests`, `action_intents`,
  `editor_intents`) exist, rows mentioning the record or its bytes are refused `intent:` dependents;
  nothing withdraws them here.
- `ArchiveAdmin.purge(archive_id, disposition_id) -> OperationReceipt` executes separately and
  reads the stored disposition (an injected trusted source is still accepted). Under the worker
  and capture locks it recomputes the plan and checks the disposition (record, plan hash, exact
  actions). Accepted facts, registry notes, session application receipts, unsettled resolution
  jobs and any external claim row are refused dependents: no fact retraction, job dismissal or
  lease override exists in this baseline.
  The adapter's `apply_purge` holds no authority; with `secure_delete` on before its first delete
  it rechecks writer and plan under `BEGIN IMMEDIATE`, deletes, and writes the receipt and tombstone in one transaction; capture
  and update consult the tombstone. The journal stages dependent operations
  `purged` afterwards and then settles the disposition with its receipt in one journal
  transaction; archive and journal stay separate stores and a repeat completes the journal half.
  Before the archive half, one journal transaction rechecks external claims and records a
  `purge_intents` row; `Journal.claim` refuses its operations while it executes, so the journal
  half cannot be blocked by a claim taken after the check. Claims are never cleared: a leftover
  row found on replay is reported as the refused `lease:` dependent. An intent whose archive half
  never committed is withdrawn by the repeat. A disposition already settled while the archive holds
  the record again (restored backup) refuses with `E_STATE`; a new review and id are required.
  Captured spool bytes, journal and Git history, backups,
  older WAL frames, pages freed earlier without `secure_delete` and identical records elsewhere
  are retained and named in the report (`whole_system_erasure: false`).
- The low-level `memory_delete` library function refuses an archive Kelmo owns.
- Remaining interfaces, not implemented here: fact, note and session-receipt retraction by the
  knowledge publisher; resolution-job settlement and external-lease settlement; remote/transport
  administrator composition; the later full state/wiki composition.

### Dreaming

Bounded, source-backed discovery over newly archived evidence and the committed wiki.
`Service.dreaming` (`Dreaming(service)`) runs periodic passes without mutating action state.
Journal tables `dream_state`, `dream_findings` and `dream_runs` are initialized on first use,
preserving the journal schema for vaults that never dream.

- Bounded evidence intake: `Dreaming._changed_evidence` reads operations strictly after a durable
  cursor (`dream_state.cursor`), ordered by `created_at` and `id`. Only operations in durable stage
  `archived` or `applied` are admitted. In-flight operations that are neither archived nor applied
  stop the cursor scan; the cursor never passes incomplete intake. Dreaming's own notes (filename
  starting with `dreaming-` or recorded in `dream_findings.note_operation_id`) are skipped and never
  treated as evidence.
- Pinned context and budget: `Dreaming._packet` gathers evidence text via `Service.show` and calls
  `Dreaming._related` to find related non-person pages citing the evidence archive IDs, followed
  by their graph neighbours. Evidence items whose cost exceeds `budget_tokens` are recorded in
  `skipped` and bypassed, so an oversize item cannot hold the cursor indefinitely. Related pages
  exceeding the token budget are recorded in `omitted`. `ContextBundle` records completeness.
  If no evidence fits, the cursor advances past the skipped items and the pass finishes.
- Model execution and task contract: `Dreaming.run` reads the pinned committed generation
  (`E_GENERATION` without one) and invokes `ModelExecutor.execute` with `ModelJob('dream', bundle, packet)`.
  In `executor.py`, task `'dream'` uses the `DREAMING` prompt and `DREAMING_SCHEMA`. Quota deferrals
  return `deferred` with `reset_at`. Non-proposal outcomes fail the run with typed errors.
- Validation against shown sources: `Dreaming._validate` accepts at most 20 findings (`MAX_FINDINGS`).
  Every finding must specify `kind`, `pages`, `sources` and `reason`. Allowed kinds are `link`,
  `contradiction`, `merge` and `split`. Page targets must be distinct pages shown in the packet.
  A `link` finding must name exactly two pages, starting on a project page that does not already link
  to the second page. For unordered kinds (`contradiction`, `merge`), page targets are sorted so
  ordering does not create duplicate findings. Every cited source must be a fact shown at its exact
  revision (`fact_key`, `revision`) or an evidence item shown at its exact version (`archive_id`,
  `version`). Similar wording or titles alone are not sources; ungrounded findings are rejected.
  Reasons are limited to one plain sentence of at most 400 characters.
- Recording and deduplication: `Dreaming._record` fingerprints each finding by its kind, pages and
  sources. Existing fingerprints increment `seen` and update `last_seen`. New findings are stored
  with state `open`, timestamps and baseline page revisions.
- Link publication via editor: for open link findings, `Dreaming._publish` re-checks staleness via
  `Dreaming._stale`. If any baseline page revision, fact revision or evidence version changed or was
  superseded, the finding transitions to `stale`. Otherwise, it captures an immutable Markdown note
  titled `# Dreaming link finding <fingerprint>` via `Service.capture_source`, archives it with
  `Service.run`, and calls `Service.editor.edit` with `link_pages=True`. In `editor.py` and
  `wiki/engine.py`, `link_pages=True` permits `add_links` on a sent page: page IDs of other sent pages
  are appended to `links` without rewriting facts, existing links or unmanaged text. Environment
  errors (`E_CONFLICT`, `E_BUSY`, `E_GIT`, `E_IO`, `E_ARCHIVE_PROCESS`) leave the finding `open` for
  future attempts via `Dreaming.publish`.
- Invariants and limits: Dreaming never mutates action state; it cannot complete, retire or reopen
  work, by age or otherwise. It never proposes status, action, ownership or protection changes, and
  never rewrites facts. The worker lock is held only during editor publication, so Dreaming never
  blocks StateAPI updates. Contradiction, merge and split findings are recorded for review only and
  are never published automatically. Pause (`Dreaming.pause`) and resume (`Dreaming.resume`) control
  execution state via `dream_state`.

### Shared clients

Shared client profile and transport boundary for multi-client adoption. A shared client root
is configured by `client.toml` (`ClientProfile`) and is never an archive or wiki writer. The client
reaches the server over one bounded stdio channel and maintains a durable outbox and a sealed,
hash-checked read cache.

- Profile configuration: `ClientProfile.load` reads `client.toml` (`schema_version = 1`,
  `mode = "shared"`, `authority_id` UUID, `command`, `timeout_seconds`, `sessions`). The client
  directory cannot be a local vault (`kelmo.toml` present is refused with `E_CONFIG`). The channel
  command must be an absolute executable path and is configured locally, never by server replies
  or incoming requests. `ClientProfile.as_dict` declares `writer: False`.
- Durable outbox: `Outbox` in `.client/outbox.sqlite3` operates with `PRAGMA synchronous=FULL`.
  Mutating requests, including `capture`, `create`, `state_apply`, `forget`,
  `retry`, `session_capture` and compatibility writes, are inserted into
  `entries` as `pending` under a unique idempotency key before any byte is transmitted. `Outbox.flush`
  acquires the process-wide `flush.lock` and delivers pending entries in sequential order over the
  channel. Successful replies settle entries as `delivered`. Non-retryable server refusals settle
  entries as `refused`. Channel disconnects, timeouts, unbound replies and retryable server errors
  record the error code and stop the flush, leaving remaining entries pending.
- Channel protocol: `StdioChannel` runs the configured subprocess with non-blocking stdin and
  length-prefixed JSON frames (`server.encode_frame`). All operations run under monotonic deadlines.
  On connection, the client sends `hello` and verifies that the server's `authority_id` matches
  `profile.authority_id` (`E_AUTHORITY` on mismatch). The server assigns caller identity from the
  trusted channel; client requests cannot choose origins, server paths, provider commands or writer
  epochs. If a server does not advertise a requested operation in its hello features, `SharedClient`
  raises `Unsent('E_CAPABILITY')`, keeping the outbox entry pending without breaking the channel.
- Context delivery and sealed read cache: `SharedClient.context` resolves local git repository and
  branch state via `local_binding` and reads work bindings from `hot-work-binding.json`. It requests
  context from the server and verifies incoming payload integrity via `verify_context`: context SHA-256,
  individual page byte counts and hashes (`sha256:bytes`), and mandatory page ordering. Valid contexts
  are sealed under `CACHE_SCHEMA = 2` and written atomically to `.client/cache/context-<key>.json`.
  When the server is unreachable or returns `CACHE_FALLBACK` codes (`E_TRANSPORT`, `E_AUTHORITY`) or
  `Unbound`, `SharedClient._stale` loads the cached generation, validates its sealed binding and
  hashes, and serves the context with a diagnostic header:
  `Kelmo HOT STALE CACHE (<code\>): ... not current state and not proof of it. Writes stay queued in the local outbox and are not accepted yet.`
  The response sets `freshness = 'stale'` and `complete = False`. Cached content is used only for
  context fallback; ordinary data reads (`SharedClient.call`) never fall back to cache.
- Native lifecycle hooks and sessions: `hook(client, action, payload)` integrates with agent
  lifecycle events (`start`, `capture`, `session-stop`, `session-checkpoint`, `session-end`).
  `start` bounds binding, flush and context within a 15-second deadline, reserving at least 5 seconds
  for context. Session captures enforce provider roots, regular non-symlink files, size limits
  (`max_snapshot_bytes` up to `MAX_CLIENT_SNAPSHOT_BYTES`), and track client transcript generations
  in `transcripts`. Internal jobs (`KELMO_INTERNAL_JOB=1`) are recorded in `skips` and bypassed.
  `ClientService.reconcile` calls the server's reconciliation operation; candidate
  receipts are issued there. Plain-text `create` archives the original source
  and queues scoped editing. Shared `--wait` observes the worker without
  submitting a competing processing request. Explicit action updates use
  `state_apply`; the server enforces enrollment and evidence ownership.

### Snapshot and import

Stopped-copy snapshot, inventory, resumable import, cutover marking and forward recovery in
`src/kelmo/snapshot.py`. Replaces ad-hoc copies with double-read consistency barriers,
disposition tracking, step-by-step verified import into schema-2 vaults, and tail-aware disaster recovery.

- Stopped-copy barrier: `snapshot.take_snapshot(sources, destination)` captures named `sqlite`, `git`
  and `tree` sources into an empty directory while writers are stopped. It fingerprints every source
  before copying and re-fingerprints after all copies finish. If any source fingerprint changed
  during the operation, the destination is removed and `E_SNAPSHOT` is raised. SQLite sources are
  copied via `sqlite3.backup` from read-only connections and validated with `PRAGMA integrity_check`.
  Git repositories must be clean (`git status --porcelain`) and record HEAD commit, refs and file maps.
  Directories are scanned with `snapshot.tree_files`, recording `{relative: 'sha256:mode'}`. A symlink
  is never followed: a Git source records it as `link:<sha256 of its target text>` and the copy
  recreates the link itself; a plain `tree` source raises `E_SNAPSHOT` on one. The snapshot writes
  `manifest.json` with schema 1, a UUID
  and a SHA-256 digest of source entries. `snapshot.verify_snapshot` verifies the manifest and rechecks
  all copied files against disk.
- Inventory dispositions: `snapshot.inventory(snapshot, queues=())` assigns exactly one disposition
  to every item in the snapshot. Journal operations with archived evidence become `import`; terminal
  operations become `carry-unresolved`; in-flight operations become `resume-after-cutover`. Action
  requests in `open` or `prepared` state become `settle-before-cutover` and block cutover. Pending files
  in old writer queues become `drain-before-cutover` and block cutover. Archive records become `import`.
  Duplicate item identities raise `E_SNAPSHOT`.
- Resumable import steps and proofs: `snapshot.import_snapshot` unpacks a verified snapshot into a
  schema-2 vault with independent roots (`state_root`, `archive_root`, `knowledge_git_root`). Progress
  is recorded in `.import/state.json` before each subsequent step, so interrupted imports resume without
  repeating completed work:
  1. `vault`: places Git history into `knowledge_git_root`, proving file maps match.
  2. `archive`: places `archive.db` into `archive_root`, proving file SHA-256.
  3. `journal`: places `journal.sqlite3` into `state_root` or initializes a fresh journal.
  4. `sources`: optionally places captured source bytes into `state_root/sources`.
  5. `config`: writes schema-2 `kelmo.toml` with `[storage]` roots and carried settings from
     `snapshot.carried_settings`. Reserved settings (`schema_version`, `writer`, `vault_id`,
     `node_label`, `wiki_root`, `storage`) are refused if present in carried settings.
  6. `wiki`: claims the placed wiki for the importing authority (`engine.activate_wiki`, the
     `kelmo-owner.json` record inside the wiki's Git metadata), so publications after the import
     do not refuse with `E_WIKI_OWNER`; a copy already owned by another vault refuses here.
  7. `open`: instantiates `Service(control)` to perform schema upgrades and authority registration.
  8. `verify`: proves that archive record versions (`archive_census`), Git HEAD and refs (`_git`),
     journal operations and requests (`journal_census`) and source files (`tree_files`) match snapshot
     manifests exactly.
- Authority preservation: if `authority_id` is omitted, the imported vault retains the authority
  recorded in `archive_writer_state` (`_snapshot_authority`). Archives containing conflicting authorities
  raise `E_IMPORT`.
- Cutover baseline and tail: `snapshot.mark_cutover(control)` requires verified import completion and
  writes `.import/cutover.json`, recording snapshot ID, archive record census, Git HEAD, journal
  operations, action requests and timestamp. `snapshot.tail(control)` inspects writes accepted after
  cutover: new archive record versions, Git commits beyond the baseline HEAD, and new or updated journal
  operations and action requests.
- Rollback prevention and forward recovery: `snapshot.rollback_check(control)` raises `KelmoError('E_TAIL', ...)`
  when any post-cutover writes exist, refusing rollback to the pre-cutover snapshot. Post-cutover
  disaster recovery uses `snapshot.forward_recover(control, snapshot, destination, roots=...)`. It
  checks that no unfinished wiki transaction exists (`wiki-transaction.json`), captures a fresh snapshot
  of the live vault including its tail, imports it into a clean destination preserving authority ID and
  settings, verifies that no accepted tail writes were lost, and establishes a new cutover baseline.

## Fixture

`tests/fixtures/adoption/manifest.json` pins adoption/1 and the base revision.
It contains two exact versions of one synthetic ID, hidden/superseded bytes and an
owning receipt, a protected exception, nested wiki files, separate task/push/deploy
actions and a hash-bound generation manifest. Archive-only receipt generation is
null, distinct from the synthetic publication generation. No real data is present.

## Verification

Run from a standalone source checkout with the locked development dependencies
installed. Select that checkout's `src` explicitly. The focused fixtures use
temporary stores, synthetic embeddings and injected model failures; they do not
establish real-model retrieval or installed-client behavior.

```sh
env PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_adoption_contract.py tests/test_archive_boundary.py tests/test_archive_crash_recovery.py tests/test_journal_recovery.py
```

The original foundation selection passed 48 cases on macOS with
Python 3.12. It includes these four files and existing cases for archive receipt
retry, hidden public reads, failed supersession, source dates, lexical fallback
and session capture. Source-import identity, scoped Ruff and whitespace checks
also passed. Two child-process cases terminate at archive/supersession failpoints
and recover in fresh processes. Journal cases cover repair after a committed
hide, concurrent repeats, conflicting events and caller transaction preservation.

The archive extension adds same-ID history, exact source metadata, loss-reporting
imports, precise listing and error/replay boundaries. Its repeatable local selection
also covers the existing Service, daemon, visibility and retry callers:

```sh
env PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_archive_*.py tests/store/test_memory_crud.py tests/store/test_list_edge_cases.py tests/test_record_visibility.py tests/test_service.py tests/test_preservation_retry.py tests/test_ownership_recovery.py tests/test_outcomes.py tests/test_daemon.py tests/test_supersede.py tests/test_forget.py tests/test_journal_recovery.py
```

These fixtures use synthetic local stores and subprocess crashes. They do not
establish a production import, a remote embedding broker, Linux installation or
native client delivery.

Real local-model recall remains separate. Supply a provisioned model directory:

```sh
env PYTHONPATH=src KELMO_MODEL_DIR=/absolute/path/to/model .venv/bin/python tests/acceptance/a01_recall.py
```

The chunk index has its own test selection. Its Service cases use a synthetic
bag-of-words provider and a synthetic tokenizer: they establish wiring, write
and read boundaries, not real-model retrieval quality.

```sh
env PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_embedding_contract.py tests/test_retrieval_parity.py tests/test_retrieval_integration.py
```

The real pinned model through the same Service path runs separately and skips
without a provisioned model directory:

```sh
env PYTHONPATH=src KELMO_NATIVE_MODEL_DIR=/absolute/path/to/pinned/model .venv/bin/python -m pytest -q tests/test_retrieval_integration.py::test_pinned_model_probe
```

The existing source section in `gate.json` discovers these tests. Keep that single
coverage owner; additional overlapping sections are refused by the runner.

## Integration and open limits

The standalone runner and separate consumer mechanisms remain unchanged. This
foundation was exercised on isolated file/SQLite fixtures. No live archive,
production route, installed profile or wiki corpus was changed.

The offline wiki validator has its own scope. Its structural checks cannot
authenticate origins, hidden receipts or actual context delivery. Runtime fields
here retain the reviewed proposal names. StateAPI publication,
full retrieval/backfill, scoped semantic publication and shared transport are
implemented. Native isolation, installed-client delivery and deployment acceptance
need their own target-environment receipts.


Chunk-index maintenance is exposed through `kelmo admin backfill` and `reembed`;
it is not run implicitly after model selection. A broker service must report
`input.special_tokens` and bind its
transforms to the declared input path, and existing vaults need `[embedding.input]`
before they can use a broker; `archive_sources.vectors_pending` records the
state at admission and can be stale after a backfill (the head value is computed
live); the record-index keyword lane for unindexed records still compares date
strings.

These results do not establish Linux, real embedding-model or production
readiness. Public licensing, distribution and production cutover retain their
separate gates.
