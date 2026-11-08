# External resolution

Fact preparation conflicts create a durable needs_resolution operation after the
archive receipt exists. Transaction-recovery conflicts remain failures requiring
intervention, rather than being delegated as semantic questions.

Use `kelmo resolution list`, `kelmo resolution claim --json`, and
`kelmo resolution submit JOB --token TOKEN --json` with a JSON proposal on stdin.
The executor receives the complete immutable source, proposed changes and current
facts in their permitted key scope, plus source hash and archive ID, and
`candidates`: for each new fact key, the stored facts with similar text. It returns
changes using the existing wiki-change schema, or {"unresolved":true} when a new
fact restates a candidate. A new fact is accepted only against the candidates the
executor read; if they changed meanwhile, its answer is dropped and the job is
asked again (`E_STALE`). The token is a local concurrency token,
not an authentication credential for a remote API.

The worker checks schema, exact key scope, target pages and registry revisions.
A rejected proposal names the fact it belongs to, its length and its page where
those are valid, and the repair that is permitted; a value that fails its own shape
check is not echoed back. A change to a fact in the resolution's key scope moves the
operation to needs_review. Unrelated registry updates do not invalidate the request. These structural checks do
not prove semantic preservation. Claim expiry allows another attempt; an old token
cannot complete it. Responses are accepted only after schema and revision validation,
then saved before wiki commit. Invalid proposals can be corrected on another
executor attempt. Accepted responses can finish
journal recovery without another archive write. No model runs while the worker
lock is held. This protocol does not yet provide automatic semantic deduplication.

A validated resolution goes through the same scoped
editor as ordinary creation. The live resolution claim permits only its saved
response; other editor calls remain blocked while that workflow owns the source.
Publication runs after releasing the worker lock and keeps the original archived
source unchanged. Restart after publication completes the journal without
publishing the proposal again. Existing fact checks use the current revision.
A new fact with reconciliation candidates is refused for identity review.

A rate-limited executor answers `{"outcome":"quota_wait","reset_at":EPOCH_SECONDS}`.
That answer is never taken as a proposal: submitting it returns `E_QUOTA`, and the
worker returns the job to pending with `next_attempt` at the reset time, refunds the
claim and leaves `error = "E_QUOTA"`. A quota wait spends no attempt and settles no
outcome; the operation stays `needs_resolution`.

Use `kelmo resolution reopen JOB --json` after inspecting a reviewed job. It
retains the original source and archive receipt. If no response was accepted, it
refreshes the request against current revisions. If a validated response exists,
reopening preserves it for commit recovery and rejects replacement responses.
Reopening resets the attempt budget and records an event in the operation trace.
Submitting a proposal that needs review returns a nonzero exit status, its
resolution ID and a reopen command. This reports an unresolved outcome rather
than claiming the proposed fact was applied.

New-fact reconciliation receipts track the returned lexical candidate revisions.
They remain valid when unrelated facts change. A changed candidate set requires
fresh reconciliation or resolution; the receipt does not prove semantic absence.
