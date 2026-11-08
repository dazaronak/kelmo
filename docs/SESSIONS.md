# Automatic session memory

Session memory keeps two outputs: the complete generated note in the archive, and optional
durable facts in wiki. The configured local worker may propose facts; a model
cannot grant itself write authority or supply trusted revisions and receipts.
Raw conversation snapshots stay separately in private service storage outside wiki
Git. They are not the generated archive notes.

## Setup

Enable capture explicitly during installation or setup:

```sh
kelmo setup --vault /path/to/vault --executable /path/to/runtime/bin/kelmo \
  --disable-native-memory --client both --provider codex --model YOUR_MODEL \
  --session-memory auto
```

Use the provider/model available in your installation. `--extractor-provider` and
`--extractor-model` optionally select a separate extraction model and must be supplied
together. Otherwise extraction uses the selected executor. Custom executors must
implement extraction and reconciliation tasks and pass the installation probe.

| Mode           | Behavior                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------- |
| `auto`         | Archive the complete note, then apply checked facts or leave wiki unchanged.                 |
| `archive-only` | Archive the complete note without proposing wiki facts.                                      |
| `off`          | Capture nothing and leave pending session work paused. HOT/manual commands remain available. |

A fresh setup defaults to off. An upgrade without the flag preserves the existing
mode and settings. Explicit mode selection clears a temporary pause. Setup backs
up modified files, preserves unrelated client settings and refuses changed owned
hooks. It replaces the legacy Stop collector with the session lifecycle hooks.

The selected executor processes synthetic decision and history sessions in a
temporary vault before hooks are enabled. `extraction_ready` and
`session_pipeline_ready` report that probe. `native_hooks_verified` stays false
until enrolled clients deliver lifecycle events; synthetic execution does not
prove client hook trust or delivery. `--no-start` prepares files but does not claim
background readiness. Restart clients and approve native hook trust when requested.

## Capture and processing

SessionStart supplies configured HOT context. Stop captures a checkpoint after 30
new user turns; PreCompact forces one. SessionEnd queues finalization. Hooks only
capture evidence and schedule work. The existing worker performs model calls.

Reads are restricted to enrolled provider roots, initially `.claude/projects` and
`.codex/sessions` under the selected home. A transcript path may pass through a
link, but the resolved file must be a regular file inside an enrolled root; special
files, malformed complete records and unsupported transcript shapes fail explicitly. A partial
last JSONL record remains in the raw snapshot and waits for a later complete
capture. The snapshot must be durable before its job is recorded.

A transcript has no size ceiling. A shared client sends it in pieces of 8 MiB from
the position it last delivered (`session_append`), so a capture costs the bytes
added since the previous one. The server extends a per-session spool and refuses a
piece whose base it does not hold (`E_CAPTURE_BASE`); the client then sends the
transcript again from its start. A snapshot that extends the previous snapshot of
its generation stores only its delta (`session_snapshot_storage`), and
`Sessions.snapshot` rebuilds the exact bytes and checks them against the identity.
Hooks only queue the capture and return; a detached `client flush` carries it to
the server, so a provider's hook timeout never cuts a delivery. Extraction chunks
are 128 KiB by default (`max_input_bytes`, at most one MiB) and a generated note at one MiB. `extraction = "tail"` (the
default) gives each job one model call over the newest messages that fill one
chunk; the call and the saved result carry `omitted_pieces`, the count of older
pieces left out, so a tail is never presented as the whole conversation.
`extraction = "complete"` splits every selected message into recorded chunks, one
model call each, at most `max_calls` (default 8) per job: past it, the newest
chunks are read and the older pieces are counted in `omitted_pieces`.

Before either mode, extraction reads the conversation whole and tool traffic short,
because tool traffic is most of a working session's bytes and little of what a note
needs: a tool call becomes one line (its name and the argument that says what it
did, at most `tool_stub_chars`, default 200), a result stays whole up to
`tool_result_chars` (default 400), an error result (the provider's error flag, or a
traceback, `Error:` or a nonzero exit in its text) up to `tool_error_chars` (default
1500), and any other result becomes `[tool result, N chars omitted]`. Every
shortened message says what was left out.

Finalization reads the complete captured generation; checkpoints process uncovered
ranges. The snapshot keeps the whole transcript in either mode.
The extractor produces a note and optional candidate facts. It receives the
registered wiki pages as `pages` (sorted, at most 300) and names a candidate's
target from that list; a fact no listed page fits stays in the note only. A valid
note is archived even when its candidates are invalid, uncertain or rejected.

