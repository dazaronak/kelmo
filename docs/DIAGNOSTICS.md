# Status and health

`kelmo status --json` returns a `result.summary` object with stable counters:

| Field | Meaning |
| --- | --- |
| total | All recorded source operations |
| pending | Nonterminal work, excluding unscheduled captures and review queues |
| captured | Sources captured but not scheduled |
| applied | Sources with a settled applied outcome |
| archive_only | Sources settled without publishing new facts |
| needs_review | Operations awaiting resolution or manual review |
| failed | Operations currently in the failed state |

Detailed stage counts remain in `operations` and disposition counts in
`outcomes`. These are different views of progress; a stored publication outcome
can precede the final processing receipt after a crash. Do not sum them to infer
that no work remains. Inspect one operation with `kelmo trace OPERATION --json`.

For an authority after writer rotation, include `--server-profile PROFILE`.
A client directory routes status through its enrolled channel. Use
`kelmo client status --probe --json` to check the pinned server identity
without sending pending writes.

`kelmo doctor --json` checks the whole local vault. Its `result.checks` is an
object of named booleans; `healthy` summarizes it. Warnings and named issues
explain failed checks. It probes the configured embedding provider with one
synthetic input, or the legacy record model when no profile is selected.

`kelmo admin doctor --json` checks the archive API. Its `result.checks` is a
list of named checks with PASS, WARN, FAIL or SKIP status; `result.status`
summarizes healthy, degraded or failed. Embedding calls require
`--probe-embedding` on this administrative command. The two commands have
different scopes and retain their existing response formats.

Neither health response establishes deployment acceptance. Keep target-host
runtime, service and fresh-agent checks with the deployment receipt.