The worker retrieves existing facts, reconciles candidates and checks real search
receipts and current revisions before applying changes. The checked proposal is
published through the scoped editor as declared changes (no
model call): one editor request per session application, keyed by the application
identity and bound to the proposal digest, so a changed proposal under the same
identity is refused and a crash after publication replays the receipt. Automatic
session facts never carry rule-change authority. Empty lexical search is
not proof of novelty. Uncertain decisions require review. This protects durable
replay and stale writes; it does not guarantee semantic accuracy or zero paraphrase
duplicates.

Repeated events reuse jobs by provider, node, session, generation, range and task.
Transcript replacement or truncation starts a new generation. Older checkpoints
cannot overwrite a completed later range. Saved extraction and archive receipts
survive retries; a crash after wiki application does not repeat that application.
Retries are bounded and persist their attempt count across restarts.

## Operations

```sh
kelmo sessions list --vault /path/to/vault --json
kelmo sessions show JOB_ID --vault /path/to/vault --json
kelmo sessions retry JOB_ID --vault /path/to/vault --json
kelmo sessions run --vault /path/to/vault --json
kelmo sessions pause --vault /path/to/vault
kelmo sessions resume --vault /path/to/vault
kelmo doctor --vault /path/to/vault --json
```

Inspection shows job identity, coverage, attempts, archive references and error
codes without printing transcript content. Completed outcomes are `wiki_applied`,
`wiki_unchanged` or `skipped`. Exhausted extraction/archive work is `needs_review`;
an archived note with an unresolved wiki proposal is `wiki_needs_review`.
Resolve the reported cause before retrying. Retries retain saved notes and receipts.

A rate-limited provider answers `{"outcome":"quota_wait","reset_at":EPOCH_SECONDS}`.
The job returns to `pending` with its next attempt at that time and the attempt is
refunded, so a quota wait never spends the retry budget; the job reports
`error_code = "E_QUOTA"` and `quota_reset_at`.

Pause stops new capture and future worker ticks; it does not cancel an in-flight
model request. Resume clears a pause but cannot enable mode off: use setup for that.
Setup with `--session-memory off` removes session hooks. Client-only uninstall
removes its owned hooks while retaining worker, runtime and all data, so already
queued work may finish unless paused first.

`kelmo.toml` stores `[sessions]` and `[sessions.sources]`: mode, paused,
checkpoint_turns, max_input_bytes, extraction (`tail` or `complete`), max_calls,
tool_stub_chars, tool_result_chars, tool_error_chars, optional
executor_config (`max_snapshot_bytes` is still read from older files and no longer
limits anything),
and provider root lists. Journal upgrades add versioned tables after a consistent
backup and reject unknown versions. No historical transcript import, automatic
deletion or retention cleanup runs during installation, upgrades or removal.

## Review before clearing

For a manual `kelmo sessions run`, an explicit `--executor-config` overrides the
saved session extractor. Without that option the saved session extractor is used,
then the default resolution executor if no session extractor is configured.
Background processing continues to prefer the saved session extractor.

Run `kelmo sessions review --vault /path/to/vault --json` to inspect pending and
failed processing without changing the queue. Without a session identity this is
only a queue summary; it cannot establish coverage of the current conversation.

```sh
kelmo sessions review --vault /path/to/vault --provider claude \
  --session-id YOUR_SESSION_ID --transcript /enrolled/path/session.jsonl --json
```

Use `--provider codex` for an enrolled Codex transcript. The check verifies the
preserved snapshot hash, compares current transcript bytes when a path is supplied,
and includes unresolved jobs from previous generations of the same session. It
does not initialize databases, capture new bytes, retry work or invoke a model.
The output contains metadata and job IDs, not conversation excerpts.

`capture.status` distinguishes a captured snapshot, uncaptured changes and missing
capture. `processing` separates pending jobs from failures. Captured records below
the scheduling threshold appear in `unscheduled_records`; a preserved partial JSONL
record appears as `incomplete_tail`. Both keep processing pending. A pending job
can still have its complete input preserved. `current_transcript_checked` says whether the
current file was compared; omitting the path verifies only the recorded snapshot.
`semantic_completeness_guaranteed` is always false, even when processing is complete.

Existing PreCompact hooks capture a checkpoint when the native client delivers the
event. The review command is an explicit advisory, not a hook that blocks clearing
or assumes the client displays PreCompact output. Run `kelmo sessions show JOB_ID`
for processing detail and `kelmo sessions retry JOB_ID` after resolving a failure.
